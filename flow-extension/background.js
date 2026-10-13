/**
 * Flow Agent — Chrome Extension Background Service Worker
 *
 * Connects to local Python agent via WebSocket (agent runs WS server).
 * Captures bearer token, solves reCAPTCHA, proxies API calls through browser.
 */

importScripts('config.js');

let callbackUrl = 'http://127.0.0.1:3001/api/ext/callback';
// NOTE: This is a browser-restricted public API key — safe to ship in extension bundles.
const API_KEY = 'AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY';

let ws = null;
let flowKey = null;
let callbackSecret = null;  // Auth secret for HTTP callback, received from server on WS connect
let httpConnected = false;
let httpPollTimer = null;
let httpPollIntervalMs = 1000;
let state = 'off'; // off | idle | running
let manualDisconnect = false;
let extensionClientId = '';
let connectedServerHost = CONFIG.DEFAULT_SERVER_HOST;

function normalizeCallbackUrl(value) {
  try {
    const raw = String(value || '').trim();
    const parsed = new URL(/^https?:\/\//i.test(raw) ? raw : `https://${raw}`);
    const local = /^(localhost|127\.0\.0\.1|192\.168\.|10\.)/.test(parsed.hostname);
    parsed.protocol = local ? 'http:' : 'https:';
    parsed.pathname = '/api/ext/callback';
    parsed.search = '';
    parsed.hash = '';
    return parsed.toString().replace(/\/$/, '');
  } catch {
    return 'http://127.0.0.1:8001/api/ext/callback';
  }
}
let metrics = {
  tokenCapturedAt: null,
  requestCount: 0,   // captcha-consuming requests only (gen image/video/upscale)
  successCount: 0,
  failedCount: 0,
  lastError: null,
};

// ─── URL → Log Type Classifier ─────────────────────────────

// Visible log types — only these appear in the request log
const _VISIBLE_TYPES = new Set(['GEN_IMG', 'GEN_VID', 'GEN_VID_REF', 'UPSCALE', 'TRACKING', 'URL_REFRESH']);

function _classifyApiUrl(url) {
  if (url.includes('uploadImage')) return 'UPLOAD';
  if (url.includes('batchGenerateImages')) return 'GEN_IMG';
  if (url.includes('UpsampleVideo')) return 'UPSCALE';
  if (url.includes('ReferenceImages')) return 'GEN_VID_REF';
  if (url.includes('batchAsyncGenerateVideo')) return 'GEN_VID';
  if (url.includes('batchCheckAsync')) return 'POLL';
  if (url.includes('upsampleImage')) return 'UPS_IMG';
  if (url.includes('/media/')) return 'MEDIA';
  if (url.includes('/credits')) return 'CREDITS';
  return 'API';
}

// ─── Request Log ────────────────────────────────────────────

let requestLog = [];

function addRequestLog(entry) {
  requestLog.unshift(entry);
  if (requestLog.length > 100) requestLog.pop();
  chrome.storage.local.set({ requestLog }).catch(() => {});
  broadcastRequestLog();
}

function updateRequestLog(id, updates) {
  const entry = requestLog.find((e) => e.id === id);
  if (entry) Object.assign(entry, updates);
  chrome.storage.local.set({ requestLog }).catch(() => {});
  broadcastRequestLog();
}

function broadcastRequestLog() {
  chrome.runtime.sendMessage({ type: 'REQUEST_LOG_UPDATE', log: requestLog }).catch(() => { });
}

// ─── Startup ────────────────────────────────────────────────

let initialization;
function ensureInitialized() {
  if (!initialization) initialization = init().catch((error) => {
    initialization = null;
    console.error('[Flow Agent] Initialization failed:', error);
  });
  return initialization;
}

chrome.runtime.onInstalled.addListener(ensureInitialized);
chrome.runtime.onStartup.addListener(ensureInitialized);
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'reconnect') connectToAgent();
  if (alarm.name === 'keepAlive') keepAlive();
  if (alarm.name === 'flushOutbox') flushOutbox();
  if (alarm.name === 'closeIdleFlowTab') await closeIdleFlowTab();
});

async function init() {
  if (chrome.sidePanel?.setPanelBehavior) {
    try {
      await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
    } catch (error) {
      console.warn('[Flow Agent] Side Panel click behavior unavailable:', error.message);
    }
  }
  try {
    if (chrome.declarativeNetRequest?.updateEnabledRulesets) {
      await chrome.declarativeNetRequest.updateEnabledRulesets({ enableRulesetIds: ['referer_rules'] });
    }
    if (chrome.declarativeNetRequest?.updateDynamicRules) {
      await chrome.declarativeNetRequest.updateDynamicRules({
        removeRuleIds: [1, 100],
        addRules: [{
          id: 100,
          priority: 100,
          action: {
            type: 'modifyHeaders',
            requestHeaders: [
              { header: 'Origin', operation: 'set', value: 'https://labs.google' },
              { header: 'Referer', operation: 'set', value: 'https://labs.google/' }
            ]
          },
          condition: {
            urlFilter: '*://aisandbox-pa.googleapis.com/*',
            resourceTypes: ['xmlhttprequest', 'other']
          }
        }]
      });
    }
  } catch (e) {
    console.warn('[Flow Agent] DNR rule update warning:', e.message);
  }
  await chrome.storage.local.remove('customServerIp');
  const data = await chrome.storage.local.get(['flowKey', 'metrics', 'callbackSecret', 'callbackUrl', 'requestLog']);
  if (data.flowKey) flowKey = data.flowKey;
  if (data.metrics) Object.assign(metrics, data.metrics);
  if (data.callbackSecret) callbackSecret = data.callbackSecret;
  if (data.callbackUrl) callbackUrl = normalizeCallbackUrl(data.callbackUrl);
  if (Array.isArray(data.requestLog)) requestLog = data.requestLog.slice(0, 100);
  await loadOutbox();
  connectToAgent();
  // 0.5 min is Chrome's minimum alarm period — anything lower is silently clamped.
  chrome.alarms.create('keepAlive', { periodInMinutes: 0.5 });
  // Retry any responses left undelivered by a previous worker lifetime.
  chrome.alarms.create('flushOutbox', { periodInMinutes: 0.5 });
  flushOutbox();
}

ensureInitialized();

// ─── Token Capture ──────────────────────────────────────────

function handleCapturedHeaders(details) {
  if (!details?.requestHeaders?.length) return;
  const authHeader = details.requestHeaders.find(
    (h) => h.name?.toLowerCase() === 'authorization',
  );
  const value = authHeader?.value || '';
  const match = value.match(/^Bearer\s+(ya29\.[^\s]+)/i);
  if (!match) return;

  const token = match[1].trim();
  if (!token) return;

  flowKey = token;
  metrics.tokenCapturedAt = Date.now();
  chrome.storage.local.set({ flowKey, metrics });
  console.log('[Flow Agent] Bearer token captured from webRequest');
  sendToAgent({ type: 'token_captured', flowKey, clientId: extensionClientId });
}

try {
  chrome.webRequest.onBeforeSendHeaders.addListener(
    handleCapturedHeaders,
    { urls: [
      'https://aisandbox-pa.googleapis.com/*',
      'https://aisandbox-pa.sandbox.googleapis.com/*',
      'https://labs.google/*',
      'https://flow.google.com/*',
      'https://flow-content.google/*'
    ] },
    ['requestHeaders', 'extraHeaders'],
  );
  console.log('[Flow Agent] Registered onBeforeSendHeaders with extraHeaders');
} catch (e) {
  console.warn('[Flow Agent] Fallback to requestHeaders without extraHeaders:', e.message);
  chrome.webRequest.onBeforeSendHeaders.addListener(
    handleCapturedHeaders,
    { urls: [
      'https://aisandbox-pa.googleapis.com/*',
      'https://aisandbox-pa.sandbox.googleapis.com/*',
      'https://labs.google/*',
      'https://flow.google.com/*',
      'https://flow-content.google/*'
    ] },
    ['requestHeaders'],
  );
}

let _openingFlowTab = false;

// ─── On-demand tab lifecycle ────────────────────────────────
// Open the Flow tab only when real work needs it (token capture or captcha).
// Keep it available in the background so user tabs are never redirected.
const FLOW_TAB_URLS = ['https://flow.google.com/*', 'https://labs.google/fx/tools/flow*', 'https://labs.google/fx/*/tools/flow*'];
const LABS_FLOW_URL = 'https://labs.google/fx/tools/flow';
const FLOW_URL = 'https://flow.google.com';
let workTabId = null;
let flowTabOpening = null;
let workTabCreatedByExtension = false;

function scheduleFlowTabClose() {
  if (workTabCreatedByExtension) {
    chrome.alarms.create('closeIdleFlowTab', { delayInMinutes: 2 });
  }
}

async function closeIdleFlowTab() {
  if (!workTabId || !workTabCreatedByExtension) return;
  if (state === 'running') {
    scheduleFlowTabClose();
    return;
  }
  const tabId = workTabId;
  workTabId = null;
  workTabCreatedByExtension = false;
  try {
    await chrome.tabs.remove(tabId);
  } catch { /* tab was already closed */ }
}

function isFlowUrl(url) {
  return !!url && FLOW_TAB_URLS.some((p) => new RegExp(p.replace(/\./g, '\\.').replace(/\*/g, '.*')).test(url));
}

