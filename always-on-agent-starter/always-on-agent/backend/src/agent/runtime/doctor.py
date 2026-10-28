"""agentd doctor: read-only connectivity checks with a fix for each problem (F18, F17).

Doctor never sends anything, except with --send-tests: one Teams self-note and one Slack DM,
each with the text TEST_TEXT.
"""

import contextlib
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from agent.core.auth import GraphAuth, graph_scopes, powerbi_scopes
from agent.core.clock import Clock
from agent.core.config import Config, load_config, load_env_file
from agent.core.errors import AgentError, ConfigError, NeedsSignIn
from agent.core.secrets import get_secret
from agent.model.ica_client import IcaClient

Status = Literal["OK", "WARN", "FAIL"]
TEST_TEXT = "Always-On Agent connectivity test"
GRAPH = "https://graph.microsoft.com/v1.0"
POWERBI = "https://api.powerbi.com/v1.0/myorg"
SLACK = "https://slack.com/api"
NOTES_TO_SELF_CHAT = "48:notes"
IT_REQUEST = Path(__file__).resolve().parents[4] / "docs" / "it-request-entra-app.md"
HOSTS = ["graph.microsoft.com", "login.microsoftonline.com", "api.powerbi.com", "slack.com"]


class CheckResult(BaseModel):
    name: str
    connector: str
    status: Status
    detail: str = ""
    fix: str = ""

    def line(self) -> str:
        text = f"[{self.status:4}] {self.name}: {self.detail}"
        return text + (f"\n         fix: {self.fix}" if self.fix and self.status != "OK" else "")


def _ok(name: str, connector: str, detail: str) -> CheckResult:
    return CheckResult(name=name, connector=connector, status="OK", detail=detail)


def _warn(name: str, connector: str, detail: str, fix: str = "") -> CheckResult:
    return CheckResult(name=name, connector=connector, status="WARN", detail=detail, fix=fix)


def _fail(name: str, connector: str, detail: str, fix: str = "") -> CheckResult:
    return CheckResult(name=name, connector=connector, status="FAIL", detail=detail, fix=fix)


SignIn = Callable[[GraphAuth, list[str]], str]


def device_code_sign_in(auth: GraphAuth, scopes: list[str]) -> str:
    return auth.sign_in_device_code(scopes)


