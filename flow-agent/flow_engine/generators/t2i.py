"""Flow Agent — Text to Image (T2I) generator.

Ported from virtual-try/go-server/image.go
"""

import logging
import os
import re
import time

from ..config import DEFAULT_IMAGE_MODEL, IMAGE_MODELS
from .common import build_client_context, build_generation_context
from flow_server.media_types import sniff_media_type

log = logging.getLogger("flow_engine.generators.t2i")

# Image aspect ratios (more options than video)
IMAGE_ASPECTS = {
    "landscape": "IMAGE_ASPECT_RATIO_LANDSCAPE",   # 16:9
    "4x3":       "IMAGE_ASPECT_RATIO_4_3",          # 4:3
    "square":    "IMAGE_ASPECT_RATIO_SQUARE",        # 1:1
    "3x4":       "IMAGE_ASPECT_RATIO_PORTRAIT",      # 3:4 maps to vertical portrait in Google Flow
    "portrait":  "IMAGE_ASPECT_RATIO_PORTRAIT",      # 9:16
}

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _parse_image_results(data: dict) -> list[dict]:
    """Parse all images from batchGenerateImages response.

    Returns list of {"media_id": str, "image_url": str}
    """
    results = []
    media_list = data.get("media", [])

    for item in media_list:
        r = {"media_id": "", "image_url": ""}

        # media[i].name = mediaId
        name = item.get("name", "")
        if UUID_RE.match(name):
            r["media_id"] = name

        # media[i].image.generatedImage.fifeUrl
        img = item.get("image", {})
        gen = img.get("generatedImage", {})
        url = gen.get("fifeUrl", "") or gen.get("imageUri", "") or item.get("fife_url", "")
        if url:
            r["image_url"] = url
            # Fallback: extract mediaId from URL
            if not r["media_id"]:
                match = UUID_RE.search(url)
                if match:
                    r["media_id"] = match.group()
        if img.get("encodedImage"):
            r["encoded_image"] = img["encodedImage"]

        results.append(r)

    return results


