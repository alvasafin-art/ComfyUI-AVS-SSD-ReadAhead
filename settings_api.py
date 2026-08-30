from __future__ import annotations

import logging

from aiohttp import web

from . import readahead

_PREFIX = "[AVS ReadAhead]"


def register_routes() -> bool:
    try:
        from server import PromptServer
    except (ImportError, AttributeError) as exc:
        logging.warning("%s settings API unavailable: %s", _PREFIX, exc)
        return False

    instance = getattr(PromptServer, "instance", None)
    if instance is None or not hasattr(instance, "routes"):
        logging.warning("%s settings API unavailable: PromptServer is not initialized", _PREFIX)
        return False

    marker = "_avs_readahead_settings_routes_v1"
    if getattr(instance, marker, False):
        return True

    routes = instance.routes

    @routes.get("/avs-readahead/status")
    async def avs_readahead_status(_request: web.Request) -> web.Response:
        return web.json_response(readahead.status())

    @routes.post("/avs-readahead/enabled")
    async def avs_readahead_enabled(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        enabled = data.get("enabled") if isinstance(data, dict) else None
        if not isinstance(enabled, bool):
            return web.json_response({"error": "'enabled' must be a boolean"}, status=400)

        try:
            result = readahead.set_enabled(enabled, persist=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logging.warning("%s could not update setting: %s", _PREFIX, exc)
            return web.json_response({"error": str(exc), **readahead.status()}, status=500)

        return web.json_response(result)

    setattr(instance, marker, True)
    return True
