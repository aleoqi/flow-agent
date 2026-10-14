#!/usr/bin/env python3
"""System routes for Flow Agent.

Extension transport (WebSocket / HTTP callback), model listing, credits,
token refresh, and health/liveness endpoints.
"""

import time
import uuid
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Header, WebSocket, Request, Query, Body
from fastapi.responses import JSONResponse

from flow_server import state
from flow_server.state import verify_api_key, get_active_bridge

# Setup logging (format configured centrally in flow_engine/__init__.py, imported above)
log = logging.getLogger("flow_engine.openai_api")

router = APIRouter()


# Extension WebSocket and Callback Endpoints
@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log.info("Extension connecting via FastAPI WebSocket...")
    bridge = state.get_bridge()
    if bridge:
        await bridge.handle_fastapi_ws(websocket)
    else:
        log.error("Bridge not initialized")
        await websocket.close()

@router.post("/api/ext/hello")
async def extension_hello(body: dict):
    bridge = state.get_bridge()
    if not bridge:
        return JSONResponse(status_code=503, content={"ok": False})
    session_id = body.get("session_id") or body.get("sessionId")
    if not session_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "session_id required"})
    flow_key = body.get("flowKey") or body.get("flow_key")
    result = bridge.register_http_session(str(session_id), flow_key, secret=bridge._callback_secret)
    poll_interval_ms = int(__import__("os").environ.get("EXT_POLL_INTERVAL_MS", "1000"))
    return {"ok": True, "session_id": result["session_id"], "secret": result["secret"],
            "callback_url": "/api/ext/callback", "poll_url": "/api/ext/poll",
            "poll_interval_ms": poll_interval_ms, "events_url": "/api/ext/events"}


@router.get("/api/ext/poll")
async def extension_poll(request: Request, session_id: str = Query(...)):
    bridge = state.get_bridge()
    if not bridge:
        return JSONResponse(status_code=503, content={"ok": False, "commands": []})
    authorization = request.headers.get("Authorization")
    if authorization is None:
        return JSONResponse(status_code=401, content={"ok": False, "commands": []})
    if not bridge.verify_http_session_authorization(session_id, authorization):
        return JSONResponse(status_code=403, content={"ok": False, "commands": []})
    result = bridge.poll_http_commands(session_id)
    return JSONResponse(status_code=200 if result.get("ok") else 404, content=result)


@router.post("/api/ext/callback")
async def http_callback(request: Request, body: dict):
    bridge = state.get_bridge()
    if bridge:
        authorization = request.headers.get("Authorization")
        session_id = body.get("session_id") or body.get("sessionId")
        if not bridge.verify_callback_authorization(authorization) and not (
            session_id and bridge.verify_http_session_authorization(str(session_id), authorization)
        ):
            return JSONResponse(status_code=403, content={"ok": False})
        success = bridge.handle_http_callback(body)
        return JSONResponse(status_code=200 if success else 404, content={"ok": bool(success)})
    return JSONResponse(status_code=503, content={"ok": False})


# OpenAI Endpoints

@router.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    """List available Google Flow models (OpenAI format)."""
    return {
        "object": "list",
        "data": [
            {"id": "harbor_seal", "object": "model", "created": int(time.time()), "owned_by": "google"},
            {"id": "narwhal", "object": "model", "created": int(time.time()), "owned_by": "google"},
            {"id": "gem_pix_2", "object": "model", "created": int(time.time()), "owned_by": "google"}
        ]
    }


