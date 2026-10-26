"""Flow Engine — Image to Video (I2V) generator + image upload."""

import base64
import logging
import os
from ..config import CLIENT_CTX, ENDPOINTS
from .common import build_client_context, build_generation_context, resolve_seed
from flow_server.media_history import get_revalidated_upload_id, record_uploaded_media
from flow_server.media_types import sniff_media_type

log = logging.getLogger("flow_engine.generators.i2v")


async def upload_image(
    bridge,
    image_path: str,
    project_id: str = None,
    *,
    force_upload: bool = False,
) -> str | None:
    """Upload a local image to Flow. Returns media_id.

    A content-addressed cached ID is remotely revalidated before it is reused.
    """
    from ..config import DEFAULT_PROJECT
    project_id = project_id or DEFAULT_PROJECT
    image_path = os.path.abspath(image_path)
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    mime_type = sniff_media_type(image_path)
    if not mime_type.startswith("image/"):
        raise ValueError(
            f"Expected an image file, but {os.path.basename(image_path)} contains {mime_type}."
        )

    from ..ledger import get_media_ledger
    ledger = get_media_ledger()
    sha256_hash = ledger.compute_file_sha256(image_path)
    client_id = getattr(bridge, "selected_client_id", None) or os.environ.get("FLOW_CLIENT_ID", "")

    if not force_upload:
        cached_media_id = ledger.lookup(sha256_hash, client_id, project_id)
        if cached_media_id:
            log.info("Four-dimensional media ledger HIT for %s -> %s (0 upload required)", os.path.basename(image_path), cached_media_id)
            return cached_media_id

    with open(image_path, "rb") as f:
        img_bytes = f.read()
    img_data = base64.b64encode(img_bytes).decode("ascii")

    # Track 0: Official same-origin batchexecute RPC (maseQ)
    try:
        from .. import batchexecute
        freq = batchexecute.upload_request(
            image_b64=img_data,
            project_id=project_id,
            mime_type=mime_type,
            file_name=os.path.basename(image_path),
        )
        rpc_res = await bridge.batch_rpc(
            rpcid=batchexecute.RPC_UPLOAD_IMAGE,
            freq=freq,
            captcha_action="IMAGE_GENERATION",
            timeout=60.0,
        )
        if rpc_res.get("status") == 200:
            payload = batchexecute.first_payload(rpc_res.get("data", ""), batchexecute.RPC_UPLOAD_IMAGE)
            media_id = batchexecute.read_uploaded_media_id(payload)
            if media_id:
                log.info("batchexecute maseQ upload succeeded: %s -> %s", os.path.basename(image_path), media_id)
                ledger.record(sha256_hash, client_id, project_id, media_id, os.path.basename(image_path))
                record_uploaded_media(image_path, media_id, project_id=project_id, mime_type=mime_type)
                return media_id
    except Exception as exc:
        log.warning("batchexecute maseQ upload failed (%s); attempting legacy REST upload...", exc)

    body = {
        "clientContext": {"tool": CLIENT_CTX["tool"], "projectId": project_id},
        "imageBytes": img_data,
        "mimeType": mime_type,
    }

    log.info("Uploading image: %s", os.path.basename(image_path))
    result = await bridge.api_request(ENDPOINTS["upload_image"], body, captcha_action=None)

    status = result.get("status", 0)
    data = result.get("data", {})
    if status != 200:
        err = data.get("error", {}).get("message", "Unknown") if isinstance(data, dict) else str(data)
        err_msg = f"Image upload failed: {err} | status={status} | full_result={result}"
        log.error("%s", err_msg)
        raise ValueError(err_msg)

    media_id = data.get("mediaId") or data.get("name")
    if not media_id and isinstance(data.get("media"), dict):
        media_id = data["media"].get("name")
    log.info("Image uploaded! media_id=%s", media_id)

    if media_id:
        record_uploaded_media(
            image_path,
            media_id,
            project_id=project_id,
            mime_type=mime_type,
        )

    return media_id


