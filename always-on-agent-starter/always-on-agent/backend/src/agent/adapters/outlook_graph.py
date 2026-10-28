"""Outlook mail through Microsoft Graph (A01).

Reads
- Delta queries on Inbox and Sent Items. The delta link is kept in kv and only replaced after the
  poller has ingested every item (commit_delta), so a crash mid-poll re-reads instead of losing mail.
- An expired token (410 / SyncStateNotFound) drops the link and resyncs the last resync_days days;
  the coordinator dedupes on the message id, so nothing is processed twice (E26).
- Immutable ids (Prefer: IdType="ImmutableId"): a moved message keeps its id, so task links, undo
  and the outbox still find it after a move.
- HTML bodies become text; images are dropped; attachments are listed as metadata only.
- Mail marked private or confidential, labelled with a sensitivity label outside mail.open_labels,
  encrypted (message.rpmsg) or S/MIME is metadata only: the body is blanked here, so it can never
  reach the model (A01). If the label cannot be read, the mail is treated as labelled.

Writes (only through the policy gate and outbox)
- Drafts the agent saves carry an AgentDraft property and a digest of their body. send() refuses a
  draft whose recipients differ from the approved ones or whose body changed after it was saved
  (someone edited it in Outlook): the approval covered the old text (golden rule 2).
- send() stamps X-Agent-Idempotency as a named property in the internet-headers namespace, so it
  goes out as a header and can be found in Sent Items (find_sent) after a crash (E18).
- discard_draft() only ever deletes an unsent draft the agent created.
"""

import base64
import hashlib
import html
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from agent.adapters.base import (
    Attachment,
    AttachmentInfo,
    HealthStatus,
    MailChanges,
    MailMessage,
    Person,
    ToolSpec,
)
from agent.adapters.graph_client import DeltaExpired, GraphClient, NotFound
from agent.adapters.html_text import body_text
from agent.core.errors import AdapterError, AgentError

log = logging.getLogger(__name__)

PS_INTERNET_HEADERS = "{00020386-0000-0000-C000-000000000046}"
PS_PUBLIC_STRINGS = "{00020329-0000-0000-C000-000000000046}"
IDEMPOTENCY_PROP = f"String {PS_INTERNET_HEADERS} Name X-Agent-Idempotency"
LABEL_PROP = f"String {PS_INTERNET_HEADERS} Name msip_labels"
AGENT_DRAFT_PROP = f"String {PS_PUBLIC_STRINGS} Name AgentDraft"
BODY_DIGEST_PROP = f"String {PS_PUBLIC_STRINGS} Name AgentBodyDigest"  # the whole saved body
TEXT_DIGEST_PROP = f"String {PS_PUBLIC_STRINGS} Name AgentTextDigest"  # the agent's own text
REPLY_TO_PROP = f"String {PS_PUBLIC_STRINGS} Name AgentReplyTo"
IMMUTABLE = {"Prefer": 'IdType="ImmutableId"'}
DELTA_KEY = "delta.mail.{folder}"
FOLDERS = {"inbox": "Inbox", "sentitems": "SentItems"}
PROTECTED_ATTACHMENTS = ("message.rpmsg", "smime.p7m")
PROTECTED_TYPES = ("application/pkcs7-mime", "application/x-pkcs7-mime")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
SELECT = ",".join(
    [
        "id",
        "conversationId",
        "subject",
        "from",
        "toRecipients",
        "ccRecipients",
        "bccRecipients",
        "receivedDateTime",
        "body",
        "hasAttachments",
        "categories",
        "importance",
        "flag",
        "isDraft",
        "parentFolderId",
        "sensitivity",
    ]
)


class Kv(Protocol):
    """KvRepo has this shape."""

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str) -> None: ...
    async def delete(self, key: str) -> None: ...


