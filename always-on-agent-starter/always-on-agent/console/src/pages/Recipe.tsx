import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, errorText, type Output, type Recipe as RecipeT, type RecipeIn, type RecipeTest, type Tool } from "../api";
import { Failed, Loading, ModeBadge, NotFound, useToast } from "../components/ui";

const OUTPUTS: { value: Output; label: string }[] = [
  { value: "none", label: "Task thread" },
  { value: "dm", label: "Main DM" },
  { value: "approval_card", label: "Approval card" },
  { value: "report", label: "Brief section" },
  { value: "digest", label: "Nightly digest" },
];
const MODELS: RecipeIn["model"][] = ["Haiku", "Sonnet", "Opus"];

export const slugOf = (name: string) =>
  name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 48);

const BLANK: RecipeIn = {
  name: "",
  project: "Doxis",
  trigger: "poll: mail, every: 10m",
  model: "Haiku",
  status: "Shadow",
  tools: ["read_mail"],
  instructions: "",
  output: "none",
};

export function yamlOf(slug: string, r: RecipeIn, tools: Tool[]): string {
  const mode = (name: string) => tools.find((t) => t.name === name)?.mode ?? "Ask";
  const words = (r.instructions || "").split(/\s+/);
  const lines: string[] = [];
  let cur = "";
  words.forEach((w) => {
    if (`${cur} ${w}`.trim().length > 64) {
      lines.push(cur.trim());
      cur = w;
    } else cur += ` ${w}`;
  });
  if (cur.trim()) lines.push(cur.trim());
  const project = { Core: "general", Doxis: "doxis", "Power BI": "powerbi" }[r.project];
  return [
    `name: ${slug || "new-recipe"}`,
    `title: ${r.name || "Untitled recipe"}`,
    `project: ${project}`,
    `status: ${(r.status ?? "Shadow").toLowerCase()}`,
    `trigger: {${r.trigger}}`,
    `model: ${r.model.toLowerCase()}`,
    "tools:",
    ...(r.tools.length ? r.tools.map((t) => `  - ${t}   # ${mode(t)}`) : ["  []"]),
    "instructions: >",
    ...lines.map((l) => `  ${l}`),
    `output: ${r.output}`,
  ].join("\n");
}

export default function Recipe({ slug }: { slug: string }) {
  const isNew = slug === "new";
  const recipe = useQuery({
    queryKey: ["recipe", slug],
    queryFn: () => api.recipe(slug),
    enabled: !isNew,
    retry: false,
  });
  const tools = useQuery({ queryKey: ["tools"], queryFn: api.tools });
  if (!isNew && recipe.isPending) return <div className="page"><Loading what="the recipe" /></div>;
  if (!isNew && recipe.isError) {
    if (recipe.error instanceof ApiError && recipe.error.status === 404) return <NotFound />;
    return <div className="page"><Failed error={recipe.error} /></div>;
  }
  return <Editor key={slug} slug={isNew ? "" : slug} base={recipe.data} tools={tools.data?.tools ?? []} />;
}

