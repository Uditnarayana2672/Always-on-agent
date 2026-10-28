"""R04 Mention digest: at each check-in, Teams @mentions and one-to-one messages nobody answered yet.

Oldest first, with the chat and the time; nothing is sent when there is nothing open (a check-in with
no news is not a push). Slack is the agent's own channel, so it holds no mentions to digest.
Deleted messages and chats the user has written in since drop out (views.read_mentions).
"""

from agent.recipes.runner import StepContext
from agent.recipes.views import MentionLine

MAX_ITEMS = 10


def lines_by_chat(items: list[MentionLine]) -> list[str]:
    """One line per chat and sender, oldest first: '25 Mar 03:00 UTC Priya Nair wrote to you (2 messages)'."""
    groups: dict[tuple[str, str], list[MentionLine]] = {}
    for item in items:
        groups.setdefault((item.chat_id, item.sender), []).append(item)
    out = []
    for group in sorted(groups.values(), key=lambda g: g[0].at):
        first = group[0]
        kind = "mentioned you" if any(m.mention for m in group) else "wrote to you"
        where = f" in {first.chat_topic}" if first.chat_topic else ""
        count = f" ({len(group)} messages)" if len(group) > 1 else ""
        out.append(f"{first.at:%d %b %H:%M} UTC {first.sender} {kind}{where}{count}")
    return out


async def steps(ctx: StepContext) -> None:
    items = [MentionLine.model_validate(m) for m in await ctx.read("read_mentions", {"hours": 72})]
    if not items:
        ctx.note("no unanswered mentions")
        return
    grouped = lines_by_chat(items)
    lines = grouped[:MAX_ITEMS]
    if len(grouped) > MAX_ITEMS:
        lines.append(f"...and {len(grouped) - MAX_ITEMS} more in the console")
    text = f"{len(items)} unanswered in Teams, oldest first:\n" + "\n".join(f"- {t}" for t in lines)
    newest = max(m.at for m in items)
    ctx.propose(
        "notify_me",
        {"kind": "checkin", "text": text},
        purpose="mention_digest",
        # The same open set is not pushed twice; a new message makes a new digest.
        idempotency_key=f"mentions:{len(items)}:{newest.isoformat()}",
    )
