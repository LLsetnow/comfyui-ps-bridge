"""Photoshop snapshots and completed-workflow delivery on ComfyUI's HTTP server."""

import asyncio
import base64
import binascii
import io
import ipaddress
import json
import logging
import struct
import time
import zlib
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4

import numpy as np
from aiohttp import web
from comfy_execution.utils import get_executing_context
from PIL import Image

from .previews import SUBFOLDER as PREVIEW_SUBFOLDER, get_preview

MAX_PIXELS = 32 * 1024 * 1024
MAX_UPLOAD = 160 * 1024 * 1024
MAX_LEGACY_JSON_UPLOAD = 96 * 1024 * 1024
MAX_CAPTURE_BYTES = 384 * 1024 * 1024
MAX_RESULT_BYTES = 384 * 1024 * 1024
CAPTURE_TTL = 24 * 60 * 60
RESULT_TTL = 2 * 60 * 60
PANEL_HEARTBEAT_TTL = 25
CAPTURE_REQUEST_TTL = 135
CAPTURE_REQUEST_HISTORY_TTL = 300
MAX_LONG_POLL_SECONDS = 15.0
MAX_CAPTURE_WAIT_SECONDS = 25.0
MAX_BINARY_META_BYTES = 1024 * 1024
PSBR_HEADER = struct.Struct("<4sII")
PSBD_HEADER = struct.Struct("<4sIIIIII")
API = "/ps-bridge/v1"
BINDING_KEY = "_comfyui_ps_bridge_binding"
LOCAL_PROMPT = ContextVar("photoshop_bridge_local_prompt", default=False)
RECT_KEYS = ("left", "top", "right", "bottom")
BINARY_CODECS = frozenset({"raw", "rle", "sub+rle", "deflate", "sub+deflate", "png"})
LOGGER = logging.getLogger("comfyui_ps_bridge")


class BridgeError(ValueError):
    pass


