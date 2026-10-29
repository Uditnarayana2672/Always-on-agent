"""The console's FastAPI app (F15): /api for the console, / for the built console (console/dist).

Every /api route except the one-time login needs the session (agent.api.auth). Errors come back as
{"detail": "..."} with 404 (unknown id), 409 (no longer applies: a stale hash, already decided, wrong
state) or 422 (not allowed from the console: a locked row, the data rule, a Hand off tool).
"""

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent.api import models as m
from agent.api.auth import COOKIE, ConsoleAuth
from agent.api.service import Conflict, ConsoleService, NotFound, Refused
from agent.api.stream import Hub, Watcher
from agent.core.config import CONFIG_DIR
from agent.runtime.assembly import Runtime

DIST = CONFIG_DIR.parent / "console" / "dist"
NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>Always-On Agent</title>
<body style="font-family:system-ui;padding:32px"><h1>Always-On Agent</h1>
<p>The console is not built yet. Run <code>npm run build</code> in <code>console/</code>.</p></body>"""


def create_app(
    rt: Runtime,
    auth: ConsoleAuth,
    *,
    fake: bool = False,
    static_dir: Path | None = None,
    hub: Hub | None = None,
    watch: bool = True,
) -> FastAPI:
    service = ConsoleService(rt, fake=fake)
    hub = hub or Hub()
    watcher = Watcher(rt, hub)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if watch:
            watcher.start()
        try:
            yield
        finally:
            await watcher.stop()

    app = FastAPI(title="Always-On Agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.service = service
    app.state.hub = hub
    app.state.auth = auth

    async def listener(kind: str, data: dict[str, Any]) -> None:
        if kind in ("task", "approval", "activity", "worker", "control"):
            await hub.publish(kind, data)

    rt.listeners.append(listener)

    @app.middleware("http")
    async def _local_only(request: Request, call_next: Any) -> Response:
        try:
            auth.check_host(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Conflict)
    async def _conflict(request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(Refused)
    async def _refused(request: Request, exc: Refused) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    # ------------------------------------------------------------ login (no session yet)

    @app.get("/api/auth/login", include_in_schema=False)
    async def login(code: Annotated[str, Query(min_length=8, max_length=200)]) -> Response:
        if not await auth.redeem(code):
            return HTMLResponse(
                "<p>This link was already used or is more than ten minutes old. "
                "Run <code>agentd console</code> for a new one.</p>",
                status_code=401,
            )
        response = RedirectResponse("/#brief", status_code=303)
        response.set_cookie(COOKIE, auth.token, httponly=True, samesite="strict", path="/", secure=False)
        return response

    # ------------------------------------------------------------ the API (session required)

    def require(request: Request) -> None:
        auth.require(request)

    api = APIRouter(prefix="/api", dependencies=[Depends(require)])

    @api.get("/status")
    async def status() -> m.StatusOut:
        return await service.status()

    @api.get("/brief")
    async def brief() -> m.BriefOut:
        return await service.brief()

    @api.get("/tasks")
    async def tasks(project: str | None = None) -> list[m.TaskCardOut]:
        return await service.tasks(project)

    @api.get("/tasks/{task_id}")
    async def task(task_id: str) -> m.TaskDetailOut:
        return await service.task(task_id)

    @api.post("/tasks/{task_id}/messages")
    async def message(task_id: str, body: m.MessageIn) -> m.EventOut:
        out = await service.message(task_id, body.text)
        await hub.publish("task", {"id": task_id})
        return out

    @api.post("/tasks/{task_id}/done")
    async def done(task_id: str) -> m.TaskCardOut:
        out = await service.mark_done(task_id)
        await hub.publish("task", {"id": task_id})
        await hub.publish("approval", {"task_id": task_id})
        return out

    @api.get("/approvals")
    async def approvals() -> list[m.ApprovalOut]:
        return await service.approvals()

    @api.post("/approvals/{action_id}/approve")
    async def approve(action_id: str, body: m.ApproveIn) -> m.DecisionOut:
        out = await service.approve(action_id, body.content_hash)
        await hub.publish("approval", {"id": action_id, "status": out.status})
        await hub.publish("task", {})
        return out

    @api.post("/approvals/{action_id}/edit")
    async def edit(action_id: str, body: m.EditIn) -> m.DecisionOut:
        out = await service.edit(action_id, body.text, body.content_hash)
        await hub.publish("approval", {"id": action_id, "status": out.status})
        return out

    @api.post("/approvals/{action_id}/reject")
    async def reject(action_id: str, body: m.RejectIn) -> m.DecisionOut:
        out = await service.reject(action_id, body.reason)
        await hub.publish("approval", {"id": action_id, "status": out.status})
        await hub.publish("task", {})
        return out

    @api.get("/recipes")
    async def recipes() -> list[m.RecipeOut]:
        return await service.recipes()

    @api.get("/recipes/tools")
    async def recipe_tools() -> m.RecipeToolsOut:
        return m.RecipeToolsOut(tools=service.tools())

    @api.get("/recipes/{slug}")
    async def recipe(slug: str) -> m.RecipeOut:
        return await service.recipe(slug)

    @api.put("/recipes/{slug}")
    async def save_recipe(slug: str, body: m.RecipeIn) -> m.RecipeOut:
        out = await service.save_recipe(slug, body)
        await hub.publish("activity", {"recipe": slug})
        return out

    @api.post("/recipes/{slug}/status")
    async def recipe_status(slug: str, body: m.RecipeStatusIn) -> m.RecipeOut:
        out = await service.set_recipe_status(slug, body.status)
        await hub.publish("activity", {"recipe": slug})
        return out

    @api.post("/recipes/{slug}/test")
    async def test_recipe(slug: str) -> m.RecipeTestOut:
        return await service.test_recipe(slug)

    @api.get("/activity")
    async def activity(mode: str | None = None) -> list[m.ActivityOut]:
        return await service.activity(mode)

    @api.post("/activity/{audit_id}/undo")
    async def undo(audit_id: int) -> m.UndoOut:
        out = await service.undo(audit_id)
        await hub.publish("activity", {"id": audit_id})
        return out

    @api.get("/workers")
    async def workers() -> m.WorkersOut:
        return await service.workers()

    @api.get("/memory/{section}")
    async def memory(section: str) -> m.MemoryOut:
        return await service.memory(section)

    @api.post("/memory/sources/{source}/reindex")
    async def reindex(source: str) -> m.ControlOut:
        raise Conflict("Re-indexing arrives with the reference imports in Phase 2.")

    @api.post("/lessons/{lesson_id}")
    async def lesson(lesson_id: str, body: m.LessonIn) -> m.LessonOut:
        return await service.lesson(lesson_id, body.action)

    @api.get("/connectors")
    async def connectors() -> list[m.ConnectorOut]:
        return await service.connectors()

    @api.get("/settings")
    async def settings() -> m.SettingsOut:
        return service.settings()

    @api.put("/settings")
    async def save_settings(body: m.SettingsIn) -> m.SettingsOut:
        out = await service.save_settings(body)
        await hub.publish("control", {"settings": True})
        return out

    @api.post("/control/pause")
    async def pause() -> m.ControlOut:
        out = await service.pause()
        await hub.publish("control", {"paused": True})
        return out

    @api.post("/control/resume")
    async def resume() -> m.ControlOut:
        out = await service.resume()
        await hub.publish("control", {"paused": False})
        return out

    @api.post("/control/checkin")
    async def checkin() -> m.ControlOut:
        out = await service.checkin()
        await hub.publish("worker", {})
        return out

    @api.post("/control/reset/request")
    async def reset_request() -> m.ResetTokenOut:
        return await service.request_reset()

    @api.post("/control/reset")
    async def reset(body: m.ResetIn) -> m.ControlOut:
        out = await service.reset(body.token)
        for kind in ("task", "approval", "activity", "control"):
            await hub.publish(kind, {"reset": True})
        return out

    @api.get("/stream")
    async def stream(request: Request) -> StreamingResponse:
        return StreamingResponse(
            hub.events(request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    app.include_router(api)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False)
    async def _unknown(rest: str, request: Request) -> Response:
        auth.require(request)
        raise NotFound(f"no route /api/{rest}")

    dist = static_dir if static_dir is not None else DIST
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="console")
    else:

        @app.get("/", include_in_schema=False)
        async def _not_built() -> HTMLResponse:
            return HTMLResponse(NOT_BUILT)

    return app
