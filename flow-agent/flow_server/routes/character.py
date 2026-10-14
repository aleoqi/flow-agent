#!/usr/bin/env python3
"""Character entity management endpoints for Flow Agent."""

import os
import logging
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

from flow_server.models import CharacterCreateRequest
from flow_server.state import verify_api_key, get_active_bridge
from flow_engine import DEFAULT_PROJECT
from flow_engine.character import (
    create_character,
    list_characters,
    get_character,
    delete_character,
)

log = logging.getLogger("flow_engine.openai_api")

router = APIRouter()


def _header_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


@router.get("/v1/characters", dependencies=[Depends(verify_api_key)])
@router.get("/api/v1/characters", dependencies=[Depends(verify_api_key)])
async def api_list_characters(
    project_id: Optional[str] = None,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """List all Flow CHARACTER entities for a project."""
    bridge = await get_active_bridge()
    proj = project_id or os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)
    cid = _header_string(x_client_id)
    try:
        characters = await list_characters(bridge, proj, client_id=cid)
        return {"status": "ok", "project_id": proj, "count": len(characters), "characters": characters}
    except Exception as exc:
        log.exception("Error listing characters")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/v1/characters", dependencies=[Depends(verify_api_key)])
@router.post("/api/v1/characters", dependencies=[Depends(verify_api_key)])
async def api_create_character(
    req: CharacterCreateRequest,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Create a new Flow CHARACTER entity in a project."""
    bridge = await get_active_bridge()
    proj = req.project_id or os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)
    cid = _header_string(x_client_id)
    try:
        result = await create_character(
            bridge,
            project_id=proj,
            name=req.name,
            face_workflow_or_media_id=req.face_media_id,
            body_workflow_or_media_id=req.body_media_id,
            voice=req.voice,
            personality=req.personality,
            client_id=cid,
        )
        return {"status": "ok", "character": result}
    except Exception as exc:
        log.exception("Error creating character")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/v1/characters/{entity_id}", dependencies=[Depends(verify_api_key)])
@router.get("/api/v1/characters/{entity_id}", dependencies=[Depends(verify_api_key)])
async def api_get_character(
    entity_id: str,
    project_id: Optional[str] = None,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Retrieve details of a single Character entity."""
    bridge = await get_active_bridge()
    proj = project_id or os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)
    cid = _header_string(x_client_id)
    char = await get_character(bridge, proj, entity_id, client_id=cid)
    if not char:
        raise HTTPException(status_code=404, detail=f"Character '{entity_id}' not found in project {proj}")
    return {"status": "ok", "character": char}


@router.delete("/v1/characters/{entity_id}", dependencies=[Depends(verify_api_key)])
@router.delete("/api/v1/characters/{entity_id}", dependencies=[Depends(verify_api_key)])
async def api_delete_character(
    entity_id: str,
    project_id: Optional[str] = None,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Delete a Flow CHARACTER entity."""
    bridge = await get_active_bridge()
    proj = project_id or os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)
    cid = _header_string(x_client_id)
    success = await delete_character(bridge, proj, entity_id, client_id=cid)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to delete character {entity_id}")
    return {"status": "ok", "deleted": entity_id}
