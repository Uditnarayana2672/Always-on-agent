"""Fake Power BI REST (A05)."""

from datetime import date
from pathlib import Path
from typing import Any

from agent.adapters.base import (
    Dataflow,
    DataflowTransaction,
    Group,
    PowerBILink,
    Refresh,
    RefreshCap,
    RefreshSchedule,
    RefreshTrigger,
    ReportCandidate,
    ToolSpec,
)
from agent.adapters.fakes._base import FIXTURES_DIR, FakeAdapter, parse_time
from agent.adapters.powerbi import (
    RefreshRefused,
    check_schedule,
    count_on_utc_day,
    parse_link,
    utc_day_window,
)
from agent.core.clock import Clock
from agent.core.errors import AdapterError
from agent.core.ids import new_ulid

_GUID = r"[0-9a-fA-F-]{36}"


class FakePowerBI(FakeAdapter):
    name = "powerbi"

    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        super().__init__(clock, fixtures)
        data = self._load("powerbi/workspace.json")
        self.data = data
        self.datasets: dict[str, dict[str, Any]] = {d["id"]: d for d in data["datasets"]}
        self.reports: dict[str, dict[str, Any]] = {r["id"]: r for r in data["reports"]}
        self.apps: dict[str, dict[str, Any]] = {a["id"]: a for a in data["apps"]}
        self.dataflows: dict[str, dict[str, Any]] = {d["objectId"]: d for d in data["dataflows"]}
        self.refreshes: dict[str, list[dict[str, Any]]] = data["refreshes"]
        self.schedules: dict[str, RefreshSchedule] = {}
        self.forbidden: set[str] = set()  # datasets the user may not refresh (tests: 403)
        self.cap_per_day = 8

    def deliver_refresh(self, dataset_id: str, raw: dict[str, Any]) -> None:
        """A refresh starts or finishes now (tests). Newest first, like the API."""
        history = self.refreshes.setdefault(dataset_id, [])
        history[:] = [r for r in history if r["requestId"] != raw["requestId"]]
        history.insert(0, raw)

    async def groups(self) -> list[Group]:
        self._check("groups")
        skus = {c["id"]: c["sku"] for c in self.data["capacities"]}
        return [
            Group(
                id=g["id"],
                name=g["name"],
                dedicated_capacity=g["isOnDedicatedCapacity"],
                capacity_sku=skus.get(g.get("capacityId", "")),
            )
            for g in self.data["groups"]
        ]

    async def resolve_link(self, url: str) -> PowerBILink:
        self._check("resolve_link")
        ids = parse_link(url)
        link = PowerBILink(
            group_id=ids.get("group"), app_id=ids.get("app"), dashboard_id=ids.get("dashboard")
        )
        if "dataset" in ids:
            link.dataset_id = ids["dataset"]
            return link
        if "report" in ids:
            report = self.reports.get(ids["report"])
            if report is None:
                raise AdapterError(f"powerbi: report not found: {ids['report']}", status_code=404)
            link.report_id = ids["report"]
            link.dataset_id = report["datasetId"]
            link.report_ids = [ids["report"]]
            link.candidates = [self._candidate(ids["report"])]
            return link
        if "app" in ids:
            app = self.apps.get(ids["app"])
            if app is None:
                raise AdapterError(f"powerbi: app not found: {ids['app']}", status_code=404)
            link.report_ids = list(app["reports"])
            link.candidates = [self._candidate(r) for r in app["reports"]]
        datasets = {c.dataset_id for c in link.candidates if c.dataset_id}
        if len(datasets) == 1:
            link.dataset_id = datasets.pop()
        return link

    def _candidate(self, report_id: str) -> ReportCandidate:
        report = self.reports[report_id]
        return ReportCandidate(
            report_id=report_id,
            name=report["name"],
            dataset_id=report["datasetId"],
            group_id=report["groupId"],
        )

    async def refresh_history(self, dataset_id: str, top: int = 60) -> list[Refresh]:
        self._check("refresh_history")
        return [self._to_refresh(r) for r in self.refreshes.get(dataset_id, [])[:top]]

    async def running_refresh(self, dataset_id: str) -> Refresh | None:
        return next((r for r in await self.refresh_history(dataset_id, 5) if r.status == "Unknown"), None)

    async def refreshes_today(self, dataset_id: str, day: date) -> int:
        """Refreshes that started on this UTC day (the service's day for the cap)."""
        return count_on_utc_day(await self.refresh_history(dataset_id), day)

    async def refresh_cap(self, dataset_id: str) -> RefreshCap:
        day = self.clock.now_utc().date()
        dedicated = {g.id: g.dedicated_capacity for g in await self.groups()}
        group = self.datasets[dataset_id]["groupId"]
        return RefreshCap(
            used=await self.refreshes_today(dataset_id, day),
            cap=None if dedicated.get(group) else self.cap_per_day,
            day=day,
            resets_at=utc_day_window(day)[1],
        )

    async def dataset_owner(self, dataset_id: str) -> str | None:
        owner = self.datasets[dataset_id].get("configuredBy")
        return str(owner).lower() if owner else None

    async def upstream_dataflows(self, dataset_id: str) -> list[Dataflow]:
        self._check("upstream_dataflows")
        out = []
        for flow_id in self.datasets[dataset_id].get("upstreamDataflows", []):
            flow = self.dataflows[flow_id]
            last = flow["transactions"][0] if flow["transactions"] else None
            out.append(
                Dataflow(
                    id=flow_id,
                    name=flow["name"],
                    last_status=last["status"] if last else None,
                    last_end=parse_time(last["endTime"]) if last and last.get("endTime") else None,
                )
            )
        return out

    async def dataflow_transactions(self, dataflow_id: str, top: int = 10) -> list[DataflowTransaction]:
        self._check("dataflow_transactions")
        return [
            DataflowTransaction(
                id=t["id"],
                status=t["status"],
                start=parse_time(t["startTime"]) if t.get("startTime") else None,
                end=parse_time(t["endTime"]) if t.get("endTime") else None,
            )
            for t in self.dataflows[dataflow_id]["transactions"][:top]
        ]

    async def refresh_schedule(self, dataset_id: str) -> RefreshSchedule:
        self._check("refresh_schedule")
        return self.schedules.get(dataset_id, RefreshSchedule(enabled=False))

    async def trigger_refresh(self, dataset_id: str) -> RefreshTrigger:
        """Same rules as the real adapter: no double refresh, the UTC-day cap, 403 hands off."""
        dataset = self.datasets.get(dataset_id)
        if dataset is None:
            raise RefreshRefused("this dataset is not visible to you", kind="unknown_dataset")
        if dataset.get("isRefreshable") is False:
            raise RefreshRefused(
                "this dataset cannot be refreshed; nothing to trigger", kind="not_refreshable"
            )
        running = await self.running_refresh(dataset_id)
        if running is not None:
            return RefreshTrigger(status="already_running", request_id=running.request_id)
        cap = await self.refresh_cap(dataset_id)
        if cap.at_cap:
            raise RefreshRefused(f"all {cap.cap} refreshes for today are used", kind="cap")
        if dataset_id in self.forbidden:
            owner = await self.dataset_owner(dataset_id)
            raise RefreshRefused(
                "you do not have permission to refresh this dataset"
                + (f"; its owner is {owner}" if owner else ""),
                kind="forbidden",
                owner=owner,
                status_code=403,
            )
        self._record("trigger_refresh", dataset_id=dataset_id)
        request_id = new_ulid()
        now = self.clock.now_utc().isoformat().replace("+00:00", "Z")
        self.deliver_refresh(
            dataset_id,
            {
                "requestId": request_id,
                "refreshType": "ViaApi",
                "status": "Unknown",
                "startTime": now,
                "endTime": None,
            },
        )
        return RefreshTrigger(
            status="started",
            request_id=request_id,
            warning="this is the last refresh allowed today" if cap.last_one else None,
        )

    async def trigger_dataflow_refresh(self, dataflow_id: str) -> RefreshTrigger:
        latest = next(iter(await self.dataflow_transactions(dataflow_id, 1)), None)
        if (
            latest is not None
            and latest.end is None
            and latest.status.lower() in ("inprogress", "notstarted")
        ):
            return RefreshTrigger(status="already_running", request_id=latest.id)
        self._record("trigger_dataflow_refresh", dataflow_id=dataflow_id)
        return RefreshTrigger(status="started", request_id=new_ulid())

    async def update_refresh_schedule(self, dataset_id: str, schedule: RefreshSchedule) -> None:
        dedicated = {g.id: g.dedicated_capacity for g in await self.groups()}
        check_schedule(schedule, bool(dedicated.get(self.datasets[dataset_id]["groupId"])))
        self._record("update_refresh_schedule", dataset_id=dataset_id, schedule=schedule.model_dump())
        self.schedules[dataset_id] = schedule

    @staticmethod
    def _to_refresh(raw: dict[str, Any]) -> Refresh:
        import json

        error = (
            json.loads(raw["serviceExceptionJson"]).get("errorCode")
            if raw.get("serviceExceptionJson")
            else None
        )
        return Refresh(
            request_id=raw["requestId"],
            status=raw["status"],
            refresh_type=raw["refreshType"],
            start=parse_time(raw["startTime"]),
            end=parse_time(raw["endTime"]) if raw.get("endTime") else None,
            error_code=error,
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="get_refresh_history", mode="silent", description="Read refresh history"),
            ToolSpec(name="trigger_refresh", mode="ask", description="Refresh a dataset"),
            ToolSpec(name="trigger_dataflow_refresh", mode="ask", description="Refresh a dataflow"),
            ToolSpec(name="update_refresh_schedule", mode="ask", description="Change a refresh schedule"),
        ]
