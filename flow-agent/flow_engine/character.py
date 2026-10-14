"""Flow Engine — Character management module.

Absorbs Google Flow's native CHARACTER entity architecture.
Supports project-scoped character creation, listing, querying, and deletion
for character face & body reference consistency.
"""

import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import ENDPOINTS

log = logging.getLogger("flow_engine.character")


@dataclass
class FlowCharacter:
    entity_id: str
    display_name: str
    thumbnail_media_id: Optional[str] = None
    workflow_ids: List[str] = None
    voice: Optional[str] = None
    personality: Optional[str] = None
    raw: Dict[str, Any] = None


async def create_character(
    bridge,
    project_id: str,
    name: str,
    face_workflow_or_media_id: Optional[str] = None,
    body_workflow_or_media_id: Optional[str] = None,
    voice: Optional[str] = None,
    personality: Optional[str] = None,
    client_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new Flow Character entity in a project.

    1. Mint entity via tRPC flow.createEntity (free, no credits).
    2. Patch display_name, image references, voice, personality via REST.
    """
    create_url = ENDPOINTS.get("trpc_create_entity", "https://labs.google/fx/api/trpc/flow.createEntity")
    create_res = await bridge.trpc_request(create_url, {"json": {"projectId": project_id}}, method="POST", client_id=client_id)
    
    if not isinstance(create_res, dict) or create_res.get("error"):
        err = create_res.get("error") if isinstance(create_res, dict) else "Unknown error"
        log.error("Failed to mint Character entity: %s", err)
        raise ValueError(f"Failed to mint Character entity: {err}")

    data = create_res.get("data", create_res)
    payload = data.get("result", {}).get("data", {}).get("json", {})
    entity_id = payload.get("entityId")
    if not entity_id:
        # Check direct shape
        entity_id = payload.get("entity_id") or data.get("entityId")
    if not entity_id:
        log.error("createEntity returned no entityId: %s", data)
        raise ValueError(f"createEntity returned no entityId: {data}")

    log.info("Character entity minted: %s in project %s", entity_id, project_id)

    # Step 2: Patch entity with metadata and image references
    workflow_ids = []
    if face_workflow_or_media_id:
        workflow_ids.append(face_workflow_or_media_id)
    if body_workflow_or_media_id:
        workflow_ids.append(body_workflow_or_media_id)

    character_info = {}
    if workflow_ids:
        character_info["imageReferences"] = [{"workflowId": w} for w in workflow_ids]
    if voice:
        character_info["voice"] = voice
    if personality:
        character_info["personality"] = personality

    patch_body = {
        "entity": {
            "name": f"projects/{project_id}/entities/{entity_id}",
            "entityType": "CHARACTER",
            "displayName": name,
        }
    }
    if character_info:
        patch_body["entity"]["characterInfo"] = character_info

    patch_url = ENDPOINTS.get("patch_entities", "/v1/projects/{project_id}/flow/entities").format(project_id=project_id)
    patch_res = await bridge.api_request(
        patch_url,
        patch_body,
        method="PATCH",
        client_id=client_id,
    )
    log.info("Character entity patched: %s -> %s", entity_id, patch_res.get("status") if isinstance(patch_res, dict) else patch_res)

    return {
        "entity_id": entity_id,
        "display_name": name,
        "project_id": project_id,
        "workflow_ids": workflow_ids,
        "voice": voice,
        "personality": personality,
    }


async def list_characters(
    bridge,
    project_id: str,
    client_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all CHARACTER entities in a Flow project."""
    input_param = urllib.parse.quote(json.dumps({"json": {"projectId": project_id}}))
    url = f"{ENDPOINTS.get('trpc_initial_data', 'https://labs.google/fx/api/trpc/flow.projectInitialData')}?input={input_param}"

    res = await bridge.trpc_request(url, method="GET", client_id=client_id)
    if not isinstance(res, dict) or res.get("error"):
        log.warning("list_characters tRPC error: %s", res)
        return []

    data = res.get("data", res)
    project_contents = data.get("result", {}).get("data", {}).get("json", {}).get("projectContents", {})
    entities = project_contents.get("entities", [])

    characters = []
    for ent in entities:
        if ent.get("entityType") == "CHARACTER":
            char_info = ent.get("characterInfo", {})
            img_refs = char_info.get("imageReferences", [])
            wf_ids = [ref.get("workflowId") for ref in img_refs if isinstance(ref, dict) and ref.get("workflowId")]
            characters.append({
                "entity_id": ent.get("entityId"),
                "display_name": ent.get("displayName", "Untitled Character"),
                "thumbnail_media_id": ent.get("thumbnailMediaId"),
                "workflow_ids": wf_ids,
                "voice": char_info.get("voice"),
                "personality": char_info.get("personality"),
            })

    log.info("Retrieved %d character(s) for project %s", len(characters), project_id)
    return characters


async def get_character(
    bridge,
    project_id: str,
    name_or_id: str,
    client_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find a character by entity_id or exact display_name."""
    chars = await list_characters(bridge, project_id, client_id=client_id)
    for c in chars:
        if c.get("entity_id") == name_or_id or c.get("display_name") == name_or_id:
            return c
    return None


async def delete_character(
    bridge,
    project_id: str,
    entity_id: str,
    client_id: Optional[str] = None,
) -> bool:
    """Delete a CHARACTER entity by entityId."""
    delete_url = ENDPOINTS.get("trpc_delete_entity", "https://labs.google/fx/api/trpc/flow.deleteEntities")
    body = {"json": {"projectId": project_id, "entityIds": [entity_id]}}
    res = await bridge.trpc_request(delete_url, body, method="POST", client_id=client_id)
    if isinstance(res, dict) and not res.get("error"):
        log.info("Character %s deleted in project %s", entity_id, project_id)
        return True
    log.error("Failed to delete character %s: %s", entity_id, res)
    return False
