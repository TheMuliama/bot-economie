"""HTTP, OAuth, RBAC and SSR tests for the admin dashboard."""

from __future__ import annotations

import re
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from mcorp.dashboard.app import create_app
from mcorp.dashboard.auth import AccessDenied, DiscordOAuthClient, OAuthIdentity
from mcorp.dashboard.config import DashboardSettings
from mcorp.dashboard.services import DashboardService
from mcorp.dashboard.scheduler import ProductionScheduleStore
from mcorp.control import EconomyControlService
from mcorp.inventory import InventoryStore
from mcorp.inventory_service import InventoryService
from mcorp.ledger import LedgerStore
from mcorp.shops import ShopStore


INSTITUTION = "mcorp:guild:1"


class ASGIClient:
    """Synchronous unittest adapter around HTTPX's in-process ASGI transport."""

    def __init__(self, app) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    def get(self, url: str, **kwargs):
        return asyncio.run(self.client.get(url, **kwargs))

    def post(self, url: str, **kwargs):
        return asyncio.run(self.client.post(url, **kwargs))

    def close(self) -> None:
        asyncio.run(self.client.aclose())


class FakeOAuth:
    def __init__(self, settings: DashboardSettings) -> None:
        self.policy = DiscordOAuthClient(settings)

    def authorization_url(self, state: str) -> str:
        return f"https://discord.test/authorize?state={state}"

    async def exchange(self, code: str) -> OAuthIdentity:
        if code == "outside":
            raise AccessDenied("hors serveur")
        identities = {
            "owner": OAuthIdentity("100", "Owner", True, frozenset()),
            "moderator": OAuthIdentity("200", "Moderator", False, frozenset({"555"})),
            "normal": OAuthIdentity("300", "Member", False, frozenset({"777"})),
        }
        return identities[code]

    def authorize(self, identity: OAuthIdentity):
        return self.policy.authorize(identity)


class DashboardWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.base_path = Path(cls.tempdir.name) / "dashboard-fixture.sqlite3"
        ledger = LedgerStore(cls.base_path)
        ledger.initialize()
        ledger.ensure_institution(INSTITUTION, "M Corp", 999)
        ledger.mark_initialized(INSTITUTION, 999, 0)
        service = DashboardService.from_database(cls.base_path, INSTITUTION)
        service.control.create_shop(
            INSTITUTION, "LUCAS_STORE", "Boutique Lucas", "USER", "42",
            administrator_id=100, license_required=True,
            auto_restock_allowed=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def setUp(self) -> None:
        self.path = Path(self.tempdir.name) / f"{self._testMethodName}.sqlite3"
        shutil.copyfile(self.base_path, self.path)
        self.settings = DashboardSettings(
            discord_client_id="public-client-id",
            discord_client_secret="never-render-this-secret",
            discord_redirect_uri="http://127.0.0.1:8000/auth/callback",
            discord_guild_id="123",
            moderator_role_ids=frozenset({"555"}),
            session_secret="s" * 48,
            database_path=self.path,
            institution_id=INSTITUTION,
            cookie_secure=False,
            scheduler_enabled=False,
        )
        self.service = DashboardService(
            EconomyControlService(
                LedgerStore(self.path), InventoryService(InventoryStore(self.path)),
                ShopStore(self.path),
            ),
            ProductionScheduleStore(self.path),
            INSTITUTION,
        )
        self.app = create_app(self.settings, self.service, FakeOAuth(self.settings))
        self.client = ASGIClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.path.unlink(missing_ok=True)

    def login(self, code: str = "owner"):
        begin = self.client.get("/auth/discord", follow_redirects=False)
        state = parse_qs(urlparse(begin.headers["location"]).query)["state"][0]
        return self.client.get(
            "/auth/callback", params={"code": code, "state": state},
            follow_redirects=False,
        )

    def csrf(self) -> str:
        response = self.client.get("/")
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        return match.group(1)

    def test_valid_discord_login_creates_session(self) -> None:
        response = self.login("owner")
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/"))
        self.assertIn("mcorp_dashboard", response.headers["set-cookie"])

    def test_user_outside_guild_is_refused(self) -> None:
        response = self.login("outside")
        self.assertEqual(response.status_code, 200)
        self.assertIn("hors serveur", response.text)

    def test_guild_owner_becomes_admin(self) -> None:
        self.login("owner")
        self.assertIn("ADMIN", self.client.get("/").text)

    def test_configured_role_becomes_moderator(self) -> None:
        self.login("moderator")
        self.assertIn("MODERATOR", self.client.get("/").text)

    def test_normal_member_is_refused(self) -> None:
        response = self.login("normal")
        self.assertIn("Aucun rôle dashboard autorisé", response.text)

    def test_invalid_oauth_state_is_refused(self) -> None:
        self.client.get("/auth/discord")
        response = self.client.get(
            "/auth/callback", params={"code": "owner", "state": "wrong"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)

    def test_protected_route_without_session_redirects(self) -> None:
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/login"))

    def test_moderator_cannot_use_admin_action(self) -> None:
        self.login("moderator")
        response = self.client.post(
            "/production", data={"item_id": "immunity", "quantity": "1", "reason": "test", "csrf_token": "x"},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_use_critical_production_action(self) -> None:
        self.login("owner")
        response = self.client.post(
            "/production", data={
                "item_id": "immunity", "quantity": "3",
                "reason": "stock test", "csrf_token": self.csrf(),
            }, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        metric = next(item for item in self.service.control.item_metrics() if item.item_id == "immunity")
        self.assertEqual(metric.mcorp_stock, 3)

    def test_production_is_audited_with_actor_role_and_reason(self) -> None:
        self.login("owner")
        self.client.post("/production", data={
            "item_id": "maro", "quantity": "2", "reason": "préparation",
            "csrf_token": self.csrf(),
        })
        event = self.service.audit(event_type="DASHBOARD_PRODUCTION")[0]
        self.assertEqual(event.actor_id, "100")
        self.assertEqual(event.metadata["actor_role"], "ADMIN")
        self.assertEqual(event.metadata["reason"], "préparation")

    def test_csrf_is_checked_server_side(self) -> None:
        self.login("owner")
        response = self.client.post("/production", data={
            "item_id": "maro", "quantity": "2", "reason": "test",
            "csrf_token": "forged",
        })
        self.assertEqual(response.status_code, 403)

    def test_invalid_production_quantity_is_rejected(self) -> None:
        self.login("owner")
        response = self.client.post("/production", data={
            "item_id": "maro", "quantity": "0", "reason": "test",
            "csrf_token": self.csrf(),
        })
        self.assertEqual(response.status_code, 422)

    def test_session_cookie_is_http_only_and_same_site(self) -> None:
        response = self.login("owner")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)

    def test_dashboard_overview_page(self) -> None:
        self.login()
        text = self.client.get("/").text
        self.assertIn("RTC couverts", text)
        self.assertIn("Réconciliation Red Bank", text)

    def test_shops_list_page(self) -> None:
        self.login()
        self.assertIn("Boutique Lucas", self.client.get("/shops").text)

    def test_shop_detail_page(self) -> None:
        self.login()
        text = self.client.get("/shops/LUCAS_STORE").text
        self.assertIn("Compte", text)
        self.assertIn("Auto-restock", text)

    def test_stocks_page(self) -> None:
        self.login()
        text = self.client.get("/stocks").text
        self.assertIn("Par objet", text)
        self.assertIn("Par propriétaire", text)

    def test_wholesale_page(self) -> None:
        self.login("moderator")
        self.assertEqual(self.client.get("/wholesale").status_code, 200)

    def test_permissions_page(self) -> None:
        self.login("moderator")
        self.assertIn("Permissions d’achat", self.client.get("/permissions").text)

    def test_licenses_page(self) -> None:
        self.login("moderator")
        text = self.client.get("/licenses").text
        self.assertIn("Boutique officielle M Corp", text)
        self.assertIn("NOT_REQUIRED", text)

    def test_restock_page(self) -> None:
        self.login("moderator")
        self.assertIn("Restock", self.client.get("/restock").text)

    def test_audit_page(self) -> None:
        self.login("moderator")
        self.assertIn("SHOP_CREATED", self.client.get("/audit").text)

    def test_alerts_page(self) -> None:
        self.login("moderator")
        self.assertIn("RECONCILIATION_UNKNOWN", self.client.get("/alerts").text)

    def test_production_page_is_admin_only(self) -> None:
        self.login("moderator")
        self.assertEqual(self.client.get("/production").status_code, 403)

    def test_admin_page_never_renders_secrets(self) -> None:
        self.login("owner")
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("never-render-this-secret", response.text)
        self.assertNotIn("s" * 48, response.text)

    def test_healthcheck_exposes_no_secret(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.json()["status"], "ok")
        self.assertNotIn("secret", response.text.casefold())

    def test_routes_do_not_contain_sql_writes(self) -> None:
        routes = (Path(__file__).parents[1] / "mcorp" / "dashboard" / "routes.py").read_text()
        normalized = routes.upper()
        self.assertNotIn("INSERT INTO", normalized)
        self.assertNotIn("UPDATE ", normalized)
        self.assertNotIn("DELETE FROM", normalized)
        self.assertNotIn("SQLITE", normalized)


if __name__ == "__main__":
    unittest.main()
