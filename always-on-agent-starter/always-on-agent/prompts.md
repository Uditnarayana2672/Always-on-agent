# prompts.md

Phase-by-phase prompts that build the Always-On Agent end to end in Claude Code (VS Code). Run them in order. Each prompt is self-contained: paste it as is into a fresh Claude Code session at the repository root.

## How to use this file

1. Put `CLAUDE.md`, `progress.md`, `prompts.md` and `docs/architecture.md` in an empty repository and commit them.
2. Open the repository in VS Code and start Claude Code.
3. Paste one prompt. Let it finish, review the diff, run the app if the prompt says so, then commit.
4. Start a new session for the next prompt. Long sessions drift; `progress.md` carries the state between them.
5. If a prompt is blocked by an approval or a missing credential, mark it `[!]` in `progress.md`, note the blocker, and move to the next prompt that does not depend on it. Everything up to prompt 1.18 can be built and tested with fakes.

Every prompt follows the same frame:

- **Start:** follow `CLAUDE.md`; read `progress.md` and the named architecture sections.
- **Build:** what to create.
- **Done when:** acceptance criteria.
- **Finish:** run the checks, update `progress.md`, summarise what changed and what the user must do next.

A prompt marked **(needs you)** includes a step only the user can do, such as signing in or answering a question.

---

## Phase 0 — Foundations and spikes

### Prompt 0.1 — Repository scaffold

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections 3.2, 13 and 14.

Build the repository scaffold for the Always-On Agent.

1. backend/: a uv project (Python 3.12) named "always-on-agent" with package src/agent and the
   subpackages from architecture section 13 (core, db, runtime, graphs, model, policy, approvals,
   ledger, memory, privacy, recipes, adapters, adapters/fakes, channels, api). Each gets an
   __init__.py and a one-line module docstring naming its feature IDs.
2. Dependencies from the CLAUDE.md stack table, pinned to exact versions that are at least two
   weeks old. Dev dependencies: pytest, pytest-asyncio, hypothesis, respx, ruff, mypy.
3. A console script "agentd" (Typer) with subcommands run, doctor, console, status. Each prints
   "not implemented yet" for now except status, which prints the version.
4. Config: config/config.example.yaml exactly as architecture section 14, and config/policy.yaml
   with these tools and modes (Hand off entries locked: true):
   Silent: read_mail, search_mail, read_report_email, read_chat, read_calendar, get_transcript,
     search_workbook, search_memory, ocr_local, read_directory, get_refresh_history,
     read_sharepoint_list, sql_count
   Report: move_mail, flag_mail, categorise, save_draft, update_ledger, add_hold,
     update_directory, write_memory
   Ask: send_mail, post_chat, trigger_refresh, trigger_dataflow_refresh, update_refresh_schedule,
     edit_shared_list, add_comment_by_reply
   Hand off: delete_item, change_ticket_state, grant_access, message_outside_company,
     enter_credentials
   config/recipes/ (empty), config/parsers/servicenow/ (empty), config/directory.example.csv
   with the header row only.
5. console/: Vite + React 18 + TypeScript app with @carbon/react installed, one placeholder page
   that says "Always-On Agent". Dev server proxies /api to http://127.0.0.1:8765.
6. scripts/dev.ps1 that starts backend and console for development.
7. .gitignore that excludes config/config.yaml, *.db, logs/, memory/, .venv/, node_modules/,
   console/dist/, and any file under fixtures/private/.
8. Tooling config: ruff (line length 110), mypy strict for src/agent, pytest asyncio mode auto.
9. A smoke test that imports every subpackage.

Done when:
- uv run pytest -q passes; ruff and mypy are clean.
- uv run agentd status prints the version.
- npm run build in console/ succeeds.

Finish: update progress.md (tick 0.1, set next prompt 0.2), summarise the tree you created.
```

### Prompt 0.2 — Core utilities: config, clock, ids, logging, secrets

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections 10.3, 14 and F17.

Build agent.core:

1. config.py: Pydantic settings model matching config.example.yaml exactly. Load from
   config/config.yaml (falls back to the example with a warning). Validate time zones with
   zoneinfo, times as HH:MM, data_rule.level in {metadata, text, chat, images}. Expose get_config().
2. clock.py: a Clock protocol with now_utc(), now_local(), to_local(dt), is_work_time(dt),
   is_quiet(dt), next_occurrence(local_time, days) and caller_time_to_local(hh_mm, date).
   A SystemClock and a FrozenClock for tests. Handles Europe DST changes correctly while
   Asia/Kolkata has none.
3. ids.py: ULID generation; task id helpers: parse INC numbers (INC + 8 digits), ENH-n, REF-n,
   PBI-xxx; normalise case.
4. logging.py: structured JSON logging with a redaction filter that masks bearer tokens, API
   keys, xoxb-/xapp- Slack tokens, email addresses and anything registered as a secret at
   runtime. Daily rotating files in logs/, 14 days kept.
5. secrets.py: get_secret(name) and set_secret(name, value) via keyring under the service name
   "always-on-agent"; registers each loaded value with the redaction filter; a CLI
   "agentd secret set <name>" that prompts without echo.
6. errors.py: AgentError hierarchy: ConfigError, AdapterError (with retryable flag),
   PolicyViolation, DataRuleViolation, ApprovalConflict.

Tests: clock across the last Sunday of March and October in Europe/Zurich (chase window
13:30 IST in winter, 12:30 IST in summer), quiet hours spanning midnight, holiday skip,
redaction of each secret type, config validation errors.

Done when tests, ruff and mypy pass.

Finish: update progress.md (tick 0.2).
```

### Prompt 0.3 — Database schema and repositories

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections 5, F08 and F13.

Build agent.db:

1. SQLAlchemy 2 Core table definitions for every table in architecture section 5, plus
   recipes_state, lessons, directory, model_usage, connector_health, kv, schedule_runs.
   Unique constraint on events(source, external_id). Index jobs(status, priority, available_at).
   documents_fts as an FTS5 virtual table created in a migration.
2. Alembic set up with an initial migration. SQLite opened with WAL, foreign keys on,
   busy_timeout 5000 ms. The URL comes from config so Postgres can replace it in Phase 3
   (keep FTS5-specific code behind a SearchIndex interface).
3. Repository classes (async, using aiosqlite through SQLAlchemy async engine): TaskRepo,
   EventRepo, JobRepo, ActionRepo (outbox), AuditRepo, UndoRepo, MemoryRepo (summaries, facts,
   task_events, links), TokenMapRepo (real values encrypted with a key from secrets),
   DirectoryRepo, LessonRepo, UsageRepo, HealthRepo, KvRepo.
4. JobRepo.claim(worker_id) must be atomic: pick the highest priority available job whose task
   has no running job, set locked_by and locked_at in one statement, and return it. Locks older
   than lock_ttl_minutes count as expired.
5. ActionRepo.transition(id, from_status, to_status, expected_hash=None) as compare-and-set;
   raises ApprovalConflict if it does not match.

