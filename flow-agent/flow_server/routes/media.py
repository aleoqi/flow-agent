#!/usr/bin/env python3
"""Media routes for Flow Agent.

Upload, generation history management, and serving of generated assets.
"""

import os
import uuid
import time
import base64
import logging

from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Header
from fastapi.responses import FileResponse

from flow_engine import DEFAULT_PROJECT
from flow_engine.bridge import target_client_id_var

from flow_server.config import OUTPUT_DIR, public_url
from flow_server.history import (
    MediaNotFoundError,
    clear_history,
    derive_filename,
    load_history,
    remove_history,
    write_history,
)
from flow_server.media_types import extension_for_mime, sniff_media_type
from flow_server.models import UploadRequest
from flow_server.state import verify_api_key, get_active_bridge, publish, append_to_history

# Setup logging (format configured centrally in flow_engine/__init__.py, imported above)
log = logging.getLogger("flow_engine.openai_api")

router = APIRouter()


import asyncio

@router.get("/v1/media/{media_id}/url", dependencies=[Depends(verify_api_key)])
async def resolve_media_url(media_id: str):
    """Resolve a ready Flow media ID to a fresh signed CDN URL."""
    active_bridge = await get_active_bridge()
    url = await active_bridge.request_media_url(media_id)
    if not url:
        raise HTTPException(status_code=404, detail=f"No signed URL available for media {media_id}")
    return {"media_id": media_id, "url": url}


@router.post("/v1/test/raw")
async def test_raw_api(req: dict):
    """Debug endpoint to test arbitrary Google Flow API calls through the bridge."""
    active_bridge = await get_active_bridge()
    action = req.get("action", "api_request")
    if action == "api_request":
        endpoint = req.get("endpoint", "/v1/flow/upsampleImage")
        body = req.get("body", {})
        captcha = req.get("captcha_action")
        client_id = req.get("client_id")
        method = req.get("method", "POST")
        return await active_bridge.api_request(endpoint, body, captcha_action=captcha, method=method, client_id=client_id)
    elif action == "trpc":
        client_id = active_bridge._select_client()
        req_id = str(uuid.uuid4())
        fut = active_bridge._loop.create_future()
        active_bridge._pending[req_id] = fut
        await active_bridge.send_message_to(client_id, {
            "id": req_id,
            "method": "trpc_request",
            "params": req.get("params", {}),
        })
        return await asyncio.wait_for(fut, timeout=30)
    elif action == "get_media_url":
        client_id = active_bridge._select_client()
        req_id = str(uuid.uuid4())
        fut = active_bridge._loop.create_future()
        active_bridge._pending[req_id] = fut
        await active_bridge.send_message_to(client_id, {
            "id": req_id,
            "method": "get_media_url",
            "params": req.get("params", {}),
        })
        return await asyncio.wait_for(fut, timeout=30)
    elif action == "solve_captcha":
        client_id = req.get("client_id") or active_bridge._select_client()
        req_id = str(uuid.uuid4())
        fut = active_bridge._loop.create_future()
        active_bridge._pending[req_id] = fut
        await active_bridge.send_message_to(client_id, {
            "id": req_id,
            "method": "solve_captcha",
            "params": {"captchaAction": req.get("captcha_action", "VIDEO_GENERATION")},
        })
        return await asyncio.wait_for(fut, timeout=35)
    elif action == "get_status":
        client_id = req.get("client_id") or active_bridge._select_client()
        req_id = str(uuid.uuid4())
        fut = active_bridge._loop.create_future()
        active_bridge._pending[req_id] = fut
        await active_bridge.send_message_to(client_id, {
            "id": req_id,
            "method": "get_status",
        })
        return await asyncio.wait_for(fut, timeout=10)
    elif action == "reload_extension":
        targets = list(active_bridge._clients.keys())
        for cid in targets:
            await active_bridge.send_message_to(cid, {"method": "reload_extension"})
        return {"reloaded": targets}
    elif action == "force_refresh":
        targets = list(active_bridge._clients.keys())
        for cid in targets:
            await active_bridge._force_refresh_client(cid)
        return {"refreshed": targets}
    elif action == "send_method":
        method_name = req.get("method", "refresh_flow_tab")
        method_params = req.get("params", {})
        client_id = req.get("client_id")
        target = client_id or active_bridge._select_client()
        req_id = str(uuid.uuid4())
        fut = active_bridge._loop.create_future()
        active_bridge._pending[req_id] = fut
        msg = {"id": req_id, "method": method_name, "params": method_params}
        await active_bridge.send_message_to(target, msg)
        return await asyncio.wait_for(fut, timeout=10)
    return {"error": f"Unknown action: {action}"}