def _integer(value, label, minimum=1, maximum=1_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise BridgeError(f"Invalid {label}.")
    return value


def _string(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BridgeError(f"Invalid {label}.")
    return value


def _photoshop_id(value, label):
    if isinstance(value, str) and len(value) <= 16 and value.isascii() and value.isdecimal():
        value = int(value)
    return str(_integer(value, label, maximum=2**53 - 1))


def _rect(value):
    if not isinstance(value, dict):
        raise BridgeError("Missing capture rectangle.")
    rect = {key: _integer(value.get(key), key, -1_000_000) for key in RECT_KEYS}
    width = rect["right"] - rect["left"]
    height = rect["bottom"] - rect["top"]
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise BridgeError(f"Capture and result rectangles must contain 1 to {MAX_PIXELS:,} pixels.")
    return rect


def _rle_encode_mask(data: bytes) -> bytes | None:
    n = len(data)
    if n < 64:
        return None
    arr = np.frombuffer(data, dtype=np.uint8)
    change_idx = np.flatnonzero(arr[1:] != arr[:-1]) + 1
    starts = np.concatenate(([0], change_idx))
    if len(starts) * 2.5 >= n:
        return None
    ends = np.concatenate((change_idx, [n]))
    lengths = ends - starts
    out = bytearray()
    lit = bytearray()

    def flush_lit():
        pos = 0
        total = len(lit)
        while pos < total:
            take = min(128, total - pos)
            out.append(take - 1)
            out.extend(lit[pos:pos + take])
            pos += take
        lit.clear()

    for run_len, val in zip(lengths.tolist(), arr[starts].tolist()):
        if run_len >= 3:
            flush_lit()
            rem = run_len
            while rem >= 3:
                take = 130 if rem > 130 else rem
                out.append(0x80 | (take - 3))
                out.append(val)
                rem -= take
            if rem > 0:
                lit.extend(bytes((val,)) * rem)
        else:
            lit.extend(bytes((val,)) * run_len)
    flush_lit()
    return bytes(out) if len(out) < int(n * 0.9) else None


def _rle_decode(src: bytes | memoryview, expected_len: int) -> bytes:
    mv = memoryview(src)
    n = len(mv)
    out = bytearray()
    extend = out.extend
    i = 0
    while i < n:
        control = mv[i]
        i += 1
        if control & 0x80:
            if i >= n:
                raise BridgeError("Truncated RLE asset payload.")
            count = (control & 0x7F) + 3
            if len(out) + count > expected_len:
                raise BridgeError("RLE asset payload exceeds expected dimensions.")
            extend(bytes((mv[i],)) * count)
            i += 1
        else:
            count = control + 1
            if i + count > n or len(out) + count > expected_len:
                raise BridgeError("Invalid RLE asset payload.")
            extend(mv[i:i + count])
            i += count
    if len(out) != expected_len:
        raise BridgeError("RLE asset length does not match dimensions.")
    return bytes(out)


def _inflate_bounded(src: bytes | memoryview, expected_len: int) -> bytes:
    try:
        decomp = zlib.decompressobj(wbits=47)
        raw = decomp.decompress(src, expected_len + 1)
        if not decomp.eof or decomp.unconsumed_tail or len(raw) != expected_len:
            raise BridgeError("Compressed asset length does not match dimensions.")
        return raw
    except zlib.error as exc:
        raise BridgeError("Invalid compressed asset payload.") from exc


def _unsub_pixels(raw: bytes, width: int, height: int, channels: int) -> bytes:
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, channels)
    return np.cumsum(arr, axis=1, dtype=np.uint8).tobytes()


def _decode_png_bytes(raw_png: bytes, size: tuple[int, int], mask: bool) -> tuple[str, bytes]:
    try:
        with Image.open(io.BytesIO(raw_png), formats=("PNG",)) as image:
            allowed = ("L",) if mask else ("RGB", "RGBA")
            if image.size != size or image.mode not in allowed:
                raise BridgeError("PNG dimensions or channels do not match the capture.")
            image.load()
            return image.mode, image.tobytes()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise BridgeError("Invalid PNG data.") from exc


def _asset_json(value, rect, mask=False):
    if not isinstance(value, dict) or _rect(value.get("rect")) != rect:
        raise BridgeError("An asset's rectangle does not match its source frame.")
    encoded = value.get("png")
    if not isinstance(encoded, str) or not encoded:
        raise BridgeError("Missing PNG data.")
    size = (rect["right"] - rect["left"], rect["bottom"] - rect["top"])
    try:
        raw_png = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise BridgeError("Invalid PNG data.") from exc
    mode, raw_pixels = _decode_png_bytes(raw_png, size, mask=mask)
    return {"rect": rect, "mode": mode, "raw": raw_pixels}


def _asset_binary(value, rect, payload_view: memoryview, mask=False):
    if not isinstance(value, dict) or _rect(value.get("rect")) != rect:
        raise BridgeError("An asset's rectangle does not match its source frame.")
    channels = _integer(value.get("channels"), "asset channels", minimum=1, maximum=4)
    if mask and channels != 1:
        raise BridgeError("Mask asset must have 1 channel.")
    if not mask and channels not in (3, 4):
        raise BridgeError("Image asset must have 3 or 4 channels.")
    codec = value.get("codec")
    if not isinstance(codec, str) or codec not in BINARY_CODECS:
        raise BridgeError("Unsupported asset codec.")
    offset = _integer(value.get("offset"), "asset offset", minimum=0, maximum=len(payload_view))
    byte_length = _integer(value.get("byte_length"), "asset byte length", minimum=1, maximum=len(payload_view))
    if offset + byte_length > len(payload_view):
        raise BridgeError("Asset payload bounds exceed capture frame.")
    chunk = payload_view[offset:offset + byte_length]
    width = rect["right"] - rect["left"]
    height = rect["bottom"] - rect["top"]
    expected_len = width * height * channels

    if codec == "png":
        mode, raw_pixels = _decode_png_bytes(bytes(chunk), (width, height), mask=mask)
        return {"rect": rect, "mode": mode, "raw": raw_pixels}

    if codec == "raw":
        if len(chunk) != expected_len:
            raise BridgeError("Raw asset length does not match dimensions.")
        raw_pixels = bytes(chunk)
    elif codec == "rle":
        raw_pixels = _rle_decode(chunk, expected_len)
    elif codec == "sub+rle":
        raw_pixels = _unsub_pixels(_rle_decode(chunk, expected_len), width, height, channels)
    elif codec == "deflate":
        raw_pixels = _inflate_bounded(chunk, expected_len)
    else:  # sub+deflate
        raw_pixels = _unsub_pixels(_inflate_bounded(chunk, expected_len), width, height, channels)

    mode = "L" if mask else ("RGB" if channels == 3 else "RGBA")
    return {"rect": rect, "mode": mode, "raw": raw_pixels}


def _validate_capture_envelope(payload, sources, decode_asset, require_layer_without_selection=True):
    document_width = _integer(payload.get("document_width"), "document width")
    document_height = _integer(payload.get("document_height"), "document height")
    layer_rect = _rect(payload.get("layer_rect"))
    selection_rect = payload.get("selection_rect")
    if not isinstance(sources, dict):
        raise BridgeError("Missing capture assets.")

    assets = {
        "layer_image": None,
        "layer_mask": None,
        "selection_image": None,
        "selection_mask": None,
    }
    if selection_rect is not None:
        selection_rect = _rect(selection_rect)
        if not (
            0 <= selection_rect["left"] < selection_rect["right"] <= document_width
            and 0 <= selection_rect["top"] < selection_rect["bottom"] <= document_height
        ):
            raise BridgeError("Selection rectangle must be inside the captured canvas.")
        assets["selection_image"] = decode_asset(sources.get("selection_image"), selection_rect, mask=False)
        assets["selection_mask"] = decode_asset(sources.get("selection_mask"), selection_rect, mask=True)
        if sources.get("layer_image") is not None:
            assets["layer_image"] = decode_asset(sources["layer_image"], layer_rect, mask=False)
        if sources.get("layer_mask") is not None:
            assets["layer_mask"] = decode_asset(sources["layer_mask"], layer_rect, mask=True)
    else:
        if sources.get("selection_image") is not None or sources.get("selection_mask") is not None:
            raise BridgeError("Selection assets require a selection rectangle.")
        if require_layer_without_selection or sources.get("layer_image") is not None:
            assets["layer_image"] = decode_asset(sources.get("layer_image"), layer_rect, mask=False)
        if sources.get("layer_mask") is not None:
            assets["layer_mask"] = decode_asset(sources["layer_mask"], layer_rect, mask=True)

    return {
        "capture_id": str(uuid4()),
        "session_id": _string(payload.get("session_id"), "Photoshop session ID"),
        "document_id": _photoshop_id(payload.get("document_id"), "document ID"),
        "document_width": document_width,
        "document_height": document_height,
        "document_name": _string(payload.get("document_name"), "document name", 1024),
        "layer_id": _photoshop_id(payload.get("layer_id"), "layer ID"),
        "layer_name": _string(payload.get("layer_name"), "layer name", 1024),
        "layer_rect": layer_rect,
        "selection_rect": selection_rect,
        "assets": assets,
    }


def _capture(payload):
    return _validate_capture_envelope(
        payload,
        payload.get("assets"),
        _asset_json,
        require_layer_without_selection=True,
    )


def _capture_binary(raw_packet: bytes) -> tuple[dict, str | None]:
    if len(raw_packet) < PSBR_HEADER.size:
        raise BridgeError("Capture packet is truncated.")
    magic, version, meta_len = PSBR_HEADER.unpack_from(raw_packet, 0)
    if magic != b"PSBR" or version != 1:
        raise BridgeError("Unsupported capture packet header.")
    if meta_len == 0 or meta_len > MAX_BINARY_META_BYTES or PSBR_HEADER.size + meta_len > len(raw_packet):
        raise BridgeError("Invalid capture packet metadata length.")
    meta_end = PSBR_HEADER.size + meta_len
    try:
        payload = json.loads(memoryview(raw_packet)[PSBR_HEADER.size:meta_end].tobytes())
    except (ValueError, RecursionError) as exc:
        raise BridgeError("Invalid capture packet metadata.") from exc
    if not isinstance(payload, dict):
        raise BridgeError("Capture packet metadata must be a JSON object.")
    request_id = payload.get("request_id")
    if request_id is not None:
        request_id = _string(request_id, "capture request ID")
    payload_view = memoryview(raw_packet)[meta_end:]
    validated = _validate_capture_envelope(
        payload,
        payload.get("assets"),
        lambda value, rect, mask=False: _asset_binary(value, rect, payload_view, mask=mask),
        require_layer_without_selection=True,
    )
    return validated, request_id


@dataclass
class Capture:
    payload: dict
    size: int
    expires: float
    retain_until: float = 0


@dataclass
class CaptureRequest:
    id: str
    session_id: str
    expires: float
    capture_id: str | None = None
    error: str | None = None

    @property
    def is_pending(self):
        return self.capture_id is None and self.error is None


@dataclass
class Delivery:
    metadata: dict
    rgba: bytes
    mask: bytes
    expires: float
    channels: int = 4
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _bundle_raw: bytes | None = field(default=None, init=False, repr=False)
    _bundle_gzip: bytes | None = field(default=None, init=False, repr=False)

    @property
    def size(self):
        return len(self.rgba) + len(self.mask)

    def bundle_bytes(self, accept_gzip: bool) -> tuple[bytes, bool]:
        with self._lock:
            if self._bundle_raw is None:
                mask_rle = _rle_encode_mask(self.mask)
                if mask_rle is not None:
                    mask_codec = 1
                    mask_payload = mask_rle
                else:
                    mask_codec = 0
                    mask_payload = self.mask
                header = PSBD_HEADER.pack(
                    b"PSBD",
                    1,
                    self.channels,
                    0,
                    mask_codec,
                    len(self.rgba),
                    len(mask_payload),
                )
                self._bundle_raw = header + self.rgba + mask_payload
            if accept_gzip:
                if self._bundle_gzip is None:
                    self._bundle_gzip = zlib.compress(self._bundle_raw, level=1, wbits=31)
                return self._bundle_gzip, True
            return self._bundle_raw, False


@dataclass
class StagedRun:
    prompt_id: str
    deliveries: dict[str, Delivery] = field(default_factory=dict)


class Bridge:
    def __init__(self):
        self.lock = Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.captures: dict[str, Capture] = {}
        self.capture_requests: dict[str, CaptureRequest] = {}
        self.panel_heartbeats: dict[str, float] = {}
        self.latest = None
        self.staged: dict[str, StagedRun] = {}
        self.pending: dict[str, Delivery] = {}
        self._session_waiters: dict[str, list[asyncio.Event]] = {}
        self._capture_waiters: dict[str, list[asyncio.Event]] = {}
        self._stage_waiter: asyncio.Event | None = None

    def _wake_waiters(self, waiters):
        for event in waiters:
            event.set()

    def _notify_session(self, session_id):
        if not session_id:
            return
        with self.lock:
            waiters = list(self._session_waiters.get(session_id, ()))
            loop = self.loop
        if waiters and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._wake_waiters, waiters)

    def _notify_capture_request(self, request_id):
        if not request_id:
            return
        with self.lock:
            waiters = list(self._capture_waiters.get(request_id, ()))
            loop = self.loop
        if waiters and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._wake_waiters, waiters)

    def _notify_stage(self):
        with self.lock:
            waiter = self._stage_waiter
            loop = self.loop
        if waiter is not None and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(waiter.set)

    def prune(self, now):
        expired_requests = []
        for request_id, capture_request in list(self.capture_requests.items()):
            if capture_request.expires <= now:
                del self.capture_requests[request_id]
                if request_id in self._capture_waiters:
                    expired_requests.extend(self._capture_waiters[request_id])
        if expired_requests and self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._wake_waiters, expired_requests)
        for session_id, seen in list(self.panel_heartbeats.items()):
            if seen + PANEL_HEARTBEAT_TTL <= now:
                del self.panel_heartbeats[session_id]
        for capture_id, capture in list(self.captures.items()):
            if capture.expires <= now:
                del self.captures[capture_id]
        if self.latest not in self.captures:
            self.latest = None
        for job_id, job in list(self.pending.items()):
            if job.expires <= now:
                del self.pending[job_id]
                LOGGER.warning("Photoshop delivery %s expired before acknowledgement", job_id)
        for run_id, run in list(self.staged.items()):
            if all(job.expires <= now for job in run.deliveries.values()):
                del self.staged[run_id]
                LOGGER.warning("Photoshop delivery expired while waiting for ComfyUI prompt %s", run.prompt_id)

    def store_capture(self, payload, queue, request_id=None):
        size = sum(len(asset["raw"]) for asset in payload["assets"].values() if asset is not None)
        now = time.monotonic()
        running, queued = queue.get_current_queue_volatile()
        pinned = {cid for item in (*running, *queued) for cid in _prompt_capture_ids(item[2])}
        with self.lock:
            self.prune(now)
            capture_request = None
            if request_id is not None:
                capture_request = self.capture_requests.get(request_id)
                if capture_request is None or capture_request.session_id != payload["session_id"]:
                    raise BridgeError("The Photoshop capture request expired or belongs to another panel.")
                if capture_request.capture_id is not None:
                    return capture_request.capture_id
                if capture_request.error is not None:
                    raise BridgeError("This capture request has already ended. Click Capture & Preview again.")
            count = len(self.captures)
            total_bytes = sum(capture.size for capture in self.captures.values()) + size
            evict = []
            for archived_id, archived in self.captures.items():
                if count < 128 and total_bytes <= MAX_CAPTURE_BYTES:
                    break
                if archived_id not in pinned and archived.retain_until <= now:
                    evict.append(archived_id)
                    count -= 1
                    total_bytes -= archived.size
            if count >= 128 or total_bytes > MAX_CAPTURE_BYTES:
                raise BridgeError("Photoshop capture storage is full. Wait for queued workflows to finish and capture again.")
            for archived_id in evict:
                del self.captures[archived_id]
            capture_id = payload["capture_id"]
            self.captures[capture_id] = Capture(payload, size, now + CAPTURE_TTL)
            self.latest = capture_id
            if capture_request is not None:
                capture_request.capture_id = capture_id
                capture_request.expires = now + CAPTURE_REQUEST_HISTORY_TTL
        if request_id is not None:
            self._notify_capture_request(request_id)
        return capture_id

    def request_capture(self):
        now = time.monotonic()
        with self.lock:
            self.prune(now)
            if not self.panel_heartbeats:
                raise BridgeError("请在 Photoshop 中打开并连接桥接面板，再点捕获并预览。")
            session_id = max(self.panel_heartbeats, key=self.panel_heartbeats.get)
            for capture_request in self.capture_requests.values():
                if capture_request.session_id == session_id and capture_request.is_pending:
                    request_id = capture_request.id
                    break
            else:
                if len(self.capture_requests) >= 128:
                    raise BridgeError("Photoshop capture request queue is full. Try again shortly.")
                request_id = str(uuid4())
                self.capture_requests[request_id] = CaptureRequest(request_id, session_id, now + CAPTURE_REQUEST_TTL)
        self._notify_session(session_id)
        return request_id

    def capture_request_status(self, request_id):
        with self.lock:
            self.prune(time.monotonic())
            capture_request = self.capture_requests.get(request_id)
            if capture_request is None:
                return None
            return {"capture_id": capture_request.capture_id, "error": capture_request.error}

    async def wait_capture_request(self, request_id, wait_seconds):
        event = asyncio.Event()
        with self.lock:
            self.prune(time.monotonic())
            capture_request = self.capture_requests.get(request_id)
            if capture_request is None:
                return None
            if not capture_request.is_pending or wait_seconds <= 0:
                return {"capture_id": capture_request.capture_id, "error": capture_request.error}
            self._capture_waiters.setdefault(request_id, []).append(event)
        try:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
        finally:
            with self.lock:
                waiters = self._capture_waiters.get(request_id)
                if waiters is not None:
                    with suppress(ValueError):
                        waiters.remove(event)
                    if not waiters:
                        del self._capture_waiters[request_id]
        return self.capture_request_status(request_id)

    def fail_capture_request(self, request_id, session_id, error):
        now = time.monotonic()
        with self.lock:
            self.prune(now)
            capture_request = self.capture_requests.get(request_id)
            if capture_request is None or capture_request.session_id != session_id:
                return False
            if capture_request.is_pending:
                capture_request.error = error
                capture_request.expires = now + CAPTURE_REQUEST_HISTORY_TTL
        self._notify_capture_request(request_id)
        return True

    def bind_prompt(self, request, node_types, local):
        prompt = request.get("prompt")
        if not isinstance(prompt, dict):
            return request
        bridge_nodes = []
        for node in prompt.values():
            if not isinstance(node, dict):
                continue
            node.pop(BINDING_KEY, None)
            class_type = node.get("class_type")
            if isinstance(class_type, str) and class_type in node_types:
                inputs = node.get("inputs")
                if class_type in ("PSBridgeLoadImage", "PSBridgeLoadMask") and isinstance(inputs, dict):
                    inputs.pop("source", None)  # Workflow files from before automatic source selection.
                bridge_nodes.append(node)

        if local and bridge_nodes:
            now = time.monotonic()
            run_id = str(uuid4())
            with self.lock:
                self.prune(now)
                for node in bridge_nodes:
                    class_type = node.get("class_type")
                    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
                    requested = inputs.get("capture_id") if class_type in ("PSBridgeLoadImage", "PSBridgeLoadMask") else None
                    capture_id = requested.strip() if isinstance(requested, str) and requested.strip() else self.latest
                    if capture_id is not None and capture_id in self.captures:
                        snapshot = self.captures[capture_id]
                        snapshot.expires = now + CAPTURE_TTL
                        # ComfyUI validates the prompt before adding it to its visible queue.
                        snapshot.retain_until = now + 120
                    node[BINDING_KEY] = [run_id, capture_id]
        return request

    def get_capture(self, capture_id):
        now = time.monotonic()
        with self.lock:
            capture = self.captures.get(capture_id)
            if capture is not None and capture.expires > now:
                return capture.payload
        if capture_id is None:
            raise BridgeError("No Photoshop capture was available when this workflow was queued. Capture the active layer in Photoshop, then queue it again.")
        raise BridgeError("The Photoshop capture expired. Capture the active layer again and requeue this workflow.")

    def stage(self, prompt_id, run_id, capture, node_id, result):
        rect = _rect(result["rect"])
        if rect not in (capture["layer_rect"], capture["selection_rect"]):
            raise BridgeError("The Photoshop result does not match a captured frame.")
        width, height = rect["right"] - rect["left"], rect["bottom"] - rect["top"]
        channels = result.get("channels", 4)
        rgba, mask = result["rgba"], result["mask"]
        if (
            channels not in (3, 4)
            or not isinstance(rgba, bytes)
            or not isinstance(mask, bytes)
            or len(rgba) != width * height * channels
            or len(mask) != width * height
        ):
            raise BridgeError("The Photoshop result pixels do not match the captured frame.")
        now = time.monotonic()
        metadata = {
            **{key: capture[key] for key in ("session_id", "document_id", "document_width", "document_height")},
            "id": str(uuid4()),
            "left": rect["left"], "top": rect["top"], "width": width, "height": height,
            "channels": channels,
            "layer_name": _string(result["layer_name"], "result layer name"),
        }
        job = Delivery(metadata, rgba, mask, now + CAPTURE_TTL, channels=channels)
        with self.lock:
            self.prune(now)
            run = self.staged.get(run_id)
            if run is not None and (run.prompt_id != prompt_id or node_id in run.deliveries):
                raise BridgeError("A Photoshop save node must produce a single result per run.")
            existing = list(self.pending.values())
            existing.extend(item for item_run in self.staged.values() for item in item_run.deliveries.values())
            if len(existing) >= 64 or sum(item.size for item in existing) + job.size > MAX_RESULT_BYTES:
                raise BridgeError("Photoshop result storage is full. Open the Photoshop bridge panel to receive waiting results.")
            if run is None:
                run = StagedRun(prompt_id)
                self.staged[run_id] = run
            run.deliveries[node_id] = job
        self._notify_stage()

    def reconcile(self, queue):
        now = time.monotonic()
        with self.lock:
            self.prune(now)
            active = [(run_id, run.prompt_id) for run_id, run in self.staged.items()]
        notify_sessions = set()
        for run_id, prompt_id in active:
            history = queue.get_history(prompt_id=prompt_id, map_function=_history_status).get(prompt_id)
            if history is None or history[0] != run_id:
                continue
            with self.lock:
                run = self.staged.pop(run_id, None)
                if run is not None and history[1]:
                    for job in run.deliveries.values():
                        job.expires = now + RESULT_TTL
                        self.pending[job.metadata["id"]] = job
                        notify_sessions.add(job.metadata["session_id"])
        for session_id in notify_sessions:
            self._notify_session(session_id)
        with self.lock:
            return bool(self.staged)

    def _poll_panel_locked(self, session_id, supports_capture, now):
        self.prune(now)
        capture_request = None
        if supports_capture:
            self.panel_heartbeats[session_id] = now
            capture_request = next((
                {"id": item.id} for item in self.capture_requests.values()
                if item.session_id == session_id and item.is_pending
            ), None)
        job = next((job.metadata for job in self.pending.values() if job.metadata["session_id"] == session_id), None)
        return {"job": job, "capture_request": capture_request}

    def poll_panel(self, session_id, supports_capture):
        with self.lock:
            return self._poll_panel_locked(session_id, supports_capture, time.monotonic())

    async def wait_panel(self, session_id, supports_capture, wait_seconds):
        event = asyncio.Event()
        with self.lock:
            result = self._poll_panel_locked(session_id, supports_capture, time.monotonic())
            if result["job"] is not None or result["capture_request"] is not None or wait_seconds <= 0:
                return result
            self._session_waiters.setdefault(session_id, []).append(event)
        try:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
        finally:
            with self.lock:
                waiters = self._session_waiters.get(session_id)
                if waiters is not None:
                    with suppress(ValueError):
                        waiters.remove(event)
                    if not waiters:
                        del self._session_waiters[session_id]
        with self.lock:
            return self._poll_panel_locked(session_id, supports_capture, time.monotonic())

    def drop_session(self, session_id):
        with self.lock:
            self.panel_heartbeats.pop(session_id, None)
            waiters = list(self._session_waiters.get(session_id, ()))
        if waiters and self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._wake_waiters, waiters)

    def job(self, job_id, session_id):
        with self.lock:
            self.prune(time.monotonic())
            job = self.pending.get(job_id)
            if job is not None and job.metadata["session_id"] == session_id:
                return job
        return None

    def acknowledge(self, job_id, session_id):
        with self.lock:
            job = self.pending.get(job_id)
            if job is None:
                return True  # A repeated acknowledgement is harmless.
            if job.metadata["session_id"] != session_id:
                return False
            del self.pending[job_id]
        return True