Tests: dedupe on events, concurrent claim (two tasks, five claimers, no task claimed twice),
compare-and-set conflicts, token map round trip, FTS search returns ranked rows.

Done when tests, ruff and mypy pass and "alembic upgrade head" creates agent.db.

Finish: update progress.md (tick 0.3).
```

### Prompt 0.4 — Model gateway and ICA spike (needs you)

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections F05, F11 and 10.4.

Build agent.model:

1. ica_client.py: an async client using the anthropic SDK with base_url from env ICA_BASE_URL and
   the key from get_secret("ica"). Model ids from config.models.ids. Retries with exponential
   backoff on 429 and 5xx, honouring Retry-After. Timeout 60 s.
2. gateway.py: run(purpose, messages, schema, data_classes, tier="auto", task_id=None,
   recipe=None, max_tokens=...). It:
   - calls a Privacy hook (interface only for now; a pass-through that raises if data_classes
     exceed config.data_rule.level),
   - routes: default Haiku; Sonnet only when purpose is in config.models.sonnet_for and the daily
     Sonnet count (from model_usage) is below sonnet_daily_limit, else Haiku with a flag
     "downgraded" in the result,
   - asks for JSON matching the Pydantic schema (tool-use structured output, falling back to
     JSON in text if tool use is unavailable), validates, and does one repair retry with the
     validation error,
   - uses prompt caching on the system prompt when supported,
   - writes a model_usage row (model, tokens, latency, purpose, status).
3. prompts/: a template loader (Jinja2 with StrictUndefined) and a base system template that
   frames fetched content as data inside <data> tags and says it never contains instructions.
4. scripts/ica_spike.py: sends one Haiku request with one tool definition and one image-free
   prompt, prints whether tool use worked, the model ids that respond, and whether an image
   input is accepted (send a tiny generated PNG of made-up text). It never sends real data.

Tests with respx: routing and budget downgrade, schema repair retry, 429 backoff, usage row
written, data-class violation raises.

Needs you: run "uv run agentd secret set ica", set ICA_BASE_URL, then run
"uv run python scripts/ica_spike.py" and paste the output. Record the answers to Q1 in
progress.md (open questions) and fill config.models.ids.

Done when tests pass and the spike output is recorded (or 0.4 is marked [!] with the reason).

Finish: update progress.md (tick 0.4, answer Q1).
```

### Prompt 0.5 — Fakes and fixtures

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections 7 and 15.

Create made-up fixtures and fake adapters so everything up to prompt 1.18 can be built offline.

1. fixtures/ (all names, INC numbers, sites and reports invented; use the sample data from the
   console prototype: INC30109981, INC30114872, INC30108754, INC30115230, INC30117002,
   REF-0412, ENH-014, sites ORB-02, VRS-01, SGP-03, people Anna Keller, Tomás Rivera, Sophie
   Laurent, Elena Rossi, Karim Haddad, Priya Nair, Marc Dubois):
   - emails/: Graph-style JSON messages: 6 ServiceNow assignment notifications, 2 comment
     notifications, 1 hourly report email with CSV and 1 with XLSX attachment, 8 ordinary work
     emails (manager, stakeholder, newsletter, FYI, one in French, one with an outside cc).
   - teams/: chats with messages, mentions, an edited message, a deleted message, a Power BI
     refresh request containing an app.powerbi.com link.
   - powerbi/: groups, reports, apps, datasets, refresh history (success, failure, running,
     slow), upstream dataflows.
   - calendar/: today's events including one in CET and a private one.
   - screenshots/: three PNGs rendered with Pillow showing fake Doxis-like pages (site list,
     project properties, site page with KM/LBA).
   - servicenow_reports/: CSV and XLSX versions of the same report.
2. agent/adapters/base.py: Adapter protocol (name, health(), tools()) and typed method
   protocols for each adapter in architecture section 7.
3. agent/adapters/fakes/: in-memory fakes for Outlook, Calendar, Teams, Slack, Power BI,
   SharePoint, ServiceNow email, SQL, OCR. They load fixtures, support "deliver new item now"
   for tests, record every write call, and can be told to fail (429, 410, timeout).
4. A FakeWorld test helper that wires all fakes and a FrozenClock together.
5. agentd run --fake: selects fakes everywhere (no real network).

Tests: each fake loads its fixtures; failure injection works.

Done when tests pass and no fixture contains a real person, address or id.

Finish: update progress.md (tick 0.5).
```

### Prompt 0.6 — Connectivity spikes and `agentd doctor` (needs you)

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections F17, F18, 16 and A01–A05.

Build agentd doctor and the sign-in flow. These touch real services, read-only.

1. agent/core/auth.py: MSAL public client (tenant and client id from config), device-code and
   interactive sign-in, token cache with msal-extensions (DPAPI on Windows). get_token(scopes)
   tries silent first and raises NeedsSignIn otherwise.
2. agentd doctor runs checks and prints one line each with OK, WARN or FAIL and a fix:
   - config valid, time zones, data rule level
   - ICA reachable and model ids respond (reuse the spike)
   - proxy / CA bundle works for ICA, graph.microsoft.com, api.powerbi.com, slack.com
   - Graph sign-in works; GET /me; read 1 mail; read 1 chat; read today's calendar
   - Teams: can the user post to their own chat? Use the "notes to self" chat if Graph exposes
     it; do not post anywhere else. Report what happened.
   - Power BI: list groups; for one dataset, read refresh history; report workspace capacity
     type if visible
   - Slack: app and bot tokens present; Socket Mode connects; can DM the user
   - laptop: power and lock settings (powercfg /query and screen-saver lock timeout) for Q11
   Doctor never sends anything except the optional Teams self-note and Slack DM test, each
   with the text "Always-On Agent connectivity test".
3. Save results to connector_health.

Needs you: fill tenant_id and client_id if you have an app registration (otherwise the Graph
checks print the request to file with IT), store Slack tokens with agentd secret set, run
"uv run agentd doctor", and paste the output.

Done when doctor runs end to end and progress.md open questions Q1, Q3, Q4, Q5, Q9, Q11 are
answered or marked unknown with the reason.

Finish: update progress.md (tick 0.6; record answers; add blockers for anything refused).
```

---

## Phase 1 — MVP on the laptop

### Prompt 1.1 — Event intake and coordinator

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections 4.1 and F03.

Build agent.runtime.coordinator:

1. Event Pydantic model: id, source (mail, teams, slack, powerbi, servicenow_report, schedule,
   api), kind, external_id, received_at, payload, task_hint.
2. Coordinator.ingest(raw, source): normalise to Event, dedupe via EventRepo, resolve the task:
   a) ids in subject/body (INC, ENH, REF) — all of them, one job per task;
   b) task_links lookup (mail conversation id, Teams chat id, Slack thread ts, dataset id);
   c) recipe create rules (a registry recipes add to in 1.9; stub with an empty list now);
   d) otherwise a digest-only event.
