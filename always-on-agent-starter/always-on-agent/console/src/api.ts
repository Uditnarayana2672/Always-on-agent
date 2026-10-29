// Typed client for the agent's /api (backend/src/agent/api/models.py). Same origin only; the session
// cookie comes from the one-time link printed by `agentd console`. Writes carry X-Agent-Request,
// which a cross-site form cannot set.

export type Mode = "Silent" | "Report" | "Ask" | "Hand off" | "Blocked";
export type Column = "needs" | "progress" | "waiting" | "scheduled" | "done";
export type Project = "Core" | "Doxis" | "Power BI";

export interface Status {
  paused: boolean;
  leave: boolean;
  model: string;
  needs_you: number;
  approvals: number;
  phase: string;
  fake: boolean;
}

export interface Brief {
  day_label: string;
  greeting: string;
  paused: boolean;
  last_checkin: string | null;
  next_checkin: string | null;
  open_tasks: number;
  approvals: number;
  model_line: string;
  needs: { id: string; title: string; project: string; reason: string }[];
  clocks: { id: string; title: string; age_hours: number; paused: boolean; estimated: boolean }[];
  thresholds: number[];
  powerbi_stats: { value: string; label: string }[];
  powerbi_items: { title: string; meta: string; task_id: string | null; severity: "alert" | "info" }[];
  done: { audit_id: number; text: string; time: string; ref: string; undo: "available" | "done" | "none" }[];
  meetings: { start: string; end: string; subject: string; join_url: string | null }[];
  meetings_note: string | null;
  latest_text: string | null;
  connectors_down: string[];
}

export interface TaskCard {
  id: string;
  project: string;
  title: string;
  column: Column;
  state: string;
  age_hours: number | null;
  clock: boolean;
  next: string;
  has_approval: boolean;
  recipe: string | null;
}

export interface TaskEvent {
  time: string;
  who: "agent" | "you" | "system" | "caller";
  name: string;
  text: string;
}

export interface Approval {
  id: string;
  task_id: string | null;
  task_title: string | null;
  kind: string;
  to: string;
  via: string;
  recipe: string | null;
  text: string;
  content_hash: string;
  status: string;
  edited: boolean;
  expires_at: string | null;
  expires_label: string;
  urgent: boolean;
  note: string | null;
}

export interface TaskDetail {
  task: TaskCard;
  kind: string;
  recipe_label: string;
  caller: string;
  affected: string;
  site: string;
  priority: string;
  summary: string | null;
  checks: { label: string; status: "pass" | "fail" | "unknown"; detail: string }[];
  precedents: { id: string; text: string }[];
  events: TaskEvent[];
  approval: Approval | null;
}

export interface Decision {
  id: string;
  status: string;
  content_hash: string;
  changed: boolean;
  message: string;
}

export interface Tool {
  name: string;
  mode: Mode;
  locked: boolean;
}

export type Output = "approval_card" | "digest" | "dm" | "report" | "none";

export interface Recipe {
  slug: string;
  name: string;
  project: Project;
  trigger: string;
  trigger_label: string;
  model: string;
  status: "Live" | "Shadow" | "Off";
  last_run: string;
  tools: string[];
  instructions: string;
  output: Output;
  output_label: string;
  has_steps: boolean;
  file: string;
  error: string | null;
  runs: number;
  would_do: number;
}

export interface RecipeIn {
  name: string;
  project: Project;
  trigger: string;
  model: "Haiku" | "Sonnet" | "Opus" | "None";
  status?: "Live" | "Shadow" | "Off";
  tools: string[];
  instructions: string;
  output: Output;
}

export interface RecipeTest {
  sample: string;
  proposals: { tool: string; mode: Mode; note: string }[];
  notes: string[];
  needs_approval: boolean;
}

export interface Activity {
  audit_id: number;
  time: string;
  at: string;
  ref: string;
  action: string;
  tool: string;
  mode: Mode;
  result: string;
  undo: "available" | "done" | "none";
}

export interface Workers {
  coordinator: string;
  queue: number;
  workers: { name: string; busy: boolean; doing: string; note: string }[];
}

export interface Memory {
  section: string;
  profile: string | null;
  preferences: string | null;
  style_samples?: number;
  directory: { site: string; name: string; km: string; lba: string; last_checked: string; stale: boolean; source: string }[];
  lessons: { id: string; text: string; source: string; status: string }[];
  sources: { source: string; items: number; last_indexed: string }[];
}

export interface Connector {
  name: string;
  status: "ok" | "part" | "pend" | "no";
  status_label: string;
  via: string;
  can: string[];
  scopes: string;
  detail: string | null;
}