class Doctor:
    def __init__(
        self,
        config: Config,
        clock: Clock,
        *,
        http: httpx.AsyncClient,
        send_tests: bool = False,
        sign_in: SignIn | None = device_code_sign_in,
        auth_factory: Callable[[Config], GraphAuth] = GraphAuth,
        ica_factory: Callable[[Config], IcaClient] = IcaClient.from_config,
        secret: Callable[[str], str] = get_secret,
        run_command: Callable[[list[str]], str] | None = None,
        read_registry: Callable[[str, str], str | None] | None = None,
        outlook_connect: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.clock = clock
        self.http = http
        self.send_tests = send_tests
        self.sign_in = sign_in
        self.auth_factory = auth_factory
        self.ica_factory = ica_factory
        self.secret = secret
        self.run_command = run_command or _run_command
        self.read_registry = read_registry or _read_registry
        self.outlook_connect = outlook_connect

    async def run_all(self) -> list[CheckResult]:
        results = [*self.check_config()]
        results += await self.check_ica()
        results += await self.check_network()
        graph_token, pbi_token, graph_results = self.graph_tokens()
        results += graph_results
        if graph_token:
            results += await self.check_graph(graph_token)
            results += await self.check_teams_self(graph_token)
        if pbi_token:
            results += await self.check_powerbi(pbi_token)
        results += await self.check_outlook_com()
        results += await self.check_slack()
        results += self.check_laptop()
        return results

    # ------------------------------------------------------------------ config

    def check_config(self) -> list[CheckResult]:
        c = self.config
        out = [_ok("config", "config", f"valid; timezone {c.timezone}, caller {c.caller_timezone}")]
        out.append(
            _ok("data rule", "config", f"level '{c.data_rule.level}'")
            if c.data_rule.level == "metadata"
            else _warn("data rule", "config", f"level '{c.data_rule.level}'", "confirm written approval (Q2)")
        )
        return out

    # ------------------------------------------------------------------ ICA

    async def check_ica(self) -> list[CheckResult]:
        try:
            client = self.ica_factory(self.config)
        except AgentError as exc:
            return [
                _fail(
                    "ICA", "ica", str(exc), "set ICA_BASE_URL in backend/.env and run: agentd secret set ica"
                )
            ]
        out = []
        try:
            models = await client.list_models()
            out.append(_ok("ICA reachable", "ica", f"{len(models)} models listed"))
        except AgentError as exc:
            await client.aclose()
            return [
                _fail("ICA reachable", "ica", str(exc)[:200], "check the key (agentd secret set ica) and URL")
            ]
        for tier, model in self.config.models.ids.model_dump().items():
            if not model:
                continue
            name = f"ICA model {tier} ({model})"
            if model not in models:
                out.append(_fail(name, "ica", "not in the model list", "update models.ids in config.yaml"))
                continue
            try:
                await client.chat(
                    {"model": model, "messages": [{"role": "user", "content": "Reply with ok."}]}
                )
                out.append(_ok(name, "ica", "responds"))
            except AgentError as exc:
                out.append(_fail(name, "ica", str(exc)[:200], _ica_fix(str(exc))))
        await client.aclose()
        return out

    # ------------------------------------------------------------------ network

    async def check_network(self) -> list[CheckResult]:
        load_env_file()
        hosts = list(HOSTS)
        ica = os.environ.get(self.config.ica.base_url_env, "")
        if "://" in ica:
            hosts.insert(0, ica.split("/")[2])
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        out = []
        for host in hosts:
            try:
                response = await self.http.get(f"https://{host}/", timeout=15)
                out.append(_ok(f"network {host}", f"network:{host}", f"TLS ok (HTTP {response.status_code})"))
            except httpx.HTTPError as exc:
                fix = "set HTTPS_PROXY, and SSL_CERT_FILE to the company CA bundle if TLS is inspected"
                via = f" via proxy {proxy}" if proxy else ""
                out.append(_fail(f"network {host}", f"network:{host}", f"{type(exc).__name__}{via}", fix))
        return out

    # ------------------------------------------------------------------ Graph and Power BI sign-in

    def graph_tokens(self) -> tuple[str | None, str | None, list[CheckResult]]:
        try:
            auth = self.auth_factory(self.config)
        except ConfigError:
            write_it_request(self.config)
            fix = (
                f"no app registration yet: send docs/{IT_REQUEST.name} to IT, "
                "then set graph.tenant_id and graph.client_id"
            )
            return (
                None,
                None,
                [
                    _warn("Graph sign-in", "graph", "tenant_id/client_id not set; Graph checks skipped", fix),
                    _warn("Power BI sign-in", "powerbi", "needs the same app registration; skipped", fix),
                ],
            )
        out = []
        tokens: list[str | None] = []
        for name, connector, scopes in [
            ("Graph sign-in", "graph", graph_scopes(self.config)),
            ("Power BI sign-in", "powerbi", powerbi_scopes(self.config)),
        ]:
            try:
                tokens.append(self._token(auth, scopes))
                out.append(_ok(name, connector, "token acquired"))
            except NeedsSignIn as exc:
                tokens.append(None)
                out.append(
                    _fail(
                        name,
                        connector,
                        str(exc)[:200],
                        "run agentd doctor again and finish the sign-in, "
                        "or ask IT to grant consent for the scopes",
                    )
                )
        return tokens[0], tokens[1], out

    def _token(self, auth: GraphAuth, scopes: list[str]) -> str:
        try:
            return auth.get_token(scopes)
        except NeedsSignIn:
            if self.sign_in is None:
                raise
            return self.sign_in(auth, scopes)

    # ------------------------------------------------------------------ Graph reads

    async def _get(self, url: str, token: str, **params: str) -> dict[str, Any]:
        response = await self.http.get(
            url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30
        )
        if response.status_code >= 400:
            raise _HttpProblem(response.status_code, response.text[:200])
        body: dict[str, Any] = response.json()
        return body

    async def check_graph(self, token: str) -> list[CheckResult]:
        now = self.clock.now_local()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        checks: list[tuple[str, str, dict[str, str], Callable[[dict[str, Any]], str]]] = [
            ("Graph /me", f"{GRAPH}/me", {"$select": "id,userPrincipalName"}, lambda b: "signed in"),
            (
                "Graph read 1 mail",
                f"{GRAPH}/me/messages",
                {"$top": "1", "$select": "id,receivedDateTime"},
                lambda b: f"{len(b.get('value', []))} message read (metadata only)",
            ),
            (
                "Graph read 1 chat",
                f"{GRAPH}/me/chats",
                {"$top": "1"},
                lambda b: f"{len(b.get('value', []))} chat read",
            ),
            (
                "Graph today's calendar",
                f"{GRAPH}/me/calendarView",
                {
                    "startDateTime": day_start.astimezone(UTC).isoformat(),
                    "endDateTime": (day_start + timedelta(days=1)).astimezone(UTC).isoformat(),
                    "$select": "id,start",
                },
                lambda b: f"{len(b.get('value', []))} events today",
            ),
        ]
        out = []
        for name, url, params, describe in checks:
            try:
                out.append(_ok(name, "graph", describe(await self._get(url, token, **params))))
            except (_HttpProblem, httpx.HTTPError) as exc:
                out.append(_fail(name, "graph", str(exc), _graph_fix(exc)))
        return out

    async def check_teams_self(self, token: str) -> list[CheckResult]:
        name = "Teams post to own chat"
        if not self.send_tests:
            return [
                _warn(
                    name, "teams", "not tried (read-only run)", "run agentd doctor --send-tests to try (Q4)"
                )
            ]
        url = f"{GRAPH}/me/chats/{NOTES_TO_SELF_CHAT}/messages"
        response = await self.http.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"body": {"content": TEST_TEXT}},
            timeout=30,
        )
        if response.status_code < 300:
            return [_ok(name, "teams", "posted to the notes-to-self chat")]
        return [
            _fail(
                name,
                "teams",
                f"HTTP {response.status_code}: {response.text[:150]}",
                "Graph cannot post to the self chat here; use Slack or email-to-self (Q4)",
            )
        ]

    # ------------------------------------------------------------------ Power BI

    async def check_powerbi(self, token: str) -> list[CheckResult]:
        try:
            groups = (await self._get(f"{POWERBI}/groups", token, **{"$top": "50"})).get("value", [])
        except (_HttpProblem, httpx.HTTPError) as exc:
            return [_fail("Power BI groups", "powerbi", str(exc), "check Power BI licence and scopes")]
        dedicated = sum(1 for g in groups if g.get("isOnDedicatedCapacity"))
        out = [
            _ok(
                "Power BI groups",
                "powerbi",
                f"{len(groups)} workspaces; {dedicated} dedicated capacity, {len(groups) - dedicated} shared"
                + (
                    f": {', '.join(sorted(str(g.get('name') or '?') for g in groups)[:10])}" if groups else ""
                ),
            )
        ]
        for group in groups:
            try:
                datasets = (await self._get(f"{POWERBI}/groups/{group['id']}/datasets", token)).get(
                    "value", []
                )
                refreshable = [d for d in datasets if d.get("isRefreshable")]
                if not refreshable:
                    continue
                history = await self._get(
                    f"{POWERBI}/groups/{group['id']}/datasets/{refreshable[0]['id']}/refreshes",
                    token,
                    **{"$top": "1"},
                )
                last = (history.get("value") or [{}])[0]
                out.append(
                    _ok(
                        "Power BI refresh history",
                        "powerbi",
                        f"read; last status {last.get('status', 'none')}",
                    )
                )
                break
            except (_HttpProblem, httpx.HTTPError) as exc:
                out.append(
                    _fail("Power BI refresh history", "powerbi", str(exc), "needs Dataset.Read permission")
                )
                break
        else:
            out.append(_warn("Power BI refresh history", "powerbi", "no refreshable dataset found"))
        return out

    # ------------------------------------------------------------------ Outlook desktop (COM)

    async def check_outlook_com(self) -> list[CheckResult]:
        """Attach to a running classic Outlook only; doctor never starts it. Counts only, no content."""
        if sys.platform != "win32":
            return [_warn("Outlook COM", "outlook_com", "not Windows; skipped")]
        from agent.adapters.outlook_com import OutlookComAdapter

        adapter = OutlookComAdapter(connect=self.outlook_connect or _attach_running_outlook)
        fix = "open classic Outlook (OUTLOOK.EXE) and leave it running; the new Outlook (olk.exe) has no COM"
        health = await adapter.health()
        if health.status != "ok":
            return [_fail("Outlook COM", "outlook_com", health.detail.split(". Start")[0], fix)]
        now = self.clock.now_local()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        out = [_ok("Outlook COM", "outlook_com", health.detail)]
        try:
            mails = await adapter.list_new(day_start)
            out.append(
                _ok("Outlook COM read today's mail", "outlook_com", f"{len(mails)} messages since midnight")
            )
            events = await adapter.events_between(day_start, day_start + timedelta(days=1))
            out.append(_ok("Outlook COM read today's calendar", "outlook_com", f"{len(events)} events today"))
        except AgentError as exc:
            out.append(_fail("Outlook COM read", "outlook_com", str(exc)[:200], fix))
        return out

    # ------------------------------------------------------------------ Slack

    async def check_slack(self) -> list[CheckResult]:
        tokens = {}
        for key in ("slack_bot_token", "slack_app_token"):
            with contextlib.suppress(AgentError):
                tokens[key] = self.secret(key)
        if len(tokens) < 2:
            missing = sorted({"slack_bot_token", "slack_app_token"} - set(tokens))
            return [
                _warn(
                    "Slack tokens",
                    "slack",
                    f"missing: {', '.join(missing)}",
                    "create the Slack app (Socket Mode), then agentd secret set slack_bot_token "
                    "and agentd secret set slack_app_token (Q5)",
                )
            ]
        out = [_ok("Slack tokens", "slack", "bot and app tokens present")]
        auth = await self._slack("auth.test", tokens["slack_bot_token"])
        out.append(
            _ok("Slack bot token", "slack", f"valid for team {auth.get('team', '?')}")
            if auth.get("ok")
            else _fail(
                "Slack bot token", "slack", str(auth.get("error")), "reinstall the app, copy the xoxb- token"
            )
        )
        sock = await self._slack("apps.connections.open", tokens["slack_app_token"])
        out.append(
            _ok("Slack Socket Mode", "slack", "connection URL issued")
            if sock.get("ok")
            else _fail(
                "Slack Socket Mode",
                "slack",
                str(sock.get("error")),
                "enable Socket Mode and use an xapp- token with connections:write",
            )
        )
        name = "Slack DM to you"
        if not self.config.slack.user_id:
            out.append(
                _warn(name, "slack", "slack.user_id not set", "put your member id (U...) in config.yaml")
            )
        elif not self.send_tests:
            out.append(_warn(name, "slack", "not tried (read-only run)", "run agentd doctor --send-tests"))
        else:
            opened = await self._slack(
                "conversations.open", tokens["slack_bot_token"], users=self.config.slack.user_id
            )
            channel = (opened.get("channel") or {}).get("id")
            sent = await self._slack(
                "chat.postMessage", tokens["slack_bot_token"], channel=channel or "", text=TEST_TEXT
            )
            out.append(
                _ok(name, "slack", "sent")
                if sent.get("ok")
                else _fail(
                    name,
                    "slack",
                    str(sent.get("error") or opened.get("error")),
                    "add im:write and chat:write",
                )
            )
        return out

    async def _slack(self, method: str, token: str, **data: str) -> dict[str, Any]:
        try:
            response = await self.http.post(
                f"{SLACK}/{method}", headers={"Authorization": f"Bearer {token}"}, data=data, timeout=30
            )
            body: dict[str, Any] = response.json()
            return body
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "error": type(exc).__name__}

    # ------------------------------------------------------------------ laptop (Q11)

    def check_laptop(self) -> list[CheckResult]:
        if sys.platform != "win32":
            return [_warn("laptop power and lock", "laptop", "not Windows; skipped")]
        values = {
            "display off": _powercfg_seconds(
                self.run_command(["powercfg", "/query", "SCHEME_CURRENT", "SUB_VIDEO", "VIDEOIDLE"])
            ),
            "sleep": _powercfg_seconds(
                self.run_command(["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"])
            ),
        }
        policy = self.read_registry(
            r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System", "InactivityTimeoutSecs"
        )
        saver = self.read_registry(
            r"HKCU\Software\Policies\Microsoft\Windows\Control Panel\Desktop", "ScreenSaveTimeOut"
        ) or self.read_registry(r"HKCU\Control Panel\Desktop", "ScreenSaveTimeOut")
        secure = self.read_registry(
            r"HKCU\Software\Policies\Microsoft\Windows\Control Panel\Desktop", "ScreenSaverIsSecure"
        ) or self.read_registry(r"HKCU\Control Panel\Desktop", "ScreenSaverIsSecure")
        lock = (
            int(policy)
            if policy and policy.isdigit() and int(policy) > 0
            else (int(saver) if saver and saver.isdigit() and secure == "1" else None)
        )
        parts = []
        for label, (ac, dc) in values.items():
            parts.append(f"{label} AC {_mins(ac)} / battery {_mins(dc)}")
        parts.append(
            f"lock after {_mins(lock)}"
            + (" (policy)" if policy and policy.isdigit() and int(policy) > 0 else "")
        )
        detail = "; ".join(parts)
        sleep_ac = values["sleep"][0]
        short = [v for v in (sleep_ac, lock) if v is not None and 0 < v < 30 * 60]
        if short:
            return [
                _warn(
                    "laptop power and lock",
                    "laptop",
                    detail,
                    "sleep or lock under 30 min stops background work; keep the laptop plugged in and awake, "
                    "or ask IT for an exception (Q11)",
                )
            ]
        return [_ok("laptop power and lock", "laptop", detail)]


