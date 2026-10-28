"""R01-R06 on fixtures, with the real recipe files from config/recipes and the full tool registry."""

from datetime import timedelta
from typing import Any

import pytest

from agent.adapters.fakes.world import FIXTURE_NOW
from agent.core.config import CONFIG_DIR, Config, load_config
from agent.db.repositories import Task
from agent.model.gateway import GatewayResult
from agent.privacy.pseudonymiser import KnownPerson
from agent.recipes.harness import run_recipe_on_fixture
from agent.recipes.steps import morning_brief
from agent.recipes.views import BRIEF_DATE_KEY
from agent.runtime.coordinator import normalise

ME = "test.user@corp.example"
DASH = chr(0x2013)
MANAGER = "anna.keller@corp.example"


def config(**mail: Any) -> Config:
    base = load_config(CONFIG_DIR / "config.example.yaml")
    powerbi = base.powerbi.model_copy(update={"owned_datasets": ["ds-own"]})
    return base.model_copy(
        update={
            "me": [ME],
            "company_domains": ["corp.example"],
            "mail": base.mail.model_copy(
                update={"manager": [MANAGER], "newsletter_senders": ["newsletter@corp.example"], **mail}
            ),
            "powerbi": powerbi,
        }
    )


def tools(result: Any) -> list[str]:
    return [p.tool for p in result.proposals]


def slack_text(result: Any) -> str:
    return "\n".join(str(d["text"]) + " " + str(d.get("blocks")) for d in result.slack)


# ---------------------------------------------------------------- R01 morning brief


async def test_brief_with_nothing_to_report_is_two_lines() -> None:
    async def empty_day(parts: dict[str, Any]) -> None:
        parts["world"].calendar.raw.clear()

    result = await run_recipe_on_fixture("morning-brief", None, config=config(), seed=empty_day)
    assert tools(result) == ["publish_brief"]
    assert result.results[0].status == "done"  # Silent: it goes out even while the recipe is in shadow
    text = result.slack[0]["text"]
    assert text.splitlines() == [f"Morning brief {DASH} Wed 25 Mar", "Nothing needs you right now."]


async def seed_tasks(parts: dict[str, Any]) -> None:
    parts["world"].calendar.raw.clear()
    now = parts["clock"].now_utc()
    ledger = parts["tasks"]  # raw repo: tasks can be seeded in any state
    await ledger.create(
        Task(
            id="INC30114872",
            project="doxis",
            kind="incident",
            title="Login fails on site Basel",
            state="needs_you",
            clock_started_at=now - timedelta(hours=19),
            next_action="Approve the reply to the caller",
        )
    )
    await ledger.create(
        Task(
            id="INC30115230",
            project="doxis",
            kind="incident",
            title="Site list empty",
            state="in_progress",
            clock_started_at=now - timedelta(hours=3),
        )
    )
    await ledger.create(
        Task(
            id="REF-0412",
            project="powerbi",
            kind="refresh_request",
            title="Refresh Sales Overview",
            state="needs_you",
        )
    )
    await ledger.create(
        Task(id="RPL-0001", project="general", kind="reply", title="Sales numbers look off", state="new")
    )
    await parts["health"].report("teams", "down", "sign in again")


async def test_brief_ranks_clocks_first_and_keeps_ids_when_the_model_rewords() -> None:
    reworded = {
        "brief_lines": [
            {
                "lines": [
                    "teams: down (sign in again)",
                    f"INC30114872 {chr(0x2013)} Login fails on site Basel {chr(0xB7)} 19 h, past 18 h",
                    "REF-0412 Refresh Sales Overview: needs you",
                    f"RPL-0002 {DASH} Sales numbers look off",  # invented id: this line is not taken
                ]
            }
        ]
    }
    result = await run_recipe_on_fixture(
        "morning-brief", None, config=config(), seed=seed_tasks, model_responses=reworded
    )
    brief = result.proposals[0].args
    headings = [s["heading"] for s in brief["sections"]]
    assert headings[:4] == ["Connections", "24-hour clocks", "Needs you", "Replies owed"]
    assert brief["sections"][2]["lines"] == ["REF-0412 Refresh Sales Overview: needs you"]
    assert brief["sections"][3]["lines"][0].startswith("RPL-0001")  # the model's RPL-0002 was refused
    assert "INC30115230" not in str(brief)  # 3 h: under the first mark, not needing the user


