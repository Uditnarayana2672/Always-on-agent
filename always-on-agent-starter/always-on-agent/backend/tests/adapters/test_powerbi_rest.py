from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from agent.adapters.base import RefreshSchedule
from agent.adapters.graph_client import POWERBI_BASE
from agent.adapters.powerbi import (
    PowerBIRestAdapter,
    RefreshRefused,
    check_schedule,
    count_on_utc_day,
    parse_link,
    to_refresh,
)
from agent.core.errors import AdapterError
from tests.adapters.graph_helpers import NOW, client
from tests.adapters.graph_sim import PowerBISim

G1 = "9edaad7b-5ce2-5924-bc1d-bf9a912483a4"
R1 = "772657e6-6a15-5fdd-8441-ee2877e081ec"
A1 = "ace75f3b-b2c6-5b26-879b-6307f171e60e"
D1 = "bab6126e-511d-55fc-a827-124aa86f4a3f"


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def router() -> AsyncIterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def sim(router: respx.MockRouter) -> PowerBISim:
    sim = PowerBISim(router)
    sim.add_dataset("ds-mine")
    sim.add_dataset("ds-theirs", owner="priya.nair@corp.example")
    sim.add_dataset("ds-live", refreshable=False)
    sim.add_dataset("ds-premium", group="g-premium")
    return sim


def pbi(http: httpx.AsyncClient, now: datetime = NOW) -> PowerBIRestAdapter:
    return PowerBIRestAdapter(client(http, "powerbi", base=POWERBI_BASE), cap_per_day=8, now=lambda: now)


# ---------------------------------------------------------------- links


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            f"https://app.powerbi.com/groups/{G1}/reports/{R1}/ReportSection?experience=power-bi",
            {"group": G1, "report": R1},
        ),
        (f"https://app.powerbi.com/groups/me/reports/{R1}", {"group": "me", "report": R1}),
        (
            f"https://app.powerbi.com/groups/me/apps/{A1}/reports/{R1}/ReportSection",
            {"group": "me", "app": A1, "report": R1},
        ),
        (f"https://app.powerbi.com/apps/{A1}", {"app": A1}),
        (f"https://app.powerbi.com/groups/{G1}/dashboards/{R1}", {"group": G1, "dashboard": R1}),
        (f"https://app.powerbi.com/groups/{G1}/datasets/{D1}/details", {"group": G1, "dataset": D1}),
        (
            f"https://app.powerbi.com/reportEmbed?reportId={R1}&groupId={G1}&autoAuth=true",
            {"group": G1, "report": R1},
        ),
        # Upper-case ids, trailing punctuation from a chat message, angle brackets from Outlook.
        (
            f"<https://app.powerbi.com/groups/{G1.upper()}/reports/{R1.upper()}>).",
            {"group": G1, "report": R1},
        ),
    ],
)
def test_link_formats(url: str, expected: dict[str, str]) -> None:
    assert parse_link(url) == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://app.powerbi.com/links/AbCdEf?ctid=x", "short Power BI links"),
        (f"https://evil.example/groups/{G1}/reports/{R1}", "not a Power BI link"),
        ("https://app.powerbi.com/home", "does not point"),
        (f"https://app.powerbi.com.evil.example/groups/{G1}/reports/{R1}", "not a Power BI link"),
    ],
)
def test_bad_links(url: str, message: str) -> None:
    with pytest.raises(AdapterError, match=message):
        parse_link(url)


async def test_resolve_report_link_to_dataset(http: httpx.AsyncClient, sim: PowerBISim) -> None:
    sim.reports[R1] = {"id": R1, "name": "Sales Overview", "datasetId": D1.upper()}
    link = await pbi(http).resolve_link(f"https://app.powerbi.com/groups/{G1}/reports/{R1}")
    assert link.dataset_id == D1 and not link.needs_choice


async def test_app_with_several_datasets_needs_a_choice(
    http: httpx.AsyncClient, router: respx.MockRouter, sim: PowerBISim
) -> None:
    router.get(f"{POWERBI_BASE}/apps/{A1}/reports").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"id": "r-a", "name": "Sales", "datasetId": "d-a"},
                    {"id": "r-b", "name": "Stock", "datasetId": "d-b"},
                ]
            },
        )
    )
    link = await pbi(http).resolve_link(f"https://app.powerbi.com/apps/{A1}")
    assert (
        link.needs_choice
        and link.dataset_id is None
        and [c.name for c in link.candidates] == ["Sales", "Stock"]
    )


# ---------------------------------------------------------------- cap


def test_cap_counts_the_utc_day_not_the_ist_day() -> None:
    history = [
        to_refresh(
            {
                "requestId": "late",
                "status": "Completed",
                "startTime": "2026-03-24T20:00:00Z",
                "endTime": "2026-03-24T20:10:00Z",
            }
        ),
        *[
            to_refresh({"requestId": f"r{i}", "status": "Completed", "startTime": f"2026-03-25T0{i}:00:00Z"})
            for i in range(7)
        ],
    ]
    # 20:00 UTC on the 24th is 01:30 IST on the 25th: same IST day, previous UTC day.
    ist_day = sum(
        1 for r in history if r.start.astimezone(ZoneInfo("Asia/Kolkata")).date() == date(2026, 3, 25)
    )
    assert ist_day == 8
    assert count_on_utc_day(history, date(2026, 3, 25)) == 7