def _binding(prompt, node_id=None):
    if node_id is not None and isinstance(prompt, dict):
        node = prompt.get(str(node_id))
        binding = node.get(BINDING_KEY) if isinstance(node, dict) else None
        if isinstance(binding, (list, tuple)) and len(binding) == 2:
            return tuple(binding)
    for node in (prompt or {}).values():
        binding = node.get(BINDING_KEY) if isinstance(node, dict) else None
        if isinstance(binding, (list, tuple)) and len(binding) == 2:
            return tuple(binding)
    return (None, None)


def _prompt_capture_ids(prompt):
    if not isinstance(prompt, dict):
        return set()
    ids = set()
    for node in prompt.values():
        binding = node.get(BINDING_KEY) if isinstance(node, dict) else None
        if isinstance(binding, (list, tuple)) and len(binding) == 2 and binding[1] is not None:
            ids.add(binding[1])
    return ids


def _history_status(entry):
    run_id, _ = _binding(entry["prompt"][2])
    status = entry.get("status") or {}
    return run_id, status.get("completed") is True and status.get("status_str") == "success"


_STATE = Bridge()


def get_prompt_binding(prompt, node_id=None):
    run_id, capture_id = _binding(prompt or {}, node_id=node_id)
    if not run_id:
        raise BridgeError("Open ComfyUI through localhost or its SSH tunnel before queuing a Photoshop workflow.")
    return run_id, capture_id


