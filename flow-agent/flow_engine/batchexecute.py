"""Google Flow Batchexecute Wire Protocol Module.

Ported from crisng95/flowkit (commit 19cb87d6840759f0c0be1739f84ffd6ce59f985a).
Provides pure encoding and decoding for Flow's internal batchexecute RPCs.
Does not perform any network operations directly.
"""
from __future__ import annotations

import json
import random
import re
import uuid
from dataclasses import dataclass
from typing import Any, Optional

BATCH_PATH = "/_/AiSandboxAngularFrontend/data/batchexecute"
MEDIA_HOST = "flow-content.google"

RPC_GEN_IMAGE = "ogiZ0b"
RPC_GEN_VIDEO = "eb1hJf"
RPC_GEN_VIDEO_TEXT = "YhhmEf"
RPC_GEN_VIDEO_FIRST_LAST = "nprQif"
RPC_GEN_VIDEO_REFERENCES = "MZZa6b"
RPC_OPERATION = "jwpduf"
RPC_CREATE_PROJECT = "jHPbke"
RPC_PROJECT_MEDIA = "Zzl0ze"
RPC_MEDIA = "as29s"
RPC_UPLOAD_IMAGE = "maseQ"
RPC_UPSCALE_IMAGE = "SPrCad"

CAPTCHA_IMAGE = "IMAGE_GENERATION"
CAPTCHA_VIDEO = "VIDEO_GENERATION"
CAPTCHA_CHAT = "CHAT_GENERATION"

CAPTCHA_SLOT = "__CAPTCHA__"

IMAGE_MODELS = {
    "GEM_PIX_2", "NARWHAL", "HARBOR_SEAL",
    "BELUGA", "BELUGA_THINKING_LOW", "BELUGA_THINKING_MED", "BELUGA_THINKING_HIGH",
}
IMAGE_MODEL = "GEM_PIX_2"

IMAGE_MODEL_BY_NICKNAME = {
    "NANO_BANANA_PRO": "GEM_PIX_2",
    "NANO_BANANA_2_1": "BELUGA",
    "NANO_BANANA_2": "BELUGA",
    "NANO_BANANA_2_LITE": "HARBOR_SEAL",
    "NANO_BANANA_LITE": "HARBOR_SEAL",
}

ASPECT_SQUARE = 1           # 1:1
ASPECT_PORTRAIT = 2         # 9:16
ASPECT_LANDSCAPE = 3        # 16:9
ASPECT_PORTRAIT_4_3 = 4     # 3:4
ASPECT_LANDSCAPE_4_3 = 5    # 4:3

ASPECT_BY_NAME = {
    "IMAGE_ASPECT_RATIO_SQUARE": ASPECT_SQUARE,
    "IMAGE_ASPECT_RATIO_PORTRAIT": ASPECT_PORTRAIT,
    "IMAGE_ASPECT_RATIO_LANDSCAPE": ASPECT_LANDSCAPE,
    "IMAGE_ASPECT_RATIO_PORTRAIT_FOUR_THREE": ASPECT_PORTRAIT_4_3,
    "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE": ASPECT_LANDSCAPE_4_3,
    "1:1": ASPECT_SQUARE,
    "9:16": ASPECT_PORTRAIT,
    "16:9": ASPECT_LANDSCAPE,
    "3:4": ASPECT_PORTRAIT_4_3,
    "4:3": ASPECT_LANDSCAPE_4_3,
    "square": ASPECT_SQUARE,
    "portrait": ASPECT_PORTRAIT,
    "landscape": ASPECT_LANDSCAPE,
}

SURFACE_ID = 22
REF_TYPE_IMAGE = 1
BASE_TYPE_IMAGE = 2
IMAGE_UPSCALE_RESOLUTIONS = {"2K": 1, "4K": 2}


class RpcError(RuntimeError):
    def __init__(self, rpcid: str, detail: Any):
        super().__init__(f"{rpcid} failed: {detail!r}")
        self.rpcid = rpcid
        self.detail = detail


class FlowBatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class RpcResult:
    rpcid: str
    data: Any
    error: Any = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class GeneratedImage:
    media_id: str
    url: str


def resolve_image_model(key: Optional[str]) -> str:
    if isinstance(key, str):
        normalized = key.strip().upper().replace("-", "_")
        if normalized in IMAGE_MODEL_BY_NICKNAME:
            return IMAGE_MODEL_BY_NICKNAME[normalized]
        if normalized in IMAGE_MODELS:
            return normalized
    return IMAGE_MODEL


def resolve_aspect(aspect: Any) -> int:
    if isinstance(aspect, int):
        if aspect in (1, 2, 3, 4, 5):
            return aspect
        raise ValueError(f"image aspect must be 1-5, got {aspect}")
    try:
        return ASPECT_BY_NAME[str(aspect).strip()]
    except KeyError:
        return ASPECT_LANDSCAPE


