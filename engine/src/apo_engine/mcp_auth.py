"""Multi-persona Google OIDC auth for a public desma MCP endpoint.

Two people (Jeremy and Fotini) each have their own Google OAuth client
registration in a shared GCP project. Rather than run this server as a
FastMCP ``OAuthProxy`` (which is built around exactly one upstream
client_id/secret — see ``GoogleProvider`` — and so can't natively serve two
client registrations from one ``/authorize`` redirect), this module makes
the server a plain OAuth *resource* server (``RemoteAuthProvider``): Google
itself is the authorization server the MCP client talks to directly (using
whichever persona's client_id/secret it was configured with), and this
module only verifies the bearer ID token FastMCP receives on every tool
call, mapping it to a persona via the client_id in its ``aud`` claim.

That split is what makes "two Google OAuth clients, one endpoint" possible
without forking FastMCP's OAuth server code, and it's why every check here
is expressible as pure JWT validation — testable with a mocked JWKS
endpoint, no network or real GCP registration required (see
tests/test_mcp_auth_google.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

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

    See ``docs/systemd/apo-desma-mcp.service.example`` for the schema with
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
    """Build the FastMCP ``auth=`` provider for the desma MCP endpoint.

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
        resource_name="Apo Desma MCP",
    )


class StaticBearerTokenVerifier(TokenVerifier):
    """Shared-secret bearer auth for machine clients on a private network.

    The tokens file maps a client name to the SHA-256 hex digest of its token
    (``{"clients": {"optima-service": {"sha256": "<hex>"}}}``), so the file on
    disk never holds a usable secret. Each client keeps its own plaintext
    token and can be revoked by deleting its entry and restarting the server.
    """

    def __init__(self, digests: dict[str, str]):
        super().__init__()
        if not digests:
            raise AuthConfigError("token auth needs at least one client digest")
        self._digests = {name: d.lower() for name, d in digests.items()}

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for name, digest in self._digests.items():
            if hmac.compare_digest(presented, digest):
                return AccessToken(token=token, client_id=name, scopes=[])
        logger.warning("Bearer token rejected: no matching client digest")
        return None


def load_token_digests(tokens_path: Path) -> dict[str, str]:
    try:
        raw = json.loads(Path(tokens_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthConfigError(f"cannot read tokens file {tokens_path}: {exc}") from exc
    clients = raw.get("clients") if isinstance(raw, dict) else None
    if not isinstance(clients, dict):
        raise AuthConfigError("tokens file needs a top-level 'clients' object")
    digests: dict[str, str] = {}
    for name, entry in clients.items():
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(digest, str) or len(digest) != 64:
            raise AuthConfigError(f"client {name!r} needs a 64-char 'sha256' digest")
        digests[name] = digest
    return digests


def build_auth_provider_from_env() -> AuthProvider | None:
    """Construct the auth provider from ``APO_MCP_AUTH`` and its config env.

    ``APO_MCP_AUTH=google`` (+ ``APO_MCP_AUTH_CONFIG``) is the public desma
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
        return StaticBearerTokenVerifier(load_token_digests(Path(tokens_path)))
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
