"""/agent commands (F14, F16): status, pause, resume, brief, tasks, undo <id>.

Replies are plain and short. Every control goes through Controls, so it is audited the same way as
from the console.
"""

from typing import Any, Protocol

from agent.channels.cards import status_text
from agent.core.errors import ControlError

OPEN_STATES = ("new", "in_progress", "needs_you", "waiting")
TASKS_SHOWN = 15


class _Controls(Protocol):
    async def state(self) -> Any: ...
    async def pause(self, actor: str = ...) -> Any: ...
    async def resume(self, actor: str = ...) -> Any: ...


class _Pending(Protocol):
    async def pending(self) -> list[Any]: ...


class _Tasks(Protocol):
    async def list(self, states: list[str] | None = ...) -> list[Any]: ...


class _Undo(Protocol):
    async def undo(self, audit_id: int, actor: str = ...) -> Any: ...


class _Health(Protocol):
    async def all(self) -> list[dict[str, Any]]: ...


class _Fire(Protocol):
    async def fire(self, name: str, due_at: Any = ..., *, manual: bool = ...) -> Any: ...


class AgentCommands:
    def __init__(
        self,
        controls: _Controls,
        approvals: _Pending,
        tasks: _Tasks,
        undo: _Undo,
        health: _Health,
        scheduler: _Fire | None = None,
    ) -> None:
        self._controls = controls
        self._approvals = approvals
        self._tasks = tasks
        self._undo = undo
        self._health = health
        self._scheduler = scheduler

    async def status(self) -> str:
        state = await self._controls.state()
        pending = len(await self._approvals.pending())
        health = {str(row["connector"]): str(row["status"]) for row in await self._health.all()}
        return status_text(state.paused, state.leave, pending, health, state.paused_at)

    async def pause(self) -> str:
        await self._controls.pause(actor="slack")
        return "Paused. I keep reading but do nothing above silent work until you resume."

    async def resume(self) -> str:
        result = await self._controls.resume(actor="slack")
        released = len(getattr(result, "released", []) or [])
        caught = len(getattr(result, "caught_up", []) or [])
        return f"Resumed. Caught up on {caught} missed run(s); {released} held action(s) released."

    async def brief(self) -> str:
        if self._scheduler is None:
            raise ControlError("the scheduler is not running")
        await self._scheduler.fire("brief", manual=True)
        return "Making the brief now."

    async def tasks(self) -> str:
        tasks = await self._tasks.list(list(OPEN_STATES))
        if not tasks:
            return "No open tasks."
        lines = [f"• {t.id} ({t.state.replace('_', ' ')}): {t.title}" for t in tasks[:TASKS_SHOWN]]
        if len(tasks) > TASKS_SHOWN:
            lines.append(f"…and {len(tasks) - TASKS_SHOWN} more in the console.")
        return "\n".join(lines)

    async def undo(self, audit_id: int) -> str:
        result = await self._undo.undo(audit_id, actor="slack")
        return str(getattr(result, "message", result))
