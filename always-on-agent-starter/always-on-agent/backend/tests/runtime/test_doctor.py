import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from agent.core.auth import GraphAuth
from agent.core.clock import FrozenClock
from agent.core.config import CONFIG_DIR, Config, load_config
from agent.core.errors import ConfigError, NeedsSignIn
from agent.model.ica_client import IcaClient
from agent.runtime import doctor as doctor_module
from agent.runtime.doctor import GRAPH, POWERBI, SLACK, TEST_TEXT, CheckResult, Doctor, worst

ICA = "https://ica.test/ica/v1/chat-models"


def make_config(**sections: dict[str, Any]) -> Config:
    data = load_config(CONFIG_DIR / "config.example.yaml").model_dump()
    data["models"]["ids"] = {"haiku": "gemma-4-26b-a4b-it", "sonnet": "claude-sonnet-5", "opus": ""}
    for key, values in sections.items():
        data[key].update(values)
    return Config.model_validate(data)


class FakeAuth:
    def __init__(self, signed_in: bool = True) -> None:
        self.signed_in = signed_in
        self.sign_ins = 0

    def get_token(self, scopes: list[str]) -> str:
        if not self.signed_in:
            raise NeedsSignIn("no account")
        return "graph-token" if "graph" in scopes[0] else "pbi-token"


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def make_doctor(
    http: httpx.AsyncClient, clock: FrozenClock, config: Config | None = None, **kwargs: Any
) -> Doctor:
    config = config or make_config(graph={"tenant_id": "t", "client_id": "c"})
    defaults: dict[str, Any] = {
        "auth_factory": lambda c: FakeAuth(),
        "ica_factory": lambda c: IcaClient(ICA, "k", c.models.ids.model_dump(), max_attempts=1),
        "secret": lambda name: {"slack_bot_token": "xoxb-fake", "slack_app_token": "xapp-fake"}[name],
        "sign_in": None,
    }
    defaults.update(kwargs)
    return Doctor(config, clock, http=http, **defaults)


def mock_ica(router: respx.Router) -> None:
    router.get(f"{ICA}/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "gemma-4-26b-a4b-it"}, {"id": "claude-sonnet-5"}]}
        )
    )

    def chat(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model == "claude-sonnet-5":
            return httpx.Response(400, json={"detail": "You've used all of your Advantage credits"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    router.post(f"{ICA}/chat/completions").mock(side_effect=chat)


def mock_graph_and_pbi(router: respx.Router) -> None:
    router.get(f"{GRAPH}/me").mock(return_value=httpx.Response(200, json={"id": "me"}))
    router.get(f"{GRAPH}/me/messages").mock(return_value=httpx.Response(200, json={"value": [{"id": "m"}]}))
    router.get(f"{GRAPH}/me/chats").mock(return_value=httpx.Response(403, text="Forbidden"))
    router.get(f"{GRAPH}/me/calendarView").mock(return_value=httpx.Response(200, json={"value": [{}, {}]}))
    router.get(f"{POWERBI}/groups").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"id": "g1", "isOnDedicatedCapacity": False},
                    {"id": "g2", "isOnDedicatedCapacity": True},
                ]
            },
        )
    )
    router.get(f"{POWERBI}/groups/g1/datasets").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "d1", "isRefreshable": True}]})
    )
    router.get(f"{POWERBI}/groups/g1/datasets/d1/refreshes").mock(
        return_value=httpx.Response(200, json={"value": [{"status": "Failed"}]})
    )


def mock_slack(router: respx.Router) -> None:
    router.post(f"{SLACK}/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": True, "team": "Fake"})
    )
    router.post(f"{SLACK}/apps.connections.open").mock(
        return_value=httpx.Response(200, json={"ok": True, "url": "wss://x"})
    )
    router.post(f"{SLACK}/conversations.open").mock(
        return_value=httpx.Response(200, json={"ok": True, "channel": {"id": "D1"}})
    )
    router.post(f"{SLACK}/chat.postMessage").mock(return_value=httpx.Response(200, json={"ok": True}))


def by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {r.name: r for r in results}


async def test_ica_check_reports_each_model(http: httpx.AsyncClient, clock: FrozenClock) -> None:
    with respx.mock as router:
        mock_ica(router)
        results = by_name(await make_doctor(http, clock).check_ica())
    assert results["ICA reachable"].status == "OK"
    assert results["ICA model haiku (gemma-4-26b-a4b-it)"].status == "OK"
    sonnet = results["ICA model sonnet (claude-sonnet-5)"]
    assert sonnet.status == "FAIL" and "credits" in sonnet.fix


async def test_ica_not_configured(http: httpx.AsyncClient, clock: FrozenClock) -> None:
    def broken(_: Config) -> IcaClient:
        raise ConfigError("environment variable ICA_BASE_URL is not set")

    results = await make_doctor(http, clock, ica_factory=broken).check_ica()
    assert results[0].status == "FAIL" and "agentd secret set ica" in results[0].fix


async def test_no_app_registration_writes_it_request(
    http: httpx.AsyncClient, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_module, "IT_REQUEST", tmp_path / "it-request.md")
    doc = make_doctor(http, clock, config=make_config(), auth_factory=GraphAuth)
    graph_token, pbi_token, results = doc.graph_tokens()
    assert graph_token is None and pbi_token is None
    assert all(r.status == "WARN" for r in results)
    request = (tmp_path / "it-request.md").read_text(encoding="utf-8")
    assert "Mail.ReadWrite" in request and "Dataset.ReadWrite.All" in request and "secret" in request.lower()