function Editor({ slug, base, tools }: { slug: string; base?: RecipeT; tools: Tool[] }) {
  const toast = useToast();
  const client = useQueryClient();
  const [draft, setDraft] = useState<RecipeIn>(() =>
    base
      ? {
          name: base.name,
          project: base.project,
          trigger: base.trigger,
          model: (["Haiku", "Sonnet", "Opus", "None"].includes(base.model) ? base.model : "Haiku") as RecipeIn["model"],
          status: base.status,
          tools: base.tools,
          instructions: base.instructions,
          output: base.output,
        }
      : BLANK,
  );
  const [test, setTest] = useState<RecipeTest | null>(null);
  useEffect(() => setTest(null), [slug]);
  const target = slug || slugOf(draft.name);
  const set = <K extends keyof RecipeIn>(key: K, value: RecipeIn[K]) => setDraft((d) => ({ ...d, [key]: value }));
  const toggleTool = (name: string, on: boolean) =>
    set("tools", on ? [...draft.tools, name] : draft.tools.filter((t) => t !== name));

  const save = useMutation({
    mutationFn: () => api.saveRecipe(target, draft),
    onSuccess: (r) => {
      toast(
        slug
          ? "Saved. Changes apply from the next run."
          : "Saved in Shadow mode. Switch it to Live on the recipes page when it looks right.",
      );
      void client.invalidateQueries({ queryKey: ["recipes"] });
      void client.invalidateQueries({ queryKey: ["recipe", r.slug] });
      location.hash = "#recipes";
    },
    onError: (error) => toast(errorText(error)),
  });
  const run = useMutation({
    mutationFn: () => api.testRecipe(slug),
    onSuccess: setTest,
    onError: (error) => toast(errorText(error)),
  });

  const onSave = () => {
    if (!draft.name.trim()) {
      toast("Give the recipe a name first.");
      document.getElementById("f-name")?.focus();
      return;
    }
    if (!target) {
      toast("Use letters or digits in the name.");
      return;
    }
    save.mutate();
  };
  const onTest = () => {
    if (!slug) {
      toast("Save the recipe first; the test runs the saved file on sample data.");
      return;
    }
    run.mutate();
  };

  return (
    <div className="page">
      <div>
        <a href="#recipes" className="note">
          ← All recipes
        </a>
      </div>
      <div className="page-head">
        <div>
          <div className="eyebrow">{slug ? "Edit recipe" : "New recipe"}</div>
          <h1>{base?.name || draft.name || "Untitled recipe"}</h1>
          <p className="lede">
            Tool modes are fixed by the connector. A recipe can use a tool, but can never make it act with less
            approval than its mode.
          </p>
        </div>
        <div className="row">
          <button className="btn" onClick={onTest} disabled={run.isPending}>
            Test on a sample
          </button>
          <button className="btn primary" onClick={onSave} disabled={save.isPending}>
            {slug ? "Save changes" : "Save in Shadow mode"}
          </button>
        </div>
      </div>
      {base?.error ? (
        <div className="banner">
          <span>
            <b>This recipe is turned off.</b> {base.error}
          </span>
        </div>
      ) : null}
      <div className="editor">
        <section className="panel">
          <div className="form">
            <label htmlFor="f-name">Name</label>
            <input
              id="f-name"
              type="text"
              value={draft.name}
              placeholder="e.g. Weekly ticket digest"
              onChange={(e) => set("name", e.target.value)}
            />
            <label htmlFor="f-project">Project</label>
            <select id="f-project" value={draft.project} onChange={(e) => set("project", e.target.value as RecipeIn["project"])}>
              {(["Core", "Doxis", "Power BI"] as const).map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
            <label htmlFor="f-trigger">
              Trigger<small>Schedule, poll or event</small>
            </label>
            <input id="f-trigger" type="text" value={draft.trigger} onChange={(e) => set("trigger", e.target.value)} />
            <label htmlFor="f-model">
              Model<small>Haiku is free and the default</small>
            </label>
            <select id="f-model" value={draft.model} onChange={(e) => set("model", e.target.value as RecipeIn["model"])}>
              {MODELS.map((m) => (
                <option key={m}>{m}</option>
              ))}
            </select>
            <label>
              Tools<small>Mode shown beside each</small>
            </label>
            <div className="tools">
              {tools.map((t) => (
                <div className="tool" key={t.name}>
                  <label>
                    <input
                      type="checkbox"
                      data-tool={t.name}
                      checked={draft.tools.includes(t.name)}
                      disabled={t.locked}
                      onChange={(e) => toggleTool(t.name, e.target.checked)}
                    />
                    {t.name}
                  </label>
                  <ModeBadge mode={t.mode} />
                </div>
              ))}
            </div>
            <label htmlFor="f-ins">
              Instructions<small>Plain language, like a note to a colleague</small>
            </label>
            <textarea
              id="f-ins"
              placeholder="What should it do each time it runs?"
              value={draft.instructions}
              onChange={(e) => set("instructions", e.target.value)}
            />
            <label htmlFor="f-out">Output</label>
            <select id="f-out" value={draft.output} onChange={(e) => set("output", e.target.value as Output)}>
              {OUTPUTS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        </section>
        <aside className="sticky" style={{ display: "flex", flexDirection: "column", gap: 18, minWidth: 0 }}>
          <section className="panel">
            <div className="panel-head">
              <h2>Recipe file</h2>
              <span className="note">recipes/{target || "new-recipe"}.yaml</span>
            </div>
            <pre className="yaml" id="yaml">
              {yamlOf(target, draft, tools)}
            </pre>
          </section>
          {test ? (
            <section className="panel" id="testOut">
              <h2>Test run on a sample</h2>
              <div>
                <p className="muted" style={{ fontSize: 13 }}>
                  Sample: {test.sample}. Model: {draft.model}.
                </p>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {test.proposals.length ? (
                    test.proposals.map((p, i) => (
                      <div className="row" style={{ justifyContent: "space-between" }} key={i}>
                        <span className="mono" style={{ fontSize: 12 }}>
                          {p.tool}
                        </span>
                        <ModeBadge mode={p.mode} />
                      </div>
                    ))
                  ) : (
                    <span className="muted">It would not do anything on this sample.</span>
                  )}
                </div>
                {test.notes.map((n) => (
                  <p className="note" key={n}>
                    {n}
                  </p>
                ))}
                <p className="note">
                  {test.needs_approval ? "You would get an approval card." : "No approval needed."}
                </p>
              </div>
            </section>
          ) : null}
        </aside>
      </div>
    </div>
  );
}