async function waitForTabComplete(tabId, maxWaitMs = 10000) {
  return new Promise((resolve) => {
    const start = Date.now();
    function listener(updatedTabId, changeInfo, tab) {
      if (updatedTabId === tabId && changeInfo.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(tab);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      chrome.tabs.get(tabId).then(resolve).catch(() => resolve(null));
    }, maxWaitMs);
  });
}

async function getExistingLabsTab() {
  const allTabs = await chrome.tabs.query({});
  return allTabs.find(t => t.url && (t.url.includes('labs.google/fx/tools/flow') || t.url.includes('labs.google/fx')));
}

async function getExistingFlowTab() {
  const allTabs = await chrome.tabs.query({});
  const labsTab = allTabs.find(t => t.url && (t.url.includes('labs.google/fx/tools/flow') || t.url.includes('labs.google/fx')));
  if (labsTab) return labsTab;
  return allTabs.find(t => t.url && (t.url.includes('flow.google.com') || t.url.includes('labs.google')));
}

// Finds/wakes/creates the Flow tab. Returns
// the tab, or null if it couldn't be opened.
async function _getOrOpenFlowTab() {
  if (workTabId !== null) {
    try {
      let tab = await chrome.tabs.get(workTabId);
      if (tab && !isFlowUrl(tab.url)) {
        await chrome.tabs.update(workTabId, { url: FLOW_URL });
        await waitForTabComplete(workTabId);
        tab = await chrome.tabs.get(workTabId);
      }
      scheduleFlowTabClose();
      return tab;
    } catch (e) {
      workTabId = null; // closed by the user — fall through and open fresh
    }
  }

  const existingTab = await getExistingFlowTab();
  if (existingTab) {
    workTabId = existingTab.id;
    workTabCreatedByExtension = false;
    return existingTab;
  }

  const createdTab = await chrome.tabs.create({ url: FLOW_URL, active: false });
  workTabId = createdTab.id;
  workTabCreatedByExtension = true;
  await waitForTabComplete(workTabId);
  await sleep(1500);

  // Inject content script to make sure reCAPTCHA bridge is ready
  try {
    await chrome.scripting.executeScript({
      target: { tabId: workTabId },
      files: ['content.js'],
    });
  } catch (e) {
    console.warn('[Flow Agent] Content script pre-injection:', e.message);
  }

  scheduleFlowTabClose();
  return createdTab;
}

async function getOrOpenFlowTab() {
  if (flowTabOpening) return flowTabOpening;
  flowTabOpening = _getOrOpenFlowTab();
  try {
    return await flowTabOpening;
  } finally {
    flowTabOpening = null;
  }
}

// Token is considered fresh if it exists and was captured less than 50 minutes ago.
// Google OAuth tokens expire after ~60 min, so 50 min gives a safe buffer.
function isTokenFresh() {
  if (!flowKey || !metrics.tokenCapturedAt) return false;
  const ageMs = Date.now() - metrics.tokenCapturedAt;
  return ageMs < 50 * 60 * 1000; // 50 minutes
}

async function refreshLabsSessionToken() {
  console.log('[Flow Agent] Initiating NextAuth OAuth session refresh...');
  try {
    const csrfResp = await fetch('https://labs.google/fx/api/auth/csrf', { credentials: 'include' });
    if (!csrfResp.ok) throw new Error(`CSRF fetch failed: HTTP ${csrfResp.status}`);
    const csrfData = await csrfResp.json();
    const csrfToken = csrfData?.csrfToken;
    if (!csrfToken) throw new Error('No csrfToken found');

    const signinResp = await fetch('https://labs.google/fx/api/auth/signin/google', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        csrfToken,
        callbackUrl: 'https://labs.google/fx/tools/flow',
        json: 'true',
      }),
    });
    if (!signinResp.ok) throw new Error(`Signin request failed: HTTP ${signinResp.status}`);
    const signinData = await signinResp.json();
    const oauthUrl = signinData?.url;
    if (!oauthUrl) throw new Error('No OAuth redirect URL returned from signin');

    console.log('[Flow Agent] Opening background tab for OAuth handshake...');
    const tab = await chrome.tabs.create({ url: oauthUrl, active: false });

    await new Promise((resolve) => {
      let resolved = false;
      const timeout = setTimeout(() => {
        if (!resolved) {
          resolved = true;
          chrome.tabs.onUpdated.removeListener(listener);
          resolve();
        }
      }, 15000);

      function listener(tabId, changeInfo, updatedTab) {
        if (tabId === tab.id && changeInfo.status === 'complete') {
          const currentUrl = updatedTab.url || '';
          if (currentUrl.includes('labs.google/fx') || currentUrl.includes('flow.google.com')) {
            if (!resolved) {
              resolved = true;
              clearTimeout(timeout);
              chrome.tabs.onUpdated.removeListener(listener);
              resolve();
            }
          }
        }
      }
      chrome.tabs.onUpdated.addListener(listener);
    });

    try {
      await chrome.tabs.remove(tab.id);
    } catch {}

    await sleep(1000);

    const sessionResp = await fetch('https://labs.google/fx/api/auth/session', {
      credentials: 'include',
      headers: { 'Accept': 'application/json' },
    });
    if (!sessionResp.ok) throw new Error(`Session fetch failed: HTTP ${sessionResp.status}`);
    const sessionData = await sessionResp.json();
    if (sessionData?.access_token && sessionData.access_token.startsWith('ya29.')) {
      flowKey = sessionData.access_token;
      metrics.tokenCapturedAt = Date.now();
      await chrome.storage.local.set({ flowKey, metrics });
      sendToAgent({ type: 'token_captured', flowKey, clientId: extensionClientId });
      console.log('[Flow Agent] Captured fresh OAuth token after refresh:', flowKey.slice(0, 15) + '...');
      return flowKey;
    }
  } catch (err) {
    console.warn('[Flow Agent] refreshLabsSessionToken error:', err.message);
  }
  return null;
}

async function fetchTokenFromSession() {
  try {
    const resp = await fetch('https://labs.google/fx/api/auth/session', {
      credentials: 'include',
      headers: { 'Accept': 'application/json' },
    });
    if (!resp.ok) return null;
    const data = await resp.json().catch(() => null);

    let isExpired = false;
    if (data?.expires) {
      const expTime = new Date(data.expires).getTime();
      if (!isNaN(expTime) && expTime <= Date.now() + 60000) {
        isExpired = true;
      }
    }

    if (data?.access_token && data.access_token.startsWith('ya29.') && !isExpired) {
      flowKey = data.access_token;
      metrics.tokenCapturedAt = Date.now();
      await chrome.storage.local.set({ flowKey, metrics });
      sendToAgent({ type: 'token_captured', flowKey, clientId: extensionClientId });
      console.log('[Flow Agent] Captured valid OAuth token from labs auth session');
      return flowKey;
    }

    console.log('[Flow Agent] Session token missing or expired, triggering OAuth refresh...');
    return await refreshLabsSessionToken();
  } catch (e) {
    console.warn('[Flow Agent] fetchTokenFromSession error:', e.message);
  }
  return null;
}

async function captureTokenFromFlowTab() {
  if (isTokenFresh()) {
    console.log('[Flow Agent] Token still fresh, skipping tab refresh');
    return flowKey;
  }

  const sessionToken = await fetchTokenFromSession();
  if (sessionToken) return sessionToken;

  if (_openingFlowTab) {
    console.log('[Flow Agent] Flow tab already opening, skipping');
    return null;
  }
  _openingFlowTab = true;
  try {
    let tab = await getExistingLabsTab();
    if (tab) {
      console.log('[Flow Agent] Found existing labs.google tab, reloading:', tab.id);
      await chrome.tabs.reload(tab.id);
      await waitForTabComplete(tab.id, 15000);
    } else {
      console.log('[Flow Agent] No labs.google tab found, creating background tab on labs.google for token capture');
      tab = await chrome.tabs.create({ url: LABS_FLOW_URL, active: false });
      workTabId = tab.id;
      workTabCreatedByExtension = true;
      await waitForTabComplete(tab.id, 15000);
    }
    await sleep(2000);
    return await fetchTokenFromSession();
  } catch (e) {
    console.error('[Flow Agent] Token refresh failed:', e);
  } finally {
    _openingFlowTab = false;
  }
  return null;
}


// ─── WebSocket to Agent ─────────────────────────────────────

