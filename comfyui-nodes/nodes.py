"""ComfyUI nodes for loading a Photoshop capture and returning a masked layer."""

import base64
import binascii
import io
from dataclasses import dataclass

import numpy as np
import torch
from PIL import Image

from .bridge import get_capture, get_prompt_binding, stage_result
from .previews import publish_preview, result_preview


CATEGORY = "Photoshop Bridge"
FRAME_TYPE = "PS_BRIDGE_FRAME"
MASK_FOR_IMAGE_SOURCE = {"layer": "layer_mask", "selection": "selection"}
MASK_ASSET = {"layer_mask": "layer_mask", "selection": "selection_mask"}
MASK_MISSING = {
    "layer_mask": "The captured Photoshop layer has no pixel mask. Add a layer mask and capture it again.",
    "selection": "The Photoshop capture has no active selection. Make a selection and capture it again.",
}


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @classmethod
    def from_asset(cls, asset):
        return cls(**asset["rect"])

    @property
    def size(self):
        return self.right - self.left, self.bottom - self.top

    def as_dict(self):
        return {"left": self.left, "top": self.top, "right": self.right, "bottom": self.bottom}


@dataclass(frozen=True)
class PhotoshopFrame:
    run_id: str
    capture_id: str
    source: str
    rect: Rect


def _asset_image(asset, mode):
    size = Rect.from_asset(asset).size
    if "raw" in asset:
        image = Image.frombytes(asset["mode"], size, asset["raw"])
        return image if image.mode == mode else image.convert(mode)
    try:
        encoded = base64.b64decode(asset["png"], validate=True)
        with Image.open(io.BytesIO(encoded), formats=["PNG"]) as png:
            if png.size != size:
                raise ValueError("Photoshop image dimensions differ from its recorded bounds. Capture it again.")
            return png.convert(mode)
    except (binascii.Error, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("The Photoshop PNG could not be read. Capture it again.") from exc


def _load_mask(asset, target):
    source = Rect.from_asset(asset)
    mask = _asset_image(asset, "L")
    if source == target:
        return mask

    # Move between rectangles in Photoshop canvas coordinates, without scaling.
    aligned = Image.new("L", target.size)
    left = max(source.left, target.left)
    top = max(source.top, target.top)
    right = min(source.right, target.right)
    bottom = min(source.bottom, target.bottom)
    if left < right and top < bottom:
        overlap = mask.crop((left - source.left, top - source.top, right - source.left, bottom - source.top))
        aligned.paste(overlap, (left - target.left, top - target.top))
    return aligned


def _to_tensor(image):
    return torch.from_numpy(np.asarray(image, dtype=np.float32) / 255.0).unsqueeze(0)


def _asset_tensor(asset, channels):
    expected_mode = "L" if channels == 1 else "RGB"
    if "raw" in asset and asset.get("mode") == expected_mode:
        width, height = Rect.from_asset(asset).size
        shape = (height, width) if channels == 1 else (height, width, channels)
        array = np.frombuffer(asset["raw"], dtype=np.uint8).reshape(shape)
        return torch.from_numpy(array.astype(np.float32) / 255.0).unsqueeze(0)
    return _to_tensor(_asset_image(asset, expected_mode))


def _to_byte_pixels(tensor):
    array = tensor.detach().to(device="cpu", dtype=torch.float32).numpy()
    if not np.isfinite(array).all():
        raise ValueError("Photoshop Bridge received pixels outside the finite numeric range.")
    return np.rint(np.clip(array, 0, 1) * 255).astype(np.uint8)


def _image_bytes(image, size):
    if image.ndim != 4 or image.shape[0] != 1 or image.shape[-1] not in (3, 4) or min(image.shape[1:3]) < 1:
        raise ValueError("Photoshop Bridge Save Image requires one non-empty RGB or RGBA image. Select a single image from the batch.")
    channels = int(image.shape[-1])
    byte_pixels = _to_byte_pixels(image[0])
    if image.shape[2] == size[0] and image.shape[1] == size[1]:
        return np.ascontiguousarray(byte_pixels).tobytes(), channels
    pixels = Image.fromarray(byte_pixels).resize(size, Image.Resampling.LANCZOS)
    return pixels.tobytes(), channels


def _mask_bytes(mask, size):
    if mask.ndim == 3 and mask.shape[0] == 1:
        mask = mask[0]
    if mask.ndim != 2 or min(mask.shape) < 1:
        raise ValueError("Photoshop Bridge Save Image requires one non-empty mask. Select a single mask from the batch.")
    byte_pixels = _to_byte_pixels(mask)
    if mask.shape[1] == size[0] and mask.shape[0] == size[1]:
        return np.ascontiguousarray(byte_pixels).tobytes()
    pixels = Image.fromarray(byte_pixels).resize(size, Image.Resampling.BILINEAR)
    return pixels.tobytes()


def _require_frame(frame, run_id):
    if not isinstance(frame, PhotoshopFrame) or frame.run_id != run_id:
        raise ValueError("Connect the frame output of PS Bridge · Load Image from this run.")


def _source(capture):
    return "selection" if capture["selection_rect"] is not None else "layer"


def _image_asset(capture):
    return capture["assets"][f"{_source(capture)}_image"]


def _mask_asset(capture):
    source = MASK_FOR_IMAGE_SOURCE[_source(capture)]
    asset = capture["assets"][MASK_ASSET[source]]
    if asset is None:
        raise ValueError(MASK_MISSING[source])
    return asset


def _input_preview(capture, kind, rect, make_image):
    return publish_preview(
        make_image,
        key=(capture["capture_id"], kind, _source(capture), rect),
        source_size=rect.size,
    )


def capture_preview(capture, kind):
    if kind == "image":
        asset = _image_asset(capture)
        return _input_preview(capture, kind, Rect.from_asset(asset), lambda: _asset_image(asset, "RGB"))
    if kind == "mask":
        asset = _mask_asset(capture)
        rect = Rect.from_asset(asset)
        return _input_preview(capture, kind, rect, lambda: _load_mask(asset, rect))
    raise ValueError("Unknown Photoshop preview type.")


class PSBridgeLayerName:
    CATEGORY = CATEGORY
    DESCRIPTION = "Set the name for the layer returned to Photoshop."
    FUNCTION = "get_layer_name"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("layer_name",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"default": "ComfyUI Result", "multiline": True}),
            },
        }

    def get_layer_name(self, text):
        return (text,)


