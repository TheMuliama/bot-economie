"""HTTP boundary tests: no Discord server and no API database are required."""

from __future__ import annotations

import json
import unittest

import httpx

from mcorp.api_client import MCorpApiClient, MCorpApiConfig, MCorpApiError


class ApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_transfer_sends_bearer_owner_payload_and_operation_id(self) -> None:
        seen: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers["Authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"source_balance": 75, "destination_balance": 25})

        client = MCorpApiClient(MCorpApiConfig("https://api.test", "secret"), transport=httpx.MockTransport(handler))
        result = await client.transfer("1", "2", 25, "fixed-operation")
        await client.aclose()
        self.assertEqual(result["source_balance"], 75)
        self.assertEqual(seen["authorization"], "Bearer secret")
        self.assertEqual(seen["body"], {
            "source": {"owner_type": "USER", "owner_id": "1"},
            "destination": {"owner_type": "USER", "owner_id": "2"},
            "amount": 25, "operation_id": "fixed-operation",
        })

    async def test_timeout_retries_with_same_operation_id(self) -> None:
        requests: list[dict[str, object]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            raise httpx.ReadTimeout("simulated", request=request)

        client = MCorpApiClient(MCorpApiConfig("https://api.test", "secret"), transport=httpx.MockTransport(handler))
        with self.assertRaises(MCorpApiError) as raised:
            await client.exchange("1", "potion", 1, 200, "retry-same-id")
        await client.aclose()
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(requests), 2)
        self.assertEqual({request["operation_id"] for request in requests}, {"retry-same-id"})

    async def test_api_conflict_is_a_clear_non_retryable_error(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"detail": "Insufficient funds"})

        client = MCorpApiClient(MCorpApiConfig("https://api.test", "secret"), transport=httpx.MockTransport(handler))
        with self.assertRaises(MCorpApiError) as raised:
            await client.consume("1", "potion", 1, "consume-id")
        await client.aclose()
        self.assertFalse(raised.exception.retryable)
        self.assertIn("fonds ou stock insuffisant", str(raised.exception))
