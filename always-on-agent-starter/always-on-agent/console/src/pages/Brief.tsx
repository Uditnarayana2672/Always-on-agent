import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, errorText } from "../api";
import {
  ModeBadge,
  PausedBanner,
  Failed,
  Loading,
  ageClass,
  barWidth,
  fmtAge,
  taskHref,
  usePauseToggle,
  useToast,
} from "../components/ui";

export function useUndo() {
  const toast = useToast();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (auditId: number) => api.undo(auditId),
    onSuccess: (result) => {
      toast(result.message);
      void client.invalidateQueries();
    },
    onError: (error) => toast(errorText(error)),
  });
}

export default function Brief() {
  const brief = useQuery({ queryKey: ["brief"], queryFn: api.brief });
  const toast = useToast();
  const client = useQueryClient();
  const { paused, toggle, busy } = usePauseToggle();
  const undo = useUndo();
  const checkin = useMutation({
    mutationFn: api.checkin,
    onSuccess: (result) => {
      toast(result.message);
      void client.invalidateQueries();
    },
    onError: (error) => toast(errorText(error)),
  });

  if (brief.isPending) return <div className="page"><Loading what="the brief" /></div>;
  if (brief.isError) return <div className="page"><Failed error={brief.error} /></div>;
  const b = brief.data;
  const ticks = b.thresholds.filter((h) => h > 0 && h < 24);

  return (
    <div className="page">
      <PausedBanner />
      <div className="page-head">
        <div>
          <div className="eyebrow">{b.day_label}</div>
          <h1>{b.greeting}</h1>
          <p className="lede">
            The agent read your mail, Teams chats, the ServiceNow report and Power BI overnight. Everything below
            links to its source.
          </p>
        </div>
        <div className="row">
          <button className="btn" onClick={() => checkin.mutate()} disabled={checkin.isPending}>
            Run a check-in now
          </button>
          <button className="btn ghost" onClick={toggle} disabled={busy}>
            {paused ? "Resume agent" : "Pause agent"}
          </button>
        </div>
      </div>
      {b.connectors_down.length ? (
        <div className="banner">
          <span>
            <b>Heads up.</b> {b.connectors_down.join(" · ")}
          </span>
          <a className="btn" href="#connectors">
            Connectors
          </a>
        </div>
      ) : null}
      <div className="strip">
        <span>
          Last check-in <b className="mono">{b.last_checkin ?? "—"}</b>
        </span>
        <span>
          Next <b className="mono">{b.next_checkin ?? (paused ? "paused" : "—")}</b>
        </span>
        <span>
          Open tasks <b className="mono">{b.open_tasks}</b>
        </span>
        <span>
          Approvals waiting <b className="mono">{b.approvals}</b>
        </span>
        <span>
          Model <b>{b.model_line.split(",")[0]}</b>
          {b.model_line.includes(",") ? `,${b.model_line.split(",").slice(1).join(",")}` : ""}
        </span>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2>Needs you now</h2>
          <a href="#approvals" className="note">
            Open the approvals queue
          </a>
        </div>
        <div className="list">
          {b.needs.length ? (
            b.needs.map((t, i) => (
              <div className="item" key={t.id}>
                <span className="rank">{i + 1}</span>
                <div>
                  <div className="item-title">
                    <a href={taskHref(t.id)}>
                      <span className="mono">{t.id}</span> · {t.title}
                    </a>
                  </div>
                  <div className="item-meta">{t.reason}</div>
                </div>
                <span className="proj">{t.project}</span>
              </div>
            ))
          ) : (
            <p className="muted">Nothing needs you right now.</p>
          )}
        </div>
      </section>

      <div className="grid2">
        <section className="panel">
          <div className="panel-head">
            <h2>Doxis 24-hour clock</h2>
            <span className="note">Ticks at {joinHours(ticks)} h</span>
          </div>
          <div>
            {b.clocks.length ? (
              b.clocks.map((c) => (
                <a className="clock" href={taskHref(c.id)} key={c.id}>
                  <span className="clock-name">
                    <span className="mono">{c.id}</span> {c.title}
                    {c.paused ? " (on hold)" : ""}
                  </span>
                  <span className={`bar ${ageClass(c.age_hours)}`}>
                    <span style={{ width: barWidth(c.age_hours) }}></span>
                    {ticks.map((h) => (
                      <i key={h} style={{ left: `${((h / 24) * 100).toFixed(1)}%` }}></i>
                    ))}
                  </span>
                  <span className={`age ${ageClass(c.age_hours)}`}>
                    {c.estimated ? "~" : ""}
                    {fmtAge(c.age_hours)}
                  </span>
                </a>
              ))
            ) : (
              <p className="muted">No open Doxis tickets.</p>
            )}
          </div>
        </section>
        <section className="panel">
          <div className="panel-head">
            <h2>Power BI</h2>
            <span className="note">From the latest refresh check</span>
          </div>
          <div className="stat-row">
            {b.powerbi_stats.map((s) => (
              <div className="stat" key={s.label}>
                <b>{s.value}</b>
                <span>{s.label}</span>
              </div>
            ))}
          </div>
          <div className="list">
            {b.powerbi_items.length ? (
              b.powerbi_items.map((p) => (
                <div className="item" key={p.title}>
                  <span className="rank">{p.severity === "alert" ? "!" : "·"}</span>
                  <div>
                    <div className="item-title">
                      {p.task_id ? <a href={taskHref(p.task_id)}>{p.title}</a> : p.title}
                    </div>
                    <div className="item-meta">{p.meta}</div>
                  </div>
                  <span></span>
                </div>
              ))
            ) : (
              <p className="muted">No failed refreshes.</p>
            )}
          </div>
        </section>
      </div>

      <div className="grid2">
        <section className="panel">
          <div className="panel-head">
            <h2>Done on its own since yesterday</h2>
            <a href="#activity" className="note">
              Full activity log
            </a>
          </div>
          <div className="list">
            {b.done.length ? (
              b.done.slice(0, 5).map((d) => (
                <div className="item wide" key={d.audit_id}>
                  <span>
                    <ModeBadge mode="Report" />
                  </span>
                  <div>
                    <div className={d.undo === "done" ? "muted" : ""}>
                      {d.text}
                      {d.undo === "done" ? " · undone" : ""}
                    </div>
                    <div className="item-meta">
                      <span className="mono">{d.time}</span> · {d.ref || "Agent"}
                    </div>
                  </div>
                  {d.undo === "available" ? (
                    <button className="btn ghost" onClick={() => undo.mutate(d.audit_id)} disabled={undo.isPending}>
                      Undo
                    </button>
                  ) : (
                    <span></span>
                  )}
                </div>
              ))
            ) : (
              <p className="muted">Nothing yet. Report actions show here with an undo.</p>
            )}
          </div>
        </section>
        <section className="panel">
          <div className="panel-head">
            <h2>Today's meetings</h2>
            <span className="note">{b.meetings_note ?? "From your calendar"}</span>
          </div>
          <div className="list">
            {b.meetings.length ? (
              b.meetings.map((m) => (
                <div className="item wide" key={`${m.start}-${m.subject}`}>
                  <span className="rank mono">{m.start}</span>
                  <div>
                    <div className="item-title">{m.subject}</div>
                    <div className="item-meta">{m.end ? `${m.start}–${m.end}` : "All day"}</div>
                  </div>
                  {m.join_url ? (
                    <a className="btn ghost" href={m.join_url} target="_blank" rel="noreferrer">
                      Join
                    </a>
                  ) : (
                    <span></span>
                  )}
                </div>
              ))
            ) : (
              <p className="muted">{b.meetings_note ? "Meetings are not shown." : "No meetings today."}</p>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function joinHours(hours: number[]): string {
  if (hours.length <= 1) return hours.join("");
  return `${hours.slice(0, -1).join(", ")} and ${hours[hours.length - 1]}`;
}
