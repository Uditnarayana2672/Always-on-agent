import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, errorText } from "../api";
import { InlineApproval } from "../components/approval";
import { Failed, Loading, NotFound, PausedBanner, STATE_LABEL, ageClass, fmtAge, useToast } from "../components/ui";

const MARK = { pass: "✓", fail: "✕", unknown: "?" } as const;

export default function Task({ id }: { id: string }) {
  const toast = useToast();
  const client = useQueryClient();
  const detail = useQuery({ queryKey: ["task", id], queryFn: () => api.task(id), retry: false });
  const [text, setText] = useState("");
  const [waitingFrom, setWaitingFrom] = useState<number | null>(null);
  const events = detail.data?.events.length ?? 0;

  useEffect(() => {
    // The agent's answer arrives through the stream; stop showing "working" once the thread grows
    // past the user's own message, or after a minute.
    if (waitingFrom === null) return;
    if (events > waitingFrom + 1) setWaitingFrom(null);
    const timer = setTimeout(() => setWaitingFrom(null), 60_000);
    return () => clearTimeout(timer);
  }, [events, waitingFrom]);

  const send = useMutation({
    mutationFn: (message: string) => api.message(detail.data!.task.id, message),
    onSuccess: (event) => {
      setText("");
      client.setQueryData(["task", id], (old: typeof detail.data) =>
        old ? { ...old, events: [...old.events, event] } : old,
      );
      setWaitingFrom(events);
      void client.invalidateQueries({ queryKey: ["task", id] });
    },
    onError: (error) => toast(errorText(error)),
  });
  const done = useMutation({
    mutationFn: () => api.markDone(detail.data!.task.id),
    onSuccess: (card) => {
      toast(`${card.id} marked done.`);
      void client.invalidateQueries();
    },
    onError: (error) => toast(errorText(error)),
  });

  if (detail.isPending) return <div className="page"><Loading what="the task" /></div>;
  if (detail.isError) {
    if (detail.error instanceof ApiError && detail.error.status === 404) return <NotFound />;
    return <div className="page"><Failed error={detail.error} /></div>;
  }
  const d = detail.data;
  const t = d.task;
  const open = t.column !== "done" && t.column !== "scheduled";
  const submit = () => {
    const message = text.trim();
    if (!message) {
      document.getElementById("msg")?.focus();
      return;
    }
    send.mutate(message);
  };

  return (
    <div className="page">
      <PausedBanner />
      <div>
        <a href="#tasks" className="note">
          ← All tasks
        </a>
      </div>
      <div className="page-head">
        <div>
          <div className="eyebrow">
            {t.project} · {d.kind} · recipe: {d.recipe_label}
          </div>
          <h1>
            <span className="mono" style={{ fontSize: ".8em" }}>
              {t.id}
            </span>{" "}
            {t.title}
          </h1>
          <div className="row" style={{ marginTop: 8 }}>
            <span className={`pill ${t.column}`}>{STATE_LABEL[t.column]}</span>
            {t.clock && t.age_hours !== null ? (
              <span className={`tag ${ageClass(t.age_hours)}`}>Age {fmtAge(t.age_hours)} of 24h</span>
            ) : null}
            <span className="muted" style={{ fontSize: 13 }}>
              Next: {t.next}
            </span>
          </div>
        </div>
        <div className="row">
          {open ? (
            <button className="btn" onClick={() => done.mutate()} disabled={done.isPending}>
              Mark done
            </button>
          ) : null}
        </div>
      </div>
      <div className="detail">
        <section className="panel">
          <div className="panel-head">
            <h2>Thread</h2>
            <span className="note">Talk to this task's agent here. It only sees this task.</span>
          </div>
          <div className="thread" id="thread">
            {d.events.length ? (
              d.events.map((e, i) => (
                <div className="ev" key={i}>
                  <span className="ev-time">{e.time}</span>
                  <div>
                    <div className={`ev-who ${e.who}`}>{e.name}</div>
                    <div className="ev-text">{e.text}</div>
                  </div>
                </div>
              ))
            ) : (
              <p className="muted">No activity yet. This task runs on its schedule.</p>
            )}
            {waitingFrom !== null ? (
              <div className="ev">
                <span className="ev-time"></span>
                <div className="typing">Agent is working on it…</div>
              </div>
            ) : null}
          </div>
          {d.approval ? <InlineApproval a={d.approval} /> : null}
          {t.column !== "done" ? (
            <div className="composer">
              <textarea
                id="msg"
                rows={2}
                aria-label="Message this task's agent"
                placeholder="e.g. check access for the affected user, or draft a shorter version"
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
                }}
              />
              <button className="btn primary" onClick={submit} disabled={send.isPending}>
                Send
              </button>
            </div>
          ) : null}
        </section>
        <aside style={{ display: "flex", flexDirection: "column", gap: 18, minWidth: 0 }}>
          <section className="panel">
            <h2>Task card</h2>
            <dl className="kv">
              <dt>Caller</dt>
              <dd>{d.caller}</dd>
              <dt>Affected person</dt>
              <dd>{d.affected}</dd>
              <dt>Where</dt>
              <dd>{d.site}</dd>
              <dt>Priority</dt>
              <dd>{d.priority}</dd>
            </dl>
          </section>
          <section className="panel">
            <div className="panel-head">
              <h2>Running summary</h2>
              <span className="note">Rewritten after each update</span>
            </div>
            <div className="summary">{d.summary ?? "No summary yet. It is written after the first update."}</div>
          </section>
          {d.checks.length ? (
            <section className="panel">
              <h2>Checks</h2>
              <div className="facts">
                {d.checks.map((f) => (
                  <div className="fact" key={f.label}>
                    <span className={`fact-mark ${f.status}`} aria-label={f.status}>
                      {MARK[f.status]}
                    </span>
                    <div>
                      {f.label}
                      <small>{f.detail}</small>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          ) : null}
          {d.precedents.length ? (
            <section className="panel">
              <div className="panel-head">
                <h2>Similar past tickets</h2>
                <span className="note">From closed tasks</span>
              </div>
              <div className="prec">
                {d.precedents.map((p) => (
                  <div key={p.id}>
                    <span className="tag">{p.id}</span> {p.text}
                  </div>
                ))}
              </div>
            </section>
          ) : null}
        </aside>
      </div>
    </div>
  );
}
