from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import get_settings
from app.models import GoogleAccount
from app.services.google_auth import (
    GoogleAuthError,
    GoogleAuthService,
    build_authorization_url,
)
from app.utils.encryption import TokenEncryption
from app.utils.exceptions import TokenExpiredError


class _AccountSession:
    """Minimal session fake that returns ``account`` for any select."""

    def __init__(self, account: GoogleAccount | None) -> None:
        self._account = account
        self.commit_count = 0

    async def execute(self, _stmt: object) -> "_AccountResult":
        return _AccountResult(self._account)

    async def commit(self) -> None:
        self.commit_count += 1


class _AccountResult:
    def __init__(self, account: GoogleAccount | None) -> None:
        self._account = account

    def scalars(self) -> "_AccountResult":
        return self

    def first(self) -> GoogleAccount | None:
        return self._account


def _make_account(
    *,
    expires_in_seconds: int = 3600,
    status: str = "active",
    encryption: TokenEncryption | None = None,
    access_plain: str = "ya29.access",
    refresh_plain: str = "1//refresh",
) -> tuple[GoogleAccount, TokenEncryption]:
    enc = encryption or TokenEncryption(get_settings().encryption_key)
    account = GoogleAccount(
        user_id=uuid.uuid4(),
        access_token_enc=enc.encrypt(access_plain),
        refresh_token_enc=enc.encrypt(refresh_plain),
        token_expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        status=status,
    )
    account.id = uuid.uuid4()
    return account, enc


def _http_factory_returning(response: MagicMock):
    """Return a no-arg factory mimicking ``httpx.AsyncClient()`` for the service."""
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.post = AsyncMock(return_value=response)
    return lambda: client


