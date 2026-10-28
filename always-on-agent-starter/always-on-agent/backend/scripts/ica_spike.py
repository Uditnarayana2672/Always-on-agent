"""ICA spike (prompt 0.4, answers Q1). Sends only made-up content; never real data.

Run from backend/:  uv run python scripts/ica_spike.py [model ...]
Needs ICA_BASE_URL (backend/.env) and the key stored with "uv run agentd secret set ica".
"""

import asyncio
import base64
import json
import struct
import sys
import zlib
from typing import Any

from agent.core.config import get_config
from agent.core.errors import AgentError
from agent.model.ica_client import IcaClient

CLAUDE_IDS = ["claude-haiku-4-5", "claude-sonnet-5", "claude-sonnet-4-5", "claude-sonnet-4-6"]
FREE_FALLBACK = "gemma-4-26b-a4b-it"
SPIKE_TEXT = "TEST 42"

# 5x7 bitmap font for the few characters the test image uses.
_FONT = {
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    " ": ["00000"] * 7,
}


def tiny_png(text: str, scale: int = 6, pad: int = 4) -> bytes:
    """Black text on white as a grayscale PNG, built without an image library."""
    glyph_w = 6
    width = (len(text) * glyph_w + pad * 2) * scale
    height = (7 + pad * 2) * scale
    rows = []
    for y in range(height):
        gy = y // scale - pad
        row = bytearray([0])  # filter byte
        for x in range(width):
            gx = x // scale - pad
            ink = False
            if 0 <= gy < 7 and gx >= 0:
                index, col = divmod(gx, glyph_w)
                if index < len(text) and col < 5:
                    ink = _FONT[text[index]][gy][col] == "1"
            row.append(0 if ink else 255)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def _short(exc: Exception) -> str:
    return str(exc)[:160]


def _message(body: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = body["choices"][0]["message"]
    return message


async def probe_model(client: IcaClient, model: str) -> str:
    try:
        body = await client.chat(
            {"model": model, "messages": [{"role": "user", "content": "Reply with ok."}]}
        )
        return f"responds: {(_message(body).get('content') or '').strip()[:20]!r}"
    except AgentError as exc:
        return f"FAILED: {_short(exc)}"


async def probe_tools(client: IcaClient, model: str) -> str:
    tool = {
        "type": "function",
        "function": {
            "name": "report_colour",
            "description": "Report a colour.",
            "parameters": {
                "type": "object",
                "properties": {"colour": {"type": "string"}},
                "required": ["colour"],
            },
        },
    }
    try:
        body = await client.chat(
            {
                "model": model,
                "messages": [{"role": "user", "content": "The sky on a clear day. Report its colour."}],
                "tools": [tool],
                "tool_choice": {"type": "function", "function": {"name": "report_colour"}},
            }
        )
    except AgentError as exc:
        return f"FAILED: {_short(exc)}"
    calls = _message(body).get("tool_calls") or []
    return f"WORKS: {calls[0]['function']['arguments']}" if calls else "no tool call returned"


async def probe_json(client: IcaClient, model: str) -> str:
    try:
        body = await client.chat(
            {
                "model": model,
                "messages": [{"role": "user", "content": "Give a JSON object with key colour for the sky."}],
                "response_format": {"type": "json_object"},
            }
        )
        content = _message(body).get("content") or ""
        json.loads(content)
        return f"WORKS: {content.strip()[:60]!r}"
    except (AgentError, ValueError) as exc:
        return f"FAILED: {_short(exc)}"


async def probe_image(client: IcaClient, model: str) -> str:
    data = base64.b64encode(tiny_png(SPIKE_TEXT)).decode()
    try:
        body = await client.chat(
            {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
                            {"type": "text", "text": "What text is in this image? Reply with the text only."},
                        ],
                    }
                ],
            }
        )
    except AgentError as exc:
        return f"REJECTED: {_short(exc)}"
    text = (_message(body).get("content") or "").strip()
    verdict = "read correctly" if SPIKE_TEXT in text.upper() else "accepted but not read"
    return f"ACCEPTED, {verdict} ({text[:30]!r})"


async def main(models: list[str]) -> int:
    config = get_config()
    try:
        client = IcaClient.from_config(config)
    except AgentError as exc:
        print(exc)
        return 2
    client._max_attempts = 2

    print("ICA spike")
    try:
        available = await client.list_models()
        print(f"  models listed: {len(available)}; Claude: {[m for m in available if 'claude' in m]}")
    except AgentError as exc:
        print(f"  models list FAILED: {_short(exc)}")
        return 1

    targets = models or [*CLAUDE_IDS, FREE_FALLBACK]
    print("  responding:")
    working = []
    for model in targets:
        status = await probe_model(client, model)
        print(f"    {model:22} {status}")
        if status.startswith("responds"):
            working.append(model)

    for model in working[:2]:
        print(f"  {model}:")
        print(f"    tool use:    {await probe_tools(client, model)}")
        print(f"    JSON mode:   {await probe_json(client, model)}")
        print(f"    image input: {await probe_image(client, model)}")
    await client.aclose()
    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
