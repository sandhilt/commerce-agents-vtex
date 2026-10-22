# SPDX-License-Identifier: Apache-2.0
"""Async HTTP client for VTEX public storefront APIs.

Sessions: the underlying `httpx.AsyncClient` keeps no cookies (its jar rejects every
cookie). `CookieStore` holds one jar per `session_id`; each call attaches that session's
cookies and merges the response's `Set-Cookie` back into it. A call without `session_id`
sends and stores nothing.

Retry policy:
- Retryable statuses: 429, 500, 502, 503, 504. Retryable exceptions: `httpx.TransportError`.
- Only read-only calls are retried: GET, and POST with `idempotent=True` (for example the
  read-only orderForms simulation).
- Cart mutations are not retried on those statuses or on timeouts, because the first
  attempt may have been applied. The one exception is `httpx.ConnectError` and
  `httpx.ConnectTimeout`: the request never reached the server, so any method may retry.
- Backoff is exponential with full jitter: `random() * min(max_delay, base_delay * 2**attempt)`
  with base 0.5s and max 8s. When `Retry-After` (seconds or HTTP date) is present, the wait
  is the larger of it and the computed delay. If it exceeds `max_retry_after` (30s) the
  client stops retrying and raises.
- `max_retries` counts retries after the first attempt.

Redirects are never followed: a 3xx response raises `VtexRedirectError` with the status and
the `Location`, which usually means `VTEX_DOMAIN` should name the final host (with or without
`www`, https instead of http).

Errors carry the method, path, status, and a body excerpt of at most 300 characters; never
cookies or request headers. Logs hold method, path, status, attempt, and delay only.

Safety: a request goes only to a path on the allowlist below, relative to the store domain:
Intelligent Search, the orderForm, its items, its `shippingData` attachment, and the orderForms
simulation. Absolute URLs and every other path are refused, so the POC never attaches customer
data to an orderForm (that would create a Master Data record and could trigger abandoned-cart
e-mails) and never reads a shopper profile. Log lines, error messages, and body excerpts mask
the orderFormId, since the id alone opens the cart.
"""

from __future__ import annotations

import asyncio
import logging
import random as _random
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Any

import httpx

from vtex_poc.settings import VtexSettings

logger = logging.getLogger(__name__)

IS_PRODUCT_SEARCH = "/api/intelligent-search/v1/product-search"
IS_PRODUCTS = "/api/intelligent-search/v1/products"
ORDER_FORM = "/api/checkout/pub/orderForm"
SIMULATION = "/api/checkout/pub/orderForms/simulation"

USER_AGENT = "commerce-agents-vtex-poc/0.1 (+https://github.com/sandhilt/commerce-agents-vtex)"
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_EXCERPT_CHARS = 300
_ORDER_FORM_ID_IN_PATH = re.compile(r"(/orderForm/)[^/?#]+", re.IGNORECASE)
_ORDER_FORM_ID_FIELD = re.compile(
    r"(orderFormId[\\\"']*\s*[:=]\s*[\\\"']*)[A-Za-z0-9]+", re.IGNORECASE
)
_MIN_LITERAL_ID_CHARS = 8

_SEGMENT = r"[A-Za-z0-9_.~-]+"
_ID = r"[A-Za-z0-9]+"
_ALLOWED_PATHS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"{IS_PRODUCT_SEARCH}(?:/{_SEGMENT})*",
        rf"{IS_PRODUCTS}(?:/{_SEGMENT})*",
        ORDER_FORM,
        rf"{ORDER_FORM}/{_ID}",
        rf"{ORDER_FORM}/{_ID}/items",
        rf"{ORDER_FORM}/{_ID}/items/update",
        rf"{ORDER_FORM}/{_ID}/attachments/shippingData",
        SIMULATION,
    )
)


def _mask(text: str) -> str:
    """The text with any orderFormId replaced, for logs and error messages."""
    text = _ORDER_FORM_ID_IN_PATH.sub(r"\1<orderFormId>", text)
    return _ORDER_FORM_ID_FIELD.sub(r"\1<orderFormId>", text)


def _mask_body(text: str, path: str) -> str:
    """A response body with the orderFormId of the requested path and any id field replaced."""
    for match in _ORDER_FORM_ID_IN_PATH.finditer(path):
        order_form_id = match.group(0).split("/")[-1]
        if len(order_form_id) >= _MIN_LITERAL_ID_CHARS:
            text = text.replace(order_form_id, "<orderFormId>")
    return _mask(text)


def _check_path(path: str) -> None:
    """Refuse anything but a relative path on the allowlist."""
    segments = path.split("/")
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or "." in segments
        or ".." in segments
        or not any(pattern.fullmatch(path) for pattern in _ALLOWED_PATHS)
    ):
        raise ValueError(
            f"Path is not allowed: {_mask(path)!r}. The POC calls only catalog, orderForm, "
            "items, shippingData, and simulation paths on the store domain, and never sends "
            "or reads customer data"
        )


