"""Thin client for the Lumen embedding service."""

from __future__ import annotations

from typing import Any

import httpx


class EmbedClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout_s: float = 60.0) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s, headers=headers)

    async def embed(self, inputs: list[str], *, model: str | None = None, input_type: str = "document",
                    dimensions: int | None = None) -> list[list[float]]:
        body: dict[str, Any] = {"inputs": inputs, "input_type": input_type}
        if model:
            body["model"] = model
        if dimensions:
            body["dimensions"] = dimensions
        r = await self._http.post("/v1/embed", json=body)
        r.raise_for_status()
        return r.json()["embeddings"]  # type: ignore[no-any-return]

    async def aclose(self) -> None:
        await self._http.aclose()
