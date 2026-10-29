import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, errorText, type Mode, type Settings, type SettingsIn } from "../api";
import { Failed, Loading, ModeBadge, PausedBanner, usePauseToggle, useToast } from "../components/ui";

const INTERVALS: [number, string][] = [
  [60, "Every hour"],
  [120, "Every 2 hours"],
  [240, "Every 4 hours"],
];
const SONNET: [string, string][] = [
  ["caller_drafts", "Caller-facing drafts"],
  ["long_transcripts", "Meeting transcripts over 30 min"],
  ["weekly_reports", "Weekly reports"],
];
const LEVELS: [string, string][] = [
  ["metadata", "Metadata"],
  ["text", "Ticket and email text (pseudonymised)"],
  ["chat", "Chat text and meeting transcripts"],
  ["images", "Screenshots as images"],
];

export default function Prefs() {
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  if (settings.isPending) return <div className="page"><Loading what="settings" /></div>;
  if (settings.isError) return <div className="page"><Failed error={settings.error} /></div>;
  return <Form s={settings.data} />;
}

function Form({ s }: { s: Settings }) {
  const toast = useToast();
  const client = useQueryClient();
  const { paused, toggle, busy } = usePauseToggle();
  const [resetAsk, setResetAsk] = useState(false);
  const save = useMutation({
    mutationFn: (body: SettingsIn) => api.saveSettings(body),
    onSuccess: (data) => {
      client.setQueryData(["settings"], data);
      toast("Saved. The agent uses it from now on.");
      void client.invalidateQueries({ queryKey: ["brief"] });
    },
    onError: (error) => {
      toast(errorText(error));
      void client.invalidateQueries({ queryKey: ["settings"] });
    },
  });
  const reset = useMutation({
    mutationFn: async () => api.reset((await api.resetRequest()).token),
    onSuccess: (r) => {
      setResetAsk(false);
      toast(r.message);
      void client.invalidateQueries();
    },
    onError: (error) => toast(errorText(error)),
  });
  const level = LEVELS.findIndex(([k]) => k === s.data_rule);
  const blurSave = (key: keyof SettingsIn, current: string | number) => (value: string) => {
    if (value === "" || String(current) === value) return;
    save.mutate({ [key]: typeof current === "number" ? Number(value) : value } as SettingsIn);
  };
  const askReset = () => {
    if (!paused) {
      toast("Pause the agent before a reset.");
      return;
    }
    setResetAsk(true);
  };

  return (
    <div className="page" style={{ maxWidth: 980 }}>
      <PausedBanner />
      <div className="page-head">
        <div>
          <div className="eyebrow">How the agent behaves</div>
          <h1>Settings</h1>
        </div>
      </div>
      <section className="panel">
        <h2>Schedule</h2>
        <div className="form">
          <label htmlFor="p-hours">Working hours</label>
          <div className="row">
            <input id="p-hours" type="time" defaultValue={s.work_start} style={{ width: "auto" }} onBlur={(e) => blurSave("work_start", s.work_start)(e.target.value)} />{" "}
            to{" "}
            <input id="p-hours-end" type="time" defaultValue={s.work_end} style={{ width: "auto" }} aria-label="End of working hours" onBlur={(e) => blurSave("work_end", s.work_end)(e.target.value)} />
          </div>
          <label htmlFor="p-brief">Morning brief</label>
          <input id="p-brief" type="time" defaultValue={s.brief} style={{ width: "auto" }} onBlur={(e) => blurSave("brief", s.brief)(e.target.value)} />
          <label htmlFor="p-interval">
            Check-ins<small>Polling for new items runs every 5–10 min regardless</small>
          </label>
          <select
            id="p-interval"
            style={{ width: "auto" }}
            value={s.checkin_every_minutes}
            onChange={(e) => save.mutate({ checkin_every_minutes: Number(e.target.value) })}
          >
            {INTERVALS.some(([m]) => m === s.checkin_every_minutes) ? null : (
              <option value={s.checkin_every_minutes}>Every {s.checkin_every_minutes} min</option>
            )}
            {INTERVALS.map(([m, label]) => (
              <option value={m} key={m}>
                {label}
              </option>
            ))}
          </select>
          <label htmlFor="p-push">
            Pushes per day<small>Extra items wait for the next check-in</small>
          </label>
          <input id="p-push" type="number" defaultValue={s.max_pushes_per_day} min={1} max={20} style={{ width: 90 }} onBlur={(e) => blurSave("max_pushes_per_day", s.max_pushes_per_day)(e.target.value)} />
        </div>
      </section>
      <section className="panel">
        <h2>Models</h2>
        <div className="form">
          <label>Default</label>
          <div>
            <b>{s.default_model}</b> <span className="muted">for every run. Free through ICA.</span>
          </div>
          <label>
            Use Sonnet for<small>Off unless ticked</small>
          </label>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {SONNET.map(([key, label]) => (
              <label className="switch" key={key}>
                <input
                  type="checkbox"
                  checked={s.sonnet_for.includes(key)}
                  onChange={(e) =>
                    save.mutate({
                      sonnet_for: e.target.checked ? [...s.sonnet_for, key] : s.sonnet_for.filter((x) => x !== key),
                    })
                  }
                />{" "}
                {label}
              </label>
            ))}
          </div>
          <label htmlFor="p-budget">Daily Sonnet limit</label>
          <div className="row">
            <input id="p-budget" type="number" defaultValue={s.sonnet_daily_limit} min={0} style={{ width: 90 }} onBlur={(e) => blurSave("sonnet_daily_limit", s.sonnet_daily_limit)(e.target.value)} />{" "}
            <span className="muted">runs a day, then Haiku only</span>
          </div>
        </div>
      </section>
      <section className="panel">
        <div className="panel-head">
          <h2>What may go to the model</h2>
          <span className={`st ${s.data_rule_approved ? "ok" : "pend"}`}>
            {s.data_rule_approved ? "Approved in writing" : "Waiting for written approval"}
          </span>
        </div>
        <p className="muted">
          {s.data_rule_approved
            ? "The client approved the data rule. It changes only in config/config.yaml, never from here. Names are always replaced with tokens."
            : "Until your delivery lead confirms the data rule with the client, only metadata goes to ICA: ticket numbers, report names, refresh times, counts. Names are always replaced with tokens."}
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {LEVELS.map(([key, label], i) => (
            <label className="switch" key={key}>
              <input type="checkbox" checked={i <= level} disabled readOnly /> {label}
            </label>
          ))}
        </div>
      </section>
      <section className="panel">
        <div className="panel-head">
          <h2>Action policy</h2>
          <span className="note">Unknown actions default to Ask. Hand off rows can't be changed.</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Action</th>
                <th>Mode</th>
              </tr>
            </thead>
            <tbody>
              {s.policy.map((p) => (
                <tr key={p.key}>
                  <td>{p.label}</td>
                  <td>
                    {p.locked ? (
                      <ModeBadge mode="Hand off" />
                    ) : (
                      <select
                        className="field"
                        style={{ width: "auto" }}
                        id={`pol-${p.key}`}
                        aria-label={`Mode for ${p.label}`}
                        value={p.mode}
                        disabled={p.options.length < 2 || save.isPending}
                        title={p.reaches_people ? "Anything other people see always asks first." : undefined}
                        onChange={(e) => save.mutate({ policy: { [p.key]: e.target.value as Mode } })}
                      >
                        {(["Silent", "Report", "Ask"] as Mode[])
                          .filter((m) => p.options.includes(m) || m === p.mode)
                          .map((m) => (
                            <option key={m}>{m}</option>
                          ))}
                      </select>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="note">
          After you approve the same kind of action unchanged several times, the agent suggests moving it to Report.
          You decide.
        </p>
      </section>
      <section className="panel">
        <h2>Stop or start over</h2>
        <div className="row">
          <button className="btn" onClick={toggle} disabled={busy}>
            {paused ? "Resume agent" : "Pause agent"}
          </button>
          <button className="btn danger" onClick={askReset}>
            Reset memory and tasks
          </button>
        </div>
        {resetAsk ? (
          <div className="confirm">
            <span>This deletes all task memory, lessons and schedules on this laptop. The audit log is kept.</span>
            <button className="btn danger" onClick={() => reset.mutate()} disabled={reset.isPending}>
              Delete and reset
            </button>
            <button className="btn ghost" onClick={() => setResetAsk(false)}>
              Cancel
            </button>
          </div>
        ) : null}
      </section>
    </div>
  );
}
