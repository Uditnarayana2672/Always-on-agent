import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, errorText, type Recipe } from "../api";
import { Failed, Loading, useToast } from "../components/ui";

const GROUPS: { project: Recipe["project"]; title: string }[] = [
  { project: "Core", title: "Phase 1 skills" },
  { project: "Doxis", title: "Doxis recipes" },
  { project: "Power BI", title: "Power BI recipes" },
];

export default function Recipes() {
  const recipes = useQuery({ queryKey: ["recipes"], queryFn: api.recipes });
  const toast = useToast();
  const client = useQueryClient();
  const status = useMutation({
    mutationFn: ({ slug, value }: { slug: string; value: Recipe["status"] }) => api.recipeStatus(slug, value),
    onSuccess: (r) => {
      toast(`${r.name} is now ${r.status}.`);
      void client.invalidateQueries({ queryKey: ["recipes"] });
    },
    onError: (error) => {
      toast(errorText(error));
      void client.invalidateQueries({ queryKey: ["recipes"] });
    },
  });

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <div className="eyebrow">Tasks are configuration, not code</div>
          <h1>Recipes</h1>
          <p className="lede">
            Each recipe says when to run, which tools it may use and where its output goes. New recipes start in
            Shadow mode: they draft and log, but never act, until you switch them to Live.
          </p>
        </div>
        <a className="btn primary" href="#recipe-new">
          New recipe
        </a>
      </div>
      {recipes.isPending ? <Loading what="recipes" /> : null}
      {recipes.isError ? <Failed error={recipes.error} /> : null}
      {recipes.data
        ? GROUPS.map((g) => {
            const rows = recipes.data.filter((r) => r.project === g.project);
            if (!rows.length && g.project !== "Core") return null;
            return (
              <section className="panel" key={g.project}>
                <div className="panel-head">
                  <h2>{g.title}</h2>
                  <span className="note">{rows.filter((r) => r.status === "Live").length} live</span>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Recipe</th>
                        <th>Trigger</th>
                        <th>Model</th>
                        <th>Last run</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((r) => (
                        <tr key={r.slug}>
                          <td>
                            <a href={`#recipe-${r.slug}`}>
                              <b>{r.name}</b>
                            </a>
                            {r.error ? <div className="err">Turned off: {r.error}</div> : null}
                          </td>
                          <td>{r.trigger_label}</td>
                          <td>{r.model}</td>
                          <td className="mono nowrap">{r.last_run}</td>
                          <td>
                            <select
                              className="field"
                              style={{ width: "auto" }}
                              aria-label={`Status of ${r.name}`}
                              value={r.status}
                              disabled={status.isPending}
                              onChange={(e) =>
                                status.mutate({ slug: r.slug, value: e.target.value as Recipe["status"] })
                              }
                            >
                              {(["Live", "Shadow", "Off"] as const).map((s) => (
                                <option key={s}>{s}</option>
                              ))}
                            </select>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            );
          })
        : null}
    </div>
  );
}
