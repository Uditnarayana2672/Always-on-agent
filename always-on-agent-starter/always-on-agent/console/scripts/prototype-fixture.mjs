// Turns the prototype's sample data (docs/prototype/agent-console.html) into API responses, so the
// console can be rendered with exactly the prototype's content: Vitest page tests and the Playwright
// pixel comparison (tests/pixel.spec.ts) both use src/test/prototype-data.json.
// Run: node scripts/prototype-fixture.mjs
import { readFileSync, writeFileSync } from "node:fs";

const html = readFileSync(new URL("../../docs/prototype/agent-console.html", import.meta.url), "utf8");
const start = html.indexOf("/* ---------- sample data ---------- */");
const end = html.indexOf("/* ---------- helpers ---------- */");
const now = () => "09:00";
const S = new Function("now", `${html.slice(start, end)}; return { S, TOOLS };`)(now);
const { S: data, TOOLS } = S;

const COL = { needs: "needs", progress: "progress", waiting: "waiting", scheduled: "scheduled", done: "done" };
const STATE = { needs: "needs_you", progress: "in_progress", waiting: "waiting", scheduled: "scheduled", done: "done" };
const doxisOpen = (t) => t.project === "Doxis" && !["done", "scheduled"].includes(t.state);
const card = (t) => ({
  id: t.id, project: t.project, title: t.title, column: COL[t.state], state: STATE[t.state],
  age_hours: doxisOpen(t) ? t.ageH : null, clock: doxisOpen(t), next: t.next,
  has_approval: Boolean(t.approval && data.approvals.some((a) => a.id === t.approval)), recipe: t.recipe,
});
const approval = (a) => {
  const t = data.tasks.find((x) => x.id === a.task);
  return {
    id: a.id, task_id: a.task, task_title: t.title, kind: a.kind, to: a.to, via: a.via, recipe: a.recipe,
    text: a.text, content_hash: `hash-${a.id}`, status: "proposed", edited: false, expires_at: null,
    expires_label: a.expires, urgent: Boolean(a.urgent), note: null,
  };
};
const detail = (t) => ({
  task: card(t), kind: t.type, recipe_label: t.recipe, caller: t.caller, affected: t.affected, site: t.site,
  priority: t.priority, summary: t.summary,
  checks: t.facts.map((f) => ({ label: f[0], status: f[1], detail: f[2] })),
  precedents: t.precedents.map((p) => ({ id: p[0], text: p[1] })),
  events: t.events.map((e) => ({ time: e[0], who: e[1], name: e[2], text: e[3] })),
  approval: t.approval ? approval(data.approvals.find((a) => a.id === t.approval)) : null,
});
const reasons = {
  INC30109981: "Crosses 24 hours at 10:36. Escalation to L2 is drafted.",
  INC30114872: "At 19 h. The redirect to the site KM/LBA is drafted.",
  "REF-0412": "Priya asked 25 minutes ago. Source changed after the last refresh.",
  INC30108754: "No reply since yesterday. Reminder ready for 13:30 IST.",
  "ENH-014": "Marc has waited 2 working days for a reply.",
};
const needs = data.tasks
  .filter((t) => t.state === "needs")
  .sort((a, b) => (b.project === "Doxis" ? b.ageH : 0) - (a.project === "Doxis" ? a.ageH : 0));
const cStart = html.indexOf("const c = [", html.indexOf("function pageConnectors"));
const protoConnectors = new Function(`${html.slice(cStart, html.indexOf("];", cStart) + 2)} return c;`)();
const modeOf = (t) => (TOOLS.find((x) => x[0] === t) || [t, "Ask"])[1];