class _HttpProblem(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"HTTP {status}: {text}")
        self.status = status


def _ica_fix(message: str) -> str:
    if "credits" in message:
        return "team frontier credits used up; use a free model or wait for the reset"
    if "Bedrock" in message or "security token" in message:
        return "ICA-side provider error; report to ICA support, use another model meanwhile"
    return "check models.ids against the ICA model list"


def _graph_fix(exc: Exception) -> str:
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return "missing consent for this scope; ask IT to grant it for the app registration"
    if status == 429:
        return "throttled; try again in a few minutes"
    return "check network and proxy"


def _powercfg_seconds(output: str) -> tuple[int | None, int | None]:
    ac = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", output)
    dc = re.search(r"Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)", output)
    return (int(ac.group(1), 16) if ac else None, int(dc.group(1), 16) if dc else None)


def _mins(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    return "never" if seconds == 0 else f"{seconds // 60} min"


def _attach_running_outlook() -> Any:
    import win32com.client

    return win32com.client.GetActiveObject("Outlook.Application")


def _run_command(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _read_registry(path: str, value: str) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    hive_name, _, sub = path.partition("\\")
    hive = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}[hive_name]
    try:
        with winreg.OpenKey(hive, sub) as key:
            return str(winreg.QueryValueEx(key, value)[0])
    except OSError:
        return None


def write_it_request(config: Config) -> Path:
    """The app registration request for IT. Contains no secrets and no tenant data."""
    graph = "\n".join(
        f"- {s}" for s in [*config.graph.scopes, "User.Read", "OnlineMeetingTranscript.Read.All (optional)"]
    )
    pbi = "\n".join(f"- {s}" for s in config.powerbi.scopes)
    IT_REQUEST.write_text(
        f"""# Request: Entra ID app registration for a personal work assistant

Please create an app registration so a personal assistant on my laptop can read my own mail,
calendar, chats and Power BI refresh status, and act only after I approve each action.

- Name: Always-On Agent (personal)
- Supported accounts: this organisation only
- Platform: Mobile and desktop applications, redirect URI `http://localhost`
- Allow public client flows: Yes (device code sign-in)
- No client secret or certificate is needed (public client; tokens stay encrypted on my laptop with DPAPI)

Delegated Microsoft Graph permissions:
{graph}

Delegated Power BI Service permissions:
{pbi}

Please send back the Application (client) ID and the Directory (tenant) ID, and grant admin
consent if your policy requires it. All permissions are delegated: the app can only see what I can see.
""",
        encoding="utf-8",
        newline="\n",
    )
    return IT_REQUEST


def worst(results: list[CheckResult]) -> dict[str, CheckResult]:
    """One result per connector: the worst one."""
    rank = {"OK": 0, "WARN": 1, "FAIL": 2}
    by_connector: dict[str, CheckResult] = {}
    for result in results:
        current = by_connector.get(result.connector)
        if current is None or rank[result.status] > rank[current.status]:
            by_connector[result.connector] = result
    return by_connector


HEALTH = {"OK": "ok", "WARN": "degraded", "FAIL": "down"}


def load_config_or_report() -> tuple[Config | None, CheckResult | None]:
    try:
        return load_config(), None
    except ConfigError as exc:
        return None, _fail("config", "config", str(exc)[:300], "fix config/config.yaml")
