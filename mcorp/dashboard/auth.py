"""Discord OAuth2 client and authorization policy."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .config import DashboardSettings
from .permissions import DashboardRole, DashboardUser


DISCORD_API = "https://discord.com/api/v10"


class OAuthError(RuntimeError):
    pass


class AccessDenied(OAuthError):
    pass


@dataclass(frozen=True)
class OAuthIdentity:
    user_id: str
    username: str
    guild_owner: bool
    role_ids: frozenset[str]


class DiscordOAuthClient:
    def __init__(self, settings: DashboardSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client

    def authorization_url(self, state: str) -> str:
        query = urlencode({
            "client_id": self.settings.discord_client_id,
            "redirect_uri": self.settings.discord_redirect_uri,
            "response_type": "code",
            "scope": "identify guilds guilds.members.read",
            "state": state,
        })
        return f"https://discord.com/oauth2/authorize?{query}"

    async def exchange(self, code: str) -> OAuthIdentity:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=10)
        try:
            token_response = await client.post(
                f"{DISCORD_API}/oauth2/token",
                data={
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": self.settings.discord_redirect_uri,
                    "client_id": self.settings.discord_client_id,
                    "client_secret": self.settings.discord_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_response.raise_for_status()
            access_token = token_response.json()["access_token"]
            headers = {"Authorization": f"Bearer {access_token}"}
            user_response = await client.get(f"{DISCORD_API}/users/@me", headers=headers)
            guilds_response = await client.get(f"{DISCORD_API}/users/@me/guilds", headers=headers)
            member_response = await client.get(
                f"{DISCORD_API}/users/@me/guilds/{self.settings.discord_guild_id}/member",
                headers=headers,
            )
            user_response.raise_for_status()
            guilds_response.raise_for_status()
            if member_response.status_code == 404:
                raise AccessDenied("L’utilisateur n’est pas membre du serveur configuré")
            member_response.raise_for_status()
            user = user_response.json()
            guild = next(
                (item for item in guilds_response.json() if str(item.get("id")) == self.settings.discord_guild_id),
                None,
            )
            if guild is None:
                raise AccessDenied("Serveur Discord configuré introuvable")
            member = member_response.json()
            return OAuthIdentity(
                str(user["id"]), str(user.get("global_name") or user["username"]),
                bool(guild.get("owner")), frozenset(str(role) for role in member.get("roles", [])),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise OAuthError("Échec de la validation Discord OAuth2") from exc
        finally:
            if owns_client:
                await client.aclose()

    def authorize(self, identity: OAuthIdentity) -> DashboardUser:
        if identity.guild_owner:
            role = DashboardRole.ADMIN
        elif identity.role_ids & self.settings.moderator_role_ids:
            role = DashboardRole.MODERATOR
        else:
            raise AccessDenied("Aucun rôle dashboard autorisé")
        return DashboardUser(identity.user_id, identity.username, role)
