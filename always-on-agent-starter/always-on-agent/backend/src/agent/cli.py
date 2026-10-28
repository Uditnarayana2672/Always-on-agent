"""agentd entry point (F01, F16, F18)."""

from typing import TYPE_CHECKING

import typer

from agent import __version__

if TYPE_CHECKING:
    from agent.core.auth import GraphAuth

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
def run(fake: bool = typer.Option(False, "--fake", help="Use fake adapters and fixtures.")) -> None:
    """Run the agent."""
    from agent.core.config import get_config
    from agent.memory.personal import PersonalMemory

    personal = PersonalMemory(get_config().paths.memory_dir or None)
    for path in personal.ensure():
        typer.echo(f"created {path} from its template; edit it to tell the agent about you")
    if not fake:
        typer.echo("not implemented yet")
        return
    import asyncio

    from agent.adapters.fakes.world import FakeWorld

    # Fake mode builds only fakes: no Graph, Slack, Power BI or ICA client is created.
    world = FakeWorld.frozen()
    for status in asyncio.run(world.health()):
        typer.echo(f"{status.connector:11} {status.status:8} {status.detail}")
    typer.echo(
        f"fake world ready at {world.clock.now_local():%Y-%m-%d %H:%M %Z}; runtime not implemented yet"
    )


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
def console() -> None:
    """Print the one-time console link."""
    typer.echo("not implemented yet")


@app.command()
def status() -> None:
    """Print the agent version."""
    typer.echo(f"always-on-agent {__version__}")