async function connectToAgent() {
  if (manualDisconnect) return;
  await connectHttpAgent();
  if (ws?.readyState === WebSocket.CONNECTING) return;
  if (ws?.readyState === WebSocket.OPEN) return;

  const data = await chrome.storage.local.get(['clientId']);
  const serverIp = CONFIG.DEFAULT_SERVER_HOST;
  connectedServerHost = serverIp;
  const isLocal = /^(127\.0\.0\.1|localhost|192\.168\.|10\.)/.test(serverIp);
  const wsScheme = isLocal ? 'ws' : 'wss';
  const httpScheme = isLocal ? 'http' : 'https';
  const wsUrl = `${wsScheme}://${serverIp}/ws`;

  // Dynamically resolve callbackUrl
  callbackUrl = `${httpScheme}://${serverIp}/api/ext/callback`;

  try {
    ws = new WebSocket(wsUrl);
  } catch (e) {
    console.error('[Flow Agent] WS connect error:', e);
    scheduleReconnect();
    return;
  }

  ws.onopen = async () => {
    console.log('[Flow Agent] Connected to agent: ' + wsUrl);
    chrome.alarms.clear('reconnect');
    setState('idle');

    const storage = await chrome.storage.local.get(['clientId']);
    let clientId = storage.clientId;
    if (!clientId) {
      const prefix = CONFIG.DEFAULT_CLIENT_ID_PREFIX || 'client';
      clientId = `${prefix}-${Math.random().toString(36).substring(2, 8)}`;
      await chrome.storage.local.set({ clientId });
    }
    extensionClientId = clientId;

    // Send current state + resend token if we have one, along with clientId
    ws.send(JSON.stringify({
      type: 'extension_ready',
      clientId: clientId,
      flowKeyPresent: !!flowKey,
      tokenAge: flowKey && metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
    }));
    if (flowKey) {
      ws.send(JSON.stringify({
        type: 'token_captured',
        clientId: clientId,
        flowKey: flowKey
      }));
    }
    // Backend is reachable again — push any responses queued while it was down.
    flushOutbox();
  };

  ws.onmessage = async ({ data }) => {
    try {
      const msg = JSON.parse(data);
      await handleAgentCommand(msg);
    } catch (e) {
      console.error('[Flow Agent] Message error:', e);
    }
  };

  ws.onclose = () => {
    setState('off');
    if (!manualDisconnect) scheduleReconnect();
  };

  ws.onerror = (e) => {
    console.error('[Flow Agent] WS error:', e);
    metrics.lastError = 'WS_ERROR';
    chrome.storage.local.set({ metrics });
  };
}

