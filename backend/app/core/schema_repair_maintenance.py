"""Closed maintenance shell: no auth, data, WebSocket or farm admission."""

from starlette.responses import JSONResponse

active = False


class SchemaRepairMaintenanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not active or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1013})
        response = JSONResponse(
            {"status": "maintenance", "reason": "schema_repair_pending"},
            status_code=503,
            headers={"Retry-After": "60"},
        )
        return await response(scope, receive, send)
