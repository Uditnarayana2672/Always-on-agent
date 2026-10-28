"""Async repositories over agent.db (F04, F07, F08, F09, F11, F13, F18).

Each repository takes an AsyncEngine and a Clock. Times are aware UTC.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from pydantic import BaseModel
from sqlalchemy import and_, delete, exists, func, insert, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import Select

from agent.core.clock import Clock
from agent.core.errors import ApprovalConflict, ConfigError
from agent.core.secrets import get_secret, set_secret
from agent.db import schema as s


class _Repo:
    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock.now_utc()

    async def _all(self, query: Select[Any]) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(query)).mappings().all()]

    async def _one(self, query: Select[Any]) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return dict(row) if row else None


# ---------------------------------------------------------------- tasks


class Task(BaseModel):
    id: str
    project: str
    kind: str
    title: str
    state: str
    priority: str = "normal"
    caller_token: str | None = None
    affected_token: str | None = None
    site: str | None = None
    opened_at: datetime | None = None
    assigned_at: datetime | None = None
    email_received_at: datetime | None = None
    clock_started_at: datetime | None = None
    clock_estimated: bool = False
    clock_paused: bool = False
    next_action: str | None = None
    recipe: str | None = None
    last_heartbeat_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TaskRepo(_Repo):
    async def create(self, task: Task) -> Task:
        now = self._now()
        values = task.model_dump() | {"created_at": now, "updated_at": now}
        async with self._engine.begin() as conn:
            await conn.execute(insert(s.tasks).values(**values))
        return Task(**values)

    async def get(self, task_id: str) -> Task | None:
        row = await self._one(select(s.tasks).where(s.tasks.c.id == task_id))
        return Task(**row) if row else None

    async def update(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = self._now()
        async with self._engine.begin() as conn:
            await conn.execute(update(s.tasks).where(s.tasks.c.id == task_id).values(**fields))

    async def transition(self, task_id: str, from_state: str, to_state: str) -> bool:
        """Compare-and-set the state; False if the task was not in from_state."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == task_id, s.tasks.c.state == from_state)
                .values(state=to_state, updated_at=self._now())
            )
        return result.rowcount == 1

    async def heartbeat(self, task_id: str) -> None:
        await self.update(task_id, last_heartbeat_at=self._now())

    async def list(self, states: list[str] | None = None) -> list[Task]:
        query = select(s.tasks).order_by(s.tasks.c.updated_at.desc())
        if states:
            query = query.where(s.tasks.c.state.in_(states))
        return [Task(**r) for r in await self._all(query)]


class ClockRepo(_Repo):
    """Pause intervals and fired thresholds for the 24-hour clock."""

    async def open_pause(self, task_id: str, at: datetime, reason: str) -> bool:
        """Start a pause unless one is already open. Returns True if a pause was started."""
        async with self._engine.begin() as conn:
            already = (
                await conn.execute(
                    select(s.clock_pauses.c.id).where(
                        s.clock_pauses.c.task_id == task_id, s.clock_pauses.c.ended_at.is_(None)
                    )
                )
            ).first()
            if already:
                return False
            await conn.execute(insert(s.clock_pauses).values(task_id=task_id, started_at=at, reason=reason))
        return True

    async def close_pause(self, task_id: str, at: datetime) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(s.clock_pauses)
                .where(s.clock_pauses.c.task_id == task_id, s.clock_pauses.c.ended_at.is_(None))
                .values(ended_at=at)
            )
        return result.rowcount > 0

    async def pauses(self, task_id: str) -> list[tuple[datetime, datetime | None]]:
        rows = await self._all(
            select(s.clock_pauses.c.started_at, s.clock_pauses.c.ended_at)
            .where(s.clock_pauses.c.task_id == task_id)
            .order_by(s.clock_pauses.c.started_at)
        )
        return [(r["started_at"], r["ended_at"]) for r in rows]

    async def fire_threshold(self, task_id: str, hours: int) -> bool:
        """Record that a threshold fired; False if it had fired before."""
        stmt = sqlite_insert(s.clock_thresholds).values(task_id=task_id, hours=hours, fired_at=self._now())
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt.on_conflict_do_nothing())
        return result.rowcount == 1


