"""Small stateful Graph and Power BI simulators on respx, for the real adapters' tests.

They implement just the endpoints the adapters call, with the behaviour that matters to them:
drafts, extended properties, send, Sent Items filters, calendar events with transactionId, chats
with members and messages, Power BI datasets and refreshes. Everything is made-up data.
"""

import json
import re
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import httpx
import respx

from agent.adapters.graph_client import GRAPH_BASE, POWERBI_BASE


def _json(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content or b"{}")


def _query(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


def _props(item: dict[str, Any]) -> list[dict[str, str]]:
    return [{"id": k, "value": v} for k, v in item.get("props", {}).items()]


class MailSim:
    FOLDERS: ClassVar[dict[str, str]] = {
        "inbox": "Inbox",
        "sentitems": "Sent Items",
        "drafts": "Drafts",
        "outbox": "Outbox",
        "msgfolderroot": "Top",
    }

    def __init__(self, router: respx.MockRouter) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.sends: list[str] = []
        self.attachments: dict[str, list[dict[str, Any]]] = {}
        self.children: dict[str, list[dict[str, str]]] = {
            "folder-inbox": [{"id": "folder-done", "displayName": "Done"}]
        }
        self._n = 0
        base = re.escape(GRAPH_BASE)
        router.get(url__regex=rf"{base}/me/mailFolders/(?P<f>[^/?]+)$").mock(side_effect=self._folder)
        router.get(url__regex=rf"{base}/me/mailFolders/(?P<f>[^/?]+)\?").mock(side_effect=self._folder)
        router.get(url__regex=rf"{base}/me/mailFolders/(?P<f>[^/]+)/childFolders").mock(
            side_effect=self._children
        )
        router.get(url__regex=rf"{base}/me/mailFolders/(?P<f>[^/]+)/messages(\?|$)").mock(
            side_effect=self._list
        )
        router.get(url__regex=rf"{base}/me/messages/(?P<m>[^/?]+)/attachments").mock(
            side_effect=lambda request, m: httpx.Response(200, json={"value": self.attachments.get(m, [])})
        )
        router.get(url__regex=rf"{base}/me/messages/(?P<m>[^/?]+)(\?|$)").mock(side_effect=self._get)
        router.patch(url__regex=rf"{base}/me/messages/(?P<m>[^/?]+)$").mock(side_effect=self._patch)
        router.delete(url__regex=rf"{base}/me/messages/(?P<m>[^/?]+)$").mock(side_effect=self._delete)
        router.post(url__regex=rf"{base}/me/messages$").mock(side_effect=self._create)
        router.post(url__regex=rf"{base}/me/messages/(?P<m>[^/]+)/createReply$").mock(side_effect=self._reply)
        router.post(url__regex=rf"{base}/me/messages/(?P<m>[^/]+)/send$").mock(side_effect=self._send)
        router.post(url__regex=rf"{base}/me/messages/(?P<m>[^/]+)/move$").mock(side_effect=self._move)

    # -------------------------------------------------- test controls

    def add(self, message_id: str, folder: str = "folder-inbox", **fields: Any) -> dict[str, Any]:
        item = {
            "id": message_id,
            "conversationId": f"conv-{message_id}",
            "subject": f"Subject {message_id}",
            "from": {"emailAddress": {"name": "Sophie Laurent", "address": "sophie.laurent@corp.example"}},
            "toRecipients": [{"emailAddress": {"address": "test.user@corp.example"}}],
            "ccRecipients": [],
            "bccRecipients": [],
            "receivedDateTime": "2026-03-25T04:00:00Z",
            "body": {"contentType": "html", "content": "<p>Hello</p>"},
            "hasAttachments": False,
            "categories": [],
            "isDraft": False,
            "parentFolderId": folder,
            "sensitivity": "normal",
            "props": {},
            **fields,
        }
        self.messages[message_id] = item
        return item

    # -------------------------------------------------- handlers

    def _view(self, item: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in item.items() if k != "props"}
        out["singleValueExtendedProperties"] = _props(item)
        return out

    def _folder(self, request: httpx.Request, f: str) -> httpx.Response:
        children = {c["id"]: c["displayName"] for kids in self.children.values() for c in kids}
        name = self.FOLDERS.get(f, children.get(f, f))
        return httpx.Response(
            200, json={"id": f"folder-{f}" if f in self.FOLDERS else f, "displayName": name}
        )

    def _children(self, request: httpx.Request, f: str) -> httpx.Response:
        return httpx.Response(200, json={"value": self.children.get(f, [])})

    def _list(self, request: httpx.Request, f: str) -> httpx.Response:
        flt = _query(request).get("$filter", "")
        folder = f"folder-{f}"
        items = [m for m in self.messages.values() if m["parentFolderId"] == folder]
        match = re.search(r"ep/value eq '((?:[^']|'')*)'", flt)
        if match:
            key = match.group(1).replace("''", "'")
            items = [m for m in items if key in m["props"].values()]
        return httpx.Response(200, json={"value": [self._view(m) for m in items]})

    def _get(self, request: httpx.Request, m: str) -> httpx.Response:
        if m not in self.messages:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        return httpx.Response(200, json=self._view(self.messages[m]))

    def _patch(self, request: httpx.Request, m: str) -> httpx.Response:
        if m not in self.messages:
            return httpx.Response(404)
        item, body = self.messages[m], _json(request)
        for prop in body.pop("singleValueExtendedProperties", []):
            item["props"][prop["id"]] = prop["value"]
        item.update(body)
        return httpx.Response(200, json=self._view(item))

    def _delete(self, request: httpx.Request, m: str) -> httpx.Response:
        self.messages.pop(m, None)
        return httpx.Response(204)

    def _new_id(self) -> str:
        self._n += 1
        return f"draft-{self._n}"

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = _json(request)
        props = {p["id"]: p["value"] for p in body.pop("singleValueExtendedProperties", [])}
        item = self.add(self._new_id(), "folder-drafts", isDraft=True, props=props, **body)
        return httpx.Response(201, json=self._view(item))

    def _reply(self, request: httpx.Request, m: str) -> httpx.Response:
        original = self.messages[m]
        item = self.add(
            self._new_id(),
            "folder-drafts",
            isDraft=True,
            subject=f"RE: {original['subject']}",
            toRecipients=[original["from"]],
            body={"contentType": "html", "content": "<hr><p>Original: Hello</p>"},
        )
        return httpx.Response(201, json=self._view(item))

    def _send(self, request: httpx.Request, m: str) -> httpx.Response:
        item = self.messages.get(m)
        if item is None:
            return httpx.Response(404)
        item["isDraft"] = False
        item["parentFolderId"] = "folder-sentitems"
        self.sends.append(m)
        return httpx.Response(202)

    def _move(self, request: httpx.Request, m: str) -> httpx.Response:
        item = self.messages[m]
        item["parentFolderId"] = _json(request)["destinationId"]
        return httpx.Response(201, json=self._view(item))


class CalendarSim:
    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router
        self.events: dict[str, dict[str, Any]] = {}
        self.view: list[dict[str, Any]] = []
        self.creates = 0
        base = re.escape(GRAPH_BASE)
        router.get(url__regex=rf"{base}/me/calendarView").mock(
            side_effect=lambda r: httpx.Response(200, json={"value": self.view})
        )
        router.post(url__regex=rf"{base}/me/events$").mock(side_effect=self._create)
        router.get(url__regex=rf"{base}/me/events/(?P<e>[^/?]+)").mock(side_effect=self._get)
        router.delete(url__regex=rf"{base}/me/events/(?P<e>[^/?]+)").mock(side_effect=self._delete)

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = _json(request)
        for event in self.events.values():
            if body.get("transactionId") and event.get("transactionId") == body["transactionId"]:
                return httpx.Response(201, json=event)  # Graph: same transactionId, same event
        self.creates += 1
        event_id = f"event-{self.creates}"
        props = {p["id"]: p["value"] for p in body.pop("singleValueExtendedProperties", [])}
        event = {
            "id": event_id,
            "isOrganizer": True,
            "attendees": [],
            **body,
            "singleValueExtendedProperties": [{"id": k, "value": v} for k, v in props.items()],
        }
        self.events[event_id] = event
        return httpx.Response(201, json=event)

    def _get(self, request: httpx.Request, e: str) -> httpx.Response:
        if e not in self.events:
            return httpx.Response(404)
        return httpx.Response(200, json=self.events[e])

    def _delete(self, request: httpx.Request, e: str) -> httpx.Response:
        self.events.pop(e, None)
        return httpx.Response(204)


class TeamsSim:
    ME = "me-id"

    def __init__(self, router: respx.MockRouter) -> None:
        self.chats: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.members: dict[str, list[dict[str, Any]]] = {}
        self.posted: list[tuple[str, str]] = []
        self.message_reads: list[str] = []
        base = re.escape(GRAPH_BASE)
        router.get(url__regex=rf"{base}/me(\?|$)").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": self.ME,
                    "mail": "test.user@corp.example",
                    "userPrincipalName": "test.user@corp.example",
                },
            )
        )
        router.get(url__regex=rf"{base}/me/chats(\?|$)").mock(side_effect=self._chats)
        router.get(url__regex=rf"{base}/me/chats/(?P<c>[^/?]+)/members").mock(side_effect=self._members)
        router.get(url__regex=rf"{base}/me/chats/(?P<c>[^/?]+)/messages").mock(side_effect=self._messages)
        router.post(url__regex=rf"{base}/me/chats/(?P<c>[^/?]+)/messages").mock(side_effect=self._post)
        router.get(url__regex=rf"{base}/me/chats/(?P<c>[^/?]+)(\?|$)").mock(side_effect=self._chat)

    def add_chat(self, chat_id: str, last: str, members: list[str], topic: str | None = None) -> None:
        self.chats[chat_id] = {"id": chat_id, "topic": topic, "lastMessagePreview": {"createdDateTime": last}}
        self.members[chat_id] = [{"email": m, "userId": m} for m in members]
        self.messages.setdefault(chat_id, [])

    def add_message(
        self, chat_id: str, message_id: str, created: str, text: str, sender: str = "u-anna", **extra: Any
    ) -> dict[str, Any]:
        raw = {
            "id": message_id,
            "messageType": "message",
            "createdDateTime": created,
            "lastModifiedDateTime": extra.pop("lastModifiedDateTime", created),
            "lastEditedDateTime": extra.pop("lastEditedDateTime", None),
            "deletedDateTime": extra.pop("deletedDateTime", None),
            "from": {
                "user": {"id": sender, "displayName": "Anna Keller" if sender != self.ME else "Test User"}
            },
            "body": {"contentType": "html", "content": f"<p>{text}</p>"},
            "mentions": extra.pop("mentions", []),
            **extra,
        }
        self.messages[chat_id] = [m for m in self.messages[chat_id] if m["id"] != message_id] + [raw]
        return raw

    def _chats(self, request: httpx.Request) -> httpx.Response:
        ordered = sorted(
            self.chats.values(), key=lambda c: c["lastMessagePreview"]["createdDateTime"], reverse=True
        )
        return httpx.Response(200, json={"value": ordered})

    def _chat(self, request: httpx.Request, c: str) -> httpx.Response:
        if c not in self.chats:
            return httpx.Response(404)
        return httpx.Response(200, json=self.chats[c])

    def _members(self, request: httpx.Request, c: str) -> httpx.Response:
        return httpx.Response(200, json={"value": self.members.get(c, [])})

    def _messages(self, request: httpx.Request, c: str) -> httpx.Response:
        self.message_reads.append(c)
        flt = _query(request).get("$filter", "")
        items = list(self.messages.get(c, []))
        match = re.search(r"lastModifiedDateTime gt (\S+)", flt)
        if match:
            after = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            items = [
                m
                for m in items
                if datetime.fromisoformat(m["lastModifiedDateTime"].replace("Z", "+00:00")) > after
            ]
        items.sort(key=lambda m: m["lastModifiedDateTime"], reverse=True)
        return httpx.Response(200, json={"value": items})

    def _post(self, request: httpx.Request, c: str) -> httpx.Response:
        content = _json(request)["body"]["content"]
        self.posted.append((c, content))
        raw = self.add_message(c, f"posted-{len(self.posted)}", "2026-03-25T04:30:00Z", "", sender=self.ME)
        raw["body"] = {"contentType": "html", "content": content}
        return httpx.Response(201, json=raw)


