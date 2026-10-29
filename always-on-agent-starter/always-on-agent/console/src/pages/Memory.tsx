import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, errorText } from "../api";
import { Failed, Loading, useToast } from "../components/ui";

const TABS = [
  ["personal", "About you"],
  ["directory", "Site directory"],
  ["lessons", "Lessons"],
  ["sources", "Reference sources"],
  ["tasks", "Task memory"],
] as const;
type Tab = (typeof TABS)[number][0];

export default function Memory() {
  const [tab, setTab] = useState<Tab>("personal");
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <div className="eyebrow">Stored on this laptop</div>
          <h1>Memory</h1>
          <p className="lede">
            The agent rebuilds a small context for every run from what is stored here, instead of carrying a long
            conversation. Everything links back to its source.
          </p>
        </div>
      </div>
      <div className="tabs" role="tablist">
        {TABS.map(([key, label]) => (
          <button className="tab" role="tab" aria-selected={tab === key} key={key} onClick={() => setTab(key)}>
            {label}
          </button>
        ))}
      </div>
      {tab === "tasks" ? <TaskMemory /> : <Section tab={tab} />}
    </div>
  );
}

function Section({ tab }: { tab: Exclude<Tab, "tasks"> }) {
  const data = useQuery({ queryKey: ["memory", tab], queryFn: () => api.memory(tab) });
  const toast = useToast();
  const client = useQueryClient();
  const lesson = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "approve" | "discard" }) => api.lesson(id, action),
    onSuccess: (_, v) => {
      toast(v.action === "approve" ? "Rule saved. It applies from the next run." : "Discarded.");
      void client.invalidateQueries({ queryKey: ["memory", "lessons"] });
    },
    onError: (error) => toast(errorText(error)),
  });
  const reindex = useMutation({
    mutationFn: (source: string) => api.reindex(source),
    onSuccess: (r) => toast(r.message),
    onError: (error) => toast(errorText(error)),
  });

  if (data.isPending) return <Loading what="memory" />;
  if (data.isError) return <Failed error={data.error} />;
  const m = data.data;

  if (tab === "personal")
    return (
      <>
        <div className="grid2">
          <section className="panel">
            <div className="panel-head">
              <h2>profile.md</h2>
              <span className="note">Loaded on every run</span>
            </div>
            <div className="memo">{m.profile || "Empty. Edit memory/profile.md to tell the agent about you."}</div>
          </section>
          <section className="panel">
            <div className="panel-head">
              <h2>preferences.md</h2>
              <span className="note">Loaded on every run</span>
            </div>
            <div className="memo">{m.preferences || "Empty. Edit memory/preferences.md with how you like to work."}</div>
          </section>
        </div>
        <section className="panel">
          <h2>Writing style samples</h2>
          <p className="muted">
            {m.style_samples
              ? `${m.style_samples} of your sent Teams messages and emails, names removed. Used for drafts in your voice.`
              : "No samples yet. Put a few of your sent messages, names removed, in memory/style; drafts then follow your voice."}
          </p>
        </section>
      </>
    );

  if (tab === "directory")
    return (
      <section className="panel">
        <div className="panel-head">
          <h2>KM and LBA per site</h2>
          <span className="note">
            Code looks these up; names never go to the model. Entries older than 90 days need a fresh screenshot.
          </span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Site</th>
                <th>Name</th>
                <th>KM</th>
                <th>LBA</th>
                <th>Last checked</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {m.directory.length ? (
                m.directory.map((d) => (
                  <tr key={d.site}>
                    <td className="mono">{d.site}</td>
                    <td>{d.name}</td>
                    <td>{d.km}</td>
                    <td>{d.lba}</td>
                    <td className="nowrap">
                      {d.last_checked}
                      {d.stale ? (
                        <>
                          {" "}
                          <span className="mode Ask">Stale</span>
                        </>
                      ) : null}
                    </td>
                    <td>{d.source}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6} className="muted">
                    No sites yet. Import the directory (config/directory.example.csv shows the format).
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    );

  if (tab === "lessons") {
    const proposed = m.lessons.filter((l) => l.status === "proposed");
    const saved = m.lessons.filter((l) => l.status === "approved");
    return (
      <>
        <section className="panel">
          <div className="panel-head">
            <h2>Proposed from your corrections</h2>
            <span className="note">Saved only when you approve</span>
          </div>
          <div className="list">
            {proposed.length ? (
              proposed.map((l) => (
                <div className="item" key={l.id}>
                  <span className="rank">→</span>
                  <div>
                    <div className="item-title">{l.text}</div>
                    <div className="item-meta">{l.source}</div>
                  </div>
                  <div className="row">
                    <button className="btn" onClick={() => lesson.mutate({ id: l.id, action: "approve" })} disabled={lesson.isPending}>
                      Save rule
                    </button>
                    <button className="btn ghost" onClick={() => lesson.mutate({ id: l.id, action: "discard" })} disabled={lesson.isPending}>
                      Discard
                    </button>
                  </div>
                </div>
              ))
            ) : (
              <p className="muted">No proposals right now.</p>
            )}
          </div>
        </section>
        <section className="panel">
          <h2>Saved rules</h2>
          <div className="list">
            {saved.length ? (
              saved.map((l) => (
                <div className="item" key={l.id}>
                  <span className="rank">✓</span>
                  <div>
                    <div className="item-title">{l.text}</div>
                    <div className="item-meta">{l.source}</div>
                  </div>
                  <span></span>
                </div>
              ))
            ) : (
              <p className="muted">No saved rules yet.</p>
            )}
          </div>
        </section>
      </>
    );
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Searchable reference material</h2>
        <span className="note">Full-text search in SQLite; no embeddings needed</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Items</th>
              <th>Last indexed</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {m.sources.length ? (
              m.sources.map((s) => (
                <tr key={s.source}>
                  <td>{s.source}</td>
                  <td className="mono">{s.items}</td>
                  <td className="nowrap">{s.last_indexed}</td>
                  <td>
                    <button className="linkbtn" onClick={() => reindex.mutate(s.source)} disabled={reindex.isPending}>
                      Re-index
                    </button>
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={4} className="muted">
                  Nothing indexed yet. Closed tasks are added as precedents automatically.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function TaskMemory() {
  return (
    <section className="panel">
      <h2>How each task remembers</h2>
      <p className="muted">
        Every task has four parts. Each run loads the first two and the last few events, then asks for more only when a
        step needs it.
      </p>
      <div className="grid2">
        <div className="summary">
          <b>Task card</b>
          <br />
          Structured fields: state, people, site, checks, next action, deadline. Always loaded.
        </div>
        <div className="summary">
          <b>Running summary</b>
          <br />
          What has happened so far, rewritten after each update to a fixed length. Always loaded.
        </div>
        <div className="summary">
          <b>Event log</b>
          <br />
          Every email, message, action and approval with source links. Only the last few are loaded.
        </div>
        <div className="summary">
          <b>Attachments</b>
          <br />
          Screenshot text, drafts, files. Loaded only on request. Raw text expires after 14 days.
        </div>
      </div>
      <p className="note">Names are replaced with tokens before any model call. The token map stays on this laptop.</p>
    </section>
  );
}
