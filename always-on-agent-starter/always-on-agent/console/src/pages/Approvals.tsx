import { useQuery } from "@tanstack/react-query";

import { api } from "../api";
import { ApprovalArticle } from "../components/approval";
import { Failed, Loading, PausedBanner } from "../components/ui";

export default function Approvals() {
  const approvals = useQuery({ queryKey: ["approvals"], queryFn: api.approvals });
  return (
    <div className="page" style={{ maxWidth: 900 }}>
      <PausedBanner />
      <div className="page-head">
        <div>
          <div className="eyebrow">Ask mode · nothing here is sent until you approve</div>
          <h1>Approvals</h1>
          <p className="lede">
            An approval covers exactly the text shown. If you edit it, the edited version needs its own approval.
            Approvals expire after 24 hours and go back to the task.
          </p>
        </div>
      </div>
      {approvals.isPending ? <Loading what="approvals" /> : null}
      {approvals.isError ? <Failed error={approvals.error} /> : null}
      {approvals.data?.length
        ? approvals.data.map((a) => <ApprovalArticle a={a} key={`${a.id}:${a.content_hash}`} />)
        : null}
      {approvals.data && !approvals.data.length ? (
        <section className="panel">
          <h2>All clear</h2>
          <p className="muted">No approvals waiting. New ones also arrive as cards in your Slack and Teams DM.</p>
        </section>
      ) : null}
    </div>
  );
}
