"""In-memory thumbnails addressed through ComfyUI's native image preview UI."""

import io
import time
from collections import OrderedDict
from threading import Lock
from uuid import NAMESPACE_URL, uuid4, uuid5

from PIL import Image, ImageChops

SUBFOLDER = "__photoshop_bridge_previews__"
THUMBNAIL_SIZE = (1024, 1024)
MAX_BYTES = 64 * 1024 * 1024
PREVIEW_TTL = 2 * 60 * 60


class PreviewStore:
    def __init__(self):
        self.lock = Lock()
        self.items = OrderedDict()
        self.total_bytes = 0

    def _get(self, filename, now):
        item = self.items.get(filename)
        if item is None:
            return None
        expires, data = item
        if expires <= now:
            self.total_bytes -= len(data)
            del self.items[filename]
            return None
        self.items.move_to_end(filename)
        return data

    def get(self, filename):
        with self.lock:
            return self._get(filename, time.monotonic())

    def publish(self, make_image, key=None, source_size=None):
        name = uuid5(NAMESPACE_URL, repr(key)) if key is not None else uuid4()
        # The native preview URL carries the caption dimensions with the PNG,
        # including when ComfyUI restores an output from execution history.
        if source_size is not None:
            width, height = source_size
            name = f"{name}_{width}x{height}"
        filename = f"{name}.png"
        descriptor = {"filename": filename, "subfolder": SUBFOLDER, "type": "temp"}
        if self.get(filename) is not None:
            return descriptor
        image = make_image()
        width, height = image.size
        if width > THUMBNAIL_SIZE[0] or height > THUMBNAIL_SIZE[1]:
            image = image.copy()
            image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG", compress_level=3)
        data = output.getvalue()
        with self.lock:
            now = time.monotonic()
            if self._get(filename, now) is not None:
                return descriptor
            self.items[filename] = (now + PREVIEW_TTL, data)
            self.total_bytes += len(data)
            while self.total_bytes > MAX_BYTES:
                _, (_, removed) = self.items.popitem(last=False)
                self.total_bytes -= len(removed)
        return descriptor


_PREVIEWS = PreviewStore()
get_preview = _PREVIEWS.get
publish_preview = _PREVIEWS.publish


def result_preview(size, rgba, mask):
    image = Image.frombytes("RGBA", size, rgba)
    image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
    alpha = Image.frombytes("L", size, mask).resize(image.size, Image.Resampling.LANCZOS)
    image.putalpha(ImageChops.multiply(image.getchannel("A"), alpha))
    return image
