"""R06 Nightly digest: what the agent did on its own today (with Undo), what waits on the user.

Every Report action today with its Undo button (already undone ones say so), approvals still open,
hand-offs waiting, and per recipe in shadow how many actions it would have taken. A quiet day still
gets a short digest so the user knows the agent ran.
"""

from agent.channels.cards import DigestItem
from agent.recipes.runner import StepContext
from agent.recipes.views import DigestData

DASH, DOT, ELLIPSIS = chr(0x2013), chr(0xB7), chr(0x2026)  # en dash, middle dot, ellipsis


async def steps(ctx: StepContext) -> None:
    data = DigestData.model_validate(await ctx.read("read_digest_data", {}))
    items = []
    for line in data.done:
        text = line.text + (" (undone)" if line.undo == "done" else "")
        items.append(
            DigestItem(
                text=text,
                audit_id=line.audit_id if line.undo == "available" else None,
                task_id=line.task_id,
            ).model_dump()
        )
    ctx.propose(
        "publish_digest",
        {
            "title": f"Today's digest {DASH} {data.day:%a %d %b}",
            "items": items,
            "open_approvals": len(data.approvals),
            "hand_offs": len(data.hand_offs),
            "shadow_counts": data.shadow,
        },
        purpose="digest",
        idempotency_key=f"digest:{ctx.trigger.get('due_at') or data.day.isoformat()}",
    )
