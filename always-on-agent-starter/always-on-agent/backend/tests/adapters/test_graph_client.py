from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from agent.adapters.graph_client import DeltaExpired, Forbidden, NotFound, retry_after_seconds
from agent.adapters.html_text import body_text, html_to_text
from agent.core.errors import AdapterError, NeedsSignIn
from tests.adapters.graph_helpers import NOW, G, Health, Sleeps, Tokens, client


@pytest.fixture
async def http() -> httpx.AsyncClient:
    async with httpx.AsyncClient() as c:
        yield c


def test_retry_after_parsing() -> None:
    assert retry_after_seconds("7", NOW, 2) == 7
    assert retry_after_seconds(None, NOW, 2) == 2
    assert retry_after_seconds("garbage", NOW, 3) == 3
    assert retry_after_seconds(format_datetime(NOW + timedelta(seconds=30), usegmt=True), NOW, 2) == 30
    assert retry_after_seconds("-5", NOW, 2) == 0  # never negative
    assert retry_after_seconds("100000", NOW, 2) == 120  # capped


@respx.mock
async def test_paging_follows_next_link(http: httpx.AsyncClient) -> None:
    respx.get(f"{G}/me/things", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"value": [3]})
    )
    respx.get(f"{G}/me/things").mock(
        return_value=httpx.Response(200, json={"value": [1, 2], "@odata.nextLink": f"{G}/me/things?page=2"})
    )
    assert await client(http).get_all("me/things") == [1, 2, 3]


@respx.mock
async def test_throttling_honours_retry_after_and_reports_degraded(http: httpx.AsyncClient) -> None:
    route = respx.get(f"{G}/me").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "11"}),
            httpx.Response(503, headers={"Retry-After": "4"}),
            httpx.Response(200, json={"id": "me"}),
        ]
    )
    sleeps, health = Sleeps(), Health()
    body = await client(http, sleep=sleeps, health=health).get("me")
    assert body["id"] == "me" and route.call_count == 3
    assert sleeps.waits == [11, 4]
    assert [r[1] for r in health.reports] == ["degraded", "degraded", "ok"]


@respx.mock
async def test_throttled_to_the_end_is_retryable_429(http: httpx.AsyncClient) -> None:
    respx.get(f"{G}/me").mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(AdapterError) as err:
        await client(http).get("me")
    assert err.value.retryable and err.value.status_code == 429


@respx.mock
async def test_401_gets_one_fresh_token(http: httpx.AsyncClient) -> None:
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={"ok": True})

    respx.get(f"{G}/me").mock(side_effect=answer)
    tokens = Tokens()
    await client(http, tokens=tokens).get("me")
    assert seen == ["Bearer token-1", "Bearer token-2"]


@respx.mock
async def test_401_twice_means_sign_in_again(http: httpx.AsyncClient) -> None:
    respx.get(f"{G}/me").mock(return_value=httpx.Response(401))
    health = Health()
    with pytest.raises(AdapterError, match="sign in again"):
        await client(http, health=health).get("me")
    assert health.last() == "down"


async def test_no_cached_token_marks_down(http: httpx.AsyncClient) -> None:
    class Gone:
        def get_token(self, scopes: list[str]) -> str:
            raise NeedsSignIn("refresh token expired")

    health = Health()
    with pytest.raises(NeedsSignIn):
        await client(http, tokens=Gone(), health=health).get("me")
    assert health.reports[-1][1] == "down" and "sign-in" in (health.reports[-1][2] or "")


@respx.mock
async def test_typed_errors(http: httpx.AsyncClient) -> None:
    respx.get(f"{G}/gone").mock(return_value=httpx.Response(410))
    respx.get(f"{G}/sync").mock(
        return_value=httpx.Response(400, json={"error": {"code": "SyncStateNotFound", "message": "x"}})
    )
    respx.get(f"{G}/no").mock(
        return_value=httpx.Response(403, json={"error": {"code": "Forbidden", "message": "Access denied"}})
    )
    respx.get(f"{G}/missing").mock(return_value=httpx.Response(404))
    c = client(http)
    with pytest.raises(DeltaExpired):
        await c.get("gone")
    with pytest.raises(DeltaExpired):
        await c.get("sync")
    with pytest.raises(Forbidden, match="Access denied"):
        await c.get("no")
    with pytest.raises(NotFound):
        await c.get("missing")


@respx.mock
async def test_server_errors_back_off_then_give_up(http: httpx.AsyncClient) -> None:
    respx.get(f"{G}/me").mock(return_value=httpx.Response(500))
    sleeps = Sleeps()
    with pytest.raises(AdapterError) as err:
        await client(http, sleep=sleeps).get("me")
    assert err.value.retryable and err.value.status_code == 500 and len(sleeps.waits) == 3


@respx.mock
async def test_transport_errors_retry(http: httpx.AsyncClient) -> None:
    respx.get(f"{G}/me").mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"id": 1})])
    assert (await client(http).get("me"))["id"] == 1


@respx.mock
async def test_empty_202_keeps_headers(http: httpx.AsyncClient) -> None:
    respx.post(f"{G}/x").mock(return_value=httpx.Response(202, headers={"RequestId": "r-1"}))
    body = await client(http).post("x", {})
    assert body["_headers"]["requestid"] == "r-1"


# ---------------------------------------------------------------- HTML to text


def test_html_to_text_drops_images_scripts_and_hidden() -> None:
    html = (
        "<html><head><style>p{color:red}</style><title>t</title></head><body>"
        "<p>Hello&nbsp;Sophie,</p><img src='cid:image001.png'>"
        "<div style='display:none'>tracking</div><script>alert(1)</script>"
        "<p>Line one<br>Line two</p><ul><li>first</li><li>second</li></ul>"
        "<p>See <a href='https://app.powerbi.com/groups/me/reports/x'>the report</a>.</p>"
        "</body></html>"
    )
    text = html_to_text(html)
    assert "Hello Sophie," in text and "tracking" not in text and "alert" not in text and "color" not in text
    assert "Line one\nLine two" in text and "- first" in text and "- second" in text
    assert "the report (https://app.powerbi.com/groups/me/reports/x)" in text
    assert "image001" not in text


def test_html_to_text_keeps_mentions_and_entities() -> None:
    assert html_to_text('<p><at id="0">Test User</at> can you check &lt;INC30115230&gt;?</p>') == (
        "Test User can you check <INC30115230>?"
    )
    assert html_to_text("<p>unclosed <b>bold<p>next") == "unclosed bold\nnext"


def test_body_text_plain_passes_through() -> None:
    assert body_text({"contentType": "text", "content": "a\r\nb "}) == "a\nb"
    assert body_text(None) == ""
