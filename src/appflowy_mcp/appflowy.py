"""Async client for the AppFlowy Cloud REST + collab API.

The whole server talks to AppFlowy as a single *service account*. We log in to
GoTrue with the password grant (or use a pre-minted JWT) and reuse the token
for every call, transparently re-authenticating on a 401 so the long-running
server survives the JWT's expiry.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import AppFlowyConfig

log = logging.getLogger("appflowy_mcp.client")


class AppFlowyError(RuntimeError):
    pass


def unwrap(payload: Any) -> Any:
    """Return the meaningful body of an AppFlowy response.

    AppFlowy wraps results as ``{"code": 0, "message": "...", "data": ...}``.
    Callers that need the actual object want ``data``; this returns it when
    present and falls back to the payload itself otherwise.
    """
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


class AppFlowyClient:
    def __init__(self, config: AppFlowyConfig) -> None:
        self._config = config
        self._base_url = config.base_url.rstrip("/")
        self._token: str | None = config.access_token
        self._login_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=30.0)

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- authentication ----------------------------------------------------
    async def _login(self) -> str:
        if self._config.access_token:
            # A static token was provided; nothing to refresh.
            self._token = self._config.access_token
            return self._token
        if not (self._config.email and self._config.password):
            raise AppFlowyError(
                "no AppFlowy credentials configured: set APPFLOWY_ACCESS_TOKEN or "
                "APPFLOWY_EMAIL + APPFLOWY_PASSWORD"
            )
        resp = await self._http.post(
            f"{self._base_url}/gotrue/token",
            params={"grant_type": "password"},
            json={"email": self._config.email, "password": self._config.password},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise AppFlowyError("GoTrue login succeeded but returned no access_token")
        self._token = token
        log.info("authenticated to AppFlowy as %s", self._config.email)
        return token

    async def _ensure_token(self) -> str:
        if self._token:
            return self._token
        async with self._login_lock:
            if not self._token:
                await self._login()
        return self._token  # type: ignore[return-value]

    async def _relogin(self) -> str:
        async with self._login_lock:
            self._token = None if not self._config.access_token else self._token
            return await self._login()

    # -- raw requests ------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        """Perform an authenticated API request and return parsed JSON.

        Re-authenticates once on a 401 so an expired service JWT is healed
        without operator intervention.
        """
        token = await self._ensure_token()
        url = f"{self._base_url}{path}"
        for attempt in range(2):
            resp = await self._http.request(
                method,
                url,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if resp.status_code == 401 and attempt == 0:
                token = await self._relogin()
                continue
            if resp.status_code >= 400:
                raise AppFlowyError(
                    f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}"
                )
            if not resp.content:
                return {}
            return resp.json()
        raise AppFlowyError(  # pragma: no cover - loop always returns or raises first
            f"{method} {path} failed after re-authentication"
        )

    # -- typed-ish helpers used by tools/access control --------------------
    async def list_workspaces(self) -> Any:
        return await self.request("GET", "/api/workspace")

    async def get_folder(
        self, workspace_id: str, depth: int = 20, root_view_id: str | None = None
    ) -> Any:
        params: dict[str, Any] = {"depth": depth}
        if root_view_id:
            params["root_view_id"] = root_view_id
        return await self.request(
            "GET", f"/api/workspace/{workspace_id}/folder", params=params
        )

    # -- collab document access (for in-place block editing) ---------------
    async def get_page_view_raw(self, workspace_id: str, page_id: str) -> Any:
        return await self.request(
            "GET", f"/api/workspace/{workspace_id}/page-view/{page_id}"
        )

    async def post_web_update(
        self, workspace_id: str, object_id: str, update: bytes
    ) -> dict:
        """Publish a Yrs update to a document collab (mirrors the web client)."""
        token = await self._ensure_token()
        url = f"{self._base_url}/api/workspace/v1/{workspace_id}/collab/{object_id}/web-update"
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Version": "2.0.0",
            "Device-Id": "appflowy-mcp",
            "Content-Type": "application/json",
        }
        body = {"doc_state": list(update), "collab_type": 0}
        resp = await self._http.post(url, headers=headers, json=body)
        if resp.status_code == 401:
            token = await self._relogin()
            headers["Authorization"] = f"Bearer {token}"
            resp = await self._http.post(url, headers=headers, json=body)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {"status_code": resp.status_code, "body": resp.text[:300]}
        if resp.status_code >= 400:
            return {"error": f"web-update failed (HTTP {resp.status_code})", "detail": data}
        return data
