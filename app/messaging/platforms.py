"""Thin HTTP clients for the supported messaging platforms.

Each platform is one class exposing ``send_text``. The Worker knows nothing
about HTTP, URLs or token placement — it calls the client for the channel's
platform and gets back a provider message id, or an exception.

Both Bale and Telegram speak the same Telegram-Bot-API-shaped protocol
(``sendMessage`` with ``chat_id`` + ``text``, and an ``ok``/``result`` envelope),
so the two clients are near-identical by design rather than by accident.

Errors are split into retryable and permanent. That distinction is what lets
the retry policy be bounded without wasting attempts: a rate limit or a socket
reset is retried, while an unknown chat or a revoked token is parked as
``failed`` on the first try, because retrying those would never succeed and
would hold up every article queued behind them.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """Base error for a failed send.

    ``retryable`` is the decision the Worker needs and an HTTP client cannot
    make on its own: whether another attempt could plausibly succeed.
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class PermanentDeliveryError(DeliveryError):
    """A send that will never succeed (bad chat id, revoked token, bad request)."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class TransientDeliveryError(DeliveryError):
    """A send that may succeed later (timeout, rate limit, server-side error)."""

    def __init__(self, message: str):
        super().__init__(message, retryable=True)


# HTTP statuses worth another attempt: 408/425 are request timeouts, 429 is a
# rate limit, and anything >=500 is a provider-side fault.
_RETRYABLE_STATUSES = {408, 425, 429}


def _classify_status(status: int, body: str) -> DeliveryError:
    """Map an HTTP status onto a retryable or permanent error."""
    detail = body.strip()[:500]
    if status in _RETRYABLE_STATUSES or status >= 500:
        return TransientDeliveryError(f"HTTP {status}: {detail}")
    return PermanentDeliveryError(f"HTTP {status}: {detail}")


class PlatformClient:
    """Base class for a messaging platform client."""

    platform: str = ""

    def __init__(self, token: str):
        if not token:
            raise ValueError(f"{self.platform or type(self).__name__}: token is not configured")
        self._token = token

    async def send_text(self, chat_id: int, text: str) -> int:
        """Send one plain-text message and return the provider message id."""
        raise NotImplementedError

    async def get_chat(self, chat: int | str) -> dict:
        """Resolve a numeric chat id or @username via ``getChat``.

        Returns the provider's ``result`` object (``id``, ``title``,
        ``username``, ...) so callers can validate a destination BEFORE
        saving it and show the resolved title. Same error policy as sends:
        retryable failures raise transient, unknown chats raise permanent.
        """
        return await self._get("getChat", {"chat_id": chat})

    async def _get(self, method: str, params: dict) -> dict:
        """GET a Bot-API-style method and return the ``result`` object.

        Shares ``_post``'s envelope/error policy; split out because creation
        needs the full result (id/title), not just a message id.
        """
        url = f"{self._api_base()}/bot{self._token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=settings.sender_request_timeout_seconds) as client:
                response = await client.get(url, params=params)
        except httpx.TimeoutException as e:
            raise TransientDeliveryError(f"timeout calling {method}: {e}") from e
        except httpx.HTTPError as e:
            raise TransientDeliveryError(f"network error calling {method}: {e}") from e

        if response.status_code >= 400:
            raise _classify_status(response.status_code, response.text)

        try:
            body = response.json()
        except ValueError as e:
            raise TransientDeliveryError(f"{method}: non-JSON response: {response.text[:200]}") from e

        if not isinstance(body, dict) or not body.get("ok"):
            description = ""
            if isinstance(body, dict):
                description = str(body.get("description", ""))[:500]
            error_code = body.get("error_code") if isinstance(body, dict) else None
            if isinstance(error_code, int) and error_code >= 500:
                raise TransientDeliveryError(f"{method}: {description} (error_code={error_code})")
            raise PermanentDeliveryError(f"{method}: {description or 'ok=false'}")

        result = body.get("result")
        if not isinstance(result, dict):
            raise PermanentDeliveryError(f"{method}: missing result object")
        return result

    async def _post(self, method: str, payload: dict) -> int:
        """POST a Bot-API-style request and unwrap the ``ok``/``result`` envelope.

        Raises:
            TransientDeliveryError: network failure, timeout, or a status worth
                retrying.
            PermanentDeliveryError: any other status, or ``ok: false`` with a
                non-retryable provider error code.
        """
        url = f"{self._api_base()}/bot{self._token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=settings.sender_request_timeout_seconds) as client:
                response = await client.post(url, json=payload)
        except httpx.TimeoutException as e:
            raise TransientDeliveryError(f"timeout calling {method}: {e}") from e
        except httpx.HTTPError as e:
            raise TransientDeliveryError(f"network error calling {method}: {e}") from e

        if response.status_code >= 400:
            raise _classify_status(response.status_code, response.text)

        try:
            body = response.json()
        except ValueError as e:
            # A 200 with an unparseable body is a provider fault, not a bad
            # request, so it is worth another attempt.
            raise TransientDeliveryError(f"{method}: non-JSON response: {response.text[:200]}") from e

        if not isinstance(body, dict) or not body.get("ok"):
            description = ""
            if isinstance(body, dict):
                description = str(body.get("description", ""))[:500]
            error_code = body.get("error_code") if isinstance(body, dict) else None
            if isinstance(error_code, int) and error_code >= 500:
                raise TransientDeliveryError(f"{method}: {description} (error_code={error_code})")
            raise PermanentDeliveryError(f"{method}: {description or 'ok=false'}")

        result = body.get("result") or {}
        return int(result.get("message_id", 0))

    def _api_base(self) -> str:
        raise NotImplementedError


class BaleClient(PlatformClient):
    """Bale client (``https://tapi.bale.ai/bot<token>/...``).

    Bale renders a single set of asterisks as bold by default, so the text from
    ``app.messaging.formatting`` is sent as-is with no parse mode.
    """

    platform = "bale"

    def _api_base(self) -> str:
        return settings.bale_api_base.rstrip("/")

    async def send_text(self, chat_id: int, text: str) -> int:
        """Send plain text (Bale bolds single asterisks with no parse mode)."""
        return await self._post("sendMessage", {"chat_id": chat_id, "text": text})


class TelegramClient(PlatformClient):
    """Telegram client (``https://api.telegram.org/bot<token>/...``).

    ``parse_mode="Markdown"`` is set so the bold title renders as bold, matching
    the Bale output; the shared ``escape_markdown`` keeps LLM-generated text
    from being reinterpreted as markup.
    """

    platform = "telegram"

    def _api_base(self) -> str:
        return settings.telegram_api_base.rstrip("/")

    async def send_text(self, chat_id: int, text: str) -> int:
        """Send Markdown text (bold title renders bold, matching Bale)."""
        return await self._post(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        )


_CLIENTS: dict[str, type[PlatformClient]] = {
    "bale": BaleClient,
    "telegram": TelegramClient,
}


def build_client(platform: str, token: str) -> PlatformClient:
    """Instantiate the client for ``platform`` using ``token``."""
    try:
        client_cls = _CLIENTS[platform]
    except KeyError:
        raise PermanentDeliveryError(f"unsupported platform: {platform!r}") from None
    return client_cls(token)


def configured_clients() -> dict[str, PlatformClient]:
    """Build a client for every platform that has a token configured.

    A platform with no token is skipped rather than raising, so the sender
    keeps working on the platforms that are set up. Channels pointing at an
    unconfigured platform fail their delivery loudly instead of being ignored.
    """
    tokens = {
        "bale": settings.bale_bot_token,
        "telegram": settings.telegram_bot_token,
    }
    clients: dict[str, PlatformClient] = {}
    for platform, token in tokens.items():
        if not token:
            logger.warning(
                "No bot token configured for platform %r; deliveries to it will fail", platform
            )
            continue
        clients[platform] = build_client(platform, token)
    return clients
