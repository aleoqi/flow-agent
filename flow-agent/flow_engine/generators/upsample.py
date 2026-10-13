"""Flow Engine — Video upsampler (720p -> 1080p / 4K).

Flow generates video at 720p. The higher-resolution download offered in the
Flow UI is a second pass over the finished media against an upsampler model,
submitted asynchronously and polled with the ordinary video status endpoint.

The request shape below is part of Google's undocumented Flow API. The
resolution enum in particular is not published, so a rejected enum is retried
with the known spelling variants and finally omitted entirely — the upsampler
model key already encodes the target resolution. A rejected request never
starts a generation, so the ladder cannot double-charge.
"""

import logging

from ..config import (
    ENDPOINTS,
    VIDEO_UPSAMPLE_MODELS,
    VIDEO_UPSAMPLE_RESOLUTIONS,
)
from .common import build_client_context, build_generation_context, resolve_seed

log = logging.getLogger("flow_engine.generators.upsample")

# Ordered spelling candidates per tier. The configured value is always tried
# first; ``None`` means "send no resolution field at all".
_RESOLUTION_CANDIDATES = {
    "1080p": ("VIDEO_RESOLUTION_1080P", "VIDEO_RESOLUTION_1080p", None),
    "4k": ("VIDEO_RESOLUTION_4K", "VIDEO_RESOLUTION_4k", None),
}

# Substrings that identify a request rejected for its shape rather than for a
# real generation failure (bad media ID, no credits, unusual activity...).
_SCHEMA_REJECTION_HINTS = (
    "resolution",
    "invalid value",
    "invalid_argument",
    "unknown name",
    "cannot find field",
)


def normalise_resolution(resolution) -> str | None:
    """Map a user-facing resolution to an upsample tier key, or None.

    Returns None for the native 720p output, which needs no upsample pass.
    """
    key = str(resolution or "").strip().lower().replace(" ", "")
    if not key or key in {"720p", "720", "native", "source", "original"}:
        return None
    aliases = {
        "1080": "1080p",
        "1080p": "1080p",
        "fhd": "1080p",
        "hd": "1080p",
        "full_hd": "1080p",
        "fullhd": "1080p",
        "4k": "4k",
        "2160": "4k",
        "2160p": "4k",
        "uhd": "4k",
    }
    tier = aliases.get(key)
    if tier is None:
        raise ValueError(
            f"Unsupported video resolution {resolution!r}; use '720p', '1080p', or '4k'."
        )
    return tier


def _resolution_candidates(tier: str) -> list[str | None]:
    configured = VIDEO_UPSAMPLE_RESOLUTIONS.get(tier)
    ordered: list[str | None] = [configured] if configured else []
    for candidate in _RESOLUTION_CANDIDATES.get(tier, ()):
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _error_message(result: dict) -> str:
    data = result.get("data", {})
    reason = ""
    message = result.get("error", "Unknown")
    if isinstance(data, dict):
        error = data.get("error", {})
        if isinstance(error, dict):
            message = error.get("message", message)
            for detail in error.get("details", []) or []:
                if isinstance(detail, dict) and "reason" in detail:
                    reason = f" ({detail['reason']})"
                    break
        elif data:
            message = data
    return f"{message}{reason}"


def _looks_like_schema_rejection(status: int, message: str) -> bool:
    if status not in (400, 404):
        return False
    lowered = message.lower()
    return any(hint in lowered for hint in _SCHEMA_REJECTION_HINTS)


def _parse_media_ids(data: dict) -> list[str]:
    """Collect upsampled media IDs from either response shape."""
    if not isinstance(data, dict):
        return []

    ids: list[str] = []

    def _add(value):
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)

    for item in data.get("media", []) or []:
        if isinstance(item, dict):
            _add(item.get("name") or item.get("mediaId") or item.get("id"))

    # Older/legacy responses nest the new media under operations.
    for item in data.get("operations", []) or []:
        if not isinstance(item, dict):
            continue
        _add(item.get("name") or item.get("mediaId"))
        nested = item.get("media") or item.get("operation")
        if isinstance(nested, dict):
            _add(nested.get("name") or nested.get("mediaId"))

    return ids


