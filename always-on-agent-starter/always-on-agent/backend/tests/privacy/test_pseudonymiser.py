from collections.abc import AsyncIterator, Sequence

import pytest
from cryptography.fernet import Fernet
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.adapters.base import MailMessage, Person
from agent.core.clock import FrozenClock
from agent.core.errors import LeakDetected, UnknownToken
from agent.db.repositories import DirectoryRepo, MemoryRepo, Task, TaskRepo, TokenCipher, TokenMapRepo
from agent.privacy.pseudonymiser import (
    SIGNATURE_MARK,
    KnownPerson,
    LedgerPeople,
    Pseudonymiser,
    greeting_names,
    name_parts,
    people_from_mail,
    strip_signatures,
)
from agent.privacy.text import fold

TASK = "INC30114872"
OTHER = "INC30114873"


class StaticPeople:
    def __init__(self, people: Sequence[KnownPerson] = ()) -> None:
        self.by_task: dict[str | None, list[KnownPerson]] = {TASK: list(people)}

    async def people(self, task_id: str | None) -> list[KnownPerson]:
        return list(self.by_task.get(task_id, []))


CALLER = KnownPerson(name="Tomás García", email="tomas.garcia@corp.example", role="caller")
KM = KnownPerson(name="Sophie Laurent", email="sophie.laurent@corp.example", role="km")
LBA = KnownPerson(name="Laurent, Marc", email="marc.laurent@corp.example", role="lba")
WILL = KnownPerson(name="Will Rose", email="will.rose@corp.example")


@pytest.fixture
async def tokens(engine: AsyncEngine, clock: FrozenClock) -> AsyncIterator[TokenMapRepo]:
    for task_id in (TASK, OTHER):
        await TaskRepo(engine, clock).create(
            Task(id=task_id, project="doxis", kind="incident", title="t", state="new")
        )
    yield TokenMapRepo(engine, clock, TokenCipher(Fernet.generate_key()))


@pytest.fixture
def people() -> StaticPeople:
    return StaticPeople([CALLER, KM, LBA, WILL])


@pytest.fixture
def pseudo(tokens: TokenMapRepo, people: StaticPeople) -> Pseudonymiser:
    return Pseudonymiser(tokens, people)


# ---------------------------------------------------------------- known people


async def test_roles_give_stable_tokens(pseudo: Pseudonymiser) -> None:
    out = await pseudo.pseudonymise("Tomás García asked Sophie Laurent and Marc Laurent.", TASK)
    assert out == "CALLER asked KM_1 and LBA_1."
    again = await pseudo.pseudonymise("Sophie Laurent again", TASK)
    assert again == "KM_1 again"


async def test_accents_and_case_insensitive(pseudo: Pseudonymiser) -> None:
    out = await pseudo.pseudonymise("TOMAS GARCIA wrote; then tomas garcia; then Tomas.", TASK)
    assert out == "CALLER wrote; then CALLER; then CALLER."


async def test_whole_word_only(pseudo: Pseudonymiser) -> None:
    out = await pseudo.pseudonymise("Tomasz and Garcias are other people. Laurentian Abyss.", TASK)
    assert out == "Tomasz and Garcias are other people. Laurentian Abyss."


async def test_first_name_that_is_a_word_needs_a_capital(pseudo: Pseudonymiser) -> None:
    out = await pseudo.pseudonymise("I will check. Will said the rose is red. will rose wrote.", TASK)
    assert out == "I will check. USER_1 said the rose is red. USER_1 wrote."


async def test_reversed_and_comma_names(pseudo: Pseudonymiser) -> None:
    out = await pseudo.pseudonymise("From: Garcia, Tomas. Also Laurent Sophie.", TASK)
    assert out == "From: CALLER. Also KM_1."


async def test_zero_width_and_nbsp_cannot_hide_a_name(pseudo: Pseudonymiser) -> None:
    out = await pseudo.pseudonymise("Ping To\u200bmás\u00a0García now", TASK)
    assert out == "Ping CALLER now"


async def test_name_across_a_line_break(pseudo: Pseudonymiser) -> None:
    assert await pseudo.pseudonymise("Sophie\nLaurent", TASK) == "KM_1"


async def test_tokens_are_per_task(pseudo: Pseudonymiser, people: StaticPeople) -> None:
    people.by_task[OTHER] = [KnownPerson(name="Anna Keller", role="caller")]
    assert await pseudo.pseudonymise("Anna Keller", OTHER) == "CALLER"
    assert await pseudo.pseudonymise("Tomás García", TASK) == "CALLER"
    assert await pseudo.restore("Hi CALLER", OTHER) == "Hi Anna Keller"
    assert await pseudo.restore("Hi CALLER", TASK) == "Hi Tomás García"


