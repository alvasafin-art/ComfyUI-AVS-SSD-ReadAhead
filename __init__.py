"""ComfyUI Sequential ReadAhead v0.9.0.

Production cleanup of the validated v0.8 I/O path:
- Windows ordinary cached sequential prefetch in a short-lived helper process
- adaptive RAM-bounded warm-prefix budgeting
- helper cancellation before sampling
- capability-based ComfyUI hooks; no hard ComfyUI/GPU-vendor lock
- no model-unload hooks, working-set trimming, mmap bouncing, sleeps, or
  custom device-transfer synchronization
"""

from .readahead import install_patch

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

install_patch()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
