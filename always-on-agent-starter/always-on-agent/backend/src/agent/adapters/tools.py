"""Tool registrations for the Microsoft 365 and Power BI adapters (A01-A03, A05; F06, F13).

Each tool names its args model, mode (policy.yaml has the final say), the adapter method it calls
and, for Report tools, the inverse undo runs. Inverses check the item is still as the agent left it
and refuse otherwise ("the message was moved to Archive since").

Undo tools never use the words delete/remove in their names: those names are always Hand off.
discard_own_draft and discard_own_hold only ever remove an unsent draft or a hold the agent itself
created with nobody invited; the adapters refuse anything else.

register_all(registry, adapters) is called once at start-up; `adapters` maps connector names
("outlook", "calendar", "teams", "powerbi") to the live adapters (real or fake).
"""

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.adapters.base import Mode, RefreshSchedule
from agent.policy.gate import GateContext
from agent.policy.registry import Adapters, Inverse, Registry

# Power BI error codes that are worth one retry on the user's own dataset (R05). Anything else alerts.
TRANSIENT_REFRESH_ERRORS = frozenset(
    {
        "DM_GWPipeline_Gateway_TimeoutError",
        "DM_GWPipeline_Client_GatewayUnreachable",
        "DM_GWPipeline_Gateway_MashupDataAccessError",
        "GatewayNotReachable",
        "ModelRefresh_ShortMessage_ProcessingError",
        "ModelRefreshFailed_CredentialsNotSpecified_Transient",
        "RequestTimeout",
        "ServiceUnavailable",
        "TooManyRequests",
    }
)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- mail (A01)


class ReadMail(_Args):
    message_id: str = Field(min_length=1)


class SearchMail(_Args):
    query: str = Field(min_length=1, max_length=200)
    top: int = Field(default=25, ge=1, le=50)


class MoveMail(_Args):
    model_config = ConfigDict(extra="ignore")  # undo passes moved_to along for its check

    message_id: str = Field(min_length=1)
    folder: str = Field(min_length=1)

    @field_validator("folder")
    @classmethod
    def _not_deleted_items(cls, value: str) -> str:
        if value.strip().lower().replace(" ", "") in ("deleteditems", "trash", "bin"):
            raise ValueError("moving to Deleted Items is deleting; that is a hand-off")
        return value


class FlagMail(_Args):
    message_id: str = Field(min_length=1)


class SetFlag(_Args):
    message_id: str = Field(min_length=1)
    flagged: bool


class Categorise(_Args):
    model_config = ConfigDict(extra="ignore")  # undo passes _set along for its check

    message_id: str = Field(min_length=1)
    categories: list[str] = Field(max_length=10)


class SaveDraft(_Args):
    to: list[str] = Field(min_length=1)
    subject: str
    body: str = Field(min_length=1)
    cc: list[str] = Field(default_factory=list)
    reply_to: str | None = None  # message id this replies to


class DiscardOwnDraft(_Args):
    draft_id: str = Field(min_length=1)


class SendMail(_Args):
    draft_id: str = Field(min_length=1)
    to: list[str] = Field(min_length=1)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    reply_all: bool = False
    # The text the approval card shows. An edit on the card changes it; send rebuilds the draft with it.
    body: str = ""
    drafted_on: Literal["haiku", "sonnet", "opus"] | None = None  # shown on the card; not sent


async def _move_prepare(adapters: Adapters, args: BaseModel) -> dict[str, Any]:
    assert isinstance(args, MoveMail)
    return {
        "message_id": args.message_id,
        "folder": await adapters["outlook"].get_folder(args.message_id),
        "moved_to": args.folder,
    }


def _move_finish(prep: dict[str, Any], result: Any) -> dict[str, Any]:
    # With immutable ids the id stays the same; a COM move may hand back a new one.
    return {
        "message_id": str(result or prep["message_id"]),
        "folder": prep["folder"],
        "moved_to": prep["moved_to"],
    }


async def _move_check(adapters: Adapters, inv: dict[str, Any]) -> str | None:
    now = await adapters["outlook"].get_folder(inv["message_id"])
    expected = str(inv["moved_to"]).strip("/").lower()
    return None if now.strip("/").lower() == expected else f"the message was moved to {now} since"


async def _flag_prepare(adapters: Adapters, args: BaseModel) -> dict[str, Any]:
    assert isinstance(args, FlagMail)
    return {"message_id": args.message_id, "flagged": await adapters["outlook"].is_flagged(args.message_id)}