async def test_second_caller_falls_back_to_user(pseudo: Pseudonymiser, people: StaticPeople) -> None:
    people.by_task[TASK].append(KnownPerson(name="Lena Vogt", role="caller"))
    out = await pseudo.pseudonymise("Tomás García and Lena Vogt", TASK)
    assert out == "CALLER and USER_1"


# ---------------------------------------------------------------- patterns


async def test_emails_known_and_unknown(pseudo: Pseudonymiser) -> None:
    out = await pseudo.pseudonymise(
        "Mail tomas.garcia@corp.example and Sophie.Laurent@corp.example, cc x.y@partner.example", TASK
    )
    assert out == "Mail CALLER and KM_1, cc USER_1"


async def test_phones_and_user_ids(pseudo: Pseudonymiser) -> None:
    out = await pseudo.pseudonymise(
        "Call +41 44 123 45 67 or 044 123 45 67 or 0041 79 555 12 12. Login CORP\\tgarcia, id a123456.", TASK
    )
    assert out == "Call PHONE_1 or PHONE_2 or PHONE_3. Login UID_1, id UID_2."


async def test_ticket_numbers_dates_and_times_are_not_phones(pseudo: Pseudonymiser) -> None:
    source = "INC30114872 opened 2026-03-25 09:15, due 02.04.2026, REF-0412, KB0012345, 12 users"
    assert await pseudo.pseudonymise(source, TASK) == source


async def test_same_phone_same_token(pseudo: Pseudonymiser) -> None:
    out = await pseudo.pseudonymise("+41 44 123 45 67 / +41441234567", TASK)
    assert out == "PHONE_1 / PHONE_1"


# ---------------------------------------------------------------- greetings and signatures


def test_greeting_names() -> None:
    assert greeting_names("Hi Tomás,\nthanks") == ["Tomás"]
    assert greeting_names("Dear Mr. Keller-Brandt,") == ["Keller-Brandt"]
    assert greeting_names("Hello all,\nHi team") == []
    assert greeting_names("Bonjour Jean Luc, merci") == ["Jean Luc"]


def test_signature_block() -> None:
    body = "Please check.\n\nBest regards,\nAnna Keller\nSenior Analyst\n+41 44 999 88 77\n\nOld quote"
    clean, names = strip_signatures(body)
    assert names == ["Anna Keller"]
    assert clean == f"Please check.\n\nBest regards,\nAnna Keller\n{SIGNATURE_MARK}\n\nOld quote"


def test_sign_off_followed_by_a_sentence_is_not_a_signature() -> None:
    body = "Thanks,\nthe export failed again at noon."
    assert strip_signatures(body) == (body, [])


async def test_unknown_names_from_greeting_and_signature(pseudo: Pseudonymiser) -> None:
    body = "Hi Priya,\nAnna Keller said Priya knows.\n\nKind regards\nAnna Keller\nDoxis Support\nBuilding 5"
    out = await pseudo.pseudonymise(body, TASK)
    assert "Priya" not in out and "Anna" not in out and "Building" not in out
    assert out.startswith("Hi USER_")
    # Remembered for the next text in the same task.
    assert "Anna" not in await pseudo.pseudonymise("anna keller called", TASK)


# ---------------------------------------------------------------- leak check and restore


async def test_leak_check(pseudo: Pseudonymiser) -> None:
    await pseudo.leak_check("CALLER asked KM_1", TASK)
    with pytest.raises(LeakDetected, match="1 known name") as exc:
        await pseudo.leak_check("CALLER asked Sophie", TASK)
    assert "Sophie" not in str(exc.value)  # the message never repeats the leak
    with pytest.raises(LeakDetected, match="e-mail"):
        await pseudo.leak_check("write to someone@else.example", TASK)
    with pytest.raises(LeakDetected, match="phone"):
        await pseudo.leak_check("call +41 44 123 45 67", TASK)


async def test_restore_round_trip(pseudo: Pseudonymiser) -> None:
    source = "Tomás García (tomas.garcia@corp.example, +41 44 123 45 67) asked Sophie Laurent."
    tokenised = await pseudo.pseudonymise(source, TASK)
    restored = await pseudo.restore(tokenised, TASK)
    assert restored == "Tomás García (Tomás García, +41 44 123 45 67) asked Sophie Laurent."


async def test_restore_blocks_an_invented_token(pseudo: Pseudonymiser) -> None:
    await pseudo.pseudonymise("Tomás García", TASK)
    with pytest.raises(UnknownToken, match="USER_7"):
        await pseudo.restore("Hi CALLER, USER_7 will help.", TASK)
    with pytest.raises(UnknownToken, match="AFFECTED"):
        await pseudo.restore("AFFECTED cannot log in", TASK)