async def generate_video_i2v(bridge, prompt: str, aspect: str, project_id: str,
                              image_media_id: str, duration: int = 8, count: int = 1,
                              seed: int = None, video_model: str = None) -> list[str] | None:
    """Generate video from a start image using same-origin batchexecute RPC.

    Tries eb1hJf (start-frame I2V) first, then falls back to MZZa6b (Omni Reference).
    Hard-fails on error without silent regression to legacy REST or DOM automation.
    """
    from .. import batchexecute
    resolved_aspect = batchexecute.resolve_video_aspect(aspect)
    last_error = None

    # Track 0a: Official same-origin batchexecute RPC (eb1hJf, First Frame)
    try:
        freq = batchexecute.omni_first_frame_request(
            prompt=prompt,
            project_id=project_id,
            source_media_id=image_media_id,
            duration_s=duration,
            resolution="720p",
            aspect=resolved_aspect,
        )
        rpc_res = await bridge.batch_rpc(
            rpcid=batchexecute.RPC_GEN_VIDEO,
            freq=freq,
            captcha_action="VIDEO_GENERATION",
            timeout=60.0,
        )
        if rpc_res.get("status") == 200:
            submit_payload = batchexecute.first_payload(rpc_res.get("data", ""), batchexecute.RPC_GEN_VIDEO)
            operation = batchexecute.read_operation(submit_payload)
            target_mid = None
            if isinstance(operation.status, list) and len(operation.status) > 4:
                target_mid = operation.status[4]
            final_mid = target_mid or batchexecute.find_media_id_in_text(rpc_res.get("data", ""), operation.operation_id) or operation.operation_id
            log.info("batchexecute eb1hJf I2V video submitted! media_id=%s, op=%s", final_mid, operation.operation_id)
            return [final_mid]
        else:
            log.warning("batchexecute eb1hJf status=%s: %s", rpc_res.get("status"), rpc_res)
            last_error = f"eb1hJf status {rpc_res.get('status')}"
    except Exception as exc:
        log.warning("batchexecute eb1hJf failed (%s), attempting Omni Reference MZZa6b...", exc)
        last_error = str(exc)

    # Track 0b: Official same-origin batchexecute RPC (MZZa6b, Omni Reference with single image)
    try:
        log.info("Generating video via batchexecute MZZa6b (Omni Reference with image %s)...", image_media_id[:12])
        freq = batchexecute.omni_reference_video_request(
            prompt=prompt,
            project_id=project_id,
            reference_media_ids=[image_media_id],
            duration_s=duration,
            resolution="720p",
            aspect=resolved_aspect,
        )
        rpc_res = await bridge.batch_rpc(
            rpcid=batchexecute.RPC_GEN_VIDEO_REFERENCES,
            freq=freq,
            captcha_action="VIDEO_GENERATION",
            timeout=60.0,
        )
        if rpc_res.get("status") == 200:
            submit_payload = batchexecute.first_payload(rpc_res.get("data", ""), batchexecute.RPC_GEN_VIDEO_REFERENCES)
            operation = batchexecute.read_operation(submit_payload)
            target_mid = None
            if isinstance(operation.status, list) and len(operation.status) > 4:
                target_mid = operation.status[4]
            final_mid = target_mid or operation.operation_id
            log.info("batchexecute MZZa6b reference video submitted! media_id=%s, op=%s", final_mid, operation.operation_id)
            return [final_mid]
        else:
            err_msg = f"batchexecute MZZa6b status={rpc_res.get('status')}: {rpc_res}"
            log.error("%s", err_msg)
            raise RuntimeError(err_msg)
    except Exception as exc:
        log.error("batchexecute MZZa6b failed: %s", exc)
        raise RuntimeError(f"Google Flow batchexecute video generation failed: {exc} (initial attempt: {last_error})")