async def test_brief_after_a_missed_day_says_so_at_the_top() -> None:
    async def seed(parts: dict[str, Any]) -> None:
        await parts["kv"].set(BRIEF_DATE_KEY, "2026-03-23")  # Monday's brief ran, Tuesday's did not

    result = await run_recipe_on_fixture("morning-brief", None, config=config(), seed=seed)
    missed = result.proposals[0].args["missed"]
    assert missed and "Tue 24 Mar" in missed
    assert result.slack[0]["text"].splitlines()[1].startswith("No brief ran on Tue 24 Mar")


async def test_brief_after_a_weekend_is_not_missed() -> None:
    async def seed(parts: dict[str, Any]) -> None:
        await parts["kv"].set(BRIEF_DATE_KEY, "2026-03-24")

    result = await run_recipe_on_fixture("morning-brief", None, config=config(), seed=seed)
    assert result.proposals[0].args["missed"] is None


async def test_brief_still_goes_out_when_the_calendar_is_down() -> None:
    async def seed(parts: dict[str, Any]) -> None:
        parts["world"].calendar.fail_next("500", times=3)

    result = await run_recipe_on_fixture("morning-brief", None, config=config(), seed=seed)
    sections = {s["heading"]: s["lines"] for s in result.proposals[0].args["sections"]}
    assert "Could not check" in sections and "calendar" in sections["Could not check"][0]


def test_same_facts_guard() -> None:
    assert morning_brief.same_facts("INC30114872 at 19.5 h", "19.5 h so far on INC30114872")
    assert not morning_brief.same_facts("INC30114872 at 19.5 h", "INC30114872 at 20 h")


# ---------------------------------------------------------------- R02 inbox triage


def mail_fixture(message_id: str) -> str:
    return f"emails/messages.json#{message_id}"


async def test_triage_keeps_manager_mail_in_inbox_flagged() -> None:
    result = await run_recipe_on_fixture(
        "inbox-triage",
        mail_fixture("AAMkAGFAKE0011"),
        config=config(),
        model_responses={"triage": [{"label": "notification", "confidence": 0.95}]},
        execute=True,
    )
    assert tools(result) == ["flag_mail"]  # never moved, even when labelled a notification
    assert result.results[0].status == "shadow"  # recipes ship in shadow: recorded, not run


async def test_triage_rules_first_and_never_deletes() -> None:
    result = await run_recipe_on_fixture("inbox-triage", mail_fixture("AAMkAGFAKE0013"), config=config())
    # The fixture already carries the Newsletter category: it is not set again, only moved.
    assert tools(result) == ["move_mail"]
    assert result.proposals[0].args["folder"] == "Newsletters"


async def test_triage_needs_reply_opens_a_reply_task() -> None:
    result = await run_recipe_on_fixture(
        "inbox-triage",
        mail_fixture("AAMkAGFAKE0018"),
        config=config(),
        model_responses={"triage": [{"label": "needs_reply", "confidence": 0.9}]},
    )
    assert tools(result) == ["flag_mail", "open_reply_task"]
    # A closed label carries no text into the arguments, so these stay Report (architecture R02).
    assert not result.proposals[0].triggered_by_fetched_content
    assert {r.decision.mode for r in result.results if r.decision} == {"report"}


async def test_triage_leaves_unsure_or_injected_mail_alone() -> None:
    result = await run_recipe_on_fixture(
        "inbox-triage",
        mail_fixture("AAMkAGFAKE0018"),
        config=config(),
        model_responses={"triage": [{"label": "notification", "confidence": 0.3}]},
    )
    assert tools(result) == [] and any("unsure" in n for n in result.notes)


