"""`agentd run`: the runtime on the real systems (F01), with whatever is set up so far.

- Microsoft 365 through Graph once graph.client_id is set (mail, calendar, Teams, Power BI); Outlook
  on this laptop (COM) for mail and calendar when it is not, or when mail.backend is com.
- The model through ICA when ICA_BASE_URL and the `ica` secret exist; otherwise recipes run their
  code-only steps (as in an outage).
- Slack when slack.user_id and the two Slack secrets exist; the Teams self note once verified (Q4);
  email to yourself through Graph as the last resort.

Nothing missing stops the start: each gap shows as a connector problem in `agentd status`, the brief
and the console, with `agentd doctor` for the fix.
"""

import logging
import sys
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import Clock, SystemClock
from agent.core.config import Config
from agent.core.errors import AgentError, ConfigError
from agent.db.repositories import HealthRepo, KvRepo, UsageRepo
from agent.model.gateway import ModelGateway, ModelRunner
from agent.privacy.hook import PrivacyLayer
from agent.privacy.pseudonymiser import Pseudonymiser
from agent.runtime.assembly import Runtime
from agent.runtime.supervisor import RunParts, data_dir_for

log = logging.getLogger(__name__)


class RealWiring:
    """Holds the shared HTTP client and the pieces built for one run."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.http = httpx.AsyncClient(timeout=30)
        self.graph: Any = None
        self.ica: Any = None
        self.notes: list[str] = []

    async def adapters(self, kv: KvRepo, health: HealthRepo, clock: Clock) -> dict[str, Any]:
        config = self.config
        out: dict[str, Any] = {}
        if config.graph.client_id:
            from agent.adapters.calendar import GraphCalendarAdapter
            from agent.adapters.graph_client import POWERBI_BASE, GraphClient
            from agent.adapters.outlook_graph import OutlookGraphAdapter
            from agent.adapters.powerbi import PowerBIRestAdapter
            from agent.adapters.teams import GraphTeamsAdapter
            from agent.core.auth import GraphAuth, graph_scopes, powerbi_scopes

            auth = GraphAuth(config)
            scopes = graph_scopes(config)
            self.graph = GraphClient(self.http, auth, scopes, "graph", health=health, now=clock.now_utc)
            if config.mail.backend in ("auto", "graph"):
                out["outlook"] = OutlookGraphAdapter(
                    self.graph, kv, config.me, clock.now_utc, config.mail.resync_days, config.mail.open_labels
                )
            out["calendar"] = GraphCalendarAdapter(self.graph, config.timezone, clock.now_utc)
            out["teams"] = GraphTeamsAdapter(
                self.graph,
                kv,
                clock.now_utc,
                data_level=config.data_rule.level,
                scopes=scopes,
                active_days=config.teams.active_days,
                max_chats=config.teams.max_chats_per_poll,
            )
            pbi = GraphClient(
                self.http,
                auth,
                powerbi_scopes(config),
                "powerbi",
                base_url=POWERBI_BASE,
                health=health,
                now=clock.now_utc,
            )
            out["powerbi"] = PowerBIRestAdapter(pbi, config.powerbi.refresh_cap_per_day, clock.now_utc)
        else:
            self.notes.append("Microsoft 365: no app registration yet (graph.client_id); see agentd doctor")
        if "outlook" not in out and config.mail.backend != "graph":
            if sys.platform == "win32":
                from agent.adapters.outlook_com import OutlookComAdapter

                com = OutlookComAdapter()
                out["outlook"] = com
                out.setdefault("calendar", com)
            else:
                self.notes.append("Outlook desktop (COM) exists only on Windows")
        return out

    async def model(
        self, config: Config, pseudo: Pseudonymiser, engine: AsyncEngine, clock: Clock
    ) -> ModelRunner | None:
        from agent.model.ica_client import IcaClient

        try:
            self.ica = IcaClient.from_config(config)
        except (ConfigError, AgentError) as exc:
            self.notes.append(f"the model (ICA): {exc}. Recipes run their code-only steps")
            return None
        privacy = PrivacyLayer(config.data_rule.level, pseudo)
        return ModelGateway(self.ica, config, UsageRepo(engine, clock), clock, privacy)

    async def ica_check(self) -> str | None:
        if self.ica is None:
            return None
        try:
            await self.ica.list_models()
        except Exception as exc:
            return str(exc)[:200]
        return None

    async def close(self) -> None:
        await self.http.aclose()
        if self.ica is not None:
            await self.ica.aclose()


async def build_real_parts(config: Config) -> RunParts:
    from agent.api.server import ConsoleServer, console_auth
    from agent.channels.email_self import EmailSelfChannel, graph_sender
    from agent.channels.teams_notify import TeamsSelfChannel

    clock = SystemClock(config)
    wiring = RealWiring(config)
    data_dir = data_dir_for(config.database.url)
    slack = _slack(config, clock, wiring.notes)
    channels: list[Any] = [slack[0]] if slack else []
    rt = await Runtime.build(
        config,
        clock,
        {},
        adapter_factory=wiring.adapters,
        model_factory=wiring.model,
        notify_channels=channels,
        approval_channels=channels,
        data_dir=data_dir,
    )
    # Fallback channels need the Graph client the adapters made.
    console_url = f"http://127.0.0.1:{config.console.port}"
    rt.router.add(TeamsSelfChannel(wiring.graph, config.teams.self_note_verified))
    rt.router.add(
        EmailSelfChannel(config.me, graph_sender(wiring.graph) if wiring.graph else None, console_url)
    )
    rt.health_monitor._ica = wiring.ica_check
    rt.health_monitor.setup_notes = list(wiring.notes)  # `agentd status` lists them
    for note in wiring.notes:
        log.warning(note)
    extras: list[Any] = []
    if slack is not None:
        bot, runner = slack
        # The bot is made before the services it calls (they need it as a channel): wire them now.
        bot._approvals = rt.approvals
        bot._ingest = rt.coordinator.ingest
        bot._links = rt.memory
        bot._undo = rt.undo.undo
        bot._health = rt.health
        from agent.channels.commands import AgentCommands

        bot._commands = AgentCommands(rt.controls, rt.approvals, rt.tasks, rt.undo, rt.health, rt.scheduler)
        extras.append(("slack", runner.start, runner.stop))
    server = ConsoleServer(rt, console_auth(rt, fake=False))
    extras.append(("console", server.serve, server.stop))
    extras.append(("http", _noop, wiring.close))
    return RunParts(runtime=rt, extras=extras, console=server.url)


def _slack(config: Config, clock: Clock, notes: list[str]) -> tuple[Any, Any] | None:
    if not config.slack.user_id:
        notes.append("Slack: set slack.user_id in config/config.yaml (approvals stay in the console)")
        return None
    from agent.core.secrets import get_secret

    try:
        bot_token, app_token = get_secret("slack_bot_token"), get_secret("slack_app_token")
    except AgentError:
        notes.append("Slack: run agentd secret set slack_bot_token and agentd secret set slack_app_token")
        return None
    from slack_sdk.web.async_client import AsyncWebClient

    from agent.channels.slack_bot import SlackBot, SlackRunner

    bot = SlackBot(
        AsyncWebClient(token=bot_token),  # type: ignore[arg-type]
        config.slack.user_id,
        clock,
        f"http://127.0.0.1:{config.console.port}",
    )
    return bot, SlackRunner(bot, bot_token, app_token)


async def _noop() -> None:
    return None
