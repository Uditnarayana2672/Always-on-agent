import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, type Column } from "../api";
import {
  Failed,
  Loading,
  ModeBadge,
  PausedBanner,
  STATE_LABEL,
  ageClass,
  barWidth,
  fmtAge,
  taskHref,
} from "../components/ui";

const COLUMNS: Column[] = ["needs", "progress", "waiting", "scheduled", "done"];
const PROJECTS = ["All", "Doxis", "Power BI", "Core"];

export default function Tasks() {
  const [project, setProject] = useState(() => readFilter());
  const tasks = useQuery({ queryKey: ["tasks"], queryFn: () => api.tasks() });
  const choose = (p: string) => {
    setProject(p);
    try {
      localStorage.setItem("tasks.project", p);
    } catch {
      /* private window */
    }
  };
  const list = (tasks.data ?? []).filter((t) => project === "All" || t.project === project);

  return (
    <div className="page" style={{ maxWidth: "none" }}>
      <PausedBanner />
      <div className="page-head">
        <div>
          <div className="eyebrow">One thread per ticket or request</div>
          <h1>Tasks</h1>
          <p className="lede">
            Each card is a task with its own memory and thread. A short-lived worker picks it up whenever something
            happens on it. Only you can mark a task done.
          </p>
        </div>
        <div className="chips" role="group" aria-label="Project">
          {PROJECTS.filter((p) => p !== "Core" || (tasks.data ?? []).some((t) => t.project === "Core")).map((p) => (
            <button className="chip" aria-pressed={project === p} key={p} onClick={() => choose(p)}>
              {p}
            </button>
          ))}
        </div>
      </div>
      {tasks.isPending ? <Loading what="tasks" /> : null}
      {tasks.isError ? <Failed error={tasks.error} /> : null}
      {tasks.data ? (
        <div className="board-wrap">
          <div className="board">
            {COLUMNS.map((c) => {
              const items = list.filter((t) => t.column === c);
              return (
                <div className="col" key={c} data-testid={`col-${c}`}>
                  <div className="col-head">
                    <span>{STATE_LABEL[c]}</span>
                    <span className="mono muted">{items.length}</span>
                  </div>
                  {items.length ? (
                    items.map((t) => (
                      <a className="card" href={taskHref(t.id)} key={t.id}>
                        <div className="spread">
                          <span className="mono" style={{ fontSize: 12 }}>
                            {t.id}
                          </span>
                          <span className="proj">{t.project}</span>
                        </div>
                        <div className="card-title">{t.title}</div>
                        {t.clock && t.age_hours !== null ? (
                          <div className={`bar ${ageClass(t.age_hours)}`}>
                            <span style={{ width: barWidth(t.age_hours) }}></span>
                          </div>
                        ) : null}
                        <div className="spread">
                          <span className="next">{t.next}</span>
                          {t.clock && t.age_hours !== null ? (
                            <span className={`age ${ageClass(t.age_hours)}`}>{fmtAge(t.age_hours)}</span>
                          ) : null}
                        </div>
                        {t.has_approval ? <ModeBadge mode="Ask" /> : null}
                      </a>
                    ))
                  ) : (
                    <div className="empty">Nothing here</div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function readFilter(): string {
  try {
    const saved = localStorage.getItem("tasks.project");
    return saved && PROJECTS.includes(saved) ? saved : "All";
  } catch {
    return "All";
  }
}