async def test_triage_skips_mail_the_user_already_filed() -> None:
    async def seed(parts: dict[str, Any]) -> None:
        parts["world"].mail.folders["AAMkAGFAKE0013"] = "Archive"

    result = await run_recipe_on_fixture(
        "inbox-triage", mail_fixture("AAMkAGFAKE0013"), config=config(), seed=seed
    )
    assert tools(result) == [] and any("already in Archive" in n for n in result.notes)


async def test_triage_on_protected_mail_reads_nothing() -> None:
    message = {
        "id": "AAMkAGFAKE0099",
        "conversationId": "AAQkAGFAKECONV0099",
        "subject": "Salary review",
        "from": {"emailAddress": {"name": "HR", "address": "hr@corp.example"}},
        "receivedDateTime": "2026-03-25T03:30:00Z",
        "sensitivity": "confidential",
        "body": {"contentType": "text", "content": "secret"},
    }

    async def seed(parts: dict[str, Any]) -> None:
        parts["world"].mail.deliver(message)

    result = await run_recipe_on_fixture(
        "inbox-triage", {"source": "mail", "message": message}, config=config(), seed=seed
    )
    assert tools(result) == [] and any("protected" in n for n in result.notes)


# ---------------------------------------------------------------- R03 draft replies


def reply_seed(message_id: str, conversation: str) -> Any:
    async def seed(parts: dict[str, Any]) -> None:
        await parts["ledger"].create(
            Task(
                id="RPL-0001",
                project="general",
                kind="reply",
                title="Integration test schedule",
                state="new",
                recipe="draft-replies",
            )
        )
        await parts["memory"].link("RPL-0001", "mail_conversation", conversation)
        await parts["memory"].set_fact(
            "RPL-0001", "_reply_message_id", message_id, source_ref=f"mail:{message_id}"
        )
        mail = await parts["world"].mail.get_message(message_id)
        await parts["people"].remember(
            "RPL-0001",
            [KnownPerson(name=p.name, email=p.address, role=None) for p in [mail.sender, *mail.to, *mail.cc]],
        )

    return seed


async def test_draft_with_an_outside_cc_becomes_hand_off() -> None:
    result = await run_recipe_on_fixture(
        "draft-replies",
        mail_fixture("AAMkAGFAKE0016"),
        config=config(),
        seed=reply_seed("AAMkAGFAKE0016", "AAQkAGFAKECONV0016"),
        model_responses={"caller_drafts": [{"reply": "Thursday works for the integration test."}]},
        execute=True,
    )
    assert tools(result) == ["save_draft", "send_mail"]
    send = result.proposals[1]
    assert send.args["cc"] == ["marc.dubois@partner.example"] and send.content.startswith("Thursday")
    assert result.results[-1].status == "handed_off"
    assert any("outside the company" in r for r in result.results[-1].decision.reasons)


async def test_draft_inside_the_company_waits_for_approval() -> None:
    result = await run_recipe_on_fixture(
        "draft-replies",
        mail_fixture("AAMkAGFAKE0012"),
        config=config(),
        seed=reply_seed("AAMkAGFAKE0012", "AAQkAGFAKECONV0012"),
        model_responses={"caller_drafts": [{"reply": "I am checking the March numbers today."}]},
        execute=True,
    )
    send = result.results[-1]
    assert send.decision.mode == "ask" and send.status == "shadow"  # in shadow: recorded with its text
    assert result.proposals[1].args["drafted_on"] == "sonnet"


class HaikuOnly:
    """A gateway whose Sonnet budget is spent: it answers on Haiku and says so."""

    async def run(self, purpose: str, messages: list[Any], schema: Any, data_classes: Any, **kw: Any) -> Any:
        return GatewayResult[schema](
            output=schema(reply="Done, please check again."), model="haiku-x", tier="haiku", downgraded=True
        )  # type: ignore[valid-type]


async def test_draft_made_on_haiku_is_marked() -> None:
    result = await run_recipe_on_fixture(
        "draft-replies",
        mail_fixture("AAMkAGFAKE0012"),
        config=config(),
        seed=reply_seed("AAMkAGFAKE0012", "AAQkAGFAKECONV0012"),
        model=HaikuOnly(),  # type: ignore[arg-type]
        execute=True,
    )
    assert result.proposals[1].args["drafted_on"] == "haiku"