# ---------------------------------------------------------------- events


class EventRepo(_Repo):
    async def add(
        self,
        source: str,
        kind: str,
        external_id: str,
        payload: dict[str, Any],
        received_at: datetime | None = None,
    ) -> int | None:
        """Insert an event; returns its id, or None if (source, external_id) was already seen."""
        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    insert(s.events).values(
                        source=source,
                        kind=kind,
                        external_id=external_id,
                        received_at=received_at or self._now(),
                        payload_json=json.dumps(payload, default=str),
                    )
                )
        except IntegrityError:
            return None
        pk = result.inserted_primary_key
        return int(pk[0]) if pk else None

    async def route(self, event_id: int, task_id: str | None, status: str = "routed") -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(s.events).where(s.events.c.id == event_id).values(task_id=task_id, status=status)
            )

    async def get(self, event_id: int) -> dict[str, Any] | None:
        return await self._one(select(s.events).where(s.events.c.id == event_id))

    async def by_status(self, status: str, limit: int = 500) -> list[dict[str, Any]]:
        return await self._all(
            select(s.events).where(s.events.c.status == status).order_by(s.events.c.id).limit(limit)
        )

    async def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self._all(
            select(s.events).where(s.events.c.status == "new").order_by(s.events.c.id).limit(limit)
        )


# ---------------------------------------------------------------- jobs


class Job(BaseModel):
    id: int
    task_id: str | None
    kind: str
    payload_json: str
    priority: int
    status: str
    attempts: int
    available_at: datetime
    locked_by: str | None
    locked_at: datetime | None
    last_error: str | None


AGE_BOOST_CEILING = 89


class JobRepo(_Repo):
    def __init__(self, engine: AsyncEngine, clock: Clock, lock_ttl_minutes: int = 10) -> None:
        super().__init__(engine, clock)
        self._ttl = timedelta(minutes=lock_ttl_minutes)

    async def enqueue(
        self,
        kind: str,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        available_at: datetime | None = None,
    ) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                insert(s.jobs).values(
                    task_id=task_id,
                    kind=kind,
                    payload_json=json.dumps(payload or {}),
                    priority=priority,
                    available_at=available_at or self._now(),
                )
            )
        return int(result.inserted_primary_key[0])  # type: ignore[index]

    async def claim(self, worker_id: str) -> Job | None:
        """Atomically lock the best available job whose task has no live running job.

        Higher priority first, then oldest. A running job whose lock is older than the TTL counts as
        expired and may be claimed again. One UPDATE ... RETURNING, so no two workers get the same job
        or two jobs of the same task.
        """
        now = self._now()
        expiry = now - self._ttl
        j, r = s.jobs.alias("j"), s.jobs.alias("r")
        live_lock = and_(r.c.status == "running", r.c.locked_at > expiry)
        claimable = or_(j.c.status == "queued", and_(j.c.status == "running", j.c.locked_at <= expiry))
        busy_task = exists().where(and_(r.c.task_id == j.c.task_id, r.c.id != j.c.id, live_lock))
        candidate = (
            select(j.c.id)
            .where(claimable, j.c.available_at <= now, ~busy_task)
            .order_by(self._effective_priority(j, now).desc(), j.c.available_at, j.c.id)
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            update(s.jobs)
            .where(s.jobs.c.id == candidate)
            .values(status="running", locked_by=worker_id, locked_at=now, attempts=s.jobs.c.attempts + 1)
            .returning(*s.jobs.c)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).mappings().first()
        return Job(**row) if row else None

    @staticmethod
    def _effective_priority(j: Any, now: datetime) -> Any:
        """Priority plus one per minute waited (starvation guard), capped at AGE_BOOST_CEILING.

        A job's own priority is never lowered, and age alone never lifts a job past approval resumes
        (90) or urgent clocks (100).
        """
        now_text = now.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ")
        waited = (func.julianday(now_text) - func.julianday(j.c.available_at)) * 1440
        boosted = func.min(j.c.priority + func.max(waited, 0), AGE_BOOST_CEILING)
        return func.max(j.c.priority, boosted)

    async def complete(self, job_id: int, worker_id: str | None = None) -> bool:
        return await self._finish(job_id, worker_id, status="done")

    async def park(self, job_id: int, worker_id: str | None = None) -> bool:
        """Waiting for an approval; a resume job is enqueued when the user decides."""
        return await self._finish(job_id, worker_id, status="parked")

    async def fail(
        self, job_id: int, error: str, retry_at: datetime | None = None, worker_id: str | None = None
    ) -> bool:
        if retry_at is None:
            return await self._finish(job_id, worker_id, status="failed", last_error=error)
        return await self._finish(job_id, worker_id, status="queued", last_error=error, available_at=retry_at)

    async def _finish(self, job_id: int, worker_id: str | None, **values: Any) -> bool:
        """Release the job. With worker_id, only if that worker still holds it (its lock may have expired
        and been taken over); returns False when it did not."""
        cond = [s.jobs.c.id == job_id]
        if worker_id is not None:
            cond += [s.jobs.c.locked_by == worker_id, s.jobs.c.status == "running"]
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(s.jobs).where(*cond).values(locked_by=None, locked_at=None, **values)
            )
        return result.rowcount == 1

    async def get(self, job_id: int) -> Job | None:
        row = await self._one(select(s.jobs).where(s.jobs.c.id == job_id))
        return Job(**row) if row else None

    async def for_task(self, task_id: str) -> list[Job]:
        rows = await self._all(select(s.jobs).where(s.jobs.c.task_id == task_id).order_by(s.jobs.c.id))
        return [Job(**r) for r in rows]

    async def queue_length(self) -> int:
        """Jobs waiting to run, including ones backing off."""
        async with self._engine.connect() as conn:
            query = select(func.count()).where(s.jobs.c.status == "queued")
            return int((await conn.execute(query)).scalar_one())


