"""Email over SMTP and webhooks over HTTPS, plus the guard against webhooks that point
inside the hub's own network."""

import asyncio
import ipaddress
import smtplib
import socket
import time
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx

from smarthome.modules.notifications.application.ports import DeliveryError
from smarthome.modules.notifications.domain.errors import InvalidWebhook
from smarthome.modules.notifications.domain.webhook import SIGNATURE_HEADER, signature

SMTP_TIMEOUT_S = 10.0
# Retrying these is pointless: the receiver understood and refused.
_RETRYABLE_4XX = frozenset({408, 409, 425, 429})


class SmtpEmailSender:
    """stdlib smtplib on a thread: a few emails a minute do not justify another client."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._username = username
        self._password = password
        self._starttls = starttls

    async def send(self, *, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        try:
            await asyncio.to_thread(self._send, message)
        except smtplib.SMTPRecipientsRefused as exc:
            raise DeliveryError(f"recipient refused: {exc}", permanent=True) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f"smtp: {exc}") from exc

    def _send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=SMTP_TIMEOUT_S) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(message)


class HttpWebhookSender:
    def __init__(self, client: httpx.AsyncClient, guard: "AddressGuard") -> None:
        self._client = client
        self._guard = guard

    async def post(self, *, url: str, secret: str, body: bytes, idempotency_key: str) -> None:
        try:
            # Again at send time: DNS may have changed since the URL was saved.
            await self._guard.check(url)
        except InvalidWebhook as exc:
            raise DeliveryError(str(exc), permanent=True) from exc
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "smarthome-hub-webhooks/1",
            "Idempotency-Key": idempotency_key,
            SIGNATURE_HEADER: signature(secret, int(time.time()), body),
        }
        try:
            response = await self._client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"{type(exc).__name__}: {exc}") from exc
        if response.is_success:
            return
        permanent = 400 <= response.status_code < 500 and response.status_code not in _RETRYABLE_4XX  # noqa: PLR2004
        raise DeliveryError(f"HTTP {response.status_code}", permanent=permanent)


class AddressGuard:
    """Refuses URLs whose host resolves to a loopback, private, link-local or otherwise
    non-public address (SSRF: a webhook must not reach the hub's database or the cloud
    metadata endpoint). Development allows them, so a receiver on the laptop works.

    A resolver answer can change between this check and the request (DNS rebinding);
    the check runs at save time and again before every send, which narrows the window
    but does not close it. ADR 0013 records the trade-off.
    """

    def __init__(self, *, allow_private: bool) -> None:
        self._allow_private = allow_private

    async def check(self, url: str) -> None:
        if self._allow_private:
            return
        host = urlsplit(url).hostname or ""
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, None, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise InvalidWebhook(f"cannot resolve {host}") from exc
        for info in infos:
            address = ipaddress.ip_address(info[4][0])
            if not address.is_global or address.is_multicast:
                raise InvalidWebhook(f"{host} resolves to a non-public address")