def get_capture(capture_id):
    return _STATE.get_capture(capture_id)


def stage_result(run_id, capture_id, node_id, result):
    context = get_executing_context()
    if context is None or not run_id:
        raise BridgeError("The Photoshop result has no active ComfyUI workflow.")
    _STATE.stage(context.prompt_id, run_id, get_capture(capture_id), node_id, result)


async def _read_body(request, max_bytes):
    if request.content_length is not None and request.content_length > max_bytes:
        raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=request.content_length)
    raw = bytearray()
    async for block in request.content.iter_chunked(256 * 1024):
        raw.extend(block)
        if len(raw) > max_bytes:
            raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=len(raw))
    return bytes(raw)


async def _json(request, max_bytes=MAX_LEGACY_JSON_UPLOAD):
    if request.content_type != "application/json":
        raise BridgeError("Expected application/json.")
    raw = await _read_body(request, max_bytes)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise BridgeError("Invalid JSON body.") from exc
    if not isinstance(payload, dict):
        raise BridgeError("Expected a JSON object.")
    return payload


def _wait_seconds(request, maximum):
    raw = request.query.get("wait")
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise BridgeError("Invalid wait timeout.") from exc
    if not (0.0 <= value <= maximum):
        raise BridgeError("Wait timeout out of range.")
    return value


