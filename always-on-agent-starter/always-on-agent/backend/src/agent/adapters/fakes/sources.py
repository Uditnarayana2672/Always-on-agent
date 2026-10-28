"""Fake SharePoint lists, ServiceNow email, SQL read-only and OCR (A06, A07, A09, A11)."""

import csv
import hashlib
import io
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from agent.adapters.base import (
    AssignmentNotice,
    Attachment,
    MailMessage,
    OcrLine,
    OcrResult,
    TicketRow,
    ToolSpec,
)
from agent.adapters.fakes._base import FIXTURES_DIR, FakeAdapter, parse_time
from agent.core.clock import Clock
from agent.core.errors import AdapterError


class FakeSharePoint(FakeAdapter):
    name = "sharepoint"

    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        super().__init__(clock, fixtures)
        self.lists: dict[str, dict[str, Any]] = {
            lst["id"]: lst for lst in self._load("sharepoint/lists.json")["lists"]
        }

    def deliver(self, list_id: str, item: dict[str, Any]) -> None:
        self.lists[list_id]["items"].append(item)

    async def list_items(self, list_id: str, since: datetime | None = None) -> list[dict[str, Any]]:
        self._check("list_items")
        if list_id not in self.lists:
            raise AdapterError(f"list {list_id} not found", status_code=404)
        items: list[dict[str, Any]] = self.lists[list_id]["items"]
        if since is not None:
            items = [i for i in items if parse_time(i["lastModifiedDateTime"]) > since]
        return items

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="read_sharepoint_list", mode="silent", description="Read list items")]


def _report_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


class FakeServiceNow(FakeAdapter):
    """Assignment parsing returns golden results; real template parsers arrive in prompt 2.1.

    CSV reports are parsed for real (semicolon or comma). An XLSX report returns the rows of the
    CSV fixture, which holds the same data, so the fake does not need openpyxl at runtime.
    """

    name = "servicenow"

    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        super().__init__(clock, fixtures)
        self.expected: dict[str, dict[str, Any]] = self._load("emails/expected_assignments.json")
        self.report_path = self.fixtures / "servicenow_reports" / "doxis_l2_open.csv"

    def parse_assignment(self, mail: MailMessage) -> AssignmentNotice | None:
        self._check("parse_assignment")
        raw = self.expected.get(mail.id)
        return AssignmentNotice.model_validate(raw) if raw else None

    def parse_report(self, attachment: Attachment) -> list[TicketRow]:
        self._check("parse_report")
        if attachment.name.lower().endswith(".xlsx"):
            return self._parse_csv(self.report_path.read_bytes())
        return self._parse_csv(attachment.data)

    async def list_open_tickets(self) -> list[TicketRow]:
        self._check("list_open_tickets")
        return self._parse_csv(self.report_path.read_bytes())

    @staticmethod
    def _parse_csv(data: bytes) -> list[TicketRow]:
        text = data.decode("utf-8-sig")
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,")
        rows = []
        for row in csv.DictReader(io.StringIO(text), dialect=dialect):
            rows.append(
                TicketRow(
                    number=row["Number"],
                    short_description=row["Short description"],
                    state=row["State"],
                    priority=row["Priority"],
                    opened=_report_time(row["Opened"]),
                    updated=_report_time(row["Updated"]),
                    assigned_to=row["Assigned to"],
                    assignment_group=row["Assignment group"],
                    caller=row["Caller"],
                )
            )
        return rows

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="read_report_email", mode="silent", description="Read ServiceNow mail"),
            ToolSpec(name="add_comment_by_reply", mode="ask", description="Reply to a notification"),
        ]


class FakeSql(FakeAdapter):
    name = "sql"

    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        super().__init__(clock, fixtures)
        self.tables: dict[str, dict[str, Any]] = self._load("sql/tables.json")["tables"]

    def _table(self, table: str) -> dict[str, Any]:
        if table not in self.tables:
            raise AdapterError(f"table {table} is not on the allow list")
        return self.tables[table]

    async def count(self, table: str) -> int:
        self._check("count")
        return int(self._table(table)["count"])

    async def max_date(self, table: str) -> date | None:
        self._check("max_date")
        value = self._table(table).get("max_date")
        return date.fromisoformat(value) if value else None

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="sql_count", mode="silent", description="Row count and latest date")]


class FakeOcr(FakeAdapter):
    """Returns the recorded text and boxes for fixture screenshots; anything else reads as empty."""

    name = "ocr"

    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        super().__init__(clock, fixtures)
        expected: dict[str, list[dict[str, Any]]] = self._load("screenshots/ocr_expected.json")
        self.by_hash: dict[str, OcrResult] = {}
        for name, lines in expected.items():
            digest = hashlib.sha256((self.fixtures / "screenshots" / name).read_bytes()).hexdigest()
            self.by_hash[digest] = OcrResult(
                lines=[OcrLine(text=ln["text"], box=tuple(ln["box"])) for ln in lines]
            )

    async def read(self, image: bytes) -> OcrResult:
        self._check("read")
        return self.by_hash.get(hashlib.sha256(image).hexdigest(), OcrResult(lines=[]))

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="ocr_local", mode="silent", description="Read text from an image locally")]