# ---------------------------------------------------------------- outbox


class ActionRequest(BaseModel):
    id: str
    task_id: str | None
    tool: str
    args_json: str
    mode: str
    status: str
    content_hash: str
    idempotency_key: str
    created_at: datetime
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    result_json: str | None = None


class ActionRepo(_Repo):
    """The outbox: proposed, approved and executed tool calls."""

    async def create(
        self,
        action_id: str,
        task_id: str | None,
        tool: str,
        args: dict[str, Any],
        mode: str,
        status: str,
        content_hash: str,
        idempotency_key: str,
        expires_at: datetime | None = None,
    ) -> ActionRequest:
        values: dict[str, Any] = {
            "id": action_id,
            "task_id": task_id,
            "tool": tool,
            "args_json": json.dumps(args, sort_keys=True),
            "mode": mode,
            "status": status,
            "content_hash": content_hash,
            "idempotency_key": idempotency_key,
            "created_at": self._now(),
            "expires_at": expires_at,
        }
        async with self._engine.begin() as conn:
            await conn.execute(insert(s.action_requests).values(**values))
        return ActionRequest(**values)

    async def get(self, action_id: str) -> ActionRequest | None:
        row = await self._one(select(s.action_requests).where(s.action_requests.c.id == action_id))
        return ActionRequest(**row) if row else None

    async def by_idempotency_key(self, key: str) -> ActionRequest | None:
        row = await self._one(select(s.action_requests).where(s.action_requests.c.idempotency_key == key))
        return ActionRequest(**row) if row else None

    async def transition(
        self,
        action_id: str,
        from_status: str,
        to_status: str,
        expected_hash: str | None = None,
        result_json: str | None = None,
    ) -> None:
        """Compare-and-set the status. Raises ApprovalConflict if status or hash do not match."""
        cond = [s.action_requests.c.id == action_id, s.action_requests.c.status == from_status]
        if expected_hash is not None:
            cond.append(s.action_requests.c.content_hash == expected_hash)
        values: dict[str, Any] = {"status": to_status}
        if from_status in {"pending", "proposed"}:
            values["decided_at"] = self._now()
        if result_json is not None:
            values["result_json"] = result_json
        async with self._engine.begin() as conn:
            result = await conn.execute(update(s.action_requests).where(*cond).values(**values))
        if result.rowcount != 1:
            raise ApprovalConflict(
                f"action {action_id} is not {from_status!r}"
                + (" with the expected content" if expected_hash is not None else "")
            )

    async def by_status(self, status: str) -> list[ActionRequest]:
        rows = await self._all(
            select(s.action_requests)
            .where(s.action_requests.c.status == status)
            .order_by(s.action_requests.c.created_at)
        )
        return [ActionRequest(**r) for r in rows]


