import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent.core.errors import AdapterError, AgentError, ApprovalConflict
from agent.core.ids import find_inc_numbers, new_ulid, normalise_task_id


def test_ulid_shape_and_order() -> None:
    a, b = new_ulid(1_000), new_ulid(2_000)
    assert len(a) == 26 and a < b


@given(st.integers(min_value=0, max_value=2**48 - 1))
def test_ulid_time_prefix_sorts(ms: int) -> None:
    assert new_ulid(ms)[:10] <= new_ulid(min(ms + 1, 2**48 - 1))[:10]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INC30114872", "INC30114872"),
        (" inc30114872 ", "INC30114872"),
        ("enh-14", "ENH-14"),
        ("REF-0412", "REF-0412"),
        ("pbi-sales7", "PBI-SALES7"),
        ("INC3011487", None),
        ("INC301148721", None),
        ("ENH-", None),
        ("hello", None),
    ],
)
def test_normalise_task_id(raw: str, expected: str | None) -> None:
    assert normalise_task_id(raw) == expected


def test_find_inc_numbers() -> None:
    text = "Re: inc30114872 and INC30114999; again INC30114872, not INC123"
    assert find_inc_numbers(text) == ["INC30114872", "INC30114999"]


def test_error_hierarchy() -> None:
    err = AdapterError("graph 503", retryable=True)
    assert isinstance(err, AgentError) and err.retryable
    assert issubclass(ApprovalConflict, AgentError)