async def test_no_draft_when_the_user_answered_last() -> None:
    async def seed(parts: dict[str, Any]) -> None:
        await reply_seed("AAMkAGFAKE0012", "AAQkAGFAKECONV0012")(parts)
        parts["world"].mail.deliver(
            {
                "id": "AAMkAGFAKE0098",
                "conversationId": "AAQkAGFAKECONV0012",
                "subject": "Re: Sales Overview numbers look off for March",
                "from": {"emailAddress": {"name": "Test User", "address": ME}},
                "receivedDateTime": "2026-03-25T03:40:00Z",
                "body": {"contentType": "text", "content": "Looking into it."},
            }
        )

    result = await run_recipe_on_fixture(
        "draft-replies", mail_fixture("AAMkAGFAKE0012"), config=config(), seed=seed, execute=True
    )
    assert tools(result) == [] and any("answered last" in n for n in result.notes)


# ---------------------------------------------------------------- R04 mention digest


async def seed_teams(parts: dict[str, Any], extra: list[Any] | None = None) -> None:
    world = parts["world"]
    for message in [*await world.teams.list_chat_messages(FIXTURE_NOW - timedelta(days=30)), *(extra or [])]:
        event = normalise(message, "teams")
        await parts["events"].add(
            event.source, event.kind, event.external_id, event.payload, received_at=event.received_at
        )


async def test_mention_digest_lists_unanswered_oldest_first() -> None:
    result = await run_recipe_on_fixture("mention-digest", None, config=config(), seed=seed_teams)
    assert tools(result) == ["notify_me"]
    text = result.proposals[0].args["text"]
    assert "unanswered in Teams, oldest first" in text and "mentioned you" in text
    assert result.slack and "unanswered" in result.slack[0]["text"]


async def test_mention_digest_drops_chats_the_user_answered() -> None:
    from agent.adapters.base import ChatMessage

    async def seed(parts: dict[str, Any]) -> None:
        world = parts["world"]
        replies = [
            ChatMessage(
                id=f"me-{chat}",
                chat_id=chat,
                chat_topic=c.get("topic"),
                sender="Test User",
                sent_at=FIXTURE_NOW,
                text="On it",
                from_me=True,
            )
            for chat, c in world.teams.chats.items()
        ]
        await seed_teams(parts, replies)

    result = await run_recipe_on_fixture("mention-digest", None, config=config(), seed=seed)
    assert tools(result) == [] and result.slack == []


# ---------------------------------------------------------------- R05 refresh watch


def refresh_event(dataset: str, code: str, request: str = "r9") -> dict[str, Any]:
    return {
        "source": "powerbi",
        "event": {
            "kind": "refresh",
            "external_id": f"refresh:{dataset}:{request}:Failed",
            "payload": {"dataset_id": dataset, "status": "Failed", "error_code": code, "request_id": request},
        },
    }


async def test_refresh_watch_retries_once_for_a_transient_error_on_own_dataset() -> None:
    result = await run_recipe_on_fixture(
        "refresh-watch", refresh_event("ds-own", "DM_GWPipeline_Gateway_TimeoutError"), config=config()
    )
    assert tools(result) == ["notify_me", "trigger_refresh"]
    alert, retry = result.results
    assert alert.status == "done" and "Retrying once" in result.proposals[0].args["text"]
    assert retry.decision.mode == "report" and retry.status == "shadow"


@pytest.mark.parametrize(
    ("dataset", "code", "why"),
    [
        ("ds-other", "DM_GWPipeline_Gateway_TimeoutError", "Not your dataset"),
        ("ds-own", "ModelRefresh_InvalidCredentials", "Not a known temporary error"),
    ],
)
async def test_refresh_watch_only_alerts_otherwise(dataset: str, code: str, why: str) -> None:
    result = await run_recipe_on_fixture("refresh-watch", refresh_event(dataset, code), config=config())
    assert tools(result) == ["notify_me"] and why in result.proposals[0].args["text"]


