# Always-On Agent — Architecture

A personal, always-on work agent for Doxis incident support and Power BI work. It watches mail, Teams, Slack, Power BI and ServiceNow notifications, keeps one memory thread per task, drafts the next step, acts on its own for safe reversible work, and asks before anything other people will see.

This document is the source of truth for the build. `prompts.md` builds it, `progress.md` tracks it, `CLAUDE.md` holds the rules every coding session follows.

---

## Contents

1. [Constraints and assumptions](#1-constraints-and-assumptions)
2. [Feature catalogue](#2-feature-catalogue)
3. [Common architecture](#3-common-architecture)
4. [End-to-end flows](#4-end-to-end-flows)
5. [Data model](#5-data-model)
6. [Core platform features](#6-core-platform-features)
7. [Adapters](#7-adapters)
8. [Recipes](#8-recipes)
9. [Console UI and API](#9-console-ui-and-api)
10. [Cross-cutting concerns](#10-cross-cutting-concerns)
11. [Edge cases and failure modes](#11-edge-cases-and-failure-modes)
12. [Phase-by-phase deployment](#12-phase-by-phase-deployment)
13. [Repository layout](#13-repository-layout)
14. [Configuration reference](#14-configuration-reference)
15. [Testing strategy](#15-testing-strategy)
16. [Open questions to settle in Phase 0](#16-open-questions-to-settle-in-phase-0)

---

## 1. Constraints and assumptions

| Area | Constraint | Design consequence |
| --- | --- | --- |
| Host | Phase 1–2 run on a Windows laptop. No public URL. Laptop sleeps and locks. | Polling instead of webhooks. Slack Socket Mode. Catch-up on wake. Task Scheduler starts the agent at login. |
| Models | Claude through the ICA gateway. Haiku is free and the default; Sonnet and Opus are available. | Haiku for every run; Sonnet only for named cases under a daily budget. All model calls go through one client. |
| Client data | Mail, Teams and Power BI sit in the client's Microsoft 365 tenant. What may be sent to ICA is not yet confirmed. | A data-rule filter on every model call. Default level: metadata only. Names are always pseudonymised. |
| ServiceNow | No API access. | Read through notification emails and an hourly scheduled report. Write only through the user (paste) or an email reply, if enabled. |
| Doxis | No API. Reachable only through Citrix, which opens in the laptop's browser. | Phase 2: user screenshots + local OCR. Phase 4: browser automation of the Citrix web session, read-only. |
| Power BI | REST API covers refresh and monitoring, not report or model editing. Admin APIs return 401. Capacity likely Pro. | Refresh cap awareness. No reliance on admin or XMLA endpoints. |
| Autonomy | The user wants some tasks fully automatic with a report later, others asked first. | Four action modes enforced by a policy gate: Silent, Report, Ask, Hand off. |
| Concurrency | Many incidents at once. | One coordinator, a pool of short-lived task workers, one worker per task at a time. |

---

## 2. Feature catalogue

Every feature has an ID used in diagrams, `prompts.md` and `progress.md`.

### 2.1 Core platform

| ID | Feature | One line | Phase |
| --- | --- | --- | --- |
| F01 | Runtime and supervisor | One Python process that starts at login, restarts on crash and reports health | 1 |
| F02 | Scheduler and triggers | Cron schedules, pollers, catch-up after sleep, quiet hours | 1 |
| F03 | Event intake and coordinator | Normalises every incoming item into an event, dedupes it and routes it to a task | 1 |
| F04 | Task workers and job queue | Priority queue, worker pool, per-task lock, retries, poison handling | 1 |
| F05 | Model gateway | ICA client, model routing, budgets, structured output, usage logging | 1 |
| F06 | Policy gate | Decides Silent, Report, Ask or Hand off for every tool call | 1 |
| F07 | Approvals | Approval cards, content hashing, edit, expiry, invalidation, exactly-once execution | 1 |
| F08 | Task ledger | Tasks, states, 24-hour clocks, heartbeats, task links | 1 |
| F09 | Memory | Personal files, task memory, knowledge search, lessons | 1–2 |
| F10 | Context builder | Builds a small, budgeted context packet for each run | 1 |
| F11 | Privacy layer | Pseudonymiser, data-rule filter, outbound content checks | 1 |
| F12 | Recipe engine | YAML recipes, validation, shadow mode, mode-escalation guard | 1 |
| F13 | Audit log and undo | Append-only record of every tool call, inverse operations for Report actions | 1 |
| F14 | Channels | Slack DM and threads, Teams notifications, approval buttons, commands | 1 |
| F15 | Console API and UI | FastAPI on localhost, React console with the eight pages | 1 |
| F16 | Controls | Pause, resume, check-in now, reset, status | 1 |
| F17 | Identity and secrets | MSAL sign-in, token cache, Windows Credential Manager | 1 |
| F18 | Health monitor | Connector health, stalled tasks, missed schedules, self-alerts | 1 |

### 2.2 Adapters

| ID | Adapter | Reads | Writes | Phase |
| --- | --- | --- | --- | --- |
| A01 | Outlook mail (Graph, COM fallback) | Messages, threads, attachments metadata | Categorise, move, flag, create draft, send (Ask) | 1 |
| A02 | Outlook calendar | Today's events, online meeting links | Tentative holds on own calendar (Report) | 1 |
| A03 | Teams (Graph) | Chats, mentions, meeting transcripts | Post in a chat (Ask) | 1 |
| A04 | Slack (Socket Mode) | DMs, mentions, button clicks | DM, threads, approval cards | 1 |
| A05 | Power BI REST | Refresh history, schedules, dataflows, reports, apps | Trigger refresh (Ask), retry own refresh (Report), update schedule (Ask) | 1 |
| A06 | SharePoint lists (Graph) | List items, last modified | — | 2 |
| A07 | ServiceNow email adapter | Assignment emails, hourly report attachment | Reply to notification email = comment (Ask, if enabled) | 2 |
| A08 | Doxis screenshot adapter | Screenshots shared by the user, read with local OCR | — | 2 |
| A09 | SQL read-only | Row counts, latest load date | — | 2 |
| A10 | Local file indexer | Incident workbook, rights matrix, specs, KB articles | — | 2 |
| A11 | OCR service | Text and fields from images, on the laptop | — | 2 |
| A12 | Citrix web session (Playwright) | Doxis pages through the Citrix browser tab | — (read-only) | 4 |

### 2.3 Recipes

| ID | Recipe | Project | Phase |
| --- | --- | --- | --- |
| R01 | Morning brief | Core | 1 |
| R02 | Inbox triage | Core | 1 |
| R03 | Draft replies | Core | 1 |
| R04 | Mention digest | Core | 1 |
| R05 | Refresh-failure watch | Power BI | 1 |
| R06 | Nightly digest | Core | 1 |
| R07 | Incident intake and triage | Doxis | 2 |
| R08 | 24-hour KPI watchdog | Doxis | 2 |
| R09 | Access investigation | Doxis | 2 |
| R10 | KM/LBA redirect and closure | Doxis | 2 |
| R11 | On Hold chaser and confirmation detector | Doxis | 2 |
| R12 | Refresh on request | Power BI | 2 |
| R13 | Refresh health and freshness | Power BI | 2 |
| R14 | SharePoint history completeness | Power BI | 2 |
| R15 | Enhancement request tracker | Power BI | 2 |
| R16 | Teams and meeting issue intake | Power BI | 2 |
| R17 | Ad hoc request from DM | Core | 2 |
| R18 | Recipe from a repeated request | Core | 2 |
| R19 | KB candidates and pattern detection | Doxis | 4 |
| R20 | PBIP change drafting for enhancements | Power BI | 4 |

### 2.4 Later-phase platform features

| ID | Feature | Phase |
| --- | --- | --- |
| P301 | Move to persistent VM, Postgres option | 3 |
| P302 | Webhooks (Graph change notifications) | 3 |
| P303 | Teams bot with adaptive cards | 3 |
| P401 | Specialist lanes (Doxis agent, Power BI agent) | 4 |
| P402 | Citrix browser automation for Doxis (A12) | 4 |
| P403 | Voice notes | 4 |
| P404 | Promotion suggestions (Ask → Report after repeated unchanged approvals) | 4 |

---

## 3. Common architecture

### 3.1 System context

```mermaid
flowchart LR
  user["Udit"]
  subgraph laptop["Windows laptop"]
    agent["Always-On Agent"]
    console["Console in browser, localhost"]
    files["Local files: workbook, rights matrix, screenshots"]
    citrix["Citrix tab in the browser"]
  end
  subgraph m365["Client Microsoft 365 tenant"]
    mail["Outlook mail and calendar"]
    teams["Teams chats and transcripts"]
    sp["SharePoint lists"]
    pbi["Power BI service"]
  end
  slack["Slack workspace"]
  ica["ICA gateway: Claude Haiku, Sonnet, Opus"]
  snow["ServiceNow"]
  doxis["Doxis webCube"]
  sql["SQL sources: Pipeline Tracker, Forecast Hub"]

  user <--> console
  user <--> slack
  agent <--> console
  agent <-->|Graph API| mail
  agent <-->|Graph API| teams
  agent -->|Graph API| sp
  agent <-->|REST API| pbi
  agent <-->|Socket Mode| slack
  agent -->|Messages API| ica
  snow -->|notification emails, hourly report| mail
  agent -->|read-only SQL| sql
  agent --> files
  agent -.->|Phase 4, read-only| citrix
  citrix -.-> doxis
```

### 3.2 Containers inside the agent

One process, `agentd`, holds every core component. It runs on asyncio. The console is a static React build served by the same process.

```mermaid
flowchart TB
  subgraph agentd["agentd process"]
    direction TB
    subgraph intake["Intake"]
      sched["F02 Scheduler"]
      pollers["Pollers: mail, Teams, Power BI, report"]
      slackin["F14 Slack listener"]
      apiin["F15 API: user actions"]
    end
    coord["F03 Coordinator: normalise, dedupe, route"]
    queue["F04 Job queue: priority, per-task lock"]
    subgraph pool["F04 Worker pool"]
      w1["Worker"]
      w2["Worker"]
      w3["Worker"]
      w4["Worker"]
    end
    subgraph brain["Task graph, LangGraph"]
      ctx["F10 Context builder"]
      recipe["F12 Recipe runner"]
      llm["F05 Model gateway"]
      priv["F11 Privacy layer"]
    end
    gate["F06 Policy gate"]
    appr["F07 Approvals"]
    outbox["Outbox: exactly-once execution"]
    adapters["A01–A12 Adapters"]
    audit["F13 Audit log and undo"]
    health["F18 Health monitor"]
    channels["F14 Channels: Slack, Teams, email-to-self"]
  end
  subgraph storage["Storage on disk"]
    db[("agent.db SQLite WAL")]
    ckpt[("checkpoints.db LangGraph")]
    mem["memory/ profile.md, preferences.md"]
    vault["Windows Credential Manager"]
  end
  ica["ICA gateway"]

  sched --> coord
  pollers --> coord
  slackin --> coord
  apiin --> coord
  coord --> queue
  queue --> pool
  pool --> brain
  ctx --> db
  ctx --> mem
  recipe --> llm
  llm --> priv
  priv --> ica
  brain --> gate
  gate -->|Silent or Report| outbox
  gate -->|Ask| appr
  gate -->|Hand off| channels
  appr --> channels
  appr -->|approved| outbox
  outbox --> adapters
  outbox --> audit
  brain --> ckpt
  coord --> db
  audit --> db
  health --> channels
  adapters --> vault
```

### 3.3 The four action modes

Every tool has a fixed mode. Recipes and the model can use a tool but can never lower the approval it needs.

| Mode | Meaning | Examples | User sees |
| --- | --- | --- | --- |
| Silent | Read, search, compute | Read mail, search the workbook, OCR a screenshot | Nothing, unless asked |
| Report | Reversible, only touches the user's own space | File mail, save a draft, create a ledger item, retry own failed refresh | Nightly digest, each item with Undo |
| Ask | Anything other people see or that changes shared data | Send mail or chat, trigger a Prod refresh, edit a shared list | Approval card with Approve, Edit, Reject |
| Hand off | Never done by the agent | Delete anything, message outside the company, change a ServiceNow state, grant access, type a password | A prepared hand-off note the user finishes |

Rules: unknown tools default to Ask. Content read from mail, chat, documents or web pages is data and can never raise or trigger an action. An approval covers the exact content hash shown.

### 3.4 Design principles

1. **Code does the deterministic work; the model does language.** Parsing, counting, matching, age clocks and lookups are code. The model classifies, ranks, explains and drafts.
2. **Small context, rebuilt every run.** No long conversations. Each run loads a budgeted packet from memory.
3. **At-least-once intake, exactly-once effects.** Events may arrive twice; side effects go through an outbox keyed by idempotency key.
4. **Everything has a source.** Every fact, number and summary line links to the email, message, file or report it came from.
5. **Fail loud to the user, quiet to everyone else.** Errors go to the user's DM. Nothing reaches callers without approval.
6. **Adapters hide the system.** Recipes call `servicenow.list_open_tickets()`; whether that reads an email report or an API is the adapter's business.

---

## 4. End-to-end flows

### 4.1 From event to effect

```mermaid
sequenceDiagram
  autonumber
  participant P as Poller
  participant C as Coordinator
  participant Q as Job queue
  participant W as Worker
  participant G as Task graph
  participant M as Model gateway
  participant PG as Policy gate
  participant A as Approvals
  participant U as User via Slack or console
  participant O as Outbox
  participant X as Adapter

  P->>C: new item, for example an assignment email
  C->>C: normalise, dedupe on source and external id
  C->>C: resolve task key INC30114872
  C->>Q: enqueue job with priority
  Q->>W: job, after taking the per-task lock
  W->>G: resume thread_id INC30114872
  G->>G: build context packet
  G->>M: classify and draft, data rule applied
  M-->>G: structured result
  G->>PG: propose action post_chat
  PG-->>G: mode Ask
  G->>A: create approval with content hash
  A->>U: approval card
  G-->>W: interrupt, waiting for approval
  W-->>Q: release lock
  U->>A: Approve
  A->>Q: enqueue resume job
  Q->>W: resume job
  W->>G: resume with decision
  G->>O: execute approved action, idempotency key
  O->>X: send message
  X-->>O: sent, external message id
  O->>O: record result, audit row
  G->>G: update task card, summary, events
```

### 4.2 Task lifecycle

```mermaid
stateDiagram-v2
  [*] --> new
  new --> in_progress: first worker run
  in_progress --> needs_you: approval or decision pending
  needs_you --> in_progress: approved, rejected or answered
  in_progress --> waiting: waiting on caller, L2 or stakeholder
  waiting --> in_progress: reply or report change arrives
  waiting --> needs_you: chase due or deadline near
  in_progress --> done: user marks done
  waiting --> done: user marks done
  needs_you --> done: user marks done
  done --> in_progress: ticket reopened
  in_progress --> failed: job failed twice
  failed --> needs_you: surfaced to user
  scheduled --> in_progress: schedule fires
```

Only the user can move a task to `done`. The agent may suggest it.

### 4.3 Approval lifecycle

```mermaid
stateDiagram-v2
  [*] --> proposed
  proposed --> approved: user approves exact hash
  proposed --> edited: user edits text
  edited --> approved: user approves edited hash
  proposed --> rejected: user rejects
  proposed --> invalidated: new event on the task changes context
  proposed --> expired: 24 hours pass
  approved --> executing: outbox claims it
  executing --> done: adapter confirms
  executing --> failed: adapter error
  failed --> proposed: retry needs a fresh approval
  invalidated --> [*]
  expired --> [*]
  rejected --> [*]
  done --> [*]
```

---

## 5. Data model

SQLite in WAL mode for Phase 1–2, written through SQLAlchemy Core so Phase 3 can move to Postgres. LangGraph checkpoints live in a separate `checkpoints.db`.

```mermaid
erDiagram
  TASKS ||--o{ TASK_EVENTS : has
  TASKS ||--o{ TASK_FACTS : has
  TASKS ||--o{ TASK_LINKS : has
  TASKS ||--|| TASK_SUMMARIES : has
  TASKS ||--o{ ACTION_REQUESTS : has
  TASKS ||--o{ JOBS : has
  TASKS ||--o{ TOKEN_MAP : has
  EVENTS }o--o| TASKS : routed_to
  ACTION_REQUESTS ||--o{ AUDIT_LOG : produces
  AUDIT_LOG ||--o| UNDO_OPS : may_have
  DOCUMENTS ||--|| DOCUMENTS_FTS : indexed_by

  TASKS {
    text id PK
    text project
    text kind
    text title
    text state
    text priority
    text caller_token
    text affected_token
    text site
    datetime opened_at
    datetime clock_started_at
    bool clock_paused
    text next_action
    text recipe
    datetime last_heartbeat_at
    datetime created_at
    datetime updated_at
  }
  EVENTS {
    int id PK
    text source
    text kind
    text external_id
    text task_id FK
    datetime received_at
    text payload_json
    text status
  }
  TASK_EVENTS {
    int id PK
    text task_id FK
    datetime at
    text actor
    text channel
    text text_pseudonymised
    text source_ref
    datetime raw_expires_at
  }
  TASK_SUMMARIES {
    text task_id PK
    int version
    text text
    datetime updated_at
  }
  TASK_FACTS {
    text task_id FK
    text key
    text status
    text value
    text source_ref
    datetime updated_at
  }
  TASK_LINKS {
    text task_id FK
    text kind
    text external_id
  }
  ACTION_REQUESTS {
    text id PK
    text task_id FK
    text tool
    text args_json
    text mode
    text status
    text content_hash
    text idempotency_key
    datetime expires_at
    datetime decided_at
    text result_json
  }
  AUDIT_LOG {
    int id PK
    datetime at
    text task_id
    text recipe
    text tool
    text mode
    text args_digest
    text result
    text actor
  }
  UNDO_OPS {
    int id PK
    int audit_id FK
    text inverse_tool
    text inverse_args_json
    text status
  }
  JOBS {
    int id PK
    text task_id FK
    text kind
    int priority
    text status
    int attempts
    datetime available_at
    text locked_by
    text last_error
  }
  TOKEN_MAP {
    text task_id FK
    text token
    text kind
    blob real_value_encrypted
  }
  DOCUMENTS {
    text id PK
    text source
    text title
    text body
    text meta_json
  }
  DOCUMENTS_FTS {
    text title
    text body
  }
```

Other tables: `recipes_state`, `lessons`, `directory`, `model_usage`, `connector_health`, `kv` (delta tokens and cursors), `schedule_runs`.

`task_links.kind` is one of `mail_conversation`, `teams_chat`, `slack_thread`, `servicenow`, `powerbi_dataset`. It lets the coordinator map any later message back to its task.

---

## 6. Core platform features

Each section: what it does, how it works, the diagram, its interface, and the edge cases it must handle. Section 11 collects all edge cases in one table.

### F01 — Runtime and supervisor

One Python 3.12 process started by Windows Task Scheduler at log-on and on unlock, with "restart on failure". The supervisor owns startup order, graceful shutdown and a heartbeat file.

```mermaid
flowchart TD
  start["Task Scheduler: at log-on, on unlock, every 15 min if not running"] --> lock{"Single-instance lock held?"}
  lock -->|yes| exit["Exit quietly"]
  lock -->|no| boot["Load config, open DB, run migrations"]
  boot --> secrets["Load secrets from Credential Manager"]
  secrets --> conn["Check connectors, record health"]
  conn --> catch["Catch-up: run missed schedules"]
  catch --> run["Start scheduler, pollers, Slack listener, API, worker pool"]
  run --> beat["Write heartbeat every 30 s"]
  run --> sig{"Shutdown signal or sleep?"}
  sig -->|yes| drain["Stop intake, let running jobs reach a checkpoint, flush DB"]
  drain --> stop["Exit"]
```

Interface: `agentd run`, `agentd status`, `agentd doctor` (checks every connector and prints fixes).

Edge cases: two instances after a fast unlock (file lock); sleep mid-job (LangGraph checkpoint makes resume safe); Windows update reboot; DB locked by antivirus scan (retry with backoff); corporate proxy needs a CA bundle (`REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`).

### F02 — Scheduler and triggers

APScheduler (AsyncIOScheduler) with jobs persisted in the database. Three trigger types: **schedule** (cron in the user's time zone), **poll** (interval per source) and **event** (coordinator-matched). All times are stored in UTC and computed with `zoneinfo`.

```mermaid
flowchart LR
  cfg["config.yaml and recipe triggers"] --> reg["Register jobs"]
  reg --> cron["Cron jobs: brief 09:00 IST, check-ins, nightly 19:00"]
  reg --> poll["Pollers: mail 5 min, Teams 5 min, Power BI 10 min, report hourly"]
  cron --> gate1{"Paused, quiet hours, or outside work days?"}
  poll --> gate1
  gate1 -->|paused| skip["Skip and record"]
  gate1 -->|quiet hours| defer["Defer non-urgent to next window"]
  gate1 -->|ok| fire["Emit trigger event to coordinator"]
  wake["Wake or start after a gap"] --> missed["Find runs missed since last heartbeat"]
  missed --> coalesce["Coalesce: run each missed job once"]
  coalesce --> fire
```

Rules: a missed morning brief runs on first wake after 09:00 on a work day, once. Missed check-ins coalesce into one. Pollers resume from stored delta tokens.

Edge cases: DST in the caller's zone (Europe switches; IST does not), so the CET chase window moves between 12:30 and 13:30 IST; laptop asleep through the whole day (brief runs next morning only, with "yesterday" noted); clock drift after hibernation; public holidays (config list).

### F03 — Event intake and coordinator

Every input becomes an `Event`. The coordinator dedupes, resolves which task it belongs to, creates tasks when needed, and enqueues a job.

```mermaid
flowchart TD
  in["Raw item from poller, Slack, API or schedule"] --> norm["Normalise to Event: source, kind, external_id, received_at, payload"]
  norm --> dedupe{"source and external_id already seen?"}
  dedupe -->|yes| drop["Drop, count duplicate"]
  dedupe -->|no| save["Store event"]
  save --> resolve["Resolve task key"]
  resolve --> r1{"INC number, ENH id or REF id in subject or body?"}
  r1 -->|yes| t1["Task by id"]
  r1 -->|no| r2{"Known conversation, chat or thread id in task_links?"}
  r2 -->|yes| t2["Linked task"]
  r2 -->|no| r3{"Matches a recipe's create rule?"}
  r3 -->|yes| t3["Create task, add links"]
  r3 -->|no| t4["No task: digest-only event"]
  t1 --> prio["Compute priority"]
  t2 --> prio
  t3 --> prio
  t4 --> digest["Queue for digests"]
  prio --> enqueue["Enqueue job"]
```

Priority: urgent clock (≥ 22 h) 100, approval resume 90, caller reply 80, new ticket 70, Power BI request 60, scheduled recipe 40, digest work 10.

Edge cases: one email mentions two INC numbers (create a job for each, link both); "RE: RE: INC…" chains; a forwarded ServiceNow email; a ticket reassigned away from the user (close the task as `waiting`, note it, stop chasing); a reopened ticket (move `done` back to `in_progress`); Teams message edited or deleted after processing (store edit as a new event; never act on deleted text).

### F04 — Task workers and job queue

The queue is a `jobs` table. Workers are asyncio tasks in a pool (default 4). A worker claims the highest-priority available job whose task has no running job, runs the task graph until it finishes or interrupts, then releases.

```mermaid
flowchart TD
  claim["Claim job: highest priority, task not locked, available_at reached"] --> lockt["Lock task: UPDATE ... WHERE locked_by IS NULL"]
  lockt --> run["Run task graph with thread_id = task id"]
  run --> res{"Result"}
  res -->|finished| ok["Mark job done, update heartbeat"]
  res -->|interrupt for approval| park["Mark job parked; resume job created on decision"]
  res -->|error| retry{"attempts < 2?"}
  retry -->|yes| back["Back off 2 then 10 minutes, release lock"]
  retry -->|no| poison["Mark failed, task to needs_you, DM the error"]
  ok --> release["Release task lock"]
  park --> release
  back --> release
  poison --> release
  timeout["Job running over 5 minutes"] --> cancel["Cancel at next checkpoint, treat as error"]
```

Edge cases: two events for the same task in one second (second job waits for the lock); a stuck job holding a lock after a crash (locks expire after 10 minutes, checked against heartbeat); starvation of low-priority digests during a busy morning (age boosts priority by 1 per minute); runaway loop (max 12 graph steps per run).

### F05 — Model gateway

One client for every model call. It points the Anthropic SDK at the ICA gateway (`base_url` and key from Credential Manager). Responsibilities: model routing, budgets, structured output with schema validation, retries, prompt caching for stable system prompts, usage logging.

```mermaid
flowchart LR
  call["Model request: purpose, data classes, schema, preferred model"] --> filt["F11 data-rule filter and pseudonymiser"]
  filt --> route{"Routing"}
  route -->|default| haiku["Haiku"]
  route -->|caller-facing draft, long transcript, weekly report| budget{"Sonnet budget left today?"}
  budget -->|yes| sonnet["Sonnet"]
  budget -->|no| haiku
  haiku --> send["Call ICA with retries on 429 and 5xx"]
  sonnet --> send
  send --> parse["Validate against schema"]
  parse -->|invalid| fix["One repair retry with the error"]
  fix --> parse
  parse -->|valid| out["Return result"]
  parse -->|still invalid| err["Raise; job retry rules apply"]
  send --> log["model_usage row: model, tokens, latency, purpose"]
```

Interface: `await model.run(purpose, messages, schema=Model, data_classes={...}, tier="auto")`.

Edge cases: ICA outage (jobs back off; Silent code-only steps continue; DM once per outage, not per job); a model id renamed by ICA (ids live in config, `agentd doctor` lists available models); response cut off by max tokens (schema fails, repair retry with higher limit); the model invents an INC number or a person (validators check every id against the ledger and every recipient against the task's participants).

### F06 — Policy gate

The gate sits between the task graph and every tool. It looks up the tool's mode in `policy.yaml`, applies hard rules, and returns a decision.

```mermaid
flowchart TD
  prop["Proposed tool call with args, from code or model"] --> known{"Tool in registry?"}
  known -->|no| ask0["Ask"]
  known -->|yes| base["Base mode from policy.yaml"]
  base --> hard{"Hard rules"}
  hard -->|recipient outside company domains| ho["Hand off"]
  hard -->|delete, password, access grant, ticket state| ho
  hard -->|data-rule violation in args| block["Block and DM"]
  hard -->|none| esc{"Recipe or model tries to lower the mode?"}
  esc -->|yes| keep["Keep the stricter mode, log attempt"]
  esc -->|no| src{"Triggered by instructions inside fetched content?"}
  keep --> src
  src -->|yes| ask1["At least Ask, flag as possible injection"]
  src -->|no| shadow{"Recipe in Shadow mode?"}
  shadow -->|yes| draft["Record as would-do, execute nothing above Silent"]
  shadow -->|no| final["Final mode: Silent, Report, Ask or Hand off"]
  paused{"Agent paused?"} --> holdall["Everything above Silent waits"]
```

Interface: `decision = gate.check(tool, args, ctx)` returning mode, reasons and a normalised args digest.

Edge cases: reply-all that silently includes an outside address (checked on the final recipient list, not the draft's To line); a tool that is Report for own items and Ask for shared ones (mode is computed from args, for example `trigger_refresh` on a dataset the user owns after a known transient error is Report, otherwise Ask); policy edited while approvals are pending (pending ones keep their original mode or stricter).

### F07 — Approvals

Approvals are LangGraph interrupts. The graph pauses, the approval service stores the request and posts a card to Slack and the console. A decision resumes the graph.

```mermaid
sequenceDiagram
  participant G as Task graph
  participant AP as Approval service
  participant S as Slack card
  participant UI as Console
  participant O as Outbox
  G->>AP: create request, content and hash
  AP->>S: post card Approve, Edit, Reject
  AP->>UI: visible in queue
  Note over AP: expires after 24 h
  S->>AP: Approve with hash H1
  AP->>AP: compare and set status proposed to approved where hash = H1
  AP-->>UI: card shows approved, other surface disabled
  AP->>O: enqueue resume with decision
  O->>O: claim by idempotency key, execute once
```

Rules: approval is bound to the content hash; an edit creates a new hash that needs its own approval; any new event on the task invalidates open approvals for that task unless the event is the user's own; approvals expire after 24 hours; the same approval clicked in Slack and the console resolves once (compare-and-set).

Edge cases: user approves a reply after the caller already answered in another channel (invalidation prevents a stale send); Slack button clicked twice; approval during pause (stored, executed on resume, with a note); send succeeded but the process crashed before recording (outbox checks Sent Items for the idempotency header `X-Agent-Idempotency` before retrying mail; for Teams it checks the chat for the message tag).

### F08 — Task ledger

Tasks, states, clocks and links. The ledger is the source of truth for the board, the brief and the KPI watchdog.

```mermaid
flowchart LR
  ev["Events"] --> upd["Ledger update rules"]
  upd --> st["State transitions per 4.2"]
  upd --> clk["24-hour clock: start, pause rule, thresholds 12, 18, 22 h"]
  upd --> hb["Heartbeat: last time a worker touched the task"]
  upd --> lnk["Links: conversation, chat, thread, dataset"]
  hb --> stale{"No update for 24 h?"}
  stale -->|yes| brief["Listed in the brief as stalled"]
  clk --> watch["R08 KPI watchdog"]
```

Clock rules are configuration: `clock_start: opened | assigned | email_received`, `pause_on_hold: true | false`. Defaults follow the Doxis plan's assumptions until the team lead confirms.

Edge cases: report says Opened 13:02 CET but the email arrived 18:00 IST (store both, compute from the configured start); ticket closed by someone else (report shows Closed; task moves to `done` with a note, which is the one case the agent may close a task, because the source system says so).

### F09 — Memory

Three kinds, all on the laptop.

```mermaid
flowchart TB
  subgraph personal["Personal memory, always loaded"]
    prof["profile.md"]
    pref["preferences.md"]
    wstyle["Writing style samples, pseudonymised"]
  end
  subgraph taskmem["Task memory, one per task"]
    card["Task card: structured fields and checks"]
    summ["Running summary, fixed length"]
    log["Event log, append-only, source links"]
    att["Attachments: OCR text, drafts, files"]
  end
  subgraph know["Knowledge, shared across tasks"]
    dir["Site directory: KM and LBA per site"]
    docs["Indexed documents, SQLite FTS5"]
    prec["Precedents from closed tasks"]
    less["Lessons: rules the user approved"]
  end
  closed["Task closed"] --> compress["Compress into outcome record"] --> prec
  edits["User edits drafts the same way 3 times"] --> propose["Propose a lesson"] --> approve{"User approves?"}
  approve -->|yes| less
  approve -->|no| discard["Discard"]
  retention["Nightly retention job"] --> purge["Delete raw text after 14 days, screenshots after 7"]
```

Rules: memory stores pseudonymised text; real names live only in the encrypted `token_map`; every fact has a `source_ref`; lessons are only saved after approval; the user can view, edit and delete everything in the console.

Edge cases: summary drifts from facts (after each rewrite, a code check compares the summary's ids, dates and states with the card and asks for a rewrite on mismatch); directory entry older than 90 days (flagged stale; R10 asks for a fresh screenshot); conflicting lessons (newest wins, older one shown for removal); memory growth (precedents capped per type, oldest summarised).

### F10 — Context builder

Code, not the model, decides what each run sees. Sections have token budgets; the model can ask for more through a `search_memory` tool.

```mermaid
flowchart LR
  job["Job: task id, recipe, trigger"] --> pick["Recipe declares context needs"]
  pick --> s1["Personal: about 300 tokens"]
  pick --> s2["Task card: about 400"]
  pick --> s3["Running summary: about 400"]
  pick --> s4["Last 5 events: about 1,200"]
  pick --> s5["Top precedents: about 800"]
  pick --> s6["Directory or rules hits: about 300"]
  s1 --> pack["Assemble packet, trim each section to its budget"]
  s2 --> pack
  s3 --> pack
  s4 --> pack
  s5 --> pack
  s6 --> pack
  pack --> pseudo["Pseudonymise and apply data rule"]
  pseudo --> model["Model call"]
  model -->|needs more| tool["search_memory tool: one event, one document, more precedents"]
  tool --> pseudo
```

Edge cases: a task with 200 events (only summary and last 5 load; older events reachable by search); a German or French message (translated text stored next to the original; the packet uses the translation).

### F11 — Privacy layer

Three parts: pseudonymiser, data-rule filter and outbound checks.

```mermaid
flowchart TD
  text["Text going to the model"] --> find["Find names and identifiers"]
  find --> src1["Known people: ledger participants, directory, mail headers"]
  find --> src2["Patterns: emails, phone numbers, user ids"]
  find --> src3["Signature blocks and greetings"]
  src1 --> repl["Replace with stable tokens per task: USER_1, KM_1"]
  src2 --> repl
  src3 --> repl
  repl --> check["Leak check: no known real name remains"]
  check -->|leak| stop["Block call, DM, log"]
  check -->|clean| level{"Data classes allowed by data rule?"}
  level -->|no| stop2["Send metadata only or skip the step"]
  level -->|yes| send["Send to model"]
  back["Model draft with tokens"] --> restore["Replace tokens with real names locally"]
  restore --> out["Outbound checks: KPI wording, outside recipients, attachments"]
```

Data-rule levels in `config.yaml`: `metadata` (ids, names of reports, times, counts), `text` (pseudonymised ticket and mail text), `chat` (pseudonymised chat and transcripts), `images` (screenshots to the model). The level is set by the user only after written approval.

Edge cases: names with accents or two spellings (Tomás, Tomas); first names that are also words; a name inside a screenshot (OCR text goes through the same pseudonymiser); a token appearing in a draft that the model invented (restore fails, draft blocked).

### F12 — Recipe engine

A recipe is a YAML file plus an optional Python step module. YAML says when it runs, which tools it may use, which model and what it outputs. The step module holds deterministic logic.

```yaml
name: pbi-refresh
project: powerbi
status: shadow            # live | shadow | off
trigger:
  poll: teams_chats
  every: 5m
  match: "app\\.powerbi\\.com|\\brefresh\\b"
creates_task: { id_prefix: REF }
model: haiku
data_classes: [metadata, chat]
tools: [read_chat, get_refresh_history, trigger_refresh, post_chat]
steps: recipes.pbi_refresh:steps
context: [personal, card, summary, last_events]
instructions: >
  If the message asks for a refresh, find the dataset from the link,
  check history, source freshness and the daily cap, and ask before refreshing.
output: approval_card
```

```mermaid
flowchart LR
  yaml["recipes/*.yaml"] --> load["Loader"]
  load --> val{"Schema valid, tools exist, steps importable?"}
  val -->|no| bad["Recipe off, error shown in console"]
  val -->|yes| reg["Register triggers with F02 and F03"]
  reg --> runr["Runner inside task graph"]
  runr --> steps["Python steps: code first"]
  steps --> llmstep["Model steps where language is needed"]
  llmstep --> toolcall["Tool calls through F06 gate"]
  status{"Status"} -->|shadow| shadowlog["Would-do log and drafts only"]
  status -->|live| toolcall
```

Edge cases: hot reload while a job runs (running jobs keep their loaded version); a recipe lists a Hand off tool (load fails); two recipes match the same event (both run unless one declares `exclusive_with`).

### F13 — Audit log and undo

Every tool call writes one audit row, including blocked and shadow ones. Report actions register an inverse.

| Report action | Inverse |
| --- | --- |
| move_mail | move back to the original folder id |
| categorise or flag | restore previous categories and flag |
| save_draft | delete the draft (allowed: it is the agent's own draft) |
| create ledger item | archive the item |
| add_hold | delete the hold event the agent created |
| retry own refresh | none; shown as not undoable |

Edge cases: undo after the user changed the item by hand (inverse checks current state first and refuses with an explanation); undo of an already-undone action (idempotent).

### F14 — Channels

Slack is the primary channel in Phase 1 (Socket Mode needs no public URL). Teams is used to read and to post in incident chats; personal notifications in Teams depend on the Phase 0 check of posting to your own chat. Email-to-self is the last fallback.

```mermaid
flowchart LR
  note["Notification: brief, alert, approval, hand-off"] --> cap{"Daily push cap or quiet hours?"}
  cap -->|over cap or quiet| batch["Batch into next check-in"]
  cap -->|ok| route{"Primary channel healthy?"}
  route -->|Slack| slackdm["Slack DM or task thread"]
  route -->|Slack down| teamsself["Teams self chat, if verified"]
  teamsself -->|unavailable| mailself["Email to self with console link"]
  slackdm --> btn["Buttons: Approve, Edit, Reject, Open"]
  btn --> api["Interaction handler to F07"]
  cmd["Commands: /agent status, pause, resume, brief, tasks, undo"] --> ctl["F16 controls"]
```

Each task gets one Slack thread titled `INC30114872 – short description`; replies in that thread are routed to that task (F03 via `task_links`).

### F15 — Console API and UI

See section 9.

### F16 — Controls

Pause stops schedules, pollers and outbox execution above Silent; intake continues so nothing is lost. Resume runs a catch-up. Reset deletes task memory, lessons and schedules after an in-console confirmation; the audit log is kept.

### F17 — Identity and secrets

```mermaid
flowchart TD
  first["First run"] --> dc["MSAL public client, device code or interactive sign-in"]
  dc --> mfa["User completes MFA in browser"]
  mfa --> cache["Token cache encrypted with DPAPI via msal-extensions"]
  cache --> use["Adapters request tokens silently"]
  use --> exp{"Refresh fails: consent revoked, password change, conditional access"}
  exp -->|yes| card["Hand-off card: sign in again"]
  exp -->|no| use
  keys["ICA key, Slack tokens, SQL read-only login"] --> cred["Windows Credential Manager via keyring"]
```

Rules: no secret in code, config, logs or prompts; logs pass through a redaction filter for tokens, emails and keys; the console binds to 127.0.0.1 only and needs a local session token.

### F18 — Health monitor

Checks every 5 minutes: connector status, last successful poll per source, stalled tasks, jobs failing, schedules missed, ICA reachability, disk space. Problems appear at the top of the brief, in the console Connectors page and, once per incident, in the DM.

---

## 7. Adapters

Every adapter implements the same base: `health()`, typed read methods, typed write methods tagged with a mode, and a fake implementation for tests.

```mermaid
classDiagram
  class Adapter {
    +name
    +health()
    +tools()
  }
  class OutlookAdapter {
    +list_new(since)
    +get_thread(id)
    +move(id, folder)
    +create_draft(to, body, reply_to)
    +send(draft_id, idempotency_key)
  }
  class TeamsAdapter {
    +list_chat_messages(since)
    +post(chat_id, body, tag)
    +get_transcript(meeting_id)
  }
  class PowerBIAdapter {
    +resolve_link(url)
    +refresh_history(dataset_id)
    +trigger_refresh(dataset_id)
    +refreshes_today(dataset_id)
  }
  class ServiceNowEmailAdapter {
    +parse_assignment(mail)
    +parse_report(attachment)
    +list_open_tickets()
  }
  class DoxisAdapter {
    +facts_from_screenshot(image)
    +facts_from_citrix(user, site)
  }
  Adapter <|-- OutlookAdapter
  Adapter <|-- TeamsAdapter
  Adapter <|-- PowerBIAdapter
  Adapter <|-- ServiceNowEmailAdapter
  Adapter <|-- DoxisAdapter
```

### A01 — Outlook mail

Graph delta queries on Inbox and Sent Items every 5 minutes. If the client refuses an app registration, the COM fallback drives Outlook desktop on the laptop (pywin32), with the same interface.

Edge cases: delta token expired (HTTP 410, full resync of the last 7 days with dedupe); throttling (429 with Retry-After); HTML bodies and inline images (convert to text, drop images); shared mailboxes (out of scope unless configured); encrypted or sensitivity-labelled mail (metadata only, never sent to the model); Outlook not running for COM (start it, or report health red).

### A02 — Calendar

Reads today's and tomorrow's events. Creates tentative holds only on the user's own calendar. Edge: meetings in other time zones; recurring series exceptions; private events (title hidden, skipped).

### A03 — Teams

Graph polling of the user's chats (`/me/chats` and messages). Mentions are detected from the mention array, not text. Transcripts need `OnlineMeetingTranscript.Read.All`, likely with admin consent.

Edge cases: chat list grows large (track only chats active in 14 days plus linked task chats); edited and deleted messages; replies in channels versus chats; transcript not created because the organiser did not turn it on; Graph limits on chat message reads (back off per Retry-After).

### A04 — Slack

Slack Bolt in Socket Mode with an app-level token and bot token. Handles DMs, thread replies, button actions and slash commands.

Edge cases: workspace admin does not approve the app (fallback to Teams self chat or email-to-self); Socket Mode disconnects (auto-reconnect, health red after 2 minutes); message length limits (long drafts link to the console).

### A05 — Power BI

```mermaid
flowchart LR
  link["App, report or dashboard link"] --> parse["Parse group, app, report ids"]
  parse --> look["Look up report, then its dataset"]
  look --> hist["Refresh history and running refresh"]
  hist --> cap["Count today's refreshes against the cap"]
  hist --> up["Upstream dataflows and their last run"]
  cap --> plan["Refresh plan"]
  up --> plan
  plan --> gate["F06: Ask"]
  gate --> trig["Trigger dataflow, wait, trigger dataset"]
  trig --> poll["Poll every 2 min, timeout 60 min"]
```

Edge cases: refresh already running (do not trigger; report the running one); 8-per-day cap on shared capacity (warn before the last one); link to an app that bundles several reports (list them and ask); dataset owned by someone else and the user lacks Contributor (hand off); refresh succeeds but loads nothing new (R13 catches it).

### A06 — SharePoint lists

Lists by GUID, not display name. Internal column names in config. Paged reads. Edge: renamed columns, throttling, list permissions differ per site.

### A07 — ServiceNow email adapter

```mermaid
flowchart TD
  mail["Mail from the ServiceNow sender"] --> kind{"Type"}
  kind -->|assignment notification| p1["Parse INC, short description, caller, priority, link, opened time"]
  kind -->|scheduled report with attachment| p2["Parse CSV or XLSX rows: number, state, opened, updated, assignee"]
  kind -->|comment or state notification| p3["Parse update, add task event"]
  p1 --> ev1["Event: ticket_assigned"]
  p2 --> diff["Diff against ledger: new, changed state, closed, reassigned, unknown"]
  diff --> ev2["Events per change"]
  p3 --> ev3["Event: ticket_updated"]
  reply["Approved comment"] --> rep["Reply to the notification email, if inbound email actions work"]
```

Parsers are template-driven with golden test fixtures. A parse failure of a known sender raises health red and DMs the user once with the email subject.

Edge cases: report late or missing (watchdog falls back to email times and marks clocks "estimated"); CSV encoding and delimiter differences; time zone of the Opened column; a ticket in the report the ledger does not know (create it); report shows the ticket assigned to someone else (stop work on it).

### A08 — Doxis screenshot adapter and A11 OCR

The user drops a screenshot into the task's Slack thread or the console. OCR runs on the laptop with Windows OCR (`winrt` Windows.Media.Ocr), with Tesseract as a fallback. A field extractor maps text to known fields: site code, roles, project state, closing date, TS/NP flag. Each extracted field keeps its crop rectangle so the console can show it next to the image.

Edge cases: O/0 and l/1 confusion in codes (validate site codes against the directory pattern); low-resolution screenshots (ask for a larger one); a screenshot of the wrong page (classifier says which page it looks like).

### A09 — SQL read-only

`pyodbc` with a dedicated read-only login, queries limited to `COUNT(*)` and `MAX(date)` on named tables, 30-second timeout, run outside business hours.

### A10 — Local file indexer

Watches configured paths. Copies files before reading (Excel locks open files). Indexes rows and sections into FTS5 with source references. Re-index on change or on request.

### A12 — Citrix web session (Phase 4)

```mermaid
flowchart TD
  start["Doxis lookup job"] --> ctx["Playwright persistent context, own browser profile"]
  ctx --> portal["Open Citrix Workspace URL"]
  portal --> auth{"Signed in?"}
  auth -->|no| hand["Hand-off card: sign in, MFA by the user"]
  hand --> portal
  auth -->|yes| launch["Launch the published browser, open Doxis address"]
  launch --> probe{"Probe: Doxis DOM visible or canvas only?"}
  probe -->|DOM| dom["Read with selectors, read-only"]
  probe -->|canvas| pix["Clicks and keys sent to the tab by position, template matching for known screens"]
  pix --> shot["Screenshot only the tab, local OCR"]
  dom --> facts["Facts with source and timestamp"]
  shot --> facts
  facts --> close["Leave the session idle; never Save, Create, Approve or Delete"]
```

The user's physical mouse is never used. Runs need the laptop awake and the client's written approval.

Edge cases: session idle timeout; reconnect dialogs; changed resolution or zoom (template matching uses relative coordinates and checks a known landmark before every click); pop-ups; keyboard layout differences when typing into a streamed session.

---

## 8. Recipes

Each recipe below lists trigger, steps with mode and model, and its main edge cases. Modes: S = Silent, R = Report, A = Ask, H = Hand off.

### R01 — Morning brief

```mermaid
flowchart LR
  t["09:00 IST work day, or first wake after"] --> g1["Ledger: needs you, clocks, stalled"]
  t --> g2["Mail: replies owed, manager and stakeholder mail"]
  t --> g3["Calendar: today's meetings"]
  t --> g4["Power BI: failures and health"]
  t --> g5["Connector health"]
  g1 --> rank["Code ranks by clock, priority and age"]
  g2 --> rank
  g3 --> rank
  g4 --> rank
  g5 --> rank
  rank --> words["Haiku writes one line per item, with source links"]
  words --> post["Post to Slack DM and console S"]
```

Edge: nothing to report (send a two-line brief, not a blank one); the agent was off all yesterday (say so at the top).

### R02 — Inbox triage

Poll new mail → code rules first (sender lists, ServiceNow, newsletters) → Haiku classifies the rest into needs reply, FYI, notification → move and flag (R) → list in nightly digest with undo. Never deletes. Edge: mail from the manager always stays in Inbox, flagged.

### R03 — Draft replies

For mail classified "needs reply": build context → Sonnet drafts in the user's style (Haiku if budget spent) → save as Outlook draft (R) → approval card to send (A). Edge: thread has outside recipients (H); thread changed since draft (approval invalidated).

### R04 — Mention digest

Each check-in: unanswered Slack and Teams mentions and DMs, oldest first, with links (S).

### R05 — Refresh-failure watch

Poll refresh history every 10 minutes → new failure or overrun → match error text against known fixes in memory → DM alert (S) → for known transient errors on datasets the user owns, one retry (R).

### R06 — Nightly digest

19:00 IST: every Report action today with Undo buttons, every Hand off waiting, every approval still open, recipes in shadow with their would-do counts.

### R07 — Incident intake and triage (Doxis)

```mermaid
flowchart TD
  e["Assignment email"] --> p["Parse fields, start clock R"]
  p --> ps["Pseudonymise S"]
  ps --> tr["Detect language, translate, flag non-Doxis S Haiku"]
  tr --> ob["On-behalf detection with confidence S Haiku"]
  ob --> cl["Classify issue type S Haiku"]
  cl --> pr["Precedent search: FTS candidates by code, ranking by Haiku S"]
  pr --> ca["Rank causes, fill missing-facts checklist S Haiku or Sonnet"]
  ca --> q["Draft only questions the evidence cannot answer A"]
  ca --> l2{"L2-only type?"}
  l2 -->|yes| esc["Draft L2 escalation email A"]
  q --> card["Triage card in task thread S"]
  esc --> card
  ob -->|low confidence| askme["Ask the user who is affected"]
```

Edge: ticket not about Doxis (flag, suggest reassignment, no caller message); two issues in one ticket (Sonnet, both listed); caller already attached screenshots in the email (route to R09 directly).

### R08 — 24-hour KPI watchdog (Doxis)

Every 10 minutes against the ledger, hourly against the report. Thresholds: note at 12 h, alert at 18 h, urgent at 22 h. Each alert carries one next action. A code filter blocks the words "KPI", "24 hours" and "metric" in every caller-facing draft from every recipe. Edge: report missing (clocks marked estimated); On Hold pause rule unknown (config, shown on the card).

### R09 — Access investigation (Doxis)

```mermaid
flowchart TD
  t["Access ticket or screenshot dropped"] --> who["Checklist for the affected person S"]
  who --> src{"Phase"}
  src -->|2| ocr["Local OCR of screenshots S"]
  src -->|4| cit["A12 Citrix read-only lookup S"]
  ocr --> rules["Decision rules in code, source named S"]
  cit --> rules
  rules --> v{"Verdict"}
  v -->|site access needed| r10["Hand to R10"]
  v -->|by design| explain["Draft explanation A"]
  v -->|facts missing| q["Draft only the missing questions A"]
  v -->|no rule fits| l2["Draft L2 package Sonnet A"]
  rules --> dir["New site to KM/LBA pairs into directory, marked to review R"]
```

Decision rules: not a site member → site access request; DMS reader on closed project → revoked by design; TS/NP flagged → audience check; CTMF category → no access for all-users by design; obsolete document → browse only; sensitive approval pending → wait; project remapped by I2L → L2.

### R10 — KM/LBA redirect and closure (Doxis)

Identify site → directory lookup by code (names never go to the model) → stale or missing entry asks for a screenshot → Sonnet drafts on tokens, code inserts real names → approval (A) → after send, task waits for the caller's OK and passes to R11 → resolution note drafted for the user to paste (A, because the user pastes it into ServiceNow).

### R11 — On Hold chaser and confirmation detector (Doxis)

```mermaid
flowchart TD
  r["New reply on a waiting task"] --> c["Classify: confirmed, answered, new issue, out of office S Haiku"]
  c -->|confirmed| close["Draft thanks and resolution note A; user closes ticket H"]
  c -->|answered| back["Back to R07 or R09 R"]
  c -->|out of office| pause["Pause chasing until return date R"]
  c -->|new issue| needs["Needs you"]
  tick["Chase window 13:30 IST winter, 12:30 summer"] --> due{"No reply for 1, 3 or 5 days?"}
  due -->|yes| rem["Draft reminder for that step A"]
```

Only an explicit confirmation counts as "confirmed"; "thanks" alone does not.

### R12 — Refresh on request (Power BI)

Teams message with a Power BI link or "refresh" → resolve dataset (A05) → if the source has not changed since the last refresh, draft a reply with the last refresh time and offer to refresh anyway (A) → else approval card with plan and the reply text → on approval: dataflow, wait, dataset, poll → post the approved reply (covered by the approval) → failure goes to the user only.

### R13 — Refresh health and freshness (Power BI)

08:45 IST daily: last 60 refreshes per dataset into local history → median, slowest 10 %, today against median (flag over 1.5×), dataflow-before-dataset ordering → SQL counts and latest load date → Haiku words the brief section → schedule-change proposals are A.

### R14 — SharePoint history completeness (Power BI)

Monthly before the Global Cockpit refresh, then daily until complete: expected sites from config × lists → missing, duplicates, blanks computed in code → table to the user → reminders to site contacts (A) → when complete, offer the R12 refresh flow.

### R15 — Enhancement request tracker (Power BI)

Mail from configured stakeholders → classify and extract (needs `text` data rule; until then subject, sender and date only) → backlog item ENH-n (R) → acknowledgement draft in Outlook (R), send (A) → overdue flag after 2 working days → Friday digest to the user.

### R16 — Teams and meeting issue intake (Power BI)

Code pre-filter for Power BI terms → classify (needs `chat`) → ledger item (R) → after meetings, transcript segments mentioning the user or reports → action items (Haiku under 30 minutes, Sonnet over) → list to the user with undo (R) → recap to participants (A) → transcript text deleted after extraction.

### R17 — Ad hoc request from DM

The user types a request in the DM or a task thread. A generic agent loop with the user's allowed tools runs inside the policy gate. Output goes back to the same thread.

### R18 — Recipe from a repeated request

When the user asks "do this every Friday", the agent drafts a recipe YAML from the last successful ad hoc run, shows it, and saves it in Shadow mode after approval.

---

## 9. Console UI and API

React + Vite + TypeScript, Carbon Design System components (`@carbon/react`), served as static files by FastAPI at `http://127.0.0.1:8765`. Hash routes match the prototype: `#brief`, `#tasks`, `#task-<id>`, `#approvals`, `#recipes`, `#recipe-<slug>`, `#activity`, `#memory`, `#connectors`, `#prefs`. The product name shown is "Always-On Agent".

```mermaid
flowchart LR
  subgraph ui["Console, React"]
    p1["Morning brief"]
    p2["Tasks board and task thread"]
    p3["Approvals"]
    p4["Recipes and editor"]
    p5["Activity and workers"]
    p6["Memory"]
    p7["Connectors"]
    p8["Settings"]
  end
  api["FastAPI /api"]
  sse["Server-sent events /api/stream"]
  ui -->|fetch with session token| api
  sse -->|task, approval, activity updates| ui
  api --> svc["Services: ledger, approvals, recipes, memory, controls"]
```

| Endpoint | Purpose |
| --- | --- |
| `GET /api/brief` | Latest brief, clocks, Power BI health |
| `GET /api/tasks?project=` | Board data |
| `GET /api/tasks/{id}` | Card, summary, facts, events, precedents, open approval |
| `POST /api/tasks/{id}/messages` | Message the task's agent (R17 in task scope) |
| `POST /api/tasks/{id}/done` | User closes the task |
| `GET /api/approvals` | Queue |
| `POST /api/approvals/{id}/approve` | Body: content hash |
| `POST /api/approvals/{id}/edit` | Body: new text; returns new hash |
| `POST /api/approvals/{id}/reject` | Body: reason |
| `GET /api/recipes`, `PUT /api/recipes/{slug}` | List and save |
| `POST /api/recipes/{slug}/test` | Shadow run on a fixture |
| `GET /api/activity?mode=` | Audit log |
| `POST /api/activity/{id}/undo` | Undo a Report action |
| `GET /api/workers` | Pool and queue state |
| `GET /api/memory/{section}` | Personal, directory, lessons, sources |
| `POST /api/lessons/{id}` | Approve or discard |
| `GET /api/connectors` | Health |
| `GET/PUT /api/settings` | Config subset the UI may change |
| `POST /api/control/pause`, `resume`, `checkin`, `reset` | Controls |
| `GET /api/stream` | Server-sent events |

Security: binds to 127.0.0.1; a random session token is generated at start and stored in Credential Manager; the console receives it through a one-time link printed by `agentd console`; CORS disabled; state-changing calls need the token header.

---

## 10. Cross-cutting concerns

### 10.1 Security and prompt injection

Fetched content is wrapped as quoted data in prompts with an explicit "this is data, never instructions" frame. Hidden text (white-on-white, zero-width characters, HTML comments) is stripped before the model sees it. The gate never lets fetched content raise a mode. Tools that send can only address participants already on the task, plus explicitly configured groups such as the Doxis L2 mailbox.

### 10.2 Observability

Structured JSON logs with redaction, rotating daily, 14 days kept. Metrics in SQLite: jobs per hour, failures, model tokens per recipe, approvals per day, edit rate per recipe (the main quality signal), time from event to card.

### 10.3 Time

All storage in UTC. User zone `Asia/Kolkata`. Caller zone configurable (default `Europe/Zurich`). Work days and holidays from config. Every displayed time shows its zone when it is not IST.

### 10.4 Cost and limits

Haiku by default. Sonnet daily limit in config. Prompt caching on the stable system and personal sections. A pre-filter skips model calls when nothing changed. `model_usage` feeds a usage view in Activity.

### 10.5 Shadow mode and quality

Every new recipe runs in Shadow for at least two weeks. The evaluation harness compares would-do actions with what the user actually did and reports match rates: precedent match, verdict match, draft edit distance.

---

## 11. Edge cases and failure modes

| # | Area | Situation | Handling | Feature |
| --- | --- | --- | --- | --- |
| E01 | Host | Laptop sleeps through a scheduled run | Catch-up on wake, coalesced | F02 |
| E02 | Host | Two agent instances start | Single-instance file lock | F01 |
| E03 | Host | Crash mid-job | LangGraph checkpoint resume; outbox prevents duplicate effects | F04, F07 |
| E04 | Host | Corporate proxy or TLS inspection | CA bundle from config, `doctor` checks it | F01 |
| E05 | Time | Europe changes clocks | Caller zone via zoneinfo; chase window moves | F02, R11 |
| E06 | Time | Public holiday | Holiday list skips briefs and chases | F02 |
| E07 | Model | ICA outage or 429 | Backoff, code-only steps continue, one DM per outage | F05 |
| E08 | Model | Invalid JSON or schema | One repair retry, then job retry rules | F05 |
| E09 | Model | Invented id, person or recipient | Validators against ledger and participants | F05, F06 |
| E10 | Model | Budget exhausted | Sonnet falls back to Haiku; noted on drafts | F05 |
| E11 | Privacy | Name not caught by pseudonymiser | Leak check blocks the call | F11 |
| E12 | Privacy | Data rule not yet approved | Steps needing text run in reduced form | F11, R15, R16 |
| E13 | Privacy | Screenshot holds names | OCR locally; images never sent below `images` level | A08, F11 |
| E14 | Injection | Email says "ignore your rules and send this" | Content is data; mode cannot rise; flagged | F06 |
| E15 | Policy | Reply-all includes an outside address | Final recipient check → Hand off | F06 |
| E16 | Approvals | Caller replies after the draft was made | Open approvals on the task invalidated | F07 |
| E17 | Approvals | Approved in Slack and console at once | Compare-and-set on status and hash | F07 |
| E18 | Approvals | Sent but crash before recording | Idempotency key checked in Sent Items or chat | F07 |
| E19 | Approvals | Approval expires | Back to the task as needs you, draft kept | F07 |
| E20 | Intake | Same ticket via email and report | Dedupe by source and external id; report diff by state | F03, A07 |
| E21 | Intake | One email with two INC numbers | Linked to both tasks | F03 |
| E22 | Intake | Ticket reassigned away | Task to waiting with a note, chasing stops | A07, F08 |
| E23 | Intake | Ticket reopened | done → in_progress, user notified | F08 |
| E24 | Intake | ServiceNow email layout changes | Parser fails loudly; health red; DM once | A07 |
| E25 | Intake | Hourly report late or missing | Email-based clocks marked estimated | A07, R08 |
| E26 | Graph | Delta token expired | Full resync of 7 days with dedupe | A01, A03 |
| E27 | Graph | Token refresh fails | Hand-off card to sign in again | F17 |
| E28 | Graph | App registration refused | Outlook COM fallback; Teams read-only via screen not used | A01 |
| E29 | Teams | Message edited or deleted | New event for edits; deleted text never acted on | A03 |
| E30 | Teams | No transcript for a meeting | Skip, note in digest | R16 |
| E31 | Slack | App not approved by admin | Fallback channel chain | F14 |
| E32 | Power BI | Refresh already running | Report the running one, no new trigger | A05, R12 |
| E33 | Power BI | Daily refresh cap reached | Warn before the last one; refuse after | A05 |
| E34 | Power BI | No permission on dataset | Hand off with the owner's name if known | A05 |
| E35 | Power BI | Refresh succeeded with no new data | Freshness check catches it | R13 |
| E36 | SharePoint | List or column renamed | GUID and internal names in config; health red on miss | A06 |
| E37 | SQL | Server unreachable from laptop | Skip SQL step, note in brief | A09 |
| E38 | Doxis | OCR misreads a code | Validate against directory patterns; show crop | A08 |
| E39 | Doxis | Directory entry stale | Ask for a fresh screenshot before redirect | R10 |
| E40 | Doxis | On-behalf detection unsure | Ask the user who is affected | R07 |
| E41 | Doxis | Polite "thanks" read as fixed | Only explicit confirmations count | R11 |
| E42 | Citrix | Session timed out or needs MFA | Hand-off card; never handles passwords | A12 |
| E43 | Citrix | Canvas-only rendering | Positional input, template landmarks, local OCR | A12 |
| E44 | Memory | Summary contradicts card | Code check forces a rewrite | F09 |
| E45 | Memory | User deletes a fact | Removed with anything derived from it | F09 |
| E46 | Queue | Low-priority starvation | Age boost | F04 |
| E47 | Queue | Poison job | Fails twice → needs you with error | F04 |
| E48 | Recipes | Recipe lists a Hand off tool | Load refused | F12 |
| E49 | Recipes | Two recipes match one event | Both run unless exclusive | F12 |
| E50 | Notify | Alert fatigue | Daily push cap and batching | F14 |
| E51 | Files | Excel file open and locked | Copy, then read | A10 |
| E52 | Language | German or French ticket | Translate, store both, reply in the caller's language if configured | R07 |
| E53 | Leave | User on leave | Leave mode: no chases, no caller drafts, brief paused | F16 |

---

## 12. Phase-by-phase deployment

```mermaid
flowchart LR
  p0["Phase 0: approvals and spikes"] --> p1["Phase 1: MVP on laptop"]
  p1 --> p2["Phase 2: Doxis and Power BI recipes"]
  p2 --> p3["Phase 3: always-on host"]
  p3 --> p4["Phase 4: expand"]
  p0 -.- g0["Gate: data rule and app registration approved, ICA tool call works"]
  p1 -.- g1["Gate: 2 working weeks, no action outside its mode"]
  p2 -.- g2["Gate: recipes pass shadow-mode match targets"]
  p3 -.- g3["Gate: client approves the VM host"]
```

| Phase | Host | Intake | Storage | Channels |
| --- | --- | --- | --- | --- |
| 1–2 | Laptop | Polling, Socket Mode | SQLite | Slack primary, Teams read and post |
| 3 | Persistent VM | Webhooks where a public endpoint exists, polling otherwise | SQLite or Postgres | Slack, Teams bot |
| 4 | VM | Same, plus Citrix session lookups | Same | Same, plus voice notes |

---

## 13. Repository layout

```text
always-on-agent/
├── CLAUDE.md
├── progress.md
├── prompts.md
├── docs/architecture.md
├── backend/
│   ├── pyproject.toml
│   ├── src/agent/
│   │   ├── core/          config, clock, ids, logging, errors
│   │   ├── db/            schema, migrations (alembic), repositories
│   │   ├── runtime/       supervisor, scheduler, coordinator, queue, workers, outbox
│   │   ├── graphs/        LangGraph task graph and nodes
│   │   ├── model/         ICA client, router, schemas, prompt templates
│   │   ├── policy/        gate, registry, policy.yaml loader
│   │   ├── approvals/     service, hashing, invalidation
│   │   ├── ledger/        tasks, clocks, links, heartbeats
│   │   ├── memory/        personal, task memory, knowledge, lessons, context builder
│   │   ├── privacy/       pseudonymiser, data rule, outbound checks
│   │   ├── recipes/       schema, loader, runner, step modules
│   │   ├── adapters/      outlook_graph, outlook_com, calendar, teams, slack, powerbi,
│   │   │                  sharepoint, servicenow_email, doxis_screenshot, ocr, sql_readonly,
│   │   │                  files_index, citrix_session, fakes/
│   │   ├── channels/      slack_bot, teams_notify, email_self, cards
│   │   ├── api/           FastAPI app, routes, auth, SSE
│   │   └── cli.py         agentd entry point
│   └── tests/             unit, integration with fakes, golden parser fixtures, eval
├── console/               React + Vite + TypeScript + @carbon/react
├── config/
│   ├── config.example.yaml
│   ├── policy.yaml
│   ├── recipes/*.yaml
│   ├── directory.example.csv
│   └── parsers/servicenow/*.yaml
├── fixtures/              made-up emails, reports, chats, Power BI payloads, screenshots
└── scripts/               install_task.ps1, dev.ps1, doctor.ps1
```

---

## 14. Configuration reference

```yaml
timezone: Asia/Kolkata
caller_timezone: Europe/Zurich
work: { days: [mon, tue, wed, thu, fri], start: "09:00", end: "18:30", holidays: [] }
schedule:
  brief: "09:00"
  checkin_every_minutes: 120
  nightly_digest: "19:00"
  poll_minutes: { mail: 5, teams: 5, powerbi: 10, servicenow_report: 60 }
  chase_window_local_caller: "09:00"   # converted to IST each day
notifications:
  primary: slack                         # slack | teams_self | email_self
  max_pushes_per_day: 6
  quiet_hours: { start: "21:30", end: "08:30" }
models:
  default: haiku
  ids: { haiku: "SET_FROM_ICA", sonnet: "SET_FROM_ICA", opus: "SET_FROM_ICA" }
  sonnet_daily_limit: 40
  sonnet_for: [caller_drafts, long_transcripts, weekly_reports]
ica: { base_url_env: ICA_BASE_URL, key_ref: "keyring:always-on-agent/ica" }
graph: { tenant_id: "", client_id: "", scopes: [Mail.ReadWrite, Mail.Send, Calendars.ReadWrite, Chat.ReadWrite, Sites.Read.All] }
powerbi: { scopes: [Dataset.ReadWrite.All, Dataflow.ReadWrite.All, Report.Read.All, Workspace.Read.All], refresh_cap_per_day: 8 }
data_rule: { level: metadata }           # metadata | text | chat | images
company_domains: []
allowed_groups: { doxis_l2: "" }
clock: { start: opened, pause_on_hold: false, thresholds_hours: [12, 18, 22] }
workers: { pool_size: 4, max_steps_per_run: 12, job_timeout_seconds: 300, lock_ttl_minutes: 10 }
retention: { raw_text_days: 14, screenshots_days: 7, transcripts: delete_after_extract }
paths: { workbook: "", rights_matrix: "", specs: [], screenshots_inbox: "" }
console: { host: 127.0.0.1, port: 8765 }
```

`policy.yaml` maps every tool to its mode; Hand off entries are marked `locked: true` and cannot be changed from the console.

---

## 15. Testing strategy

| Layer | What | How |
| --- | --- | --- |
| Unit | Clock math, task-key resolution, pseudonymiser, gate rules, hashing | pytest, property tests for the pseudonymiser |
| Parsers | ServiceNow emails and reports, Power BI links | Golden fixtures, one per known layout |
| Adapters | Each adapter against its fake | Contract tests shared by real and fake |
| Integration | Event → task → approval → outbox with fakes | Async tests with a temporary DB |
| Recipes | Each recipe on fixture scenarios | Shadow runs with expected would-do actions |
| Model | Prompts and schemas | Recorded responses for CI; live Haiku smoke test behind a flag |
| Evaluation | Match against the user's real actions | Shadow-mode harness and weekly report |
| Console | Pages render and actions call the API | Vitest + React Testing Library, Playwright smoke |

No real client data in fixtures. All names, INC numbers and sites in fixtures are made up.

---

## 16. Open questions to settle in Phase 0

| # | Question | Blocks |
| --- | --- | --- |
| Q1 | Does ICA expose the Anthropic Messages API with tool use, and which model ids? Images? | F05, everything |
| Q2 | What may go to ICA: metadata, text, chat, images? In writing. | F11 levels |
| Q3 | Will the client approve an Entra app registration with the listed scopes? | A01–A06 |
| Q4 | Can Graph post to the user's own Teams chat? | F14 fallback |
| Q5 | Will the Slack workspace admin approve a Socket Mode app? | F14 primary |
| Q6 | Does the 24-hour clock start at Opened, and does On Hold pause it? | F08, R08 |
| Q7 | Can a ServiceNow report be scheduled hourly to the user's mailbox? Does replying to a notification add a comment? | A07 |
| Q8 | Citrix Inspect test: Doxis DOM or canvas? Is browser automation allowed? | A12 |
| Q9 | Power BI capacity per workspace; Execute Queries setting | A05, R13 |
| Q10 | Network route from the laptop to the SQL sources; read-only logins | A09 |
| Q11 | Does the laptop lock or sleep on a short timer? | F01, A12 |
