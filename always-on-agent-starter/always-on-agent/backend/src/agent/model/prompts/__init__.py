"""Versioned prompt templates for the model (F05). Templates live in templates/<name>.v<N>.j2."""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(name: str, version: int = 1, **variables: Any) -> str:
    """Render templates/<name>.v<version>.j2. Missing variables raise jinja2.UndefinedError."""
    return _env.get_template(f"{name}.v{version}.j2").render(**variables).strip()


def wrap_data(content: str, label: str = "") -> str:
    """Frame fetched content as data. Any closing tag inside the content is neutralised."""
    safe = content.replace("</data>", "</ data>").replace("<data", "< data")
    attr = f' source="{label}"' if label else ""
    return f"<data{attr}>\n{safe}\n</data>"
