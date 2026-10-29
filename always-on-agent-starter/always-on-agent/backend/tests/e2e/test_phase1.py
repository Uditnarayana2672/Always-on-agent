"""Phase 1 end to end (prompt 1.18): one simulated working day on FakeWorld.

A FrozenClock walks Wednesday 25 March from 08:00 to 20:00 IST one minute at a time, doing what agentd
does: a heartbeat every minute (scheduler.tick), each cron slot and poller when it falls due, then the
workers. The Slack bot runs on a fake web client and is the notification and approval channel, as in a
real run. The day:

    08:00  start; the mail poller reads the fixture inbox, triage files it, replies are drafted
    09:00  morning brief and the first check-in
    09:30  the user approves the drafts in Slack, then the sends (one click is delivered twice)
    10:30  a refresh of the user's own dataset fails with a temporary error: alert, one retry
    12:00  the laptop sleeps; a newsletter arrives at 12:30; the 13:00 check-in and 13:30 chase are due
    14:00  wake: the gap is noticed, missed slots run once, the newsletter is filed
    19:00  nightly digest with Undo; the user presses Undo (twice)

Then, over the whole day: no action ran above its mode, and every side effect ran exactly once.
"""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent.adapters.fakes.world import FakeWorld
from agent.channels import cards
from agent.channels.slack_bot import SlackBot
from agent.core.clock import FrozenClock
from agent.core.config import CONFIG_DIR, Config, load_config
from agent.model.fake import DemoModel
from agent.policy.modes import RANK
from agent.runtime.assembly import Runtime
from agent.runtime.fake import fake_config, live_recipes
from agent.runtime.pollers import WATERMARK
from agent.runtime.scheduler import FireResult, occurrences
from tests.channels.test_slack_bot import FakeWeb

ME = "UME"
START = datetime(2026, 3, 25, 2, 30, tzinfo=UTC)  # 08:00 IST
END = datetime(2026, 3, 25, 14, 30, tzinfo=UTC)  # 20:00 IST
SLEEP = ("12:00", "14:00")  # IST, the laptop is asleep: nothing runs
SALES = "bab6126e-511d-55fc-a827-124aa86f4a3f"  # Sales Overview, configured by the user
INVENTORY = "ed820357-fbf8-5c13-8fd4-4379afb0bb05"  # Inventory Health, someone else's
MANAGER_MAIL = "AAMkAGFAKE0011"
NEWSLETTER = "AAMkAGFAKE0013"
ASLEEP_MAIL = "AAMkAGFAKE0099"

# Which fake write each tool's effect is (and its inverse, for Undo).
EFFECTS = {
    "move_mail": ("outlook", "move"),
    "flag_mail": ("outlook", "flag"),
    "categorise": ("outlook", "categorise"),
    "save_draft": ("outlook", "create_draft"),
    "send_mail": ("outlook", "send"),
    "trigger_refresh": ("powerbi", "trigger_refresh"),
}


def day_config() -> Config:
    base = fake_config(load_config(CONFIG_DIR / "config.example.yaml"))
    watch = base.powerbi.watch.model_copy(update={"datasets": [SALES, INVENTORY]})
    powerbi = base.powerbi.model_copy(update={"watch": watch, "owned_datasets": [SALES]})
    slack = base.slack.model_copy(update={"user_id": ME})
    return base.model_copy(update={"powerbi": powerbi, "slack": slack})


