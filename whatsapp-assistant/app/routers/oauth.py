"""Google OAuth2 authorization endpoints.

The flow is:

1. WhatsApp message handler sends the user a link to ``/oauth/google/authorize``
   (or directly to Google with our state). Hitting ``/authorize`` returns the
   Google consent URL.
2. After consent, Google redirects to ``/oauth/google/callback`` with ``code``
   and ``state``. We decode the state to recover the wa_id, exchange the code
   for tokens, encrypt them, and persist them in ``google_accounts``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import GoogleAccount, User
from app.services.google_auth import (
    GOOGLE_TOKEN_URL,
    build_authorization_url,
)
from app.utils.encryption import TokenEncryption
from app.utils.oauth_state import OAuthStateCodec, StateError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/oauth/google")

_SUCCESS_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>Authorization successful</title></head>"
    "<body style='font-family: system-ui, sans-serif; padding: 2rem;'>"
    "<h1>Authorization successful!</h1>"
    "<p>You can close this window and return to WhatsApp.</p>"
    "</body></html>"
)


@router.get("/authorize")
async def authorize(wa_id: str = Query(..., min_length=1)) -> dict[str, str]:
    """Return the Google OAuth2 authorization URL for ``wa_id``."""
    return {"authorization_url": build_authorization_url(wa_id)}


@router.get("/callback", response_class=HTMLResponse)
async def callback(
    code: str = Query(..., min_length=1),
    state: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Receive Google's redirect, exchange the code, and store encrypted tokens."""
    settings = get_settings()

    try:
        wa_id = OAuthStateCodec(settings.encryption_key).decode(state)
    except StateError as exc:
        logger.warning("Rejecting OAuth callback: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid state") from exc

    token_data = await _exchange_code_for_tokens(code, settings)

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")
    if not access_token or not refresh_token or not expires_in:
        # Google returns no refresh_token on subsequent consents *unless* we
        # pass prompt=consent. We do, so this should never happen; treat as a
        # hard failure rather than silently storing a half-account.
        logger.error("Google token exchange returned incomplete payload")
        raise HTTPException(status_code=400, detail="Incomplete token response")

    encryption = TokenEncryption(settings.encryption_key)
    access_enc = encryption.encrypt(access_token)
    refresh_enc = encryption.encrypt(refresh_token)
    expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))

    user = (
        await db.execute(select(User).where(User.wa_id == wa_id))
    ).scalar_one_or_none()
    if user is None:
        user = User(wa_id=wa_id)
        db.add(user)
        await db.flush()

    account = (
        await db.execute(
            select(GoogleAccount).where(GoogleAccount.user_id == user.id)
        )
    ).scalars().first()
    if account is None:
        account = GoogleAccount(
            user_id=user.id,
            access_token_enc=access_enc,
            refresh_token_enc=refresh_enc,
            token_expires_at=expires_at,
            status="active",
        )
        db.add(account)
    else:
        account.access_token_enc = access_enc
        account.refresh_token_enc = refresh_enc
        account.token_expires_at = expires_at
        account.status = "active"

    await db.commit()
    logger.info("Stored Google credentials for wa_id=%s", wa_id)
    return HTMLResponse(_SUCCESS_HTML)


async def _exchange_code_for_tokens(code: str, settings) -> dict:
    payload = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            response = await http.post(GOOGLE_TOKEN_URL, data=payload)
    except httpx.HTTPError as exc:
        logger.exception("Network error during Google token exchange")
        raise HTTPException(status_code=502, detail="Google token exchange failed") from exc

    if response.status_code >= 400:
        logger.error(
            "Google token exchange failed: status=%s", response.status_code
        )
        raise HTTPException(status_code=400, detail="Token exchange rejected")

    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Malformed token response") from exc