async function handleAgentCommand(msg) {
  if (!msg) return;
  try {
    if (msg.method === 'api_request') {
      await handleApiRequest(msg);
    } else if (msg.method === 'get_media_url') {
      await handleGetMediaUrl(msg);
    } else if (msg.method === 'trpc_request') {
      await handleTrpcRequest(msg);
    } else if (msg.method === 'upload_video') {
      await handleUploadVideo(msg);
    } else if (msg.method === 'solve_captcha') {
      await handleSolveCaptcha(msg);
    } else if (msg.method === 'reload_extension') {
      chrome.runtime.reload();
    } else if (msg.method === 'open_tab') {
      const tab = await chrome.tabs.create({ url: msg.params?.url || 'https://flow.google.com', active: true });
      sendToAgent({ id: msg.id, result: { ok: true, tabId: tab.id, url: tab.url } });
    } else if (msg.method === 'eval_tab') {
      const { tabId, urlPattern, action = 'GET_PAGE_INFO', payload = {} } = msg.params || {};
      try {
        let targetId = tabId;
        if (!targetId) {
          const allTabs = await chrome.tabs.query({});
          const pattern = urlPattern || 'flow.google.com/project';
          const found = allTabs.find(t => t.url && t.url.includes(pattern));
          if (found) targetId = found.id;
          else if (allTabs.length) targetId = allTabs[0].id;
        }
        if (!targetId) throw new Error('No matching tab found');

        const results = await chrome.scripting.executeScript({
          target: { tabId: targetId },
          world: 'MAIN',
          func: async (act, pl, key) => {
            try {
              if (act === 'GET_PAGE_INFO') {
                const buttons = Array.from(document.querySelectorAll('button, div[role="button"]')).map(b => ({
                  text: b.innerText?.trim()?.slice(0, 50),
                  ariaLabel: b.getAttribute('aria-label'),
                  role: b.getAttribute('role'),
                  disabled: b.disabled || b.getAttribute('aria-disabled') === 'true'
                })).filter(b => b.text || b.ariaLabel);
                const inputs = Array.from(document.querySelectorAll('textarea, input')).map(i => ({
                  tag: i.tagName,
                  placeholder: i.placeholder,
                  value: i.value?.slice(0, 50),
                  ariaLabel: i.getAttribute('aria-label')
                }));
                return {
                  success: true,
                  result: {
                    url: window.location.href,
                    title: document.title,
                    buttons: buttons.slice(0, 50),
                    inputs: inputs.slice(0, 20),
                  }
                };
              }
              if (act === 'GET_PERF_ENTRIES') {
                const entries = performance.getEntriesByType('resource')
                  .map(r => r.name)
                  .filter(u => u.includes('googleapis') || u.includes('/fx/') || u.includes('/api/') || u.includes('flow-content') || u.includes('/asb/'));
                return { success: true, result: entries.slice(-50) };
              }
              if (act === 'HOVER') {
                const { selector } = pl;
                const els = Array.from(document.querySelectorAll(selector || 'img.thumbnail'));
                els.forEach(target => {
                  let curr = target;
                  for (let d = 0; d < 5 && curr; d++) {
                    ['pointerenter', 'pointerover', 'mouseenter', 'mouseover', ' pointermove', 'mousemove'].forEach(ev => {
                      curr.dispatchEvent(new PointerEvent(ev.trim(), { bubbles: true, cancelable: true, composed: true }));
                      curr.dispatchEvent(new MouseEvent(ev.trim(), { bubbles: true, cancelable: true, composed: true }));
                    });
                    curr = curr.parentElement;
                  }
                });
                return { success: true, result: { hovered: els.length } };
              }
              if (act === 'GENERATE_VIDEO_NATIVE') {
                const { prompt, aspect, duration, startImageId, endImageId, projectId, seed } = pl;
                const siteKey = '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV';
                let recaptchaToken = '';
                if (window.grecaptcha?.enterprise?.execute) {
                  recaptchaToken = await window.grecaptcha.enterprise.execute(siteKey, { action: 'VIDEO_GENERATION' });
                } else if (window.grecaptcha?.execute) {
                  recaptchaToken = await window.grecaptcha.execute(siteKey, { action: 'VIDEO_GENERATION' });
                }
                const modelKey = `abra_t2v_${duration}s`;
                const body = {
                  mediaGenerationContext: { batchId: crypto.randomUUID() },
                  clientContext: {
                    projectId: projectId,
                    tool: "PINHOLE",
                    userPaygateTier: "PAYGATE_TIER_ONE",
                    sessionId: `;${Date.now()}`,
                    recaptchaContext: {
                      applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB",
                      token: recaptchaToken
                    }
                  },
                  requests: [{
                    aspectRatio: aspect,
                    textInput: { structuredPrompt: { parts: [{ text: prompt }] } },
                    videoModelKey: modelKey,
                    seed: seed || Math.floor(Math.random() * 9999) + 1,
                    metadata: {},
                    startImage: { mediaId: startImageId },
                    endImage: { mediaId: endImageId }
                  }],
                  useV2ModelConfig: true
                };
                const resp = await window.fetch(`https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartAndEndImage?key=AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY`, {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json',
                    'authorization': `Bearer ${key}`
                  },
                  body: JSON.stringify(body)
                });
                const resData = await resp.json().catch(() => ({}));
                return {
                  success: true,
                  result: {
                    status: resp.status,
                    ok: resp.ok,
                    data: resData
                  }
                };
              }
              if (act === 'PAGE_FETCH') {
                const { url, options } = pl;
                const resp = await window.fetch(url, options);
                const contentType = resp.headers.get('content-type') || '';
                let data;
                if (contentType.includes('application/json')) {
                  data = await resp.json().catch(() => null);
                } else {
                  data = await resp.text().catch(() => '');
                }
                return {
                  success: true,
                  result: {
                    status: resp.status,
                    ok: resp.ok,
                    data
                  }
                };
              }
              if (act === 'QUERY_DOM') {
                const { selector } = pl;
                const els = Array.from(document.querySelectorAll(selector)).map(el => ({
                  text: el.innerText?.trim(),
                  html: el.outerHTML?.slice(0, 300),
                  ariaLabel: el.getAttribute('aria-label')
                }));
                return { success: true, result: els };
              }
              if (act === 'CLICK') {
                const { selector, textMatch } = pl;
                let target = null;
                if (selector) {
                  const list = Array.from(document.querySelectorAll(selector));
                  if (textMatch) {
                    target = list.find(el => (el.innerText || '').includes(textMatch) || (el.getAttribute('aria-label') || '').includes(textMatch));
                  } else {
                    target = list[0];
                  }
                }
                if (!target && textMatch) {
                  const all = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
                  target = all.find(el => (el.innerText || '').includes(textMatch) || (el.getAttribute('aria-label') || '').includes(textMatch));
                }
                if (target) {
                  target.scrollIntoView({ behavior: 'instant', block: 'center' });
                  target.click();
                  return { success: true, result: { clicked: true, text: target.innerText?.trim() || target.getAttribute('aria-label') } };
                }
                return { success: false, error: 'Target element not found' };
              }
              if (act === 'INPUT_TEXT') {
                const { selector, text } = pl;
                const el = document.querySelector(selector || '.ProseMirror, textarea, input');
                if (!el) return { success: false, error: 'Input element not found' };
                el.focus();
                if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') {
                  const sel = window.getSelection();
                  const range = document.createRange();
                  range.selectNodeContents(el);
                  sel.removeAllRanges();
                  sel.addRange(range);
                  const ok = document.execCommand('insertText', false, text);
                  if (!ok || !el.innerText?.includes(text.slice(0, 20))) {
                    el.innerText = text;
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
                  }
                  return { success: true, result: { text: el.innerText?.slice(0, 100) } };
                } else {
                  el.value = text;
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                  return { success: true, result: { value: el.value } };
                }
              }
              return { success: false, error: `Unknown action: ${act}` };
            } catch (err) {
              return { success: false, error: err.message, stack: err.stack };
            }
          },
          args: [action, payload, flowKey],
        });
        const evalRes = results?.[0]?.result;
        sendToAgent({ id: msg.id, result: evalRes });
      } catch (err) {
        sendToAgent({ id: msg.id, result: { success: false, error: err.message } });
      }
    } else if (msg.method === 'get_status') {
      const allTabs = await chrome.tabs.query({});
      const flowTabs = allTabs.filter(t => t.url && (t.url.includes('flow.google.com') || t.url.includes('labs.google')));
      sendToAgent({
        id: msg.id,
        result: {
          state,
          flowKeyPresent: !!flowKey,
          flowKeySample: flowKey ? flowKey.slice(0, 15) + '...' : null,
          tabCount: allTabs.length,
          flowTabs: flowTabs.map(t => ({ id: t.id, url: t.url, title: t.title })),
          manualDisconnect,
          tokenAge: metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
          metrics,
        },
      });
    } else if (msg.method === 'probe_token') {
      try {
        if (!msg.force && !msg.inspect) {
          const storage = await chrome.storage.local.get(['flowKey', 'metrics']);
          if (storage.flowKey) {
            flowKey = storage.flowKey;
            sendToAgent({ type: 'token_captured', flowKey, clientId: extensionClientId });
            sendToAgent({ id: msg.id, result: { found: true, source: 'storage', tokenSample: flowKey.slice(0, 15) + '...' } });
            return;
          }
        } else if (msg.force) {
          console.log('[Flow Agent] probe_token: force refresh requested');
          flowKey = null;
          metrics.tokenCapturedAt = null;
          await chrome.storage.local.remove(['flowKey', 'metrics']);
          const refreshedToken = await refreshLabsSessionToken();
          if (refreshedToken) {
            sendToAgent({ id: msg.id, result: { found: true, source: 'nextauth_refresh', tokenSample: refreshedToken.slice(0, 15) + '...' } });
            return;
          }
        }

        const tab = await getExistingFlowTab();
        if (!tab) {
          sendToAgent({ id: msg.id, result: { found: false, reason: 'no_flow_tab_open' } });
          return;
        }

        if (msg.url) {
          console.log('[Flow Agent] Navigating tab to', msg.url);
          await chrome.tabs.update(tab.id, { url: msg.url });
          await waitForTabComplete(tab.id, 15000);
          await sleep(4000);
        }

        if (msg.click_selector) {
          console.log('[Flow Agent] Clicking selector:', msg.click_selector);
          await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            world: 'MAIN',
            func: (sel) => {
              const el = document.querySelector(sel);
              if (el) el.click();
            },
            args: [msg.click_selector]
          });
          await sleep(5000);
        }

        const evalResults = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          world: 'MAIN',
          func: () => {
            const foundTokens = [];
            // 1. Search localStorage & sessionStorage
            try {
              for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                const v = localStorage.getItem(k);
                if (v && v.includes('ya29.')) foundTokens.push({ source: 'localStorage', key: k, token: (v.match(/ya29\.[^\s"'\\]+/) || [])[0] });
              }
              for (let i = 0; i < sessionStorage.length; i++) {
                const k = sessionStorage.key(i);
                const v = sessionStorage.getItem(k);
                if (v && v.includes('ya29.')) foundTokens.push({ source: 'sessionStorage', key: k, token: (v.match(/ya29\.[^\s"'\\]+/) || [])[0] });
              }
            } catch {}

            // 2. Search WIZ_global_data or window properties
            try {
              if (window.WIZ_global_data) {
                for (const [k, v] of Object.entries(window.WIZ_global_data)) {
                  if (typeof v === 'string' && v.includes('ya29.')) {
                    foundTokens.push({ source: 'WIZ_global_data', key: k, token: (v.match(/ya29\.[^\s"'\\]+/) || [])[0] });
                  }
                }
              }
            } catch {}

            // 3. Inspect recent API calls from performance entries
            let apiUrls = [];
            try {
              apiUrls = performance.getEntriesByType('resource')
                .map(r => r.name)
                .filter(u => u.includes('googleapis.com') || u.includes('/fx/') || u.includes('/api/'))
                .slice(-20);
            } catch {}

            let grecaptchaInfo = {
              hasGrecaptcha: typeof window.grecaptcha !== 'undefined',
              hasEnterprise: !!window.grecaptcha?.enterprise,
              visibilityState: document.visibilityState,
              hasFocus: document.hasFocus(),
              scripts: Array.from(document.querySelectorAll('script[src*="recaptcha"]')).map(s => s.src),
            };

            let images = [];
            try {
              images = Array.from(document.querySelectorAll('img')).map(el => el.src).filter(u => u && !u.startsWith('data:image/svg'));
            } catch {}

            let elements = [];
            try {
              elements = Array.from(document.querySelectorAll('a, button')).map(el => ({
                tag: el.tagName,
                text: el.innerText ? el.innerText.trim().slice(0, 50) : '',
                href: el.href || el.getAttribute('href') || ''
              })).filter(x => x.text || x.href);
            } catch {}

            return {
              url: window.location.href,
              title: document.title,
              grecaptchaInfo,
              elements: elements.slice(0, 30),
              apiUrls,
              images: Array.from(new Set(images)),
            };
          },
        });

        // Test NextAuth signin flow
        let nextAuthResult = {};
        try {
          const csrfResp = await fetch('https://labs.google/fx/api/auth/csrf', { credentials: 'include' });
          const csrfData = await csrfResp.json().catch(() => null);
          const csrfToken = csrfData?.csrfToken;
          nextAuthResult.csrfToken = csrfToken;

          if (csrfToken) {
            const signinResp = await fetch('https://labs.google/fx/api/auth/signin/google', {
              method: 'POST',
              credentials: 'include',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
              body: new URLSearchParams({ csrfToken, callbackUrl: 'https://labs.google/fx/tools/flow', json: 'true' }),
            });
            const signinData = await signinResp.json().catch(() => null);
            nextAuthResult.signinStatus = signinResp.status;
            nextAuthResult.redirectUrl = signinData?.url;
          }
        } catch (e) {
          nextAuthResult.error = e.message;
        }

        const probeData = evalResults?.[0]?.result || {};
        sendToAgent({ id: msg.id, result: { found: !!flowKey, probeData, nextAuthResult } });
      } catch (err) {
        sendToAgent({ id: msg.id, result: { found: false, error: err.message } });
      }
    } else if (msg.method === 'open_flow_tab') {
      if (isTokenFresh()) {
        console.log('[Flow Agent] open_flow_tab: token fresh, sending cached token');
        sendToAgent({ type: 'token_captured', flowKey, clientId: extensionClientId });
      } else {
        console.log('[Flow Agent] open_flow_tab: token missing/expired, capturing...');
        const token = await captureTokenFromFlowTab();
        if (token) {
          sendToAgent({ type: 'token_captured', flowKey: token, clientId: extensionClientId });
        }
      }
    } else if (msg.method === 'refresh_flow_tab' || msg.method === 'force_refresh') {
      const force = msg.force === true || msg.method === 'force_refresh';
      if (force) {
        flowKey = null;
        metrics.tokenCapturedAt = null;
        await chrome.storage.local.remove(['flowKey']);
      }
      if (isTokenFresh() && !force) {
        console.log('[Flow Agent] refresh_flow_tab: token fresh, sending cached token');
        sendToAgent({ type: 'token_captured', flowKey, clientId: extensionClientId });
      } else if (force) {
        console.log('[Flow Agent] force_refresh: performing NextAuth OAuth handshake...');
        const token = await refreshLabsSessionToken();
        if (token) {
          sendToAgent({ type: 'token_captured', flowKey: token, clientId: extensionClientId });
        }
      } else {
        console.log('[Flow Agent] refresh_flow_tab: refreshing token...');
        const token = await captureTokenFromFlowTab();
        if (token) {
          sendToAgent({ type: 'token_captured', flowKey: token, clientId: extensionClientId });
        }
      }
    } else if (msg.type === 'callback_config') {
      callbackSecret = msg.secret;
      callbackUrl = normalizeCallbackUrl(msg.callback_url);
      chrome.storage.local.set({ callbackSecret: msg.secret, callbackUrl });
      console.log('[Flow Agent] Received callback config:', callbackUrl);
    } else if (msg.type === 'callback_secret') {
      callbackSecret = msg.secret;
      chrome.storage.local.set({ callbackSecret: msg.secret });
      console.log('[Flow Agent] Received callback secret');
    } else if (msg.type === 'pong') {
      // keepalive response
    }
  } catch (e) {
    console.error('[Flow Agent] handleAgentCommand error:', e);
  }
}

function agentHttpBase() {
  const host = String(connectedServerHost || CONFIG.DEFAULT_SERVER_HOST).trim().replace(/\/$/, '');
  const hostWithoutScheme = host.replace(/^https?:\/\//i, '');
  const local = /^(127\.0\.0\.1|localhost|192\.168\.|10\.)(:|$)/.test(hostWithoutScheme);
  return /^https?:\/\//i.test(host) ? host : `${local ? 'http' : 'https'}://${host}`;
}

async function connectHttpAgent() {
  if (manualDisconnect || httpConnected) return;
  const storage = await chrome.storage.local.get(['clientId']);
  let clientId = storage.clientId;
  if (!clientId) {
    const prefix = CONFIG.DEFAULT_CLIENT_ID_PREFIX || 'client';
    clientId = `${prefix}-${Math.random().toString(36).substring(2, 8)}`;
    await chrome.storage.local.set({ clientId });
  }
  extensionClientId = clientId;
  connectedServerHost = CONFIG.DEFAULT_SERVER_HOST;
  try {
    const response = await fetch(`${agentHttpBase()}/api/ext/hello`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: clientId,
        clientId,
        flowKey,
        flowKeyPresent: !!flowKey,
        extension_version: chrome.runtime.getManifest().version,
      }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    callbackSecret = data.secret;
    callbackUrl = new URL(data.callback_url, agentHttpBase()).toString();
    httpPollIntervalMs = Math.max(250, Number(data.poll_interval_ms) || 1000);
    httpConnected = true;
    await chrome.storage.local.set({ callbackSecret, callbackUrl });
    setState('idle');
    scheduleHttpPoll(0);
    flushOutbox();
  } catch (error) {
    httpConnected = false;
    console.warn('[Flow Agent] HTTP bridge unavailable; using WebSocket fallback:', error.message);
  }
}

function scheduleHttpPoll(delay = httpPollIntervalMs) {
  if (httpPollTimer) clearTimeout(httpPollTimer);
  if (!httpConnected || manualDisconnect) return;
  httpPollTimer = setTimeout(pollHttpCommands, delay);
}

async function pollHttpCommands() {
  if (!httpConnected || manualDisconnect) return;
  try {
    const response = await fetch(`${agentHttpBase()}/api/ext/poll?session_id=${encodeURIComponent(extensionClientId)}`, {
      headers: { Authorization: `Bearer ${callbackSecret}` },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const commands = data.commands || [];
    if (commands.length > 0) {
      commands.forEach((command) => {
        handleAgentCommand(command).catch((err) => {
          console.error('[Flow Agent] Command execution error:', err);
        });
      });
    }
    scheduleHttpPoll();
  } catch (error) {
    httpConnected = false;
    console.warn('[Flow Agent] HTTP polling stopped:', error.message);
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  chrome.alarms.create('reconnect', { delayInMinutes: 0.5 });
}

function keepAlive() {
  if (!isTokenFresh()) {
    fetchTokenFromSession().catch(() => {});
  }
  if (httpConnected) {
    connectHttpAgent();
  } else if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping' }));
  } else {
    connectToAgent();
  }
}

function sendToAgent(msg) {
  // API responses (with msg.id) go through a durable outbox so a generated
  // result is never lost — persisted and retried until the agent acks it.
  if (msg.id) {
    enqueueResponse(msg);
    return;
  }
  if (httpConnected && callbackSecret) {
    fetch(callbackUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${callbackSecret}` },
      body: JSON.stringify({ ...msg, session_id: extensionClientId }),
    }).catch(() => {});
  } else if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

// ─── Durable Response Outbox ────────────────────────────────
// A generated image/video result must survive a momentary backend hiccup or a
// service-worker restart. Every id-bearing response is persisted and retried
// with backoff until the agent confirms receipt, then dropped.

const MAX_DELIVERY_ATTEMPTS = 8;
let outbox = {};              // id -> { msg, attempts, nextAt }
let _flushingOutbox = false;

async function loadOutbox() {
  try {
    const { responseOutbox } = await chrome.storage.local.get('responseOutbox');
    if (responseOutbox && typeof responseOutbox === 'object') outbox = responseOutbox;
  } catch { }
}

function persistOutbox() {
  chrome.storage.local.set({ responseOutbox: outbox }).catch(() => { });
}

function enqueueResponse(msg) {
  outbox[msg.id] = { msg, attempts: 0, nextAt: 0 };
  persistOutbox();
  flushOutbox();
}

async function deliverOnce(entry) {
  try {
    const serverIp = connectedServerHost || CONFIG.DEFAULT_SERVER_HOST;
    const targetCallbackUrl = normalizeCallbackUrl(serverIp);

    const resp = await fetch(targetCallbackUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(callbackSecret ? { Authorization: `Bearer ${callbackSecret}` } : {}),
      },
      body: JSON.stringify({ ...entry.msg, session_id: extensionClientId }),
    });
    // Any HTTP reply means the backend is reachable and has taken the response
    // (ok:true = matched a request, ok:false = unknown id / already handled).
    // Either way there is nothing to retry — only transport failures retry.
    if (resp.ok) return true;
    // 5xx / transient server error — retry.
    return false;
  } catch {
    // Network error: backend unreachable. Try WS as an immediate fallback but
    // keep the entry queued so a later flush can still deliver it.
    if (ws?.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify(entry.msg)); } catch { }
    }
    return false;
  }
}

async function flushOutbox() {
  if (_flushingOutbox) return;
  _flushingOutbox = true;
  try {
    const ids = Object.keys(outbox);
    if (!ids.length) return;
    const now = Date.now();
    for (const id of ids) {
      const entry = outbox[id];
      if (!entry) continue;
      if (entry.nextAt && entry.nextAt > now) continue;
      const delivered = await deliverOnce(entry);
      if (delivered) {
        delete outbox[id];
        persistOutbox();
        continue;
      }
      entry.attempts++;
      if (entry.attempts >= MAX_DELIVERY_ATTEMPTS) {
        console.error('[Flow Agent] Dropping response', id, 'after', entry.attempts, 'failed deliveries');
        delete outbox[id];
      } else {
        // Exponential backoff, capped at 30s.
        entry.nextAt = Date.now() + Math.min(30000, 1000 * 2 ** entry.attempts);
      }
      persistOutbox();
    }
  } finally {
    _flushingOutbox = false;
  }
}

// ─── reCAPTCHA Solving ──────────────────────────────────────

async function requestCaptchaFromTab(tabId, requestId, pageAction) {
  try {
    return await chrome.tabs.sendMessage(tabId, {
      type: 'GET_CAPTCHA',
      requestId,
      pageAction,
    });
  } catch (error) {
    const msg = error?.message || '';
    const shouldInject =
      msg.includes('Receiving end does not exist') ||
      msg.includes('Could not establish connection');
    if (!shouldInject) throw error;

    // Inject content script and retry
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['content.js'],
    });
    await sleep(200);
    return await chrome.tabs.sendMessage(tabId, {
      type: 'GET_CAPTCHA',
      requestId,
      pageAction,
    });
  }
}

const RECAPTCHA_SITE_KEY = '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV';

let _captchaMutex = Promise.resolve();

async function _withCaptchaMutex(fn) {
  const currentMutex = _captchaMutex;
  let releaseMutex;
  _captchaMutex = new Promise(resolve => { releaseMutex = resolve; });

  try {
    await currentMutex;
  } catch {}

  try {
    return await fn();
  } finally {
    await sleep(350); // Small cooldown gap between sequential captcha solves
    releaseMutex();
  }
}

async function ensureFlowProjectTab() {
  const allTabs = await chrome.tabs.query({});
  let targetTab = allTabs.slice().reverse().find(
    t => t.url && (t.url.includes('flow.google.com/project/') || t.url.includes('labs.google/fx/tools/flow/project/'))
  );
  if (!targetTab) {
    targetTab = allTabs.slice().reverse().find(
      t => t.url && (t.url.includes('flow.google.com') || t.url.includes('labs.google/fx/tools/flow'))
    );
  }
  if (!targetTab) {
    targetTab = await chrome.tabs.create({ url: FLOW_URL, active: true });
    await waitForTabComplete(targetTab.id, 15000);
    await sleep(2000);
  }

  const targetTabId = targetTab.id;
  try {
    await chrome.tabs.update(targetTabId, { active: true });
    if (targetTab.windowId) {
      await chrome.windows.update(targetTab.windowId, { focused: true, state: 'normal' });
    }
    await sleep(300);
  } catch {}

  if (targetTab.discarded) {
    await chrome.tabs.reload(targetTabId);
    await waitForTabComplete(targetTabId, 15000);
    await sleep(1500);
  }

  let currentTab = await chrome.tabs.get(targetTabId).catch(() => targetTab);
  let currentUrl = currentTab?.url || '';

  // If tab is on the root landing page (https://flow.google.com/) instead of a /project/<uuid> canvas,
  // automatically enter the first existing project link on the landing page so enterprise.js loads.
  if (!currentUrl.includes('/project/')) {
    try {
      const navResults = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        world: 'MAIN',
        func: () => {
          const link = document.querySelector('a[href*="/project/"]');
          if (link && link.href) {
            link.click();
            return link.href;
          }
          return null;
        },
      });
      const projectHref = navResults?.[0]?.result;
      if (projectHref) {
        await sleep(3500);
        currentTab = await chrome.tabs.get(targetTabId).catch(() => currentTab);
        currentUrl = currentTab?.url || '';
        if (!currentUrl.includes('/project/')) {
          await chrome.tabs.update(targetTabId, { url: projectHref, active: true });
          await waitForTabComplete(targetTabId, 15000);
          await sleep(2500);
          currentTab = await chrome.tabs.get(targetTabId).catch(() => currentTab);
          currentUrl = currentTab?.url || '';
        }
      }
    } catch (e) {
      console.warn('[Flow Agent] Auto-navigate to project canvas failed:', e.message);
    }
  }

  const match = currentUrl.match(/\/project\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
  const projectId = match ? match[1] : null;
  return { tabId: targetTabId, tabUrl: currentUrl, projectId };
}

async function solveCaptcha(requestId, captchaAction) {
  return await _withCaptchaMutex(() => _solveCaptchaInternal(requestId, captchaAction));
}

async function _solveCaptchaInternal(requestId, captchaAction) {
  const scriptTimeoutMs = captchaAction === 'VIDEO_GENERATION' ? 30000 : 20000;

  try {
    const { tabId: targetTabId } = await ensureFlowProjectTab();

    // Execute directly in MAIN world (Attempt 1 via content.js removed due to TrustedTypes CSP)
    const results = await chrome.scripting.executeScript({
      target: { tabId: targetTabId },
      world: 'MAIN',
      func: async (action, siteKey, timeoutMs) => {
        return new Promise((resolve) => {
          let settled = false;
          const finish = (value) => {
            if (settled) return;
            settled = true;
            resolve(value);
          };
          try {
            try {
              window.focus();
              window.dispatchEvent(new Event('focus'));
              Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
              Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
              document.hasFocus = () => true;
              document.dispatchEvent(new MouseEvent('mousemove', {
                bubbles: true,
                clientX: Math.max(32, Math.floor((window.innerWidth || 1280) * 0.42)),
                clientY: Math.max(32, Math.floor((window.innerHeight || 720) * 0.36)),
              }));
            } catch (e) {}

            async function run() {
              try {
                if (window.grecaptcha?.enterprise?.ready) {
                  window.grecaptcha.enterprise.ready(async () => {
                    try {
                      const tok = await window.grecaptcha.enterprise.execute(siteKey, { action: action || 'IMAGE_GENERATION' });
                      finish({ token: tok });
                    } catch (e) {
                      finish({ error: e?.message || 'reCAPTCHA execution failed' });
                    }
                  });
                  return;
                }
                if (window.grecaptcha?.enterprise?.execute) {
                  const tok = await window.grecaptcha.enterprise.execute(siteKey, { action: action || 'IMAGE_GENERATION' });
                  return finish({ token: tok });
                }
                finish({ error: 'grecaptcha.enterprise.execute unavailable' });
              } catch (err) {
                finish({ error: err?.message || 'reCAPTCHA execution failed' });
              }
            }

            if (typeof window.grecaptcha !== 'undefined' && window.grecaptcha?.enterprise?.execute) {
              run();
            } else {
              let waited = 0;
              const poll = setInterval(() => {
                waited += 250;
                if (typeof window.grecaptcha !== 'undefined' && window.grecaptcha?.enterprise?.execute) {
                  clearInterval(poll);
                  run();
                } else if (waited >= timeoutMs) {
                  clearInterval(poll);
                  finish({ error: 'Timeout waiting for grecaptcha in page' });
                }
              }, 250);
            }

            setTimeout(() => finish({ error: 'Timeout generating reCAPTCHA locally' }), timeoutMs + 2000);
          } catch (e) {
            finish({ error: e.message });
          }
        });
      },
      args: [captchaAction || 'IMAGE_GENERATION', RECAPTCHA_SITE_KEY, scriptTimeoutMs],
    });

    const res = results?.[0]?.result;
    if (res && res.token) {
      return { token: res.token };
    }
    return { error: res?.error || 'No token returned from page' };
  } catch (e) {
    return { error: e.message || 'Script execution failed' };
  }
}

async function handleSolveCaptcha(msg) {
  const { id, params } = msg;
  const result = await solveCaptcha(id, params?.captchaAction || 'VIDEO_GENERATION');

  // Standalone captcha solve counts as captcha-consuming
  metrics.requestCount++;
  if (result?.token) {
    metrics.successCount++;
  } else {
    metrics.failedCount++;
    metrics.lastError = result?.error || 'NO_TOKEN';
  }
  chrome.storage.local.set({ metrics });

  sendToAgent({ id, result });
}

// ─── API Request Proxy ──────────────────────────────────────

async function handleTrpcRequest(msg) {
  const { id, params } = msg;
  const { url, method = 'POST', headers = {}, body } = params;

  if (!url || !url.startsWith('https://labs.google/')) {
    sendToAgent({ id, error: 'INVALID_TRPC_URL' });
    return;
  }

  setState('running');
  // TRPC calls don't consume captcha and are silent — no metrics, no request log.

  const fetchHeaders = { 'Content-Type': 'application/json', ...headers };
  if (flowKey) {
    fetchHeaders['authorization'] = `Bearer ${flowKey}`;
  }

  try {
    const resp = await fetch(url, {
      method,
      headers: fetchHeaders,
      body: body ? JSON.stringify(body) : undefined,
      credentials: 'include',
    });
    const data = await resp.json();
    sendToAgent({ id, status: resp.status, data });
  } catch (e) {
    console.error('[Flow Agent] tRPC request failed:', e);
    sendToAgent({ id, error: e.message || 'TRPC_FETCH_FAILED' });
  } finally {
    setState('idle');
  }
}


async function handleUploadVideo(msg) {
  const { id, params } = msg;
  const { videoBase64, projectId, videoSize } = params;

  try {
    const tabs = await chrome.tabs.query({ url: '*://labs.google/*' });
    if (!tabs.length) {
      sendToAgent({ id, error: 'NO_FLOW_TAB' });
      return;
    }

    const size = videoSize || (videoBase64 ? Math.floor(videoBase64.length * 3 / 4) : 0);

    // Get session URL via page context XHR (needs session cookies)
    const startResults = await chrome.scripting.executeScript({
      target: { tabId: tabs[0].id },
      world: 'MAIN',
      func: (projId, sz) => {
        return new Promise((resolve) => {
          const xhr = new XMLHttpRequest();
          xhr.open('POST', '/fx/api/upload-video?action=start');
          xhr.setRequestHeader('X-Upload-Project-Id', projId);
          xhr.setRequestHeader('X-Upload-Content-Type', 'video/mp4');
          xhr.setRequestHeader('X-Upload-Content-Length', sz.toString());
          xhr.withCredentials = true;
          xhr.onload = () => {
            let data;
            try { data = JSON.parse(xhr.responseText); } catch { data = {}; }
            resolve({
              sessionUrl: data.sessionUrl || xhr.getResponseHeader('X-Upload-Session-Url') || '',
              status: xhr.status,
            });
          };
          xhr.onerror = () => resolve({ error: 'POST_FAILED' });
          xhr.send();
        });
      },
      args: [projectId, size],
    });

    const step1 = startResults?.[0]?.result;
    if (!step1 || step1.error || !step1.sessionUrl) {
      sendToAgent({ id, error: step1?.error || 'NO_SESSION_URL' });
      return;
    }

    // Return sessionUrl + token — caller handles PUT
    sendToAgent({
      id,
      result: {
        sessionUrl: step1.sessionUrl,
        token: flowKey || '',
      },
    });
  } catch (e) {
    sendToAgent({ id, error: `UPLOAD_ERROR: ${e.message}` });
  }
}

async function handleApiRequest(msg) {
  const { id, params } = msg;
  const { url, method, headers, body, captchaAction } = params;

  if (!url) {
    sendToAgent({ id, error: 'MISSING_URL' });
    return;
  }

  if (!url.startsWith('https://aisandbox-pa.googleapis.com/')) {
    sendToAgent({ id, error: 'INVALID_URL' });
    return;
  }

  setState('running');
  const hasCaptcha = !!captchaAction;
  if (hasCaptcha) metrics.requestCount++;

  const logId = id;
  const logType = _classifyApiUrl(url);
  if (_VISIBLE_TYPES.has(logType)) {
    const payloadSummary = body ? JSON.stringify(body).slice(0, 200) : null;
    addRequestLog({ id: logId, type: logType, time: new Date().toISOString(), status: 'processing', error: null, outputUrl: null, url, payloadSummary });
  }

  try {
    let activeFlowKey = flowKey;
    if (!activeFlowKey) {
      activeFlowKey = await fetchTokenFromSession();
    }
    if (!activeFlowKey) {
      sendToAgent({ id, status: 503, error: 'NO_FLOW_KEY' });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = 'NO_FLOW_KEY'; }
      chrome.storage.local.set({ metrics });
      updateRequestLog(logId, { status: 'failed', error: 'NO_FLOW_KEY' });
      setState('idle');
      return;
    }

    let responseStatus = 0;
    let responseOk = false;
    let responseText = '';
    let responseData = null;

    if (hasCaptcha) {
      const scriptTimeoutMs = captchaAction === 'VIDEO_GENERATION' ? 30000 : 20000;
      const inPageResult = await _withCaptchaMutex(async () => {
        const { tabId: targetTabId, projectId: tabProjectId } = await ensureFlowProjectTab();

        let targetUrl = url;
        let finalBody = body ? JSON.parse(JSON.stringify(body)) : body;

        // Align projectId in URL and body with the client's open project canvas tab
        if (tabProjectId) {
          targetUrl = targetUrl.replace(
            /\/projects\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\//i,
            `/projects/${tabProjectId}/`
          );
          if (finalBody && typeof finalBody === 'object') {
            if (finalBody.clientContext && typeof finalBody.clientContext === 'object' && finalBody.clientContext.projectId) {
              finalBody.clientContext.projectId = tabProjectId;
            }
            if (Array.isArray(finalBody.requests)) {
              for (const req of finalBody.requests) {
                if (req && req.clientContext && typeof req.clientContext === 'object' && req.clientContext.projectId) {
                  req.clientContext.projectId = tabProjectId;
                }
              }
            }
          }
        }

        const evalResults = await chrome.scripting.executeScript({
          target: { tabId: targetTabId },
          world: 'MAIN',
          func: async ({ websiteKey, actionName, payload, waitTimeoutMs }) => {
            try {
              try {
                window.focus();
                window.dispatchEvent(new Event('focus'));
                Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
                Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
                document.hasFocus = () => true;
                document.dispatchEvent(new MouseEvent('mousemove', {
                  bubbles: true,
                  clientX: Math.max(32, Math.floor((window.innerWidth || 1280) * 0.42)),
                  clientY: Math.max(32, Math.floor((window.innerHeight || 720) * 0.36)),
                }));
              } catch (e) {}

              const waitForEnterprise = () => new Promise((resolve) => {
                if (typeof window.grecaptcha !== 'undefined' && typeof window.grecaptcha?.enterprise?.execute === 'function') {
                  return resolve(true);
                }
                let waited = 0;
                const timer = setInterval(() => {
                  waited += 250;
                  if (typeof window.grecaptcha !== 'undefined' && typeof window.grecaptcha?.enterprise?.execute === 'function') {
                    clearInterval(timer);
                    resolve(true);
                  } else if (waited >= waitTimeoutMs) {
                    clearInterval(timer);
                    resolve(false);
                  }
                }, 250);
              });

              const ready = await waitForEnterprise();
              if (!ready) {
                return { captchaError: 'Timeout waiting for grecaptcha in page' };
              }

              const solveToken = () => new Promise((resolve, reject) => {
                const timer = setTimeout(() => reject(new Error('captcha_timeout')), 25000);
                try {
                  window.grecaptcha.enterprise.execute(websiteKey, { action: actionName })
                    .then((tok) => {
                      clearTimeout(timer);
                      resolve(tok);
                    })
                    .catch((err) => {
                      clearTimeout(timer);
                      reject(err);
                    });
                } catch (err) {
                  clearTimeout(timer);
                  reject(err);
                }
              });

              let token;
              try {
                token = await solveToken();
              } catch (err) {
                return { captchaError: err?.message || 'reCAPTCHA execution failed' };
              }
              if (!token) {
                return { captchaError: 'Empty reCAPTCHA token returned' };
              }

              const sessionId = `;${Date.now()}`;
              const patchPayload = (value) => {
                if (!value) return;
                if (Array.isArray(value)) {
                  value.forEach(patchPayload);
                  return;
                }
                if (typeof value !== 'object') return;
                if (value.sessionId && typeof value.sessionId === 'string') {
                  value.sessionId = sessionId;
                }
                if (value.recaptchaContext && typeof value.recaptchaContext === 'object') {
                  value.recaptchaContext.token = token;
                  if (!value.recaptchaContext.applicationType) {
                    value.recaptchaContext.applicationType = 'RECAPTCHA_APPLICATION_TYPE_WEB';
                  }
                }
                Object.values(value).forEach(patchPayload);
              };

              const bodyToSend = payload ? JSON.parse(JSON.stringify(payload)) : payload;
              patchPayload(bodyToSend);

              return {
                token,
                patchedBody: bodyToSend,
              };
            } catch (err) {
              return { captchaError: err?.message || 'In-page script failed' };
            }
          },
          args: [{
            websiteKey: RECAPTCHA_SITE_KEY,
            actionName: captchaAction || 'IMAGE_GENERATION',
            payload: finalBody,
            waitTimeoutMs: scriptTimeoutMs,
          }],
        });

        const res = evalResults?.[0]?.result || { captchaError: 'Empty executeScript result' };
        return { ...res, targetUrl };
      });

      if (inPageResult.captchaError) {
        const errDetail = inPageResult.captchaError;
        console.error(`[Flow Agent] Captcha error: ${errDetail}. Aborting request.`);
        sendToAgent({
          id,
          status: 403,
          error: `RECAPTCHA_FAILED: ${errDetail}. Ensure Chrome has an active flow.google.com tab visible.`,
        });
        metrics.failedCount++;
        metrics.lastError = `RECAPTCHA_FAILED: ${errDetail}`;
        chrome.storage.local.set({ metrics });
        updateRequestLog(logId, { status: 'failed', error: `RECAPTCHA_FAILED: ${errDetail}` });
        setState('idle');
        return;
      }

      const fetchHeaders = { ...(headers || {}) };
      fetchHeaders['authorization'] = `Bearer ${activeFlowKey}`;

      const swResponse = await fetch(inPageResult.targetUrl || url, {
        method: method || 'POST',
        headers: fetchHeaders,
        credentials: 'include',
        body: (method || 'POST') === 'GET' ? undefined : JSON.stringify(inPageResult.patchedBody),
      });
      responseStatus = swResponse.status;
      responseOk = swResponse.ok;
      responseText = await swResponse.text();
    } else {
      const fetchHeaders = { ...(headers || {}) };
      fetchHeaders['authorization'] = `Bearer ${activeFlowKey}`;

      const response = await fetch(url, {
        method: method || 'POST',
        headers: fetchHeaders,
        credentials: 'include',
        body: method === 'GET' ? undefined : JSON.stringify(body),
      });
      responseStatus = response.status;
      responseOk = response.ok;
      responseText = await response.text();
    }

    try {
      responseData = JSON.parse(responseText);
    } catch {
      responseData = responseText;
    }

    // Self-heal: a 401 means Google invalidated our cached token
    if (responseStatus === 401) {
      console.warn('[Flow Agent] 401 UNAUTHENTICATED on request: dropping dead token and initiating session refresh');
      flowKey = null;
      metrics.tokenCapturedAt = null;
      chrome.storage.local.remove(['flowKey']);
      setTimeout(() => {
        refreshLabsSessionToken().then(token => {
          if (token) {
            sendToAgent({ type: 'token_captured', flowKey: token, clientId: extensionClientId });
          }
        }).catch(() => {});
      }, 100);
    }

    sendToAgent({
      id,
      status: responseStatus,
      data: responseData,
    });

    const responseSummary = responseText ? responseText.slice(0, 300) : null;
    if (responseOk) {
      if (hasCaptcha) { metrics.successCount++; metrics.lastError = null; }
      updateRequestLog(logId, { status: 'success', httpStatus: responseStatus, responseSummary });
    } else {
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = `API_${responseStatus}`; }
      updateRequestLog(logId, { status: 'failed', error: `API_${responseStatus}`, httpStatus: responseStatus, responseSummary });
    }
  } catch (e) {
    sendToAgent({
      id,
      status: 500,
      error: e.message || 'API_REQUEST_FAILED',
    });
    if (hasCaptcha) { metrics.failedCount++; metrics.lastError = e.message; }
    updateRequestLog(logId, { status: 'failed', error: e.message || 'API_REQUEST_FAILED' });
  }

  chrome.storage.local.set({ metrics });
  setState('idle');
}

async function handleGetMediaUrl(msg) {
  const { id, params } = msg;
  const mediaId = params?.media_id;
  if (!mediaId) { sendToAgent({ id, error: 'MISSING_MEDIA_ID' }); return; }
  try {
    const url = new URL('https://labs.google/fx/api/trpc/media.getMediaUrlRedirect');
    url.searchParams.set('name', mediaId);
    const response = await fetch(url.toString(), { credentials: 'include', redirect: 'follow' });
    if (!response.ok) { sendToAgent({ id, status: response.status, error: `MEDIA_URL_HTTP_${response.status}` }); return; }
    sendToAgent({ id, status: 200, result: { url: response.url } });
  } catch (error) {
    sendToAgent({ id, error: `MEDIA_URL_FAILED: ${error.message}` });
  }
}

// ─── State & Popup ──────────────────────────────────────────

function setState(newState) {
  state = newState;
  const badges = { idle: '●', running: '▶', off: '○' };
  const colors = { idle: '#22c55e', running: '#f59e0b', off: '#6b7280' };
  chrome.action.setBadgeText({ text: badges[state] || '' });
  chrome.action.setBadgeBackgroundColor({ color: colors[state] || '#000' });
  broadcastStatus();
}

function broadcastStatus() {
  chrome.runtime.sendMessage({ type: 'STATUS_PUSH' }).catch(() => { });
}

chrome.runtime.onMessage.addListener((msg, _, reply) => {
  if (msg.type === 'SETTINGS_UPDATED') {
    if (ws) {
      try { ws.close(); } catch { }
    }
    connectToAgent();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'STATUS') {
    reply({
      connected: httpConnected || ws?.readyState === WebSocket.OPEN,
      agentConnected: httpConnected || ws?.readyState === WebSocket.OPEN,
      httpConnected,
      transport: httpConnected ? 'http' : (ws?.readyState === WebSocket.OPEN ? 'ws' : 'none'),
      flowKeyPresent: !!flowKey,
      manualDisconnect,
      tokenAge: metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
      metrics: {
        requestCount: metrics.requestCount,
        successCount: metrics.successCount,
        failedCount: metrics.failedCount,
        lastError: metrics.lastError,
      },
      state,
      clientId: extensionClientId,
    });
  }

  if (msg.type === 'DISCONNECT') {
    manualDisconnect = true;
    httpConnected = false;
    if (httpPollTimer) clearTimeout(httpPollTimer);
    if (ws) ws.close();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'RECONNECT') {
    manualDisconnect = false;
    connectToAgent();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'REQUEST_LOG') {
    reply({ log: requestLog });
    return true;
  }

  if (msg.type === 'GET_CLIENT_CREDITS') {
    const host = String(connectedServerHost || CONFIG.DEFAULT_SERVER_HOST).trim().replace(/\/$/, '');
    const hostWithoutScheme = host.replace(/^https?:\/\//i, '');
    const local = /^(127\.0\.0\.1|localhost|192\.168\.|10\.)(:|$)/.test(hostWithoutScheme);
    const base = /^https?:\/\//i.test(host) ? host : `${local ? 'http' : 'https'}://${host}`;
    chrome.storage.local.get(['clientId']).then(({ clientId }) => fetch(`${base}/v1/credits`, {
      headers: (extensionClientId || clientId) ? { 'X-Client-Id': extensionClientId || clientId } : {},
    }))
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        reply(data);
      })
      .catch((error) => {
        console.error('[Flow Agent] Credit request failed:', error);
        reply({ error: error.message });
      });
    return true;
  }

  if (msg.type === 'CLEAR_REQUEST_LOG') {
    requestLog = [];
    chrome.storage.local.remove('requestLog').then(() => {
      broadcastRequestLog();
      reply({ ok: true });
    });
    return true;
  }

  if (msg.type === 'ADD_HISTORY') {
    addRequestLog({
      id: msg.entry?.id || `popup-${Date.now()}`,
      time: msg.entry?.time || new Date().toISOString(),
      type: msg.entry?.type || 'GEN_IMG',
      status: msg.entry?.status || 'success',
      url: msg.entry?.url || '',
      payloadSummary: msg.entry?.prompt || '',
      responseSummary: msg.entry?.url ? 'Generated result ready' : 'Generation completed',
    });
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'OPEN_FLOW_TAB') {
    chrome.tabs.query({
      url: FLOW_TAB_URLS,
    }).then((tabs) => {
      if (tabs.length) {
        chrome.tabs.update(tabs[0].id, { active: true });
        reply({ ok: true, tabId: tabs[0].id });
      } else {
        chrome.tabs.create({ url: FLOW_URL })
          .then((tab) => reply({ ok: true, tabId: tab.id }))
          .catch((e) => reply({ error: e.message }));
      }
    }).catch((e) => reply({ error: e.message }));
    return true;
  }

  if (msg.type === 'REFRESH_TOKEN') {
    captureTokenFromFlowTab()
      .then(() => reply({ ok: true }))
      .catch((e) => reply({ error: e.message }));
    return true;
  }

  if (msg.type === 'CAPTURED_PAGE_TOKEN' && msg.token) {
    flowKey = msg.token;
    metrics.tokenCapturedAt = Date.now();
    chrome.storage.local.set({ flowKey, metrics });
    console.log('[Flow Agent] Injected page token captured and synced');
    sendToAgent({ type: 'token_captured', flowKey, clientId: extensionClientId });
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'TEST_CAPTCHA') {
    solveCaptcha(`test-${Date.now()}`, msg.pageAction || 'IMAGE_GENERATION')
      .then((r) => reply(r))
      .catch((e) => reply({ error: e.message }));
    return true;
  }

  if (msg.type === 'TRPC_MEDIA_URLS') {
    handleTrpcMediaUrls(msg.trpcUrl, msg.body);
    reply({ ok: true });
    return true;
  }

  return true;
});

// ─── TRPC Media URL Extractor ──────────────────────────────

function handleTrpcMediaUrls(trpcUrl, bodyText) {
  try {
    // Extract all fresh GCS signed URLs
    const urlRegex = /https:\/\/(?:storage\.googleapis\.com\/ai-sandbox-videofx|flow-content\.google\/(?:image|video))\/[0-9a-f-]{36}\?[^"'\s]+/g;
    const matches = bodyText.match(urlRegex) || [];
    if (!matches.length) return;

    // Deduplicate and parse
    const urlMap = {};
    for (const rawUrl of matches) {
      // Unescape JSON-escaped URLs
      const url = rawUrl.replace(/\\u0026/g, '&').replace(/\\/g, '');
      const mediaMatch = url.match(/\/(image|video)\/([0-9a-f-]{36})\?/);
      if (mediaMatch) {
        const [, mediaType, mediaId] = mediaMatch;
        // Keep last occurrence (freshest)
        urlMap[mediaId] = { mediaType, url, mediaId };
      }
    }

    const entries = Object.values(urlMap);
    if (!entries.length) return;

    console.log(`[Flow Agent] Captured ${entries.length} fresh media URLs from TRPC`);
    // URL refresh is silent — don't show in request log

    // Forward to agent for DB update
    sendToAgent({ type: 'media_urls_refresh', urls: entries, session_id: extensionClientId });
  } catch (e) {
    console.error('[Flow Agent] Failed to extract TRPC media URLs:', e);
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ─── Human-like Telemetry ──────────────────────────────────
// Periodically send tracking events to Google's analytics endpoints
// to mimic normal browser behavior.

const _UA = navigator.userAgent;
let _telemetrySessionId = `;${Date.now()}`;

function _rand(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }

function _buildBatchLogPayload() {
  const events = [];
  const types = ['FLOW_IMAGE_LATENCY', 'FLOW_VIDEO_LATENCY'];
  const count = _rand(1, 3);
  for (let i = 0; i < count; i++) {
    events.push({
      event: types[_rand(0, types.length - 1)],
      eventProperties: [
        { key: 'CURRENT_TIME_MS', doubleValue: Date.now() },
        { key: 'DURATION_MS', doubleValue: _rand(150, 800) },
        { key: 'USER_AGENT', stringValue: _UA },
        { key: 'IS_DESKTOP', booleanValue: true },
      ],
      eventMetadata: { sessionId: _telemetrySessionId },
      eventTime: new Date().toISOString(),
    });
  }
  return { appEvents: events };
}

function _buildFrontendEventsPayload() {
  const eventTypes = [
    'FLOW_IMAGE_LATENCY', 'FLOW_VIDEO_LATENCY', 'GRID_SCROLL_DEPTH',
    'FLOW_PROJECT_OPEN', 'FLOW_SCENE_VIEW',
  ];
  const count = _rand(1, 4);
  const events = [];
  for (let i = 0; i < count; i++) {
    const et = eventTypes[_rand(0, eventTypes.length - 1)];
    const params = {
      USER_AGENT: { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: _UA },
      IS_DESKTOP: { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: 'true' },
    };
    if (et.includes('LATENCY')) {
      params.CURRENT_TIME_MS = { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: String(Date.now()) };
      params.DURATION_MS = { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: String(_rand(100, 600)) };
    }
    if (et === 'GRID_SCROLL_DEPTH') {
      params.MEDIA_GENERATION_PAYGATE_TIER = { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: 'PAYGATE_TIER_TWO' };
    }
    events.push({
      eventType: et,
      metadata: {
        sessionId: _telemetrySessionId,
        createTime: new Date().toISOString(),
        additionalParams: params,
      },
    });
  }
  return { events };
}

async function sendTelemetry() {
  if (!flowKey || state === 'off') return;

  const headers = {
    'Content-Type': 'text/plain;charset=UTF-8',
    'authorization': `Bearer ${flowKey}`,
  };

  // Telemetry is silent — don't show in request log
  try {
    if (Math.random() < 0.5) {
      await fetch(`https://aisandbox-pa.googleapis.com/v1:batchLog`, {
        method: 'POST', headers, credentials: 'include',
        body: JSON.stringify(_buildBatchLogPayload()),
      });
    } else {
      await fetch(`https://aisandbox-pa.googleapis.com/v1/flow:batchLogFrontendEvents`, {
        method: 'POST', headers, credentials: 'include',
        body: JSON.stringify(_buildFrontendEventsPayload()),
      });
    }
  } catch { }
}

// Send telemetry at random intervals (45-120s) to look organic
function scheduleTelemetry() {
  const delay = _rand(45, 120) * 1000;
  setTimeout(async () => {
    await sendTelemetry();
    scheduleTelemetry(); // reschedule with new random interval
  }, delay);
}

// Refresh session ID every ~30min like a real user
setInterval(() => { _telemetrySessionId = `;${Date.now()}`; }, _rand(25, 35) * 60 * 1000);

scheduleTelemetry();

console.log('[Flow Agent] Extension loaded');
