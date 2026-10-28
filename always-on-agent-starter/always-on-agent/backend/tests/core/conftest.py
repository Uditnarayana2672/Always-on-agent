from typing import Any

import pytest

from agent.core.config import CONFIG_DIR, Config, load_config


def with_overrides(config: Config, **sections: dict[str, Any]) -> Config:
    data = config.model_dump()
    for key, values in sections.items():
        data[key].update(values)
    return Config.model_validate(data)


@pytest.fixture
def config() -> Config:
    return load_config(CONFIG_DIR / "config.example.yaml")
