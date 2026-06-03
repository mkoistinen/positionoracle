"""Generate and verify API keys for the REST API.

Design
------
- The cleartext key is shown to the user *once* at generation time and
  never stored.
- We persist only the SHA-256 hex digest plus an 8-character prefix.
  The prefix lets the user identify a key in the management UI
  without exposing the secret.
- Verification hashes the incoming key with SHA-256 and uses
  ``hmac.compare_digest`` for constant-time comparison.

Key format: ``po_<43-char-urlsafe-base64>``. The ``po_`` prefix makes
keys grep-friendly in logs and prevents accidental confusion with
external-service tokens. 32 random bytes give 256 bits of entropy.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_KEY_PREFIX_TAG = "po_"
_PREFIX_DISPLAY_LEN = 8
_ENTROPY_BYTES = 32


def generate_key() -> tuple[str, str, str]:
    """Generate a new API key.

    Returns
    -------
    tuple[str, str, str]
        ``(cleartext, sha256_hex, display_prefix)`` where:

        - ``cleartext`` is the secret to hand to the user (shown once).
        - ``sha256_hex`` is the value to persist for future lookup.
        - ``display_prefix`` is the first 8 characters of ``cleartext``,
          stored verbatim for identification in management lists.
    """
    token = secrets.token_urlsafe(_ENTROPY_BYTES)
    cleartext = f"{_KEY_PREFIX_TAG}{token}"
    digest = hashlib.sha256(cleartext.encode("utf-8")).hexdigest()
    display_prefix = cleartext[:_PREFIX_DISPLAY_LEN]
    return cleartext, digest, display_prefix


def hash_key(cleartext: str) -> str:
    """Return the SHA-256 hex digest of a cleartext key."""
    return hashlib.sha256(cleartext.encode("utf-8")).hexdigest()


def verify_key(cleartext: str, stored_hash: str) -> bool:
    """Constant-time compare a cleartext key to a stored hash.

    Parameters
    ----------
    cleartext : str
        The key received in the request.
    stored_hash : str
        The SHA-256 hex digest pulled from the database.

    Returns
    -------
    bool
        True if the keys match.
    """
    candidate = hash_key(cleartext)
    return hmac.compare_digest(candidate, stored_hash)
