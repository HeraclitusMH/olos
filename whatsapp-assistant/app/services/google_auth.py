"""Google OAuth token management: storage, refresh, and revocation."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import GoogleAccount
from app.utils.encryption import TokenEncryption, TokenEncryptionError
from app.utils.exceptions import TokenExpiredError
from app.utils.oauth_state import OAuthStateCodec

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"

# Refresh proactively when the token will expire within this window.
REFRESH_THRESHOLD_SECONDS = 5 * 60


class GoogleAuthError(Exception):
    """Generic, *transient* Google auth failure (network/HTTP/decoding).

    Distinct from :class:`app.utils.exceptions.TokenExpiredError`, which means
    the user must reauthorize. ``GoogleAuthError`` means "try again later".
    """


class _RefreshRevokedError(Exception):
    """Internal signal that a refresh attempt was rejected as revoked."""


def build_authorization_url(wa_id: str) -> str:
    """Build the Google OAuth authorization URL for ``wa_id``.

    The wa_id is embedded in an encrypted, time-limited ``state`` parameter so
    the callback can identify which WhatsApp user authorized the flow.
    """
    settings = get_settings()
    state = OAuthStateCodec(settings.encryption_key).encode(wa_id)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


class GoogleAuthService:
    """Read/refresh Google OAuth tokens stored encrypted in ``google_accounts``."""

    def __init__(
        self,
        session: AsyncSession,
        encryption: TokenEncryption | None = None,
        http_client_factory=None,
    ) -> None:
        settings = get_settings()
        self._session = session
        self._encryption = encryption or TokenEncryption(settings.encryption_key)
        self._client_id = settings.google_client_id
        self._client_secret = settings.google_client_secret
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=10.0)
        )

    async def _get_account(self, user_id: uuid.UUID) -> GoogleAccount | None:
        result = await self._session.execute(
            select(GoogleAccount).where(GoogleAccount.user_id == user_id)
        )
        return result.scalars().first()

    async def is_authorized(self, user_id: uuid.UUID) -> bool:
        account = await self._get_account(user_id)
        return account is not None and account.status == "active"

    async def get_valid_token(self, user_id: uuid.UUID) -> str | None:
        """Return a usable access token.

        * ``None`` — no Google account is linked yet (caller should prompt the
          user to authorize for the first time).
        * raises :class:`TokenExpiredError` — the link is permanently broken
          (revoked, ``invalid_grant``, or unrecoverable decrypt failure). The
          caller must prompt the user to *re-authorize*.
        * raises :class:`GoogleAuthError` — transient failure (network, 5xx).
          Caller should surface a "try again later" message.
        """
        account = await self._get_account(user_id)
        if account is None:
            return None
        if account.status != "active":
            raise TokenExpiredError(
                log_message=f"google_account user_id={user_id} status={account.status}"
            )

        now = datetime.now(UTC)
        threshold = timedelta(seconds=REFRESH_THRESHOLD_SECONDS)

        # Token is comfortably valid — decrypt and return it.
        if account.token_expires_at - now > threshold:
            try:
                return self._encryption.decrypt(account.access_token_enc)
            except TokenEncryptionError as exc:
                logger.error(
                    "Failed to decrypt access token for account=%s; marking revoked",
                    account.id,
                )
                account.status = "revoked"
                await self._session.commit()
                raise TokenExpiredError(
                    log_message=f"access token decrypt failed for account={account.id}"
                ) from exc

        # Within refresh window — try to refresh.
        try:
            return await self.refresh_token(account)
        except _RefreshRevokedError as exc:
            logger.warning("Refresh rejected for account=%s; marking revoked", account.id)
            account.status = "revoked"
            await self._session.commit()
            raise TokenExpiredError(
                log_message=f"refresh rejected for account={account.id}"
            ) from exc
        except (httpx.HTTPError, GoogleAuthError) as exc:
            logger.warning(
                "Transient refresh failure for account=%s: %s", account.id, exc
            )
            raise GoogleAuthError(str(exc)) from exc

    async def refresh_token(self, account: GoogleAccount) -> str:
        """Refresh ``account``'s access token in place.

        Decrypts the stored refresh token, calls Google's token endpoint, and
        re-encrypts + persists the new access token plus expiry. Raises
        :class:`_RefreshRevokedError` on 4xx (revoked / invalid_grant) so the
        caller can mark the account revoked.
        """
        try:
            refresh_plain = self._encryption.decrypt(account.refresh_token_enc)
        except TokenEncryptionError as exc:
            raise _RefreshRevokedError("refresh token decrypt failed") from exc

        async with self._http_client_factory() as http:
            response = await http.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_plain,
                    "grant_type": "refresh_token",
                },
            )

        if response.status_code in (400, 401):
            raise _RefreshRevokedError(
                f"refresh rejected: status={response.status_code}"
            )
        if response.status_code >= 400:
            raise GoogleAuthError(
                f"unexpected refresh status: {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise GoogleAuthError("Malformed refresh response") from exc

        new_access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not new_access_token or not expires_in:
            raise GoogleAuthError("Refresh response missing access_token/expires_in")

        account.access_token_enc = self._encryption.encrypt(new_access_token)
        account.token_expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
        await self._session.commit()
        return new_access_token

    async def revoke_token(self, user_id: uuid.UUID) -> None:
        account = await self._get_account(user_id)
        if account is None:
            return
        account.status = "revoked"
        await self._session.commit()