class Day:
    def __init__(
        self, rt: Runtime, world: FakeWorld, clock: FrozenClock, bot: SlackBot, web: FakeWeb
    ) -> None:
        self.rt, self.world, self.clock, self.bot, self.web = rt, world, clock, bot, web
        self.catch_ups: list[FireResult] = []
        self.live: list[FireResult] = []
        self.replies: list[str] = []
        self.clicks = 0

    @classmethod
    async def open(cls, folder: Path) -> "Day":
        config = day_config()
        clock = FrozenClock(config, START)
        world = FakeWorld(clock)
        web = FakeWeb()
        bot = SlackBot(web, ME, clock, f"http://127.0.0.1:{config.console.port}")
        rt = await Runtime.build(
            config,
            clock,
            {a.name: a for a in world.adapters()},
            db_url=f"sqlite+aiosqlite:///{(folder / 'agent.db').as_posix()}",
            checkpoints=folder / "checkpoints.db",
            notify_channels=[bot],
            approval_channels=[bot],
            personal_dir=folder / "memory",
            recipes_dir=live_recipes(folder),
            model=DemoModel(),
            data_dir=folder,
        )
        # As runtime.real wires it: the bot is made before the services it calls.
        bot._approvals, bot._ingest, bot._links = rt.approvals, rt.coordinator.ingest, rt.memory
        bot._undo, bot._health = rt.undo.undo, rt.health
        await bot.mark(True)  # Socket Mode connected
        await rt.load_recipes()
        week_ago = (START - timedelta(days=7)).isoformat()
        for source in ("mail", "teams"):
            await rt.kv.set(WATERMARK.format(source=source), week_ago)
        await rt.kv.set(WATERMARK.format(source="powerbi"), START.isoformat())
        assert await rt.scheduler.catch_up() == []  # first start: only the heartbeat
        return cls(rt, world, clock, bot, web)

    # ------------------------------------------------------------ the clock

    def local(self, at: datetime) -> str:
        return self.clock.to_local(at).strftime("%H:%M")

    async def run(self, until: datetime, events: dict[str, Any]) -> None:
        """Minute by minute up to `until`, skipping the sleep. events: IST "HH:MM" -> callable."""
        at = self.clock.now_utc()
        previous = at - timedelta(minutes=1)
        while at <= until:
            hh_mm = self.local(at)
            if SLEEP[0] <= hh_mm < SLEEP[1]:
                if hh_mm in events:
                    await events[hh_mm]()  # the world goes on while the laptop sleeps
                at += timedelta(minutes=1)
                continue
            self.clock.set(at)
            self.catch_ups += await self.rt.scheduler.tick(at)
            for name, spec in self.rt.scheduler.specs.items():
                if spec.kind == "cron":
                    # APScheduler fires the slots it missed too; the slot table must refuse them.
                    for slot in occurrences(spec, previous, at, self.rt.scheduler._calendar):
                        if previous < slot <= at:
                            self.live.append(await self.rt.scheduler.fire(name, slot))
                elif (at - START) % timedelta(minutes=spec.every_minutes or 1) == timedelta(0):
                    self.live.append(await self.rt.scheduler.fire(name, at))
            await self.rt.drain()
            if hh_mm in events:
                await events[hh_mm]()
                await self.rt.drain()
            previous = at
            at += timedelta(minutes=1)

    # ------------------------------------------------------------ Slack

    def texts(self) -> list[str]:
        return [str(post.get("text") or "") for post in self.web.posts]

    def buttons(self, action_id: str) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        """(post, block, button) for every button of this kind posted so far."""
        found = []
        for post in self.web.posts:
            for block in post.get("blocks") or []:
                elements = block.get("elements") or [] if block["type"] == "actions" else []
                elements = [*elements, block["accessory"]] if block.get("accessory") else elements
                found += [(post, block, e) for e in elements if e.get("action_id") == action_id]
        return found

    async def click(self, action_id: str, value: str, *, redelivered: bool = False) -> None:
        if not redelivered:
            self.clicks += 1
        body = {
            "user": {"id": ME},
            "trigger_id": f"trig-{self.clicks}",
            "actions": [{"action_id": action_id, "value": value, "action_ts": str(self.clicks)}],
        }
        await self.bot.on_action(body, self._respond)
        await self.rt.drain()

    async def _respond(self, text: str) -> None:
        self.replies.append(text)

    async def approve_cards(self, tool_label: str, redeliver_last: bool = False) -> list[str]:
        """Press Approve on every open card whose text names tool_label. Returns the action ids."""
        pending = {a.id for a in await self.rt.approvals.pending()}
        pressed = []
        for post, _, button in self.buttons(cards.APPROVE):
            action_id = json.loads(button["value"])["a"]
            if action_id in pending and tool_label in str(post.get("text")):
                await self.click(cards.APPROVE, button["value"])
                pressed.append((action_id, button["value"]))
        if redeliver_last and pressed:
            await self.click(cards.APPROVE, pressed[-1][1], redelivered=True)  # Slack retries the click
            await self.click(cards.APPROVE, pressed[-1][1])  # the user presses again
        return [a for a, _ in pressed]


