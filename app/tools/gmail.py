"""Gmail: the ONLY module that sends email (CLAUDE.md §2 rules 1, 2 and 7; issue #49).

- Auth: a refresh token (created once by `scripts/gmail_auth.py`, scope `gmail.send` only) is
  exchanged for short-lived access tokens over plain HTTPS. No Google SDK.
- Test mode (`EMAIL_MODE=test`): refuses any recipient not in `EMAIL_TEST_RECIPIENTS` right before
  the HTTP call, whatever the queue contains. A test checks that no other file has the send URL.
- Every failure is classified, because "was it sent?" decides what happens next:
    NotSent        definitely not sent (could not connect, rate limited, token refresh failed):
                   safe to retry later.
    Ambiguous      the request may have reached Gmail (timeout after connecting, 5xx): the caller
                   must NOT retry; owners check the Sent folder.
    Rejected       Gmail refused this message (bad recipient, malformed): will never work.
    AuthProblem    the sign-in is expired/revoked or lacks permission: Jeff must reconnect.
    RecipientNotAllowed  test-mode allowlist refusal.
"""

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate

import httpx

from app.config import Settings
from app.redact import redact

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SCOPE = "https://www.googleapis.com/auth/gmail.send"
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "sendingLimitExceeded"}


class GmailError(Exception):
    pass


class NotSent(GmailError):
    pass


class Ambiguous(GmailError):
    pass


class Rejected(GmailError):
    pass


class AuthProblem(GmailError):
    pass


class RecipientNotAllowed(GmailError):
    pass


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    thread_id: str


def build_message(
    *,
    from_name: str,
    from_address: str,
    to: str,
    subject: str,
    text: str,
    message_id: str,
    headers: dict[str, str],
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_address))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = message_id
    for name, value in headers.items():
        msg[name] = value
    msg.set_content(text)  # UTF-8; accents are safe
    return msg


class GmailClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self.sleep = sleep
        self._token: str | None = None
        self._token_expires = 0.0

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.gmail_client_id and s.gmail_client_secret and s.gmail_refresh_token and s.gmail_sender)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _access_token(self, force: bool = False) -> str:
        if self._token and not force and time.monotonic() < self._token_expires - 60:
            return self._token
        s = self.settings
        try:
            response = await self.http.post(
                TOKEN_URL,
                data={
                    "client_id": s.gmail_client_id,
                    "client_secret": s.gmail_client_secret,
                    "refresh_token": s.gmail_refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:  # nothing was sent yet
            raise NotSent(f"could not reach Google to sign in: {type(exc).__name__}") from exc
        if response.status_code == 400 and "invalid_grant" in response.text:
            raise AuthProblem(
                "Google sign-in expired or was revoked (invalid_grant): run scripts/gmail_auth.py"
            )
        if response.status_code in (400, 401):
            raise AuthProblem(
                f"Google refused the sign-in (HTTP {response.status_code}): check the Gmail settings"
            )
        if response.status_code != 200:
            raise NotSent(f"Google sign-in failed (HTTP {response.status_code})")
        try:
            data = response.json()
            self._token = data["access_token"]
            self._token_expires = time.monotonic() + float(data.get("expires_in", 300))
        except (ValueError, KeyError, TypeError) as exc:
            raise NotSent("Google sign-in answered with something unreadable") from exc
        return self._token

    async def send(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        message_id: str,
        headers: dict[str, str],
    ) -> SentMessage:
        recipient = to.strip().lower()
        if self.settings.email_mode == "test" and recipient not in self.settings.test_recipients():
            raise RecipientNotAllowed("test mode: recipient is not in EMAIL_TEST_RECIPIENTS")
        if not self.configured:
            raise AuthProblem("Gmail is not connected: GMAIL_* settings are empty")
        s = self.settings
        try:
            msg = build_message(
                from_name=f"{s.company_sender_name}, {s.company_name}"
                if s.company_sender_name
                else s.company_name,
                from_address=s.gmail_sender,
                to=recipient,
                subject=subject,
                text=text,
                message_id=message_id,
                headers=headers,
            )
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        except (ValueError, UnicodeError) as exc:  # e.g. a newline in a header: can never be built
            raise Rejected(f"the email could not be built: {type(exc).__name__}") from exc

        for attempt in (1, 2):
            token = await self._access_token(force=attempt == 2)
            try:
                response = await self.http.post(
                    SEND_URL, json={"raw": raw}, headers={"Authorization": f"Bearer {token}"}
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise NotSent(f"could not connect to Gmail: {type(exc).__name__}") from exc
            except httpx.HTTPError as exc:  # the request may have been delivered
                raise Ambiguous(f"no clear answer from Gmail: {type(exc).__name__}") from exc

            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                    return SentMessage(message_id=data["id"], thread_id=data["threadId"])
                except (ValueError, KeyError, TypeError) as exc:  # Gmail accepted it but we can't read how
                    raise Ambiguous("Gmail accepted the email but its answer was unreadable") from exc
            if status == 401 and attempt == 1:
                continue  # expired access token: refresh once and retry (not sent)
            raise self._classify(status, response)
        raise AuthProblem("Gmail kept answering 401")  # pragma: no cover

    @staticmethod
    def _classify(status: int, response: httpx.Response) -> GmailError:
        detail = redact(response.text)[:300]
        reasons: set[str] = set()
        try:
            for item in response.json().get("error", {}).get("errors", []):
                reasons.add(item.get("reason", ""))
        except ValueError:
            pass
        if status == 429 or reasons & RATE_LIMIT_REASONS:
            return NotSent(f"Gmail rate limit (HTTP {status})")
        if status in (401, 403):
            return AuthProblem(f"Gmail refused the sign-in or permission (HTTP {status}): {detail}")
        if status >= 500:
            return Ambiguous(f"Gmail server error (HTTP {status}); the email may have been sent")
        return Rejected(f"Gmail refused this email (HTTP {status}): {detail}")
