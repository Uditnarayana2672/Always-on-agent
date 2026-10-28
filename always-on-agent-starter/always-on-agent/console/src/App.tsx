// The console shell: dark header, side navigation with counts, hash routes as in the prototype
// (#brief, #tasks, #task-<id>, #approvals, #recipes, #recipe-<slug>, #activity, #memory,
// #connectors, #prefs). Live updates come from /api/stream; every event refetches what it touches.
import { useEffect, useState } from "react";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "./api";
import { NotFound, ToastProvider, useStatus } from "./components/ui";
import Activity from "./pages/Activity";
import Approvals from "./pages/Approvals";
import Brief from "./pages/Brief";
import Connectors from "./pages/Connectors";
import Memory from "./pages/Memory";
import Prefs from "./pages/Prefs";
import Recipe from "./pages/Recipe";
import Recipes from "./pages/Recipes";
import Task from "./pages/Task";
import Tasks from "./pages/Tasks";

export function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchOnWindowFocus: true,
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      },
    },
  });
}

// Which queries each stream event makes stale.
export const STREAM_KEYS: Record<string, string[]> = {
  task: ["tasks", "task", "brief", "status"],
  approval: ["approvals", "task", "tasks", "brief", "status"],
  activity: ["activity", "brief", "recipes", "recipe"],
  worker: ["workers"],
  control: ["status", "brief", "settings", "workers"],
};

export function useHash(): string {
  const [hash, setHash] = useState(() => (location.hash || "#brief").slice(1));
  useEffect(() => {
    const on = () => {
      setHash((location.hash || "#brief").slice(1));
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

function useStream() {
  const client = useQueryClient();
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const source = new EventSource("/api/stream", { withCredentials: true });
    const handlers = Object.entries(STREAM_KEYS).map(([kind, keys]) => {
      const handler = () => keys.forEach((key) => void client.invalidateQueries({ queryKey: [key] }));
      source.addEventListener(kind, handler);
      return [kind, handler] as const;
    });
    // After a reconnect (agentd restarted, laptop woke), anything may have changed.
    source.addEventListener("open", () => void client.invalidateQueries());
    return () => {
      handlers.forEach(([kind, handler]) => source.removeEventListener(kind, handler));
      source.close();
    };
  }, [client]);
}

export function route(hash: string): { nav: string; page: JSX.Element } {
  if (hash === "brief" || hash === "") return { nav: "brief", page: <Brief /> };
  if (hash === "tasks") return { nav: "tasks", page: <Tasks /> };
  if (hash.startsWith("task-")) return { nav: "tasks", page: <Task key={hash} id={hash.slice(5).toUpperCase()} /> };
  if (hash === "approvals") return { nav: "approvals", page: <Approvals /> };
  if (hash === "recipes") return { nav: "recipes", page: <Recipes /> };
  if (hash.startsWith("recipe-")) return { nav: "recipes", page: <Recipe key={hash} slug={hash.slice(7)} /> };
  if (hash === "activity") return { nav: "activity", page: <Activity /> };
  if (hash === "memory") return { nav: "memory", page: <Memory /> };
  if (hash === "connectors") return { nav: "connectors", page: <Connectors /> };
  if (hash === "prefs") return { nav: "prefs", page: <Prefs /> };
  return { nav: "", page: <NotFound /> };
}

function NavLink({ to, active, children }: { to: string; active: string; children: React.ReactNode }) {
  return (
    <a href={`#${to}`} data-route={to} className={active === to ? "active" : undefined} aria-current={active === to ? "page" : undefined}>
      {children}
    </a>
  );
}

function SignIn() {
  return (
    <div className="page login">
      <div className="page-head">
        <div>
          <div className="eyebrow">Signed out</div>
          <h1>Open the console from its link</h1>
          <p className="lede">
            The console only answers on this laptop, to a browser that opened its one-time link. In a terminal in the
            backend folder, run <span className="mono">uv run agentd console</span> and open the link it prints. The
            link works once, for ten minutes.
          </p>
        </div>
      </div>
    </div>
  );
}

export function Shell() {
  const hash = useHash();
  const status = useStatus();
  useStream();
  const signedOut = status.error instanceof ApiError && status.error.status === 401;
  const { nav, page } = route(hash);
  const s = status.data;
  const paused = s?.paused ?? false;
  const offline = status.isError && !signedOut;

  useEffect(() => {
    document.title = s && s.approvals ? `(${s.approvals}) Always-On Agent` : "Always-On Agent";
  }, [s]);

  return (
    <>
      <header className="shell">
        <div className="shell-left">
          <span className="shell-name">
            Always-On <b>Agent</b>
          </span>
          <span className="shell-sep"></span>
          <span className="shell-crumb">Doxis and Power BI support</span>
        </div>
        <span className="status">
          <span className={`dot${paused || offline ? " paused" : ""}`} id="statusDot"></span>
          <span id="statusText">
            {offline
              ? "Not reachable · is agentd running?"
              : paused
                ? "Paused · nothing runs"
                : s
                  ? `Running · ${s.model}${s.leave ? " · on leave" : ""}`
                  : "Connecting…"}
          </span>
        </span>
      </header>
      <div className="app">
        <aside className="side">
          <nav id="nav" aria-label="Pages">
            <span className="nav-group">Today</span>
            <NavLink to="brief" active={nav}>
              Morning brief
            </NavLink>
            <NavLink to="tasks" active={nav}>
              Tasks <span className="count" id="navTasks">{s && s.needs_you ? s.needs_you : ""}</span>
            </NavLink>
            <NavLink to="approvals" active={nav}>
              Approvals <span className="count" id="navApprovals">{s && s.approvals ? s.approvals : ""}</span>
            </NavLink>
            <span className="nav-group">How it works</span>
            <NavLink to="recipes" active={nav}>
              Recipes
            </NavLink>
            <NavLink to="activity" active={nav}>
              Activity
            </NavLink>
            <NavLink to="memory" active={nav}>
              Memory
            </NavLink>
            <NavLink to="connectors" active={nav}>
              Connectors
            </NavLink>
            <NavLink to="prefs" active={nav}>
              Settings
            </NavLink>
          </nav>
          <div className="side-foot">
            <span>{s?.phase ?? "Phase 1 · on your laptop"}</span>
            <span>{s?.fake ? "Sample data from the fixtures (agentd run --fake)" : "Your data stays on this laptop"}</span>
          </div>
        </aside>
        <main id="main">{signedOut ? <SignIn /> : page}</main>
      </div>
    </>
  );
}

export default function App({ client }: { client?: QueryClient }) {
  const [queryClient] = useState(() => client ?? makeClient());
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <Shell />
      </ToastProvider>
    </QueryClientProvider>
  );
}
