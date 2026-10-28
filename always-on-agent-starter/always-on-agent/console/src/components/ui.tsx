// Small pieces shared by the pages, with the prototype's markup and class names.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, errorText, type Mode } from "../api";

export const STATE_LABEL: Record<string, string> = {
  needs: "Needs you",
  progress: "In progress",
  waiting: "Waiting on others",
  scheduled: "Scheduled",
  done: "Done",
};

export const ageClass = (h: number) => (h >= 22 ? "crit" : h >= 12 ? "warn" : "");
export const fmtAge = (h: number) =>
  h < 1 ? `${Math.round(h * 60)}m` : h < 48 ? `${h.toFixed(1)}h` : `${Math.round(h / 24)}d`;
export const taskHref = (id: string) => `#task-${id.toLowerCase()}`;
export const barWidth = (h: number) => `${Math.min(h / 24, 1) * 100}%`;

export function ModeBadge({ mode }: { mode: Mode }) {
  return <span className={`mode ${mode === "Hand off" ? "Handoff" : mode}`}>{mode}</span>;
}

// ---------------------------------------------------------------- toast

type Toast = (message: string) => void;
const ToastContext = createContext<Toast>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const toast = useCallback((text: string) => {
    setMessage(text);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setMessage(null), 3200);
  }, []);
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div className="toast" id="toast" role="status" hidden={message === null}>
        {message}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);

// ---------------------------------------------------------------- pause

export function useStatus() {
  return useQuery({ queryKey: ["status"], queryFn: api.status });
}

/** Pause or resume, with the toast saying what happened. */
export function usePauseToggle() {
  const toast = useToast();
  const client = useQueryClient();
  const status = useStatus();
  const paused = status.data?.paused ?? false;
  const mutation = useMutation({
    mutationFn: () => (paused ? api.resume() : api.pause()),
    onSuccess: (result) => {
      toast(result.message);
      client.setQueryData(["status"], (old: unknown) =>
        old ? { ...(old as object), paused: result.paused } : old,
      );
      void client.invalidateQueries();
    },
    onError: (error) => toast(errorText(error)),
  });
  return { paused, toggle: () => mutation.mutate(), busy: mutation.isPending };
}

export function PausedBanner() {
  const { paused, toggle, busy } = usePauseToggle();
  if (!paused) return null;
  return (
    <div className="banner">
      <span>
        <b>The agent is paused.</b> No check-ins, schedules or pending actions will run until you resume.
      </span>
      <button className="btn" onClick={toggle} disabled={busy}>
        Resume
      </button>
    </div>
  );
}

// ---------------------------------------------------------------- helpers

export function Loading({ what }: { what: string }) {
  return <p className="muted">Loading {what}…</p>;
}

export function Failed({ error }: { error: unknown }) {
  return (
    <section className="panel">
      <h2>Could not load this page</h2>
      <p className="muted">{errorText(error)}</p>
    </section>
  );
}

export function NotFound() {
  return (
    <div className="page">
      <h1>Page not found</h1>
      <p>
        <a href="#brief">Go to the morning brief</a>
      </p>
    </div>
  );
}