# ---------------------------------------------------------------- audit and undo


class AuditRepo(_Repo):
    """Append-only: no update or delete methods."""

    async def append(
        self,
        tool: str,
        mode: str,
        args_digest: str,
        result: str,
        actor: str,
        task_id: str | None = None,
        recipe: str | None = None,
        action_id: str | None = None,
    ) -> int:
        async with self._engine.begin() as conn:
            res = await conn.execute(
                insert(s.audit_log).values(
                    at=self._now(),
                    task_id=task_id,
                    action_id=action_id,
                    recipe=recipe,
                    tool=tool,
                    mode=mode,
                    args_digest=args_digest,
                    result=result,
                    actor=actor,
                )
            )
        return int(res.inserted_primary_key[0])  # type: ignore[index]

    async def get(self, audit_id: int) -> dict[str, Any] | None:
        return await self._one(select(s.audit_log).where(s.audit_log.c.id == audit_id))

    async def for_action(self, action_id: str) -> list[dict[str, Any]]:
        return await self._all(
            select(s.audit_log).where(s.audit_log.c.action_id == action_id).order_by(s.audit_log.c.id)
        )

    async def for_task(self, task_id: str) -> list[dict[str, Any]]:
        return await self._all(
            select(s.audit_log).where(s.audit_log.c.task_id == task_id).order_by(s.audit_log.c.id)
        )

    async def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self._all(select(s.audit_log).order_by(s.audit_log.c.id.desc()).limit(limit))


class UndoRepo(_Repo):
    async def add(self, audit_id: int, inverse_tool: str, inverse_args: dict[str, Any]) -> int:
        async with self._engine.begin() as conn:
            res = await conn.execute(
                insert(s.undo_ops).values(
                    audit_id=audit_id, inverse_tool=inverse_tool, inverse_args_json=json.dumps(inverse_args)
                )
            )
        return int(res.inserted_primary_key[0])  # type: ignore[index]

    async def for_audit(self, audit_id: int) -> dict[str, Any] | None:
        return await self._one(select(s.undo_ops).where(s.undo_ops.c.audit_id == audit_id))

    async def mark(self, undo_id: int, from_status: str, to_status: str) -> bool:
        async with self._engine.begin() as conn:
            res = await conn.execute(
                update(s.undo_ops)
                .where(s.undo_ops.c.id == undo_id, s.undo_ops.c.status == from_status)
                .values(status=to_status)
            )
        return res.rowcount == 1


# ---------------------------------------------------------------- memory


