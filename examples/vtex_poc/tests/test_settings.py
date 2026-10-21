# SPDX-License-Identifier: Apache-2.0
"""Tests for VtexSettings and load_settings."""

from __future__ import annotations

import pytest

from vtex_poc.settings import VtexConfigError, VtexSettings, load_settings

VALID = {
    "VTEX_ACCOUNT": "acme-store",
    "VTEX_DOMAIN": "www.acme.com.br",
    "VTEX_SC": "1",
    "VTEX_SELLER": "1",
}
KEYS = [
    "ACCOUNT",
    "DOMAIN",
    "SC",
    "SELLER",
    "ENVIRONMENT",
    "LOCALE",
    "COUNTRY",
    "CURRENCY",
    "TIMEOUT_SECONDS",
    "MAX_RETRIES",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in KEYS:
        monkeypatch.delenv(f"VTEX_{key}", raising=False)


def set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for key, value in {**VALID, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_valid_load_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    s = load_settings()
    assert (s.account, s.domain, s.sc, s.seller) == ("acme-store", "www.acme.com.br", "1", "1")
    assert s.environment == "vtexcommercestable"
    assert (s.locale, s.country, s.currency) == ("pt-BR", "BRA", "BRL")
    assert s.timeout_seconds == 10.0
    assert s.max_retries == 3


def test_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    s = load_settings()
    assert s.base_url == "https://www.acme.com.br"
    assert s.commercestable_url == "https://acme-store.vtexcommercestable.com.br"
    assert s.checkout_url("abc123") == "https://www.acme.com.br/checkout/?orderFormId=abc123#/cart"
    with pytest.raises(ValueError):
        s.checkout_url("abc&x=1")


@pytest.mark.parametrize("key", ["ACCOUNT", "DOMAIN", "SC", "SELLER"])
def test_missing_key_is_named(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    set_env(monkeypatch)
    monkeypatch.delenv(f"VTEX_{key}")
    with pytest.raises(VtexConfigError, match=f"VTEX_{key} is required"):
        load_settings()


@pytest.mark.parametrize("key", ["ACCOUNT", "DOMAIN", "SC", "SELLER"])
def test_empty_key_is_named(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    set_env(monkeypatch, **{f"VTEX_{key}": "  "})
    with pytest.raises(VtexConfigError, match=f"VTEX_{key} is required"):
        load_settings()


def test_all_problems_listed_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VTEX_SC", "abc")
    with pytest.raises(VtexConfigError) as info:
        load_settings()
    message = str(info.value)
    for name in ("VTEX_ACCOUNT", "VTEX_DOMAIN", "VTEX_SELLER", "VTEX_SC must be a numeric"):
        assert name in message


@pytest.mark.parametrize(
    "domain", ["https://www.acme.com.br", "www.acme.com.br/path", "www acme.com", "-bad.com"]
)
def test_bad_domain_rejected(monkeypatch: pytest.MonkeyPatch, domain: str) -> None:
    set_env(monkeypatch, VTEX_DOMAIN=domain)
    with pytest.raises(VtexConfigError, match="VTEX_DOMAIN"):
        load_settings()


def test_domain_trailing_slash_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, VTEX_DOMAIN="www.acme.com.br/")
    assert load_settings().domain == "www.acme.com.br"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("VTEX_SC", "1a"),
        ("VTEX_ACCOUNT", "Acme_Store"),
        ("VTEX_COUNTRY", "br"),
        ("VTEX_CURRENCY", "brl"),
        ("VTEX_LOCALE", "ptBR"),
        ("VTEX_TIMEOUT_SECONDS", "abc"),
        ("VTEX_TIMEOUT_SECONDS", "0"),
        ("VTEX_MAX_RETRIES", "99"),
        ("VTEX_MAX_RETRIES", "-1"),
        ("VTEX_MAX_RETRIES", "many"),
    ],
)
def test_invalid_values_rejected(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    set_env(monkeypatch, **{key: value})
    with pytest.raises(VtexConfigError, match=key):
        load_settings()


def test_numeric_errors_name_the_variable_once_each(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, VTEX_TIMEOUT_SECONDS="abc", VTEX_MAX_RETRIES="99")
    with pytest.raises(VtexConfigError) as info:
        load_settings()
    message = str(info.value)
    assert "VTEX_TIMEOUT_SECONDS: " in message
    assert "VTEX_MAX_RETRIES: " in message


def test_env_file_is_used_only_when_given(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "vtex.env"
    env.write_text("\n".join(f"{k}={v}" for k, v in VALID.items()))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(VtexConfigError):
        load_settings()
    assert load_settings(env).account == "acme-store"


def test_no_credential_fields() -> None:
    names = set(VtexSettings.model_fields)
    assert not {n for n in names if "key" in n or "token" in n}
