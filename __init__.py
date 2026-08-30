"""AVS SSD ReadAhead for ComfyUI.

Standalone custom-node adaptation of the Windows model ReadAhead PR. It keeps
ComfyUI's normal model loader authoritative and adds no command-line argument.
"""

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

# ComfyUI loads this directory as a package. The guard also lets static tools and
# standalone test discovery inspect the repository without a running ComfyUI.
if __package__:
    from .readahead import install_patch
    from .settings_api import register_routes

    install_patch()
    register_routes()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
