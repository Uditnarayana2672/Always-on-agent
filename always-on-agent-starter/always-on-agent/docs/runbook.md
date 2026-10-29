# Runbook

How to install, run and look after the Always-On Agent on the laptop. Commands run in PowerShell from the
repository root unless a step says `cd backend`.

## 1. Install (once)

1. Install `uv` and Node.js 20+.
2. Backend and console:

   ```powershell
   cd backend; uv sync; cd ..
   cd console; npm install; npm run build; cd ..
   ```

3. Config: copy `config/config.example.yaml` to `config/config.yaml` and fill in `me`, `company_domains`,
   `mail.manager`, `slack.user_id` (your member id, `U...`) and, once IT has approved it, `graph.tenant_id`
   and `graph.client_id`. `config/config.yaml` is git-ignored; never commit it.
4. Model gateway: put `ICA_BASE_URL=...` in `backend/.env`, then store the key:

   ```powershell
   cd backend; uv run agentd secret set ica
   ```

5. Check everything: `uv run agentd doctor` (read-only). Section 9 says what to do with each red line.
6. Start it at log-on, on unlock and every 15 minutes if it stopped:

   ```powershell
   .\scripts\install_task.ps1 -StartNow
   ```

   Remove it with `.\scripts\install_task.ps1 -Uninstall` (data and settings are kept).
7. Confirm: `cd backend; uv run agentd status` shows `State: running`.

## 2. Sign in

| What | How | Needed for |
| --- | --- | --- |
| Microsoft 365 (Graph, Power BI) | `uv run agentd signin` (device code; `--interactive` for a browser window) | Teams, Power BI, Graph mail. Needs the app registration (Q3) |
| Outlook without Graph | Open classic Outlook (`OUTLOOK.EXE`) and leave it running | Mail and calendar until the app registration exists |
| Slack | `uv run agentd secret set slack_bot_token`, then `slack_app_token`, and `slack.user_id` in config | Cards and DMs in Slack; until then everything is in the console |
| ICA | `uv run agentd secret set ica` | Wording, classification, drafts. Without it recipes run their code-only steps |

When a sign-in expires, the agent sends one message ("... is not working: sign in again"). Run
`uv run agentd signin` and the next poll picks up.

## 3. Daily use

- **Console:** `uv run agentd console` prints a one-time link (ten minutes). Open it once per browser.
- **Morning brief** at 09:00, **check-ins** every two hours, **nightly digest** at 19:00 (IST). Times are
  in Settings.
- **Approvals** come as cards in Slack, or on the Approvals page. An approval covers the exact text shown;
  editing it makes a new card that needs its own approval. Cards expire after 24 hours and the task goes
  back to Needs you with the draft kept.
- Anything that would reach someone outside `company_domains`, delete something, change a ServiceNow
  state, grant access or type a password is never done by the agent: you get a prepared hand-off to finish.

## 4. Switch a recipe from Shadow to Live

In Shadow a recipe records what it would do (the digest counts it as "would have acted N times") and acts
on nothing. Live lets it act within each tool's mode.

1. Look at a few days of its would-do lines first: Activity page, filter by the recipe.
2. Console, **Recipes** page: set its status to **Live**. This rewrites only the `status:` line of
   `config/recipes/<name>.yaml` and takes effect at once. If you edit the file by hand instead, restart
   the agent (end the scheduled task; it starts again within 15 minutes, or run `install_task.ps1
   -StartNow`): a running agent does not watch the files yet.
3. The next digest lists its Report actions with Undo. To go back, set it to **Shadow** the same way.

Phase 1 runs morning-brief, inbox-triage, mention-digest and nightly-digest Live; draft-replies and
refresh-watch stay in Shadow until you decide.

## 5. Pause and resume

- Console: **Pause agent** (brief page or Settings). Slack: `/agent pause`, `/agent resume`.
- While paused nothing above Silent runs: no filing, no drafts, no sends. Approvals you give are held and
  run after resume. Missed schedules catch up once on resume.
- For a stop of the process itself: end the scheduled task (Task Scheduler) or run with `-Uninstall`.

## 6. Read the audit log

Every tool call is in the audit log with its mode, who decided and the result.

- **Console, Activity page:** the newest first, filter by mode (Silent, Report, Ask, Hand off), with Undo
  where it is still possible.
- **Nightly digest:** today's Report actions, open approvals, hand-offs and shadow counts.
- **Raw:** the `audit_log` and `model_usage` tables in `backend/agent.db` (SQLite). Read-only queries are
  safe while the agent runs, for example the Friday report in section 10.

## 7. Undo

Report actions (filing, flags, categories, own drafts, calendar holds) can be undone:

- the **Undo** button on the digest line or the Activity page, or `/agent undo <id>` with the number from
  the digest.

Undo refuses when the item changed since (you moved the mail yourself, someone was invited to the hold)
and says why. Pressing it twice does nothing the second time. Things other people saw (sent mail, chat
posts, Prod refreshes, the refresh retry) cannot be undone.

## 8. Reset

Reset deletes task memory (tasks, summaries, facts, threads), lessons and the schedule history. It keeps
the audit log, the Settings page changes, sign-ins and the Graph delta cursors. Afterwards the pollers read
the last day again and rebuild tasks from it. Use it after a bad test period, not for everyday cleanup.

1. Pause the agent.
2. Console, **Settings**, **Reset**: confirm in the page (the confirmation is valid for a short time).
3. Resume.

## 9. When doctor shows red

Run `uv run agentd doctor`. Each line ends with a fix. The common ones:

| Line | Meaning | Do |
| --- | --- | --- |
| `config` FAIL | `config/config.yaml` is missing a section or has a bad value | Compare with `config/config.example.yaml` |
| `ICA reachable` FAIL | The gateway is down, the key is wrong or the network blocks it | Check `ICA_BASE_URL` in `backend/.env`; `agentd secret set ica`; try again later. Recipes keep their code-only steps meanwhile |
| `<model> ... not in the model list` | A model id in `models.ids` is not offered | Update `models.ids` in `config/config.yaml` |
| `network <host>` FAIL | No route to Microsoft or ICA | Set `HTTPS_PROXY`, and `SSL_CERT_FILE` to the company CA bundle if TLS is inspected |
| `Graph sign-in` WARN | No app registration yet | Expected until Q3; mail comes from Outlook desktop |
| Graph or Power BI FAIL after sign-in | Token expired or consent missing | `uv run agentd signin`; if it repeats, ask IT about the scopes in `docs/it-request-entra-app.md` |
| `Outlook COM` FAIL | Classic Outlook is not running (the new Outlook has no COM) | Open classic Outlook (`OUTLOOK.EXE`) and leave it running |
| `slack` WARN / FAIL | `slack.user_id` or the tokens are missing, or the app is not installed | Section 2; until then everything is in the console |
| `data rule` WARN | `data_rule.level` is set but not confirmed in writing (Q2) | Keep `metadata` until the written answer |
| `laptop power and lock` WARN | Sleep or lock timers are short | Expected (Q11); the agent catches up after sleep |

`agentd status` shows the same health summary, plus `Last wake from sleep ... (caught up)` after a sleep.
Exit code 0 running, 1 not running, 2 stuck (no heartbeat for minutes: end the task, it restarts within
15 minutes). The process log is `logs/agentd.out.log`.

## 10. Friday report (Phase 1 gate)

During the two working weeks of real use, each Friday ask Claude Code for a short report from
`backend/agent.db`: actions by mode, approvals and the edit rate, failures, tokens by recipe
(`audit_log`, `action_requests`, `model_usage`). The gate is passed when no action ran outside its mode and
the brief was read on most days.
