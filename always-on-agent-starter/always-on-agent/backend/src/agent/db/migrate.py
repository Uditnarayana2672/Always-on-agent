"""Run Alembic migrations from code (F08)."""

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def upgrade_head(url: str) -> None:
    """Bring the database at `url` up to the latest schema."""
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={url}"])
    command.upgrade(cfg, "head")
