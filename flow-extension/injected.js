/**
 * Injected into MAIN world on labs.google — has access to window.grecaptcha
 * Also intercepts TRPC fetch responses to capture fresh signed media URLs.
 */
(() => {
if (window.__FLOW_AGENT_MAIN_INJECTED__) return;
window.__FLOW_AGENT_MAIN_INJECTED__ = true;

const SITE_KEY = '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV';

// ─── TRPC Response Monitor ─────────────────────────────────
// Monkey-patch fetch to intercept TRPC responses containing media URLs.
// Fresh signed GCS URLs are extracted and forwarded to the agent.

const _originalFetch = window.fetch;
window.fetch = async function (...args) {
  try {
    const opts = args[1];
    let authHeader = null;
    if (opts && opts.headers) {
      if (typeof opts.headers.get === 'function') {
        authHeader = opts.headers.get('authorization') || opts.headers.get('Authorization');
      } else if (typeof opts.headers === 'object') {
        authHeader = opts.headers['authorization'] || opts.headers['Authorization'];
      }
    }
    if (authHeader && /^bearer\s+ya29\./i.test(authHeader)) {
      const token = authHeader.replace(/^Bearer\s+/i, '').trim();
      window.dispatchEvent(new CustomEvent('TOKEN_INTERCEPTED', { detail: { token } }));
    }
  } catch {}

  const response = await _originalFetch.apply(this, args);
  try {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    // Only intercept TRPC calls on labs.google that return project/flow data
    if (url.includes('/fx/api/trpc/') && response.ok) {
      const clone = response.clone();
      clone.text().then(text => {
        if (text.includes('storage.googleapis.com/ai-sandbox-videofx/') || text.includes('flow-content.google/')) {
          window.dispatchEvent(new CustomEvent('TRPC_MEDIA_URLS', {
            detail: { url, body: text },
          }));
        }
      }).catch(() => {});
    }
  } catch {}
  return response;
};


window.addEventListener('GET_CAPTCHA', async ({ detail }) => {
  const { requestId, pageAction } = detail;
  try {
    try {
      Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
      Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
      document.hasFocus = () => true;
      window.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 200, clientY: 200 }));
    } catch (e) {}

    const exec = async (act) => {
      if (window.grecaptcha?.enterprise?.execute) {
        return await window.grecaptcha.enterprise.execute(SITE_KEY, act ? { action: act } : undefined);
      }
      if (window.grecaptcha?.execute) {
        return await window.grecaptcha.execute(SITE_KEY, act ? { action: act } : undefined);
      }
      throw new Error('grecaptcha execute unavailable');
    };

    let token = null;
    try {
      token = await exec(pageAction);
    } catch {
      try {
        token = await exec('IMAGE_GENERATION');
      } catch {
        try {
          token = await exec(null);
        } catch (finalErr) {
          await waitForGrecaptcha();
          token = await exec(pageAction || 'IMAGE_GENERATION');
        }
      }
    }

    window.dispatchEvent(new CustomEvent('CAPTCHA_RESULT', {
      detail: { requestId, token },
    }));
  } catch (e) {
    window.dispatchEvent(new CustomEvent('CAPTCHA_RESULT', {
      detail: { requestId, error: e.message },
    }));
  }
});

function waitForGrecaptcha(timeout = 10000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const check = () => {
      if (window.grecaptcha?.enterprise?.execute) return resolve();
      if (Date.now() - start > timeout) return reject(new Error('grecaptcha not available'));
      setTimeout(check, 200);
    };
    check();
  });
}

// ─── Video Upload Handler ───────────────────────────────────
window.addEventListener('UPLOAD_VIDEO', async ({ detail }) => {
  const { requestId, videoBase64, projectId } = detail;
  try {
    // Convert base64 to Blob
    const byteChars = atob(videoBase64);
    const byteArray = new Uint8Array(byteChars.length);
    for (let i = 0; i < byteChars.length; i++) {
      byteArray[i] = byteChars.charCodeAt(i);
    }
    const blob = new Blob([byteArray], { type: 'video/mp4' });

    // Step 1: POST start — get session URL
    const startResp = await _originalFetch('/fx/api/upload-video?action=start', {
      method: 'POST',
      credentials: 'include',
      headers: {
        'X-Upload-Project-Id': projectId || '',
        'X-Upload-Content-Type': 'video/mp4',
        'X-Upload-Content-Length': blob.size.toString(),
      },
    });
    const sessionUrl = startResp.headers.get('X-Upload-Session-Url') || '';
    const startData = await startResp.json().catch(() => ({}));
    // sessionUrl may be in header OR in response body
    const finalSessionUrl = sessionUrl || startData.sessionUrl || '';
    startData._sessionUrl = finalSessionUrl;
    startData._status = startResp.status;

    if (!finalSessionUrl) {
      window.dispatchEvent(new CustomEvent('UPLOAD_VIDEO_RESULT', {
        detail: { requestId, error: 'NO_SESSION_URL', startData },
      }));
      return;
    }

    // Step 2: PUT directly to GCS session URL with resumable upload headers
    const uploadResp = await _originalFetch(finalSessionUrl, {
      method: 'PUT',
      body: blob,
      headers: {
        'Content-Type': 'video/mp4',
        'X-Goog-Upload-Command': 'upload, finalize',
        'X-Goog-Upload-Offset': '0',
      },
    });
    const uploadData = await uploadResp.json().catch(() => ({}));
    uploadData._status = uploadResp.status;

    window.dispatchEvent(new CustomEvent('UPLOAD_VIDEO_RESULT', {
      detail: { requestId, startData, uploadData, status: uploadResp.status },
    }));
  } catch (e) {
    window.dispatchEvent(new CustomEvent('UPLOAD_VIDEO_RESULT', {
      detail: { requestId, error: e.message },
    }));
  }
});
})();
