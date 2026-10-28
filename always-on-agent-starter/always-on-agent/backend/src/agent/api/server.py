"""Serve the console API inside agentd (F15): uvicorn on 127.0.0.1 only, in agentd's own event loop."""

import logging

import uvicorn

from agent.api.app import create_app
from agent.api.auth import ConsoleAuth
from agent.runtime.assembly import Runtime

log = logging.getLogger(__name__)


def console_auth(rt: Runtime, *, fake: bool) -> ConsoleAuth:
    """Real runs keep the session token in Credential Manager; fake runs make a new one each start."""
    if fake:
        return ConsoleAuth(rt.kv, rt.clock.now_utc, port=rt.config.console.port)
    from agent.core.secrets import get_secret, set_secret

    return ConsoleAuth(
        rt.kv, rt.clock.now_utc, port=rt.config.console.port, load=get_secret, store=set_secret
    )


async def serve(rt: Runtime, auth: ConsoleAuth, *, fake: bool = False) -> None:
    """Runs until the task is cancelled or uvicorn stops (Ctrl+C)."""
    app = create_app(rt, auth, fake=fake)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",  # never 0.0.0.0: the console is for this laptop only
        port=rt.config.console.port,
        log_config=None,
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)
    log.info("console listening", extra={"port": rt.config.console.port})
    await server.serve()