async def generate_image(bridge, prompt: str, aspect: str, project_id: str,
                         count: int = 1, ref_media_ids: list[str] = None,
                         model: str = None, seed: int = None) -> list[dict] | None:
    """Generate images from text prompt.

    Args:
        bridge: ExtensionBridge instance
        prompt: Text prompt for image
        aspect: Aspect ratio key (portrait/landscape/square/4x3/3x4)
        project_id: Flow project ID
        count: Number of variations (1-4)
        ref_media_ids: Optional reference image media IDs
        model: Optional image model name (harbor_seal, narwhal, gem_pix_2, etc.)
        seed: Optional explicit seed; reproducible when set, timestamp-derived when not

    Returns:
        List of {"media_id": str, "image_url": str} or None on error
    """
    count = max(1, min(4, count))
    ts = int(time.time() * 1000)

    aspect_ratio = IMAGE_ASPECTS.get(aspect, aspect)
    target_model = IMAGE_MODELS.get((model or "").lower(), DEFAULT_IMAGE_MODEL)

    # Track 0: Official same-origin batchexecute RPC (ogiZ0b)
    try:
        from .. import batchexecute
        freq = batchexecute.image_request(
            prompt=prompt,
            project_id=project_id,
            count=count,
            aspect=aspect,
            seed=seed,
            model=model or "GEM_PIX_2",
            ref_media_ids=ref_media_ids,
        )
        rpc_res = await bridge.batch_rpc(
            rpcid=batchexecute.RPC_GEN_IMAGE,
            freq=freq,
            captcha_action="IMAGE_GENERATION",
            timeout=180.0,
        )
        if rpc_res.get("status") == 200:
            raw_text = rpc_res.get("data", "")
            payload = batchexecute.first_payload(raw_text, batchexecute.RPC_GEN_IMAGE)
            gen_imgs = batchexecute.read_images(payload)
            if gen_imgs:
                results = [
                    {
                        "media_id": img.media_id,
                        "image_url": img.url,
                        "_model": target_model,
                        "_client_id": rpc_res.get("_client_id", ""),
                        "_track": "batchexecute_ogiz0b",
                    }
                    for img in gen_imgs
                ]
                log.info("batchexecute ogiZ0b generated %d image(s) successfully!", len(results))
                return results
    except Exception as exc:
        log.warning("batchexecute ogiZ0b failed (%s); attempting legacy REST fallback...", exc)

    # Build N items in requests array (1 API call = N images)
    requests = []
    for i in range(count):
        req_item = {
            "clientContext": build_client_context(project_id),
            "seed": ((int(seed) + i) % 4294967296) if seed is not None else (ts + i * 1000) % 1000000,
            "imageModelName": target_model,
            "imageAspectRatio": aspect_ratio,
            "structuredPrompt": {"parts": [{"text": prompt}]},
            "imageInputs": [
                {"name": mid, "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"}
                for mid in ref_media_ids
            ] if ref_media_ids else [],
        }

        requests.append(req_item)

    body = {
        "clientContext": build_client_context(project_id),
        "mediaGenerationContext": build_generation_context(),
        "useNewMedia": True,
        "requests": requests,
    }

    # Endpoint: /v1/projects/{projectId}/flowMedia:batchGenerateImages
    endpoint = f"/v1/projects/{project_id}/flowMedia:batchGenerateImages"

    log.info('  Generating: "%s" [%s] x%d', prompt[:50], aspect, count)
    result = await bridge.api_request(
        endpoint, body, captcha_action="IMAGE_GENERATION", timeout=300,
        meta={"prompt": prompt, "count": count},
    )

    client_id = result.get("_client_id", "") if isinstance(result, dict) else ""
    parsed = _parse_image_results(result.get("data", {}))
    for p in parsed:
        p["_client_id"] = client_id

    status = result.get("status", 0)
    if status != 200:
        if result.get("error") == "TIMEOUT":
            err_msg = (
                "timed out waiting for Google Flow to respond. The image may still "
                "have been generated — check labs.google/fx/tools/flow. Try again or "
                "raise API_REQUEST_TIMEOUT if this recurs."
            )
            log.error("Timeout: %s", err_msg)
            raise ValueError(err_msg)
        err = result.get("data", {})
        reason = ""
        if isinstance(err, dict):
            details = err.get("error", {}).get("details", [])
            for detail in details:
                if isinstance(detail, dict) and "reason" in detail:
                    reason = f" ({detail['reason']})"
                    break
            err = err.get("error", {}).get("message", result.get("error", "Unknown"))
        err_msg = f"{err}{reason}"
        upper_err = err_msg.upper()
        log.error("Track 1 REST T2I failed (%s): %s", status, err_msg)
        raise ValueError(f"Google Flow REST T2I/R2I failed ({status}): {err_msg}")


    data = result.get("data", {})
    client_id = result.get("_client_id")
    results = _parse_image_results(data)

    if not results:
        log.error("No images in response")
        return None

    # Inject client_id into each result for history tracking
    for r in results:
        r["_model"] = target_model
        if client_id:
            r["_client_id"] = client_id

    credits = data.get("remainingCredits", "?")
    log.info("Generated! %d image(s), credits=%s", len(results), credits)
    for r in results:
        log.info("   media_id=%s", r["media_id"][:12] if r["media_id"] else "?")

    return results


async def download_image(bridge, image_url: str, output_path: str, client_id: str | None = None) -> bool:
    """Download a generated image to disk.

    Supports both public signed GCS URLs and cookie-authenticated `/asb/` URLs
    via in-tab `FETCH_BASE64`.
    """
    import base64
    import urllib.request
    import time as _time

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if "/asb/" in (image_url or ""):
        from ..canvas_driver import _download_canvas_image_b64
        b64 = await _download_canvas_image_b64(bridge, image_url, client_id=client_id)
        if b64:
            image_bytes = base64.b64decode(b64)
            mime_type = sniff_media_type(image_bytes, filename=image_url)
            if mime_type.startswith("image/"):
                with open(output_path, "wb") as f:
                    f.write(image_bytes)
                log.info("Saved (/asb/ in-tab): %s (%.0f KB, %s)", output_path, len(image_bytes) / 1024, mime_type)
                return True

    # Bypass any *_proxy env vars: signed GCS URLs 403 through most proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    last_err = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=60) as resp:
                image_bytes = resp.read()
                declared_mime = resp.headers.get("Content-Type", "")
            mime_type = sniff_media_type(
                image_bytes, declared_mime=declared_mime, filename=image_url
            )
            if not mime_type.startswith("image/"):
                raise ValueError(f"downloaded response is {mime_type}, not an image")
            with open(output_path, "wb") as f:
                f.write(image_bytes)
            size_kb = os.path.getsize(output_path) / 1024
            if size_kb <= 0:
                raise ValueError("empty download")
            log.info("Saved: %s (%.0f KB, %s)", output_path, size_kb, mime_type)
            return True
        except Exception as e:
            last_err = e
            log.warning("Download attempt %d/3 failed: %s", attempt, e)
            if attempt < 3:
                _time.sleep(1.5 * attempt)

    log.error("Download failed after 3 attempts: %s", last_err)
    return False
