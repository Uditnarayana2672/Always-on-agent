from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from agent.channels.commands import AgentCommands
from agent.channels.teams_notify import TeamsSelfChannel
from agent.core.errors import AgentError, ControlError
from agent.db.repositories import Task
from agent.runtime.controls import ControlState
from tests.adapters.graph_helpers import G, client


class Stub:
    def __init__(self) -> None:
        self.state_value = ControlState()
        self.fired: list[tuple[str, bool]] = []

    async def state(self) -> ControlState:
        return self.state_value

    async def pause(self, actor: str = "user") -> ControlState:
        self.state_value = ControlState(
            paused=True, paused_at=datetime(2026, 3, 25, 4, tzinfo=UTC), changed_by=actor
        )
        return self.state_value

    async def resume(self, actor: str = "user") -> Any:
        self.state_value = ControlState()
        return type("R", (), {"released": ["a1"], "caught_up": ["brief", "checkin"]})()

    async def pending(self) -> list[Any]:
        return [object(), object()]

    async def undo(self, audit_id: int, actor: str = "user") -> Any:
        return type("U", (), {"message": "Undone."})()

    async def all(self) -> list[dict[str, Any]]:
        return [{"connector": "slack", "status": "ok"}, {"connector": "powerbi", "status": "down"}]

    async def fire(self, name: str, due_at: Any = None, *, manual: bool = False) -> Any:
        self.fired.append((name, manual))

    # Defined last: inside the class body the name `list` would hide the builtin used above.
    async def list(self, states: list[str] | None = None) -> list[Task]:
        return [
            Task(
                id=f"INC3011487{i}", project="doxis", kind="incident", title=f"Ticket {i}", state="needs_you"
            )
            for i in range(17)
        ]


async def test_commands() -> None:
    stub = Stub()
    commands = AgentCommands(stub, stub, stub, stub, stub, stub)
    assert "Running" in await commands.status() and "2 waiting" in await commands.status()
    assert "powerbi (down)" in await commands.status()
    assert (await commands.pause()).startswith("Paused.")
    assert "Paused since 25 Mar 04:00 UTC" in await commands.status()
    assert await commands.resume() == "Resumed. Caught up on 2 missed run(s); 1 held action(s) released."
    assert await commands.brief() == "Making the brief now." and stub.fired == [("brief", True)]
    tasks = await commands.tasks()
    assert tasks.count("\n") == 15 and "2 more in the console" in tasks and "needs you" in tasks
    assert await commands.undo(5) == "Undone."
    with pytest.raises(ControlError):
        await AgentCommands(stub, stub, stub, stub, stub).brief()


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_teams_self_note_posts_escaped_text(http: httpx.AsyncClient) -> None:
    route = respx.post(f"{G}/me/chats/48:notes/messages").mock(
        return_value=httpx.Response(201, json={"id": "n1"})
    )
    channel = TeamsSelfChannel(client(http), verified=True)
    await channel.send("INC30114872 <b>needs you</b>\nnow")
    content = route.calls.last.request.content.decode()
    assert "&lt;b&gt;" in content and "<br>" in content
    assert (await channel.health()).status == "ok"
    route.mock(return_value=httpx.Response(403))
    with pytest.raises(AgentError):
        await channel.send("x")
    assert (await channel.health()).status == "degraded"
