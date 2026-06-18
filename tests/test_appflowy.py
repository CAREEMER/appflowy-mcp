"""Unit tests for appflowy_mcp.appflowy."""

from __future__ import annotations

import pytest

from appflowy_mcp.appflowy import AppFlowyClient, AppFlowyError, unwrap
from appflowy_mcp.config import AppFlowyConfig
from tests.conftest import FakeResponse


class FakeHTTP:
    """Scripted replacement for httpx.AsyncClient."""

    def __init__(self, request_responses=None, post_responses=None):
        self.request_responses = list(request_responses or [])
        self.post_responses = list(post_responses or [])
        self.requests = []
        self.posts = []
        self.closed = False

    async def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        return self.request_responses.pop(0)

    async def post(self, url, **kw):
        self.posts.append((url, kw))
        return self.post_responses.pop(0)

    async def aclose(self):
        self.closed = True


def make_client(http, **cfg):
    client = AppFlowyClient(AppFlowyConfig(**cfg))
    client._http = http
    return client


def test_unwrap_returns_data_field():
    assert unwrap({"code": 0, "data": [1, 2]}) == [1, 2]


def test_unwrap_returns_payload_without_data():
    assert unwrap({"a": 1}) == {"a": 1}


def test_base_url_strips_trailing_slash():
    client = make_client(FakeHTTP(), base_url="https://x/")
    assert client.base_url == "https://x"


async def test_aclose_closes_http():
    http = FakeHTTP()
    client = make_client(http)
    await client.aclose()
    assert http.closed is True


async def test_login_with_static_access_token():
    client = make_client(FakeHTTP(), access_token="static")
    assert await client._login() == "static"


async def test_login_without_credentials_raises():
    client = make_client(FakeHTTP())
    with pytest.raises(AppFlowyError, match="no AppFlowy credentials"):
        await client._login()


async def test_login_password_grant_success():
    http = FakeHTTP(post_responses=[FakeResponse(json_data={"access_token": "jwt"})])
    client = make_client(http, email="e@e", password="p")
    assert await client._login() == "jwt"


async def test_login_without_access_token_in_response_raises():
    http = FakeHTTP(post_responses=[FakeResponse(json_data={})])
    client = make_client(http, email="e@e", password="p")
    with pytest.raises(AppFlowyError, match="returned no access_token"):
        await client._login()


async def test_ensure_token_returns_cached_token():
    client = make_client(FakeHTTP(), access_token="cached")
    assert await client._ensure_token() == "cached"


async def test_ensure_token_logs_in_when_absent():
    http = FakeHTTP(post_responses=[FakeResponse(json_data={"access_token": "fresh"})])
    client = make_client(http, email="e@e", password="p")
    assert await client._ensure_token() == "fresh"


async def test_concurrent_ensure_token_logs_in_only_once():
    import asyncio

    class SlowHTTP:
        """Logs in with a suspension, so a second caller waits on the lock."""

        def __init__(self):
            self.posts = []

        async def post(self, url, **kw):
            self.posts.append((url, kw))
            await asyncio.sleep(0)
            return FakeResponse(json_data={"access_token": "once"})

    http = SlowHTTP()
    client = make_client(http, email="e@e", password="p")
    results = await asyncio.gather(client._ensure_token(), client._ensure_token())
    assert results == ["once", "once"]
    assert len(http.posts) == 1


async def test_relogin_keeps_static_token():
    client = make_client(FakeHTTP(), access_token="static")
    assert await client._relogin() == "static"


async def test_request_returns_json_on_success():
    http = FakeHTTP(request_responses=[FakeResponse(json_data={"ok": 1})])
    client = make_client(http, access_token="t")
    assert await client.request("GET", "/x") == {"ok": 1}


async def test_request_returns_empty_dict_when_no_content():
    http = FakeHTTP(request_responses=[FakeResponse(content=b"")])
    client = make_client(http, access_token="t")
    assert await client.request("GET", "/x") == {}


async def test_request_raises_on_http_error():
    http = FakeHTTP(request_responses=[FakeResponse(status_code=500, text="boom")])
    client = make_client(http, access_token="t")
    with pytest.raises(AppFlowyError, match="HTTP 500"):
        await client.request("GET", "/x")


async def test_request_reauthenticates_once_on_401():
    http = FakeHTTP(
        request_responses=[
            FakeResponse(status_code=401),
            FakeResponse(json_data={"ok": 1}),
        ]
    )
    client = make_client(http, access_token="t")
    assert await client.request("GET", "/x") == {"ok": 1}
    assert len(http.requests) == 2


async def test_request_raises_on_second_401():
    http = FakeHTTP(
        request_responses=[FakeResponse(status_code=401), FakeResponse(status_code=401)]
    )
    client = make_client(http, access_token="t")
    with pytest.raises(AppFlowyError, match="HTTP 401"):
        await client.request("GET", "/x")


async def test_list_workspaces_hits_endpoint():
    http = FakeHTTP(request_responses=[FakeResponse(json_data={"data": []})])
    client = make_client(http, access_token="t")
    await client.list_workspaces()
    assert http.requests[0][1].endswith("/api/workspace")


async def test_get_folder_without_root_view():
    http = FakeHTTP(request_responses=[FakeResponse(json_data={})])
    client = make_client(http, access_token="t")
    await client.get_folder("WS")
    assert http.requests[0][2]["params"] == {"depth": 20}


async def test_get_folder_with_root_view():
    http = FakeHTTP(request_responses=[FakeResponse(json_data={})])
    client = make_client(http, access_token="t")
    await client.get_folder("WS", depth=3, root_view_id="V")
    assert http.requests[0][2]["params"] == {"depth": 3, "root_view_id": "V"}


async def test_get_page_view_raw_hits_endpoint():
    http = FakeHTTP(request_responses=[FakeResponse(json_data={})])
    client = make_client(http, access_token="t")
    await client.get_page_view_raw("WS", "P")
    assert http.requests[0][1].endswith("/api/workspace/WS/page-view/P")


async def test_post_web_update_success():
    http = FakeHTTP(post_responses=[FakeResponse(json_data={"applied": True})])
    client = make_client(http, access_token="t")
    result = await client.post_web_update("WS", "OBJ", b"\x01\x02")
    assert result == {"applied": True}


async def test_post_web_update_reauthenticates_on_401():
    http = FakeHTTP(
        post_responses=[
            FakeResponse(status_code=401),
            FakeResponse(json_data={"applied": True}),
        ]
    )
    client = make_client(http, access_token="t")
    result = await client.post_web_update("WS", "OBJ", b"\x01")
    assert result == {"applied": True}
    assert len(http.posts) == 2


async def test_post_web_update_non_json_body_falls_back():
    http = FakeHTTP(
        post_responses=[FakeResponse(status_code=200, raise_json=True, text="plain")]
    )
    client = make_client(http, access_token="t")
    result = await client.post_web_update("WS", "OBJ", b"\x01")
    assert result == {"status_code": 200, "body": "plain"}


async def test_post_web_update_error_status_returns_error_dict():
    http = FakeHTTP(post_responses=[FakeResponse(status_code=403, json_data={"e": 1})])
    client = make_client(http, access_token="t")
    result = await client.post_web_update("WS", "OBJ", b"\x01")
    assert result["error"].startswith("web-update failed")
    assert result["detail"] == {"e": 1}
