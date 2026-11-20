from server import PromptServer

from .bridge import register
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, capture_preview

WEB_DIRECTORY = "./web"
register(PromptServer.instance, NODE_CLASS_MAPPINGS, capture_preview)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