async def test_restore_accepts_a_mis_cased_token(pseudo: Pseudonymiser) -> None:
    await pseudo.pseudonymise("Sophie Laurent", TASK)
    assert await pseudo.restore("Ask Km_1 please", TASK) == "Ask Sophie Laurent please"


async def test_token_like_text_in_the_source_is_neutralised(pseudo: Pseudonymiser) -> None:
    out = await pseudo.pseudonymise("The field CALLER shows USER_3.", TASK)
    assert out == "The field caller shows user-3."
    # The task's own tokens in already-pseudonymised text survive a second pass.
    await pseudo.pseudonymise("Tomás García", TASK)
    assert await pseudo.pseudonymise("CALLER replied", TASK) == "CALLER replied"


async def test_mapping_is_encrypted(pseudo: Pseudonymiser, engine: AsyncEngine) -> None:
    await pseudo.pseudonymise("Tomás García", TASK)
    async with engine.connect() as conn:
        blobs = (await conn.execute(text("SELECT real_value_encrypted, value_digest FROM token_map"))).all()
    assert blobs and all(b"Tom" not in blob and "tom" not in digest for blob, digest in blobs)


# ---------------------------------------------------------------- people sources


def test_name_parts() -> None:
    assert name_parts("Laurent, Sophie (Doxis)") == ["Sophie", "Laurent"]
    assert name_parts("J. R. Ewing") == ["Ewing"]


async def test_ledger_people_and_mail_headers(engine: AsyncEngine, clock: FrozenClock) -> None:
    await TaskRepo(engine, clock).create(
        Task(id=TASK, project="doxis", kind="incident", title="t", state="new")
    )
    memory, directory = MemoryRepo(engine, clock), DirectoryRepo(engine, clock)
    await directory.upsert("km.one@corp.example", "Karin Moser", role="KM")
    source = LedgerPeople(memory, directory)
    mail = MailMessage(
        id="m1",
        conversation_id="c1",
        subject="s",
        sender=Person(name="Tomás García", address="Tomas.Garcia@corp.example"),
        to=[Person(name="Me", address="me@corp.example")],
        received_at=clock.now_utc(),
        body="",
    )
    await source.remember(TASK, people_from_mail(mail, {"tomas.garcia@corp.example": "caller"}))
    await source.remember(TASK, [KnownPerson(email="tomas.garcia@corp.example", role="caller")])
    people = await source.people(TASK)
    assert {(p.email, p.role) for p in people} == {
        ("km.one@corp.example", "km"),
        ("tomas.garcia@corp.example", "caller"),
        ("me@corp.example", None),
    }
    pseudo = Pseudonymiser(TokenMapRepo(engine, clock, TokenCipher(Fernet.generate_key())), source)
    assert await pseudo.pseudonymise("Karin Moser and Tomás García", TASK) == "KM_1 and CALLER"


# ---------------------------------------------------------------- property: names never survive

_LETTERS = "abcdefghijklmnopqrstuvwxyzáéíóúàèäöüñçø"
_part = st.text(alphabet=_LETTERS, min_size=2, max_size=9).map(lambda p: p[0].upper() + p[1:])
_filler = st.lists(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8), max_size=12)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    first=_part, last=_part, words=_filler, at=st.integers(min_value=0, max_value=12), how=st.integers(0, 3)
)
async def test_random_names_never_survive(
    tokens: TokenMapRepo, first: str, last: str, words: list[str], at: int, how: int
) -> None:
    full = f"{first} {last}"
    shown = [full, full.upper(), full.lower(), first][how]
    pseudo = Pseudonymiser(tokens, StaticPeople([KnownPerson(name=full, role="caller")]))
    source = " ".join([*words[:at], shown, *words[at:]])
    out = await pseudo.pseudonymise(source, TASK)
    await pseudo.leak_check(out, TASK)
    if how < 3:
        assert fold(full) not in fold(out).replace("caller", "")
    assert first not in out.split()


async def test_participants_are_stored_encrypted(engine: AsyncEngine, clock: FrozenClock) -> None:
    await TaskRepo(engine, clock).create(
        Task(id=TASK, project="doxis", kind="incident", title="t", state="new")
    )
    memory = MemoryRepo(engine, clock)
    cipher = TokenCipher(Fernet.generate_key())
    source = LedgerPeople(memory, DirectoryRepo(engine, clock), cipher)
    await source.remember(
        TASK, [KnownPerson(name="Tomás García", email="tomas.garcia@corp.example", role="caller")]
    )
    stored = (await memory.facts(TASK))[0]["value"]
    assert stored.startswith("enc:") and "garcia" not in stored.lower()
    assert [p.email for p in await source.participants(TASK)] == ["tomas.garcia@corp.example"]
    with pytest.raises(ValueError, match="cipher"):
        await LedgerPeople(memory, DirectoryRepo(engine, clock)).participants(TASK)
