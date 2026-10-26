"""Flow Engine — Track 2 Canvas UI & Native Flow Agent Driver.

Provides DOM-driven fallback generation (Image & Video) and native Flow Canvas
Agent (`button.agent-mode-chip`) interaction via Chrome extension `eval_tab`.
"""

import asyncio
import base64
import logging
import re
import time
import urllib.request
import uuid as _uuid

from .bridge import ExtensionBridge, target_client_id_var

log = logging.getLogger("flow_engine.canvas_driver")

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

_AGENT_TEXT_SELECTOR = (
    ".agent-row.message-row .agent-message-content, "
    ".agent-bubble .agent-message-content, "
    "flow-chat-view .agent-message-content, "
    ".agent-panel-content .agent-message-content, "
    ".agent-panel-wrapper [class*='message-content'], "
    ".agent-panel-content p, "
    "[class*='agent'] [class*='message']"
)


def _base_url(url: str) -> str:
    return (url or "").split("?")[0].split("=")[0].strip()


def _extract_uuid(url: str) -> str:
    m = UUID_RE.search(url or "")
    return m.group(0) if m else str(_uuid.uuid4())


def _download_bytes_direct(url: str, timeout: int = 60) -> bytes | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(1, 3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read()
                if data:
                    return data
        except Exception as exc:
            log.warning("Direct download attempt %d/2 failed for %s: %s", attempt, url[:80], exc)
            time.sleep(1.0)
    return None


async def _download_canvas_image_b64(bridge: ExtensionBridge, url: str, client_id: str | None = None) -> str:
    """Download image via in-tab FETCH_BASE64 (carries flow.google.com cookies for /asb/ URLs) with direct fallback."""
    try:
        res = await bridge.eval_tab(
            url_pattern="flow.google.com/project",
            action="FETCH_BASE64",
            payload={"url": url},
            timeout=30,
            client_id=client_id,
        )
        inner = res.get("result", {}) if isinstance(res, dict) else {}
        if isinstance(inner, dict) and inner.get("b64"):
            return inner["b64"]
    except Exception as exc:
        log.warning("In-tab FETCH_BASE64 failed for %s: %s", url[:80], exc)
    raw_bytes = await asyncio.to_thread(_download_bytes_direct, url, 60)
    return base64.b64encode(raw_bytes).decode("ascii") if raw_bytes else ""


def _register_canvas_url(bridge: ExtensionBridge, media_id: str, url: str) -> None:
    if not hasattr(bridge, "_canvas_media_urls"):
        bridge._canvas_media_urls = {}
    if media_id and url:
        bridge._canvas_media_urls[media_id] = url


async def canvas_extract_media(
    bridge: ExtensionBridge,
    client_id: str | None = None,
) -> dict:
    """Extract all current video/image signed URLs from the active Flow project tab.

    Automatically dispatches hover events across thumbnails/cards to trigger
    Angular lazy-loading of ``<video src="...">`` elements before reading DOM.
    """
    cid = client_id or target_client_id_var.get()
    res = await bridge.eval_tab(
        url_pattern="flow.google.com/project",
        action="EXTRACT_MEDIA_URLS",
        timeout=25,
        client_id=cid,
    )
    inner = res.get("result", {}) if isinstance(res, dict) else {}
    if not isinstance(inner, dict):
        inner = {}
    videos = inner.get("videos") or []
    images = inner.get("images") or []
    perf_urls = inner.get("perfUrls") or []

    for v_url in videos:
        vid_id = _extract_uuid(v_url)
        _register_canvas_url(bridge, vid_id, v_url)

    return {
        "ok": bool(res.get("success", True)) if isinstance(res, dict) else False,
        "videos": videos,
        "images": images,
        "perfUrls": perf_urls,
        "client_id": res.get("_client_id", cid or "") if isinstance(res, dict) else (cid or ""),
    }


async def canvas_set_agent_mode(
    bridge: ExtensionBridge,
    enabled: bool = False,
    client_id: str | None = None,
) -> dict:
    """Toggle the native Flow Canvas Agent chip (`button.agent-mode-chip`)."""
    cid = client_id or target_client_id_var.get()
    res = await bridge.eval_tab(
        url_pattern="flow.google.com/project",
        action="SET_AGENT_MODE",
        payload={"enabled": bool(enabled)},
        timeout=15,
        client_id=cid,
    )
    inner = res.get("result", {}) if isinstance(res, dict) else {}
    return {
        "ok": bool(res.get("success", False)) if isinstance(res, dict) else False,
        **(inner if isinstance(inner, dict) else {}),
        "client_id": res.get("_client_id", cid or "") if isinstance(res, dict) else (cid or ""),
    }


async def _bind_frame_by_caption_or_id(
    bridge: ExtensionBridge,
    slot_label: str,
    caption_or_id: str,
    client_id: str | None = None,
) -> bool:
    """Bind start ('开始') or end ('结束') frame on the Flow Canvas UI."""
    if not caption_or_id:
        return False

    log.info("Canvas UI: binding %s frame -> %s", slot_label, caption_or_id[:40])
    await bridge.eval_tab(
        url_pattern="flow.google.com/project",
        action="CLICK",
        payload={"selector": "button, div[role='button']", "textMatch": slot_label},
        timeout=15,
        client_id=client_id,
    )
    await asyncio.sleep(0.7)

    # First try matching by alt/text or src containing UUID
    if UUID_RE.fullmatch(caption_or_id.strip()):
        click_res = await bridge.eval_tab(
            url_pattern="flow.google.com/project",
            action="CLICK",
            payload={"selector": f'img[src*="{caption_or_id.strip()}"]'},
            timeout=15,
            client_id=client_id,
        )
    else:
        click_res = await bridge.eval_tab(
            url_pattern="flow.google.com/project",
            action="CLICK",
            payload={
                "selector": 'img.thumbnail, [role="option"], [class*="card"], div[role="button"], span',
                "textMatch": caption_or_id,
            },
            timeout=15,
            client_id=client_id,
        )

    await asyncio.sleep(0.5)
    # Click '添加到提示' if a confirmation button is shown
    await bridge.eval_tab(
        url_pattern="flow.google.com/project",
        action="CLICK",
        payload={"selector": "button, div[role='button']", "textMatch": "添加到提示"},
        timeout=10,
        client_id=client_id,
    )
    await asyncio.sleep(0.4)
    return bool(click_res and click_res.get("success"))


_CANVAS_LOCKS: dict[str, asyncio.Lock] = {}


def _get_canvas_lock(client_id: str) -> asyncio.Lock:
    key = client_id or "__default__"
    if key not in _CANVAS_LOCKS:
        _CANVAS_LOCKS[key] = asyncio.Lock()
    return _CANVAS_LOCKS[key]


def _candidate_clients(bridge: ExtensionBridge, primary_cid: str | None) -> list[str]:
    connected = list(getattr(bridge, "_clients", {}).keys())
    if primary_cid and primary_cid in connected:
        return [primary_cid]
    if "client-jhbh11" in connected:
        return ["client-jhbh11"]
    return [primary_cid] if primary_cid else []


async def canvas_generate_image(
    bridge: ExtensionBridge,
    prompt: str,
    aspect: str = "16:9",
    count: int = 1,
    project_id: str | None = None,
    timeout: int = 180,
    client_id: str | None = None,
) -> dict:
    """Canvas UI DOM generation is completely banned by Rule XVI and Rule XXII."""
    raise RuntimeError("Canvas UI DOM image generation is completely banned by Rule XVI/XXII. Use native REST / batchexecute channel.")


async def canvas_generate_video(
    bridge: ExtensionBridge,
    prompt: str,
    aspect: str = "16:9",
    duration: int = 4,
    start_caption: str | None = None,
    end_caption: str | None = None,
    project_id: str | None = None,
    timeout: int = 240,
    client_id: str | None = None,
) -> dict:
    """Canvas UI DOM generation is completely banned by Rule XVI and Rule XXII."""
    raise RuntimeError("Canvas UI DOM video generation is completely banned by Rule XVI/XXII. Use native REST / batchexecute channel.")


async def canvas_invoke_agent(
    bridge: ExtensionBridge,
    prompt: str,
    wait_seconds: int = 45,
    auto_reset_mode: bool = True,
    client_id: str | None = None,
) -> dict:
    """Interact with Google Flow's native Canvas Agent (`button.agent-mode-chip`)."""
    cid = client_id or target_client_id_var.get() or bridge._select_client()
    start_ts = time.monotonic()

    pre_texts = set(await _query_page_texts(bridge, client_id=cid))
    pre_media = await canvas_extract_media(bridge, client_id=cid)
    pre_img_bases = {_base_url(img.get("src", "")) for img in pre_media.get("images", []) if img.get("src")}
    pre_vid_bases = {_base_url(v) for v in pre_media.get("videos", []) if v}

    interact_res = await bridge.eval_tab(
        url_pattern="flow.google.com/project",
        action="AGENT_INTERACT",
        payload={"prompt": prompt},
        timeout=30,
        client_id=cid,
    )
    submitted_info = interact_res.get("result", interact_res) if isinstance(interact_res, dict) else {}

    new_texts: list[str] = []
    new_images: list[dict] = []
    new_videos: list[str] = []

    try:
        deadline = time.monotonic() + max(4, int(wait_seconds))
        while time.monotonic() < deadline:
            await asyncio.sleep(4.0)
            cur_texts = await _query_page_texts(bridge, client_id=cid)
            for t in cur_texts:
                if t not in pre_texts and t != prompt.strip() and t not in new_texts:
                    new_texts.append(t)

            cur_media = await canvas_extract_media(bridge, client_id=cid)
            for img in cur_media.get("images", []):
                src = img.get("src", "")
                base = _base_url(src)
                if base and base not in pre_img_bases:
                    pre_img_bases.add(base)
                    new_images.append(img)
            for v_url in cur_media.get("videos", []):
                base = _base_url(v_url)
                if base and base not in pre_vid_bases:
                    pre_vid_bases.add(base)
                    new_videos.append(v_url)
    finally:
        if auto_reset_mode:
            await canvas_set_agent_mode(bridge, False, client_id=cid)

    elapsed = round(time.monotonic() - start_ts, 2)
    return {
        "ok": bool(interact_res.get("success", False)) if isinstance(interact_res, dict) else False,
        "prompt": prompt,
        "submitted": submitted_info,
        "new_texts": new_texts,
        "new_images": new_images,
        "new_videos": new_videos,
        "elapsed_seconds": elapsed,
        "client_id": cid or "",
    }