@router.post("/v1/upload", dependencies=[Depends(verify_api_key)])
async def upload_file_endpoint(
    req: UploadRequest,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Upload a file (image or video) to Google Flow and return its media ID and local URL."""
    cid = str(x_client_id).strip() if isinstance(x_client_id, str) and x_client_id.strip() else None
    target_client_id_var.set(cid)
    active_bridge = await get_active_bridge()
    project_id = req.project_id or os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

    b64_data = req.image_base64
    declared_mime = ""
    if b64_data.startswith("data:") and "," in b64_data:
        metadata, b64_data = b64_data.split(",", 1)
        declared_mime = metadata[5:].split(";", 1)[0]
    elif "," in b64_data:
        b64_data = b64_data.split(",", 1)[1]

    try:
        media_bytes = base64.b64decode("".join(b64_data.split()), validate=True)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Invalid base64 upload: {error}") from error

    mime_type = sniff_media_type(media_bytes, declared_mime=declared_mime)
    is_video_input = mime_type.startswith("video/")
    if not (is_video_input or mime_type.startswith("image/")):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported upload type {mime_type}; provide a valid image or video file.",
        )

    # 1. SHA-256 Content Fingerprint Zero-Duplicate Upload Guard
    if not req.force_upload:
        import hashlib
        content_fingerprint = hashlib.sha256(media_bytes).hexdigest()
        from flow_server.history import find_history
        cached_record = find_history(fingerprint=content_fingerprint)
        if cached_record and cached_record.get("media_id"):
            cached_media_id = str(cached_record["media_id"]).strip()
            cached_url = cached_record.get("url") or public_url(cached_record.get("filename", ""))
            log.info(
                "Upload cache HIT for sha256=%s... -> reusing media_id=%s",
                content_fingerprint[:12],
                cached_media_id,
            )
            return {
                "media_id": cached_media_id,
                "url": cached_url,
            }

    timestamp = int(time.time())
    extension = extension_for_mime(mime_type)
    temp_name = f"upload_{timestamp}_{uuid.uuid4().hex[:6]}{extension}"
    temp_path = os.path.join(OUTPUT_DIR, temp_name)

    try:
        with open(temp_path, "wb") as f:
            f.write(media_bytes)

        if is_video_input:
            from flow_engine.upload import upload_video
            upload_res = await upload_video(temp_path, project_id, active_bridge)
            media_id = upload_res.get("mediaId") or upload_res.get("name") or upload_res.get("id")
            if not media_id and isinstance(upload_res.get("media"), dict):
                media_id = upload_res["media"].get("name") or upload_res["media"].get("mediaId")
            if not media_id:
                raise HTTPException(status_code=500, detail="Failed to upload video reference to Google Flow.")
        else:
            from flow_engine.generators.i2v import upload_image
            media_id = await upload_image(active_bridge, temp_path, project_id, force_upload=bool(req.force_upload))
            if not media_id:
                raise HTTPException(status_code=500, detail="Failed to upload image reference to Google Flow.")

        # Make the file web-accessible (R2 if configured, else local /download)
        download_url, r2_key = await publish(temp_name, temp_path)
        # If it went to R2, the local copy is no longer needed for serving
        if r2_key and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

        # Add to history
        await append_to_history(
            "video" if is_video_input else "image",
            download_url,
            "Uploaded reference file",
            media_id,
            r2_key,
            local_path=temp_path if os.path.isfile(temp_path) else None,
            project_id=project_id,
            mime_type=mime_type,
        )

        return {
            "media_id": media_id,
            "url": download_url
        }
    except Exception as e:
        log.exception("Error in /v1/upload")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/v1/history")
async def get_history():
    """Get previously generated images and videos."""
    document = load_history()
    if not document.get("history"):
        # Auto-detect existing generated files to populate initial history
        history_list = []
        try:
            files = sorted(
                [f for f in os.listdir(OUTPUT_DIR) if f.startswith(("openai_img_", "flowagent_img_", "flow_vid_", "openai_chat_vid_"))],
                key=lambda f: os.path.getmtime(os.path.join(OUTPUT_DIR, f)),
                reverse=True
            )
            for filename in files[:100]:
                file_path = os.path.join(OUTPUT_DIR, filename)
                t = int(os.path.getmtime(file_path))
                is_vid = sniff_media_type(file_path).startswith("video/")
                download_url = public_url(filename)
                history_list.append({
                    "type": "video" if is_vid else "image",
                    "url": download_url,
                    "prompt": "Pre-existing generation" if not filename.startswith("openai_chat_") else "Chat video prompt",
                    "timestamp": t,
                    "media_id": None
                })
            document["history"] = history_list
            return write_history(document)
        except Exception:
            return document
    return document


@router.delete("/v1/history")
async def delete_all_history():
    """Clear all generation history and delete files."""
    try:
        registered_files = {
            derive_filename(entry)
            for entry in load_history().get("history", [])
            if derive_filename(entry)
        }
        # Remove all generated and uploaded output files
        for filename in os.listdir(OUTPUT_DIR):
            if filename in registered_files or filename.startswith(("openai_img_", "flowagent_img_", "flow_vid_", "openai_chat_vid_", "openai_chat_img_", "upload_", "input_", "i2i_upload_", "i2v_upload_")):
                file_path = os.path.join(OUTPUT_DIR, filename)
                if os.path.exists(file_path):
                    os.remove(file_path)
        clear_history()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear output folder: {str(e)}")


@router.delete("/v1/history/{filename}")
async def delete_history_item(filename: str):
    """Delete a single history item and its corresponding file."""
    safe_filename = os.path.basename(filename)
    try:
        remove_history(filename=safe_filename)
    except MediaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Delete from disk
    file_path = os.path.join(OUTPUT_DIR, safe_filename)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            log.error(f"Failed to delete file {file_path}: {e}")

    return {"status": "success", "deleted": 1}


@router.get("/download/{filename}")
async def download_file(filename: str):
    """Serve the generated assets."""
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(OUTPUT_DIR, safe_filename)
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Media not found in FLOW_OUTPUT_DIR: {safe_filename}. "
                "Generate or download it again, then retry."
            ),
        )

    media_type = sniff_media_type(file_path, filename=safe_filename)
    return FileResponse(path=file_path, media_type=media_type)
