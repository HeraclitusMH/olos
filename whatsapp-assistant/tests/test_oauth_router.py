from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import GoogleAccount, User
from app.utils.encryption import TokenEncryption
from app.utils.oauth_state import OAuthStateCodec


# --- helpers / fakes --------------------------------------------------------


class FakeResult:
    """Minimal async-result shim for an injected fake AsyncSession."""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value

    def scalars(self) -> "FakeResult":
        return self

    def first(self) -> object:
        return self._value


class FakeSession:
    """Stand-in for :class:`AsyncSession` that records ORM activity in memory.

    The OAuth callback's only DB interactions are: select User by wa_id, select
    GoogleAccount by user_id, ``add`` either, and ``flush``/``commit``. We model
    each with a queue of canned results.
    """

    def __init__(
        self,
        user: User | None = None,
        account: GoogleAccount | None = None,
    ) -> None:
        self._user = user
        self._account = account
        self._select_calls = 0
        self.added: list[object] = []
        self.committed = False

    async def execute(self, _stmt: object) -> FakeResult:
        self._select_calls += 1
        # Per the callback's order: user lookup first, then account lookup.
        if self._select_calls == 1:
            return FakeResult(self._user)
        return FakeResult(self._account)

    def add(self, obj: object) -> None:
        self.added.append(obj)
        # Approximate a flush() that assigns a PK to a newly-added User.
        if isinstance(obj, User) and obj.id is None:
            obj.id = uuid.uuid4()
            self._user = obj

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def _override_db(session: FakeSession):
    async def _dep() -> AsyncGenerator[FakeSession, None]:
        yield session

    return _dep


@pytest.fixture
async def http_client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- /oauth/google/authorize ------------------------------------------------


async def test_authorize_returns_google_url_with_required_params(
    http_client: AsyncClient,
) -> None:
    response = await http_client.get(
        "/oauth/google/authorize", params={"wa_id": "34600111222"}
    )
    assert response.status_code == 200

    body = response.json()
    auth_url = body["authorization_url"]
    parsed = urlparse(auth_url)
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"

    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert qs["client_id"] == "google-client-id-test"
    assert qs["redirect_uri"] == "https://example.com/oauth/google/callback"
    assert qs["response_type"] == "code"
    assert qs["access_type"] == "offline"
    assert qs["prompt"] == "consent"
    assert "calendar" in qs["scope"]
    assert qs["state"]
    # The state must not leak the wa_id in plaintext.
    assert "34600111222" not in qs["state"]


async def test_authorize_state_decodes_back_to_wa_id(
    http_client: AsyncClient,
) -> None:
    response = await http_client.get(
        "/oauth/google/authorize", params={"wa_id": "34600111222"}
    )
    state = parse_qs(urlparse(response.json()["authorization_url"]).query)["state"][0]
    settings = get_settings()
    assert OAuthStateCodec(settings.encryption_key).decode(state) == "34600111222"


async def test_authorize_rejects_missing_wa_id(http_client: AsyncClient) -> None:
    response = await http_client.get("/oauth/google/authorize")
    assert response.status_code == 422


# --- /oauth/google/callback -------------------------------------------------


def _valid_state(wa_id: str) -> str:
    settings = get_settings()
    return OAuthStateCodec(settings.encryption_key).encode(wa_id)


def _mock_token_response(
    *,
    access_token: str = "ya29.new-access",
    refresh_token: str | None = "1//refresh-value",
    expires_in: int = 3600,
    status_code: int = 200,
) -> AsyncMock:
    response = AsyncMock()
    response.status_code = status_code
    body: dict = {}
    if access_token is not None:
        body["access_token"] = access_token
    if refresh_token is not None:
        body["refresh_token"] = refresh_token
    if expires_in is not None:
        body["expires_in"] = expires_in
    response.json = lambda: body  # type: ignore[assignment]
    return response


def _patch_token_exchange(response_mock):
    """Patch ``httpx.AsyncClient`` used inside the OAuth callback."""
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=response_mock)
    return patch("app.routers.oauth.httpx.AsyncClient", return_value=mock_client)


