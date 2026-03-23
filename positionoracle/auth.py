"""WebAuthn passkey authentication for single-user model."""

from __future__ import annotations

import base64
import datetime
import json
import logging
import secrets
from typing import TYPE_CHECKING

import webauthn
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialType,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from positionoracle.types import CredentialRecord, CredentialStore

logger = logging.getLogger(__name__)

# In-memory challenge store, keyed by a short-lived random token.
_challenges: dict[str, bytes] = {}

# Fixed user ID for the single-user model.
_USER_ID = b"positionoracle-owner"
_USER_NAME = "owner"


# ---------------------------------------------------------------------------
# Credential persistence
# ---------------------------------------------------------------------------


def _creds_path(data_dir: Path) -> Path:
    """Return the path to the credentials JSON file.

    Parameters
    ----------
    data_dir : Path
        Application data directory.

    Returns
    -------
    Path
        Absolute path to ``credentials.json``.
    """
    return data_dir / "credentials.json"


def load_credentials(data_dir: Path) -> CredentialStore:
    """Load registered WebAuthn credentials from disk.

    Parameters
    ----------
    data_dir : Path
        Application data directory.

    Returns
    -------
    CredentialStore
        List of stored credential records (may be empty).
    """
    path = _creds_path(data_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            logger.warning("credentials.json is not a list — returning empty")
            return []
        return raw  # type: ignore[return-value]
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read credentials.json — returning empty")
        return []


def save_credentials(data_dir: Path, creds: CredentialStore) -> None:
    """Persist WebAuthn credentials to disk.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    creds : CredentialStore
        Complete list of credential records to persist.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _creds_path(data_dir)
    path.write_text(json.dumps(creds, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def begin_registration(
    rp_id: str,
    rp_name: str,
    creds: CredentialStore,
) -> tuple[str, str]:
    """Start a WebAuthn registration ceremony.

    Parameters
    ----------
    rp_id : str
        Relying Party ID (domain).
    rp_name : str
        Human-readable Relying Party name.
    creds : CredentialStore
        Existing credentials (used to build the exclude list).

    Returns
    -------
    tuple[str, str]
        ``(options_json, challenge_token)`` where *options_json* is the
        JSON-serialized ``PublicKeyCredentialCreationOptions`` and
        *challenge_token* is a key into the in-memory challenge store.
    """
    exclude = [
        PublicKeyCredentialDescriptor(
            type=PublicKeyCredentialType.PUBLIC_KEY,
            id=base64.urlsafe_b64decode(c["id"] + "=="),
        )
        for c in creds
    ]

    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_name=_USER_NAME,
        user_id=_USER_ID,
        user_display_name="PositionOracle Owner",
        exclude_credentials=exclude or None,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )

    challenge_token = secrets.token_urlsafe(32)
    _challenges[challenge_token] = options.challenge

    options_json: str = webauthn.options_to_json(options)
    return options_json, challenge_token


def complete_registration(
    credential_json: str | Mapping[str, object],
    challenge_token: str,
    rp_id: str,
    expected_origin: str,
    name: str,
) -> CredentialRecord:
    """Verify and return a new credential record from a registration response.

    Parameters
    ----------
    credential_json : str | Mapping[str, object]
        The JSON credential response from the browser.
    challenge_token : str
        Token returned by :func:`begin_registration`.
    rp_id : str
        Relying Party ID (domain).
    expected_origin : str
        Expected origin URL for verification.
    name : str
        Human-readable name for this credential (e.g. "My iPhone").

    Returns
    -------
    CredentialRecord
        The verified credential ready for persistence.

    Raises
    ------
    ValueError
        If the challenge token is invalid or has already been consumed.
    Exception
        Any verification error from the ``webauthn`` library.
    """
    challenge = _challenges.pop(challenge_token, None)
    if challenge is None:
        raise ValueError("Invalid or expired challenge token")

    verification = webauthn.verify_registration_response(
        credential=credential_json,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=expected_origin,
    )

    now = datetime.datetime.now(tz=datetime.UTC).isoformat()
    record: CredentialRecord = {
        "id": base64.urlsafe_b64encode(verification.credential_id).rstrip(b"=").decode(),
        "public_key": base64.urlsafe_b64encode(verification.credential_public_key).decode(),
        "sign_count": verification.sign_count,
        "name": name,
        "registered_at": now,
    }
    return record


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def begin_authentication(
    rp_id: str,
    creds: CredentialStore,
) -> tuple[str, str]:
    """Start a WebAuthn authentication ceremony.

    Parameters
    ----------
    rp_id : str
        Relying Party ID (domain).
    creds : CredentialStore
        Registered credentials (used to build the allow list).

    Returns
    -------
    tuple[str, str]
        ``(options_json, challenge_token)`` — the JSON options to send to the
        browser and the challenge key for later verification.
    """
    allow = [
        PublicKeyCredentialDescriptor(
            type=PublicKeyCredentialType.PUBLIC_KEY,
            id=base64.urlsafe_b64decode(c["id"] + "=="),
        )
        for c in creds
    ]

    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow or None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    challenge_token = secrets.token_urlsafe(32)
    _challenges[challenge_token] = options.challenge

    options_json: str = webauthn.options_to_json(options)
    return options_json, challenge_token


def complete_authentication(
    credential_json: str | dict[str, object],
    challenge_token: str,
    rp_id: str,
    expected_origin: str,
    creds: CredentialStore,
) -> CredentialRecord | None:
    """Verify an authentication response and update sign count.

    Parameters
    ----------
    credential_json : str | dict[str, object]
        The JSON credential response from the browser.
    challenge_token : str
        Token returned by :func:`begin_authentication`.
    rp_id : str
        Relying Party ID (domain).
    expected_origin : str
        Expected origin URL for verification.
    creds : CredentialStore
        All registered credentials (searched for the matching one).

    Returns
    -------
    CredentialRecord | None
        The matched credential with updated ``sign_count``, or ``None`` if
        verification fails.
    """
    challenge = _challenges.pop(challenge_token, None)
    if challenge is None:
        logger.warning("Invalid or expired authentication challenge token")
        return None

    if isinstance(credential_json, str):
        cred_data: dict[str, object] = json.loads(credential_json)
    else:
        cred_data = dict(credential_json)

    raw_id = str(cred_data.get("rawId", cred_data.get("id", "")))

    matched: CredentialRecord | None = None
    for c in creds:
        if c["id"] == raw_id:
            matched = c
            break

    if matched is None:
        logger.warning("No registered credential matches rawId=%s", raw_id)
        return None

    try:
        verification = webauthn.verify_authentication_response(
            credential=credential_json,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=expected_origin,
            credential_public_key=base64.urlsafe_b64decode(matched["public_key"] + "=="),
            credential_current_sign_count=matched["sign_count"],
        )
    except Exception:
        logger.exception("Authentication verification failed")
        return None

    matched["sign_count"] = verification.new_sign_count
    return matched
