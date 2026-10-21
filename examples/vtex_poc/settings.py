# SPDX-License-Identifier: Apache-2.0
"""Settings for the VTEX proof of concept, read from `VTEX_*` environment variables.

The POC only calls public storefront APIs, so there is no app key or app token. Settings
read an env file only when `load_settings` is given one; nothing reads the repo-root `.env`.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_SLUG = re.compile(_LABEL)
_HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})*(?::\d{{1,5}})?")
_LOCALE = re.compile(r"[a-z]{2}-[A-Z]{2}")
_UPPER3 = re.compile(r"[A-Z]{3}")
_ORDER_FORM_ID = re.compile(r"[A-Za-z0-9]+")


class VtexConfigError(ValueError):
    """Raised by `load_settings` with every missing or invalid key in one message."""


def _required(name: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"VTEX_{name.upper()} is required")
    return value


class VtexSettings(BaseSettings):
    """Connection settings for one VTEX store."""

    model_config = SettingsConfigDict(env_prefix="VTEX_", env_file=None, extra="ignore")

    account: str
    domain: str
    sc: str
    seller: str
    environment: str = "vtexcommercestable"
    locale: str = "pt-BR"
    country: str = "BRA"
    currency: str = "BRL"
    timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)

    @field_validator("account")
    @classmethod
    def _check_account(cls, value: str) -> str:
        value = _required("account", value)
        if not _SLUG.fullmatch(value):
            raise ValueError("VTEX_ACCOUNT must be a lowercase slug of [a-z0-9-]")
        return value

    @field_validator("domain")
    @classmethod
    def _check_domain(cls, value: str) -> str:
        value = _required("domain", value).removesuffix("/")
        if "://" in value or "/" in value or any(c.isspace() for c in value):
            raise ValueError("VTEX_DOMAIN must be a bare host such as www.example.com.br")
        if not _HOST.fullmatch(value.lower()):
            raise ValueError("VTEX_DOMAIN is not a valid host name")
        return value.lower()

    @field_validator("sc")
    @classmethod
    def _check_sc(cls, value: str) -> str:
        value = _required("sc", value)
        if not value.isascii() or not value.isdigit():
            raise ValueError("VTEX_SC must be a numeric sales channel id")
        return value

    @field_validator("seller")
    @classmethod
    def _check_seller(cls, value: str) -> str:
        return _required("seller", value)

    @field_validator("environment")
    @classmethod
    def _check_environment(cls, value: str) -> str:
        value = value.strip()
        if not _SLUG.fullmatch(value):
            raise ValueError("VTEX_ENVIRONMENT must be a slug of [a-z0-9-]")
        return value

    @field_validator("locale")
    @classmethod
    def _check_locale(cls, value: str) -> str:
        if not _LOCALE.fullmatch(value):
            raise ValueError("VTEX_LOCALE must look like pt-BR")
        return value

    @field_validator("country")
    @classmethod
    def _check_country(cls, value: str) -> str:
        if not _UPPER3.fullmatch(value):
            raise ValueError("VTEX_COUNTRY must be 3 uppercase letters, e.g. BRA")
        return value

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str) -> str:
        if not _UPPER3.fullmatch(value):
            raise ValueError("VTEX_CURRENCY must be 3 uppercase letters, e.g. BRL")
        return value

    @property
    def base_url(self) -> str:
        """Store domain. Public APIs are called here so checkout cookies match the handoff."""
        return f"https://{self.domain}"

    @property
    def commercestable_url(self) -> str:
        """Account host, a fallback when the store domain does not proxy the APIs."""
        return f"https://{self.account}.{self.environment}.com.br"

    def checkout_url(self, order_form_id: str) -> str:
        """Hosted checkout link for an existing orderForm."""
        if not _ORDER_FORM_ID.fullmatch(order_form_id):
            raise ValueError("order_form_id must be alphanumeric")
        return f"https://{self.domain}/checkout/?orderFormId={order_form_id}#/cart"


def load_settings(env_file: str | Path | None = None) -> VtexSettings:
    """Load settings, raising one `VtexConfigError` that lists every problem."""
    try:
        return VtexSettings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        problems: list[str] = []
        for err in exc.errors():
            name = f"VTEX_{str(err['loc'][0]).upper()}" if err["loc"] else "VTEX"
            if err["type"] == "missing":
                problems.append(f"{name} is required")
            else:
                message = err["msg"].removeprefix("Value error, ")
                problems.append(message if name in message else f"{name}: {message}")
        raise VtexConfigError("Invalid VTEX configuration: " + "; ".join(problems)) from None