class _MaskOrderFormIds(logging.Filter):
    """httpx logs every request URL at INFO; mask orderFormIds in those records too."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _mask(str(arg)) if isinstance(arg, str | httpx.URL) else arg for arg in record.args
            )
        return True


_HTTPX_LOGGER = logging.getLogger("httpx")
if not any(isinstance(f, _MaskOrderFormIds) for f in _HTTPX_LOGGER.filters):
    _HTTPX_LOGGER.addFilter(_MaskOrderFormIds())


class VtexError(Exception):
    """Base class for client errors."""


class VtexHTTPError(VtexError):
    """A non-2xx response, after retries."""

    def __init__(self, status_code: int, method: str, path: str, body_excerpt: str) -> None:
        self.status_code = status_code
        self.method = method
        self.path = path
        self.body_excerpt = _mask(body_excerpt)[:_EXCERPT_CHARS]
        super().__init__(f"{method} {path} returned {status_code}: {self.body_excerpt}")


class VtexRedirectError(VtexHTTPError):
    """A 3xx response. Redirects are not followed; the store domain probably needs changing."""

    def __init__(self, status_code: int, method: str, path: str, location: str) -> None:
        super().__init__(status_code, method, path, "")
        self.location = _mask(location)[:_EXCERPT_CHARS]
        self.args = (
            f"{method} {path} was redirected ({status_code}) to {self.location or '(no Location)'}; "
            "redirects are not followed. Set VTEX_DOMAIN to the host in Location "
            "(for example with or without www) so requests reach it directly",
        )


class VtexNetworkError(VtexError):
    """A transport failure, after retries."""

    def __init__(self, method: str, path: str, cause: Exception) -> None:
        self.method = method
        self.path = path
        super().__init__(f"{method} {path} failed: {type(cause).__name__}")


class _RejectAllCookies(DefaultCookiePolicy):
    def set_ok(self, cookie: Cookie, request: Any) -> bool:
        return False


class CookieStore:
    """In-memory cookie jars keyed by session id."""

    def __init__(self) -> None:
        self._jars: dict[str, httpx.Cookies] = {}

    def jar(self, session_id: str) -> httpx.Cookies:
        return self._jars.setdefault(session_id, httpx.Cookies())

    def has(self, session_id: str) -> bool:
        return session_id in self._jars

    def clear(self, session_id: str) -> None:
        self._jars.pop(session_id, None)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class VtexClient:
    """Generic JSON client with per-session cookies and a conservative retry policy."""

    def __init__(
        self,
        settings: VtexSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float] = _random.random,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        max_retry_after: float = 30.0,
    ) -> None:
        self.settings = settings
        self.cookies = CookieStore()
        self._sleep = sleep
        self._random = random
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._max_retry_after = max_retry_after
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            cookies=CookieJar(policy=_RejectAllCookies()),
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> VtexClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        return await self._request("GET", path, params=params, session_id=session_id, safe=True)

    async def post_json(
        self,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        idempotent: bool = False,
    ) -> Any:
        return await self._request(
            "POST", path, json=json, params=params, session_id=session_id, safe=idempotent
        )

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        delay = self._random() * min(self._max_delay, self._base_delay * 2**attempt)
        return delay if retry_after is None else max(retry_after, delay)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        session_id: str | None,
        safe: bool,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        _check_path(path)
        shown = _mask(path)
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            request = self._http.build_request(method, path, params=params, json=json)
            if session_id is not None:
                self.cookies.jar(session_id).set_cookie_header(request)
            last = attempt == attempts - 1
            try:
                response = await self._http.send(request)
            except httpx.TransportError as exc:
                never_sent = isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
                logger.debug(
                    "%s %s transport error %s attempt=%d",
                    method,
                    shown,
                    type(exc).__name__,
                    attempt,
                )
                if last or not (safe or never_sent):
                    raise VtexNetworkError(method, shown, exc) from exc
                delay = self._delay(attempt, None)
                logger.info("%s %s retrying after %.2fs attempt=%d", method, shown, delay, attempt)
                await self._sleep(delay)
                continue

            if session_id is not None:
                self.cookies.jar(session_id).extract_cookies(response)
            logger.debug("%s %s status=%d attempt=%d", method, shown, response.status_code, attempt)
            if response.is_success:
                return self._decode(response, method, shown)

            if response.is_redirect or 300 <= response.status_code < 400:
                raise VtexRedirectError(
                    response.status_code, method, shown, response.headers.get("Location", "")
                )

            error = VtexHTTPError(
                response.status_code, method, shown, _mask_body(response.text, path)
            )
            if last or not safe or response.status_code not in RETRYABLE_STATUSES:
                raise error
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None and retry_after > self._max_retry_after:
                raise error
            delay = self._delay(attempt, retry_after)
            logger.info(
                "%s %s status=%d retrying after %.2fs attempt=%d",
                method,
                shown,
                response.status_code,
                delay,
                attempt,
            )
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _decode(response: httpx.Response, method: str, path: str) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise VtexHTTPError(response.status_code, method, path, "invalid JSON body") from None