class MemoryRepo(_Repo):
    """Task summaries, facts, task events and links."""

    async def get_summary(self, task_id: str) -> dict[str, Any] | None:
        return await self._one(select(s.task_summaries).where(s.task_summaries.c.task_id == task_id))

    async def put_summary(self, task_id: str, text: str, expected_version: int | None = None) -> int:
        """Write a new summary version. With expected_version, fails if someone else wrote first."""
        now = self._now()
        async with self._engine.begin() as conn:
            current = (
                await conn.execute(
                    select(s.task_summaries.c.version).where(s.task_summaries.c.task_id == task_id)
                )
            ).scalar()
            if expected_version is not None and current != expected_version:
                raise ApprovalConflict(
                    f"summary for {task_id} is at version {current}, not {expected_version}"
                )
            version = (current or 0) + 1
            if current is None:
                await conn.execute(
                    insert(s.task_summaries).values(
                        task_id=task_id, version=version, text=text, updated_at=now
                    )
                )
            else:
                await conn.execute(
                    update(s.task_summaries)
                    .where(s.task_summaries.c.task_id == task_id)
                    .values(version=version, text=text, updated_at=now)
                )
        return version

    async def set_fact(
        self, task_id: str, key: str, value: str, status: str = "known", source_ref: str | None = None
    ) -> None:
        stmt = sqlite_insert(s.task_facts).values(
            task_id=task_id,
            key=key,
            value=value,
            status=status,
            source_ref=source_ref,
            updated_at=self._now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["task_id", "key"],
            set_={"value": value, "status": status, "source_ref": source_ref, "updated_at": self._now()},
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def facts(self, task_id: str) -> list[dict[str, Any]]:
        return await self._all(
            select(s.task_facts).where(s.task_facts.c.task_id == task_id).order_by(s.task_facts.c.key)
        )

    async def add_task_event(
        self,
        task_id: str,
        actor: str,
        channel: str,
        text_pseudonymised: str,
        source_ref: str | None = None,
        raw_expires_at: datetime | None = None,
        at: datetime | None = None,
    ) -> int:
        async with self._engine.begin() as conn:
            res = await conn.execute(
                insert(s.task_events).values(
                    task_id=task_id,
                    at=at or self._now(),
                    actor=actor,
                    channel=channel,
                    text_pseudonymised=text_pseudonymised,
                    source_ref=source_ref,
                    raw_expires_at=raw_expires_at,
                )
            )
        return int(res.inserted_primary_key[0])  # type: ignore[index]

    async def task_events(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._all(
            select(s.task_events)
            .where(s.task_events.c.task_id == task_id)
            .order_by(s.task_events.c.at.desc(), s.task_events.c.id.desc())
            .limit(limit)
        )
        return list(reversed(rows))

    async def link(self, task_id: str, kind: str, external_id: str) -> None:
        stmt = sqlite_insert(s.task_links).values(task_id=task_id, kind=kind, external_id=external_id)
        async with self._engine.begin() as conn:
            await conn.execute(stmt.on_conflict_do_nothing())

    async def task_for_link(self, kind: str, external_id: str) -> str | None:
        row = await self._one(
            select(s.task_links.c.task_id).where(
                s.task_links.c.kind == kind, s.task_links.c.external_id == external_id
            )
        )
        return row["task_id"] if row else None

    async def links(self, task_id: str) -> list[dict[str, Any]]:
        return await self._all(select(s.task_links).where(s.task_links.c.task_id == task_id))


# ---------------------------------------------------------------- token map


TOKEN_KEY_SECRET = "token_map_key"


class TokenCipher:
    """Fernet encryption for real values, plus a keyed digest so equal values map to one token."""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)
        self._mac_key = hashlib.sha256(b"token-map-digest" + key).digest()

    @classmethod
    def from_secrets(cls) -> "TokenCipher":
        """Load the key from Credential Manager, creating it on first use."""
        try:
            key = get_secret(TOKEN_KEY_SECRET).encode()
        except ConfigError:
            key = Fernet.generate_key()
            set_secret(TOKEN_KEY_SECRET, key.decode())
        return cls(key)

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, blob: bytes) -> str:
        return self._fernet.decrypt(blob).decode("utf-8")

    def digest(self, kind: str, value: str) -> str:
        return hmac.new(self._mac_key, f"{kind}\x00{value}".encode(), hashlib.sha256).hexdigest()


class TokenMapRepo(_Repo):
    def __init__(self, engine: AsyncEngine, clock: Clock, cipher: TokenCipher) -> None:
        super().__init__(engine, clock)
        self._cipher = cipher

    async def put(self, token: str, kind: str, real_value: str, task_id: str | None = None) -> str:
        """Store a mapping. If the value is already mapped, returns the existing token instead."""
        digest = self._cipher.digest(kind, real_value)
        existing = await self.token_for(kind, real_value)
        if existing:
            return existing
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(s.token_map).values(
                        task_id=task_id,
                        token=token,
                        kind=kind,
                        real_value_encrypted=self._cipher.encrypt(real_value),
                        value_digest=digest,
                    )
                )
        except IntegrityError:
            raced = await self.token_for(kind, real_value)
            if raced is None:
                raise
            return raced
        return token

    async def token_for(self, kind: str, real_value: str) -> str | None:
        row = await self._one(
            select(s.token_map.c.token).where(
                s.token_map.c.kind == kind,
                s.token_map.c.value_digest == self._cipher.digest(kind, real_value),
            )
        )
        return row["token"] if row else None

    async def real_value(self, token: str) -> str | None:
        row = await self._one(select(s.token_map.c.real_value_encrypted).where(s.token_map.c.token == token))
        return self._cipher.decrypt(row["real_value_encrypted"]) if row else None

    async def count(self, kind: str) -> int:
        async with self._engine.connect() as conn:
            return int(
                (await conn.execute(select(func.count()).where(s.token_map.c.kind == kind))).scalar_one()
            )