def _rgba_pixels(job: Delivery) -> bytes:
    if job.channels == 4:
        return job.rgba
    width = job.metadata["width"]
    height = job.metadata["height"]
    rgb = np.frombuffer(job.rgba, dtype=np.uint8).reshape(height, width, 3)
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[:, :, :3] = rgb
    rgba[:, :, 3] = 255
    return rgba.tobytes()


CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _host_peer(request):
    transport = request.transport
    if transport is None:
        return False
    peer = transport.get_extra_info("peername")
    local = transport.get_extra_info("sockname")
    if not isinstance(peer, tuple) or not isinstance(local, tuple):
        return False
    peer_ip = ipaddress.ip_address(peer[0].split("%", 1)[0])
    local_ip = ipaddress.ip_address(local[0].split("%", 1)[0])
    if isinstance(peer_ip, ipaddress.IPv6Address):
        peer_ip = peer_ip.ipv4_mapped or peer_ip
    if isinstance(local_ip, ipaddress.IPv6Address):
        local_ip = local_ip.ipv4_mapped or local_ip
    return (
        peer_ip.is_loopback
        or peer_ip.is_private
        or peer_ip.is_link_local
        or (isinstance(peer_ip, ipaddress.IPv4Address) and peer_ip in CGNAT_NET)
        or peer_ip == local_ip
    )