@router.get("/v1/credits", dependencies=[Depends(verify_api_key)])
async def get_flow_credits(x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    """Credits for connected Chrome clients.

    - With an X-Client-Id header: returns just that client's credits.
    - Without it: queries every connected client and returns per-client
      breakdown plus the cumulative pool across all browsers.
    """
    active_bridge = await get_active_bridge()

    # Specific client requested → return only that one.
    if x_client_id:
        try:
            res = await active_bridge.api_request(
                "/v1/credits", body=None, captcha_action=None, method="GET",
                client_id=x_client_id,
            )
            return res
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Otherwise fan out across all connected clients.
    client_ids = list(active_bridge._clients.keys())
    if not client_ids:
        raise HTTPException(status_code=503, detail="No extensions connected")

    async def _one(cid):
        try:
            res = await active_bridge.api_request(
                "/v1/credits", body=None, captcha_action=None, method="GET",
                client_id=cid,
            )
            data = res.get("data", {}) if isinstance(res, dict) else {}
            return {
                "client_id": cid,
                "credits": int(data.get("credits", 0)),
                "tier": data.get("userPaygateTier"),
                "sku": data.get("sku"),
                "ok": res.get("status") == 200,
            }
        except Exception as e:
            return {"client_id": cid, "credits": 0, "ok": False, "error": str(e)}

    clients = await asyncio.gather(*[_one(cid) for cid in client_ids])
    total = sum(c["credits"] for c in clients if c.get("ok"))
    return {
        "total_credits": total,
        "client_count": len(clients),
        "clients": clients,
    }


@router.post("/v1/refresh-tokens", dependencies=[Depends(verify_api_key)])
async def refresh_tokens(x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    """Ask token-less clients to open/refresh their Flow tab so a token gets
    captured. With X-Client-Id only that client is nudged; without it, every
    connected client that currently has no token is nudged."""
    active_bridge = await get_active_bridge()

    if x_client_id:
        targets = [x_client_id]
    else:
        targets = [cid for cid in active_bridge._clients
                   if not active_bridge._tokens.get(cid)]

    if not targets:
        return {"nudged": 0, "clients": [], "detail": "All connected clients already have tokens"}

    async def _nudge(cid):
        try:
            await active_bridge.send_message_to(cid, {"method": "open_flow_tab"})
            await asyncio.sleep(6)
            await active_bridge.send_message_to(cid, {"method": "refresh_flow_tab"})
            return {"client_id": cid, "ok": True}
        except Exception as e:
            return {"client_id": cid, "ok": False, "error": str(e)}

    results = await asyncio.gather(*[_nudge(cid) for cid in targets])
    return {"nudged": len(results), "clients": results}


@router.post("/v1/system/reload-extension")
async def reload_extension():
    """Instruct all connected extension clients to reload, picking up new code."""
    bridge = state.get_bridge()
    if not bridge:
        raise HTTPException(status_code=503, detail="Bridge not initialized")
    clients = list(bridge._clients.keys())
    if not clients:
        return {"reloaded": 0, "clients": [], "detail": "No WebSocket clients connected"}

    async def _reload(cid):
        try:
            await bridge.send_message_to(cid, {"method": "reload_extension"})
            return {"client_id": cid, "ok": True}
        except Exception as e:
            return {"client_id": cid, "ok": False, "error": str(e)}

    results = await asyncio.gather(*[_reload(cid) for cid in clients])
    return {"reloaded": len(results), "clients": results}


@router.post("/v1/system/token")
async def set_token(body: dict = Body(...)):
    """Directly set or inject a flow key into the bridge for quick headless recovery."""
    token = body.get("token") or body.get("flow_key") or body.get("flowKey")
    if not token:
        raise HTTPException(status_code=400, detail="Token required")
    bridge = state.get_bridge()
    if not bridge:
        raise HTTPException(status_code=503, detail="Bridge not running")
    # Store token for default client or all connected clients
    for cid in bridge._clients:
        bridge._tokens[cid] = token
    bridge._tokens["__direct__"] = token
    bridge._connected.set()
    return {"ok": True, "token_length": len(token)}


@router.post("/v1/system/probe-token")
async def probe_token(body: Optional[dict] = Body(default=None)):
    """Ask connected extension clients to actively probe token from open Flow tabs."""
    bridge = state.get_bridge()
    if not bridge:
        raise HTTPException(status_code=503, detail="Bridge not running")
    clients = list(bridge._clients.keys())
    if not clients:
        return {"ok": False, "detail": "No WebSocket clients connected"}

    payload = body or {}

    async def _probe(cid):
        req_id = str(uuid.uuid4())
        fut = bridge._loop.create_future()
        bridge._pending[req_id] = fut
        try:
            msg = {"id": req_id, "method": "probe_token", **payload}
            sent = await bridge.send_message_to(cid, msg)
            if not sent:
                return {"client_id": cid, "ok": False, "error": "send failed"}
            res = await asyncio.wait_for(fut, timeout=25)
            return {"client_id": cid, "ok": True, "result": res}
        except Exception as e:
            return {"client_id": cid, "ok": False, "error": str(e)}
        finally:
            bridge._pending.pop(req_id, None)

    results = await asyncio.gather(*[_probe(cid) for cid in clients])
    return {"probed": len(results), "clients": results, "has_flow_key": bridge.has_flow_key()}


@router.post("/v1/system/reload-extension")
async def reload_extension():
    bridge = state.get_bridge()
    if not bridge:
        raise HTTPException(status_code=503, detail="Bridge not running")
    clients = list(bridge._clients.keys())
    for cid in clients:
        msg = {"id": str(uuid.uuid4()), "method": "reload_extension"}
        await bridge.send_message_to(cid, msg)
    return {"reloading": len(clients), "clients": clients}


@router.post("/v1/system/eval-tab")
async def eval_tab(body: dict = Body(...), x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    """Execute arbitrary JavaScript in an extension client's tab (MAIN world)."""
    bridge = state.get_bridge()
    if not bridge:
        raise HTTPException(status_code=503, detail="Bridge not running")
    client_id = x_client_id or body.get("client_id") or "client-jhbh11"
    req_id = str(uuid.uuid4())
    fut = bridge._loop.create_future()
    bridge._pending[req_id] = fut
    try:
        msg = {"id": req_id, "method": "eval_tab", "params": body}
        sent = await bridge.send_message_to(client_id, msg)
        if not sent:
            raise HTTPException(status_code=500, detail="Send failed")
        timeout_sec = float(body.get("timeout", 45))
        res = await asyncio.wait_for(fut, timeout=timeout_sec)
        return res
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Tab eval execution timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        bridge._pending.pop(req_id, None)


@router.post("/v1/system/reload-extension")
async def reload_extension_endpoint(x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    bridge = state.get_bridge()
    if not bridge:
        raise HTTPException(status_code=503, detail="Bridge not running")
    target_clients = [x_client_id] if x_client_id else list(bridge._clients.keys())
    results = {}
    for cid in target_clients:
        req_id = str(uuid.uuid4())
        msg = {"id": req_id, "method": "reload_extension"}
        sent = await bridge.send_message_to(cid, msg)
        results[cid] = sent
    return {"reloaded": results}


@router.post("/v1/canvas/agent", dependencies=[Depends(verify_api_key)])
async def canvas_agent_endpoint(
    body: dict = Body(...),
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Interact with Google Flow's native Canvas Agent (`button.agent-mode-chip`)."""
    active_bridge = await get_active_bridge()
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="'prompt' is required and cannot be empty")
    wait_seconds = int(body.get("wait_seconds", 45))
    auto_reset_mode = bool(body.get("auto_reset_mode", True))
    client_id = x_client_id or body.get("client_id") or None
    from flow_engine.canvas_driver import canvas_invoke_agent
    try:
        res = await canvas_invoke_agent(
            active_bridge,
            prompt=prompt,
            wait_seconds=wait_seconds,
            auto_reset_mode=auto_reset_mode,
            client_id=client_id,
        )
        return res
    except Exception as e:
        log.exception("Canvas Agent invocation failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/v1/canvas/extract-media", dependencies=[Depends(verify_api_key)])
async def canvas_extract_media_endpoint(
    body: Optional[dict] = Body(default=None),
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Extract current video and image signed URLs from the Flow Canvas DOM."""
    active_bridge = await get_active_bridge()
    payload = body or {}
    client_id = x_client_id or payload.get("client_id") or None
    from flow_engine.canvas_driver import canvas_extract_media
    try:
        res = await canvas_extract_media(active_bridge, client_id=client_id)
        return res
    except Exception as e:
        log.exception("Canvas media extraction failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/v1/system/status")
async def system_status():
    """Return detailed system and connected client status."""
    bridge = state.get_bridge()
    if not bridge:
        return {"status": "starting", "version": "2.1.0", "connected": False, "clients": []}
    clients = [
        {
            "client_id": cid,
            "state": bridge._states.get(cid, "idle"),
            "has_token": bool(bridge._tokens.get(cid)),
            "tier": bridge._tiers.get(cid),
            "credits": bridge._credits.get(cid),
        }
        for cid in bridge._clients
    ]
    return {
        "status": "healthy" if await bridge.health_check() else "unauthorized_or_disconnected",
        "version": "2.1.0",
        "extension_connected": bridge.is_extension_connected(),
        "has_flow_key": bridge.has_flow_key(),
        "transport": bridge.active_transport(),
        "client_count": len(clients),
        "clients": clients,
    }


@router.get("/")
async def root():
    return {"status": "running", "service": "Flow Agent API", "version": "2.1.0"}


# Health Check
@router.get("/health")
async def health():
    bridge = state.get_bridge()
    if not bridge:
        return {"status": "starting", "connected": False, "transport": "none", "version": "2.1.0"}
    return {
        "status": "healthy" if await bridge.health_check() else "unauthorized_or_disconnected",
        "version": "2.1.0",
        "extension_connected": bridge.is_extension_connected(),
        "has_flow_key": bridge.has_flow_key(),
        "transport": bridge.active_transport(),
    }
