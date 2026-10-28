"""Power BI REST (A05).

Links (resolve_link)
- app.powerbi.com (and sovereign *.powerbi.com / .de / .cn hosts) only; anything else is refused.
- /groups/<id|me>/reports/<id>, /groups/<id>/dashboards/<id>, /groups/<id>/datasets/<id>,
  /groups/<id>/apps/<id>/reports/<id>, /apps/<id>, /apps/<id>/reports/<id>, /reportEmbed?reportId=..
  Short links (/links/...) cannot be resolved without a browser: the user is asked for the full link.
- Ids are matched case-insensitively and lowercased; trailing punctuation from chat text is ignored.
- An app or dashboard behind several datasets returns candidates and needs_choice (A05: ask).

Refresh rules
- A refresh that is running (status Unknown, or NotStarted) is never doubled: trigger_refresh returns
  the running one with status already_running (E32).
- Shared capacity allows powerbi.refresh_cap_per_day refreshes per dataset per day, scheduled and
  on-demand together. The service counts the day in UTC, so do we: from 05:30 IST a new allowance
  starts. At the cap the trigger is refused; the last one left comes with a warning (E33). Dedicated
  capacity has no daily cap for API refreshes.
- 401/403 on a trigger is a hand-off naming the dataset's configuredBy owner when visible (E34).
- A dataset that is not refreshable (DirectQuery, live connection, push) is refused with that reason.
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from agent.adapters.base import (
    Dataflow,
    DataflowTransaction,
    Group,
    HealthStatus,
    PowerBILink,
    Refresh,
    RefreshCap,
    RefreshSchedule,
    RefreshTrigger,
    ReportCandidate,
    ToolSpec,
)
from agent.adapters.graph_client import Forbidden, GraphClient, NotFound
from agent.adapters.outlook_graph import parse_graph_time
from agent.core.errors import AdapterError, AgentError

log = logging.getLogger(__name__)

GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_HOSTS = re.compile(r"(^|\.)powerbi\.(com|de|cn)$|(^|\.)powerbigov\.us$", re.I)
_SEGMENT_IDS = ("groups", "reports", "dashboards", "datasets", "apps", "dataflows")
RUNNING = ("Unknown", "NotStarted", "InProgress")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_HH_MM_HALF = re.compile(r"^([01]\d|2[0-3]):(00|30)$")
MAX_SCHEDULE_TIMES = {False: 8, True: 48}  # shared, dedicated


class RefreshRefused(AdapterError):
    """trigger_refresh will not run. kind: running | cap | forbidden | not_refreshable | unknown_dataset."""

    def __init__(
        self, message: str, kind: str, owner: str | None = None, status_code: int | None = None
    ) -> None:
        super().__init__(message, retryable=False, status_code=status_code)
        self.kind = kind
        self.owner = owner


def parse_link(url: str) -> dict[str, str]:
    """Ids from a Power BI link, keyed group/report/dashboard/dataset/app/dataflow. Raises on bad links."""
    cleaned, previous = url.strip(), ""
    while cleaned != previous:  # '<https://...>).' from chat text or Outlook
        previous = cleaned
        cleaned = cleaned.strip().lstrip("<(['\"").rstrip(">).,;:!?'\"]")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not _HOSTS.search(parsed.hostname or ""):
        raise AdapterError("this is not a Power BI link")
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    lowered = [p.lower() for p in parts]
    if lowered and lowered[0] == "links":
        raise AdapterError(
            "short Power BI links cannot be opened here; paste the full link from the address bar"
        )
    ids: dict[str, str] = {}
    for index, part in enumerate(lowered[:-1]):
        if part in _SEGMENT_IDS:
            value = parts[index + 1]
            key = part[:-1]  # groups -> group
            if part == "groups" and value.lower() == "me":
                ids[key] = "me"
            elif re.fullmatch(GUID, value):
                ids.setdefault(key, value.lower())
    query = {k.lower(): v[0] for k, v in parse_qs(parsed.query).items() if v}
    for key, name in (
        ("reportid", "report"),
        ("groupid", "group"),
        ("dashboardid", "dashboard"),
        ("appid", "app"),
    ):
        value = query.get(key, "")
        if re.fullmatch(GUID, value):
            ids.setdefault(name, value.lower())
    if not ({"report", "dashboard", "dataset", "app", "dataflow"} & set(ids)):
        raise AdapterError("the link does not point to a report, dashboard, dataset or app")
    return ids


def utc_day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), UTC)
    return start, start + timedelta(days=1)


def count_on_utc_day(history: list[Refresh], day: date) -> int:
    """Refreshes that started on this UTC day: every type and outcome counts against the cap."""
    start, end = utc_day_window(day)
    return sum(1 for r in history if start <= r.start < end)


def to_refresh(raw: dict[str, Any]) -> Refresh:
    error = None
    if raw.get("serviceExceptionJson"):
        try:
            error = json.loads(raw["serviceExceptionJson"]).get("errorCode")
        except (ValueError, AttributeError):
            error = "UnparsedError"
    status = str(raw.get("status") or "Unknown")
    if status in ("NotStarted", "InProgress"):
        status = "Unknown"  # the API's 'running'
    return Refresh(
        request_id=str(raw.get("requestId") or raw.get("id") or ""),
        status=status if status in ("Completed", "Failed", "Unknown", "Disabled", "Cancelled") else "Unknown",  # type: ignore[arg-type]
        refresh_type=str(raw.get("refreshType") or ""),
        start=parse_graph_time(str(raw["startTime"])),
        end=parse_graph_time(str(raw["endTime"])) if raw.get("endTime") else None,
        error_code=error,
    )


class PowerBIRestAdapter:
    """Implements PowerBIAdapter over the Power BI REST API (delegated, the user's own permissions)."""

    name = "powerbi"

    def __init__(self, client: GraphClient, cap_per_day: int, now: Callable[[], datetime]) -> None:
        self._g = client
        self._cap = cap_per_day
        self._now = now
        self._datasets: dict[str, dict[str, Any]] = {}  # dataset id -> {groupId, name, configuredBy, ...}
        self._dataflows: dict[str, str] = {}  # dataflow id -> group id
        self._groups: dict[str, Group] = {}

    async def health(self) -> HealthStatus:
        try:
            await self._g.get("groups", {"$top": "1"})
        except AgentError as exc:
            return HealthStatus(connector=self.name, status="down", detail=str(exc)[:200])
        return HealthStatus(connector=self.name, status="ok", detail="Power BI reachable")

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="get_refresh_history", mode="silent", description="Read refresh history"),
            ToolSpec(name="trigger_refresh", mode="ask", description="Refresh a dataset"),
            ToolSpec(name="trigger_dataflow_refresh", mode="ask", description="Refresh a dataflow"),
            ToolSpec(name="update_refresh_schedule", mode="ask", description="Change a refresh schedule"),
        ]

    # ------------------------------------------------------------ workspaces and lookups

    async def groups(self) -> list[Group]:
        raw = await self._g.get_all("groups")
        capacities = {c["id"]: c.get("sku") for c in (await self._g.get_all("capacities"))}
        out = [
            Group(
                id=str(g["id"]).lower(),
                name=str(g.get("name") or ""),
                dedicated_capacity=bool(g.get("isOnDedicatedCapacity")),
                capacity_sku=capacities.get(g.get("capacityId", "")),
            )
            for g in raw
        ]
        self._groups = {g.id: g for g in out}
        return out

    async def _group_prefix(self, group_id: str | None) -> str:
        return "" if group_id in (None, "", "me") else f"groups/{group_id}/"

    async def _dataset(self, dataset_id: str) -> dict[str, Any]:
        """Dataset metadata with its workspace; searched across workspaces once, then cached."""
        key = dataset_id.lower()
        if key in self._datasets:
            return self._datasets[key]
        try:
            raw = await self._g.get(f"datasets/{key}")  # My workspace
            raw["groupId"] = "me"
            self._datasets[key] = raw
            return raw
        except (NotFound, Forbidden):
            pass
        for group in self._groups.values() or await self.groups():
            for raw in await self._g.get_all(f"groups/{group.id}/datasets"):
                raw["groupId"] = group.id
                self._datasets[str(raw["id"]).lower()] = raw
            if key in self._datasets:
                return self._datasets[key]
        raise RefreshRefused("this dataset is not visible to you", kind="unknown_dataset")

    async def dataset_owner(self, dataset_id: str) -> str | None:
        raw = await self._dataset(dataset_id)
        owner = raw.get("configuredBy")
        return str(owner).lower() if owner else None

    async def _dedicated(self, group_id: str) -> bool:
        if group_id == "me":
            return False
        if group_id not in self._groups:
            await self.groups()
        group = self._groups.get(group_id)
        return bool(group and group.dedicated_capacity)

    # ------------------------------------------------------------ links

    async def resolve_link(self, url: str) -> PowerBILink:
        ids = parse_link(url)
        group = ids.get("group")
        link = PowerBILink(group_id=group, app_id=ids.get("app"), dashboard_id=ids.get("dashboard"))
        prefix = await self._group_prefix(group)
        if "dataset" in ids:
            link.dataset_id = ids["dataset"]
            return link
        if "report" in ids:
            path = (
                f"apps/{ids['app']}/reports/{ids['report']}"
                if "app" in ids
                else f"{prefix}reports/{ids['report']}"
            )
            report = await self._g.get(path)
            link.report_id = ids["report"]
            link.dataset_id = str(report.get("datasetId") or "").lower() or None
            link.report_ids = [ids["report"]]
            link.candidates = [
                ReportCandidate(
                    report_id=ids["report"],
                    name=str(report.get("name") or ""),
                    dataset_id=link.dataset_id,
                    group_id=group,
                )
            ]
            return link
        if "dashboard" in ids:
            tiles = await self._g.get_all(f"{prefix}dashboards/{ids['dashboard']}/tiles")
            seen: dict[str, ReportCandidate] = {}
            for tile in tiles:
                report_id = str(tile.get("reportId") or "").lower()
                dataset_id = str(tile.get("datasetId") or "").lower() or None
                if report_id and report_id not in seen:
                    seen[report_id] = ReportCandidate(
                        report_id=report_id,
                        name=str(tile.get("title") or ""),
                        dataset_id=dataset_id,
                        group_id=group,
                    )
            link.candidates = list(seen.values())
        elif "app" in ids:
            reports = await self._g.get_all(f"apps/{ids['app']}/reports")
            link.candidates = [
                ReportCandidate(
                    report_id=str(r["id"]).lower(),
                    name=str(r.get("name") or ""),
                    dataset_id=str(r.get("datasetId") or "").lower() or None,
                )
                for r in reports
            ]
        link.report_ids = [c.report_id for c in link.candidates]
        datasets = {c.dataset_id for c in link.candidates if c.dataset_id}
        if len(datasets) == 1:
            link.dataset_id = datasets.pop()
        return link

    # ------------------------------------------------------------ refresh reads

    async def refresh_history(self, dataset_id: str, top: int = 60) -> list[Refresh]:
        raw = await self._dataset(dataset_id)
        prefix = await self._group_prefix(raw["groupId"])
        body = await self._g.get(
            f"{prefix}datasets/{dataset_id.lower()}/refreshes", {"$top": str(min(top, 60))}
        )
        return [to_refresh(r) for r in body.get("value", [])]

    async def running_refresh(self, dataset_id: str) -> Refresh | None:
        for refresh in await self.refresh_history(dataset_id, top=5):
            if refresh.status == "Unknown":
                return refresh
        return None

    async def refreshes_today(self, dataset_id: str, day: date) -> int:
        """Refreshes on this UTC day (the service's day for the cap)."""
        return count_on_utc_day(await self.refresh_history(dataset_id, top=60), day)

    async def refresh_cap(self, dataset_id: str) -> RefreshCap:
        raw = await self._dataset(dataset_id)
        day = self._now().astimezone(UTC).date()
        used = await self.refreshes_today(dataset_id, day)
        dedicated = await self._dedicated(str(raw["groupId"]))
        return RefreshCap(
            used=used, cap=None if dedicated else self._cap, day=day, resets_at=utc_day_window(day)[1]
        )

    async def upstream_dataflows(self, dataset_id: str) -> list[Dataflow]:
        raw = await self._dataset(dataset_id)
        group = str(raw["groupId"])
        if group == "me":
            return []
        links = await self._g.get_all(f"groups/{group}/datasets/upstreamDataflows")
        flow_ids = [
            (str(link.get("dataflowObjectId")).lower(), str(link.get("workspaceObjectId") or group).lower())
            for link in links
            if str(link.get("datasetObjectId", "")).lower() == dataset_id.lower()
        ]
        out = []
        for flow_id, flow_group in flow_ids:
            self._dataflows[flow_id] = flow_group
            names = {
                str(d["objectId"]).lower(): d.get("name")
                for d in await self._g.get_all(f"groups/{flow_group}/dataflows")
            }
            last = next(iter(await self.dataflow_transactions(flow_id, top=1)), None)
            out.append(
                Dataflow(
                    id=flow_id,
                    name=str(names.get(flow_id) or flow_id),
                    last_status=last.status if last else None,
                    last_end=last.end if last else None,
                )
            )
        return out

    async def dataflow_transactions(self, dataflow_id: str, top: int = 10) -> list[DataflowTransaction]:
        group = self._dataflows.get(dataflow_id.lower())
        if group is None:
            raise AdapterError("dataflow workspace unknown; read the dataset's upstream dataflows first")
        body = await self._g.get(
            f"groups/{group}/dataflows/{dataflow_id.lower()}/transactions", {"$top": str(top)}
        )
        return [
            DataflowTransaction(
                id=str(t.get("id") or ""),
                status=str(t.get("status") or ""),
                refresh_type=str(t.get("refreshType") or ""),
                start=parse_graph_time(str(t["startTime"])) if t.get("startTime") else None,
                end=parse_graph_time(str(t["endTime"])) if t.get("endTime") else None,
            )
            for t in body.get("value", [])
        ]

    async def refresh_schedule(self, dataset_id: str) -> RefreshSchedule:
        raw = await self._dataset(dataset_id)
        prefix = await self._group_prefix(raw["groupId"])
        body = await self._g.get(f"{prefix}datasets/{dataset_id.lower()}/refreshSchedule")
        return RefreshSchedule(
            enabled=bool(body.get("enabled")),
            days=list(body.get("days") or []),
            times=list(body.get("times") or []),
            local_time_zone=str(body.get("localTimeZoneId") or "UTC"),
            notify_option=str(body.get("notifyOption") or "MailOnFailure"),
        )

    # ------------------------------------------------------------ writes (via the outbox only)

    async def trigger_refresh(self, dataset_id: str) -> RefreshTrigger:
        raw = await self._dataset(dataset_id)
        if raw.get("isRefreshable") is False:
            raise RefreshRefused(
                "this dataset cannot be refreshed (DirectQuery, live or push); nothing to trigger",
                kind="not_refreshable",
            )
        running = await self.running_refresh(dataset_id)
        if running is not None:
            return RefreshTrigger(status="already_running", request_id=running.request_id)
        cap = await self.refresh_cap(dataset_id)
        if cap.at_cap:
            raise RefreshRefused(
                f"all {cap.cap} refreshes for today are used; the count resets at {cap.resets_at:%H:%M} UTC",
                kind="cap",
            )
        warning = "this is the last refresh allowed today" if cap.last_one else None
        prefix = await self._group_prefix(raw["groupId"])
        try:
            response = await self._g.post(
                f"{prefix}datasets/{dataset_id.lower()}/refreshes", {"notifyOption": "NoNotification"}
            )
        except Forbidden as exc:
            owner = await self.dataset_owner(dataset_id)
            raise RefreshRefused(
                "you do not have permission to refresh this dataset"
                + (f"; its owner is {owner}" if owner else ""),
                kind="forbidden",
                owner=owner,
                status_code=403,
            ) from exc
        except AdapterError as exc:
            if exc.status_code in (400, 409) and "progress" in str(exc).lower():
                again = await self.running_refresh(dataset_id)
                return RefreshTrigger(
                    status="already_running", request_id=again.request_id if again else None
                )
            if exc.status_code == 429 or "limit" in str(exc).lower():
                raise RefreshRefused(
                    f"Power BI refused the refresh: {exc}", kind="cap", status_code=exc.status_code
                ) from exc
            raise
        headers = {k.lower(): v for k, v in (response.get("_headers") or {}).items()}
        request_id = headers.get("requestid") or headers.get("x-ms-request-id")
        location = headers.get("location", "")
        if not request_id and location:
            request_id = location.rstrip("/").rsplit("/", 1)[-1]
        return RefreshTrigger(status="started", request_id=request_id, warning=warning)

    async def trigger_dataflow_refresh(self, dataflow_id: str) -> RefreshTrigger:
        flow = dataflow_id.lower()
        latest = next(iter(await self.dataflow_transactions(flow, top=1)), None)
        if (
            latest is not None
            and latest.status.lower() in ("inprogress", "notstarted", "unknown")
            and latest.end is None
        ):
            return RefreshTrigger(status="already_running", request_id=latest.id)
        group = self._dataflows[flow]
        try:
            await self._g.post(
                f"groups/{group}/dataflows/{flow}/refreshes", {"notifyOption": "NoNotification"}
            )
        except Forbidden as exc:
            raise RefreshRefused(
                "you do not have permission to refresh this dataflow", kind="forbidden", status_code=403
            ) from exc
        return RefreshTrigger(status="started", request_id=None)

    async def update_refresh_schedule(self, dataset_id: str, schedule: RefreshSchedule) -> None:
        raw = await self._dataset(dataset_id)
        dedicated = await self._dedicated(str(raw["groupId"]))
        check_schedule(schedule, dedicated)
        prefix = await self._group_prefix(raw["groupId"])
        try:
            await self._g.patch(
                f"{prefix}datasets/{dataset_id.lower()}/refreshSchedule",
                {
                    "value": {
                        "enabled": schedule.enabled,
                        "days": schedule.days,
                        "times": schedule.times,
                        "localTimeZoneId": schedule.local_time_zone,
                        "notifyOption": schedule.notify_option,
                    }
                },
            )
        except Forbidden as exc:
            owner = await self.dataset_owner(dataset_id)
            raise RefreshRefused(
                "you do not have permission to change this schedule"
                + (f"; its owner is {owner}" if owner else ""),
                kind="forbidden",
                owner=owner,
                status_code=403,
            ) from exc


def check_schedule(schedule: RefreshSchedule, dedicated: bool) -> None:
    bad_days = [d for d in schedule.days if d not in DAYS]
    if bad_days:
        raise AdapterError(f"unknown day(s): {', '.join(bad_days)}")
    bad_times = [t for t in schedule.times if not _HH_MM_HALF.match(t)]
    if bad_times:
        raise AdapterError(f"times must be on the hour or half hour (HH:00 or HH:30): {', '.join(bad_times)}")
    if len(set(schedule.times)) != len(schedule.times):
        raise AdapterError("the same time is listed twice")
    limit = MAX_SCHEDULE_TIMES[dedicated]
    if len(schedule.times) > limit:
        raise AdapterError(f"at most {limit} refresh times a day on this capacity")
    if schedule.enabled and (not schedule.days or not schedule.times):
        raise AdapterError("an enabled schedule needs at least one day and one time")