def newsletter_at(received: datetime) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "fixtures" / "emails" / "messages.json"
    raw = next(m for m in json.loads(path.read_text(encoding="utf-8")) if m["id"] == NEWSLETTER)
    return {
        **raw,
        "id": ASLEEP_MAIL,
        "conversationId": "AAQkAGFAKECONV0099",
        "internetMessageId": "<fake-0099@corp.example>",
        "subject": "IT News - April preview",
        "receivedDateTime": received.isoformat().replace("+00:00", "Z"),
    }


@pytest.fixture
async def day(tmp_path: Path) -> Any:
    opened = await Day.open(tmp_path)
    try:
        yield opened
    finally:
        await opened.rt.stop()


async def test_a_simulated_phase1_day(day: Day) -> None:
    rt, world = day.rt, day.world
    drafts: list[str] = []
    sends: list[str] = []

    async def approve_drafts() -> None:
        drafts.extend(await day.approve_cards("save_draft"))

    async def approve_sends() -> None:
        sends.extend(await day.approve_cards("Send this email", redeliver_last=True))

    async def refresh_fails() -> None:
        world.powerbi.deliver_refresh(
            SALES,
            {
                "requestId": "r-e2e-failed",
                "refreshType": "Scheduled",
                "status": "Failed",
                "startTime": "2026-03-25T04:50:00Z",
                "endTime": "2026-03-25T04:55:00Z",
                "serviceExceptionJson": json.dumps({"errorCode": "DM_GWPipeline_Gateway_TimeoutError"}),
            },
        )

    async def retry_finishes() -> None:
        running = world.powerbi.refreshes[SALES][0]
        assert running["status"] == "Unknown"  # the one retry the agent started
        running.update(status="Completed", endTime="2026-03-25T05:40:00Z")

    async def newsletter_while_asleep() -> None:
        world.mail.deliver(newsletter_at(day.clock.now_utc() + timedelta(minutes=30)))

    await day.run(
        END,
        {
            "09:30": approve_drafts,
            "09:31": approve_sends,
            "10:30": refresh_fails,
            "11:10": retry_finishes,
            "12:30": newsletter_while_asleep,
        },
    )
    texts = day.texts()

    # Brief: once, at 09:00, from the fixture day.
    briefs = [t for t in texts if t.startswith("Morning brief")]
    assert len(briefs) == 1 and "Replies owed" in briefs[0]

    # Triage: manager mail stays in the Inbox, flagged; the newsletter is filed; nothing deleted.
    assert world.mail.folders[MANAGER_MAIL] == "Inbox"
    assert any(w.args.get("message_id") == MANAGER_MAIL for w in world.mail.writes_to("flag"))
    assert not [w for w in world.mail.writes if "delete" in w.method or "discard" in w.method]

    # Draft replies approved in Slack: each saved once; the reply with an outside cc is handed off,
    # the others sent once each, though Slack delivered one Approve twice and the user pressed it again.
    assert len(drafts) == 3 and len(sends) == 2
    assert len(world.mail.writes_to("create_draft")) == 3
    assert len(world.mail.sent) == len(world.mail.writes_to("send")) == 2
    assert "Nothing was done: this request is already done." in day.replies
    sent_tasks = {(await rt.actions.get(a)).task_id for a in sends}  # type: ignore[union-attr]
    for task_id in sent_tasks:
        task = await rt.tasks.get(str(task_id))
        assert task is not None and task.state == "waiting"
    handed = [t for t in await rt.tasks.list() if t.next_action and t.next_action.startswith("Hand-off")]
    assert len(handed) == 1 and handed[0].id not in sent_tasks

    # Refresh failure: one alert, one retry (Report, own dataset, temporary error), nothing stuck.
    alerts = [t for t in texts if t.startswith(f"Refresh of {SALES}")]
    assert len(alerts) == 1 and "Retrying once" in alerts[0]
    assert [w.args for w in world.powerbi.writes_to("trigger_refresh")] == [{"dataset_id": SALES}]

    # Sleep 12:00-14:00: the 13:00 check-in and the 13:30 chase run once, on wake; the late live fires
    # of the same slots are refused; the newsletter that arrived asleep is filed after wake.
    woke = {r.schedule: r for r in day.catch_ups if r.status == "caught_up"}
    assert {"checkin", "chase_window", "poll:mail"} <= set(woke)
    assert all(day.local(r.due_at) in ("13:00", "13:30", "14:00") for r in woke.values())
    duplicates = {r.schedule for r in day.live if r.status == "duplicate"}
    assert {"checkin", "chase_window"} <= duplicates
    runs = await rt.schedule_runs.recorded_since("checkin", START)
    assert sorted(day.local(slot) for slot in runs) == ["09:00", "11:00", "13:00", "15:00", "17:00"]
    assert world.mail.folders[ASLEEP_MAIL] == "Newsletters"
    filed = [w for w in world.mail.writes_to("move") if w.args["message_id"] == ASLEEP_MAIL]
    assert len(filed) == 1 and day.local(filed[0].at) == "14:00"

    # Nightly digest: once, at 19:00, every Report action today with Undo.
    digests = [t for t in texts if t.startswith("Today's digest")]
    assert len(digests) == 1 and "(undo " in digests[0]
    move = next(
        b for _, block, b in day.buttons(cards.UNDO) if "Moved a mail to Newsletters" in json.dumps(block)
    )
    moves_before = len(world.mail.writes_to("move"))
    await day.click(cards.UNDO, move["value"])
    await day.click(cards.UNDO, move["value"])  # pressed again later: nothing more happens
    assert len(world.mail.writes_to("move")) == moves_before + 1
    assert sorted(world.mail.folders[m] for m in (NEWSLETTER, ASLEEP_MAIL)) == ["Inbox", "Newsletters"]
    assert day.replies[-2:] == ["Undone.", "Already undone."]

    # No action above its mode: every effect ran at or above the tool's configured mode; Ask effects
    # only with the user's approval of that exact text; Hand off never. The one exception is by design
    # (architecture 3.3): trigger_refresh's mode_fn makes one retry of the user's own dataset Report.
    rows = await rt.audit.recent(5000)
    done = [r for r in rows if r["result"] == "done"]
    assert done
    own_retries = 0
    for row in done:
        tool = rt.registry.get(row["tool"])
        assert tool is not None, row
        configured = rt.registry.policy.mode_of(row["tool"]) or tool.mode
        if row["tool"] == "trigger_refresh" and row["mode"] == "report":
            own_retries += 1
            continue
        assert RANK[row["mode"]] >= RANK[configured], row
        assert row["mode"] != "hand_off", row
        if row["mode"] == "ask":
            action = await rt.actions.get(row["action_id"])
            assert action is not None and action.decided_by == "slack", row
            assert action.content_hash == row["args_digest"], row
    assert own_retries == 1  # and it was on SALES, the owned dataset (checked above)

    # Every effect exactly once: each fake write matches one audited effect (the undo's move included:
    # it goes through the outbox like any other effect, and the original row records it once).
    expected: Counter[tuple[str, str]] = Counter(EFFECTS[r["tool"]] for r in done if r["tool"] in EFFECTS)
    assert [r["result"] for r in rows if str(r["result"]).startswith("undo")] == ["undo: done"]
    actual: Counter[tuple[str, str]] = Counter(
        (name, w.method) for name, adapter in rt.adapters.items() for w in getattr(adapter, "writes", [])
    )
    assert actual == expected
    keys = [a.idempotency_key for a in await rt.actions.by_status("done")]
    assert len(keys) == len(set(keys))
