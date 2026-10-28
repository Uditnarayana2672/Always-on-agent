"""The gateway with the real privacy layer: names never reach the model; leaks and data-rule breaches
stop the call before any request is sent (F05, F11)."""

import json
from collections.abc import Sequence

import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.core.config import CONFIG_DIR, Config, load_config
from agent.core.errors import DataRuleViolation, LeakDetected
from agent.db.repositories import Task, TaskRepo, TokenCipher, TokenMapRepo, UsageRepo
from agent.model.gateway import ModelGateway
from agent.model.ica_client import IcaClient
from agent.privacy.hook import PrivacyLayer
from agent.privacy.pseudonymiser import KnownPerson, Pseudonymiser
from tests.model.test_gateway import BASE, IDS, URL, Triage, tool_reply

TASK = "INC30114872"


@pytest.fixture
def config() -> Config:
    data = load_config(CONFIG_DIR / "config.example.yaml").model_dump()
    data["models"]["ids"] = IDS
    return Config.model_validate(data)


class People:
    async def people(self, task_id: str | None) -> list[KnownPerson]:
        return [KnownPerson(name="Tomás García", email="tomas.garcia@corp.example", role="caller")]


class BrokenPseudonymiser(Pseudonymiser):
    """Simulates a pseudonymiser bug: nothing is replaced. The leak check must still stop the call."""

    async def pseudonymise(
        self, text: str, task_id: str | None, extra_people: Sequence[KnownPerson] = ()
    ) -> str:
        return text


async def _gateway(
    engine: AsyncEngine, clock: FrozenClock, config: Config, level: str = "text", broken: bool = False
) -> ModelGateway:
    await TaskRepo(engine, clock).create(
        Task(id=TASK, project="doxis", kind="incident", title="t", state="new")
    )
    tokens = TokenMapRepo(engine, clock, TokenCipher(Fernet.generate_key()))
    pseudo = (BrokenPseudonymiser if broken else Pseudonymiser)(tokens, People())
    return ModelGateway(
        IcaClient(BASE, "k", IDS),
        config,
        UsageRepo(engine, clock),
        clock,
        PrivacyLayer(level, pseudo),  # type: ignore[arg-type]
    )


MESSAGES = [
    {"role": "user", "content": "Tomás García (tomas.garcia@corp.example, +41 44 123 45 67) cannot log in."},
    {"role": "user", "content": [{"type": "text", "text": "Hi Tomas, see above."}]},
]


@respx.mock
async def test_names_are_pseudonymised_before_the_call(
    engine: AsyncEngine, clock: FrozenClock, config: Config
) -> None:
    gateway = await _gateway(engine, clock, config)
    route = respx.post(URL).mock(return_value=tool_reply({"category": "access", "urgent": False}))
    await gateway.run("triage", MESSAGES, Triage, {"text"}, task_id=TASK)
    sent = json.loads(route.calls[-1].request.content)["messages"][1:]
    assert sent[0]["content"] == "CALLER (CALLER, PHONE_1) cannot log in."
    assert sent[1]["content"] == [{"type": "text", "text": "Hi CALLER, see above."}]
    body = route.calls[-1].request.content.decode()
    assert "Tom" not in body and "garcia" not in body.lower()


async def test_leak_stops_the_call(engine: AsyncEngine, clock: FrozenClock, config: Config) -> None:
    gateway = await _gateway(engine, clock, config, broken=True)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(URL).mock(return_value=tool_reply({"category": "x", "urgent": False}))
        with pytest.raises(LeakDetected):
            await gateway.run("triage", MESSAGES, Triage, {"text"}, task_id=TASK)
        assert route.call_count == 0


async def test_data_rule_stops_the_call(engine: AsyncEngine, clock: FrozenClock, config: Config) -> None:
    gateway = await _gateway(engine, clock, config, level="metadata")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(URL).mock(return_value=tool_reply({"category": "x", "urgent": False}))
        with pytest.raises(DataRuleViolation, match="text"):
            await gateway.run("triage", MESSAGES, Triage, {"text"}, task_id=TASK)
        assert route.call_count == 0


async def test_undeclared_image_is_refused(engine: AsyncEngine, clock: FrozenClock, config: Config) -> None:
    gateway = await _gateway(engine, clock, config, level="images")
    image = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}
    ]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(URL).mock(return_value=tool_reply({"category": "x", "urgent": False}))
        with pytest.raises(DataRuleViolation, match="not declared"):
            await gateway.run("ocr", image, Triage, {"text"}, task_id=TASK)
        assert route.call_count == 0
