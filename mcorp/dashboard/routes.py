"""Thin HTTP controllers for server-rendered dashboard pages."""

from __future__ import annotations

import hmac
import secrets
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from .auth import AccessDenied, DiscordOAuthClient, OAuthError
from .permissions import current_user, require_admin, require_csrf
from .services import DashboardService


def build_router(service: DashboardService, oauth: DiscordOAuthClient) -> APIRouter:
    router = APIRouter()

    def render(request: Request, template: str, **context):
        user = None
        try:
            user = current_user(request)
        except HTTPException:
            pass
        return request.app.state.templates.TemplateResponse(
            request=request, name=template,
            context={
                "user": user, "csrf_token": request.session.get("csrf_token", ""),
                "notice": request.session.pop("notice", None), **context,
            },
        )

    def redirect_after_action(request: Request, location: str, notice: str):
        request.session["notice"] = notice
        return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/login", name="login")
    async def login(request: Request):
        return render(request, "login.html")

    @router.get("/auth/discord", name="discord_login")
    async def discord_login(request: Request):
        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state
        return RedirectResponse(oauth.authorization_url(state), status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/auth/callback", name="discord_callback")
    async def discord_callback(request: Request, code: str, state: str):
        expected = request.session.pop("oauth_state", "")
        if not expected or not hmac.compare_digest(expected, state):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "État OAuth invalide")
        try:
            identity = await oauth.exchange(code)
            user = oauth.authorize(identity)
        except AccessDenied as exc:
            return render(request, "denied.html", message=str(exc))
        except OAuthError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        request.session.clear()
        service.remember_discord_identity(user.discord_id, user.username)
        request.session["identity"] = {
            "discord_id": user.discord_id, "username": user.username, "role": user.role.value,
        }
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/logout", name="logout")
    async def logout(request: Request, csrf_token: Annotated[str, Form()]):
        current_user(request)
        require_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/", name="dashboard")
    async def dashboard(request: Request):
        current_user(request)
        return render(request, "dashboard.html", overview=service.overview())

    @router.get("/shops", name="shops")
    async def shops(request: Request):
        current_user(request)
        return render(request, "shops.html", reports=service.shop_reports())

    @router.post("/shops", name="create_shop")
    async def create_shop(
        request: Request, shop_id: Annotated[str, Form(pattern=r"^[A-Z0-9_]+$")],
        display_name: Annotated[str, Form(min_length=1, max_length=100)],
        owner_type: Annotated[str, Form(pattern=r"^(USER|SYSTEM)$")],
        owner_id: Annotated[str, Form(min_length=1, max_length=40)],
        reason: Annotated[str, Form(min_length=3, max_length=300)],
        csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.create_shop(user, shop_id, display_name, owner_type, owner_id, reason)
        return redirect_after_action(request, f"/shops/{shop_id}", "Boutique créée.")

    @router.get("/shops/{shop_id}", name="shop_detail")
    async def shop_detail(request: Request, shop_id: str):
        current_user(request)
        return render(request, "shop_detail.html", **service.shop_detail_data(shop_id))

    @router.post("/shops/{shop_id}/configure", name="configure_shop")
    async def configure_shop(
        request: Request, shop_id: str,
        display_name: Annotated[str, Form(min_length=1, max_length=100)],
        license_required: Annotated[bool, Form()], license_fee: Annotated[int, Form(ge=0)],
        license_period_days: Annotated[int, Form(gt=0, le=3650)],
        commission_bps: Annotated[int, Form(ge=0, le=10000)],
        payment_methods: Annotated[str, Form(pattern=r"^(BANK|CASH|BANK,CASH|CASH,BANK)$")],
        auto_restock_allowed: Annotated[bool, Form()],
        visibility: Annotated[str, Form(pattern=r"^(PUBLIC|PRIVATE|HIDDEN)$")],
        restock_fee_bps: Annotated[int, Form(ge=0, le=10000)],
        reason: Annotated[str, Form(min_length=3, max_length=300)],
        csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.configure_shop(
            user, shop_id, reason, display_name=display_name,
            license_required=license_required, license_fee=license_fee,
            license_period_days=license_period_days, commission_bps=commission_bps,
            payment_methods=tuple(payment_methods.split(",")),
            auto_restock_allowed=auto_restock_allowed, visibility=visibility,
            restock_fee_bps=restock_fee_bps,
        )
        return redirect_after_action(request, f"/shops/{shop_id}", "Configuration enregistrée.")

    @router.post("/shops/{shop_id}/suspension", name="shop_suspension")
    async def shop_suspension(
        request: Request, shop_id: str,
        suspended: Annotated[bool, Form()], reason: Annotated[str, Form(min_length=3, max_length=300)],
        csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.suspend_shop(user, shop_id, suspended, reason)
        return redirect_after_action(request, f"/shops/{shop_id}", "Statut de la boutique mis à jour.")

    @router.get("/stocks", name="stocks")
    async def stocks(request: Request):
        current_user(request)
        return render(request, "stocks.html", **service.stock_data())

    @router.get("/production", name="production")
    async def production(request: Request):
        current_user(request)
        return RedirectResponse("/wholesale", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/production", name="produce")
    async def produce(
        request: Request, item_id: Annotated[str, Form()],
        quantity: Annotated[int, Form(gt=0, le=1_000_000)],
        reason: Annotated[str, Form(min_length=3, max_length=300)],
        csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.produce(user, item_id, quantity, reason)
        return redirect_after_action(request, "/wholesale", "Production enregistrée.")

    @router.post("/production/schedules", name="create_schedule")
    async def create_schedule(
        request: Request, item_id: Annotated[str, Form()],
        quantity: Annotated[int, Form(gt=0, le=1_000_000)],
        weekday: Annotated[int, Form(ge=0, le=6)], time_utc: Annotated[str, Form(pattern=r"^\d{2}:\d{2}$")],
        reason: Annotated[str, Form(min_length=3, max_length=300)], csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.create_schedule(user, item_id, quantity, weekday, time_utc, reason)
        return redirect_after_action(request, "/wholesale", "Production hebdomadaire créée.")

    @router.post("/production/schedules/{schedule_id}", name="update_schedule")
    async def update_schedule(
        request: Request, schedule_id: str,
        quantity: Annotated[int, Form(gt=0, le=1_000_000)],
        weekday: Annotated[int, Form(ge=0, le=6)], time_utc: Annotated[str, Form(pattern=r"^\d{2}:\d{2}$")],
        enabled: Annotated[bool, Form()], reason: Annotated[str, Form(min_length=3, max_length=300)],
        csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.update_schedule(
            user, schedule_id, quantity=quantity, weekday=weekday,
            time_utc=time_utc, enabled=enabled, reason=reason,
        )
        return redirect_after_action(request, "/wholesale", "Production hebdomadaire modifiée.")

    @router.post("/production/schedules/{schedule_id}/archive", name="archive_schedule")
    async def archive_schedule(
        request: Request, schedule_id: str, reason: Annotated[str, Form(min_length=3, max_length=300)],
        csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.archive_schedule(user, schedule_id, reason)
        return redirect_after_action(request, "/wholesale", "Production hebdomadaire archivée.")

    @router.get("/wholesale", name="wholesale")
    async def wholesale(request: Request, shop_id: str | None = Query(None)):
        current_user(request)
        return render(request, "wholesale.html", **service.wholesale_data(shop_id))

    @router.post("/wholesale", name="update_wholesale")
    async def update_wholesale(
        request: Request, item_id: Annotated[str, Form()], price: Annotated[int, Form(ge=0)],
        available: Annotated[bool, Form()], minimum_order: Annotated[int, Form(gt=0)],
        maximum_order: Annotated[int | None, Form()] = None,
        reason: Annotated[str, Form(min_length=3, max_length=300)] = "mise à jour dashboard",
        csrf_token: Annotated[str, Form()] = "",
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.set_wholesale(user, item_id, price, available, minimum_order, maximum_order, reason)
        return redirect_after_action(request, "/wholesale", "Catalogue grossiste mis à jour.")

    @router.get("/permissions", name="permissions")
    async def permissions(request: Request):
        current_user(request)
        return RedirectResponse("/wholesale", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/permissions", name="update_permission")
    async def update_permission(
        request: Request, shop_id: Annotated[str, Form()], item_id: Annotated[str, Form()],
        allowed: Annotated[bool, Form()], reason: Annotated[str, Form(min_length=3, max_length=300)],
        csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.set_permission(user, shop_id, item_id, allowed, reason)
        return redirect_after_action(request, f"/wholesale?shop_id={shop_id}", "Droit d’achat mis à jour.")

    @router.post("/shops/{shop_id}/permissions", name="update_shop_permission")
    async def update_shop_permission(
        request: Request, shop_id: str, item_id: Annotated[str, Form()],
        allowed: Annotated[bool, Form()], reason: Annotated[str, Form(min_length=3, max_length=300)],
        csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.set_permission(user, shop_id, item_id, allowed, reason)
        return redirect_after_action(request, f"/shops/{shop_id}", "Droit d’achat mis à jour.")

    @router.get("/restock", name="restock")
    async def restock(request: Request):
        current_user(request)
        return RedirectResponse("/wholesale", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/restock", name="update_restock")
    async def update_restock(
        request: Request, shop_id: Annotated[str, Form()], item_id: Annotated[str, Form()],
        enabled: Annotated[bool, Form()], threshold: Annotated[int, Form(ge=0)],
        target_stock: Annotated[int, Form(gt=0)], max_unit_price: Annotated[int, Form(ge=0)],
        max_total_spend: Annotated[int, Form(ge=0)],
        reason: Annotated[str, Form(min_length=3, max_length=300)], csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.set_restock_policy(
            user, shop_id, item_id, enabled=enabled, threshold=threshold,
            target_stock=target_stock, max_unit_price=max_unit_price,
            max_total_spend=max_total_spend, reason=reason,
        )
        return redirect_after_action(request, f"/wholesale?shop_id={shop_id}", "Politique de restock enregistrée.")

    @router.post("/shops/{shop_id}/restock", name="update_shop_restock")
    async def update_shop_restock(
        request: Request, shop_id: str, item_id: Annotated[str, Form()],
        enabled: Annotated[bool, Form()], threshold: Annotated[int, Form(ge=0)],
        target_stock: Annotated[int, Form(gt=0)], max_unit_price: Annotated[int, Form(ge=0)],
        max_total_spend: Annotated[int, Form(ge=0)],
        reason: Annotated[str, Form(min_length=3, max_length=300)], csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.set_restock_policy(
            user, shop_id, item_id, enabled=enabled, threshold=threshold,
            target_stock=target_stock, max_unit_price=max_unit_price,
            max_total_spend=max_total_spend, reason=reason,
        )
        return redirect_after_action(request, f"/shops/{shop_id}", "Politique de restock enregistrée.")

    @router.get("/licenses", name="licenses")
    async def licenses(request: Request):
        current_user(request)
        return RedirectResponse("/shops", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/licenses/{shop_id}/pay", name="pay_license")
    async def pay_license(
        request: Request, shop_id: str, reason: Annotated[str, Form(min_length=3, max_length=300)],
        csrf_token: Annotated[str, Form()],
    ):
        user = require_admin(request)
        require_csrf(request, csrf_token)
        service.pay_license(user, shop_id, reason)
        return redirect_after_action(request, f"/shops/{shop_id}", "Licence payée ou renouvelée.")

    @router.get("/audit", name="audit")
    async def audit(
        request: Request, actor_id: str | None = Query(None), shop_id: str | None = Query(None),
        event_type: str | None = Query(None), event_status: str | None = Query(None),
        since: str | None = Query(None), user_id: str | None = Query(None),
        transaction_id: str | None = Query(None),
    ):
        current_user(request)
        events = service.audit(
            actor_id=actor_id, shop_id=shop_id, event_type=event_type,
            status=event_status, since=since, user_id=user_id,
            transaction_id=transaction_id,
        )
        return render(request, "audit.html", events=events)

    @router.get("/alerts", name="alerts")
    async def alerts(request: Request):
        current_user(request)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/admin", name="admin")
    async def admin(request: Request):
        user = require_admin(request)
        settings = request.app.state.settings
        safe_config = {
            "guild_id": settings.discord_guild_id,
            "moderator_role_ids": sorted(settings.moderator_role_ids),
            "oauth_configured": True,
            "scheduler_enabled": settings.scheduler_enabled,
            "database_exists": settings.database_path.exists(),
            "version": "dashboard-v1",
        }
        return render(request, "admin.html", user=user, config=safe_config)

    return router
