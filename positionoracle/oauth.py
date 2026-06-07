"""OAuth 2.1 server primitives — discovery payloads, token helpers, PKCE.

Implements the same minimum subset of OAuth as the sibling Exercise
app:

- **Authorization Code + PKCE (S256)** for public clients registered
  via Dynamic Client Registration (e.g. Claude Cowork).
- **Client Credentials** for confidential clients minted from the
  management UI (SDK / CLI / cron use).

The route handlers themselves live in ``main.py``; this module is the
stateless toolbox they call into.

Tokens
------
We issue opaque random tokens (32 bytes of urlsafe base64 ≈ 256 bits).
The cleartext is returned to the client once at issue time; only the
SHA-256 hex digest is persisted, so a database read never gives anyone
a usable token.

PKCE
----
Per RFC 7636 §4.2, the ``S256`` method requires
``BASE64URL(SHA256(code_verifier)) == code_challenge``. We accept only
``S256`` — ``plain`` is allowed by the RFC but disallowed by OAuth 2.1.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Lifetime of an authorization code (seconds). Spec recommends ≤10 min.
AUTH_CODE_TTL = 600

#: Lifetime of a freshly issued access token.
ACCESS_TOKEN_TTL = 3600  # 1 hour

#: Lifetime of a refresh token.
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days

#: The only scope we currently understand. Add more by widening this.
DEFAULT_SCOPE = "mcp"

#: Bytes of entropy per opaque token.
_TOKEN_ENTROPY_BYTES = 32

#: Prefix on client-credentials secrets, makes them grep-friendly.
CLIENT_SECRET_PREFIX = "po_cs_"

#: Prefix on client identifiers issued via DCR + management UI.
CLIENT_ID_PREFIX = "po_cid_"

#: How many characters of the cleartext secret we store for display.
SECRET_DISPLAY_PREFIX_LEN = 8


# ---------------------------------------------------------------------------
# Token + identifier generation
# ---------------------------------------------------------------------------


def _opaque_token() -> str:
    """Return a 256-bit urlsafe-base64 random string."""
    return secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)


def hash_token(cleartext: str) -> str:
    """Return the SHA-256 hex digest of a token."""
    return hashlib.sha256(cleartext.encode("utf-8")).hexdigest()


def generate_client_id() -> str:
    """Mint a new public ``client_id``."""
    return f"{CLIENT_ID_PREFIX}{secrets.token_urlsafe(16)}"


def generate_client_secret() -> tuple[str, str, str]:
    """Mint a confidential client's ``client_secret``.

    Returns
    -------
    tuple[str, str, str]
        ``(cleartext, sha256_hex, display_prefix)``. Hand the cleartext
        to the user once; persist the hash + 8-char prefix.
    """
    token = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    cleartext = f"{CLIENT_SECRET_PREFIX}{token}"
    digest = hash_token(cleartext)
    display_prefix = cleartext[:SECRET_DISPLAY_PREFIX_LEN]
    return cleartext, digest, display_prefix


def generate_authorization_code() -> str:
    """Mint a one-shot authorization code."""
    return _opaque_token()


def generate_access_token() -> tuple[str, str]:
    """Mint an access token. Returns ``(cleartext, sha256_hex)``."""
    token = _opaque_token()
    return token, hash_token(token)


def generate_refresh_token() -> tuple[str, str]:
    """Mint a refresh token. Returns ``(cleartext, sha256_hex)``."""
    token = _opaque_token()
    return token, hash_token(token)


def verify_client_secret(cleartext: str, stored_hash: str) -> bool:
    """Constant-time compare a presented secret to its stored hash."""
    return hmac.compare_digest(hash_token(cleartext), stored_hash)


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def verify_pkce_s256(code_verifier: str, code_challenge: str) -> bool:
    """Check the RFC 7636 §4.2 ``S256`` PKCE relation.

    ``BASE64URL-NOPAD(SHA256(ASCII(code_verifier))) == code_challenge``.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, code_challenge)


# ---------------------------------------------------------------------------
# Discovery payloads
# ---------------------------------------------------------------------------


def authorization_server_metadata(issuer: str) -> dict[str, Any]:
    """Build the RFC 8414 metadata document.

    Parameters
    ----------
    issuer : str
        The HTTPS origin of this server, e.g. ``https://positionoracle.com``.
        Do not include a trailing slash.
    """
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "revocation_endpoint": f"{issuer}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
            "client_credentials",
        ],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none",                # public clients (PKCE)
            "client_secret_post",  # confidential clients
            "client_secret_basic", # confidential clients
        ],
        "scopes_supported": [DEFAULT_SCOPE],
        "service_documentation": f"{issuer}/docs",
    }


def protected_resource_metadata(issuer: str) -> dict[str, Any]:
    """Build the RFC 9728 protected-resource metadata document.

    Tells MCP clients which authorization server to talk to for the
    ``/mcp`` resource.
    """
    return {
        "resource": f"{issuer}/mcp",
        "authorization_servers": [issuer],
        "scopes_supported": [DEFAULT_SCOPE],
        "bearer_methods_supported": ["header"],
    }


# ---------------------------------------------------------------------------
# Helpers shared across route handlers
# ---------------------------------------------------------------------------


def split_scope(scope: str | None) -> list[str]:
    """Parse a space-separated OAuth scope string."""
    if not scope:
        return []
    return [s for s in scope.split(" ") if s]


def normalize_scope(requested: str | None) -> str:
    """Return the granted scope, intersecting *requested* with what we support.

    Currently we only know ``mcp`` — anything else is silently dropped.
    Empty request → default scope (``mcp``).
    """
    supported = {DEFAULT_SCOPE}
    asked = set(split_scope(requested))
    if not asked:
        return DEFAULT_SCOPE
    granted = asked & supported
    return " ".join(sorted(granted)) if granted else DEFAULT_SCOPE