async def test_cap_warns_on_the_last_and_refuses_after(http: httpx.AsyncClient, sim: PowerBISim) -> None:
    sim.add_refresh("ds-mine", "2026-03-24T20:00:00Z", end="2026-03-24T20:10:00Z")  # yesterday in UTC
    for hour in range(7):
        sim.add_refresh(
            "ds-mine", f"2026-03-25T0{hour // 2}:{(hour % 2) * 30:02d}:00Z", end="2026-03-25T03:59:00Z"
        )
    adapter = pbi(http)
    cap = await adapter.refresh_cap("ds-mine")
    assert (cap.used, cap.cap, cap.remaining, cap.last_one) == (7, 8, 1, True)
    assert cap.resets_at == datetime(2026, 3, 26, tzinfo=UTC)
    started = await adapter.trigger_refresh("ds-mine")
    assert started.status == "started" and started.request_id == "new-1" and "last" in (started.warning or "")
    sim.add_refresh("ds-mine", "2026-03-25T04:30:00Z", end="2026-03-25T04:31:00Z")
    with pytest.raises(RefreshRefused) as err:
        await adapter.trigger_refresh("ds-mine")
    assert err.value.kind == "cap" and "05:30" not in str(err.value) and "00:00 UTC" in str(err.value)
    assert sim.posts == ["ds-mine"]


async def test_dedicated_capacity_has_no_daily_cap(http: httpx.AsyncClient, sim: PowerBISim) -> None:
    for i in range(9):
        sim.add_refresh("ds-premium", f"2026-03-25T00:0{i}:00Z", end="2026-03-25T00:30:00Z")
    cap = await pbi(http).refresh_cap("ds-premium")
    assert cap.cap is None and not cap.at_cap
    assert (await pbi(http).trigger_refresh("ds-premium")).status == "started"


# ---------------------------------------------------------------- refusals


async def test_running_refresh_is_reported_not_doubled(http: httpx.AsyncClient, sim: PowerBISim) -> None:
    sim.add_refresh("ds-mine", "2026-03-25T04:20:00Z", status="Unknown", request_id="running-1")
    adapter = pbi(http)
    assert (await adapter.running_refresh("ds-mine")).request_id == "running-1"  # type: ignore[union-attr]
    result = await adapter.trigger_refresh("ds-mine")
    assert result.status == "already_running" and result.request_id == "running-1" and sim.posts == []


async def test_403_hands_off_with_the_owner(http: httpx.AsyncClient, sim: PowerBISim) -> None:
    sim.forbidden.add("ds-theirs")
    with pytest.raises(RefreshRefused) as err:
        await pbi(http).trigger_refresh("ds-theirs")
    assert err.value.kind == "forbidden" and err.value.owner == "priya.nair@corp.example"
    assert "priya.nair@corp.example" in str(err.value)


async def test_not_refreshable_and_unknown_datasets(http: httpx.AsyncClient, sim: PowerBISim) -> None:
    with pytest.raises(RefreshRefused) as err:
        await pbi(http).trigger_refresh("ds-live")
    assert err.value.kind == "not_refreshable"
    with pytest.raises(RefreshRefused) as unknown:
        await pbi(http).trigger_refresh("ds-nobody")
    assert unknown.value.kind == "unknown_dataset"


async def test_history_maps_errors_and_running_states(http: httpx.AsyncClient, sim: PowerBISim) -> None:
    sim.add_refresh(
        "ds-mine",
        "2026-03-25T00:30:00Z",
        status="Failed",
        end="2026-03-25T00:33:00Z",
        error="DM_GWPipeline_Gateway_TimeoutError",
    )
    sim.add_refresh("ds-mine", "2026-03-25T04:00:00Z", status="NotStarted")
    history = await pbi(http).refresh_history("ds-mine")
    assert history[0].status == "Unknown" and history[1].error_code == "DM_GWPipeline_Gateway_TimeoutError"


def test_schedule_rules() -> None:
    ok = RefreshSchedule(enabled=True, days=["Monday"], times=["07:00", "13:30"])
    check_schedule(ok, dedicated=False)
    with pytest.raises(AdapterError, match="half hour"):
        check_schedule(ok.model_copy(update={"times": ["07:15"]}), dedicated=False)
    with pytest.raises(AdapterError, match="at most 8"):
        check_schedule(ok.model_copy(update={"times": [f"{h:02d}:00" for h in range(9)]}), dedicated=False)
    check_schedule(ok.model_copy(update={"times": [f"{h:02d}:00" for h in range(9)]}), dedicated=True)
    with pytest.raises(AdapterError, match="day"):
        check_schedule(ok.model_copy(update={"days": ["Funday"]}), dedicated=False)
    with pytest.raises(AdapterError, match="needs at least"):
        check_schedule(RefreshSchedule(enabled=True), dedicated=False)