const out = {
  status: { paused: false, leave: false, model: "Haiku", needs_you: needs.length, approvals: data.approvals.length, phase: "Phase 1 · on your laptop", fake: true },
  brief: {
    day_label: "Wed 28 Oct · 09:00 IST brief",
    greeting: `Good morning, Udit. ${needs.length} things need you.`,
    paused: false, last_checkin: "08:58", next_checkin: "11:00",
    open_tasks: data.tasks.filter((t) => !["done", "scheduled"].includes(t.state)).length,
    approvals: data.approvals.length, model_line: "Haiku, Sonnet for caller drafts",
    needs: needs.map((t) => ({ id: t.id, title: t.title, project: t.project, reason: reasons[t.id] || t.next })),
    clocks: data.tasks.filter(doxisOpen).sort((a, b) => b.ageH - a.ageH)
      .map((t) => ({ id: t.id, title: t.title, age_hours: t.ageH, paused: false, estimated: false })),
    thresholds: [12, 18, 22],
    powerbi_stats: [{ value: "13 / 14", label: "datasets normal" }, { value: "41 / 43", label: "ISMS sites in" }, { value: "2", label: "enhancements waiting" }],
    powerbi_items: [
      { title: "Forecast Hub results table has no new rows since Monday", meta: "Dataset refresh succeeded, so the SQL load job is the likely cause.", task_id: "PBI-H03", severity: "alert" },
      { title: "Pipeline Tracker refresh took 42 min", meta: "The 30-day median is 18 min. No action needed yet.", task_id: null, severity: "info" },
    ],
    done: data.activity.filter((a) => a.mode === "Report").map((a, i) => ({ audit_id: 100 + i, text: a.action, time: a.time, ref: a.ref, undo: a.undo ? "available" : "none" })),
    meetings: [
      { start: "11:30", end: "12:00", subject: "Doxis support weekly", join_url: null },
      { start: "15:00", end: "15:30", subject: "Pipeline Tracker review with Marc", join_url: null },
    ],
    meetings_note: "Prep briefs 30 min before", latest_text: null, connectors_down: [],
  },
  tasks: data.tasks.map(card),
  task_details: Object.fromEntries(data.tasks.map((t) => [t.id, detail(t)])),
  approvals: data.approvals.map(approval),
  recipes: data.recipes.map((r) => ({
    slug: r.slug, name: r.name, project: r.project, trigger: "schedule: brief", trigger_label: r.trigger, model: r.model,
    status: r.status, last_run: r.last, tools: r.tools, instructions: r.instructions, output: "dm", output_label: r.output,
    has_steps: true, file: `recipes/${r.slug}.yaml`, error: null, runs: 0, would_do: 0,
  })),
  tools: TOOLS.map(([name, mode]) => ({ name, mode, locked: mode === "Hand off" })),
  activity: data.activity.map((a, i) => ({
    audit_id: 200 + i, time: a.time, at: "2026-10-28T03:00:00Z", ref: a.ref, action: a.action, tool: a.tool,
    mode: a.mode, result: a.result, undo: a.undo ? "available" : "none",
  })),
  workers: {
    coordinator: "Watching Outlook, Teams, Power BI", queue: 2,
    workers: [
      { name: "Worker 1", busy: true, doing: "INC30117002 · precedent search", note: "14 s · Haiku" },
      { name: "Worker 2", busy: false, doing: "Idle", note: "—" },
      { name: "Worker 3", busy: true, doing: "PBI-H03 · SQL row count", note: "3 s · code only" },
      { name: "Worker 4", busy: false, doing: "Idle", note: "Next: INC30115230 · reply received" },
    ],
  },
  memory: {
    personal: { section: "personal", profile: "Udit · App developer · Bangalore (IST)\nSupports Doxis (RDDMS) incidents and Power BI reports for the client\nWorking hours 09:00–18:30 IST, Mon–Fri\nMost callers work on CET", preferences: "Keep caller messages short; offer a with-steps version\nSign caller messages \"Thanks, Udit\"\nNever mention the 24-hour KPI to callers\nBatch non-urgent items into the next check-in\nAt most 6 pushes a day", style_samples: 24, directory: [], lessons: [], sources: [] },
    directory: { section: "directory", profile: null, preferences: null, lessons: [], sources: [], directory: data.directory.map((d) => ({ site: d[0], name: d[1], km: d[2], lba: d[3], last_checked: d[4], stale: d[4] === "04 Jun", source: d[5] })) },
    lessons: { section: "lessons", profile: null, preferences: null, directory: [], sources: [], lessons: data.lessons.map((l) => ({ id: l.id, text: l.text, source: l.from, status: l.state })) },
    sources: { section: "sources", profile: null, preferences: null, directory: [], lessons: [], sources: [["Doxis incident workbook (L2 history)", 408, "21 Oct"], ["Closed tickets saved as precedents", 37, "Today 02:10"]].map(([source, items, last_indexed]) => ({ source, items, last_indexed })) },
  },
  connectors: protoConnectors.map(([name, status, status_label, via, can, scopes]) => ({ name, status, status_label, via, can, scopes, detail: null })),
  settings: {
    work_start: "09:00", work_end: "18:30", brief: "09:00", nightly_digest: "19:00", checkin_every_minutes: 120,
    max_pushes_per_day: 6, quiet_start: "21:30", quiet_end: "08:30", default_model: "Haiku",
    sonnet_for: ["caller_drafts", "long_transcripts"], sonnet_daily_limit: 40, data_rule: "metadata", data_rule_approved: false,
    policy: [
      ["read", "Read mail, chats, reports and files", "Silent", ["Silent", "Report", "Ask"]],
      ["file_mail", "File or flag your own mail", "Report", ["Report", "Ask"]],
      ["save_draft", "Save a draft", "Report", ["Report", "Ask"]],
      ["calendar", "Add a task or a hold on your own calendar", "Report", ["Report", "Ask"]],
      ["retry_refresh", "Retry your own failed refresh (known transient error)", "Report", ["Report", "Ask"]],
      ["send", "Send an email or chat message", "Ask", ["Ask"]],
      ["refresh", "Trigger a Prod refresh", "Ask", ["Ask"]],
      ["shared_edit", "Edit a shared list or document", "Ask", ["Report", "Ask"]],
      ["ticket_state", "Change a ServiceNow ticket state", "Hand off", ["Hand off"]],
      ["delete", "Delete anything", "Hand off", ["Hand off"]],
      ["outside", "Message anyone outside the company", "Hand off", ["Hand off"]],
      ["access", "Grant access", "Hand off", ["Hand off"]],
    ].map(([key, label, mode, options]) => ({ key, label, mode, options, locked: mode === "Hand off", reaches_people: key === "send", tools: [] })),
  },
};
writeFileSync(new URL("../src/test/prototype-data.json", import.meta.url), JSON.stringify(out, null, 1) + "\n");
console.log("wrote src/test/prototype-data.json");
