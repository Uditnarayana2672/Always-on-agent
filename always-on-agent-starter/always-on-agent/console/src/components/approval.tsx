// An approval card: the exact text that will be sent, bound to its content hash. Approve sends the
// hash the user saw; an edit returns a new hash and needs its own approval.
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, errorText, type Approval } from "../api";
import { ModeBadge, taskHref, useStatus, useToast } from "./ui";

export function useDecisions() {
  const client = useQueryClient();
  const toast = useToast();
  const status = useStatus();
  const refresh = () => client.invalidateQueries();
  const fail = (error: unknown) => {
    toast(errorText(error));
    void refresh();
  };
  const approve = useMutation({
    mutationFn: (a: Approval) => api.approve(a.id, a.content_hash),
    onSuccess: (decision, a) => {
      if (!decision.changed) toast(decision.message);
      else if (status.data?.paused) toast("Approved. It runs when you resume the agent.");
      else toast(`Approved. Sending to ${a.to}.`);
      void refresh();
    },
    onError: fail,
  });
  const reject = useMutation({
    mutationFn: (a: Approval) => api.reject(a.id),
    onSuccess: () => {
      toast("Rejected. Nothing was sent.");
      void refresh();
    },
    onError: fail,
  });
  const edit = useMutation({
    mutationFn: ({ a, text }: { a: Approval; text: string }) => api.edit(a.id, text, a.content_hash),
    onSuccess: (decision) => {
      toast(decision.changed ? "Edit saved. Approve the edited text to send it." : "No change.");
      void refresh();
    },
    onError: fail,
  });
  const busy = approve.isPending || reject.isPending || edit.isPending;
  return { approve, reject, edit, busy };
}

/** The queue card on the Approvals page (with the Edit box). */
export function ApprovalArticle({ a }: { a: Approval }) {
  const { approve, reject, edit, busy } = useDecisions();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(a.text);
  const startEdit = () => {
    setText(a.text);
    setEditing(true);
  };
  const save = () => {
    if (text.trim() === a.text.trim()) {
      setEditing(false);
      return;
    }
    edit.mutate({ a, text }, { onSuccess: () => setEditing(false) });
  };
  return (
    <article className={`approval${a.edited ? " edited" : ""}`} data-testid={`approval-${a.id}`}>
      <div className="spread">
        <div className="row">
          <ModeBadge mode="Ask" />
          <b>{a.kind}</b>
          <span className="muted">to {a.to}</span>
        </div>
        <span className={`note${a.urgent ? " warn" : ""}`}>{a.expires_label}</span>
      </div>
      <div className="note">
        {a.task_id ? (
          <a href={taskHref(a.task_id)}>
            <span className="mono">{a.task_id}</span>
            {a.task_title ? ` · ${a.task_title}` : ""}
          </a>
        ) : (
          "No task"
        )}
        {` · via ${a.via}`}
        {a.recipe ? ` · recipe: ${a.recipe}` : ""}
      </div>
      {editing ? (
        <>
          <textarea
            className="draft-edit"
            id={`edit-${a.id}`}
            aria-label="Edit the text"
            value={text}
            autoFocus
            onChange={(e) => setText(e.target.value)}
          />
          <div className="row">
            <button className="btn primary" onClick={save} disabled={busy || !text.trim()}>
              Save edit
            </button>
            <button className="btn ghost" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="draft">{a.text}</div>
          {a.note ? <div className="note">{a.note}</div> : null}
          {a.edited ? <div className="note">You edited this text. Approve the edited version to send it.</div> : null}
          <div className="row">
            <button className="btn primary" onClick={() => approve.mutate(a)} disabled={busy}>
              {a.edited ? "Approve edited text" : "Approve"}
            </button>
            <button className="btn" onClick={startEdit} disabled={busy}>
              Edit
            </button>
            <button className="btn ghost" onClick={() => reject.mutate(a)} disabled={busy}>
              Reject
            </button>
          </div>
        </>
      )}
    </article>
  );
}

/** The inline card inside a task's thread. */
export function InlineApproval({ a }: { a: Approval }) {
  const { approve, reject, busy } = useDecisions();
  return (
    <div className={`approval${a.edited ? " edited" : ""}`}>
      <div className="spread">
        <span>
          <ModeBadge mode="Ask" />{" "}
          <b style={{ marginLeft: 6 }}>
            {a.kind} to {a.to}
          </b>
        </span>
        <span className="note">{a.expires_label}</span>
      </div>
      <div className="draft">{a.text}</div>
      {a.note ? <div className="note">{a.note}</div> : null}
      <div className="row">
        <button className="btn primary" onClick={() => approve.mutate(a)} disabled={busy}>
          {a.edited ? "Approve edited text" : "Approve"}
        </button>
        <a className="btn" href="#approvals">
          Edit in approvals
        </a>
        <button className="btn ghost" onClick={() => reject.mutate(a)} disabled={busy}>
          Reject
        </button>
      </div>
    </div>
  );
}