async def test_sign_in_needed_without_prompt_fails_cleanly(
    http: httpx.AsyncClient, clock: FrozenClock
) -> None:
    doc = make_doctor(http, clock, auth_factory=lambda c: FakeAuth(signed_in=False))
    graph_token, _, results = doc.graph_tokens()
    assert graph_token is None and all(r.status == "FAIL" for r in results)


async def test_sign_in_callback_used_when_needed(http: httpx.AsyncClient, clock: FrozenClock) -> None:
    calls: list[list[str]] = []

    def sign_in(auth: Any, scopes: list[str]) -> str:
        calls.append(scopes)
        return "fresh"

    doc = make_doctor(http, clock, auth_factory=lambda c: FakeAuth(signed_in=False), sign_in=sign_in)
    graph_token, pbi_token, _ = doc.graph_tokens()
    assert (graph_token, pbi_token) == ("fresh", "fresh")
    assert calls[0][0].startswith("https://graph.microsoft.com/")
    assert calls[1][0].startswith("https://analysis.windows.net/powerbi/api/")


async def test_graph_and_powerbi_reads(http: httpx.AsyncClient, clock: FrozenClock) -> None:
    with respx.mock as router:
        mock_graph_and_pbi(router)
        doc = make_doctor(http, clock)
        graph = by_name(await doc.check_graph("graph-token"))
        pbi = by_name(await doc.check_powerbi("pbi-token"))
        assert router.calls.last.request.headers["authorization"] == "Bearer pbi-token"
    assert graph["Graph /me"].status == "OK"
    assert graph["Graph today's calendar"].detail == "2 events today"
    chats = graph["Graph read 1 chat"]
    assert chats.status == "FAIL" and "consent" in chats.fix
    assert "1 dedicated capacity, 1 shared" in pbi["Power BI groups"].detail
    assert pbi["Power BI refresh history"].detail == "read; last status Failed"


async def test_read_only_run_sends_nothing(http: httpx.AsyncClient, clock: FrozenClock) -> None:
    config = make_config(graph={"tenant_id": "t", "client_id": "c"}, slack={"user_id": "U123"})
    with respx.mock(assert_all_called=False) as router:
        mock_slack(router)
        teams_post = router.post(f"{GRAPH}/me/chats/48:notes/messages")
        doc = make_doctor(http, clock, config=config)
        teams = await doc.check_teams_self("graph-token")
        slack = by_name(await doc.check_slack())
        assert teams_post.call_count == 0
        sent_paths = [call.request.url.path for call in router.calls]
        assert not any(p.endswith(("chat.postMessage", "conversations.open")) for p in sent_paths)
    assert teams[0].status == "WARN" and "--send-tests" in teams[0].fix
    assert slack["Slack Socket Mode"].status == "OK"
    assert slack["Slack DM to you"].status == "WARN"


async def test_send_tests_posts_only_the_test_text(http: httpx.AsyncClient, clock: FrozenClock) -> None:
    config = make_config(graph={"tenant_id": "t", "client_id": "c"}, slack={"user_id": "U123"})
    with respx.mock as router:
        mock_slack(router)
        teams_post = router.post(f"{GRAPH}/me/chats/48:notes/messages").mock(
            return_value=httpx.Response(201, json={})
        )
        doc = make_doctor(http, clock, config=config, send_tests=True)
        teams = await doc.check_teams_self("graph-token")
        slack = by_name(await doc.check_slack())
        assert json.loads(teams_post.calls.last.request.content) == {"body": {"content": TEST_TEXT}}
        sent = router.calls.last.request
        assert (
            sent.url.path.endswith("chat.postMessage")
            and TEST_TEXT.replace(" ", "+") in sent.content.decode()
        )
    assert teams[0].status == "OK" and slack["Slack DM to you"].status == "OK"


async def test_slack_tokens_missing(http: httpx.AsyncClient, clock: FrozenClock) -> None:
    def no_secret(name: str) -> str:
        raise ConfigError("not set")

    results = await make_doctor(http, clock, secret=no_secret).check_slack()
    assert results[0].status == "WARN" and "slack_app_token" in results[0].detail


async def test_network_failure_suggests_proxy(http: httpx.AsyncClient, clock: FrozenClock) -> None:
    with respx.mock as router:
        router.get(url__regex=r"https://graph\.microsoft\.com/.*").mock(
            side_effect=httpx.ConnectError("refused")
        )
        router.get(url__regex=r"https://.*").mock(return_value=httpx.Response(401))
        results = by_name(await make_doctor(http, clock).check_network())
    assert results["network graph.microsoft.com"].status == "FAIL"
    assert "HTTPS_PROXY" in results["network graph.microsoft.com"].fix
    assert results["network slack.com"].status == "OK"


POWERCFG = """Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (Sleep after)
    Current AC Power Setting Index: 0x00000384
    Current DC Power Setting Index: 0x0000012c
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only settings")
async def test_laptop_settings_warn_on_short_timers(http: httpx.AsyncClient, clock: FrozenClock) -> None:
    registry = {"InactivityTimeoutSecs": "600"}
    doc = make_doctor(
        http, clock, run_command=lambda args: POWERCFG, read_registry=lambda path, value: registry.get(value)
    )
    result = doc.check_laptop()[0]
    assert result.status == "WARN"
    assert (
        "sleep AC 15 min / battery 5 min" in result.detail and "lock after 10 min (policy)" in result.detail
    )


def test_worst_result_per_connector() -> None:
    results = [
        CheckResult(name="a", connector="graph", status="OK"),
        CheckResult(name="b", connector="graph", status="FAIL", detail="x"),
        CheckResult(name="c", connector="slack", status="WARN"),
    ]
    summary = worst(results)
    assert summary["graph"].name == "b" and summary["slack"].status == "WARN"