def digest_text(text: str) -> str:
    """Digest of a body as text, whitespace-normalised (Graph re-wraps HTML on save)."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def _person(raw: dict[str, Any] | None) -> Person:
    email = (raw or {}).get("emailAddress") or {}
    return Person(name=str(email.get("name") or ""), address=str(email.get("address") or "").lower())


def _addresses(raw: list[dict[str, Any]] | None) -> list[str]:
    return sorted(_person(r).address for r in raw or [] if _person(r).address)


def _recipients(addresses: list[str]) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": a}} for a in addresses]


def _odata_quote(value: str) -> str:
    return value.replace("'", "''")


def parse_graph_time(value: str) -> datetime:
    """Graph times: '2026-03-25T03:00:00Z', with 7 fractional digits, or naive (then UTC)."""
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, _, rest = text.partition(".")
        digits = "".join(c for c in rest if c.isdigit())
        tail = rest[len(digits) :]
        text = f"{head}.{digits[:6]}{tail}"
    dt = datetime.fromisoformat(text)
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def label_names(value: str | None) -> list[str]:
    """Names from an msip_labels header: 'MSIP_Label_<guid>_Name=Confidential;...'."""
    if not value:
        return []
    out = []
    for part in value.split(";"):
        key, _, val = part.strip().partition("=")
        if key.endswith("_Name") and val:
            out.append(val.strip())
    return out


class OutlookGraphAdapter:
    """Implements MailAdapter over Graph for the signed-in user's own mailbox only."""

    name = "outlook"

    def __init__(
        self,
        client: GraphClient,
        kv: Kv,
        me: list[str],
        now: Callable[[], datetime],
        resync_days: int = 7,
        open_labels: list[str] | None = None,
    ) -> None:
        self._g = client
        self._kv = kv
        self._me = {m.lower() for m in me}
        self._now = now
        self._resync_days = resync_days
        self._open_labels = {label.lower() for label in (open_labels or ["Public", "General"])}
        self._folder_ids: dict[str, str] = {}  # well-known name -> id
        self._folder_names: dict[str, str] = {}  # id -> display path

    # ------------------------------------------------------------ Adapter

    async def health(self) -> HealthStatus:
        try:
            await self._g.get("me/mailFolders/inbox", {"$select": "id,totalItemCount"}, headers=IMMUTABLE)
        except AgentError as exc:
            return HealthStatus(connector=self.name, status="down", detail=str(exc)[:200])
        return HealthStatus(connector=self.name, status="ok", detail="Graph mail reachable")

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="read_mail", mode="silent", description="Read mail"),
            ToolSpec(name="search_mail", mode="silent", description="Search mail"),
            ToolSpec(name="move_mail", mode="report", description="Move a message to a folder"),
            ToolSpec(name="categorise", mode="report", description="Set categories"),
            ToolSpec(name="flag_mail", mode="report", description="Flag a message"),
            ToolSpec(name="save_draft", mode="report", description="Save a draft"),
            ToolSpec(name="send_mail", mode="ask", description="Send a draft"),
        ]

    # ------------------------------------------------------------ delta (pollers)

    async def changes(self, folder: str) -> MailChanges:
        """New and changed messages in inbox or sentitems since the stored delta link."""
        if folder not in FOLDERS:
            raise ValueError(f"delta is only kept for {sorted(FOLDERS)}")
        stored = await self._kv.get(DELTA_KEY.format(folder=folder))
        resynced = False
        try:
            raw, link = await self._delta(folder, stored)
        except DeltaExpired:
            log.warning("mail delta token expired; resyncing", extra={"folder": folder})
            await self._kv.delete(DELTA_KEY.format(folder=folder))
            raw, link = await self._delta(folder, None)
            resynced = True
        messages = []
        for item in raw:
            if "@removed" in item:
                continue  # deleted or moved out: nothing to act on
            messages.append(await self._to_mail(item, FOLDERS[folder]))
        messages.sort(key=lambda m: m.received_at)
        return MailChanges(folder=folder, messages=messages, commit_token=link, resynced=resynced)

    async def commit_delta(self, changes: MailChanges) -> None:
        if changes.commit_token:
            await self._kv.set(DELTA_KEY.format(folder=changes.folder), changes.commit_token)

    async def _delta(self, folder: str, link: str | None) -> tuple[list[dict[str, Any]], str | None]:
        if link:
            path, params = link, None
        else:
            since = self._now() - timedelta(days=self._resync_days)
            path = f"me/mailFolders/{folder}/messages/delta"
            params = {"$select": SELECT, "$filter": f"receivedDateTime ge {_iso(since)}"}
        items: list[dict[str, Any]] = []
        delta_link = None
        async for page in self._g.pages(path, params, IMMUTABLE):
            items.extend(page.get("value", []))
            delta_link = page.get("@odata.deltaLink") or delta_link
        return items, delta_link

    # ------------------------------------------------------------ reads

    async def list_new(self, since: datetime) -> list[MailMessage]:
        raw = await self._g.get_all(
            "me/mailFolders/inbox/messages",
            {
                "$select": SELECT,
                "$filter": f"receivedDateTime gt {_iso(since)}",
                "$orderby": "receivedDateTime asc",
            },
            headers=IMMUTABLE,
        )
        return [await self._to_mail(item, "Inbox") for item in raw]

    async def get_message(self, message_id: str) -> MailMessage:
        item = await self._g.get(f"me/messages/{message_id}", {"$select": SELECT}, headers=IMMUTABLE)
        return await self._to_mail(item, await self._folder_name(str(item.get("parentFolderId") or "")))

    async def get_thread(self, conversation_id: str) -> list[MailMessage]:
        raw = await self._g.get_all(
            "me/messages",
            {"$select": SELECT, "$filter": f"conversationId eq '{_odata_quote(conversation_id)}'"},
            max_items=200,
            headers=IMMUTABLE,
        )
        out = [
            await self._to_mail(i, await self._folder_name(str(i.get("parentFolderId") or ""))) for i in raw
        ]
        return sorted((m for m in out if not m.is_draft), key=lambda m: m.received_at)

    async def search(self, query: str, top: int = 25) -> list[MailMessage]:
        cleaned = query.replace('"', " ").replace("\\", " ").strip()
        if not cleaned:
            return []
        raw = await self._g.get_all(
            "me/messages",
            {"$select": SELECT, "$search": f'"{cleaned}"', "$top": str(min(top, 50))},
            max_items=top,
            headers=IMMUTABLE,
        )
        return [
            await self._to_mail(i, await self._folder_name(str(i.get("parentFolderId") or ""))) for i in raw
        ]

    async def get_attachments(self, message_id: str) -> list[Attachment]:
        """File attachments with content (ServiceNow reports); inline images and protected mail skipped."""
        message = await self.get_message(message_id)
        if message.metadata_only:
            raise AdapterError("this message is protected; its attachments stay in Outlook")
        raw = await self._g.get_all(f"me/messages/{message_id}/attachments", headers=IMMUTABLE)
        out = []
        for att in raw:
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment" or att.get("isInline"):
                continue
            if int(att.get("size") or 0) > MAX_ATTACHMENT_BYTES:
                log.info("attachment too large, skipped", extra={"size": att.get("size")})
                continue
            out.append(
                Attachment(
                    name=str(att.get("name") or ""),
                    content_type=str(att.get("contentType") or "application/octet-stream"),
                    data=base64.b64decode(att.get("contentBytes") or ""),
                )
            )
        return out

    async def get_folder(self, message_id: str) -> str:
        item = await self._g.get(
            f"me/messages/{message_id}", {"$select": "parentFolderId"}, headers=IMMUTABLE
        )
        return await self._folder_name(str(item.get("parentFolderId") or ""))

    # ------------------------------------------------------------ writes (via the outbox only)

    async def move(self, message_id: str, folder: str) -> str:
        destination = await self._folder_id(folder)
        moved = await self._g.post(
            f"me/messages/{message_id}/move", {"destinationId": destination}, headers=IMMUTABLE
        )
        return str(moved.get("id") or message_id)

    async def categorise(self, message_id: str, categories: list[str]) -> None:
        await self._g.patch(f"me/messages/{message_id}", {"categories": categories}, headers=IMMUTABLE)

    async def get_categories(self, message_id: str) -> list[str]:
        item = await self._g.get(f"me/messages/{message_id}", {"$select": "categories"}, headers=IMMUTABLE)
        return list(item.get("categories") or [])

    async def flag(self, message_id: str) -> None:
        await self._set_flag(message_id, True)

    async def unflag(self, message_id: str) -> None:
        await self._set_flag(message_id, False)

    async def is_flagged(self, message_id: str) -> bool:
        item = await self._g.get(f"me/messages/{message_id}", {"$select": "flag"}, headers=IMMUTABLE)
        return (item.get("flag") or {}).get("flagStatus") == "flagged"

    async def set_flag(self, message_id: str, flagged: bool) -> None:
        await self._set_flag(message_id, flagged)

    async def _set_flag(self, message_id: str, flagged: bool) -> None:
        status = "flagged" if flagged else "notFlagged"
        await self._g.patch(f"me/messages/{message_id}", {"flag": {"flagStatus": status}}, headers=IMMUTABLE)

    async def create_draft(
        self, to: list[str], subject: str, body: str, cc: list[str] | None = None, reply_to: str | None = None
    ) -> str:
        marker = {"id": AGENT_DRAFT_PROP, "value": "1"}
        extra = [
            {"id": TEXT_DIGEST_PROP, "value": digest_text(body)},
            {"id": REPLY_TO_PROP, "value": reply_to or ""},
        ]
        if reply_to:
            draft = await self._g.post(f"me/messages/{reply_to}/createReply", {}, headers=IMMUTABLE)
            quoted = str((draft.get("body") or {}).get("content") or "")
            content = _text_as_html(body) + quoted
            draft_id = str(draft["id"])
            await self._g.patch(
                f"me/messages/{draft_id}",
                {
                    "body": {"contentType": "html", "content": content},
                    "toRecipients": _recipients(to),
                    "ccRecipients": _recipients(cc or []),
                    "singleValueExtendedProperties": [marker],
                },
                headers=IMMUTABLE,
            )
        else:
            draft = await self._g.post(
                "me/messages",
                {
                    "subject": subject,
                    "body": {"contentType": "text", "content": body},
                    "toRecipients": _recipients(to),
                    "ccRecipients": _recipients(cc or []),
                    "singleValueExtendedProperties": [marker],
                },
                headers=IMMUTABLE,
            )
            draft_id = str(draft["id"])
        # Re-read what Outlook stored and remember its digest: send() refuses if it changes later.
        saved = await self._g.get(f"me/messages/{draft_id}", {"$select": "body"}, headers=IMMUTABLE)
        stamp = {"id": BODY_DIGEST_PROP, "value": digest_text(body_text(saved.get("body")))}
        await self._g.patch(
            f"me/messages/{draft_id}", {"singleValueExtendedProperties": [stamp, *extra]}, headers=IMMUTABLE
        )
        return draft_id

    async def discard_draft(self, draft_id: str) -> None:
        """The save_draft inverse. Deletes only an unsent draft the agent created."""
        try:
            draft = await self._draft(draft_id)
        except NotFound:
            return  # already gone: nothing to undo
        if not draft.get("isDraft"):
            raise AdapterError("this message was sent; it cannot be discarded")
        if _prop(draft, AGENT_DRAFT_PROP) != "1":
            raise AdapterError("this draft was not created by the agent; it is left alone")
        await self._g.delete(f"me/messages/{draft_id}", headers=IMMUTABLE)

    async def send(
        self,
        draft_id: str,
        idempotency_key: str,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body: str | None = None,
    ) -> str:
        """Send a saved draft exactly as approved.

        - recipients must equal the approved To/Cc(/Bcc), else refused;
        - a draft edited in Outlook since it was saved is refused (the approval covered the old text);
        - when the approver edited the text (body differs from the agent's saved text), the draft is
          rebuilt with the approved text, the old agent draft discarded, and the new one sent.
        """
        try:
            draft = await self._draft(draft_id)
        except NotFound:
            sent = await self.find_sent(idempotency_key)
            if sent is not None:
                return sent
            raise AdapterError("the draft no longer exists and nothing was sent; make a new draft") from None
        if not draft.get("isDraft"):
            sent = await self.find_sent(idempotency_key)
            if sent is not None:
                return sent
            raise AdapterError("this message is not a draft; nothing was sent")
        if to is not None:
            approved = {"to": sorted(a.lower() for a in to), "cc": sorted(a.lower() for a in cc or [])}
            actual = {
                "to": _addresses(draft.get("toRecipients")),
                "cc": _addresses(draft.get("ccRecipients")),
            }
            if bcc is not None:
                approved["bcc"] = sorted(a.lower() for a in bcc)
                actual["bcc"] = _addresses(draft.get("bccRecipients"))
            if approved != actual:
                raise AdapterError(
                    "the draft's recipients changed in Outlook after approval; approve it again"
                )
        expected = _prop(draft, BODY_DIGEST_PROP)
        if expected and digest_text(body_text(draft.get("body"))) != expected:
            raise AdapterError("the draft was edited in Outlook after approval; approve the new text")
        if body and _prop(draft, TEXT_DIGEST_PROP) not in (None, digest_text(body)):
            if _prop(draft, AGENT_DRAFT_PROP) != "1":
                raise AdapterError("the approved text differs from this draft; make a new draft")
            rebuilt = await self.create_draft(
                to or _addresses(draft.get("toRecipients")),
                str(draft.get("subject") or ""),
                body,
                cc if to is not None else _addresses(draft.get("ccRecipients")),
                _prop(draft, REPLY_TO_PROP) or None,
            )
            await self.discard_draft(draft_id)
            draft_id = rebuilt
        await self._g.patch(
            f"me/messages/{draft_id}",
            {"singleValueExtendedProperties": [{"id": IDEMPOTENCY_PROP, "value": idempotency_key}]},
            headers=IMMUTABLE,
        )
        await self._g.post(f"me/messages/{draft_id}/send", None, headers=IMMUTABLE)
        return draft_id

    async def find_sent(self, idempotency_key: str) -> str | None:
        """Sent Items (and the Outbox, while it is still leaving) by the idempotency header."""
        flt = (
            f"singleValueExtendedProperties/Any(ep: ep/id eq '{IDEMPOTENCY_PROP}' "
            f"and ep/value eq '{_odata_quote(idempotency_key)}')"
        )
        for folder in ("sentitems", "outbox"):
            found = await self._g.get(
                f"me/mailFolders/{folder}/messages",
                {"$filter": flt, "$select": "id", "$top": "1"},
                headers=IMMUTABLE,
            )
            items = found.get("value") or []
            if items:
                return str(items[0]["id"])
        return None

    # ------------------------------------------------------------ helpers

    async def _draft(self, draft_id: str) -> dict[str, Any]:
        expand = (
            "singleValueExtendedProperties($filter="
            + " or ".join(
                f"id eq '{p}'" for p in (AGENT_DRAFT_PROP, BODY_DIGEST_PROP, TEXT_DIGEST_PROP, REPLY_TO_PROP)
            )
            + ")"
        )
        return await self._g.get(
            f"me/messages/{draft_id}",
            {
                "$select": "id,isDraft,subject,body,toRecipients,ccRecipients,bccRecipients",
                "$expand": expand,
            },
            headers=IMMUTABLE,
        )

    async def _to_mail(self, raw: dict[str, Any], folder: str) -> MailMessage:
        attachments: list[AttachmentInfo] = []
        if raw.get("hasAttachments"):
            for att in await self._g.get_all(
                f"me/messages/{raw['id']}/attachments",
                {"$select": "name,contentType,size,isInline"},
                headers=IMMUTABLE,
            ):
                attachments.append(
                    AttachmentInfo(
                        name=str(att.get("name") or ""),
                        content_type=str(att.get("contentType") or ""),
                        size=int(att.get("size") or 0),
                        inline=bool(att.get("isInline")),
                    )
                )
        sensitivity = str(raw.get("sensitivity") or "normal")
        protected = sensitivity in ("private", "confidential") or any(
            a.name.lower() in PROTECTED_ATTACHMENTS or a.content_type.lower() in PROTECTED_TYPES
            for a in attachments
        )
        if not protected:
            protected = await self._labelled(str(raw["id"]))
        sender = _person(raw.get("from"))
        return MailMessage(
            id=str(raw["id"]),
            conversation_id=str(raw.get("conversationId") or raw["id"]),
            subject=str(raw.get("subject") or ""),
            sender=sender,
            to=[_person(p) for p in raw.get("toRecipients") or []],
            cc=[_person(p) for p in raw.get("ccRecipients") or []],
            received_at=parse_graph_time(str(raw.get("receivedDateTime") or _iso(self._now()))),
            body="" if protected else body_text(raw.get("body")),
            has_attachments=bool(raw.get("hasAttachments")),
            categories=list(raw.get("categories") or []),
            importance=str(raw.get("importance") or "normal"),
            folder=folder,
            flagged=(raw.get("flag") or {}).get("flagStatus") == "flagged",
            is_draft=bool(raw.get("isDraft")),
            attachments=[a for a in attachments if not a.inline],
            sensitivity=sensitivity,
            metadata_only=protected,
        )

    async def _labelled(self, message_id: str) -> bool:
        """True when a sensitivity label outside open_labels is set, or when it cannot be checked."""
        try:
            item = await self._g.get(
                f"me/messages/{message_id}",
                {"$select": "id", "$expand": f"singleValueExtendedProperties($filter=id eq '{LABEL_PROP}')"},
                headers=IMMUTABLE,
            )
        except AgentError:
            log.warning("could not read the sensitivity label; treating the mail as protected")
            return True
        names = label_names(_prop(item, LABEL_PROP))
        return any(n.lower() not in self._open_labels for n in names)

    async def _folder_id(self, folder: str) -> str:
        """'Inbox', 'Archive' or a path like 'Inbox/Done' -> folder id."""
        parts = [p for p in folder.split("/") if p]
        if not parts:
            raise AdapterError("no folder given")
        well_known = parts[0].lower().replace(" ", "")
        if well_known in ("inbox", "archive", "sentitems", "deleteditems", "drafts", "junkemail"):
            if well_known == "deleteditems":
                raise AdapterError("moving to Deleted Items is deleting; that is a hand-off")
            current = await self._well_known(well_known)
            rest = parts[1:]
        else:
            current = await self._well_known("msgfolderroot")
            rest = parts
        for part in rest:
            children = await self._g.get_all(
                f"me/mailFolders/{current}/childFolders", {"$select": "id,displayName"}
            )
            match = next((c for c in children if str(c.get("displayName", "")).lower() == part.lower()), None)
            if match is None:
                raise AdapterError(f"Outlook folder not found: {folder}")
            current = str(match["id"])
        return current

    async def _well_known(self, name: str) -> str:
        if name not in self._folder_ids:
            item = await self._g.get(
                f"me/mailFolders/{name}", {"$select": "id,displayName"}, headers=IMMUTABLE
            )
            self._folder_ids[name] = str(item["id"])
            self._folder_names[str(item["id"])] = {"inbox": "Inbox", "sentitems": "SentItems"}.get(
                name, str(item.get("displayName") or name)
            )
        return self._folder_ids[name]

    async def _folder_name(self, folder_id: str) -> str:
        if not folder_id:
            return ""
        if folder_id not in self._folder_names:
            for name in ("inbox", "sentitems", "drafts"):
                if await self._well_known(name) == folder_id:
                    return self._folder_names[folder_id]
            item = await self._g.get(
                f"me/mailFolders/{folder_id}", {"$select": "displayName"}, headers=IMMUTABLE
            )
            self._folder_names[folder_id] = str(item.get("displayName") or folder_id)
        return self._folder_names[folder_id]


def _prop(item: dict[str, Any], prop_id: str) -> str | None:
    for prop in item.get("singleValueExtendedProperties") or []:
        if str(prop.get("id", "")).lower() == prop_id.lower():
            return str(prop.get("value"))
    return None


def _text_as_html(text: str) -> str:
    return "<div>" + html.escape(text).replace("\n", "<br>") + "</div><br>"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
