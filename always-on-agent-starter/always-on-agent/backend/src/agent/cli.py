"""agentd entry point (F01, F16, F18)."""

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from agent.core.auth import GraphAuth
    from agent.runtime.supervisor import RunParts

app = typer.Typer(help="Always-On Agent daemon.", no_args_is_help=True)
secret_app = typer.Typer(help="Manage secrets in Windows Credential Manager.", no_args_is_help=True)
app.add_typer(secret_app, name="secret")


@secret_app.command("set")
def secret_set(name: str) -> None:
    """Store a secret. The value is prompted for and not echoed."""
    from agent.core.secrets import set_secret

    value: str = typer.prompt(f"Value for {name}", hide_input=True, confirmation_prompt=True)
    set_secret(name, value)
    typer.echo(f"Saved secret {name}.")


@app.command()
def run(
    fake: bool = typer.Option(False, "--fake", help="Use fake adapters and fixtures."),
    real_slack: bool = typer.Option(
        False, "--real-slack", help="With --fake: send one test approval card to your real Slack DM."
    ),
    data_dir: str | None = typer.Option(None, "--data-dir", help="With --fake: keep the fake database here."),
    demo_only: bool = typer.Option(False, "--demo-only", help="With --fake: run the fake day, then exit."),
    live_recipes: bool = typer.Option(
        False, "--live-recipes", help="With --fake: run the recipes Live against the fake systems."
    ),
) -> None:
    """Run the agent."""
    from agent.core.config import get_config
    from agent.memory.personal import PersonalMemory

    personal = PersonalMemory(get_config().paths.memory_dir or None)
    for path in personal.ensure():
        typer.echo(f"created {path} from its template; edit it to tell the agent about you")
    import asyncio

    if real_slack:
        _slack_check()
        return
    if not fake:
        raise typer.Exit(asyncio.run(_run_real()))
    raise typer.Exit(asyncio.run(_run_fake(data_dir, demo_only, live_recipes)))


async def _run_real() -> int:
    """The supervisor on the real systems (F01): lock, build, health, start, heartbeat, drain."""
    from agent.core.config import get_config
    from agent.runtime.real import build_real_parts
    from agent.runtime.supervisor import Supervisor, data_dir_for

    config = get_config()

    async def build() -> "RunParts":
        return await build_real_parts(config)

    return await Supervisor(data_dir_for(config.database.url), build, echo=typer.echo).run()


async def _run_fake(data_dir: str | None, demo_only: bool, live: bool = False) -> int:
    """Fake mode builds only fakes: no Graph, Slack, Power BI or ICA client is created."""
    import tempfile
    from pathlib import Path

    from agent.api.server import ConsoleServer, console_auth
    from agent.core.config import get_config
    from agent.runtime.fake import build_fake_runtime, run_demo
    from agent.runtime.supervisor import RunParts, Supervisor

    config = get_config()
    folder = Path(data_dir) if data_dir else Path(tempfile.mkdtemp(prefix="agentd-fake-"))

    async def build() -> RunParts:
        runtime, world = await build_fake_runtime(config, folder, live=live)
        for status in await world.health():
            typer.echo(f"{status.connector:11} {status.status:8} {status.detail}")
        log = await run_demo(runtime, world)
        typer.echo(f"\nfake day at {runtime.clock.now_local():%Y-%m-%d %H:%M %Z}; fake Slack DM:")
        for message in log:
            typer.echo("  " + str(message["text"]).replace("\n", "\n  "))
        auth = console_auth(runtime, fake=True)
        server = ConsoleServer(runtime, auth, fake=True)
        if not demo_only:
            typer.echo(f"open the console (link works once, for 10 minutes): {await auth.login_link()}")
        return RunParts(runtime=runtime, extras=[("console", server.serve, server.stop)], console=server.url)

    if demo_only:
        parts = await build()
        await parts.runtime.stop()
        return 0
    supervisor = Supervisor(folder, build, mode="fake", echo=typer.echo, start_scheduler=False)
    return await supervisor.run()


def _slack_check() -> None:
    import asyncio

    from agent.channels.slack_check import run_slack_check
    from agent.core.config import get_config
    from agent.core.errors import AgentError
    from agent.core.secrets import get_secret

    config = get_config()
    if not config.slack.user_id:
        typer.echo("Set slack.user_id (your member id, U...) in config/config.yaml first.")
        raise typer.Exit(1)
    try:
        bot_token, app_token = get_secret("slack_bot_token"), get_secret("slack_app_token")
    except AgentError as exc:
        typer.echo(f"{exc}. Run agentd secret set slack_bot_token and agentd secret set slack_app_token.")
        raise typer.Exit(1) from exc
    typer.echo(asyncio.run(run_slack_check(config, bot_token, app_token, echo=typer.echo)))