async def test_callback_exchanges_code_and_persists_encrypted_tokens(
    http_client: AsyncClient,
) -> None:
    session = FakeSession(user=None, account=None)
    app.dependency_overrides[get_db] = _override_db(session)

    state = _valid_state("34600111222")
    with _patch_token_exchange(_mock_token_response()):
        response = await http_client.get(
            "/oauth/google/callback",
            params={"code": "auth-code-xyz", "state": state},
        )

    assert response.status_code == 200
    assert "Authorization successful" in response.text
    assert session.committed is True

    user = next((o for o in session.added if isinstance(o, User)), None)
    account = next((o for o in session.added if isinstance(o, GoogleAccount)), None)
    assert user is not None and user.wa_id == "34600111222"
    assert account is not None
    assert account.status == "active"
    assert isinstance(account.access_token_enc, bytes)
    assert isinstance(account.refresh_token_enc, bytes)
    # Confirm tokens are NOT in plaintext.
    assert b"ya29.new-access" not in account.access_token_enc
    assert b"1//refresh-value" not in account.refresh_token_enc

    # And that the encrypted bytes round-trip back to the originals.
    enc = TokenEncryption(get_settings().encryption_key)
    assert enc.decrypt(account.access_token_enc) == "ya29.new-access"
    assert enc.decrypt(account.refresh_token_enc) == "1//refresh-value"


async def test_callback_updates_existing_account(http_client: AsyncClient) -> None:
    user = User(wa_id="34600111222")
    user.id = uuid.uuid4()
    enc = TokenEncryption(get_settings().encryption_key)
    account = GoogleAccount(
        user_id=user.id,
        access_token_enc=enc.encrypt("old-access"),
        refresh_token_enc=enc.encrypt("old-refresh"),
        token_expires_at=datetime.now(UTC),
        status="revoked",
    )

    session = FakeSession(user=user, account=account)
    app.dependency_overrides[get_db] = _override_db(session)

    state = _valid_state("34600111222")
    with _patch_token_exchange(
        _mock_token_response(access_token="ya29.fresh", refresh_token="1//fresh")
    ):
        response = await http_client.get(
            "/oauth/google/callback",
            params={"code": "code", "state": state},
        )

    assert response.status_code == 200
    assert session.committed is True
    assert account.status == "active"
    assert enc.decrypt(account.access_token_enc) == "ya29.fresh"
    assert enc.decrypt(account.refresh_token_enc) == "1//fresh"
    # No new GoogleAccount created.
    assert not any(isinstance(o, GoogleAccount) for o in session.added)


async def test_callback_rejects_invalid_state(http_client: AsyncClient) -> None:
    session = FakeSession()
    app.dependency_overrides[get_db] = _override_db(session)

    response = await http_client.get(
        "/oauth/google/callback",
        params={"code": "code", "state": "not-a-real-state"},
    )
    assert response.status_code == 400
    assert session.committed is False


async def test_callback_propagates_token_exchange_failure(
    http_client: AsyncClient,
) -> None:
    session = FakeSession()
    app.dependency_overrides[get_db] = _override_db(session)

    state = _valid_state("34600111222")
    failed = _mock_token_response(status_code=400, access_token=None, refresh_token=None)
    with _patch_token_exchange(failed):
        response = await http_client.get(
            "/oauth/google/callback",
            params={"code": "bad-code", "state": state},
        )

    assert response.status_code == 400
    assert session.committed is False


async def test_callback_rejects_response_without_refresh_token(
    http_client: AsyncClient,
) -> None:
    session = FakeSession()
    app.dependency_overrides[get_db] = _override_db(session)

    state = _valid_state("34600111222")
    incomplete = _mock_token_response(refresh_token=None)
    with _patch_token_exchange(incomplete):
        response = await http_client.get(
            "/oauth/google/callback",
            params={"code": "code", "state": state},
        )

    assert response.status_code == 400
    assert session.committed is False