async def upsample_video(
    bridge,
    media_id: str,
    aspect: str,
    project_id: str,
    resolution: str = "1080p",
    seed: int = None,
    scene_id: str = None,
) -> list[str] | None:
    """Submit an upsample pass for one finished video. Returns new media IDs.

    ``resolution`` accepts '1080p' or '4k'. Poll the returned IDs with
    ``flow_engine.generators.common.poll_status`` and download them the same way
    as any generated video.
    """
    tier = normalise_resolution(resolution)
    if tier is None:
        raise ValueError(
            "Upsampling to 720p is a no-op; Flow already generates at 720p."
        )

    model_key = VIDEO_UPSAMPLE_MODELS.get(tier)
    if not model_key:
        raise ValueError(f"No upsampler model configured for {tier}.")

    last_error = None
    for attempt, resolution_enum in enumerate(_resolution_candidates(tier)):
        request_item = {
            "clientContext": build_client_context(project_id),
            "aspectRatio": aspect,
            "videoModelKey": model_key,
            "seed": resolve_seed(seed),
            "metadata": {"sceneId": scene_id} if scene_id else {},
            "videoInput": {"mediaId": media_id},
        }
        if resolution_enum is not None:
            request_item["resolution"] = resolution_enum

        body = {
            "mediaGenerationContext": build_generation_context(),
            "clientContext": build_client_context(project_id),
            "requests": [request_item],
        }

        log.info(
            "Upsampling %s to %s [%s]%s",
            media_id[:12],
            tier,
            model_key,
            "" if resolution_enum is None else f" resolution={resolution_enum}",
        )
        result = await bridge.api_request(
            ENDPOINTS["upsample_video"], body, captcha_action="VIDEO_GENERATION"
        )

        status = result.get("status", 0)
        if status == 200:
            media_ids = _parse_media_ids(result.get("data", {}))
            if not media_ids:
                log.error("Upsample accepted but returned no media: %r", result.get("data"))
                return None
            log.info("Upsample submitted! %s -> %s", media_id[:12], ", ".join(media_ids))
            return media_ids

        last_error = _error_message(result)
        if _looks_like_schema_rejection(status, last_error) and attempt + 1 < len(
            _resolution_candidates(tier)
        ):
            log.warning(
                "Upsample rejected the request shape (%s): %s — retrying with the "
                "next resolution spelling.",
                status,
                last_error,
            )
            continue

        log.error("Upsample failed (%s): %s", status, last_error)
        raise ValueError(last_error)

    raise ValueError(last_error or "Upsample request was rejected.")


IMAGE_UPSAMPLE_RESOLUTIONS = {
    "2k": "UPSAMPLE_IMAGE_RESOLUTION_2K",
    "4k": "UPSAMPLE_IMAGE_RESOLUTION_4K",
}


async def upsample_image(
    bridge,
    media_id: str,
    project_id: str,
    resolution: str = "2k",
) -> dict:
    """Upsample an image to 2K or 4K using Google Flow's official cloud AI upsampler.

    Args:
        bridge: ExtensionBridge instance
        media_id: Media ID of the original generated image
        project_id: Flow project ID
        resolution: Target resolution ('2k' or '4k')

    Returns:
        dict with keys:
            - 'media_id': the new upscaled Google Flow media ID
            - 'encoded_image': base64-encoded bytes of the upscaled image
            - 'resolution': the target resolution
            - 'project_id': the project ID that succeeded
            - 'data': the full raw response data from Google
    """
    res_key = str(resolution or "2k").strip().lower()
    enum_val = IMAGE_UPSAMPLE_RESOLUTIONS.get(res_key, "UPSAMPLE_IMAGE_RESOLUTION_2K")
    endpoint = ENDPOINTS.get("upsample_image", "/v1/flow/upsampleImage")
    log.info("Google Flow upsampling image %s -> %s [%s]", media_id[:12], res_key, enum_val)

    # Collect project candidates: passed project_id first, then known fallback projects
    candidates = [project_id] if project_id else []
    for alt in ("0143adf4-5864-4cb4-abb5-fe4254ad0dc7", "576adec2-b74d-4cd9-93da-ad81378711db"):
        if alt and alt not in candidates:
            candidates.append(alt)

    last_error = ""
    status = 0
    for pid in candidates:
        body = {
            "mediaId": media_id,
            "targetResolution": enum_val,
            "clientContext": build_client_context(pid),
        }

        for attempt in range(2):
            result = await bridge.api_request(
                endpoint, body, captcha_action="IMAGE_GENERATION"
            )

            status = result.get("status", 0)
            if status == 200:
                data = result.get("data", {})
                media_info = data.get("media", {})
                new_media_id = media_info.get("name") or media_info.get("mediaId")
                encoded_image = data.get("encodedImage")
                log.info("Google Flow upsampled image successfully! %s -> %s (project: %s)", media_id[:12], new_media_id or "direct", pid)
                return {
                    "media_id": new_media_id or media_id,
                    "encoded_image": encoded_image,
                    "resolution": res_key,
                    "project_id": pid,
                    "data": data,
                }

            last_error = _error_message(result)
            if status == 403 and "unusual_activity" in last_error.lower() and attempt == 0:
                log.info("reCAPTCHA rate limit encountered; pausing 2.5s before retry...")
                import asyncio
                await asyncio.sleep(2.5)
                continue
            break

        if status in (400, 404) and len(candidates) > 1:
            log.warning("Upsample with project %s returned %s (%s); trying fallback project...", pid, status, last_error)
            continue
        break

    log.error("Google Flow image upsample failed (%s): %s", status, last_error)
    raise ValueError(f"Google Flow image upsample failed ({status}): {last_error}")

