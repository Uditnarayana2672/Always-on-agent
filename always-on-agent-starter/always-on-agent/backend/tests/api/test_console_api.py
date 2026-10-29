"""Console API (prompt 1.15): auth, approvals bound to the content hash, settings limits, live updates."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.api.app import create_app
from agent.api.auth import COOKIE, ConsoleAuth
from agent.api.stream import Hub, Watcher
from agent.core.config import CONFIG_DIR, load_config
from agent.db.repositories import Task
from agent.policy.gate import GateContext
from agent.runtime.assembly import Runtime
from agent.runtime.fake import build_fake_runtime
from agent.runtime.settings import OVERRIDES_KEY, POLICY_KEY

BASE = "http://127.0.0.1:8765"
TASK = "INC30114872"


class Env:
    def __init__(self, rt: Runtime, auth: ConsoleAuth, hub: Hub, client: httpx.AsyncClient) -> None:
        self.rt, self.auth, self.hub, self.client = rt, auth, hub, client
        self.headers = {"X-Agent-Token": auth.token, "X-Agent-Request": "1"}

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.get(url, headers=self.headers, **kw)

    async def post(self, url: str, json: Any = None) -> httpx.Response:
        return await self.client.post(url, headers=self.headers, json=json if json is not None else {})

    async def put(self, url: str, json: Any) -> httpx.Response:
        return await self.client.put(url, headers=self.headers, json=json)

    async def propose(self, body: str = "Hi Anna, please add Tomás to the site.") -> str:
        if await self.rt.tasks.get(TASK) is None:
            await self.rt.tasks.create(
                Task(
                    id=TASK, project="doxis", kind="incident", title="Cannot see documents", state="needs_you"
                )
            )
        result = await self.rt.approvals.create(
            TASK,
            "post_chat",
            {"chat_id": "c1", "body": body, "recipients": ["anna.keller@corp.example"]},
            body,
            GateContext(participants=frozenset({"anna.keller@corp.example"})),
        )
        assert result.status == "proposed", result
        return result.action_id


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    rt, _ = await build_fake_runtime(load_config(CONFIG_DIR / "config.example.yaml"), tmp_path / "data")
    await rt.load_recipes()
    auth = ConsoleAuth(rt.kv, rt.clock.now_utc, port=8765)
    hub = Hub()
    app = create_app(rt, auth, fake=True, hub=hub, watch=False, static_dir=tmp_path / "no-dist")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        env = Env(rt, auth, hub, client)
        yield env
    await env.rt.stop()


# ---------------------------------------------------------------- auth


async def test_every_api_route_needs_the_session(env: Env) -> None:
    for url in ("/api/status", "/api/tasks", "/api/approvals", "/api/settings", "/api/stream", "/api/nope"):
        response = await env.client.get(url)
        assert response.status_code == 401, url
    wrong = await env.client.get("/api/status", headers={"X-Agent-Token": "not-it"})
    assert wrong.status_code == 401
    assert (await env.get("/api/status")).status_code == 200


async def test_state_changes_need_the_request_header(env: Env) -> None:
    response = await env.client.post("/api/control/pause", headers={"X-Agent-Token": env.auth.token})
    assert response.status_code == 403
    assert not await env.rt.controls.paused()


async def test_other_hosts_and_origins_are_refused(env: Env) -> None:
    rebind = await env.client.get("/api/status", headers={**env.headers, "Host": "evil.example"})
    assert rebind.status_code == 403
    cross = await env.client.post(
        "/api/control/pause", headers={**env.headers, "Origin": "http://evil.example"}
    )
    assert cross.status_code == 403
    assert "access-control-allow-origin" not in {k.lower() for k in cross.headers}


async def test_one_time_link_sets_the_cookie_once(env: Env) -> None:
    link = await env.auth.login_link()
    path = link.removeprefix(BASE)
    first = await env.client.get(path)
    assert first.status_code == 303
    cookie = first.headers["set-cookie"]
    assert (
        f"{COOKIE}=" in cookie
        and "HttpOnly" in cookie
        and "SameSite=strict" in cookie.replace("Strict", "strict")
    )
    again = await env.client.get(path)
    assert again.status_code == 401  # used
    env.client.cookies.set(COOKIE, env.auth.token)
    assert (await env.client.get("/api/status")).status_code == 200
    env.client.cookies.clear()


async def test_login_codes_expire_and_are_stored_as_digests(env: Env) -> None:
    code = await env.auth.one_time_code()
    stored = await env.rt.kv.get("console.login_codes") or ""
    assert code not in stored
    later = ConsoleAuth(
        env.rt.kv, lambda: env.rt.clock.now_utc() + __import__("datetime").timedelta(minutes=11)
    )
    assert not await later.redeem(code)


# ---------------------------------------------------------------- approvals


async def test_approve_needs_the_current_hash(env: Env) -> None:
    action_id = await env.propose()
    queue = (await env.get("/api/approvals")).json()
    card = next(a for a in queue if a["id"] == action_id)
    assert card["kind"] == "Teams message" and card["task_id"] == TASK

    stale = await env.post(f"/api/approvals/{action_id}/approve", {"content_hash": "0" * 64})
    assert stale.status_code == 409
    assert (await env.rt.actions.get(action_id)).status == "proposed"  # type: ignore[union-attr]

    ok = await env.post(f"/api/approvals/{action_id}/approve", {"content_hash": card["content_hash"]})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    twice = await env.post(f"/api/approvals/{action_id}/approve", {"content_hash": card["content_hash"]})
    assert twice.status_code == 200 and twice.json()["changed"] is False


async def test_edit_returns_a_new_hash_and_the_old_one_no_longer_approves(env: Env) -> None:
    action_id = await env.propose()
    old = next(a for a in (await env.get("/api/approvals")).json() if a["id"] == action_id)["content_hash"]
    edited = await env.post(
        f"/api/approvals/{action_id}/edit", {"text": "Hi Anna, shorter version.", "content_hash": old}
    )
    assert edited.status_code == 200
    new = edited.json()["content_hash"]
    assert new != old
    assert (await env.post(f"/api/approvals/{action_id}/approve", {"content_hash": old})).status_code == 409
    racing = await env.post(f"/api/approvals/{action_id}/edit", {"text": "Another.", "content_hash": old})
    assert racing.status_code == 409  # a second editor working on the old text loses
    assert (await env.post(f"/api/approvals/{action_id}/approve", {"content_hash": new})).status_code == 200


async def test_reject_and_unknown_ids(env: Env) -> None:
    action_id = await env.propose()
    assert (await env.post(f"/api/approvals/{action_id}/reject", {"reason": "not now"})).json()[
        "status"
    ] == "rejected"
    late = await env.post(f"/api/approvals/{action_id}/approve", {"content_hash": "x" * 64})
    assert late.status_code == 409
    assert (await env.post("/api/approvals/NOPE/approve", {"content_hash": "x" * 64})).status_code == 404
    assert (await env.post(f"/api/approvals/{action_id}/approve", {})).status_code == 422  # hash required


async def test_mark_done_closes_the_task_and_drops_its_approvals(env: Env) -> None:
    action_id = await env.propose()
    response = await env.post(f"/api/tasks/{TASK}/done")
    assert response.status_code == 200 and response.json()["column"] == "done"
    assert (await env.rt.actions.get(action_id)).status == "invalidated"  # type: ignore[union-attr]
    assert (await env.post(f"/api/tasks/{TASK}/messages", {"text": "hello"})).status_code == 409
    assert (await env.post("/api/tasks/INC00000000/done")).status_code == 404


# ---------------------------------------------------------------- settings


async def test_settings_change_schedule_and_open_rows(env: Env) -> None:
    response = await env.put(
        "/api/settings",
        {"brief": "08:30", "max_pushes_per_day": 4, "policy": {"file_mail": "Ask", "retry_refresh": "Ask"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["brief"] == "08:30" and body["max_pushes_per_day"] == 4
    rows = {r["key"]: r for r in body["policy"]}
    assert rows["file_mail"]["mode"] == "Ask" and rows["retry_refresh"]["mode"] == "Ask"
    assert env.rt.registry.policy.mode_of("move_mail") == "ask"
    assert env.rt.config.powerbi.retry_own_transient is False
    assert env.rt.scheduler.specs["brief"].times == ("08:30",)
    assert await env.rt.kv.get(OVERRIDES_KEY) and await env.rt.kv.get(POLICY_KEY)


async def test_settings_cannot_touch_locked_rows_people_or_the_data_rule(env: Env) -> None:
    before = env.rt.registry.policy
    for policy in ({"delete": "Ask"}, {"ticket_state": "Report"}, {"send": "Report"}, {"read": "Hand off"}):
        response = await env.put("/api/settings", {"policy": policy})
        assert response.status_code == 422, policy
    assert env.rt.registry.policy == before
    assert env.rt.registry.policy.mode_of("delete_item") == "hand_off"
    rule = await env.put("/api/settings", {"data_rule": "text"})
    assert rule.status_code == 422  # not a field the console may send
    level = await env.put("/api/settings", {"data_rule": {"level": "images"}})
    assert level.status_code == 422
    assert env.rt.config.data_rule.level == "metadata"
    bad = await env.put("/api/settings", {"work_start": "19:00", "work_end": "09:00"})
    assert bad.status_code == 422


async def test_settings_survive_a_restart(env: Env, tmp_path: Path) -> None:
    await env.put("/api/settings", {"brief": "08:45", "policy": {"save_draft": "Ask"}})
    await env.rt.stop()
    env.rt = await _reopen(tmp_path)  # the fixture stops this one
    assert env.rt.config.schedule.brief == "08:45"
    assert env.rt.registry.policy.mode_of("save_draft") == "ask"
    assert env.rt.registry.policy.mode_of("delete_item") == "hand_off"


async def _reopen(tmp_path: Path) -> Runtime:
    rt, _ = await build_fake_runtime(load_config(CONFIG_DIR / "config.example.yaml"), tmp_path / "data")
    return rt


# ---------------------------------------------------------------- live updates


async def test_stream_emits_after_an_approval(env: Env) -> None:
    action_id = await env.propose()
    card = next(a for a in (await env.get("/api/approvals")).json() if a["id"] == action_id)
    queue = env.hub.subscribe()
    try:
        await env.post(f"/api/approvals/{action_id}/approve", {"content_hash": card["content_hash"]})
        kinds = []
        while not queue.empty():
            kinds.append(queue.get_nowait().kind)
        assert "approval" in kinds
    finally:
        env.hub.unsubscribe(queue)


async def test_stream_body_is_server_sent_events() -> None:
    hub = Hub()
    stop = asyncio.Event()

    async def gone() -> bool:
        return stop.is_set()

    chunks: list[str] = []

    async def read() -> None:
        async for chunk in hub.events(gone, heartbeat=0.05):
            chunks.append(chunk)
            if "event: approval" in chunk:
                stop.set()

    reader = asyncio.create_task(read())
    while hub.subscribers == 0:
        await asyncio.sleep(0)
    await hub.publish("approval", {"id": "A1"})
    await asyncio.wait_for(reader, 2)
    text = "".join(chunks)
    assert text.startswith("retry: ")
    assert 'event: approval\ndata: {"id": "A1"}' in text
    assert hub.subscribers == 0


async def test_watcher_sees_changes_made_outside_the_api(env: Env) -> None:
    watcher = Watcher(env.rt, env.hub)
    queue = env.hub.subscribe()
    try:
        assert await watcher.check() == []  # first look: nothing to compare with
        await env.propose()  # a worker drafting, not the API
        changed = await watcher.check()
        assert "approval" in changed and "activity" in changed
        await env.rt.controls.pause()
        assert "control" in await watcher.check()
    finally:
        env.hub.unsubscribe(queue)


async def test_watcher_sees_one_approval_replaced_by_another(env: Env) -> None:
    # Approving a draft closes one card and the worker opens the send card: same count, and the
    # largest open hash can stay the same, yet the console must still hear about it.
    from sqlalchemy import update

    from agent.db import schema as s

    table = s.action_requests

    async def set_row(action_id: str, **values: str) -> None:
        async with env.rt.engine.begin() as conn:
            await conn.execute(update(table).where(table.c.id == action_id).values(**values))

    watcher = Watcher(env.rt, env.hub)
    queue = env.hub.subscribe()
    try:
        first = await env.propose("Draft one")
        second = await env.propose("Draft two")
        await set_row(first, content_hash="a" * 64)
        await set_row(second, content_hash="f" * 64)
        await watcher.check()
        await set_row(first, status="done")
        third = await env.propose("Email to send")
        await set_row(third, content_hash="0" * 64)
        assert "approval" in await watcher.check()
    finally:
        env.hub.unsubscribe(queue)


# ---------------------------------------------------------------- pages


async def test_pages_answer_on_the_fake_day(env: Env) -> None:
    from agent.adapters.fakes.world import FakeWorld
    from agent.runtime.fake import run_demo

    await run_demo(env.rt, FakeWorld(env.rt.clock))
    for url in (
        "/api/brief",
        "/api/tasks",
        "/api/tasks?project=Doxis",
        "/api/recipes",
        "/api/recipes/tools",
        "/api/activity",
        "/api/activity?mode=Report",
        "/api/workers",
        "/api/memory/personal",
        "/api/memory/directory",
        "/api/memory/lessons",
        "/api/memory/sources",
        "/api/memory/tasks",
        "/api/connectors",
        "/api/settings",
    ):
        response = await env.get(url)
        assert response.status_code == 200, (url, response.text)
    tasks = (await env.get("/api/tasks")).json()
    assert tasks and {t["column"] for t in tasks} <= {"needs", "progress", "waiting", "scheduled", "done"}
    detail = await env.get(f"/api/tasks/{tasks[0]['id']}")
    assert detail.status_code == 200
    assert (await env.get("/api/memory/nope")).status_code == 404
    recipes = {r["slug"]: r for r in (await env.get("/api/recipes")).json()}
    assert recipes["morning-brief"]["name"] == "Morning brief"
    assert recipes["morning-brief"]["status"] == "Live"  # Phase 1: R01, R02, R04, R06 Live
    assert recipes["draft-replies"]["status"] == "Shadow"  # R03 and R05 until the user decides


async def test_recipe_status_and_save_keep_the_rules(env: Env, tmp_path: Path) -> None:
    import shutil

    folder = tmp_path / "recipes"
    shutil.copytree(CONFIG_DIR / "recipes", folder)
    env.rt.loader.directory = folder
    await env.rt.load_recipes()

    off = await env.post("/api/recipes/inbox-triage/status", {"status": "Off"})
    assert off.status_code == 200 and off.json()["status"] == "Off"
    text = (folder / "inbox-triage.yaml").read_text(encoding="utf-8")
    assert "status: off" in text and text.startswith("#")  # comments kept

    new = {
        "name": "Weekly ticket digest",
        "project": "Core",
        "trigger": "schedule: 16:00",
        "model": "Haiku",
        "status": "Live",
        "tools": ["read_mail", "notify_me"],
        "instructions": "List what closed this week.",
        "output": "dm",
    }
    created = await env.put("/api/recipes/weekly-digest", new)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "Shadow"  # new recipes start in Shadow, whatever was asked
    hand_off = await env.put("/api/recipes/weekly-digest", {**new, "tools": ["read_mail", "delete_item"]})
    assert hand_off.status_code == 422
    bad = await env.put("/api/recipes/weekly-digest", {**new, "trigger": "schedule: 25:00"})
    assert bad.status_code == 422
    edited = await env.put(
        "/api/recipes/morning-brief",
        {**new, "name": "Morning brief", "tools": ["read_brief_data", "publish_brief"]},
    )
    assert edited.status_code == 200, edited.text
    kept = (folder / "morning-brief.yaml").read_text(encoding="utf-8")
    assert "steps: agent.recipes.steps.morning_brief:steps" in kept and kept.startswith("# R01")
    assert (await env.put("/api/recipes/..%2Fevil", new)).status_code in (404, 422)


async def test_recipe_test_runs_on_sample_data(env: Env) -> None:
    response = await env.post("/api/recipes/inbox-triage/test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "nothing was sent" in body["notes"][-1]


async def test_controls_pause_reset_with_confirmation(env: Env) -> None:
    assert (await env.post("/api/control/reset/request")).status_code == 409  # pause first
    assert (await env.post("/api/control/pause")).json()["paused"] is True
    assert (await env.get("/api/status")).json()["paused"] is True
    token = (await env.post("/api/control/reset/request")).json()["token"]
    assert (await env.post("/api/control/reset", {"token": "wrong-token"})).status_code == 409
    done = await env.post("/api/control/reset", {"token": token})
    assert done.status_code == 200 and "audit log is kept" in done.json()["message"]
    assert (await env.post("/api/control/reset", {"token": token})).status_code == 409  # once
    assert (await env.post("/api/control/resume")).json()["paused"] is False


async def test_undo_only_for_report_actions(env: Env) -> None:
    audit_id = await env.rt.audit.append("send_mail", "ask", "h", "done", "agent", TASK)
    assert (await env.post(f"/api/activity/{audit_id}/undo")).status_code == 422
    assert (await env.post("/api/activity/999999/undo")).status_code == 404


async def test_a_console_message_is_in_the_thread_at_once_and_only_once(env: Env) -> None:
    await env.propose()
    response = await env.post(f"/api/tasks/{TASK}/messages", {"text": "please draft a shorter version"})
    assert response.status_code == 200 and response.json()["who"] == "you"
    events = (await env.get(f"/api/tasks/{TASK}")).json()["events"]
    mine = [e for e in events if e["text"] == "please draft a shorter version"]
    assert len(mine) == 1 and mine[0]["name"] == "You"
    await env.rt.drain()
    events = (await env.get(f"/api/tasks/{TASK}")).json()["events"]
    assert len([e for e in events if e["text"] == "please draft a shorter version"]) == 1
