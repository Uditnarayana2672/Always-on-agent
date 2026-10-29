import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../api";
import { Failed, Loading, ModeBadge } from "../components/ui";
import { useUndo } from "./Brief";

const MODES = ["All", "Silent", "Report", "Ask", "Hand off"];

export default function Activity() {
  const [mode, setMode] = useState("All");
  const rows = useQuery({ queryKey: ["activity", mode], queryFn: () => api.activity(mode) });
  const workers = useQuery({ queryKey: ["workers"], queryFn: api.workers, refetchInterval: 5000 });
  const undo = useUndo();
  const w = workers.data;

  return (
    <div className="page" style={{ maxWidth: "none" }}>
      <div className="page-head">
        <div>
          <div className="eyebrow">Append-only audit log</div>
          <h1>Activity</h1>
          <p className="lede">
            Every tool call the agent makes, with its mode and result. Report actions can be undone from here.
          </p>
        </div>
        <div className="chips" role="group" aria-label="Mode">
          {MODES.map((m) => (
            <button className="chip" aria-pressed={mode === m} key={m} onClick={() => setMode(m)}>
              {m}
            </button>
          ))}
        </div>
      </div>
      <section className="panel">
        <div className="panel-head">
          <h2>Workers right now</h2>
          <span className="note">
            One coordinator, a pool of {w?.workers.length ?? 4} short-lived task workers, one worker per task at a time
          </span>
        </div>
        <div className="workers">
          <div className="worker busy">
            <b>Coordinator</b>
            <span>{w?.coordinator ?? "…"}</span>
            <span className="note">Queue: {w?.queue ?? 0} jobs</span>
          </div>
          {(w?.workers ?? []).map((x) => (
            <div className={`worker${x.busy ? " busy" : ""}`} key={x.name}>
              <b>{x.name}</b>
              <span>{x.doing}</span>
              <span className="note">{x.note}</span>
            </div>
          ))}
        </div>
      </section>
      {rows.isPending ? <Loading what="the activity log" /> : null}
      {rows.isError ? <Failed error={rows.error} /> : null}
      {rows.data ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Task or recipe</th>
                <th>What it did</th>
                <th>Tool</th>
                <th>Mode</th>
                <th>Result</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.data.length ? (
                rows.data.map((a) => (
                  <tr className={a.undo === "done" ? "struck" : ""} key={a.audit_id}>
                    <td className="mono nowrap">{a.time}</td>
                    <td className="nowrap">{a.ref}</td>
                    <td>{a.action}</td>
                    <td className="mono">{a.tool}</td>
                    <td>
                      <ModeBadge mode={a.mode} />
                    </td>
                    <td>{a.undo === "done" ? "Undone" : a.result}</td>
                    <td className="nowrap">
                      {a.undo === "available" ? (
                        <button className="linkbtn" onClick={() => undo.mutate(a.audit_id)} disabled={undo.isPending}>
                          Undo
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={7} className="muted">
                    Nothing logged{mode === "All" ? " yet" : ` in ${mode} mode`}.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