@app.command()
def doctor(
    send_tests: bool = typer.Option(False, "--send-tests", help="Also try a Teams self-note and a Slack DM."),
    no_sign_in: bool = typer.Option(False, "--no-sign-in", help="Never start a sign-in; report it instead."),
) -> None:
    """Check every connector and print fixes. Read-only unless --send-tests."""
    import asyncio

    asyncio.run(_doctor(send_tests, no_sign_in))


async def _doctor(send_tests: bool, no_sign_in: bool) -> None:
    import httpx

    from agent.core.clock import SystemClock
    from agent.db.engine import create_engine_async
    from agent.db.migrate import upgrade_head
    from agent.db.repositories import HealthRepo
    from agent.runtime.doctor import HEALTH, Doctor, load_config_or_report, worst

    config, problem = load_config_or_report()
    if config is None:
        assert problem is not None
        typer.echo(problem.line())
        raise typer.Exit(1)
    clock = SystemClock(config)
    async with httpx.AsyncClient() as http:
        doc = Doctor(
            config, clock, http=http, send_tests=send_tests, sign_in=None if no_sign_in else _sign_in
        )
        results = await doc.run_all()
    for result in results:
        typer.echo(result.line())

    upgrade_head(config.database.url)
    engine = create_engine_async(config.database.url)
    repo = HealthRepo(engine, clock)
    for connector, result in worst(results).items():
        await repo.report(connector, HEALTH[result.status], None if result.status == "OK" else result.detail)
    await engine.dispose()
    counts = {s: sum(r.status == s for r in results) for s in ("OK", "WARN", "FAIL")}
    typer.echo("")
    typer.echo(f"{counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL. Saved to connector_health.")


def _sign_in(auth: "GraphAuth", scopes: list[str]) -> str:
    typer.echo("Sign-in needed. Follow these steps in your browser:")
    return auth.sign_in_device_code(scopes, show=typer.echo)


@app.command()
def signin(
    interactive: bool = typer.Option(False, "--interactive", help="Use a browser window, not a device code."),
) -> None:
    """Sign in to Microsoft 365 for Graph and Power BI."""
    from agent.core.auth import GraphAuth, graph_scopes, powerbi_scopes
    from agent.core.config import load_config

    config = load_config()
    auth = GraphAuth(config)
    for scopes in (graph_scopes(config), powerbi_scopes(config)):
        if interactive:
            auth.sign_in_interactive(scopes)
        else:
            auth.sign_in_device_code(scopes, show=typer.echo)
    typer.echo("Signed in. The token cache is encrypted on this laptop.")


@app.command()
def console(
    data_dir: str | None = typer.Option(
        None, "--data-dir", help="The --data-dir of a running agentd run --fake."
    ),
) -> None:
    """Print a one-time console link (works once, for ten minutes)."""
    import asyncio

    typer.echo(asyncio.run(_console_link(data_dir)))


async def _console_link(data_dir: str | None) -> str:
    import asyncio
    from pathlib import Path

    from agent.api.auth import ConsoleAuth
    from agent.core.clock import SystemClock
    from agent.core.config import get_config
    from agent.db.engine import create_engine_async
    from agent.db.migrate import upgrade_head
    from agent.db.repositories import KvRepo

    config = get_config()
    url = config.database.url
    if data_dir:
        url = f"sqlite+aiosqlite:///{(Path(data_dir) / 'agent.db').as_posix()}"
    await asyncio.to_thread(upgrade_head, url)
    engine = create_engine_async(url)
    try:
        clock = SystemClock(config)
        # The code goes into the agent's database; the running agentd redeems it and sets the cookie.
        auth = ConsoleAuth(KvRepo(engine, clock), clock.now_utc, token="-", port=config.console.port)
        return f"Open this link in your browser: {await auth.login_link()}"
    finally:
        await engine.dispose()


@app.command()
def status(
    data_dir: str | None = typer.Option(None, "--data-dir", help="The --data-dir of an agentd run --fake."),
) -> None:
    """Is agentd running? Uptime, last check-in, queue and health. Exit 0 running, 1 not, 2 stuck."""
    from datetime import UTC, datetime
    from pathlib import Path

    from agent.core.config import get_config
    from agent.runtime.supervisor import data_dir_for, status_lines

    folder = Path(data_dir) if data_dir else data_dir_for(get_config().database.url)
    code, lines = status_lines(folder, datetime.now(UTC))
    for line in lines:
        typer.echo(line)
    raise typer.Exit(code)
