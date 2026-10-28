"""Generate fixtures/ (prompt 0.5). Every person, address, id, site and report here is made up.

Run from backend/:  uv run python scripts/make_fixtures.py
Output is deterministic, so re-running produces the same files.
"""

import base64
import csv
import io
import json
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2] / "fixtures"
DAY = "2026-03-25"  # a Wednesday, before the European clock change
CORP = "corp.example"
NS = uuid.UUID("6f1c2d3e-0000-4000-8000-000000000000")


def gid(name: str) -> str:
    return str(uuid.uuid5(NS, name))


def addr(name: str, domain: str = CORP) -> dict[str, Any]:
    local = name.lower().replace("á", "a").replace(" ", ".")
    return {"emailAddress": {"name": name, "address": f"{local}@{domain}"}}


ME = {"emailAddress": {"name": "Test User", "address": f"test.user@{CORP}"}}
SNOW = {"emailAddress": {"name": "ServiceNow", "address": f"servicenow@{CORP}"}}
PEOPLE = [
    "Anna Keller",
    "Tomás Rivera",
    "Sophie Laurent",
    "Elena Rossi",
    "Karim Haddad",
    "Priya Nair",
    "Marc Dubois",
]

TICKETS = [
    # number, caller, site, short description, priority, opened (UTC), state
    (
        "INC30108754",
        "Tomás Rivera",
        "SGP-03",
        "KM/LBA field missing on site page",
        "3 - Moderate",
        "2026-03-23 08:12",
        "On Hold",
    ),
    (
        "INC30109981",
        "Elena Rossi",
        "ORB-02",
        "Cannot open archived documents in Doxis",
        "3 - Moderate",
        "2026-03-24 07:40",
        "In Progress",
    ),
    (
        "INC30114872",
        "Sophie Laurent",
        "VRS-01",
        "Access denied to project folder",
        "2 - High",
        "2026-03-24 13:05",
        "In Progress",
    ),
    (
        "INC30115230",
        "Elena Rossi",
        "ORB-02",
        "Search returns no results for site",
        "3 - Moderate",
        "2026-03-25 02:15",
        "Assigned",
    ),
    (
        "INC30116640",
        "Tomás Rivera",
        "SGP-03",
        "Request new reviewer role for site",
        "4 - Low",
        "2026-03-25 02:50",
        "Assigned",
    ),
    (
        "INC30117002",
        "Sophie Laurent",
        "VRS-01",
        "Closing date not saved on project",
        "3 - Moderate",
        "2026-03-25 03:20",
        "Assigned",
    ),
]
REPORT_COLUMNS = [
    "Number",
    "Short description",
    "State",
    "Priority",
    "Opened",
    "Updated",
    "Assigned to",
    "Assignment group",
    "Caller",
]


def message(
    n: int, subject: str, sender: dict[str, Any], body: str, received: str, **extra: Any
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "id": f"AAMkAGFAKE{n:04d}",
        "conversationId": f"AAQkAGFAKECONV{n:04d}",
        "internetMessageId": f"<fake-{n:04d}@{CORP}>",
        "subject": subject,
        "from": sender,
        "sender": sender,
        "toRecipients": [ME],
        "ccRecipients": [],
        "receivedDateTime": f"{received}Z",
        "isRead": False,
        "importance": "normal",
        "categories": [],
        "hasAttachments": False,
        "bodyPreview": body[:120],
        "body": {"contentType": "text", "content": body},
        "attachments": [],
    }
    msg.update(extra)
    return msg


def report_rows() -> list[list[str]]:
    rows = []
    for number, caller, _site, desc, prio, opened, state in TICKETS:
        rows.append(
            [
                number,
                desc,
                state,
                prio,
                opened + ":00",
                "2026-03-25 03:30:00",
                "Test User",
                "Doxis L2",
                caller,
            ]
        )
    return rows


def report_csv() -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(REPORT_COLUMNS)
    writer.writerows(report_rows())
    return out.getvalue().encode("utf-8-sig")


