"""Flow Engine — Common utilities for all generators.

Shared functions: client context builder, poll_status, download_video.
"""

import asyncio
import base64
import logging
import os
import random
import time
import uuid
import urllib.request

from ..config import (
    CLIENT_CTX, ENDPOINTS, POLL_INTERVAL, POLL_TIMEOUT,
)
from flow_server.media_types import sniff_media_type

log = logging.getLogger("flow_engine.generators")


def resolve_seed(seed: int | None, index: int = 0) -> int:
    """Resolve the seed for one request item.

    An explicit seed makes a generation reproducible, which is what lets a shot
    be re-rolled while holding its look. Items in a multi-variation batch are
    offset by their index so the takes still differ from each other but stay
    reproducible as a set. None keeps the original behaviour: a fresh random
    seed per request.
    """
    if seed is None:
        return random.randint(1, 9999)
    return (int(seed) + index) % 4294967296


def build_client_context(project_id: str) -> dict:
    """Build the clientContext dict used by all API requests."""
    return {
        "projectId": project_id,
        "tool": CLIENT_CTX["tool"],
        "userPaygateTier": CLIENT_CTX["tier"],
        "sessionId": f";{int(time.time() * 1000)}",
        "recaptchaContext": {
            "applicationType": CLIENT_CTX["recaptcha_app_type"],
            "token": "",
        },
    }


def build_generation_context(audio_pref: str = None) -> dict:
    """Build the mediaGenerationContext dict."""
    ctx = {"batchId": str(uuid.uuid4())}
    if audio_pref:
        ctx["audioFailurePreference"] = audio_pref
    return ctx


async def poll_status(bridge, media_id: str, project_id: str) -> bool:
    """Poll until video is ready. Returns True on success."""
    if hasattr(bridge, "_canvas_media_urls") and media_id in bridge._canvas_media_urls:
        log.info("Video ready via Canvas UI fallback! (%s)", media_id[:12])
        return True
    body = {"media": [{"name": media_id, "projectId": project_id}]}
    start = time.time()

    while time.time() - start < POLL_TIMEOUT:
        # Track 0: Official same-origin batchexecute RPC (as29s)
        try:
            from .. import batchexecute
            freq = batchexecute.media_request(media_id)
            rpc_res = await bridge.batch_rpc(
                rpcid=batchexecute.RPC_MEDIA,
                freq=freq,
                timeout=30.0,
            )
            if rpc_res.get("status") == 200:
                raw_media = rpc_res.get("data", "")
                payload = batchexecute.first_payload(raw_media, batchexecute.RPC_MEDIA)
                urls = batchexecute.read_media_urls(payload, media_id)
                if urls.video:
                    elapsed = int(time.time() - start)
                    log.info("Video ready via batchexecute as29s! (%ds) URL=%s...", elapsed, urls.video[:60])
                    if not hasattr(bridge, "_cached_video_urls"):
                        bridge._cached_video_urls = {}
                    bridge._cached_video_urls[media_id] = urls.video
                    return True
        except Exception:
            pass

        result = await bridge.api_request(ENDPOINTS["poll_status"], body, captcha_action="")
        data = result.get("data", {})
        media = data.get("media", [])

        if media:
            meta = media[0].get("mediaMetadata", {}).get("mediaStatus", {})
            status = meta.get("mediaGenerationStatus", "")

            if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                elapsed = int(time.time() - start)
                log.info("Video ready! (%ds)", elapsed)
                return True
            elif "FAILED" in status or "BLOCKED" in status:
                log.error("Failed: %s", status)
                return False

        elapsed = int(time.time() - start)
        log.info("Waiting... (%ds)", elapsed)
        await asyncio.sleep(POLL_INTERVAL)

    log.error("Timeout after %ds", POLL_TIMEOUT)
    return False


async def download_video(bridge, media_id: str, output_path: str) -> bool:
    """Download video from Google's signed URL, with legacy API fallback."""
    signed_url = None
    if hasattr(bridge, "_cached_video_urls") and media_id in bridge._cached_video_urls:
        signed_url = bridge._cached_video_urls[media_id]
    if not signed_url:
        try:
            from .. import batchexecute
            freq = batchexecute.media_request(media_id)
            rpc_res = await bridge.batch_rpc(rpcid=batchexecute.RPC_MEDIA, freq=freq, timeout=30.0)
            if rpc_res.get("status") == 200:
                payload = batchexecute.first_payload(rpc_res.get("data", ""), batchexecute.RPC_MEDIA)
                urls = batchexecute.read_media_urls(payload, media_id)
                if urls.video:
                    signed_url = urls.video
        except Exception:
            pass
    if signed_url:
        try:
            import urllib.request
            urllib.request.urlretrieve(signed_url, output_path)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                log.info("Video downloaded via official CDN stream to %s (%d bytes)", output_path, os.path.getsize(output_path))
                return True
        except Exception as e:
            log.warning("Stream download from CDN failed (%s), trying request_media_url...", e)

    signed_url = await bridge.request_media_url(media_id)
    if signed_url:
        if "/asb/" in signed_url:
            try:
                from ..canvas_driver import _download_canvas_image_b64
                b64 = await _download_canvas_image_b64(bridge, signed_url)
                if b64:
                    video_bytes = base64.b64decode(b64)
                    mime_type = sniff_media_type(video_bytes)
                    if mime_type.startswith("video/"):
                        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
                        with open(output_path, "wb") as f:
                            f.write(video_bytes)
                        log.info("Saved /asb/ video URL: %s (%.1f MB, %s)", output_path, len(video_bytes) / (1024 * 1024), mime_type)
                        return True
            except Exception as exc:
                log.warning("In-tab /asb/ video download failed for %s: %s", media_id, exc)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(signed_url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(request, timeout=90) as response:
                video_bytes = response.read()
            mime_type = sniff_media_type(video_bytes)
            if mime_type.startswith("video/"):
                os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
                with open(output_path, "wb") as f:
                    f.write(video_bytes)
                log.info("Saved signed media URL: %s (%.1f MB, %s)", output_path, len(video_bytes) / (1024 * 1024), mime_type)
                return True
            log.warning("Signed media URL for %s returned %s", media_id, mime_type)
        except Exception as exc:
            log.warning("Signed media URL download failed for %s: %s", media_id, exc)

    url_path = ENDPOINTS["get_media"].format(media_id=media_id)
    result = await bridge.api_request(url_path, {}, captcha_action="", method="GET")
    data = result.get("data", result)

    video_b64 = ""
    if isinstance(data, dict):
        v = data.get("video", {})
        if isinstance(v, dict):
            video_b64 = v.get("encodedVideo", "")
        elif isinstance(v, str):
            video_b64 = v

    if not video_b64:
        log.error("No video data in legacy response for %s: %r", media_id, result)
        return False

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    video_bytes = base64.b64decode(video_b64)
    mime_type = sniff_media_type(video_bytes)
    if not mime_type.startswith("video/"):
        log.error("Downloaded media %s is %s, not video", media_id, mime_type)
        return False
    with open(output_path, "wb") as f:
        f.write(video_bytes)

    size_mb = len(video_bytes) / (1024 * 1024)
    log.info("Saved: %s (%.1f MB, %s)", output_path, size_mb, mime_type)
    return True
