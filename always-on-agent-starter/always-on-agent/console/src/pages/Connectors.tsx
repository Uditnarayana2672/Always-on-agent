import { useQuery } from "@tanstack/react-query";

import { api } from "../api";
import { Failed, Loading } from "../components/ui";

export default function Connectors() {
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <div className="eyebrow">Adapters · one per system</div>
          <h1>Connectors</h1>
          <p className="lede">
            Recipes talk to adapters, not to systems directly. When ServiceNow or Doxis gets an API later, only the
            adapter changes.
          </p>
        </div>
      </div>
      {connectors.isPending ? <Loading what="connectors" /> : null}
      {connectors.isError ? <Failed error={connectors.error} /> : null}
      <div className="grid3">
        {(connectors.data ?? []).map((c) => (
          <section className="panel conn" key={c.name}>
            <div className="spread">
              <h3>{c.name}</h3>
              <span className={`st ${c.status}`}>{c.status_label}</span>
            </div>
            <span className="note">{c.via}</span>
            <ul>
              {c.can.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
            {c.detail ? <span className="err">{c.detail}</span> : null}
            <span className="tag" style={{ whiteSpace: "normal" }}>
              {c.scopes}
            </span>
          </section>
        ))}
      </div>
      <p className="note">Run agentd doctor for a full check with fixes.</p>
    </div>
  );
}
