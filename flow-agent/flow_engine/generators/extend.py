"""Flow Engine — Video Extend generator.

Absorbs Google Flow's official video extension wire protocol (veo_3_1_extension_lite).
Extends/continues an existing video clip by another 8s segment.
"""

import logging
import os
import uuid

from ..config import ASPECTS, ENDPOINTS
from .common import build_client_context, build_generation_context, resolve_seed

log = logging.getLogger("flow_engine.generators.extend")


async def extend_video(
    bridge,
    prompt: str,
    aspect: str,
    project_id: str,
    video_media_id: str,
    start_frame: int = 1,
    end_frame: int = 24,
    model_key: str = "veo_3_1_extension_lite",
    seed: int = None,
    client_id: str = None,
) -> list[str] | None:
    """Submit Video Extension request to Google Flow.

    Continues/extends an existing clip by 8 seconds (seeded by the source clip's end window).
    """
    aspect_mapped = ASPECTS.get(aspect.lower(), "VIDEO_ASPECT_RATIO_LANDSCAPE" if aspect.lower() == "landscape" else "VIDEO_ASPECT_RATIO_PORTRAIT")

    req_item = {
        "aspectRatio": aspect_mapped,
        "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
        "videoModelKey": model_key,
        "seed": resolve_seed(seed),
        "metadata": {},
        "videoInput": {
            "mediaId": video_media_id,
            "startFrameIndex": start_frame,
            "endFrameIndex": end_frame,
        },
    }

    body = {
        "mediaGenerationContext": build_generation_context("RETURN_SILENCED_VIDEOS"),
        "clientContext": build_client_context(project_id),
        "requests": [req_item],
        "useV2ModelConfig": True,
    }

    log.info(
        'Extending video: "%s" [%s] source_media=%s',
        prompt[:50],
        model_key,
        video_media_id[:12] if video_media_id else "None",
    )
    result = await bridge.api_request(
        ENDPOINTS.get("generate_extend", "/v1/video:batchAsyncGenerateVideoExtendVideo"),
        body,
        client_id=client_id,
    )

    status = result.get("status", 0)
    if status != 200:
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
        log.error("Video extend failed (%s): %s", status, err_msg)
        raise ValueError(f"Video extend failed: {err_msg}")

    data = result.get("data", {})
    media = data.get("media", [])
    if not media:
        log.error("No media in extend response")
        return None

    media_ids = [m.get("name") for m in media if m.get("name")]
    credits = data.get("remainingCredits", "?")
    log.info("Extend submitted! %d video(s), credits=%s", len(media_ids), credits)
    for mid in media_ids:
        log.info("   media_id=%s", mid)
    return media_ids