export interface PolicyRow {
  key: string;
  label: string;
  mode: Mode;
  options: Mode[];
  locked: boolean;
  reaches_people: boolean;
  tools: string[];
}

export interface Settings {
  work_start: string;
  work_end: string;
  brief: string;
  nightly_digest: string;
  checkin_every_minutes: number;
  max_pushes_per_day: number;
  quiet_start: string;
  quiet_end: string;
  default_model: string;
  sonnet_for: string[];
  sonnet_daily_limit: number;
  data_rule: string;
  data_rule_approved: boolean;
  policy: PolicyRow[];
}

export type SettingsIn = Partial<
  Pick<
    Settings,
    | "work_start"
    | "work_end"
    | "brief"
    | "nightly_digest"
    | "checkin_every_minutes"
    | "max_pushes_per_day"
    | "sonnet_for"
    | "sonnet_daily_limit"
  >
> & { policy?: Record<string, Mode> };

export interface ControlResult {
  paused: boolean;
  message: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-Agent-Request"] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = response.statusText || `Request failed (${response.status})`;
    try {
      const data = (await response.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
      else if (Array.isArray(data.detail) && data.detail[0]?.msg) detail = String(data.detail[0].msg);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

const enc = encodeURIComponent;

export const api = {
  status: () => call<Status>("GET", "/api/status"),
  brief: () => call<Brief>("GET", "/api/brief"),
  tasks: (project?: string) =>
    call<TaskCard[]>("GET", project && project !== "All" ? `/api/tasks?project=${enc(project)}` : "/api/tasks"),
  task: (id: string) => call<TaskDetail>("GET", `/api/tasks/${enc(id)}`),
  message: (id: string, text: string) => call<TaskEvent>("POST", `/api/tasks/${enc(id)}/messages`, { text }),
  markDone: (id: string) => call<TaskCard>("POST", `/api/tasks/${enc(id)}/done`, {}),
  approvals: () => call<Approval[]>("GET", "/api/approvals"),
  approve: (id: string, content_hash: string) =>
    call<Decision>("POST", `/api/approvals/${enc(id)}/approve`, { content_hash }),
  edit: (id: string, text: string, content_hash: string) =>
    call<Decision>("POST", `/api/approvals/${enc(id)}/edit`, { text, content_hash }),
  reject: (id: string, reason = "") => call<Decision>("POST", `/api/approvals/${enc(id)}/reject`, { reason }),
  recipes: () => call<Recipe[]>("GET", "/api/recipes"),
  recipe: (slug: string) => call<Recipe>("GET", `/api/recipes/${enc(slug)}`),
  tools: () => call<{ tools: Tool[] }>("GET", "/api/recipes/tools"),
  saveRecipe: (slug: string, body: RecipeIn) => call<Recipe>("PUT", `/api/recipes/${enc(slug)}`, body),
  recipeStatus: (slug: string, status: Recipe["status"]) =>
    call<Recipe>("POST", `/api/recipes/${enc(slug)}/status`, { status }),
  testRecipe: (slug: string) => call<RecipeTest>("POST", `/api/recipes/${enc(slug)}/test`, {}),
  activity: (mode?: string) =>
    call<Activity[]>("GET", mode && mode !== "All" ? `/api/activity?mode=${enc(mode)}` : "/api/activity"),
  undo: (auditId: number) => call<{ status: string; message: string }>("POST", `/api/activity/${auditId}/undo`, {}),
  workers: () => call<Workers>("GET", "/api/workers"),
  memory: (section: string) => call<Memory>("GET", `/api/memory/${enc(section)}`),
  reindex: (source: string) => call<ControlResult>("POST", `/api/memory/sources/${enc(source)}/reindex`, {}),
  lesson: (id: string, action: "approve" | "discard") =>
    call<{ id: string; status: string }>("POST", `/api/lessons/${enc(id)}`, { action }),
  connectors: () => call<Connector[]>("GET", "/api/connectors"),
  settings: () => call<Settings>("GET", "/api/settings"),
  saveSettings: (body: SettingsIn) => call<Settings>("PUT", "/api/settings", body),
  pause: () => call<ControlResult>("POST", "/api/control/pause", {}),
  resume: () => call<ControlResult>("POST", "/api/control/resume", {}),
  checkin: () => call<ControlResult>("POST", "/api/control/checkin", {}),
  resetRequest: () => call<{ token: string }>("POST", "/api/control/reset/request", {}),
  reset: (token: string) => call<ControlResult>("POST", "/api/control/reset", { token }),
};

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}