3. Priority function from architecture F03 with age boost applied at claim time.
4. Enqueue jobs through JobRepo with kind (new_event, resume, schedule).
5. Handle: RE:/FW: prefixes, forwarded ServiceNow mail, edited Teams messages (new event with
   kind message_edited), deleted messages (event recorded, never actionable).

Tests with FakeWorld: duplicate delivery ignored; one email with two INC numbers creates two
jobs; a Teams reply in a linked chat routes to its task; unknown chatter becomes digest-only;
edited and deleted message handling.

Done when tests, ruff and mypy pass.

Finish: update progress.md (tick 1.1).
```

### Prompt 1.2 — Task ledger and state machine

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections 4.2, F08 and R08 (clock rules only).

Build agent.ledger:

1. Task model and a state machine with the transitions in architecture 4.2. Illegal transitions
   raise. Only actor "user" may move to done, except actor "source_system" when the ServiceNow
   report shows Closed.
2. Clock: start rule (opened | assigned | email_received) and pause_on_hold from config;
   age(task, now) respects pauses; threshold events at 12, 18 and 22 hours emitted once each.
3. Heartbeat updates on every worker touch; stalled(now) lists tasks with no update in 24 h.
4. Links: add_link(task, kind, external_id); unique per kind and external id.
5. Reassignment and reopen helpers used later by the ServiceNow adapter.

Tests: every legal and illegal transition; clocks with and without pause across a weekend;
thresholds fire once; reopen moves done to in_progress.

Done when tests, ruff and mypy pass.

Finish: update progress.md (tick 1.2).
```

### Prompt 1.3 — Tool registry, policy gate, outbox, audit and undo

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections 3.3, F06 and F13, and config/policy.yaml.

Build agent.policy and the outbox:

