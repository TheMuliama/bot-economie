"""Small asynchronous HTTP boundary for the MCorp API.

The cog has no economic persistence: every balance, inventory and mutation is
authoritative only after the API confirms it. Mutation retries reuse their
caller-provided operation id, which is the API idempotency key.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


SHOP_OWNER = {"owner_type": "BOT", "owner_id": "mcorp-shop"}


class MCorpApiError(Exception):
    """An API failure safe to show as a concise Discord error."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(message)


class MCorpApiConfigurationError(MCorpApiError):
    pass


@dataclass(frozen=True)
class MCorpApiConfig:
    base_url: str
    api_key: str
    timeout_seconds: float = 10.0

    @classmethod
    def from_environment(cls) -> "MCorpApiConfig":
        key = os.environ.get("MCORP_BOT_API_KEY", "").strip()
        if not key:
            raise MCorpApiConfigurationError("MCORP_BOT_API_KEY n'est pas configurée.")
        base_url = os.environ.get("MCORP_API_URL", "http://127.0.0.1:8000").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise MCorpApiConfigurationError("MCORP_API_URL doit être une URL HTTP(S).")
        try:
            timeout = float(os.environ.get("MCORP_API_TIMEOUT_SECONDS", "10"))
        except ValueError as exc:
            raise MCorpApiConfigurationError("MCORP_API_TIMEOUT_SECONDS est invalide.") from exc
        if timeout <= 0:
            raise MCorpApiConfigurationError("MCORP_API_TIMEOUT_SECONDS doit être positif.")
        return cls(base_url, key, timeout)


class MCorpApiClient:
    """Typed endpoint helpers; API rules deliberately stay on the server."""

    def __init__(self, config: MCorpApiConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def wallet(self, owner_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/wallets/USER/{quote(owner_id, safe='')}")

    async def inventory(self, owner: dict[str, str]) -> dict[str, Any]:
        return await self._request(
            "GET", f"/inventories/{owner['owner_type']}/{quote(owner['owner_id'], safe='')}"
        )

    async def transfer(self, source_id: str, destination_id: str, amount: int, operation_id: str) -> dict[str, Any]:
        return await self._mutation("/wallets/transfer", {
            "source": _user(source_id), "destination": _user(destination_id),
            "amount": amount, "operation_id": operation_id,
        })

    async def exchange(self, buyer_id: str, item_id: str, quantity: int, total_price: int, operation_id: str) -> dict[str, Any]:
        return await self._mutation("/exchanges", {
            "buyer": _user(buyer_id), "seller": SHOP_OWNER, "item_id": item_id,
            "quantity": quantity, "total_price": total_price, "operation_id": operation_id,
        })

    async def consume(self, owner_id: str, item_id: str, quantity: int, operation_id: str) -> dict[str, Any]:
        return await self._mutation("/inventories/consume", {
            "owner": _user(owner_id), "item_id": item_id, "quantity": quantity,
            "operation_id": operation_id,
        })

    async def _mutation(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        # A single automatic retry is safe because operation_id is unchanged.
        for attempt in range(2):
            try:
                return await self._request("POST", path, payload)
            except MCorpApiError as exc:
                if not exc.retryable or attempt:
                    raise
                await asyncio.sleep(0)
        raise AssertionError("unreachable")

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            raise MCorpApiError("Le service M Corp ne répond pas ; réessayez.", retryable=True) from exc
        except httpx.RequestError as exc:
            raise MCorpApiError("Le service M Corp est indisponible ; réessayez.", retryable=True) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_success:
            if not isinstance(body, dict):
                raise MCorpApiError("Réponse M Corp invalide.")
            return body
        detail = body.get("detail") if isinstance(body, dict) else None
        messages = {
            401: "Le bot n'est pas autorisé auprès de M Corp.",
            403: "Cette opération n'est pas autorisée.",
            404: "Ressource M Corp introuvable.",
            409: "Opération refusée : fonds, stock ou état insuffisant.",
            422: "Données d'opération invalides.",
        }
        message = messages.get(response.status_code, "Le service M Corp a rencontré une erreur.")
        if isinstance(detail, str) and "Insufficient" in detail:
            message = "Opération refusée : fonds ou stock insuffisant."
        raise MCorpApiError(message, status_code=response.status_code, retryable=response.status_code >= 500)


def _user(discord_id: str) -> dict[str, str]:
    return {"owner_type": "USER", "owner_id": str(discord_id)}