def build_envelope(rpcid: str, inner: Any) -> str:
    return json.dumps(
        [[[rpcid, json.dumps(inner, separators=(",", ":"), ensure_ascii=False), None, "generic"]]],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def parse_envelope(text: str) -> list[RpcResult]:
    if not text:
        return []
    body = text.split("\n", 1)[1] if text.startswith(")]}'") else text
    decoder = json.JSONDecoder()
    results: list[RpcResult] = []
    index = 0
    while index < len(body):
        start = body.find("[", index)
        if start == -1:
            break
        try:
            chunk, consumed = decoder.raw_decode(body[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        index = start + consumed
        for entry in chunk if isinstance(chunk, list) else []:
            if not isinstance(entry, list) or not entry or entry[0] != "wrb.fr":
                continue
            rpcid = entry[1] if len(entry) > 1 else "?"
            payload = entry[2] if len(entry) > 2 else None
            if payload is None:
                results.append(RpcResult(rpcid, None, entry[5] if len(entry) > 5 else True))
                continue
            results.append(
                RpcResult(rpcid, json.loads(payload) if isinstance(payload, str) else payload)
            )
    return results


def first_payload(text: str, rpcid: str) -> Any:
    results = parse_envelope(text)
    for result in results:
        if result.rpcid != rpcid:
            continue
        if not result.ok:
            raise RpcError(rpcid, result.error)
        return result.data
    raise FlowBatchError(f"no {rpcid} envelope in response ({len(results)} others)")


def _client_uuid() -> str:
    return str(uuid.uuid4()).upper()


def _context(project_id: Optional[str], asset_id: Optional[str] = None) -> list:
    return [None, SURFACE_ID, None, None, asset_id, project_id, None, None, None, None,
            [CAPTCHA_SLOT, 1]]


def _image_input(media_id: str, input_type: int) -> list:
    return [media_id, None, None, None, input_type]


def _reference(media_id: str) -> list:
    return _image_input(media_id, REF_TYPE_IMAGE)


def _base_image(media_id: str) -> list:
    return _image_input(media_id, BASE_TYPE_IMAGE)


def image_request(prompt: str, project_id: str, count: int = 1,
                  aspect: Any = ASPECT_LANDSCAPE, seed: Optional[int] = None,
                  model: str = IMAGE_MODEL,
                  ref_media_ids: Optional[list[str]] = None,
                  base_media_id: Optional[str] = None,
                  asset_id: Optional[str] = None) -> str:
    count = max(1, min(4, int(count)))
    ratio = resolve_aspect(aspect)
    resolved_model = resolve_image_model(model)
    base = seed if seed is not None else random.randint(1, 10**9)
    items = []
    target_asset = asset_id or base_media_id
    for index in range(count):
        image_inputs = []
        if base_media_id:
            image_inputs.append(_base_image(base_media_id))
        image_inputs.extend(
            _reference(mid) for mid in (ref_media_ids or []) if mid != base_media_id
        )
        items.append([None, None, image_inputs or None, base + index * 9973, ratio,
                      resolved_model, None, _context(project_id, target_asset), [[[prompt]]],
                      None, None, None, None, _client_uuid()])
    return build_envelope(RPC_GEN_IMAGE, [None, items, 1, _context(project_id, target_asset),
                                          [_client_uuid()]])


def upload_request(image_b64: str, project_id: str, mime_type: str = "image/jpeg",
                   file_name: str = "upload.jpg") -> str:
    return build_envelope(RPC_UPLOAD_IMAGE, [
        _context(project_id), image_b64, mime_type, 1, None, None, None, None,
        file_name, None, _client_uuid(), _client_uuid(),
    ])


def image_upscale_request(media_id: str, resolution: str = "2K") -> str:
    key = str(resolution).strip().upper()
    if key.startswith("UPSAMPLE_IMAGE_RESOLUTION_"):
        key = key.removeprefix("UPSAMPLE_IMAGE_RESOLUTION_")
    try:
        code = IMAGE_UPSCALE_RESOLUTIONS[key]
    except KeyError:
        raise ValueError("image upscale resolution must be 2K or 4K") from None
    return build_envelope(RPC_UPSCALE_IMAGE, [media_id, code, _context(None)])


def _walk_strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)


def read_images(payload: Any) -> list[GeneratedImage]:
    images: list[GeneratedImage] = []
    seen: set[str] = set()
    for text in _walk_strings(payload):
        if MEDIA_HOST + "/image/" not in text:
            continue
        media_id = text.split("/image/", 1)[1].split("?", 1)[0]
        if media_id in seen:
            continue
        seen.add(media_id)
        images.append(GeneratedImage(media_id=media_id, url=text))
    return images


def read_uploaded_media_id(payload: Any) -> str:
    record = payload[0] if isinstance(payload, list) and payload else None
    media_id = record[0] if isinstance(record, list) and record else None
    if not isinstance(media_id, str) or not media_id:
        raise FlowBatchError(f"upload response carried no media id: {payload!r}")
    return media_id


def read_upscaled_image(payload: Any) -> str:
    encoded = payload[1] if isinstance(payload, list) and len(payload) > 1 else None
    if not isinstance(encoded, str) or len(encoded) < 100:
        raise FlowBatchError("image upscale response carried no encoded image")
    return encoded


# ── Video Constants and Requests ─────────────────────────────────────────────

VIDEO_ASPECT_PORTRAIT = 1
VIDEO_ASPECT_LANDSCAPE = 2
FULL_FRAME_CROP = [[0, 0], [1, 1]]


@dataclass(frozen=True)
class Operation:
    operation_id: str
    project_id: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class MediaUrls:
    media_id: str
    video: Optional[str] = None
    image: Optional[str] = None


def resolve_video_aspect(aspect: Any) -> int:
    if isinstance(aspect, int) and aspect in (VIDEO_ASPECT_PORTRAIT, VIDEO_ASPECT_LANDSCAPE):
        return aspect
    s = str(aspect).strip().lower()
    if s in ("portrait", "9:16", "2"):
        return VIDEO_ASPECT_PORTRAIT
    return VIDEO_ASPECT_LANDSCAPE


def omni_reference_video_request(prompt: str, project_id: str,
                                 reference_media_ids: list[str],
                                 *, duration_s: int = 8, resolution: str = "720p",
                                 aspect: Any = VIDEO_ASPECT_LANDSCAPE) -> str:
    refs = [str(mid) for mid in reference_media_ids if str(mid)]
    if not refs:
        raise ValueError("Omni reference-to-video requires at least one reference image")
    model = f"abra_r2v_{duration_s}s"
    request = [
        [None, None, [[[prompt]]]],
        [[None, mid] for mid in refs],
        model,
        resolve_video_aspect(aspect),
        None,
        [None, None, None, None, _client_uuid(), _client_uuid()],
    ]
    return build_envelope(RPC_GEN_VIDEO_REFERENCES, [
        [request],
        _context(project_id),
        [_client_uuid(), 2],
    ])


def omni_first_frame_request(prompt: str, project_id: str, source_media_id: str,
                             *, duration_s: int = 8, resolution: str = "720p",
                             aspect: Any = VIDEO_ASPECT_LANDSCAPE,
                             crop: Optional[list] = None) -> str:
    model = f"abra_i2v_{duration_s}s"
    request = [
        [None, None, [[[prompt]]]],
        model,
        resolve_video_aspect(aspect),
        None,
        [None, source_media_id, None, None, None,
         FULL_FRAME_CROP if crop is None else crop],
        [None, None, None, None, _client_uuid(), _client_uuid()],
    ]
    return build_envelope(RPC_GEN_VIDEO, [
        [request],
        _context(project_id),
        [_client_uuid(), 2],
    ])


def omni_first_last_request(prompt: str, project_id: str,
                            start_media_id: str, end_media_id: str,
                            *, duration_s: int = 8, resolution: str = "720p",
                            aspect: Any = VIDEO_ASPECT_LANDSCAPE,
                            start_crop: Optional[list] = None,
                            end_crop: Optional[list] = None) -> str:
    model = f"omni_flash_i2v_{duration_s}s_first_last"
    request = [
        [None, None, [[[prompt]]]],
        model,
        resolve_video_aspect(aspect),
        None,
        [None, start_media_id, None, None, None,
         FULL_FRAME_CROP if start_crop is None else start_crop],
        [None, end_media_id, None, None, None,
         FULL_FRAME_CROP if end_crop is None else end_crop],
        [None, None, None, None, _client_uuid(), _client_uuid()],
    ]
    return build_envelope(RPC_GEN_VIDEO_FIRST_LAST, [
        [request],
        _context(project_id),
        [_client_uuid(), 2],
    ])


def operation_request(operation_id: str) -> str:
    return build_envelope(RPC_OPERATION, [None, None, [[operation_id]]])


def project_media_request(project_id: str) -> str:
    return build_envelope(RPC_PROJECT_MEDIA, [f"projects/{project_id}", None, None, None, [1]])


def media_request(media_id: str) -> str:
    return build_envelope(RPC_MEDIA, [media_id])


def read_operation(payload: Any) -> Operation:
    records = payload[2] if isinstance(payload, list) and len(payload) > 2 else None
    record = records[0] if isinstance(records, list) and records else None
    if not isinstance(record, list) or not record:
        raise FlowBatchError("operation payload carried no record")
    return Operation(
        operation_id=record[0],
        project_id=record[1] if len(record) > 1 else None,
        status=record[3] if len(record) > 3 else None,
        error=None,
    )


_MEDIA_SLOT = re.compile(r'null,null,\?"([0-9a-fA-F-]{36})\?"')


def find_media_id_in_text(text: str, operation_id: str) -> Optional[str]:
    lower_text = text.lower()
    lower_op = operation_id.lower()
    start = lower_text.find(lower_op)
    if start == -1:
        return None
    window_start = start
    window_end = min(len(text), start + 800)
    match = _MEDIA_SLOT.search(text, window_start, window_end)
    return match.group(1) if match else None


def read_media_urls(payload: Any, media_id: str) -> MediaUrls:
    video = image = None
    for text in _walk_strings(payload):
        if not text.startswith("https://"):
            continue
        if MEDIA_HOST + "/video/" in text and video is None:
            video = text
        elif MEDIA_HOST + "/image/" in text and image is None:
            image = text
    return MediaUrls(media_id=media_id, video=video, image=image)