def report_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Page 1"
    fixed = datetime(2026, 3, 25, 3, 30)
    wb.properties.created = wb.properties.modified = fixed
    ws.append(REPORT_COLUMNS)
    for row in report_rows():
        ws.append(row)
    raw = io.BytesIO()
    wb.save(raw)
    # Re-zip with fixed entry timestamps so the file is byte-for-byte reproducible.
    out = io.BytesIO()
    with zipfile.ZipFile(raw) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            info = zipfile.ZipInfo(item.filename, date_time=(2026, 3, 25, 3, 30, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            data = src.read(item.filename)
            if item.filename == "docProps/core.xml":  # openpyxl stamps the save time here
                data = re.sub(
                    rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*", rb"\g<1>2026-03-25T03:30:00Z", data
                )
            dst.writestr(info, data)
    return out.getvalue()


def attachment(name: str, content_type: str, data: bytes) -> dict[str, Any]:
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": f"ATT-{name}",
        "name": name,
        "contentType": content_type,
        "size": len(data),
        "contentBytes": base64.b64encode(data).decode(),
    }


def emails() -> list[dict[str, Any]]:
    out = []
    times = [
        "2026-03-23T08:13:00",
        "2026-03-24T07:41:00",
        "2026-03-24T13:06:00",
        "2026-03-25T02:16:00",
        "2026-03-25T02:51:00",
        "2026-03-25T03:21:00",
    ]
    for i, ((number, caller, site, desc, prio, opened, _state), at) in enumerate(
        zip(TICKETS, times, strict=True)
    ):
        body = (
            f"Incident {number} has been assigned to group Doxis L2.\n\n"
            f"Short description: {desc}\nCaller: {caller}\nSite: {site}\nPriority: {prio}\n"
            f"Opened: {opened} UTC\n\nLink: https://servicenow.{CORP}/nav_to.do?uri=incident.do?sysparm_query=number={number}"
        )
        out.append(
            message(
                1 + i,
                f"Incident {number} has been assigned to group Doxis L2",
                SNOW,
                body,
                at,
                categories=["ServiceNow"],
            )
        )
    out.append(
        message(
            7,
            "Incident INC30114872 - comment added",
            SNOW,
            "Sophie Laurent added a comment to INC30114872:\n\nThanks, I will try again tomorrow.\n\n"
            "Reply to this email to add a comment.",
            "2026-03-25T02:40:00",
        )
    )
    out.append(
        message(
            8,
            "Incident INC30109981 - comment added",
            SNOW,
            "Elena Rossi added a comment to INC30109981:\n\nThe archive still shows an empty list for ORB-02.\n\n"
            "Reply to this email to add a comment.",
            "2026-03-25T03:05:00",
        )
    )
    out.append(
        message(
            9,
            "Hourly report: Doxis L2 open incidents (CSV)",
            SNOW,
            "Scheduled report attached.",
            "2026-03-25T03:30:00",
            hasAttachments=True,
            attachments=[attachment("doxis_l2_open.csv", "text/csv", report_csv())],
        )
    )
    xlsx_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    out.append(
        message(
            10,
            "Hourly report: Doxis L2 open incidents (XLSX)",
            SNOW,
            "Scheduled report attached.",
            "2026-03-25T03:31:00",
            hasAttachments=True,
            attachments=[attachment("doxis_l2_open.xlsx", xlsx_type, report_xlsx())],
        )
    )
    out += [
        message(
            11,
            "Priorities for this week",
            addr("Anna Keller"),
            "Hi, please focus on INC30114872 first, then the Sales Overview refresh issue. Thanks, Anna",
            "2026-03-24T07:30:00",
            importance="high",
        ),
        message(
            12,
            "Sales Overview numbers look off for March",
            addr("Tomás Rivera"),
            "The March total on the Sales Overview report is lower than in the source. Can you check the last refresh?",
            "2026-03-25T03:10:00",
        ),
        message(
            13,
            "IT News - March edition",
            {"emailAddress": {"name": "IT News", "address": f"newsletter@{CORP}"}},
            "This month: new laptops, password tips and the spring cleanup.",
            "2026-03-24T06:00:00",
            categories=["Newsletter"],
        ),
        message(
            14,
            "FYI: dataflow maintenance on Saturday",
            addr("Priya Nair"),
            "FYI, the Sales Staging dataflow will be paused on Saturday 09:00-11:00 CET. No action needed.",
            "2026-03-24T15:20:00",
        ),
        message(
            15,
            "Question sur l'accès au dossier projet",
            addr("Sophie Laurent"),
            "Bonjour, je n'arrive toujours pas à ouvrir le dossier du projet VRS-01. Pouvez-vous vérifier mes droits ? "
            "Merci, Sophie",
            "2026-03-25T02:30:00",
        ),
        message(
            16,
            "Integration test schedule",
            addr("Karim Haddad"),
            "Hi all, the integration test for ORB-02 moves to Friday. Marc, please confirm your team is available.",
            "2026-03-24T11:00:00",
            ccRecipients=[addr("Marc Dubois", "partner.example")],
        ),
        message(
            17,
            "ENH-014: add region filter to Sales Overview",
            addr("Priya Nair"),
            "Request ENH-014: business asks for a region filter on the Sales Overview report. Target: April release.",
            "2026-03-24T09:45:00",
        ),
        message(
            18,
            "Thanks for the quick help",
            addr("Elena Rossi"),
            "Thanks for looking into it so quickly!",
            "2026-03-25T03:25:00",
        ),
    ]
    return out


def expected_assignments() -> dict[str, Any]:
    """Golden parse results for the assignment notifications, keyed by message id."""
    out = {}
    for i, (number, caller, site, desc, prio, opened, _state) in enumerate(TICKETS):
        out[f"AAMkAGFAKE{1 + i:04d}"] = {
            "number": number,
            "short_description": desc,
            "caller": caller,
            "site": site,
            "priority": prio,
            "opened_at": opened.replace(" ", "T") + ":00Z",
        }
    return out


def user(name: str) -> dict[str, Any]:
    return {"user": {"id": gid(name), "displayName": name, "userIdentityType": "aadUser"}}


def chat_msg(chat: str, n: int, who: str, text: str, at: str, **extra: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "id": f"{chat}-m{n}",
        "chatId": chat,
        "messageType": "message",
        "createdDateTime": f"{DAY}T{at}:00Z",
        "lastModifiedDateTime": f"{DAY}T{at}:00Z",
        "deletedDateTime": None,
        "from": user(who),
        "body": {"contentType": "html", "content": f"<p>{text}</p>"},
        "mentions": [],
    }
    msg.update(extra)
    return msg


def teams() -> dict[str, Any]:
    report = gid("report:Sales Overview")
    group = gid("group:Sales Analytics")
    link = f"https://app.powerbi.com/groups/{group}/reports/{report}/ReportSection?experience=power-bi"
    me_mention = {"id": 0, "mentionText": "Test User", "mentioned": user("Test User")}
    return {
        "me": user("Test User")["user"],
        "chats": [
            {
                "id": "19:fake-priya@unq.gbl.spaces",
                "chatType": "oneOnOne",
                "topic": None,
                "members": [user("Test User"), user("Priya Nair")],
                "messages": [
                    chat_msg(
                        "19:fake-priya@unq.gbl.spaces",
                        1,
                        "Priya Nair",
                        f"REF-0412: can you refresh Sales Overview? {link}",
                        "03:00",
                    ),
                    chat_msg(
                        "19:fake-priya@unq.gbl.spaces",
                        2,
                        "Priya Nair",
                        "Business needs it before 11:00 CET.",
                        "03:01",
                    ),
                ],
            },
            {
                "id": "19:fake-doxis-l2@thread.v2",
                "chatType": "group",
                "topic": "Doxis L2 support",
                "members": [user("Test User"), user("Karim Haddad"), user("Anna Keller")],
                "messages": [
                    chat_msg(
                        "19:fake-doxis-l2@thread.v2",
                        1,
                        "Karim Haddad",
                        '<at id="0">Test User</at> can you look at INC30115230? Search is empty for ORB-02.',
                        "02:30",
                        mentions=[me_mention],
                    ),
                    chat_msg(
                        "19:fake-doxis-l2@thread.v2",
                        2,
                        "Karim Haddad",
                        "Update: it affects SGP-03 as well.",
                        "02:35",
                        lastModifiedDateTime=f"{DAY}T02:41:00Z",
                    ),
                    chat_msg(
                        "19:fake-doxis-l2@thread.v2",
                        3,
                        "Anna Keller",
                        "",
                        "02:50",
                        deletedDateTime=f"{DAY}T02:52:00Z",
                        body={"contentType": "html", "content": ""},
                    ),
                    chat_msg(
                        "19:fake-doxis-l2@thread.v2",
                        4,
                        "Anna Keller",
                        "Please keep the caller updated today.",
                        "02:55",
                    ),
                ],
            },
        ],
    }


def powerbi() -> dict[str, Any]:
    g1, g2 = gid("group:Sales Analytics"), gid("group:Operations Reporting")
    ds = {
        n: gid(f"dataset:{n}") for n in ["Sales Overview", "Inventory Health", "Ops KPI", "Finance Monthly"]
    }
    df = {n: gid(f"dataflow:{n}") for n in ["Sales Staging", "Inventory Staging"]}

    def refresh(n: int, status: str, start: str, end: str | None, error: str | None = None) -> dict[str, Any]:
        r: dict[str, Any] = {
            "requestId": gid(f"refresh:{n}"),
            "id": n,
            "refreshType": "Scheduled",
            "status": status,
            "startTime": f"{start}Z",
            "endTime": f"{end}Z" if end else None,
        }
        if error:
            r["serviceExceptionJson"] = json.dumps({"errorCode": error})
        return r

    return {
        "groups": [
            {"id": g1, "name": "Sales Analytics", "isOnDedicatedCapacity": False, "type": "Workspace"},
            {
                "id": g2,
                "name": "Operations Reporting",
                "isOnDedicatedCapacity": True,
                "capacityId": gid("capacity:P1"),
                "type": "Workspace",
            },
        ],
        "capacities": [{"id": gid("capacity:P1"), "displayName": "Fake P1", "sku": "P1", "state": "Active"}],
        "datasets": [
            {
                "id": ds["Sales Overview"],
                "groupId": g1,
                "name": "Sales Overview",
                "configuredBy": f"test.user@{CORP}",
                "isRefreshable": True,
                "upstreamDataflows": [df["Sales Staging"]],
            },
            {
                "id": ds["Inventory Health"],
                "groupId": g1,
                "name": "Inventory Health",
                "configuredBy": f"priya.nair@{CORP}",
                "isRefreshable": True,
                "upstreamDataflows": [df["Inventory Staging"]],
            },
            {
                "id": ds["Ops KPI"],
                "groupId": g2,
                "name": "Ops KPI",
                "configuredBy": f"test.user@{CORP}",
                "isRefreshable": True,
                "upstreamDataflows": [],
            },
            {
                "id": ds["Finance Monthly"],
                "groupId": g2,
                "name": "Finance Monthly",
                "configuredBy": f"anna.keller@{CORP}",
                "isRefreshable": True,
                "upstreamDataflows": [],
            },
        ],
        "reports": [
            {
                "id": gid(f"report:{n}"),
                "groupId": g,
                "name": n,
                "datasetId": ds[d],
                "webUrl": f"https://app.powerbi.com/groups/{g}/reports/{gid(f'report:{n}')}",
            }
            for n, g, d in [
                ("Sales Overview", g1, "Sales Overview"),
                ("Inventory Health", g1, "Inventory Health"),
                ("Ops KPI", g2, "Ops KPI"),
                ("Finance Monthly", g2, "Finance Monthly"),
            ]
        ],
        "apps": [
            {
                "id": gid("app:Sales App"),
                "name": "Sales App",
                "workspaceId": g1,
                "reports": [gid("report:Sales Overview"), gid("report:Inventory Health")],
            }
        ],
        "refreshes": {
            ds["Sales Overview"]: [
                refresh(3, "Completed", f"{DAY}T00:30:00", f"{DAY}T00:41:00"),
                refresh(
                    2,
                    "Failed",
                    "2026-03-24T00:30:00",
                    "2026-03-24T00:33:00",
                    "DM_GWPipeline_Gateway_TimeoutError",
                ),
                refresh(1, "Completed", "2026-03-23T00:30:00", "2026-03-23T00:40:00"),
            ],
            ds["Inventory Health"]: [
                refresh(
                    6,
                    "Failed",
                    f"{DAY}T01:00:00",
                    f"{DAY}T01:02:00",
                    "ModelRefresh_ShortMessage_ProcessingError",
                ),
                refresh(5, "Completed", "2026-03-24T01:00:00", "2026-03-24T01:12:00"),
            ],
            ds["Ops KPI"]: [
                refresh(8, "Unknown", f"{DAY}T03:40:00", None),
                refresh(7, "Completed", "2026-03-24T03:40:00", "2026-03-24T03:52:00"),
            ],
            ds["Finance Monthly"]: [
                refresh(10, "Completed", f"{DAY}T00:00:00", f"{DAY}T01:35:00"),
                refresh(9, "Completed", "2026-03-24T00:00:00", "2026-03-24T00:21:00"),
            ],
        },
        "dataflows": [
            {
                "objectId": df["Sales Staging"],
                "groupId": g1,
                "name": "Sales Staging",
                "transactions": [
                    {
                        "id": "t2",
                        "status": "Success",
                        "startTime": f"{DAY}T00:05:00Z",
                        "endTime": f"{DAY}T00:20:00Z",
                    }
                ],
            },
            {
                "objectId": df["Inventory Staging"],
                "groupId": g1,
                "name": "Inventory Staging",
                "transactions": [
                    {
                        "id": "t4",
                        "status": "Failed",
                        "startTime": f"{DAY}T00:35:00Z",
                        "endTime": f"{DAY}T00:50:00Z",
                    }
                ],
            },
        ],
    }


def calendar() -> list[dict[str, Any]]:
    def event(
        n: int, subject: str, start: str, end: str, tz: str = "India Standard Time", **extra: Any
    ) -> dict[str, Any]:
        e: dict[str, Any] = {
            "id": f"AAMkAGFAKEEVT{n:02d}",
            "subject": subject,
            "sensitivity": "normal",
            "showAs": "busy",
            "start": {"dateTime": f"{DAY}T{start}:00.0000000", "timeZone": tz},
            "end": {"dateTime": f"{DAY}T{end}:00.0000000", "timeZone": tz},
            "organizer": addr("Anna Keller"),
            "isOnlineMeeting": False,
            "onlineMeeting": None,
        }
        e.update(extra)
        return e

    return [
        event(
            1,
            "Doxis L2 stand-up",
            "09:30",
            "09:45",
            isOnlineMeeting=True,
            onlineMeeting={"joinUrl": "https://teams.example/meet/fake-standup"},
        ),
        event(
            2,
            "Sales Overview review with business",
            "10:00",
            "10:30",
            tz="W. Europe Standard Time",
            organizer=addr("Tomás Rivera"),
            isOnlineMeeting=True,
            onlineMeeting={"joinUrl": "https://teams.example/meet/fake-sales-review"},
        ),
        event(3, "Private appointment", "17:00", "18:00", sensitivity="private", organizer=ME),
        event(4, "Focus time", "15:00", "16:00", showAs="tentative", organizer=ME),
    ]


SCREENS = {
    "doxis_site_list": (
        "Doxis - Site list",
        [
            "Site code   Name                 State",
            "ORB-02      Orbital Depot        Active",
            "VRS-01      Verso Plant          Active",
            "SGP-03      Seagate Hub          Closing",
        ],
    ),
    "doxis_project_properties": (
        "Doxis - Project properties",
        [
            "Project: VRS-01 Line upgrade",
            "State: Open",
            "Closing date: 2026-04-30",
            "TS/NP: TS",
            "Roles: Reader, Editor",
            "Owner: Sophie Laurent",
        ],
    ),
    "doxis_site_page_km_lba": (
        "Doxis - Site page SGP-03",
        [
            "Site: SGP-03 Seagate Hub",
            "KM: Tomás Rivera",
            "LBA: (empty)",
            "Reviewer: Karim Haddad",
            "Last change: 2026-03-20",
        ],
    ),
}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Arial covers accents such as the "á" in Tomás; the built-in font does not.
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def screenshot(title: str, lines: list[str]) -> tuple[bytes, list[dict[str, Any]]]:
    img = Image.new("RGB", (900, 420), "white")
    draw = ImageDraw.Draw(img)
    big, small = _font(26), _font(20)
    draw.rectangle((0, 0, 900, 56), fill=(22, 58, 110))
    draw.text((20, 14), title, fill="white", font=big)
    boxes = [{"text": title, "box": [20, 14, *draw.textbbox((20, 14), title, font=big)[2:]]}]
    y = 90
    for line in lines:
        draw.text((30, y), line, fill="black", font=small)
        boxes.append({"text": line, "box": [30, y, *draw.textbbox((30, y), line, font=small)[2:]]})
        y += 48
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue(), boxes


def sharepoint() -> dict[str, Any]:
    list_id = gid("list:Report refresh history")
    return {
        "lists": [
            {
                "id": list_id,
                "displayName": "Report refresh history",
                "columns": {"Report": "Title", "RefreshDate": "field_1", "Rows": "field_2"},
                "items": [
                    {
                        "id": str(i),
                        "lastModifiedDateTime": f"2026-03-{20 + i:02d}T06:00:00Z",
                        "fields": {
                            "Title": "Sales Overview",
                            "field_1": f"2026-03-{20 + i:02d}",
                            "field_2": 1000 + i,
                        },
                    }
                    for i in range(1, 5)
                ],
            }
        ]
    }


def sql() -> dict[str, Any]:
    return {
        "tables": {
            "sales.fact_orders": {"count": 182344, "max_date": "2026-03-24"},
            "inv.stock_snapshot": {"count": 9120, "max_date": "2026-03-23"},
        }
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    write_json(ROOT / "emails" / "messages.json", emails())
    write_json(ROOT / "emails" / "expected_assignments.json", expected_assignments())
    write_json(ROOT / "teams" / "chats.json", teams())
    write_json(ROOT / "powerbi" / "workspace.json", powerbi())
    write_json(ROOT / "calendar" / "events.json", calendar())
    write_json(ROOT / "sharepoint" / "lists.json", sharepoint())
    write_json(ROOT / "sql" / "tables.json", sql())
    reports = ROOT / "servicenow_reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "doxis_l2_open.csv").write_bytes(report_csv())
    (reports / "doxis_l2_open.xlsx").write_bytes(report_xlsx())
    shots = ROOT / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    ocr = {}
    for name, (title, lines) in SCREENS.items():
        png, boxes = screenshot(title, lines)
        (shots / f"{name}.png").write_bytes(png)
        ocr[f"{name}.png"] = boxes
    write_json(shots / "ocr_expected.json", ocr)
    write_json(
        ROOT / "people.json",
        {
            "company_domain": CORP,
            "user": "Test User",
            "people": PEOPLE,
            "note": "All names, addresses, ids, sites and reports are made up.",
        },
    )
    print(f"fixtures written to {ROOT}")


if __name__ == "__main__":
    main()
