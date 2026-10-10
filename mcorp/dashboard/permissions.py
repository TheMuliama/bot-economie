"""Dashboard identities and server-side RBAC dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from fastapi import HTTPException, Request, status


class DashboardRole(StrEnum):
    ADMIN = "ADMIN"
    MODERATOR = "MODERATOR"


@dataclass(frozen=True)
class DashboardUser:
    discord_id: str
    username: str
    role: DashboardRole


def current_user(request: Request) -> DashboardUser:
    identity = request.session.get("identity")
    if not isinstance(identity, dict):
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    try:
        return DashboardUser(
            discord_id=str(identity["discord_id"]),
            username=str(identity["username"]),
            role=DashboardRole(str(identity["role"])),
        )
    except (KeyError, ValueError, TypeError) as exc:
        request.session.clear()
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}) from exc


def require_admin(request: Request) -> DashboardUser:
    user = current_user(request)
    if user.role is not DashboardRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Action réservée au propriétaire du serveur")
    return user


def require_csrf(request: Request, submitted_token: str) -> None:
    from hmac import compare_digest

    expected = request.session.get("csrf_token")
    if not isinstance(expected, str) or not compare_digest(expected, submitted_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Jeton CSRF invalide")
