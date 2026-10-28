"""Task lifecycle (architecture 4.2).

Only the user moves a task to done. The one exception is the source system: when the ServiceNow
report shows the ticket Closed, the task follows (golden rule 10).
"""

from collections import deque
from typing import Literal

from agent.core.errors import IllegalTransition

TaskState = Literal["new", "scheduled", "in_progress", "needs_you", "waiting", "done", "failed"]
Actor = Literal["user", "agent", "worker", "scheduler", "source_system"]

INITIAL_STATES: frozenset[str] = frozenset({"new", "scheduled"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"in_progress"}),
    "scheduled": frozenset({"in_progress"}),
    "in_progress": frozenset({"needs_you", "waiting", "done", "failed"}),
    "needs_you": frozenset({"in_progress", "done"}),
    "waiting": frozenset({"in_progress", "needs_you", "done"}),
    "done": frozenset({"in_progress"}),  # ticket reopened
    "failed": frozenset({"needs_you"}),
}

SOURCE_CLOSED_STATES = frozenset({"closed"})


def check_transition(from_state: str, to_state: str, actor: str, source_state: str | None = None) -> None:
    """Raise IllegalTransition unless the lifecycle and the actor rules allow the move."""
    if to_state not in TRANSITIONS.get(from_state, frozenset()):
        raise IllegalTransition(f"{from_state} -> {to_state} is not allowed")
    if to_state == "done":
        if actor == "user":
            return
        if actor == "source_system" and (source_state or "").strip().lower() in SOURCE_CLOSED_STATES:
            return
        raise IllegalTransition(
            f"only the user can mark a task done (actor {actor!r}"
            + (f", source state {source_state!r})" if source_state else ")")
        )


def path(from_state: str, to_state: str) -> list[str]:
    """Shortest legal route from one state to another, excluding the start; never passes through done.

    Raises IllegalTransition when there is none.
    """
    if from_state == to_state:
        return []
    queue: deque[tuple[str, list[str]]] = deque([(from_state, [])])
    seen = {from_state}
    while queue:
        state, route = queue.popleft()
        for nxt in sorted(TRANSITIONS.get(state, frozenset())):
            if nxt in seen or (nxt == "done" and nxt != to_state):
                continue
            if nxt == to_state:
                return [*route, nxt]
            seen.add(nxt)
            queue.append((nxt, [*route, nxt]))
    raise IllegalTransition(f"no route from {from_state} to {to_state}")