1. registry.py: @tool decorator registering name, Pydantic args model, base mode, an optional
   mode_fn(args, ctx) for args-dependent modes (for example trigger_refresh is Report only for
   the user's own dataset after a known transient error), an optional inverse for undo, and the
   adapter method it calls.
2. gate.py: check(tool, args, ctx) -> Decision(mode, reasons, args_digest). Implements every
   rule in the F06 diagram: unknown tool -> Ask; Hand off list; outside recipients (final list,
   including cc/bcc and reply-all expansion) against company_domains -> Hand off; recipients
   must be task participants or allowed_groups; mode can never be lowered by recipe or model;
   triggered_by_fetched_content -> at least Ask and flagged; shadow recipe -> record only;
   paused -> nothing above Silent runs.
3. outbox.py: execute(action) claims by idempotency key, calls the adapter, records the result,
   writes an audit row, registers the inverse for Report actions. Before retrying a send it asks
   the adapter whether the idempotency key already exists (Sent Items header or chat tag).
4. audit.py and undo.py: every gate decision and execution is audited (including blocked and
   shadow). undo(audit_id) checks current state, runs the inverse once, audits the undo.

Tests: a table-driven test with at least 30 cases covering every rule; outbox idempotency when
the adapter succeeded but the process "crashed" before recording; undo refuses when the item
changed; Hand off tools cannot be registered as anything else.

Done when tests pass and coverage of agent.policy is at least 95 %.

Finish: update progress.md (tick 1.3).
```

### Prompt 1.4 — Job queue and worker pool

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section F04.

Build agent.runtime.workers:

1. WorkerPool(size from config): asyncio tasks that loop: claim job (JobRepo.claim), run the
   handler with a timeout, then mark done, parked (waiting for approval), retry or failed.
2. Retry policy: attempts < 2 -> back off 2 then 10 minutes; else failed, task to needs_you,
   and a notification request with the error summary.
3. Age boost for starvation; lock TTL recovery; graceful drain on shutdown (stop claiming,
   wait for running jobs to reach their next checkpoint, up to 30 s).
4. A handler registry keyed by job kind; for now a test handler.
5. Expose pool state for the API: per worker current task, step and elapsed time; queue length.

Tests: four workers, ten tasks, two events per task: no task runs twice at once; poison job
fails twice then surfaces; shutdown drains; a stuck lock is recovered after TTL.

Done when tests, ruff and mypy pass.

Finish: update progress.md (tick 1.4).
```

### Prompt 1.5 — LangGraph task graph and approvals

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections 4.1, 4.3, F07 and F10.

Build agent.graphs and agent.approvals:

1. A LangGraph StateGraph "task_graph" with an AsyncSqliteSaver checkpointer in
   checkpoints.db; thread_id = task id. Nodes: load_context, run_recipe_step, propose_action,
   gate, await_approval (uses interrupt()), execute (through the outbox), update_memory, end.
   Max steps per run from config.
2. Approval service: create(task, tool, args, content) stores an action_request with
   content_hash = sha256 of the normalised content and args; posts to channels (interface only;
   real Slack in 1.11); approve(id, hash) and reject(id, reason) use compare-and-set;
   edit(id, new_content) creates a new hash and marks edited; expire() after 24 h;
   invalidate_for_task(task, except_actor="user") when a new event arrives.
3. On a decision, enqueue a resume job; the worker resumes the graph with
   Command(resume=decision).
4. Wire the coordinator so any new event on a task calls invalidate_for_task before enqueueing.

Tests: full loop with fakes — event, draft, approval card, approve, execute once; edit then
approve the edited hash; approve the old hash after an edit fails; caller reply invalidates;
double approve resolves once; crash between execute and record does not double-send.

Done when tests, ruff and mypy pass.

Finish: update progress.md (tick 1.5).
```

### Prompt 1.6 — Scheduler, catch-up, quiet hours and controls

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections F02 and F16.

Build agent.runtime.scheduler and controls:

1. APScheduler AsyncIOScheduler with a SQLAlchemy job store in agent.db. Register cron jobs
   (brief, check-ins, nightly digest) in the user's time zone and pollers per
   config.schedule.poll_minutes.
2. Each fire emits a schedule Event through the coordinator; schedule_runs records every run.
3. Catch-up on start and on wake (detect a gap in the heartbeat): run each missed cron job once,
   coalesce missed check-ins, run the brief once if the gap crossed 09:00 on a work day.
4. Quiet hours and push cap are enforced at notification time, not at schedule time.
5. Controls service: pause, resume (triggers catch-up), checkin_now, leave_mode on/off
   (no chases, no caller drafts, brief paused), reset (requires a confirmation token, keeps the
   audit log). Persist control state in kv.

Tests with FrozenClock: sleep from 18:00 to 10:30 next day runs the brief once and one
check-in; pause blocks everything above Silent; leave mode suppresses chases; holiday skip.

Done when tests, ruff and mypy pass.

Finish: update progress.md (tick 1.6).
```

### Prompt 1.7 — Privacy layer

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section F11.

Build agent.privacy and connect it to the model gateway's Privacy hook:

1. pseudonymiser.py: per task, stable tokens (USER_n, KM_n, LBA_n, CALLER, AFFECTED) from known
   people (task participants, directory, mail headers), regexes for emails, phone numbers and
   user ids, and signature and greeting detection. Accent-insensitive matching (Tomás/Tomas),
   whole-word only. Store mappings in TokenMapRepo.
2. leak_check(text, task): fails if any known real name or email remains.
3. restore(draft, task): replace tokens with real values locally; fail if the draft contains a
   token that does not exist for the task.
4. data_rule.py: data classes metadata, text, chat, images; allowed(level) and a reducer that
   strips disallowed classes from a context packet (for example drop message bodies at metadata
   level and keep subject-free metadata only).
5. outbound.py: checks on every outgoing draft: forbidden phrases list (config; default
   "KPI", "24 hours", "metric" for caller-facing drafts), outside recipients, attachments.

Tests: hypothesis-based test that random names inserted into text never survive
pseudonymisation; restore round trip; invented token blocked; reducer at each level; forbidden
phrase block.

Done when tests pass and the gateway raises DataRuleViolation or LeakDetected appropriately.

Finish: update progress.md (tick 1.7).
```

### Prompt 1.8 — Memory and context builder

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections F09 and F10.

Build agent.memory:

1. personal.py: read memory/profile.md and memory/preferences.md (create from templates on first
   run); expose as a section with a token estimate.
2. task_memory.py: add_event (pseudonymised text, source_ref, raw_expires_at), facts upsert with
   source_ref, card fields, summary get/set with version.
3. summary update: after each run, ask Haiku to rewrite the running summary to at most 120
   words from the previous summary plus new events; then a code check that every id, date and
   state in the summary matches the card; on mismatch, one rewrite with the mismatch listed.
4. knowledge.py: SearchIndex interface with the SQLite FTS5 implementation; search(query, kind,
   k) returns snippets with source_ref.
5. context_builder.py: build(task, recipe) assembles sections with budgets from architecture
   F10, trims each to its budget, and exposes a search_memory tool (Silent) that returns one
   event, one document or more precedents on request.
6. retention job: nightly purge of raw text after retention.raw_text_days, screenshots after
   retention.screenshots_days.

Tests: budgets respected for a task with 200 events; summary mismatch triggers a rewrite
(recorded model responses); retention deletes only expired rows; search ranks a matching
precedent first.

Done when tests, ruff and mypy pass.

Finish: update progress.md (tick 1.8).
```

### Prompt 1.9 — Recipe engine

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections F12 and 8.

Build agent.recipes:

1. schema.py: Pydantic model of the recipe YAML in architecture F12 (name, project, status,
   trigger {schedule | poll | event, match, every}, creates_task, model, data_classes, tools,
   steps, context, instructions, output, exclusive_with).
2. loader.py: load config/recipes/*.yaml; validate tools exist in the registry and none is
   Hand off; import the steps module; on error mark the recipe off with the error in
   recipes_state. Hot reload on file change; running jobs keep their loaded version.
3. runner.py: inside the task graph, run Python steps first; steps get a StepContext with
   read-only adapters, the context packet, model.run, and propose(tool, args). Steps cannot call
   write methods directly (the StepContext does not expose them).
4. Shadow mode: proposals above Silent are recorded as would-do with the drafted content.
5. Register triggers with the scheduler and create rules with the coordinator.
6. A recipe test harness: run_recipe_on_fixture(slug, fixture) -> list of proposals and outputs.

Tests: invalid recipe is turned off with a clear error; a Hand off tool in a recipe is refused;
shadow records would-do; two matching recipes both run unless exclusive; steps module cannot
reach write methods.

Done when tests, ruff and mypy pass.

Finish: update progress.md (tick 1.9).
```

### Prompt 1.10 — Outlook mail and calendar adapters

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections A01 and A02.

Build agent.adapters.outlook_graph, outlook_com and calendar, passing the same contract tests
as the fakes from 0.5:

1. Graph mail: delta queries on Inbox and Sent Items, stored delta tokens in kv; full resync of
   7 days on 410; HTML to text; attachment metadata only; sensitivity labels mark the message
   metadata-only. Tools: read_mail, search_mail (Silent), move_mail, flag_mail, categorise,
   save_draft (Report, with inverses), send_mail (Ask; adds X-Agent-Idempotency header and can
   check Sent Items for it).
2. COM fallback (pywin32, Windows only): same interface through Outlook desktop; selected by
   config when Graph is unavailable; skips gracefully on non-Windows test runs.
3. Calendar: today's and tomorrow's events with time zones; private events skipped; tools
   read_calendar (Silent) and add_hold (Report, own calendar only, inverse delete).
4. Pollers emit raw items to the coordinator.
5. Throttling: honour Retry-After; record connector health.

Tests with respx: delta paging, 410 resync with dedupe, throttling, idempotency header lookup,
contract tests shared with the fakes.

Done when tests pass, and (needs you) agentd doctor shows mail and calendar OK with the real
account.

Finish: update progress.md (tick 1.10).
```

### Prompt 1.11 — Slack channel

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections A04 and F14.

Build agent.channels.slack_bot and agent.channels.cards:

1. slack_bolt app in Socket Mode inside agentd; reconnect handling; health updates.
2. Cards (Block Kit): brief, alert, approval (Approve, Edit, Reject, Open in console), hand-off,
   digest with Undo buttons. Long content links to the console instead of exceeding limits.
3. One thread per task titled "<task id> – <short title>"; store the thread ts in task_links.
   Replies in a task thread become events for that task (R17 handles them later; for now store
   them and acknowledge).
4. Button actions call the approval service with the hash embedded in the card; Edit opens a
   modal with the text; double clicks resolve once.
5. Slash command /agent with status, pause, resume, brief, tasks, undo <id>.
6. Notification router: push cap and quiet hours batching; fallback chain Slack -> Teams self
   chat (if Q4 is yes) -> email to self.

Tests: card builders snapshot tests; action handler compare-and-set; router batching under
the cap; fallback when Slack health is red.

Done when tests pass and (needs you) a test approval card arrives in your Slack DM from
agentd run --fake --real-slack and the Approve button resolves it.

Finish: update progress.md (tick 1.11).
```

### Prompt 1.12 — Teams adapter and notifications

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections A03 and F14.

Build agent.adapters.teams:

1. Poll /me/chats (active in 14 days plus chats linked to tasks) and their messages since the
   last cursor; mentions from the mentions array; edited and deleted flags.
2. Tools: read_chat (Silent), post_chat (Ask; message carries a hidden idempotency tag the
   outbox can search for), get_transcript (Silent; requires the chat data rule and the
   transcript scope; returns a clear "not available" result otherwise).
3. Teams self-note notifications if Q4 answered yes; otherwise the router skips this channel.
4. Health and throttling as in 1.10.

Tests with respx and contract tests against the fake.

Done when tests pass and (needs you) doctor shows Teams read OK.

Finish: update progress.md (tick 1.12).
```

### Prompt 1.13 — Power BI adapter

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section A05.

Build agent.adapters.powerbi:

1. resolve_link(url): parse group, app, report and dashboard ids from app.powerbi.com links
   (apps, reports, groups/me); look up the report and its dataset; return candidates when an app
   bundles several reports.
2. refresh_history(dataset_id, top=60), running_refresh(dataset_id), refreshes_today(dataset_id),
   upstream_dataflows(dataset_id), dataflow_transactions(dataflow_id), refresh_schedule.
3. Tools: get_refresh_history (Silent), trigger_refresh and trigger_dataflow_refresh (Ask by
   default; mode_fn makes a single retry Report for the user's own dataset after a known
   transient error), update_refresh_schedule (Ask).
4. Refusals: already running -> return the running refresh; at cap -> refuse with a clear
   reason; 403 -> hand off with the dataset owner if visible.
5. Poller for refresh failures feeding R05.

Tests with respx: each link format, cap counting across midnight UTC versus IST (document which
the service uses and follow it), running refresh, 403 hand-off.

Done when tests pass and (needs you) doctor lists your groups and one refresh history.

Finish: update progress.md (tick 1.13).
```

### Prompt 1.14 — Phase 1 recipes R01–R06

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section 8 (R01–R06).

Create config/recipes/*.yaml and agent/recipes/steps/*.py for:
R01 morning-brief, R02 inbox-triage, R03 draft-replies, R04 mention-digest,
R05 refresh-watch, R06 nightly-digest.

For each:
- trigger, tools, model, context and output exactly as architecture section 8.
- deterministic steps in Python (ranking, sender rules, counting); model steps only for
  classification and wording, each with a Pydantic schema and a versioned prompt template.
- R02 never deletes; manager mail stays in Inbox, flagged.
- R03 uses Sonnet when allowed, otherwise Haiku, and marks drafts made on Haiku.
- R05 retries once only for known transient errors on the user's own dataset (Report),
  otherwise alerts.
- R06 includes every Report action today with Undo, open approvals, hand-offs, and shadow
  would-do counts.
- Each recipe ships in status shadow.

Tests: run_recipe_on_fixture for each recipe with expected proposals; brief with nothing to
report; brief after a missed day; triage keeps manager mail; draft with an outside cc becomes
Hand off.

Done when tests pass and agentd run --fake produces a brief, triage actions and a nightly
digest in the fake Slack log.

Finish: update progress.md (tick 1.14).
```

### Prompt 1.15 — Console API

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section 9.

Build agent.api:

1. FastAPI app mounted inside agentd on 127.0.0.1:<config.console.port>; serves console/dist
   at / and the API at /api.
2. Auth: random session token at start, stored in Credential Manager; agentd console prints a
   one-time link that sets it in the browser (cookie, SameSite=Strict); every state-changing
   route needs it; CORS off.
3. Every endpoint in the architecture section 9 table, with Pydantic response models.
   Approve requires the content hash; edit returns a new hash.
4. GET /api/stream: server-sent events for task, approval, activity and worker changes.
5. Settings PUT accepts only the subset the console may change (schedule times, push cap,
   Sonnet options, policy rows that are not locked, recipe status) and never data_rule.level.

Tests: auth required; approve with a stale hash returns 409; settings cannot change locked
policy rows or the data rule; SSE emits after an approval.

Done when tests, ruff and mypy pass.

Finish: update progress.md (tick 1.15).
```

### Prompt 1.16 — Console UI

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section 9. If the HTML prototype
"Always-On Agent Console" is available in the repo under docs/prototype/, match its pages,
copy and behaviour.

Build the console in console/ with React 18, TypeScript, Vite, @carbon/react (default White
theme, Gray 100 in dark mode following the OS), TanStack Query and hash routing.

Shell: Carbon UI shell header with the product name "Always-On Agent" (no company name),
agent status on the right, side navigation: Morning brief, Tasks, Approvals, Recipes,
Activity, Memory, Connectors, Settings, with counts on Tasks and Approvals.

Pages:
- Morning brief: needs-you list linked to tasks, Doxis 24-hour clocks with 12/18/22 h ticks,
  Power BI health, Report actions with Undo, today's meetings, Run check-in, Pause.
- Tasks: board columns Needs you, In progress, Waiting on others, Scheduled, Done; project
  filter; cards with age bar for open Doxis tickets.
- Task detail: thread, inline approval, composer to message the task's agent, task card,
  running summary, checks with pass/fail/unknown, similar past tickets, Mark done.
- Approvals: queue with Approve, Edit (edited text needs its own approval), Reject, expiry.
- Recipes: grouped tables with status select; editor with tool checkboxes showing each tool's
  mode, Hand off tools disabled, live YAML preview, Test on a sample, Save in Shadow mode.
- Activity: worker pool tiles, mode filter, audit table with Undo.
- Memory: tabs About you, Site directory (stale flag over 90 days), Lessons (save or discard),
  Reference sources (re-index), Task memory explainer.
- Connectors: health tiles per adapter with what each can do.
- Settings: schedule, models, data rule (read-only status), action policy (Hand off rows
  locked), pause and reset with in-page confirmation.

Live updates through /api/stream. Every action shows a toast stating what happened. Works at
400 px wide.

Tests: Vitest + RTL for each page with mocked API; one Playwright smoke test against
agentd run --fake that approves a card and sees the task move.

Done when npm run test and the smoke test pass and the console builds into console/dist.

Finish: update progress.md (tick 1.16).
```

### Prompt 1.17 — Health monitor, supervisor and Windows install

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections F01 and F18.

1. agent.runtime.supervisor: startup order from the F01 diagram, single-instance lock file,
   heartbeat every 30 s, graceful drain, wake detection (heartbeat gap) that triggers catch-up.
2. agent.runtime.health: every 5 minutes check connectors, last successful poll per source,
   stalled tasks, failing jobs, missed schedules, ICA, disk space; write connector_health;
   one DM per incident (not per check); top-of-brief summary.
3. scripts/install_task.ps1: registers a Windows Task Scheduler task "Always-On Agent" that
   runs at log-on, on workstation unlock and every 15 minutes if not running, with restart on
   failure, running uv run agentd run from the repo; and an uninstall option.
4. agentd status prints running state, uptime, last check-in, queue, health summary.

Tests: lock prevents a second instance; heartbeat gap triggers catch-up; health incident
de-duplication.

Done when tests pass and (needs you) the task is installed, survives a sleep and an unlock,
and agentd status shows it running.

Finish: update progress.md (tick 1.17).
```

### Prompt 1.18 — End-to-end run and Phase 1 exit check (needs you)

```text
Follow CLAUDE.md. Read progress.md and the whole of docs/architecture.md sections 3, 4 and 11.

1. Write tests/e2e/test_phase1.py that runs agentd with FakeWorld for a simulated day
   (FrozenClock from 08:00 to 20:00 IST): brief, triage, a draft reply approved through the
   Slack fake, a refresh failure alert, a sleep from 12:00 to 14:00 with catch-up, nightly
   digest with undo. Assert no action ran above its mode and every effect ran exactly once.
2. Walk the edge-case table in architecture section 11. For every row whose feature exists in
   Phase 1, confirm a test covers it; add missing tests. List rows not yet covered by design
   (later phases) in progress.md.
3. Write docs/runbook.md: install, sign in, switch a recipe from Shadow to Live, pause, read
   the audit log, undo, reset, what to do when doctor shows red.
4. Switch R01, R02, R04, R06 to Live; keep R03 and R05 in Shadow until the user decides.

Needs you: run the real agent for two working weeks. Each Friday, ask Claude Code to produce
a short report from the audit log and model_usage: actions by mode, approvals and edit rate,
failures, tokens by recipe.

Done when the e2e test passes, the edge-case coverage list is in progress.md, and the user
confirms the Phase 1 gate.

Finish: update progress.md (tick 1.18, set phase 2).
```

---

## Phase 2 — Doxis and Power BI recipes

### Prompt 2.1 — ServiceNow email adapter

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections A07, F08 and R08.

Build agent.adapters.servicenow_email:

1. Template-driven parsers in config/parsers/servicenow/*.yaml (sender, subject regex, body
   field regexes) for assignment, comment and state notifications. Parse INC number, short
   description, caller, priority, link, opened time (with its time zone).
2. Report parser for the hourly scheduled report: CSV (detect delimiter and encoding) and XLSX
   (copy first, then openpyxl). Columns mapped by config.
3. list_open_tickets() from the latest report; diff against the ledger producing events:
   ticket_new, state_changed, reassigned_away, closed, reopened, unknown_in_ledger.
4. Health red and one DM when a known sender's mail fails to parse; clocks marked estimated
   when the report is over 2 hours late.
5. Optional tool add_comment_by_reply (Ask) that replies to the notification email, enabled
   only when config says inbound email actions were verified (Q7).

Tests: golden fixtures for every layout; delimiter and encoding variants; reassignment and
reopen diffs; late report marks clocks estimated.

Done when tests pass.

Finish: update progress.md (tick 2.1).
```

### Prompt 2.2 — File indexer, site directory, OCR and screenshot intake

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections A08, A10, A11 and F09.

1. agent.adapters.files_index: index configured files into SearchIndex: the incident workbook
   (one document per row, all columns, synonyms per issue type from config), the rights matrix
   (one per row), specs and KB articles (one per section). Copy before reading; re-index on
   change and on request; record last indexed time.
2. Site directory: import config/directory.csv into the directory table; lookups by site code;
   stale after 90 days; entries added from screenshots marked "to review".
3. agent.adapters.ocr: Windows OCR through winrt with line boxes; Tesseract fallback;
   returns text with bounding boxes.
4. agent.adapters.doxis_screenshot: classify which Doxis page a screenshot shows; extract known
   fields (site code and name, roles, project state, closing date, TS/NP flag, KM and LBA names)
   with crop boxes; validate site codes against the directory pattern (O/0, l/1 fixes).
5. Screenshot intake from a Slack task thread file upload or the console; stored locally with
   retention.

Tests: workbook rows searchable by synonyms; OCR on the fixture screenshots extracts the
expected fields; O/0 correction; stale directory flag.

Done when tests pass.

Finish: update progress.md (tick 2.2).
```

### Prompt 2.3 — R07 Incident intake and triage

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section R07, plus the Doxis plan
details summarised there.

Create config/recipes/doxis-triage.yaml and steps:
1. Trigger: ticket_new events from A07; also on request "triage INC…".
2. Steps exactly as the R07 diagram: parse, start clock, pseudonymise, translate and flag
   non-Doxis (Haiku), on-behalf detection with confidence (Haiku; low confidence asks the
   user), classify issue type from the fixed list, precedent search (FTS candidates by code,
   Haiku ranks the top 3–5 with one reason each, names stripped), rank causes and fill the
   per-type missing-facts checklist (Haiku; Sonnet when no precedent scores well or two issues),
   draft only the questions the evidence cannot answer (Ask), L2 escalation draft for
   L2-only types (Ask, to the configured Doxis L2 group), triage card in the task thread.
3. Checklists per issue type in config/doxis/checklists.yaml.
4. Ship in shadow.

Tests: fixture tickets for access, workflow, rendition (L2), upload in French, non-Doxis,
on-behalf; expected precedents and questions; no question asks for a fact already in the email.

Done when tests pass.

Finish: update progress.md (tick 2.3).
```

### Prompt 2.4 — R08 24-hour KPI watchdog

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections R08 and F08.

Create config/recipes/kpi-watchdog.yaml and steps:
1. Every 10 minutes against the ledger and hourly against the report.
2. Thresholds 12/18/22 h from config: note in the check-in, DM alert, urgent ping. Each alert
   carries one next action computed from the task's open approvals and state (Haiku words it).
3. Brief section: tickets by age band and those that will cross 24 h before the caller's
   working day starts (caller time zone with DST).
4. Register the caller-facing forbidden-phrase filter (KPI, 24 hours, metric) for every recipe
   through the outbound checks.
5. Ship in shadow.

Tests: thresholds fire once each; estimated clocks when the report is late; brief ordering by
time left; filter blocks a draft containing "KPI".

Done when tests pass.

Finish: update progress.md (tick 2.4).
```

### Prompt 2.5 — R09 Access investigation

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section R09.

Create config/recipes/access-check.yaml and steps:
1. Triggers: R07 classifies access or visibility; on request; screenshot dropped in the thread.
2. Checklist for the affected person; facts from screenshots through A08 (Phase 2 path).
3. Decision rules in code (config/doxis/access_rules.yaml) exactly as listed in R09, each
   naming its source document; verdict card with passed, failed and unknown checks.
4. Outcomes: hand to R10; draft a by-design explanation (Ask); draft only missing questions
   (Ask); Sonnet L2 package when no rule fits (Ask).
5. Add new site to KM/LBA pairs from screenshots to the directory as "to review" (Report).
6. Ship in shadow.

Tests: each rule with a fixture; conflicting checks escalate; OCR field shown with its crop
reference.

Done when tests pass.

Finish: update progress.md (tick 2.5).
```

### Prompt 2.6 — R10 KM/LBA redirect and closure

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section R10.

Create config/recipes/km-redirect.yaml and steps:
1. Trigger: R07 or R09 decides the step belongs to the site KM/LBA; on request.
2. Identify the site; directory lookup in code (names never go to the model); missing or stale
   entries ask the user for a screenshot.
3. Sonnet drafts on tokens: why the step sits with the KM/LBA, the exact Doxis steps for the
   case (templates in config/doxis/redirect_steps.yaml), the KM/LBA names inserted by code,
   a polite request to close; short and with-steps variants; addresses both caller and
   affected person when on-behalf.
4. After an approved send: task to waiting for the caller's OK, hand over to R11; draft the
   resolution note for the user to paste (Ask).
5. Ship in shadow.

Tests: stale entry blocks the draft; on-behalf addressing; no KPI wording; both variants.

Done when tests pass.

Finish: update progress.md (tick 2.6).
```

### Prompt 2.7 — R11 On Hold chaser and confirmation detector

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section R11.

Create config/recipes/onhold-chaser.yaml and steps:
1. Triggers: replies on waiting tasks (Teams chat and mail); the daily chase window computed
   from the caller's 09:00 in their time zone.
2. Classify replies with Haiku: confirmed (explicit only), answered, new issue, out of office
   (extract return date), no reply.
3. Confirmed: draft thanks and the resolution note (Ask); closing in ServiceNow is a hand-off.
4. Answered: route back to R07 or R09 and remind the user to change the ServiceNow state.
5. Reminders on day 1, 3 and 5 (Ask), the day-5 text only if config allows close-after-no-reply.
6. Out of office pauses chasing until the return date.
7. Ship in shadow.

Tests: "thanks" alone is not confirmed; chase window across a Europe DST change; out of office
pause; reminders never go to a new recipient.

Done when tests pass.

Finish: update progress.md (tick 2.7).
```

### Prompt 2.8 — SharePoint and SQL adapters

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections A06 and A09.

1. agent.adapters.sharepoint: read list items by site and list GUID with internal column names
   from config; paging; throttling; last modified per list. Tool read_sharepoint_list (Silent).
2. agent.adapters.sql_readonly: pyodbc with a read-only login from Credential Manager; only
   whitelisted queries from config (COUNT(*) and MAX(<date column>) per named table); 30 s
   timeout; refuses to run during configured business hours unless forced by the user.
   Tool sql_count (Silent).
3. Health checks for both.

Tests: paging, renamed column error, query whitelist enforcement, business-hours refusal.

Done when tests pass and (needs you) doctor shows SharePoint read OK and, if Q10 allows, SQL OK.

Finish: update progress.md (tick 2.8).
```

### Prompt 2.9 — R12 Refresh on request

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections R12 and A05.

Create config/recipes/pbi-refresh.yaml and steps:
1. Trigger: Teams messages matching app.powerbi.com links or "refresh"; creates REF-n tasks.
2. Classify the message (Haiku); resolve the link; check last refresh, running refresh, cap,
   upstream dataflows and source freshness (SharePoint last modified or SQL MAX date).
3. Source unchanged: draft a reply with the last refresh time and an offer to refresh anyway
   (Ask). Otherwise one approval card containing the plan and the reply text to post when done.
4. After approval: dataflow first if stale, wait, then dataset; poll every 2 minutes, timeout
   60 minutes; post the approved reply; on failure tell only the user.
5. Ledger records request, approval, durations and outcome.
6. Ship in shadow.

Tests: unchanged source; running refresh; cap reached; app with several reports asks which;
failure path posts nothing to the requester.

Done when tests pass.

Finish: update progress.md (tick 2.9).
```

### Prompt 2.10 — R13 Refresh health and freshness

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section R13.

Create config/recipes/pbi-health.yaml and steps:
1. 08:45 IST daily: store the last 60 refreshes per dataset and dataflow in local history.
2. In code: 30-day median and slowest 10 %, today against median (flag over 1.5×), success
   rate, dataset refreshes that started before their upstream dataflow finished.
3. SQL counts and latest load date for configured tables; flag zero growth or a drop over 10 %.
4. Haiku words the brief section and a Monday trend summary from computed numbers only.
5. Schedule-change proposals (Ask) when ordering problems repeat 3 days in a row.
6. Do not re-alert failures already sent by R05; link them.
7. Ship in shadow.

Tests: median and flags on fixture history; ordering detection; zero-growth flag; no duplicate
alerts with R05.

Done when tests pass.

Finish: update progress.md (tick 2.10).
```

### Prompt 2.11 — R14 SharePoint history completeness

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section R14.

Create config/recipes/pbi-sharepoint.yaml and steps:
1. Trigger: a configured working day each month, then daily until complete; on request.
2. Config file config/powerbi/history_lists.yaml: list GUIDs, site and period columns, expected
   site codes per region, site-contact map.
3. In code: missing site and list pairs, duplicates, blank required columns, month-over-month
   row-count changes.
4. Haiku writes two sentences around the table (list names, site codes and counts only).
5. Reminders to site contacts (Ask); when complete, offer the R12 refresh flow (Ask).
6. Lists the user cannot read are reported as "not checked".
7. Ship in shadow.

Tests: missing and duplicate detection; complete state offers refresh; unreadable list.

Done when tests pass.

Finish: update progress.md (tick 2.11).
```

### Prompt 2.12 — R15 Enhancement request tracker

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section R15.

Create config/recipes/pbi-enhancements.yaml and steps:
1. Trigger: mail from configured stakeholders (config/powerbi/stakeholders.yaml), every
   10 minutes; brief section; Friday 16:00 IST digest.
2. At data rule "metadata": log subject, sender and date only as ENH-n and let the user
   summarise. At "text": classify (new, follow-up, question, not Power BI), extract report,
   change, deadline and urgency (Haiku), match against open items for the same report.
3. Acknowledgement draft saved in Outlook (Report); send (Ask); outside recipients make it
   Hand off.
4. Overdue flag after 2 working days without a reply; closure email draft when the user marks
   an item done.
5. Ship in shadow.

Tests: both data-rule levels; follow-up matched to an existing item; outside recipient; overdue
across a weekend.

Done when tests pass.

Finish: update progress.md (tick 2.12).
```

### Prompt 2.13 — R16 Teams and meeting issue intake

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section R16.

Create config/recipes/pbi-intake.yaml and steps:
1. Code pre-filter for Power BI links, owned report names and keywords.
2. At data rule "chat": classify flagged messages (refresh request -> R12, data looks wrong,
   bug, enhancement -> R15, access -> tell the user, question) and create ledger items (Report);
   attach last refresh facts to "data looks wrong" items.
3. Meetings: 30 minutes and 2 hours after a meeting ends, fetch the transcript if available;
   keep only segments mentioning the user, owned reports or Power BI terms; extract action items,
   decisions and deadlines (Haiku under 30 minutes, Sonnet over); list to the user with undo;
   recap to participants is Ask; delete transcript text after extraction.
4. Below "chat": log that a message matched with a link only.
5. Ship in shadow.

Tests: routing to R12 and R15; transcript unavailable; segment filter; deletion after extract.

Done when tests pass.

Finish: update progress.md (tick 2.13).
```

### Prompt 2.14 — R17 ad hoc requests and R18 recipe from request

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections R17, R18 and F12.

1. R17: messages in the main DM or a task thread start a generic agent loop with the tools the
   user allows for ad hoc work (config), inside the policy gate, scoped to the task when sent in
   a task thread. The loop plans with Haiku (Sonnet when the user asks for a long write-up),
   max 12 steps, replies in the same thread.
2. R18: when the user says to repeat something ("every Friday", "each morning"), draft a recipe
   YAML from the last successful ad hoc run (trigger, tools actually used, instructions,
   output), show it in the thread and the console editor, and save it in shadow after approval.
3. Console: the task composer from 1.16 now goes through R17.

Tests: ad hoc request cannot exceed its tool modes; scoped thread cannot touch other tasks;
recipe draft validates against the schema.

Done when tests pass.

Finish: update progress.md (tick 2.14).
```

### Prompt 2.15 — Lessons and memory growth

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section F09.

1. Track edits the user makes to drafts (diff between proposed and approved text) per recipe.
2. When the same kind of edit repeats 3 times, Haiku proposes a lesson (one sentence rule with
   scope); it appears in the console Lessons tab and the nightly digest; saved only on approval.
3. Approved lessons are loaded into the context of recipes in their scope; newest wins on
   conflict, the older one is shown for removal.
4. On task close, compress the task into a precedent record (type, symptoms, cause, resolution,
   time to close) and index it.
5. Precedent cap per issue type with summarisation of the oldest.

Tests: lesson proposal after three similar edits; scoped loading; precedent created on close.

Done when tests pass.

Finish: update progress.md (tick 2.15).
```

### Prompt 2.16 — Shadow-mode evaluation harness

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section 10.5 and the success checks
in section 8.

1. For each recipe in shadow, compare would-do records with what actually happened (the user's
   sends, closures, approvals and edits found in the audit log and mailbox).
2. Metrics: R07 top-3 precedent match and top cause match; R09 verdict match; R10 drafts
   approved without edits; R11 reminders sent versus drafted; R12 requests handled end to end;
   general draft edit distance; false alerts.
3. agentd eval --since 14d prints a table and writes docs/eval/<date>.md.
4. A recipe can go Live from the console only if its targets are met or the user overrides
   with a reason (recorded in progress.md decisions).

Tests: metrics on a synthetic audit history.

Done when tests pass.

Finish: update progress.md (tick 2.16; record each recipe's first evaluation).
```

---

## Phase 3 — Always-on host

### Prompt 3.1 — VM packaging and Postgres option (needs you)

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section 12.

1. Package agentd as a Windows service (pywin32 service wrapper or NSSM script) for a
   persistent Windows VM, with the same config and secrets model.
2. Make the database URL switchable to Postgres; port FTS5 search to Postgres full-text search
   behind the SearchIndex interface; migration script from SQLite to Postgres.
3. A network check script for the VM: ICA, Graph, Power BI, Slack, SQL sources.
4. Document the move in docs/runbook.md, including how sign-in works on the VM.

Needs you: client approval to run on the VM and the network check output.

Done when the agent runs as a service on the VM with fakes and passes the network check.

Finish: update progress.md (tick 3.1).
```

### Prompt 3.2 — Webhooks

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections P302 and A01–A03.

1. Graph change-notification subscriptions for mail and chats with validation tokens,
   clientState checks, renewal before expiry, and lifecycle notifications.
2. Keep polling as a safety net at a lower frequency; dedupe ensures no double processing.
3. Only enable when config has a reachable HTTPS endpoint; otherwise stay on polling.

Tests: validation handshake, renewal, missed-notification recovery via delta.

Done when tests pass.

Finish: update progress.md (tick 3.2).
```

### Prompt 3.3 — Teams bot with adaptive cards

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section P303 and F14.

1. A Teams bot (Bot Framework) delivering the same cards as Slack as adaptive cards, with
   Approve, Edit and Reject actions calling the approval service.
2. Notification router gains Teams bot as a primary option.

Tests: card rendering, action handling with compare-and-set.

Done when tests pass and the bot works in a test tenant or the client tenant if approved.

Finish: update progress.md (tick 3.3).
```

---

## Phase 4 — Expand

### Prompt 4.1 — Specialist lanes

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section P401.

Split recipes into a Doxis lane and a Power BI lane with their own system prompts, tool
allowlists, lessons and evaluation, under one coordinator that routes tasks by project.
Cross-lane questions go through the coordinator. No change to the policy gate.

Done when tests pass and every recipe is assigned to a lane.

Finish: update progress.md (tick 4.1).
```

### Prompt 4.2 — Citrix session adapter (needs you)

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md sections A12 and R09.

Build agent.adapters.citrix_session with Playwright (Python), read-only:
1. Persistent browser profile dedicated to the agent; open the Citrix Workspace URL from config;
   detect sign-in pages and send a hand-off card instead of typing credentials.
2. Launch the published browser and open the Doxis address.
3. Probe whether Doxis content is in the DOM or only on a canvas.
4. DOM path: selectors from config/doxis/selectors.yaml to read the user's sites and roles,
   project properties and site KM/LBA list.
5. Canvas path: positional clicks and keystrokes sent to the tab through Playwright (never the
   physical mouse), landmark templates checked before every click, screenshots of the tab only,
   local OCR through A08.
6. A deny list of button labels (Save, Create, Approve, Delete, Submit) that are never clicked.
7. R09 uses this adapter when config enables it, falling back to screenshots.

Needs you: Q8 answered, client approval in writing, the Citrix URL, and a sign-in.

Tests: the probe on recorded pages; the deny list; landmark failure stops the run.

Done when one read-only lookup works end to end on a test user, or 4.2 is marked [!].

Finish: update progress.md (tick 4.2).
```

### Prompt 4.3 — Promotion suggestions

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section P404.

When the user approves the same kind of Ask action unchanged 10 times in 14 days for the same
recipe and recipient class, propose moving it to Report. The proposal shows the evidence and
only changes policy after the user accepts in the console. Never propose for Hand off or for
anything sent outside the user's own space to new recipients.

Done when tests pass.

Finish: update progress.md (tick 4.3).
```

### Prompt 4.4 — Voice notes

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section P403.

Accept Slack voice clips in the DM and task threads; transcribe locally (Windows speech or a
local Whisper model if allowed) and treat the text as a typed message through R17. Store the
transcript, delete the audio after 7 days.

Done when tests pass.

Finish: update progress.md (tick 4.4).
```

### Prompt 4.5 — KB candidates and pattern detection

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section R19.

1. Weekly: list closed tickets worth a KB article (repeated cause, long resolution, many
   questions) and draft the article in the user's KB format (Sonnet), saved as a draft file.
2. Pattern detection: the same error or issue type from several users within 24 hours raises a
   "possible platform issue" card with the tickets listed and an L2 escalation draft (Ask).

Done when tests pass.

Finish: update progress.md (tick 4.5).
```

### Prompt 4.6 — PBIP change drafting

```text
Follow CLAUDE.md. Read progress.md and docs/architecture.md section R20.

For reports stored in Power BI Project (PBIP) format in a Git repository the user configures:
for an enhancement item, draft the change to the model (TMDL measure) or report definition on a
new branch, with a description of what changed and how to test it in Power BI Desktop. Never
publish. Opening the pull request is Ask.

Done when tests pass on a sample PBIP project.

Finish: update progress.md (tick 4.6, all phases complete).
```
