"""Google OIDC auth for a public Apo MCP endpoint.

Each persona has its own Google OAuth client registration. This server is a
resource server (``RemoteAuthProvider``): the identity provider is the
authorization server, and this module verifies the bearer token on each call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import AnyHttpUrl

from fastmcp.server.auth import (
    AccessToken,
    AuthProvider,
    JWTVerifier,
    RemoteAuthProvider,
    TokenVerifier,
)

logger = logging.getLogger(__name__)

GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]


class AuthConfigError(Exception):
    pass


@dataclass(frozen=True)
class Persona:
    name: str
    client_id: str
    client_secret: str | None
    allowed_emails: frozenset[str]


@dataclass(frozen=True)
class AuthSettings:
    personas: tuple[Persona, ...]
    issuer: list[str]
    base_url: str


def load_persona_config(config_path: Path) -> AuthSettings:
    """Load and validate the ``~/.apo/mcp-auth.json``-shaped persona config.

    See ``docs/systemd/apo-mcp.service.example`` for the schema with
    placeholder values — real client secrets never belong in this repo.
    """
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise AuthConfigError(f"cannot read auth config {config_path}: {e}") from e
    except json.JSONDecodeError as e:
        raise AuthConfigError(f"invalid JSON in auth config {config_path}: {e}") from e

    personas_raw = raw.get("personas")
    if not personas_raw:
        raise AuthConfigError(f"auth config {config_path} has no personas")

    personas = []
    for p in personas_raw:
        try:
            personas.append(
                Persona(
                    name=p["name"],
                    client_id=p["client_id"],
                    client_secret=p.get("client_secret"),
                    allowed_emails=frozenset(e.lower() for e in p["allowed_emails"]),
                )
            )
        except KeyError as e:
            raise AuthConfigError(f"persona entry missing {e} in {config_path}") from e

    issuer_raw = raw.get("issuer") or GOOGLE_ISSUERS[0]
    issuer = GOOGLE_ISSUERS if issuer_raw in GOOGLE_ISSUERS else [issuer_raw]

    base_url = raw.get("base_url")
    if not base_url:
        raise AuthConfigError(f"auth config {config_path} missing base_url")

    return AuthSettings(personas=tuple(personas), issuer=issuer, base_url=base_url)


class PersonaGoogleTokenVerifier(JWTVerifier):
    """Validates a Google-issued OIDC ID token against >=1 client registrations.

    Wraps FastMCP's stock ``JWTVerifier`` — which already enforces
    signature, issuer, audience-in-set, and expiry — with the one check it
    can't express: which persona's *own* client registration the token's
    ``aud`` names, and whether the token's ``email`` is on THAT persona's
    allowlist. That second check is what makes two separate Google OAuth
    clients safe to accept on one endpoint: a token whose ``aud`` names one
    persona's client but whose ``email`` belongs to the other person is
    rejected, even though the base verifier trusts that ``aud`` value.
    """

    def __init__(
        self,
        *,
        personas: tuple[Persona, ...],
        issuer: list[str],
        jwks_uri: str,
        http_client: httpx.AsyncClient | None = None,
        base_url: AnyHttpUrl | str | None = None,
    ):
        if not personas:
            raise AuthConfigError("at least one persona is required")
        super().__init__(
            jwks_uri=jwks_uri,
            issuer=issuer,
            audience=[p.client_id for p in personas],
            algorithm="RS256",
            base_url=base_url,
            http_client=http_client,
        )
        self._by_client_id = {p.client_id: p for p in personas}

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await self.load_access_token(token)
        if access is None:
            return None

        aud = access.claims.get("aud")
        candidates = aud if isinstance(aud, list) else [aud]
        persona = next(
            (self._by_client_id[a] for a in candidates if a in self._by_client_id),
            None,
        )
        if persona is None:
            logger.warning(
                "Bearer token rejected: aud %r matches no configured persona", aud
            )
            return None

        email = (access.claims.get("email") or "").lower()
        if not email or email not in persona.allowed_emails:
            logger.warning(
                "Bearer token rejected for persona %s: email %r not allow-listed",
                persona.name,
                email,
            )
            return None

        access.claims["persona"] = persona.name
        access.scopes = sorted(set(access.scopes) | {f"persona:{persona.name}"})
        return access


def _metadata_issuer(issuer: list[str]) -> str:
    for u in issuer:
        if u.startswith("http://") or u.startswith("https://"):
            return u
    return "https://accounts.google.com"


def build_auth_provider(
    config_path: Path | str,
    *,
    base_url_override: str | None = None,
    issuer_override: list[str] | None = None,
    jwks_uri_override: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> RemoteAuthProvider:
    """Build the FastMCP ``auth=`` provider for the portal MCP endpoint.

    ``issuer_override``/``jwks_uri_override``/``http_client`` exist so tests
    (and, if ever needed, a non-Google dev IdP) can point verification at a
    mocked OIDC provider instead of the real accounts.google.com.
    """
    settings = load_persona_config(Path(config_path).expanduser())
    base_url = base_url_override or settings.base_url
    issuer = issuer_override or settings.issuer

    verifier = PersonaGoogleTokenVerifier(
        personas=settings.personas,
        issuer=issuer,
        jwks_uri=jwks_uri_override or GOOGLE_JWKS_URI,
        http_client=http_client,
        base_url=base_url,
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(_metadata_issuer(issuer))],
        base_url=base_url,
        resource_name=os.environ.get("APO_MCP_RESOURCE_NAME", "Apo MCP"),
    )


class StaticBearerTokenVerifier(TokenVerifier):
    """Shared-secret bearer auth for machine clients on a private network.

    The tokens file maps a client name to the SHA-256 hex digest of its token
    (``{"clients": {"optima-service": {"sha256": "<hex>"}}}``), so the file on
    disk never holds a usable secret. Each client keeps its own plaintext
    token and can be revoked by deleting its entry and restarting the server.

    An entry may also carry a vault scope —
    ``{"sha256": ..., "vaults": ["atlas", "grid"], "default_vault": "atlas"}``
    — surfaced as access-token claims (``apo_vaults`` / ``apo_default_vault``)
    and enforced by :class:`apo_engine.vault_scope.VaultScopeMiddleware`.
    Entries without ``vaults`` are unscoped, exactly as before.

    An entry with an ``admin`` policy (see :mod:`apo_engine.token_admin`) is
    an *admin* client: it is accepted only by :meth:`verify_admin` (the
    ``/admin/clients`` API) and refused on ``/mcp``, so an admin token is never
    also an unscoped MCP client.

    Built with ``tokens_path`` (:meth:`from_file`), the verifier re-reads the
    file when it changes (mtime/size/inode), so issuing or revoking a client
    needs no restart. A changed file that fails to parse is logged and the
    previous clients stay in force. A file that disappears revokes every
    client until it returns.
    """

    def __init__(
        self,
        digests: dict[str, str],
        scopes: dict[str, dict] | None = None,
        admins: dict[str, Any] | None = None,
        tokens_path: Path | str | None = None,
    ):
        super().__init__()
        if not digests:
            raise AuthConfigError("token auth needs at least one client digest")
        self._set(digests, scopes or {}, admins or {})
        self._path = Path(tokens_path).expanduser() if tokens_path else None
        self._stamp = self._stat()

    @classmethod
    def from_file(cls, tokens_path: Path | str) -> "StaticBearerTokenVerifier":
        p = Path(tokens_path)
        return cls(load_token_digests(p), load_token_scopes(p), load_token_admins(p), tokens_path=p)

    def _set(self, digests: dict[str, str], scopes: dict[str, dict], admins: dict[str, Any]) -> None:
        self._digests = {name: d.lower() for name, d in digests.items()}
        self._scopes = scopes
        self._admins = admins

    def _stat(self):
        if self._path is None:
            return None
        try:
            st = os.stat(self._path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _maybe_reload(self) -> None:
        if self._path is None:
            return
        stamp = self._stat()
        if stamp == self._stamp:
            return
        self._stamp = stamp
        if stamp is None:
            # The file is gone (or unreadable). Fail closed: deleting the
            # tokens file must revoke, not leave the last clients in force.
            # They come back when the file does (its stamp changes again).
            self._set({}, {}, {})
            logger.error("tokens file %s is missing; refusing every client until it returns", self._path)
            return
        try:
            digests = load_token_digests(self._path)
            scopes = load_token_scopes(self._path)
            admins = load_token_admins(self._path)
            if not digests:
                raise AuthConfigError("no clients")
        except AuthConfigError as exc:
            logger.error("tokens file changed but is invalid; keeping the previous clients: %s", exc)
            return
        self._set(digests, scopes, admins)
        logger.info("token clients reloaded: %d client(s)", len(digests))

    def _match(self, token: str) -> str | None:
        self._maybe_reload()
        presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for name, digest in self._digests.items():
            if hmac.compare_digest(presented, digest):
                return name
        return None

    async def verify_token(self, token: str) -> AccessToken | None:
        name = self._match(token)
        if name is None:
            logger.warning("Bearer token rejected: no matching client digest")
            return None
        if name in self._admins:
            logger.warning("Bearer token rejected on /mcp: %s is an admin client", name)
            return None
        scope = self._scopes.get(name) or {}
        claims = (
            {"apo_vaults": list(scope["vaults"]), "apo_default_vault": scope["default_vault"]}
            if scope.get("vaults")
            else {}
        )
        return AccessToken(token=token, client_id=name, scopes=[], claims=claims)

    def verify_admin(self, token: str) -> tuple[str, Any] | None:
        """``(client name, AdminPolicy)`` for an admin token, else None."""
        name = self._match(token)
        if name is None or name not in self._admins:
            return None
        return name, self._admins[name]


def _read_token_clients(tokens_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(tokens_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthConfigError(f"cannot read tokens file {tokens_path}: {exc}") from exc
    clients = raw.get("clients") if isinstance(raw, dict) else None
    if not isinstance(clients, dict):
        raise AuthConfigError("tokens file needs a top-level 'clients' object")
    return clients


def load_token_digests(tokens_path: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for name, entry in _read_token_clients(tokens_path).items():
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(digest, str) or len(digest) != 64:
            raise AuthConfigError(f"client {name!r} needs a 64-char 'sha256' digest")
        digests[name] = digest
    return digests


def load_token_scopes(tokens_path: Path) -> dict[str, dict]:
    """Per-client vault scopes: ``{name: {"vaults": [...], "default_vault": str}}``.

    Only clients whose entry has ``vaults`` appear. ``default_vault`` defaults
    to the first listed vault and must be one of them.
    """
    scopes: dict[str, dict] = {}
    for name, entry in _read_token_clients(tokens_path).items():
        if not isinstance(entry, dict) or "vaults" not in entry:
            continue
        vaults = entry["vaults"]
        if (
            not isinstance(vaults, list)
            or not vaults
            or not all(isinstance(v, str) and v.strip() for v in vaults)
        ):
            raise AuthConfigError(f"client {name!r}: 'vaults' must be a non-empty list of vault ids")
        vaults = [v.strip() for v in vaults]
        default = (entry.get("default_vault") or vaults[0]).strip()
        if default not in vaults:
            raise AuthConfigError(f"client {name!r}: default_vault {default!r} is not in its vaults")
        scopes[name] = {"vaults": vaults, "default_vault": default}
    return scopes


def load_token_admins(tokens_path: Path) -> dict[str, Any]:
    """Admin clients: ``{name: AdminPolicy}`` for entries with an ``admin`` policy."""
    from apo_engine.token_admin import AdminPolicy

    admins: dict[str, Any] = {}
    for name, entry in _read_token_clients(tokens_path).items():
        if not isinstance(entry, dict) or "admin" not in entry:
            continue
        try:
            admins[name] = AdminPolicy.parse(name, entry["admin"])
        except ValueError as exc:
            raise AuthConfigError(str(exc)) from exc
    return admins


def build_auth_provider_from_env() -> AuthProvider | None:
    """Construct the auth provider from ``APO_MCP_AUTH`` and its config env.

    ``APO_MCP_AUTH=google`` (+ ``APO_MCP_AUTH_CONFIG``) is the public portal
    endpoint; ``APO_MCP_AUTH=token`` (+ ``APO_MCP_TOKENS_FILE``) is static
    bearer auth for machine clients reaching a tailnet-exposed instance.

    Returns ``None`` when auth is not enabled (``APO_MCP_AUTH`` unset) so the
    existing no-auth stdio/loopback path is unaffected — every other client
    (Claude Code, Cursor, each Hermes gateway) keeps working exactly as
    before.
    """
    kind = (os.environ.get("APO_MCP_AUTH") or "").strip().lower()
    if not kind:
        return None
    if kind == "token":
        tokens_path = os.environ.get("APO_MCP_TOKENS_FILE")
        if not tokens_path:
            raise AuthConfigError("APO_MCP_AUTH=token requires APO_MCP_TOKENS_FILE")
        return StaticBearerTokenVerifier.from_file(Path(tokens_path))
    if kind != "google":
        raise AuthConfigError(
            f"unsupported APO_MCP_AUTH={kind!r}; expected 'google' or 'token'"
        )

    config_path = os.environ.get("APO_MCP_AUTH_CONFIG")
    if not config_path:
        raise AuthConfigError("APO_MCP_AUTH=google requires APO_MCP_AUTH_CONFIG")

    issuer_override = None
    if os.environ.get("APO_MCP_AUTH_ISSUER"):
        issuer_override = [os.environ["APO_MCP_AUTH_ISSUER"]]

    return build_auth_provider(
        config_path,
        base_url_override=os.environ.get("APO_MCP_AUTH_BASE_URL"),
        issuer_override=issuer_override,
        jwks_uri_override=os.environ.get("APO_MCP_AUTH_JWKS_URI"),
    )