class PowerBISim:
    def __init__(self, router: respx.MockRouter) -> None:
        self.groups = [
            {"id": "g-shared", "name": "Sales", "isOnDedicatedCapacity": False},
            {"id": "g-premium", "name": "Ops", "isOnDedicatedCapacity": True, "capacityId": "cap-1"},
        ]
        self.datasets: dict[str, dict[str, Any]] = {}
        self.refreshes: dict[str, list[dict[str, Any]]] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.forbidden: set[str] = set()
        self.posts: list[str] = []
        base = re.escape(POWERBI_BASE)
        router.get(url__regex=rf"{base}/groups(\?|$)").mock(
            side_effect=lambda r: httpx.Response(200, json={"value": self.groups})
        )
        router.get(url__regex=rf"{base}/capacities").mock(
            return_value=httpx.Response(200, json={"value": [{"id": "cap-1", "sku": "P1"}]})
        )
        router.get(url__regex=rf"{base}/datasets/(?P<d>[^/?]+)$").mock(return_value=httpx.Response(404))
        router.get(url__regex=rf"{base}/groups/(?P<g>[^/]+)/datasets(\?|$)").mock(side_effect=self._datasets)
        router.get(url__regex=rf"{base}/groups/(?P<g>[^/]+)/datasets/(?P<d>[^/]+)/refreshes").mock(
            side_effect=self._history
        )
        router.post(url__regex=rf"{base}/groups/(?P<g>[^/]+)/datasets/(?P<d>[^/]+)/refreshes").mock(
            side_effect=self._trigger
        )
        router.get(url__regex=rf"{base}/groups/(?P<g>[^/]+)/reports/(?P<r>[^/?]+)").mock(
            side_effect=self._report
        )
        router.get(url__regex=rf"{base}/reports/(?P<r>[^/?]+)").mock(side_effect=self._my_report)

    def add_dataset(
        self,
        dataset_id: str,
        group: str = "g-shared",
        owner: str = "test.user@corp.example",
        refreshable: bool = True,
    ) -> None:
        self.datasets[dataset_id] = {
            "id": dataset_id,
            "name": f"DS {dataset_id}",
            "configuredBy": owner,
            "isRefreshable": refreshable,
            "group": group,
        }
        self.refreshes.setdefault(dataset_id, [])

    def add_refresh(
        self,
        dataset_id: str,
        start: str,
        status: str = "Completed",
        end: str | None = None,
        request_id: str | None = None,
        error: str | None = None,
    ) -> None:
        raw: dict[str, Any] = {
            "requestId": request_id or f"r{len(self.refreshes[dataset_id]) + 1}",
            "refreshType": "Scheduled",
            "status": status,
            "startTime": start,
            "endTime": end,
        }
        if error:
            raw["serviceExceptionJson"] = json.dumps({"errorCode": error})
        self.refreshes[dataset_id].insert(0, raw)

    def _datasets(self, request: httpx.Request, g: str) -> httpx.Response:
        return httpx.Response(200, json={"value": [d for d in self.datasets.values() if d["group"] == g]})

    def _history(self, request: httpx.Request, g: str, d: str) -> httpx.Response:
        top = int(_query(request).get("$top", "60"))
        return httpx.Response(200, json={"value": self.refreshes.get(d, [])[:top]})

    def _trigger(self, request: httpx.Request, g: str, d: str) -> httpx.Response:
        if d in self.forbidden:
            return httpx.Response(403, json={"error": {"code": "Unauthorized", "message": "no"}})
        self.posts.append(d)
        return httpx.Response(202, headers={"RequestId": f"new-{len(self.posts)}"})

    def _my_report(self, request: httpx.Request, r: str) -> httpx.Response:
        return self._report(request, "me", r)

    def _report(self, request: httpx.Request, g: str, r: str) -> httpx.Response:
        if r not in self.reports:
            return httpx.Response(404)
        return httpx.Response(200, json=self.reports[r])