async def generate_video_fl(bridge, prompt: str, aspect: str, project_id: str,
                             start_image_id: str, end_image_id: str,
                             duration: int = 8, count: int = 1,
                             seed: int = None,
                             video_model: str = None) -> list[str] | None:
    """Generate video with First+Last frame control.

    Video transitions smoothly from start_image to end_image.
    """
    model_key = video_model or f"abra_t2v_{duration}s"

    requests = []
    for i in range(count):
        requests.append({
            "aspectRatio": aspect,
            "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
            "videoModelKey": model_key,
            "seed": resolve_seed(seed, i),
            "metadata": {},
            "startImage": {"mediaId": start_image_id},
            "endImage": {"mediaId": end_image_id},
        })

    body = {
        "mediaGenerationContext": build_generation_context(),
        "clientContext": build_client_context(project_id),
        "requests": requests,
        "useV2ModelConfig": True,
    }

    log.info('FL: "%s" start=%s end=%s', prompt[:40], start_image_id[:12], end_image_id[:12])
    result = await bridge.api_request(ENDPOINTS["generate_fl"], body)

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
        upper_err = err_msg.upper()
        if status == 403 or any(k in upper_err for k in ("PUBLIC_ERROR_UNUSUAL_ACTIVITY", "UNUSUAL_ACTIVITY", "RECAPTCHA", "403")):
            log.error("Google Flow 403 or Unusual Activity encountered in FL: %s", err_msg)
            raise ValueError(f"Google Flow FL 403 / Recaptcha triggered: {err_msg}")
        log.error("FL failed (%s): %s", status, err_msg)
        raise ValueError(err_msg)

    data = result.get("data", {})
    media = data.get("media", [])
    if not media:
        log.error("No media in response")
        return None

    media_ids = [m.get("name") for m in media]
    credits = data.get("remainingCredits", "?")
    log.info("FL submitted! %d video(s), credits=%s", len(media_ids), credits)
    return media_ids


async def generate_video_r2v(bridge, prompt: str, aspect: str, project_id: str,
                              ref_media_ids: list[str],
                              duration: int = 8, count: int = 1,
                              seed: int = None, video_model: str = None) -> list[str] | None:
    """Generate video from reference images (character/style consistency) via batchexecute MZZa6b."""
    # Track 0: Official same-origin batchexecute RPC (MZZa6b)
    from .. import batchexecute
    resolved_aspect = batchexecute.resolve_video_aspect(aspect)
    try:
        freq = batchexecute.omni_reference_video_request(
            prompt=prompt,
            project_id=project_id,
            reference_media_ids=ref_media_ids,
            duration_s=duration,
            resolution="720p",
            aspect=resolved_aspect,
        )
        rpc_res = await bridge.batch_rpc(
            rpcid=batchexecute.RPC_GEN_VIDEO_REFERENCES,
            freq=freq,
            captcha_action="VIDEO_GENERATION",
            timeout=60.0,
        )
        if rpc_res.get("status") == 200:
            submit_payload = batchexecute.first_payload(rpc_res.get("data", ""), batchexecute.RPC_GEN_VIDEO_REFERENCES)
            operation = batchexecute.read_operation(submit_payload)
            target_mid = None
            if isinstance(operation.status, list) and len(operation.status) > 4:
                target_mid = operation.status[4]
            final_mid = target_mid or operation.operation_id
            log.info("batchexecute MZZa6b reference video submitted! media_id=%s, op=%s", final_mid, operation.operation_id)
            return [final_mid]
        else:
            err_msg = f"batchexecute MZZa6b status={rpc_res.get('status')}: {rpc_res}"
            log.error("%s", err_msg)
            raise RuntimeError(err_msg)
    except Exception as exc:
        log.error("batchexecute MZZa6b failed: %s", exc)
        raise RuntimeError(f"Google Flow batchexecute R2V failed: {exc}")