async def test_refresh_watch_does_not_retry_twice_a_day() -> None:
    async def seed(parts: dict[str, Any]) -> None:
        await parts["actions"].create(
            action_id="A-earlier",
            task_id=None,
            tool="trigger_refresh",
            args={"dataset_id": "ds-own"},
            mode="report",
            status="failed",
            content_hash="h",
            idempotency_key="earlier-retry",
        )

    result = await run_recipe_on_fixture(
        "refresh-watch",
        refresh_event("ds-own", "DM_GWPipeline_Gateway_TimeoutError", "r10"),
        config=config(),
        seed=seed,
    )
    assert tools(result) == ["notify_me"] and "Already retried once today" in result.proposals[0].args["text"]


async def test_refresh_watch_ignores_success_and_alerts_stuck() -> None:
    ok = refresh_event("ds-own", "")
    ok["event"]["payload"]["status"] = "Completed"
    ok["event"]["external_id"] = "refresh:ds-own:r1:Completed"
    assert tools(await run_recipe_on_fixture("refresh-watch", ok, config=config())) == []
    stuck = refresh_event("ds-own", "")
    stuck["event"].update(kind="refresh_stuck", external_id="refresh:ds-own:r2:stuck")
    stuck["event"]["payload"].update(status="Unknown", start="2026-03-25T00:00:00+00:00")
    result = await run_recipe_on_fixture("refresh-watch", stuck, config=config())
    assert tools(result) == ["notify_me"] and "running since" in result.proposals[0].args["text"]


# ---------------------------------------------------------------- R06 nightly digest


async def test_nightly_digest_has_undo_open_approvals_and_shadow_counts() -> None:
    async def seed(parts: dict[str, Any]) -> None:
        audit, undo, actions = parts["audit"], parts["undo"], parts["actions"]
        await actions.create(
            action_id="A-move",
            task_id=None,
            tool="move_mail",
            args={"message_id": "m1", "folder": "Notifications"},
            mode="report",
            status="done",
            content_hash="h1",
            idempotency_key="k-move",
        )
        moved = await audit.append(
            tool="move_mail",
            mode="report",
            args_digest="h1",
            result="done",
            actor="inbox-triage",
            recipe="inbox-triage",
            action_id="A-move",
        )
        await undo.add(moved, "move_mail", {"message_id": "m1", "folder": "Inbox"})
        flagged = await audit.append(
            tool="flag_mail",
            mode="report",
            args_digest="h2",
            result="done",
            actor="inbox-triage",
            recipe="inbox-triage",
        )
        op = await undo.add(flagged, "set_flag", {"message_id": "m2", "flagged": False})
        await undo.mark(op, "available", "done")
        for _ in range(3):
            await audit.append(
                tool="categorise",
                mode="report",
                args_digest="h3",
                result="decision:shadow (recipe in shadow: recorded only)",
                actor="inbox-triage",
                recipe="inbox-triage",
            )
        await actions.create(
            action_id="A-ask",
            task_id=None,
            tool="send_mail",
            args={"draft_id": "d", "to": ["x@corp.example"]},
            mode="ask",
            status="proposed",
            content_hash="h4",
            idempotency_key="k-ask",
        )

    result = await run_recipe_on_fixture("nightly-digest", None, config=config(), seed=seed)
    digest = result.proposals[0].args
    assert digest["items"][0]["text"] == "Moved a mail to Notifications" and digest["items"][0]["audit_id"]
    assert digest["items"][1]["text"].endswith("(undone)") and digest["items"][1]["audit_id"] is None
    assert digest["open_approvals"] == 1 and digest["shadow_counts"] == {"inbox-triage": 3}
    blocks = str(result.slack[0]["blocks"])
    assert "undo" in blocks and "trial mode" in blocks


async def test_quiet_day_still_gets_a_short_digest() -> None:
    result = await run_recipe_on_fixture("nightly-digest", None, config=config())
    assert "No actions on my own today" in slack_text(result)