class PSBridgeLoadImage:
    CATEGORY = CATEGORY
    DESCRIPTION = "Load pixels within the captured selection, or the whole active layer when there is no selection. Also output a full-white mask with the same dimensions as the image."
    FUNCTION = "load_image"
    RETURN_TYPES = ("IMAGE", FRAME_TYPE, "MASK")
    RETURN_NAMES = ("image", "frame", "mask")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {"capture_id": ("STRING", {"default": ""})},
            "hidden": {"prompt": "PROMPT", "node_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(cls, **_):
        return float("nan")

    def load_image(self, capture_id="", prompt=None, node_id=None):
        run_id, bound_capture_id = get_prompt_binding(prompt, node_id)
        capture = get_capture(bound_capture_id)
        asset = _image_asset(capture)
        rect = Rect.from_asset(asset)
        frame = PhotoshopFrame(run_id, bound_capture_id, _source(capture), rect)
        preview = _input_preview(capture, "image", rect, lambda: _asset_image(asset, "RGB"))
        width, height = rect.size
        mask = torch.ones((1, height, width), dtype=torch.float32)
        return {"ui": {"images": [preview]}, "result": (_asset_tensor(asset, 3), frame, mask)}


class PSBridgeLoadMask:
    CATEGORY = CATEGORY
    DESCRIPTION = "Read the captured selection mask, or the active layer's pixel mask when there is no selection. White is 1."
    FUNCTION = "load_mask"
    RETURN_TYPES = ("MASK",)
    RETURN_NAMES = ("mask",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
                "frame": (FRAME_TYPE,),
                "capture_id": ("STRING", {"default": ""}),
            },
            "hidden": {"prompt": "PROMPT", "node_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(cls, **_):
        return float("nan")

    def load_mask(self, frame=None, capture_id="", prompt=None, node_id=None):
        run_id, bound_capture_id = get_prompt_binding(prompt, node_id)
        capture = get_capture(bound_capture_id)
        asset = _mask_asset(capture)
        source_rect = Rect.from_asset(asset)
        target = source_rect
        if frame is not None:
            _require_frame(frame, run_id)
            target = frame.rect
        if source_rect == target and "raw" in asset and asset.get("mode") == "L":
            preview = _input_preview(capture, "mask", target, lambda: _asset_image(asset, "L"))
            tensor = _asset_tensor(asset, 1)
        else:
            mask = _load_mask(asset, target)
            preview = _input_preview(capture, "mask", target, lambda: mask)
            tensor = _to_tensor(mask)
        return {"ui": {"images": [preview]}, "result": (tensor,)}


class PSBridgeSaveImage:
    CATEGORY = CATEGORY
    DESCRIPTION = "Return one processed image as a masked layer in its original Photoshop position after the workflow succeeds."
    FUNCTION = "save_image"
    RETURN_TYPES = ()
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "frame": (FRAME_TYPE,),
                "layer_name": ("STRING", {"default": "ComfyUI Result"}),
            },
            "optional": {"mask": ("MASK",)},
            "hidden": {"prompt": "PROMPT", "node_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(cls, **_):
        return float("nan")

    def save_image(self, image, frame, layer_name, mask=None, prompt=None, node_id=None):
        run_id, _ = get_prompt_binding(prompt, node_id)
        _require_frame(frame, run_id)
        capture = get_capture(frame.capture_id)
        size = frame.rect.size
        if mask is None:
            source = MASK_FOR_IMAGE_SOURCE[frame.source]
            asset = capture["assets"][MASK_ASSET[source]]
            if asset is None:
                mask_data = b"\xff" * (size[0] * size[1])
            elif Rect.from_asset(asset) == frame.rect and "raw" in asset and asset.get("mode") == "L":
                mask_data = asset["raw"]
            else:
                mask_data = _load_mask(asset, frame.rect).tobytes()
        else:
            mask_data = _mask_bytes(mask, size)
        layer_name = layer_name.replace("\0", " ").replace("\r", " ").replace("\n", " ").strip()[:160]
        pixels, channels = _image_bytes(image, size)
        stage_result(run_id, frame.capture_id, node_id, {
            "rect": frame.rect.as_dict(),
            "layer_name": layer_name or "ComfyUI Result",
            "rgba": pixels,
            "channels": channels,
            "mask": mask_data,
        })
        preview = publish_preview(lambda: result_preview(size, pixels, mask_data, channels=channels))
        return {"ui": {"images": [preview]}}


NODE_CLASS_MAPPINGS = {
    "PSBridgeLayerName": PSBridgeLayerName,
    "PSBridgeLoadImage": PSBridgeLoadImage,
    "PSBridgeLoadMask": PSBridgeLoadMask,
    "PSBridgeSaveImage": PSBridgeSaveImage,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PSBridgeLayerName": "PS Bridge · Layer Name",
    "PSBridgeLoadImage": "PS Bridge · Load Image",
    "PSBridgeLoadMask": "PS Bridge · Load Mask",
    "PSBridgeSaveImage": "PS Bridge · Save Image",
}
