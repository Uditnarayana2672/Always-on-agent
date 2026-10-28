from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest

from agent.adapters import outlook_com as oc
from agent.adapters.outlook_com import OutlookComAdapter
from agent.core.errors import AdapterError


class Obj:
    """A bag of attributes standing in for a COM object."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)

    # COM objects have dynamic attributes; these two let mypy accept that.
    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        self.__dict__[name] = value


class Items(list[Any]):
    def __init__(self, items: list[Any]) -> None:
        super().__init__(items)
        self.filters: list[str] = []
        self.IncludeRecurrences = False

    @property
    def Count(self) -> int:
        return len(self)

    def Restrict(self, query: str) -> "Items":
        self.filters.append(query)
        return self

    def Sort(self, key: str, descending: bool = False) -> None:
        pass


def local(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    """Wall-clock local time, the way Outlook returns it."""
    return datetime(y, mo, d, h, mi)


def mail(entry: str, conv: str, received: datetime, **extra: Any) -> Obj:
    saved: list[bool] = []
    attrs: dict[str, Any] = {
        "EntryID": entry,
        "ConversationID": conv,
        "Class": 43,
        "Subject": f"Subject {entry}",
        "SenderName": "Anna Keller",
        "SenderEmailAddress": "/O=CORP/OU=EXCHANGE/CN=RECIPIENTS/CN=AKELLER",
        "SenderEmailType": "EX",
        "Sender": Obj(GetExchangeUser=lambda: Obj(PrimarySmtpAddress="anna.keller@corp.example")),
        "Recipients": [
            Obj(
                Name="Test User",
                Type=1,
                Address="x",
                PropertyAccessor=Obj(GetProperty=lambda p: "test.user@corp.example"),
            ),
            Obj(
                Name="Marc Dubois",
                Type=2,
                Address="marc.dubois@partner.example",
                PropertyAccessor=Obj(GetProperty=lambda p: (_ for _ in ()).throw(RuntimeError())),
            ),
        ],
        "ReceivedTime": received,
        "Body": "body text",
        "Attachments": Items([]),
        "Categories": "ServiceNow, Doxis",
        "Importance": 2,
        "saved": saved,
    }
    attrs.update(extra)
    item = Obj(**attrs)
    item.Save = lambda: saved.append(True)
    return item


class FakeOutlook:
    def __init__(self) -> None:
        self.inbox = Items(
            [
                mail("E1", "C1", local(2026, 3, 25, 9, 0)),
                Obj(EntryID="M1", Class=53, ConversationID="C9"),  # meeting request: skipped
                mail("E2", "C2", local(2026, 3, 25, 9, 30)),
            ]
        )
        self.sent = Items(
            [
                mail(
                    "S1",
                    "C1",
                    local(2026, 3, 25, 9, 10),
                    SenderEmailType="SMTP",
                    SenderEmailAddress="test.user@corp.example",
                )
            ]
        )
        self.calendar = Items(
            [
                Obj(
                    EntryID="V1",
                    Subject="Stand-up",
                    Start=local(2026, 3, 25, 9, 30),
                    End=local(2026, 3, 25, 9, 45),
                    Sensitivity=0,
                    BusyStatus=2,
                    Organizer="Anna Keller",
                    Body="Join: https://teams.microsoft.com/l/meetup-join/19%3afake%40thread.v2/0 ",
                ),
                Obj(
                    EntryID="V2",
                    Subject="Doctor",
                    Start=local(2026, 3, 25, 17, 0),
                    End=local(2026, 3, 25, 18, 0),
                    Sensitivity=2,
                    BusyStatus=3,
                    Organizer="Test User",
                    Body="private notes",
                ),
            ]
        )
        self.done = Obj(Name="Done")
        self.root = Obj(Folders={"Inbox": Obj(Folders={"Done": self.done})})
        self.created: list[Obj] = []
        self.moves: list[tuple[str, Any]] = []
        self.by_id = {i.EntryID: i for i in [*self.inbox, *self.sent, *self.calendar]}

    def GetNamespace(self, name: str) -> Obj:
        folders = {
            6: Obj(Items=self.inbox, Parent=self.root),
            5: Obj(Items=self.sent),
            9: Obj(Items=self.calendar),
        }

        def get_item(entry: str) -> Any:
            if entry not in self.by_id:
                raise RuntimeError("item not found")
            item = self.by_id[entry]
            item.Move = lambda folder: self.moves.append((entry, folder))
            item.MarkAsTask = lambda kind: None
            return item

        return Obj(GetDefaultFolder=lambda i: folders[i], GetItemFromID=get_item)

    def CreateItem(self, kind: int) -> Obj:
        item = Obj(EntryID=f"NEW{len(self.created)}", kind=kind)
        item.Save = lambda: None
        item.Send = lambda: setattr(item, "sent", True)
        self.created.append(item)
        self.by_id[item.EntryID] = item
        return item


@pytest.fixture
def outlook() -> FakeOutlook:
    return FakeOutlook()


@pytest.fixture
def adapter(outlook: FakeOutlook) -> OutlookComAdapter:
    return OutlookComAdapter(connect=lambda: outlook, executor=ThreadPoolExecutor(max_workers=1))


def test_filters_are_utc_dasl() -> None:
    since = datetime(2026, 3, 25, 9, 0, tzinfo=UTC)
    assert oc.dasl_received_after(since) == "@SQL=\"urn:schemas:httpmail:datereceived\" > '2026-03-25 09:00'"
    assert "dtstart\" < '2026-03-26 00:00'" in oc.dasl_calendar_overlap(
        since, datetime(2026, 3, 26, tzinfo=UTC)
    )


def test_local_time_round_trip() -> None:
    wall = datetime(2026, 3, 25, 9, 0)
    aware = oc.com_time_to_utc(wall)
    assert aware.tzinfo == UTC and oc.utc_to_com_local(aware) == wall
    labelled_utc = wall.replace(tzinfo=UTC)  # pywin32 sometimes labels local times as UTC
    assert oc.com_time_to_utc(labelled_utc) == aware


async def test_list_new_maps_mail_and_skips_other_classes(
    adapter: OutlookComAdapter, outlook: FakeOutlook
) -> None:
    mails = await adapter.list_new(datetime(2026, 3, 25, tzinfo=UTC))
    assert [m.id for m in mails] == ["E1", "E2"]
    first = mails[0]
    assert first.sender.address == "anna.keller@corp.example"  # X.500 resolved to SMTP
    assert [p.address for p in first.to] == ["test.user@corp.example"]
    assert [p.address for p in first.cc] == ["marc.dubois@partner.example"]  # fallback to Address
    assert first.categories == ["ServiceNow", "Doxis"] and first.importance == "high"
    assert first.received_at == oc.com_time_to_utc(local(2026, 3, 25, 9, 0))
    assert outlook.inbox.filters[0].startswith("@SQL=")


async def test_thread_spans_inbox_and_sent(adapter: OutlookComAdapter) -> None:
    thread = await adapter.get_thread("C1")
    assert [m.id for m in thread] == ["E1", "S1"]


async def test_calendar_private_and_join_link(adapter: OutlookComAdapter, outlook: FakeOutlook) -> None:
    events = await adapter.events_between(
        datetime(2026, 3, 24, tzinfo=UTC), datetime(2026, 3, 26, tzinfo=UTC)
    )
    standup, private = events
    assert standup.join_url and standup.join_url.startswith("https://teams.microsoft.com/l/meetup-join/")
    assert private.private and private.subject == "(private)" and private.join_url is None
    assert private.show_as == "oof"
    assert outlook.calendar.IncludeRecurrences is True


async def test_writes(adapter: OutlookComAdapter, outlook: FakeOutlook) -> None:
    await adapter.categorise("E1", ["Task", "INC30114872"])
    assert outlook.by_id["E1"].Categories == "Task, INC30114872" and outlook.by_id["E1"].saved
    await adapter.move("E2", "Inbox/Done")
    assert outlook.moves == [("E2", outlook.done)]
    with pytest.raises(AdapterError, match="folder not found"):
        await adapter.move("E2", "Inbox/Missing")

    draft_id = await adapter.create_draft(
        ["sophie.laurent@corp.example"], "Re: access", "Done.", cc=["a@corp.example"]
    )
    draft = outlook.by_id[draft_id]
    assert (draft.To, draft.CC, draft.Subject) == (
        "sophie.laurent@corp.example",
        "a@corp.example",
        "Re: access",
    )
    await adapter.send(draft_id, "idem-1")
    assert draft.sent is True
    await adapter.send("GONE", "idem-2")  # already sent: no error

    hold_id = await adapter.add_hold(
        "Focus", datetime(2026, 3, 25, 10, tzinfo=UTC), datetime(2026, 3, 25, 11, tzinfo=UTC)
    )
    hold = outlook.by_id[hold_id]
    assert hold.BusyStatus == oc.OL_BUSY_TENTATIVE and hold.Start.tzinfo is None


async def test_com_failure_becomes_adapter_error_and_reconnects(outlook: FakeOutlook) -> None:
    calls = {"n": 0}

    def connect() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("Outlook not running")
        return outlook

    adapter = OutlookComAdapter(connect=connect, executor=ThreadPoolExecutor(max_workers=1))
    health = await adapter.health()
    assert health.status == "down" and "classic Outlook" in health.detail
    assert (await adapter.health()).status == "ok"
