"""R05 Refresh-failure watch: alert on failed, stuck or upstream-failed refreshes; one safe retry.

Runs on Power BI poller events (refresh, refresh_stuck, dataflow_refresh), task-less unless the dataset
is linked to a task. For a failure:
- known fixes from lessons are quoted in the alert;
- one retry runs on its own (Report) only when the dataset is the user's (powerbi.owned_datasets),
  the error code is a known transient one, no refresh is running, and the agent has not tried today.
  The facts come from code (read_refresh_facts); trigger_refresh's mode_fn and the gate decide again.
  Otherwise the alert says why nothing was retried.
Completed refreshes are ignored here (freshness is R13). Each failure alerts once: the poller's event
id is per request and status, and the alert's idempotency key repeats it.
"""

from typing import Any

from agent.adapters.tools import TRANSIENT_REFRESH_ERRORS
from agent.recipes.runner import StepContext
from agent.recipes.views import RefreshFacts


async def steps(ctx: StepContext) -> None:
    event: dict[str, Any] = dict(ctx.trigger.get("event") or {})
    kind = str(ctx.trigger.get("kind") or "")
    dataset_id = str(event.get("dataset_id") or "")
    if ctx.trigger.get("source") != "powerbi" or not dataset_id:
        return
    status = str(event.get("status") or "")
    if kind == "refresh" and status != "Failed":
        return  # completed or cancelled by someone: nothing to alert
    key = f"refresh-alert:{ctx.trigger.get('external_id') or dataset_id}"

    if kind == "refresh_stuck":
        ctx.propose(
            "notify_me",
            {
                "kind": "alert",
                "text": f"Refresh of {dataset_id} has been running since {event.get('start', '?')[:16]} UTC.",
            },
            purpose="alert",
            idempotency_key=key,
        )
        return
    if kind == "dataflow_refresh":
        name = event.get("dataflow") or event.get("dataflow_id")
        ctx.propose(
            "notify_me",
            {
                "kind": "alert",
                "text": f"Dataflow {name} failed; {dataset_id} will load old data until it runs.",
            },
            purpose="alert",
            idempotency_key=key,
        )
        return

    code = str(event.get("error_code") or "") or None
    fixes = await ctx.read("find_known_fix", {"error_code": code or ""}) if code else []
    facts = RefreshFacts.model_validate(await ctx.read("read_refresh_facts", {"dataset_id": dataset_id}))
    retry, why = decide_retry(facts, code)
    text = f"Refresh of {dataset_id} failed" + (f" ({code})" if code else "") + "."
    if fixes:
        text += f" Known fix: {fixes[0]}"
    text += f" {why}"
    ctx.propose("notify_me", {"kind": "alert", "text": text[:2900]}, purpose="alert", idempotency_key=key)
    if retry:
        ctx.propose(
            "trigger_refresh",
            {"dataset_id": dataset_id},
            purpose="refresh_retry",
            idempotency_key=f"refresh-retry:{dataset_id}:{ctx.trigger.get('external_id')}",
            facts={
                "owned_datasets": [dataset_id] if facts.owned else [],
                "last_error_code": code,
                "agent_retries_today": facts.agent_retries_today,
            },
        )


def decide_retry(facts: RefreshFacts, code: str | None) -> tuple[bool, str]:
    if code not in TRANSIENT_REFRESH_ERRORS:
        return False, "Not a known temporary error, so nothing was retried."
    if not facts.owned:
        return False, "Not your dataset, so nothing was retried."
    if facts.running:
        return False, "A refresh is already running."
    if facts.agent_retries_today:
        return False, "Already retried once today; check it by hand."
    return True, "Retrying once."
