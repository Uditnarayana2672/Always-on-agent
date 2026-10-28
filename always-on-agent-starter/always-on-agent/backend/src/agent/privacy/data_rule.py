"""Data-rule levels and the context-packet reducer (F11, E12).

Levels are cumulative: metadata < text < chat < images. At each level the reducer keeps what the level
allows and strips the rest, noting what it left out so the step can run in reduced form:

- metadata: ids, report names, times, counts. No subjects (they carry names and case detail), no
  bodies, no images.
- text: plus pseudonymised ticket and mail text (and OCR text of screenshots).
- chat: plus chat messages and transcripts.
- images: plus the screenshots themselves.
"""

from typing import Any

from pydantic import BaseModel, Field

from agent.privacy.hook import LEVEL_ORDER, DataClass

# Metadata keys that carry free text; dropped below the level the item's text needs.
TEXT_META_KEYS = frozenset({"subject", "body", "text", "preview", "snippet", "body_preview", "topic"})
TASK_TEXT_KEYS = frozenset({"title", "summary", "next_action", "description"})


def allowed(level: DataClass, data_class: DataClass) -> bool:
    return LEVEL_ORDER.index(data_class) <= LEVEL_ORDER.index(level)


class PacketItem(BaseModel):
    data_class: DataClass  # metadata | text (mail, ticket) | chat (chat, transcript) | images (screenshot)
    kind: str  # mail, ticket, chat_message, transcript, screenshot, refresh ...
    meta: dict[str, Any] = Field(default_factory=dict)
    subject: str | None = None
    text: str | None = None  # for screenshots: the OCR text, read locally
    image_refs: list[str] = Field(default_factory=list)


class ContextPacket(BaseModel):
    task: dict[str, Any] = Field(default_factory=dict)  # card fields: ids, state, times, counts
    items: list[PacketItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def data_classes(self) -> set[DataClass]:
        """What the packet holds, for the gateway's data-rule check."""
        out: set[DataClass] = {"metadata"}
        for item in self.items:
            if item.subject or item.text:
                out.add(_text_class(item))
            if item.image_refs:
                out.add("images")
        return out


def _text_class(item: PacketItem) -> DataClass:
    return "chat" if item.data_class == "chat" else "text"


def reduce(packet: ContextPacket, level: DataClass) -> ContextPacket:
    """A copy of the packet with everything above `level` stripped, and a note per kind of loss."""
    dropped_text: dict[str, int] = {}
    dropped_images = 0
    items = []
    for item in packet.items:
        keep_text = allowed(level, _text_class(item))
        meta = (
            dict(item.meta) if keep_text else {k: v for k, v in item.meta.items() if k not in TEXT_META_KEYS}
        )
        if not keep_text and (item.subject or item.text or len(meta) != len(item.meta)):
            dropped_text[item.kind] = dropped_text.get(item.kind, 0) + 1
        images = item.image_refs if allowed(level, "images") else []
        dropped_images += len(item.image_refs) - len(images)
        items.append(
            item.model_copy(
                update={
                    "meta": meta,
                    "subject": item.subject if keep_text else None,
                    "text": item.text if keep_text else None,
                    "image_refs": images,
                }
            )
        )
    task = dict(packet.task)
    if not allowed(level, "text"):
        # Task titles come from mail subjects; summaries are free text.
        task = {k: v for k, v in task.items() if k not in TASK_TEXT_KEYS}
    notes = list(packet.notes)
    if len(task) != len(packet.task):
        notes.append(f"task title and summary left out: data rule is {level}")
    for kind, count in sorted(dropped_text.items()):
        notes.append(f"{count} {kind} text(s) left out: data rule is {level}")
    if dropped_images:
        notes.append(f"{dropped_images} image(s) left out: data rule is {level}")
    return ContextPacket(task=task, items=items, notes=notes)
