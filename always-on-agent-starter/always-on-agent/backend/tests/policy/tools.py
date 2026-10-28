"""A registry of test tools shaped like the real ones in policy.yaml, wired to the fakes."""

from typing import Any

from pydantic import BaseModel, Field

from agent.adapters.base import Mode
from agent.policy.gate import GateContext
from agent.policy.modes import load_policy
from agent.policy.registry import Adapters, Inverse, Registry

COMPANY = ["corp.example"]
GROUPS = {"doxis_l2": "doxis-l2@corp.example"}
CALLER = "sophie.laurent@corp.example"
ME = "test.user@corp.example"


class MoveMail(BaseModel):
    message_id: str
    folder: str


class SaveDraft(BaseModel):
    to: list[str]
    subject: str
    body: str
    cc: list[str] = Field(default_factory=list)


class DiscardDraft(BaseModel):
    draft_id: str


class SendMail(BaseModel):
    draft_id: str
    to: list[str]
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    reply_all: bool = False


class PostChat(BaseModel):
    chat_id: str
    body: str
    recipients: list[str] = Field(default_factory=list)


class TriggerRefresh(BaseModel):
    dataset_id: str


class DeleteItem(BaseModel):
    item_id: str


class Note(BaseModel):
    text: str


class Ocr(BaseModel):
    image_ref: str


class Search(BaseModel):
    query: str


async def _folder_before(adapters: Adapters, args: BaseModel) -> dict[str, Any]:
    assert isinstance(args, MoveMail)
    return {
        "message_id": args.message_id,
        "folder": await adapters["outlook"].get_folder(args.message_id),
        "moved_to": args.folder,
    }


async def _still_where_we_left_it(adapters: Adapters, inv: dict[str, Any]) -> str | None:
    now = await adapters["outlook"].get_folder(inv["message_id"])
    return None if now == inv["moved_to"] else f"the message was moved to {now} since"


async def _no_prep(adapters: Adapters, args: BaseModel) -> dict[str, Any]:
    return {}


async def _draft_still_there(adapters: Adapters, inv: dict[str, Any]) -> str | None:
    return None if inv["draft_id"] in adapters["outlook"].drafts else "the draft is already gone"


MOVE_BACK = Inverse(tool="move_mail", prepare=_folder_before, check=_still_where_we_left_it)
DISCARD = Inverse(
    tool="discard_own_draft",
    prepare=_no_prep,
    finish=lambda prep, result: {"draft_id": result},
    check=_draft_still_there,
)


def _refresh_mode(args: BaseModel, ctx: GateContext) -> Mode:
    assert isinstance(args, TriggerRefresh)
    own = args.dataset_id in ctx.facts.get("owned_datasets", ())
    transient = ctx.facts.get("last_error") == "transient"
    return "report" if own and transient else "ask"


ledger_notes: list[str] = []


async def _note(args: BaseModel, key: str) -> str:
    assert isinstance(args, Note)
    ledger_notes.append(args.text)
    return f"note-{len(ledger_notes)}"


def build() -> Registry:
    reg = Registry(load_policy())
    reg.tool("search_mail", mode="silent", handler=_search)(Search)
    reg.tool("move_mail", mode="report", adapter="outlook", method="move", inverse=MOVE_BACK)(MoveMail)
    reg.tool("save_draft", mode="report", adapter="outlook", method="create_draft", inverse=DISCARD)(
        SaveDraft
    )
    reg.tool("discard_own_draft", mode="report", adapter="outlook", method="discard_draft")(DiscardDraft)
    reg.tool(
        "send_mail",
        mode="ask",
        adapter="outlook",
        method="send",
        lookup="find_sent",
        recipients=lambda a: [*a.to, *a.cc, *a.bcc],  # type: ignore[attr-defined]
    )(SendMail)
    reg.tool("post_chat", mode="ask", adapter="teams", method="post", lookup="find_tagged")(PostChat)
    reg.tool(
        "trigger_refresh", mode="ask", adapter="powerbi", method="trigger_refresh", mode_fn=_refresh_mode
    )(TriggerRefresh)
    reg.tool("delete_item", mode="hand_off")(DeleteItem)
    reg.tool("update_ledger", mode="report", handler=_note)(Note)
    reg.tool("ocr_local", mode="silent", handler=_search_ocr, data_level="images")(Ocr)
    return reg


async def _search(args: BaseModel, key: str) -> list[str]:
    return ["hit"]


async def _search_ocr(args: BaseModel, key: str) -> str:
    return "text"
