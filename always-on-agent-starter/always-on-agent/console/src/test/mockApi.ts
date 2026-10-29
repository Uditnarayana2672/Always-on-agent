// A fake /api for page tests: answers from the prototype's data, records every call.
import data from "./prototype-data.json" with { type: "json" };

export type Call = { method: string; path: string; body: unknown };
type Handler = (body: unknown, path: string) => unknown;
export type Overrides = Record<string, Handler | { status: number; detail: string }>;

export const fixture = data;

export function routeApi(method: string, path: string, body: unknown, overrides: Overrides = {}): { status: number; json: unknown } {
  const key = `${method} ${path.split("?")[0]}`;
  const override = overrides[key];
  if (override) {
    if (typeof override === "function") return { status: 200, json: override(body, path) };
    return { status: override.status, json: { detail: override.detail } };
  }
  const p = path.split("?")[0];
  const query = new URLSearchParams(path.split("?")[1] ?? "");
  if (method === "GET") {
    if (p === "/api/status") return ok(data.status);
    if (p === "/api/brief") return ok(data.brief);
    if (p === "/api/tasks") {
      const project = query.get("project");
      return ok(data.tasks.filter((t) => !project || t.project === project));
    }
    if (p.startsWith("/api/tasks/")) {
      const id = decodeURIComponent(p.slice("/api/tasks/".length));
      const d = (data.task_details as Record<string, unknown>)[id];
      return d ? ok(d) : { status: 404, json: { detail: `no task ${id}` } };
    }
    if (p === "/api/approvals") return ok(data.approvals);
    if (p === "/api/recipes") return ok(data.recipes);
    if (p === "/api/recipes/tools") return ok({ tools: data.tools });
    if (p.startsWith("/api/recipes/")) {
      const r = data.recipes.find((x) => x.slug === p.slice("/api/recipes/".length));
      return r ? ok(r) : { status: 404, json: { detail: "no recipe" } };
    }
    if (p === "/api/activity") {
      const mode = query.get("mode");
      return ok(data.activity.filter((a) => !mode || a.mode === mode));
    }
    if (p === "/api/workers") return ok(data.workers);
    if (p.startsWith("/api/memory/")) {
      const section = p.slice("/api/memory/".length) as keyof typeof data.memory;
      return data.memory[section] ? ok(data.memory[section]) : { status: 404, json: { detail: "no section" } };
    }
    if (p === "/api/connectors") return ok(data.connectors);
    if (p === "/api/settings") return ok(data.settings);
  }
  if (method === "POST" && p.endsWith("/approve")) return ok({ id: "x", status: "approved", content_hash: "h", changed: true, message: "Approved." });
  if (method === "POST" && p.endsWith("/reject")) return ok({ id: "x", status: "rejected", content_hash: "h", changed: true, message: "Rejected." });
  if (method === "POST" && p.endsWith("/edit")) return ok({ id: "x", status: "edited", content_hash: "h2", changed: true, message: "Edit saved." });
  if (method === "POST" && p === "/api/control/pause") return ok({ paused: true, message: "Paused. Nothing runs until you resume." });
  if (method === "POST" && p === "/api/control/resume") return ok({ paused: false, message: "Resumed." });
  if (method === "POST" && p === "/api/control/checkin") return ok({ paused: false, message: "Check-in started. Results arrive in your DM." });
  if (method === "PUT" && p === "/api/settings") {
    const { policy, ...rest } = body as { policy?: Record<string, string> };
    const rows = data.settings.policy.map((r) => (policy && policy[r.key] ? { ...r, mode: policy[r.key] } : r));
    return ok({ ...data.settings, ...rest, policy: rows });
  }
  return { status: 404, json: { detail: `no mock for ${key}` } };
}

function ok(json: unknown) {
  return { status: 200, json };
}

/** Install a fetch mock; returns the list of calls made. */
export function mockFetch(overrides: Overrides = {}): Call[] {
  const calls: Call[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input.toString();
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path, body });
    const { status, json } = routeApi(method, path, body, overrides);
    return new Response(JSON.stringify(json), { status, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  return calls;
}