def _browser_peer(request):
    if not _host_peer(request) or request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
        return False
    try:
        hostname = urlsplit(f"//{request.host}").hostname
        if not hostname:
            return False
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        parsed = urlsplit(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc.casefold() == request.host.casefold()
    except ValueError:
        return False


def register(server, node_types, capture_preview):
    routes = server.routes
    node_types = frozenset(node_types)
    server.add_on_prompt_handler(lambda request: _STATE.bind_prompt(request, node_types, LOCAL_PROMPT.get()))

    @web.middleware
    async def prompt_source(request, handler):
        if (request.method in ("GET", "HEAD") and request.path in ("/view", "/api/view")
                and request.query.get("subfolder") == PREVIEW_SUBFOLDER):
            # ComfyUI's renderer expects native image descriptors. This virtual
            # folder serves private thumbnails without writing files to disk.
            if not _browser_peer(request):
                raise web.HTTPForbidden()
            data = get_preview(request.query.get("filename")) if request.query.get("type") == "temp" else None
            if data is None:
                raise web.HTTPNotFound()
            return web.Response(body=data, content_type="image/png", headers={
                "Cache-Control": "no-store",
                "Cross-Origin-Resource-Policy": "same-origin",
                "X-Content-Type-Options": "nosniff",
            })
        if request.method != "POST" or request.path not in ("/prompt", "/api/prompt"):
            return await handler(request)
        # A co-located SSH forwarder may connect to a ComfyUI LAN bind from
        # the server's own LAN address. Direct traffic from other hosts cannot
        # use the current Photoshop snapshot through the general prompt API.
        marker = LOCAL_PROMPT.set(_browser_peer(request))
        try:
            return await handler(request)
        finally:
            LOCAL_PROMPT.reset(marker)

    server.app.middlewares.append(prompt_source)

    def authorized(handler):
        @wraps(handler)
        async def wrapped(request):
            if not _host_peer(request):
                return web.json_response({"error": "Connect through localhost or an SSH tunnel."}, status=403)
            try:
                response = await handler(request)
            except BridgeError as exc:
                response = web.json_response({"error": str(exc)}, status=400)
            except web.HTTPRequestEntityTooLarge as exc:
                limit_mib = (exc.max_size or MAX_UPLOAD) // (1024 * 1024)
                response = web.json_response({"error": f"Photoshop bridge payload exceeds {limit_mib} MiB."}, status=413)
            response.headers["Cache-Control"] = "no-store"
            return response
        return wrapped

    def browser_only(handler):
        @wraps(handler)
        async def wrapped(request):
            if not _browser_peer(request):
                return web.json_response({"error": "Connect through localhost or an SSH tunnel."}, status=403)
            try:
                response = await handler(request)
            except ValueError as exc:
                response = web.json_response({"error": str(exc)}, status=400)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            return response
        return wrapped

    @routes.get(f"{API}/health")
    @authorized
    async def health(_request):
        return web.json_response({
            "ok": True,
            "version": 2,
            "capabilities": ["binary_capture_v1", "job_bundle_v1", "long_poll_v1"],
        })

    @routes.post(f"{API}/captures")
    @authorized
    async def capture(request):
        if request.content_type == "application/octet-stream":
            raw_packet = await _read_body(request, MAX_UPLOAD)
            validated, request_id = await asyncio.to_thread(_capture_binary, raw_packet)
        elif request.content_type == "application/json":
            payload = await _json(request, MAX_LEGACY_JSON_UPLOAD)
            request_id = payload.get("request_id")
            if request_id is not None:
                request_id = _string(request_id, "capture request ID")
            validated = await asyncio.to_thread(_capture, payload)
        else:
            raise BridgeError("Expected application/octet-stream or application/json.")
        capture_id = _STATE.store_capture(validated, server.prompt_queue, request_id)
        return web.json_response({"capture_id": capture_id}, status=201)

    @routes.post(f"{API}/capture-requests")
    @browser_only
    async def request_capture(request):
        if request.content_type != "application/json":
            raise BridgeError("Expected application/json.")
        return web.json_response({"request_id": _STATE.request_capture()}, status=202)

    @routes.get(f"{API}/capture-requests/{{request_id}}")
    @browser_only
    async def capture_status(request):
        wait = _wait_seconds(request, MAX_CAPTURE_WAIT_SECONDS)
        request_id = request.match_info["request_id"]
        if wait > 0:
            state = await _STATE.wait_capture_request(request_id, wait)
        else:
            state = _STATE.capture_request_status(request_id)
        if state is None:
            return web.json_response({"error": "捕获请求已过期，请再试一次。"}, status=404)
        return web.json_response(state)

    @routes.post(f"{API}/capture-requests/{{request_id}}/error")
    @authorized
    async def capture_error(request):
        payload = await _json(request)
        session_id = _string(payload.get("session_id"), "Photoshop session ID")
        error = _string(payload.get("error"), "Photoshop error", 2048)
        if not _STATE.fail_capture_request(request.match_info["request_id"], session_id, error):
            return web.json_response({"error": "The capture request is unavailable for this panel."}, status=404)
        return web.json_response({"ok": True})

    @routes.get(f"{API}/capture-preview")
    @browser_only
    async def preview(request):
        capture_id = _string(request.query.get("capture_id"), "capture ID")
        capture = get_capture(capture_id)
        descriptor = await asyncio.to_thread(
            capture_preview, capture, request.query.get("kind")
        )
        return web.json_response({"image": descriptor})

    @routes.get(f"{API}/jobs/pending")
    @authorized
    async def pending(request):
        session_id = _string(request.query.get("session_id"), "Photoshop session ID")
        supports_capture = request.query.get("capture_requests") == "1"
        wait = _wait_seconds(request, MAX_LONG_POLL_SECONDS)
        if wait > 0:
            return web.json_response(await _STATE.wait_panel(session_id, supports_capture, wait))
        return web.json_response(_STATE.poll_panel(session_id, supports_capture))

    @routes.post(f"{API}/sessions/bye")
    @authorized
    async def session_bye(request):
        payload = await _json(request)
        session_id = _string(payload.get("session_id"), "Photoshop session ID")
        _STATE.drop_session(session_id)
        return web.json_response({"ok": True})

    @routes.get(f"{API}/jobs/{{job_id}}/bundle")
    @authorized
    async def bundle(request):
        session_id = _string(request.query.get("session_id"), "Photoshop session ID")
        job = _STATE.job(request.match_info["job_id"], session_id)
        if job is None:
            return web.json_response({"error": "Photoshop result is unavailable for this session."}, status=404)
        accept_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower()
        payload, is_gzip = await asyncio.to_thread(job.bundle_bytes, accept_gzip)
        headers = {"Vary": "Accept-Encoding"}
        if is_gzip:
            headers["Content-Encoding"] = "gzip"
        return web.Response(body=payload, content_type="application/octet-stream", headers=headers)

    @routes.get(f"{API}/jobs/{{job_id}}/{{asset:pixels|mask}}")
    @authorized
    async def pixels(request):
        session_id = _string(request.query.get("session_id"), "Photoshop session ID")
        job = _STATE.job(request.match_info["job_id"], session_id)
        if job is None:
            return web.json_response({"error": "Photoshop result is unavailable for this session."}, status=404)
        if request.match_info["asset"] == "pixels":
            body = await asyncio.to_thread(_rgba_pixels, job)
        else:
            body = job.mask
        return web.Response(body=body, content_type="application/octet-stream")

    @routes.post(f"{API}/jobs/{{job_id}}/{{action:complete|error}}")
    @authorized
    async def acknowledge(request):
        payload = await _json(request)
        job_id = request.match_info["job_id"]
        session_id = _string(payload.get("session_id"), "Photoshop session ID")
        error = None
        if request.match_info["action"] == "error":
            error = _string(payload.get("error"), "Photoshop error", 2048)
        else:
            _photoshop_id(payload.get("layer_id"), "result layer ID")
        if not _STATE.acknowledge(job_id, session_id):
            return web.json_response({"error": "Photoshop result is unavailable for this session."}, status=404)
        if error is not None:
            LOGGER.warning("Photoshop rejected result %s: %r", job_id, error)
        return web.json_response({"ok": True})

    async def reconcile_forever(stage_waiter):
        while True:
            has_staged = False
            try:
                has_staged = _STATE.reconcile(server.prompt_queue)
            except Exception:
                LOGGER.exception("Could not check ComfyUI workflow completion")
            if has_staged:
                await asyncio.sleep(0.08)
            else:
                stage_waiter.clear()
                with _STATE.lock:
                    has_staged = bool(_STATE.staged)
                if has_staged:
                    continue
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stage_waiter.wait(), timeout=1.0)

    async def delivery_lifecycle(_app):
        stage_waiter = asyncio.Event()
        with _STATE.lock:
            _STATE.loop = asyncio.get_running_loop()
            _STATE._stage_waiter = stage_waiter
        task = asyncio.create_task(reconcile_forever(stage_waiter))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with _STATE.lock:
                _STATE.loop = None
                _STATE._stage_waiter = None

    server.app.cleanup_ctx.append(delivery_lifecycle)
