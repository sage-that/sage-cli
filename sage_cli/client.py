"""
HTTP SSE client for Sage backend API.

Streams Server-Sent Events from the backend's agent endpoints
and translates them into the event format the CLI renderer expects.
"""

import json
from typing import AsyncIterator, Optional

import httpx

from .auth import get_auth_header_or_die


class SageBackendError(Exception):
    pass


class SageBackend:
    """HTTP client for Sage backend streaming API."""

    def __init__(
        self,
        base_url: str,
        user_id: str,
        auth_token: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self._auth_token = auth_token
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def auth_token(self) -> str:
        if self._auth_token is None:
            header = get_auth_header_or_die()
            self._auth_token = header.replace("Bearer ", "")
        return self._auth_token

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.auth_token}"},
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def stream_chat(
        self,
        text: str,
        *,
        deep_mode: bool = False,
        mobile_mode: bool = True,
        journal_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """Stream a chat response from POST /sage/{userId}/thought/stream."""
        client = await self._get_client()
        url = f"{self.base_url}/sage/{self.user_id}/thought/stream"
        body = {"text": text, "deep_mode": deep_mode, "mobile_mode": mobile_mode}
        if journal_prompt:
            body["journal_prompt"] = journal_prompt
        if session_id:
            body["session_id"] = session_id

        async with client.stream("POST", url, json=body) as response:
            if response.status_code in (401, 403):
                raise SageBackendError(
                    "Authentication failed. Run 'sage login' to re-authenticate."
                )
            if response.status_code >= 400:
                body_text = await response.aread()
                raise SageBackendError(
                    f"Backend error {response.status_code}: {body_text.decode()[:200]}"
                )
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    try:
                        yield json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

    async def resume_session(
        self,
        session_id: str,
        selected_index: Optional[int] = None,
        user_input: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """Resume a deferred session via POST /sage/{userId}/session/{sessionId}/resume."""
        client = await self._get_client()
        url = f"{self.base_url}/sage/{self.user_id}/session/{session_id}/resume"
        body = {}
        if selected_index is not None:
            body["selected_index"] = selected_index
        if user_input:
            body["user_input"] = user_input

        async with client.stream("POST", url, json=body) as response:
            if response.status_code >= 400:
                body_text = await response.aread()
                raise SageBackendError(
                    f"Resume error {response.status_code}: {body_text.decode()[:200]}"
                )
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    try:
                        yield json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

    async def get_thoughts(self, limit: int = 50, days: Optional[int] = None) -> list:
        client = await self._get_client()
        params = {"limit": limit}
        if days:
            params["days"] = days
        response = await client.get(
            f"{self.base_url}/sage/{self.user_id}/thoughts", params=params
        )
        response.raise_for_status()
        return response.json().get("content", [])

    async def search_thoughts(self, query: str, limit: int = 10) -> list:
        client = await self._get_client()
        response = await client.post(
            f"{self.base_url}/sage/{self.user_id}/thoughts/search",
            json={"query": query, "limit": limit},
        )
        response.raise_for_status()
        return response.json().get("content", [])

    async def get_insight_patterns(self, days: int = 30) -> dict:
        client = await self._get_client()
        params = {}
        if days:
            params["days"] = days
        response = await client.get(
            f"{self.base_url}/sage/{self.user_id}/insights/patterns", params=params
        )
        response.raise_for_status()
        return response.json().get("content", {})