# ---------------------------------------------------------------- directory, lessons, usage, health, kv


class DirectoryRepo(_Repo):
    async def upsert(
        self, email: str, name: str, role: str | None = None, site: str | None = None, team: str | None = None
    ) -> None:
        values = {"name": name, "role": role, "site": site, "team": team, "updated_at": self._now()}
        stmt = sqlite_insert(s.directory).values(email=email.lower(), **values)
        async with self._engine.begin() as conn:
            await conn.execute(stmt.on_conflict_do_update(index_elements=["email"], set_=values))

    async def get(self, email: str) -> dict[str, Any] | None:
        return await self._one(select(s.directory).where(s.directory.c.email == email.lower()))

    async def by_site(self, site: str) -> list[dict[str, Any]]:
        return await self._all(select(s.directory).where(s.directory.c.site == site))


class LessonRepo(_Repo):
    async def add(self, lesson_id: str, scope: str, text: str, source_ref: str | None = None) -> None:
        now = self._now()
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(s.lessons).values(
                    id=lesson_id,
                    scope=scope,
                    text=text,
                    source_ref=source_ref,
                    created_at=now,
                    updated_at=now,
                )
            )

    async def set_status(self, lesson_id: str, status: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(s.lessons)
                .where(s.lessons.c.id == lesson_id)
                .values(status=status, updated_at=self._now())
            )

    async def active(self, scope: str | None = None) -> list[dict[str, Any]]:
        query = select(s.lessons).where(s.lessons.c.status == "active")
        if scope is not None:
            query = query.where(s.lessons.c.scope == scope)
        return await self._all(query.order_by(s.lessons.c.created_at))


class UsageRepo(_Repo):
    async def record(
        self,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        ok: bool = True,
        latency_ms: int | None = None,
        task_id: str | None = None,
        recipe: str | None = None,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(s.model_usage).values(
                    at=self._now(),
                    model=model,
                    purpose=purpose,
                    task_id=task_id,
                    recipe=recipe,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    ok=ok,
                )
            )

    async def calls_since(self, model: str, since: datetime) -> int:
        async with self._engine.connect() as conn:
            query = select(func.count()).where(s.model_usage.c.model == model, s.model_usage.c.at >= since)
            return int((await conn.execute(query)).scalar_one())


class HealthRepo(_Repo):
    async def report(self, connector: str, status: str, error: str | None = None) -> None:
        now = self._now()
        values: dict[str, Any] = {"status": status, "last_error": error, "checked_at": now}
        if status == "ok":
            values["last_ok_at"] = now
        stmt = sqlite_insert(s.connector_health).values(connector=connector, **values)
        async with self._engine.begin() as conn:
            await conn.execute(stmt.on_conflict_do_update(index_elements=["connector"], set_=values))

    async def all(self) -> list[dict[str, Any]]:
        return await self._all(select(s.connector_health).order_by(s.connector_health.c.connector))


class KvRepo(_Repo):
    """Delta tokens, cursors and small state."""

    async def get(self, key: str) -> str | None:
        row = await self._one(select(s.kv.c.value).where(s.kv.c.key == key))
        return row["value"] if row else None

    async def set(self, key: str, value: str) -> None:
        stmt = sqlite_insert(s.kv).values(key=key, value=value, updated_at=self._now())
        async with self._engine.begin() as conn:
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["key"], set_={"value": value, "updated_at": self._now()}
                )
            )

    async def delete(self, key: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(delete(s.kv).where(s.kv.c.key == key))