async def _flag_check(adapters: Adapters, inv: dict[str, Any]) -> str | None:
    return None if await adapters["outlook"].is_flagged(inv["message_id"]) else "the flag was cleared since"


async def _categories_prepare(adapters: Adapters, args: BaseModel) -> dict[str, Any]:
    assert isinstance(args, Categorise)
    return {
        "message_id": args.message_id,
        "categories": await adapters["outlook"].get_categories(args.message_id),
        "_set": args.categories,
    }


def _categories_finish(prep: dict[str, Any], result: Any) -> dict[str, Any]:
    return prep


async def _categories_check(adapters: Adapters, inv: dict[str, Any]) -> str | None:
    now = await adapters["outlook"].get_categories(inv["message_id"])
    return None if sorted(now) == sorted(inv.get("_set", [])) else "the categories were changed since"


async def _no_prep(adapters: Adapters, args: BaseModel) -> dict[str, Any]:
    return {}


MOVE_BACK = Inverse(tool="move_mail", prepare=_move_prepare, finish=_move_finish, check=_move_check)
RESTORE_FLAG = Inverse(tool="set_flag", prepare=_flag_prepare, check=_flag_check)
RESTORE_CATEGORIES = Inverse(
    tool="categorise", prepare=_categories_prepare, finish=_categories_finish, check=_categories_check
)
DISCARD_DRAFT = Inverse(
    tool="discard_own_draft", prepare=_no_prep, finish=lambda prep, result: {"draft_id": result}
)


# ---------------------------------------------------------------- calendar (A02)


class ReadCalendar(_Args):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _order(self) -> "ReadCalendar":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class AddHold(_Args):
    """Only these fields exist: a hold can never invite anyone (own calendar only)."""

    subject: str = Field(min_length=1, max_length=255)
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _order(self) -> "AddHold":
        if self.end <= self.start:
            raise ValueError("a hold must end after it starts")
        return self


class DiscardOwnHold(_Args):
    event_id: str = Field(min_length=1)


async def _hold_check(adapters: Adapters, inv: dict[str, Any]) -> str | None:
    event = await adapters["calendar"].get_event(inv["event_id"])
    if event is None:
        return "the hold is already gone"
    if event.attendees:
        return "people were invited to it since"
    return None


DISCARD_HOLD = Inverse(
    tool="discard_own_hold",
    prepare=_no_prep,
    finish=lambda prep, result: {"event_id": result},
    check=_hold_check,
)


# ---------------------------------------------------------------- Teams (A03)


class ReadChat(_Args):
    since: AwareDatetime
    chat_id: str | None = None


class PostChat(_Args):
    chat_id: str = Field(min_length=1)
    body: str = Field(min_length=1, max_length=20000)
    # Every member of the chat other than the user: the gate checks each one (outside -> Hand off)
    # and the adapter refuses if the chat's real members are not all listed here.
    recipients: list[str] = Field(min_length=1)


class GetTranscript(_Args):
    meeting_id: str = Field(min_length=1)


# ---------------------------------------------------------------- Power BI (A05)


class GetRefreshHistory(_Args):
    dataset_id: str = Field(min_length=1)
    top: int = Field(default=60, ge=1, le=60)


class TriggerRefresh(_Args):
    dataset_id: str = Field(min_length=1)


class TriggerDataflowRefresh(_Args):
    dataflow_id: str = Field(min_length=1)


class UpdateRefreshSchedule(_Args):
    dataset_id: str = Field(min_length=1)
    schedule: RefreshSchedule


def refresh_mode(dataset_key: str) -> Any:
    """Ask by default; Report for exactly one retry of the user's own dataset after a transient error.

    Facts the recipe passes in GateContext.facts: owned_datasets (ids), last_error_code, and
    agent_retries_today (refreshes the agent already started today for this item).
    """

    def mode_fn(args: BaseModel, ctx: GateContext) -> Mode:
        item = str(getattr(args, dataset_key))
        own = item in set(ctx.facts.get("owned_datasets", ()))
        transient = ctx.facts.get("last_error_code") in TRANSIENT_REFRESH_ERRORS
        first_retry = int(ctx.facts.get("agent_retries_today", 0) or 0) == 0
        return "report" if own and transient and first_retry else "ask"

    return mode_fn


# ---------------------------------------------------------------- handlers that filter


def _calendar_reader(adapters: Mapping[str, Any]) -> Any:
    async def read(args: BaseModel, key: str) -> list[dict[str, Any]]:
        assert isinstance(args, ReadCalendar)
        events = await adapters["calendar"].events_between(args.start, args.end)
        # Private events are skipped entirely; cancelled ones are not meetings any more.
        return [e.model_dump(mode="json") for e in events if not e.private and not e.cancelled]

    return read


