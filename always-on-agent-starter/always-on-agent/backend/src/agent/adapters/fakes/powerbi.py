"""Fake Power BI REST (A05)."""

import re
from datetime import date
from pathlib import Path
from typing import Any

from agent.adapters.base import Dataflow, Group, PowerBILink, Refresh, ToolSpec
from agent.adapters.fakes._base import FIXTURES_DIR, FakeAdapter, parse_time
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
        group = re.search(rf"/groups/({_GUID})", url)
        report = re.search(rf"/reports/({_GUID})", url)
        app = re.search(rf"/apps/({_GUID})", url)
        link = PowerBILink(group_id=group.group(1) if group else None)
        if report and report.group(1) in self.reports:
            link.report_id = report.group(1)
            link.dataset_id = self.reports[link.report_id]["datasetId"]
        if app and app.group(1) in self.apps:
            link.app_id = app.group(1)
            link.report_ids = list(self.apps[link.app_id]["reports"])
        if not (link.report_id or link.app_id):
            raise AdapterError(f"unknown Power BI link: {url[:120]}")
        return link

    async def refresh_history(self, dataset_id: str, top: int = 10) -> list[Refresh]:
        self._check("refresh_history")
        return [self._to_refresh(r) for r in self.refreshes.get(dataset_id, [])[:top]]

    async def refreshes_today(self, dataset_id: str, day: date) -> int:
        self._check("refreshes_today")
        return sum(1 for r in self.refreshes.get(dataset_id, []) if parse_time(r["startTime"]).date() == day)

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

    async def trigger_refresh(self, dataset_id: str) -> str:
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
        return request_id

    async def trigger_dataflow_refresh(self, dataflow_id: str) -> str:
        self._record("trigger_dataflow_refresh", dataflow_id=dataflow_id)
        return new_ulid()

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
        ]
