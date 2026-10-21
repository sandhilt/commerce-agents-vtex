# SPDX-License-Identifier: Apache-2.0
"""Tests for VtexClient, using httpx.MockTransport only."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

import httpx
import pytest

from vtex_poc.client import (
    IS_PRODUCTS,
    ORDER_FORM,
    SIMULATION,
    USER_AGENT,
    VtexClient,
    VtexHTTPError,
    VtexNetworkError,
    VtexRedirectError,
)
from vtex_poc.settings import VtexSettings

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [k for k in os.environ if k.startswith("VTEX_")]:
        monkeypatch.delenv(key)


def make_settings(**overrides: object) -> VtexSettings:
    """Every field is explicit, so no `VTEX_*` variable can change a test."""
    values = {
        "account": "acme",
        "domain": "www.acme.com.br",
        "sc": "1",
        "seller": "1",
        "environment": "vtexcommercestable",
        "locale": "pt-BR",
        "country": "BRA",
        "currency": "BRL",
        "timeout_seconds": 10.0,
        "max_retries": 3,
    }
    return VtexSettings(_env_file=None, **{**values, **overrides})  # type: ignore[arg-type]


class Harness:
    def __init__(self, handler: Handler, **settings: object) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        async def fake_sleep(delay: float) -> None:
            self.sleeps.append(delay)

        self.client = VtexClient(
            make_settings(**settings),
            transport=httpx.MockTransport(wrapped),
            sleep=fake_sleep,
            random=lambda: 0.5,
        )


def sequence(*items: httpx.Response | Exception) -> Handler:
    queue = list(items)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


async def test_get_retries_on_503_then_succeeds() -> None:
    h = Harness(
        sequence(httpx.Response(503), httpx.Response(503), httpx.Response(200, json={"ok": 1}))
    )
    assert await h.client.get_json(f"{IS_PRODUCTS}/x") == {"ok": 1}
    assert len(h.requests) == 3
    assert h.sleeps == [0.5 * 0.5 * 1, 0.5 * 0.5 * 2]
    for attempt, delay in enumerate(h.sleeps):
        assert 0 <= delay <= min(8.0, 0.5 * 2**attempt)


async def test_retry_after_seconds_is_honoured() -> None:
    h = Harness(
        sequence(httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json=[]))
    )
    await h.client.get_json(f"{IS_PRODUCTS}/x")
    assert h.sleeps[0] >= 2


async def test_retry_after_over_cap_raises_without_sleeping() -> None:
    h = Harness(sequence(httpx.Response(429, headers={"Retry-After": "3600"})))
    with pytest.raises(VtexHTTPError) as info:
        await h.client.get_json(f"{IS_PRODUCTS}/x")
    assert info.value.status_code == 429
    assert h.sleeps == []
    assert len(h.requests) == 1


async def test_non_idempotent_post_not_retried_on_503() -> None:
    h = Harness(sequence(httpx.Response(503, text="down")))
    with pytest.raises(VtexHTTPError) as info:
        await h.client.post_json(ORDER_FORM, json={"a": 1})
    assert info.value.status_code == 503
    assert len(h.requests) == 1
    assert h.sleeps == []


async def test_non_idempotent_post_not_retried_on_timeout() -> None:
    h = Harness(sequence(httpx.ReadTimeout("slow")))
    with pytest.raises(VtexNetworkError):
        await h.client.post_json(ORDER_FORM, json={})
    assert len(h.requests) == 1


async def test_post_retried_on_connect_error() -> None:
    h = Harness(sequence(httpx.ConnectError("refused"), httpx.Response(200, json={"id": "1"})))
    assert await h.client.post_json(ORDER_FORM, json={}) == {"id": "1"}
    assert len(h.requests) == 2
    assert len(h.sleeps) == 1


async def test_idempotent_simulation_retried_on_502() -> None:
    h = Harness(sequence(httpx.Response(502), httpx.Response(200, json={"items": []})))
    result = await h.client.post_json(SIMULATION, json={}, idempotent=True)
    assert result == {"items": []}
    assert len(h.requests) == 2


async def test_network_error_exhausted() -> None:
    h = Harness(sequence(*[httpx.ReadTimeout("t") for _ in range(3)]), max_retries=2)
    with pytest.raises(VtexNetworkError):
        await h.client.get_json(f"{IS_PRODUCTS}/x")
    assert len(h.requests) == 3
    assert len(h.sleeps) == 2


async def test_http_error_excerpt_is_capped_and_status_kept() -> None:
    h = Harness(sequence(httpx.Response(404, text="n" * 1000)))
    with pytest.raises(VtexHTTPError) as info:
        await h.client.get_json(f"{IS_PRODUCTS}/missing")
    assert info.value.status_code == 404
    assert len(info.value.body_excerpt) == 300
    assert len(h.requests) == 1


async def test_per_session_cookies_are_isolated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/set"):
            return httpx.Response(
                200,
                json={},
                headers=[
                    ("Set-Cookie", "checkout.vtex.com=__ofid=abc; Path=/"),
                    ("Set-Cookie", "CheckoutOrderFormOwnership=xyz; Path=/"),
                ],
            )
        return httpx.Response(200, json={})

    h = Harness(handler)
    await h.client.get_json(f"{IS_PRODUCTS}/set", session_id="A")
    await h.client.get_json(f"{IS_PRODUCTS}/next", session_id="A")
    await h.client.get_json(f"{IS_PRODUCTS}/next", session_id="B")
    await h.client.get_json(f"{IS_PRODUCTS}/next")

    assert h.requests[0].headers.get("cookie") is None
    sent_a = h.requests[1].headers["cookie"]
    assert "checkout.vtex.com=__ofid=abc" in sent_a
    assert "CheckoutOrderFormOwnership=xyz" in sent_a
    assert "cookie" not in h.requests[2].headers
    assert "cookie" not in h.requests[3].headers
    assert len(h.client._http.cookies.jar) == 0

    h.client.cookies.clear("A")
    await h.client.get_json(f"{IS_PRODUCTS}/next", session_id="A")
    assert "cookie" not in h.requests[4].headers


async def test_session_keeps_cookie_set_on_error_response() -> None:
    h = Harness(
        sequence(
            httpx.Response(400, headers={"Set-Cookie": "vtex_session=s1; Path=/"}),
            httpx.Response(200, json={}),
        )
    )
    with pytest.raises(VtexHTTPError):
        await h.client.get_json(f"{IS_PRODUCTS}/a", session_id="A")
    await h.client.get_json(f"{IS_PRODUCTS}/b", session_id="A")
    assert "vtex_session=s1" in h.requests[1].headers["cookie"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/checkout/pub/orderForm/abc/attachments/clientProfileData",
        "/api/checkout/pub/orderForm/abc/attachments/shippingData/x",
        "/api/checkout/pub/profiles",
        "/API/checkout/pub/profiles/?email=a",
        "/api/checkout/pub/orderForm/abc/coupons",
        "/api/checkout/pub/orderForm/abc/items/../attachments/clientProfileData",
        "/api/intelligent-search/v1/products/../../../checkout/pub/profiles",
        "/api/checkout/pub/orderForm/",
        "/x",
        "https://www.acme.com.br/api/checkout/pub/orderForm",
        "https://evil.example/api/checkout/pub/orderForm",
        "//evil.example/api/checkout/pub/orderForm",
        "api/checkout/pub/orderForm",
    ],
)
async def test_customer_data_paths_are_refused(path: str) -> None:
    h = Harness(sequence())
    with pytest.raises(ValueError):
        await h.client.post_json(path, json={"email": "a@b.c"})
    assert h.requests == []


async def test_logs_contain_no_cookie_values(caplog: pytest.LogCaptureFixture) -> None:
    secret = "supersecretvalue123"
    h = Harness(
        sequence(
            httpx.Response(200, headers={"Set-Cookie": f"vtex_segment={secret}; Path=/"}),
            httpx.Response(503),
            httpx.Response(200, json={}),
        )
    )
    with caplog.at_level(logging.DEBUG):
        await h.client.get_json(f"{IS_PRODUCTS}/a", session_id="S")
        await h.client.get_json(f"{IS_PRODUCTS}/b", session_id="S")
    assert caplog.records
    assert secret not in caplog.text
    assert "vtex_segment" not in caplog.text
    assert "Set-Cookie" not in caplog.text


async def test_headers_and_no_redirects() -> None:
    h = Harness(sequence(httpx.Response(302, headers={"Location": "/elsewhere"})))
    with pytest.raises(VtexHTTPError):
        await h.client.get_json(f"{IS_PRODUCTS}/x")
    request = h.requests[0]
    assert request.headers["user-agent"] == USER_AGENT
    assert request.headers["accept"] == "application/json"
    assert str(request.url) == f"https://www.acme.com.br{IS_PRODUCTS}/x"
    assert len(h.requests) == 1


async def test_context_manager_closes() -> None:
    h = Harness(sequence(httpx.Response(200, json={})))
    async with h.client as client:
        await client.get_json(f"{IS_PRODUCTS}/x")
    assert client._http.is_closed


async def test_customer_data_paths_are_refused_on_get_too() -> None:
    h = Harness(sequence())
    with pytest.raises(ValueError):
        await h.client.get_json("/api/checkout/pub/profiles", params={"email": "a@b.c"})
    assert h.requests == []


async def test_order_form_id_is_masked_in_logs_and_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    order_form_id = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    h = Harness(sequence(httpx.Response(400, text="bad item")))
    with caplog.at_level(logging.DEBUG), pytest.raises(VtexHTTPError) as info:
        await h.client.post_json(f"{ORDER_FORM}/{order_form_id}/items", json={})
    assert order_form_id in str(h.requests[0].url)
    assert order_form_id not in caplog.text
    assert order_form_id not in str(info.value)
    assert "/orderForm/<orderFormId>/items" in info.value.path


async def test_retry_after_http_date_is_honoured() -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    when = format_datetime(datetime.now(UTC) + timedelta(seconds=5), usegmt=True)
    h = Harness(
        sequence(httpx.Response(503, headers={"Retry-After": when}), httpx.Response(200, json={}))
    )
    await h.client.get_json(f"{IS_PRODUCTS}/x")
    assert 3 <= h.sleeps[0] <= 6


@pytest.mark.parametrize(
    "path",
    [
        f"{IS_PRODUCTS}",
        "/api/intelligent-search/v1/product-search/shoes/brand/acme",
        ORDER_FORM,
        f"{ORDER_FORM}/abc123",
        f"{ORDER_FORM}/abc123/items",
        f"{ORDER_FORM}/abc123/items/update",
        SIMULATION,
    ],
)
async def test_allowed_paths_pass(path: str) -> None:
    h = Harness(sequence(httpx.Response(200, json={})))
    await h.client.post_json(path, json={})
    assert len(h.requests) == 1


async def test_shipping_data_passes_without_email_and_client_profile_data_is_refused() -> None:
    h = Harness(sequence(httpx.Response(200, json={})))
    base = f"{ORDER_FORM}/abc123/attachments"
    address = {"address": {"postalCode": "01310100", "country": "BRA"}}
    await h.client.post_json(f"{base}/shippingData", json=address)
    assert len(h.requests) == 1
    assert b"email" not in h.requests[0].content
    with pytest.raises(ValueError):
        await h.client.post_json(f"{base}/clientProfileData", json={"email": "a@b.c"})
    assert len(h.requests) == 1


async def test_body_excerpt_masks_order_form_id() -> None:
    order_form_id = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    bodies = [
        f"cart {order_form_id} not found",
        f'{{"orderFormId":"{order_form_id}","error":"x"}}',
        f"see /api/checkout/pub/orderForm/{order_form_id}/items",
        f"https://www.acme.com.br/checkout/?orderFormId={order_form_id}#/cart",
    ]
    for body in bodies:
        h = Harness(sequence(httpx.Response(400, text=body)))
        with pytest.raises(VtexHTTPError) as info:
            await h.client.post_json(f"{ORDER_FORM}/{order_form_id}/items", json={})
        assert order_form_id not in info.value.body_excerpt
        assert order_form_id not in str(info.value)
        assert "<orderFormId>" in info.value.body_excerpt


async def test_body_excerpt_masks_id_before_truncating() -> None:
    order_form_id = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    body = "n" * 290 + order_form_id
    h = Harness(sequence(httpx.Response(400, text=body)))
    with pytest.raises(VtexHTTPError) as info:
        await h.client.post_json(f"{ORDER_FORM}/{order_form_id}/items", json={})
    assert order_form_id[:8] not in str(info.value)


@pytest.mark.parametrize(
    ("status", "location"),
    [
        (301, "https://www.acme.com.br/api/intelligent-search/v1/products/x"),
        (308, "https://acme.com.br/api/intelligent-search/v1/products/x"),
        (302, "/elsewhere"),
    ],
)
async def test_redirect_raises_clear_error_with_status_and_location(
    status: int, location: str
) -> None:
    h = Harness(sequence(httpx.Response(status, headers={"Location": location})))
    with pytest.raises(VtexRedirectError) as info:
        await h.client.get_json(f"{IS_PRODUCTS}/x")
    message = str(info.value)
    assert str(status) in message
    assert location in message
    assert "VTEX_DOMAIN" in message
    assert info.value.status_code == status
    assert len(h.requests) == 1
    assert h.sleeps == []


async def test_redirect_location_masks_order_form_id() -> None:
    order_form_id = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    location = f"https://www.acme.com.br{ORDER_FORM}/{order_form_id}/items"
    h = Harness(sequence(httpx.Response(301, headers={"Location": location})))
    with pytest.raises(VtexRedirectError) as info:
        await h.client.post_json(f"{ORDER_FORM}/{order_form_id}/items", json={})
    assert order_form_id not in str(info.value)


async def test_exported_vtex_env_does_not_change_retry_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VTEX_MAX_RETRIES", "0")
    monkeypatch.setenv("VTEX_TIMEOUT_SECONDS", "abc")
    h = Harness(sequence(httpx.Response(503), httpx.Response(200, json={"ok": 1})))
    assert h.client.settings.max_retries == 3
    assert await h.client.get_json(f"{IS_PRODUCTS}/x") == {"ok": 1}
    assert len(h.requests) == 2