def _chat_reader(adapters: Mapping[str, Any]) -> Any:
    async def read(args: BaseModel, key: str) -> list[dict[str, Any]]:
        assert isinstance(args, ReadChat)
        messages = await adapters["teams"].list_chat_messages(args.since)
        return [
            m.model_dump(mode="json")
            for m in messages
            if (args.chat_id is None or m.chat_id == args.chat_id) and not m.deleted
        ]

    return read


def _transcript_reader(adapters: Mapping[str, Any]) -> Any:
    async def read(args: BaseModel, key: str) -> dict[str, Any]:
        assert isinstance(args, GetTranscript)
        result = await adapters["teams"].transcript(args.meeting_id)
        return dict(result.model_dump(mode="json"))

    return read


# ---------------------------------------------------------------- registration


def register_mail(reg: Registry) -> None:
    reg.tool("read_mail", mode="silent", adapter="outlook", method="get_message")(ReadMail)
    reg.tool("search_mail", mode="silent", adapter="outlook", method="search")(SearchMail)
    reg.tool("move_mail", mode="report", adapter="outlook", method="move", inverse=MOVE_BACK)(MoveMail)
    reg.tool("flag_mail", mode="report", adapter="outlook", method="flag", inverse=RESTORE_FLAG)(FlagMail)
    reg.tool("set_flag", mode="report", adapter="outlook", method="set_flag")(SetFlag)
    reg.tool("categorise", mode="report", adapter="outlook", method="categorise", inverse=RESTORE_CATEGORIES)(
        Categorise
    )
    reg.tool("save_draft", mode="report", adapter="outlook", method="create_draft", inverse=DISCARD_DRAFT)(
        SaveDraft
    )
    reg.tool("discard_own_draft", mode="report", adapter="outlook", method="discard_draft")(DiscardOwnDraft)
    reg.tool(
        "send_mail",
        mode="ask",
        adapter="outlook",
        method="send",
        lookup="find_sent",
        recipients=lambda a: [*a.to, *a.cc, *a.bcc],  # type: ignore[attr-defined]
        content_field="body",
        description="Send this email",
    )(SendMail)


def register_calendar(reg: Registry, adapters: Mapping[str, Any]) -> None:
    reg.tool("read_calendar", mode="silent", handler=_calendar_reader(adapters))(ReadCalendar)
    reg.tool("add_hold", mode="report", adapter="calendar", method="add_hold", inverse=DISCARD_HOLD)(AddHold)
    reg.tool("discard_own_hold", mode="report", adapter="calendar", method="discard_hold")(DiscardOwnHold)


def register_teams(reg: Registry, adapters: Mapping[str, Any]) -> None:
    reg.tool("read_chat", mode="silent", handler=_chat_reader(adapters))(ReadChat)
    reg.tool(
        "post_chat",
        mode="ask",
        adapter="teams",
        method="post",
        lookup="find_tagged",
        recipients=lambda a: list(a.recipients),  # type: ignore[attr-defined]
        content_field="body",
        description="Post this in the Teams chat",
    )(PostChat)
    reg.tool("get_transcript", mode="silent", handler=_transcript_reader(adapters), data_level="chat")(
        GetTranscript
    )


def register_powerbi(reg: Registry) -> None:
    reg.tool("get_refresh_history", mode="silent", adapter="powerbi", method="refresh_history")(
        GetRefreshHistory
    )
    reg.tool(
        "trigger_refresh",
        mode="ask",
        adapter="powerbi",
        method="trigger_refresh",
        mode_fn=refresh_mode("dataset_id"),
        description="Refresh this dataset",
    )(TriggerRefresh)
    reg.tool(
        "trigger_dataflow_refresh",
        mode="ask",
        adapter="powerbi",
        method="trigger_dataflow_refresh",
        mode_fn=refresh_mode("dataflow_id"),
        description="Refresh this dataflow",
    )(TriggerDataflowRefresh)
    reg.tool(
        "update_refresh_schedule",
        mode="ask",
        adapter="powerbi",
        method="update_refresh_schedule",
        description="Change this dataset's refresh schedule",
    )(UpdateRefreshSchedule)


def register_all(reg: Registry, adapters: Mapping[str, Any]) -> Registry:
    register_mail(reg)
    register_calendar(reg, adapters)
    register_teams(reg, adapters)
    register_powerbi(reg)
    return reg