def _token_response(status_code: int = 200, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body if body is not None else {}
    return resp


# --- is_authorized ----------------------------------------------------------


async def test_is_authorized_true_for_active_account() -> None:
    account, _ = _make_account()
    session = _AccountSession(account)
    service = GoogleAuthService(session)  # type: ignore[arg-type]
    assert await service.is_authorized(account.user_id) is True


async def test_is_authorized_false_for_revoked_account() -> None:
    account, _ = _make_account(status="revoked")
    session = _AccountSession(account)
    service = GoogleAuthService(session)  # type: ignore[arg-type]
    assert await service.is_authorized(account.user_id) is False


async def test_is_authorized_false_when_no_account() -> None:
    session = _AccountSession(None)
    service = GoogleAuthService(session)  # type: ignore[arg-type]
    assert await service.is_authorized(uuid.uuid4()) is False


# --- get_valid_token --------------------------------------------------------


async def test_get_valid_token_returns_decrypted_when_fresh() -> None:
    account, enc = _make_account(expires_in_seconds=3600, access_plain="ya29.fresh")
    session = _AccountSession(account)
    service = GoogleAuthService(session, encryption=enc)  # type: ignore[arg-type]

    token = await service.get_valid_token(account.user_id)
    assert token == "ya29.fresh"
    # Did not need to refresh.
    assert session.commit_count == 0


async def test_get_valid_token_refreshes_when_within_threshold() -> None:
    account, enc = _make_account(expires_in_seconds=60, access_plain="ya29.old")
    session = _AccountSession(account)
    response = _token_response(
        body={"access_token": "ya29.new", "expires_in": 3600}
    )
    service = GoogleAuthService(
        session,  # type: ignore[arg-type]
        encryption=enc,
        http_client_factory=_http_factory_returning(response),
    )

    token = await service.get_valid_token(account.user_id)
    assert token == "ya29.new"
    # Storage updated.
    assert enc.decrypt(account.access_token_enc) == "ya29.new"
    assert account.token_expires_at - datetime.now(UTC) > timedelta(minutes=30)
    assert session.commit_count == 1


async def test_get_valid_token_refreshes_when_already_expired() -> None:
    account, enc = _make_account(expires_in_seconds=-100)
    session = _AccountSession(account)
    response = _token_response(
        body={"access_token": "ya29.refreshed", "expires_in": 3600}
    )
    service = GoogleAuthService(
        session,  # type: ignore[arg-type]
        encryption=enc,
        http_client_factory=_http_factory_returning(response),
    )
    assert await service.get_valid_token(account.user_id) == "ya29.refreshed"


async def test_get_valid_token_raises_when_revoked() -> None:
    account, enc = _make_account(status="revoked")
    session = _AccountSession(account)
    service = GoogleAuthService(session, encryption=enc)  # type: ignore[arg-type]
    with pytest.raises(TokenExpiredError):
        await service.get_valid_token(account.user_id)


async def test_get_valid_token_returns_none_when_no_account() -> None:
    session = _AccountSession(None)
    service = GoogleAuthService(session)  # type: ignore[arg-type]
    assert await service.get_valid_token(uuid.uuid4()) is None


async def test_get_valid_token_marks_revoked_on_refresh_4xx() -> None:
    account, enc = _make_account(expires_in_seconds=10)
    session = _AccountSession(account)
    response = _token_response(status_code=400, body={"error": "invalid_grant"})
    service = GoogleAuthService(
        session,  # type: ignore[arg-type]
        encryption=enc,
        http_client_factory=_http_factory_returning(response),
    )

    with pytest.raises(TokenExpiredError):
        await service.get_valid_token(account.user_id)
    assert account.status == "revoked"
    assert session.commit_count == 1


async def test_get_valid_token_raises_transient_on_network_error() -> None:
    account, enc = _make_account(expires_in_seconds=10)
    session = _AccountSession(account)

    class _ExplodingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            import httpx as _httpx

            raise _httpx.ConnectError("boom")

    service = GoogleAuthService(
        session,  # type: ignore[arg-type]
        encryption=enc,
        http_client_factory=lambda: _ExplodingClient(),
    )

    with pytest.raises(GoogleAuthError):
        await service.get_valid_token(account.user_id)
    # Transient failure must not mark the account revoked.
    assert account.status == "active"


async def test_get_valid_token_marks_revoked_on_corrupted_access_token() -> None:
    account, enc = _make_account(expires_in_seconds=3600)
    # Corrupt the stored ciphertext so decryption fails.
    account.access_token_enc = b"corrupted-ciphertext-bytes"
    session = _AccountSession(account)
    service = GoogleAuthService(session, encryption=enc)  # type: ignore[arg-type]

    with pytest.raises(TokenExpiredError):
        await service.get_valid_token(account.user_id)
    assert account.status == "revoked"


async def test_get_valid_token_marks_revoked_on_corrupted_refresh_token() -> None:
    account, enc = _make_account(expires_in_seconds=10)
    account.refresh_token_enc = b"corrupted"
    session = _AccountSession(account)
    service = GoogleAuthService(session, encryption=enc)  # type: ignore[arg-type]

    with pytest.raises(TokenExpiredError):
        await service.get_valid_token(account.user_id)
    assert account.status == "revoked"


# --- revoke_token -----------------------------------------------------------


async def test_revoke_token_marks_account_revoked() -> None:
    account, enc = _make_account()
    session = _AccountSession(account)
    service = GoogleAuthService(session, encryption=enc)  # type: ignore[arg-type]

    await service.revoke_token(account.user_id)
    assert account.status == "revoked"
    assert session.commit_count == 1


async def test_revoke_token_is_a_noop_when_account_missing() -> None:
    session = _AccountSession(None)
    service = GoogleAuthService(session)  # type: ignore[arg-type]
    await service.revoke_token(uuid.uuid4())  # must not raise
    assert session.commit_count == 0


# --- build_authorization_url ------------------------------------------------


def test_build_authorization_url_includes_required_oauth_params() -> None:
    url = build_authorization_url("34600111222")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "scope=https" in url
    assert "calendar" in url
    assert "state=" in url
    # wa_id not leaked in plaintext
    assert "34600111222" not in url
