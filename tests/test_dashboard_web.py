"""HTTP, OAuth, RBAC and SSR tests for the admin dashboard."""

from __future__ import annotations

import re
import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import httpx

from mcorp.dashboard.app import create_app
from mcorp.dashboard.auth import AccessDenied, DiscordOAuthClient, OAuthIdentity
from mcorp.dashboard.config import DashboardSettings
from mcorp.identities import DiscordIdentityStore
from mcorp.dashboard.services import DashboardService
from mcorp.dashboard.scheduler import ProductionScheduleStore
from mcorp.control import EconomyControlService
from mcorp.inventory import InventoryOwner, InventoryStore
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
            "owner": OAuthIdentity("100", "Owner", True, frozenset(), "Propriétaire"),
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
        self.assertIn("Production enregistrée", self.client.get(response.headers["location"]).text)
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
        self.assertRegex(text, r"RTC couverts</span><strong>\d+ RTC")
        self.assertIn("Réconciliation Red Bank", text)
        self.assertIn("Produit par M Corp", text)
        self.assertIn("objets créés officiellement", text)

    def test_navigation_only_contains_v1_pages(self) -> None:
        self.login()
        text = self.client.get("/").text
        navigation = re.search(r'<nav[^>]*>(.*?)</nav>', text, re.DOTALL)
        assert navigation is not None
        labels = re.findall(r'<a[^>]*>([^<]+)</a>', navigation.group(1))
        self.assertEqual(
            labels,
            ["Vue générale", "Boutiques", "Stocks", "Grossiste", "Audit", "Administration"],
        )
        for removed in ("Production", "Permissions", "Restock", "Licences", "Alertes"):
            self.assertNotIn(f">{removed}<", navigation.group(1))

    def test_shops_list_page(self) -> None:
        self.login()
        self.assertIn("Boutique Lucas", self.client.get("/shops").text)

    def test_shop_detail_page(self) -> None:
        self.login()
        text = self.client.get("/shops/LUCAS_STORE").text
        for section in ("Informations générales", "Compte / stock", "Licence", "Approvisionnement"):
            self.assertIn(section, text)
        self.assertIn("Stock inclus avec la licence", text)
        self.assertIn('/shops/LUCAS_STORE/permissions', text)
        self.assertIn('/shops/LUCAS_STORE/restock', text)

    def test_moderator_shop_detail_is_read_only(self) -> None:
        self.login("moderator")
        text = self.client.get("/shops/LUCAS_STORE").text
        self.assertIn("Approvisionnement", text)
        self.assertNotIn('/shops/LUCAS_STORE/permissions', text)
        self.assertNotIn('/shops/LUCAS_STORE/restock', text)

    def test_stocks_page(self) -> None:
        self.login()
        text = self.client.get("/stocks").text
        self.assertIn("Par objet", text)
        self.assertIn("Par propriétaire", text)
        self.assertIn("Produit par M Corp", text)
        self.assertIn("Entrepôt M Corp", text)

    def test_stocks_use_known_discord_nickname(self) -> None:
        self.service.control.inventory.add_item(
            InventoryOwner("USER", "42"), "maro", 1, administrator_id=100
        )
        self.service.remember_discord_identity("42", "Lucas")
        self.login()
        text = self.client.get("/stocks").text
        self.assertIn("Lucas", text)
        self.assertNotIn(">42<", text)

    def test_wholesale_page(self) -> None:
        self.login("moderator")
        text = self.client.get("/wholesale").text
        self.assertIn("Entrepôt M Corp", text)
        self.assertIn("Catalogue grossiste", text)
        self.assertIn("Approvisionnement des boutiques", text)
        self.assertNotIn('action="/production"', text)

    def test_permissions_page(self) -> None:
        self.login("moderator")
        response = self.client.get("/permissions", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/wholesale"))

    def test_licenses_page(self) -> None:
        self.login("moderator")
        response = self.client.get("/licenses", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/shops"))

    def test_restock_page(self) -> None:
        self.login("moderator")
        response = self.client.get("/restock", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/wholesale"))

    def test_audit_page(self) -> None:
        self.login("moderator")
        self.assertIn("SHOP_CREATED", self.client.get("/audit").text)

    def test_alerts_page(self) -> None:
        self.login("moderator")
        response = self.client.get("/alerts", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/"))
        self.assertIn("RECONCILIATION_UNKNOWN", self.client.get("/").text)

    def test_production_page_is_admin_only(self) -> None:
        self.login("moderator")
        response = self.client.get("/production", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/wholesale"))
        self.assertNotIn('action="/production"', self.client.get("/wholesale").text)

    def test_admin_can_update_and_disable_schedule_from_html(self) -> None:
        self.login("owner")
        token = self.csrf()
        created = self.client.post("/production/schedules", data={
            "item_id": "maro", "quantity": "2", "weekday": "1", "time_utc": "09:30",
            "reason": "planning initial", "csrf_token": token,
        }, follow_redirects=False)
        self.assertEqual(created.status_code, 303)
        schedule = self.service.schedules.list(INSTITUTION)[0]
        page = self.client.get("/wholesale").text
        self.assertIn(f'/production/schedules/{schedule.schedule_id}', page)
        updated = self.client.post(f"/production/schedules/{schedule.schedule_id}", data={
            "quantity": "7", "weekday": "4", "time_utc": "15:45", "enabled": "false",
            "reason": "ajustement planning", "csrf_token": self.csrf(),
        }, follow_redirects=False)
        self.assertEqual(updated.status_code, 303)
        current = self.service.schedules.get(schedule.schedule_id)
        assert current is not None
        self.assertEqual((current.quantity, current.weekday, current.time_utc, current.enabled), (7, 4, "15:45", False))

    def test_shop_supply_actions_remain_admin_only_server_side(self) -> None:
        self.login("moderator")
        response = self.client.post("/shops/LUCAS_STORE/permissions", data={
            "item_id": "maro", "allowed": "true", "reason": "test lecture seule",
            "csrf_token": self.csrf(),
        })
        self.assertEqual(response.status_code, 403)

    def test_oauth_login_remembers_server_nickname(self) -> None:
        self.login("owner")
        self.assertEqual(self.service.identities.known_nickname("100"), "Propriétaire")

    def test_identity_migration_is_additive_and_idempotent(self) -> None:
        import sqlite3

        store = DiscordIdentityStore(self.path)
        store.initialize()
        store.initialize()
        with sqlite3.connect(self.path) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            columns = {row[1] for row in connection.execute("PRAGMA table_info(discord_identities)")}
        self.assertIn("ledger_transactions", tables)
        self.assertIn("inventory_transactions", tables)
        self.assertIn("control_events", tables)
        self.assertEqual(
            {"discord_id", "known_nickname", "imposed_nickname", "previous_nickname", "rename_expires_at", "updated_at"},
            columns,
        )

    def test_known_nickname_update_preserves_future_rename_fields(self) -> None:
        import sqlite3

        self.service.identities.remember_known_nickname("42", "Lucas")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """UPDATE discord_identities SET imposed_nickname = ?, previous_nickname = ?,
                   rename_expires_at = ? WHERE discord_id = ?""",
                ("Capitaine", "Lucas", "2030-01-01T00:00:00+00:00", "42"),
            )
        self.service.identities.remember_known_nickname("42", "Lucas Actif")
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """SELECT known_nickname, imposed_nickname, previous_nickname, rename_expires_at
                   FROM discord_identities WHERE discord_id = ?""",
                ("42",),
            ).fetchone()
        self.assertEqual(
            row,
            ("Lucas Actif", "Capitaine", "Lucas", "2030-01-01T00:00:00+00:00"),
        )

    def test_audit_unifies_control_ledger_and_inventory(self) -> None:
        self.service.control.ledger.record_cash_event(
            INSTITUTION, "ADMIN_CREDIT", 1, (42,), {"administrator_id": 100}
        )
        self.service.control.inventory.add_item(
            InventoryOwner("USER", "42"), "maro", 1, administrator_id=100
        )
        self.login("moderator")
        text = self.client.get("/audit").text
        self.assertIn("Contrôle", text)
        self.assertIn("Banque", text)
        self.assertIn("Objets", text)

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


class DashboardEnvironmentTests(unittest.TestCase):
    def test_dotenv_is_loaded_without_being_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / ".env").write_text(
                "MCORP_DISCORD_CLIENT_ID=dotenv-client\n"
                "MCORP_DISCORD_CLIENT_SECRET=dotenv-secret\n"
                "MCORP_DISCORD_REDIRECT_URI=http://localhost/callback\n"
                "MCORP_DISCORD_GUILD_ID=dotenv-guild\n"
                "MCORP_SESSION_SECRET=abcdefghijklmnopqrstuvwxyz123456\n"
                f"MCORP_DATABASE_PATH={path / 'test.sqlite3'}\n",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(path)
                with patch.dict(os.environ, {}, clear=True):
                    settings = DashboardSettings.from_env()
                    ledger = LedgerStore(settings.database_path)
                    ledger.initialize()
                    ledger.ensure_institution(settings.institution_id, "M Corp", 999)
                    ledger.mark_initialized(settings.institution_id, 999, 0)
                    application = create_app(settings)
                    client = ASGIClient(application)
                    try:
                        health = client.get("/health")
                    finally:
                        client.close()
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(settings.discord_client_id, "dotenv-client")
        self.assertEqual(health.json()["status"], "ok")

    def test_exported_environment_has_priority_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / ".env").write_text(
                "MCORP_DISCORD_CLIENT_ID=dotenv-client\n",
                encoding="utf-8",
            )
            exported = {
                "MCORP_DISCORD_CLIENT_ID": "exported-client",
                "MCORP_DISCORD_CLIENT_SECRET": "exported-secret",
                "MCORP_DISCORD_REDIRECT_URI": "http://localhost/callback",
                "MCORP_DISCORD_GUILD_ID": "guild",
                "MCORP_SESSION_SECRET": "abcdefghijklmnopqrstuvwxyz123456",
                "MCORP_DATABASE_PATH": str(path / "test.sqlite3"),
            }
            previous_cwd = Path.cwd()
            try:
                os.chdir(path)
                with patch.dict(os.environ, exported, clear=True):
                    settings = DashboardSettings.from_env()
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(settings.discord_client_id, "exported-client")


if __name__ == "__main__":
    unittest.main()
