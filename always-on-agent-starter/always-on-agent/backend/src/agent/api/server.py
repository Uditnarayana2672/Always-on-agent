"""Serve the console API inside agentd (F15): uvicorn on 127.0.0.1 only, in agentd's own event loop."""

import contextlib
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

    # Without a credential store (CI, Linux) get/set raise ConfigError: the token lives for this run.
    port = rt.config.console.port
    return ConsoleAuth(rt.kv, rt.clock.now_utc, port=port, load=get_secret, store=set_secret)


class ConsoleServer:
    """The console as a part the supervisor starts and stops."""

    def __init__(self, rt: Runtime, auth: ConsoleAuth, *, fake: bool = False) -> None:
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
        self.server = uvicorn.Server(config)
        self.url = f"http://127.0.0.1:{rt.config.console.port}"

    async def serve(self) -> None:
        log.info("console listening", extra={"url": self.url})
        await self.server.serve()
        if not self.server.started:
            log.error("the console could not start; is another program using the port?")

    async def stop(self) -> None:
        self.server.should_exit = True


async def serve(rt: Runtime, auth: ConsoleAuth, *, fake: bool = False) -> None:
    """Runs until uvicorn stops (Ctrl+C)."""
    with contextlib.suppress(Exception):
        await ConsoleServer(rt, auth, fake=fake).serve()
