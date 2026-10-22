# VTEX proof of concept

Foundation for a VTEX storefront backend: settings, money helpers, and an HTTP client for
the public storefront APIs. Business mapping (search, cart) is not included yet.

- `settings.py`: `VtexSettings` and `load_settings` (reads `VTEX_*`; no app key or token).
- `money.py`: Decimal conversions for cents and decimal prices, `format_brl`.
- `client.py`: `VtexClient` with per-session cookie jars, retries for read-only calls, and
  a guard that refuses customer-data endpoints.

## Environment

Required: `VTEX_ACCOUNT`, `VTEX_DOMAIN`, `VTEX_SC`, `VTEX_SELLER`. Optional:
`VTEX_ENVIRONMENT`, `VTEX_LOCALE`, `VTEX_COUNTRY`, `VTEX_CURRENCY`, `VTEX_TIMEOUT_SECONDS`,
`VTEX_MAX_RETRIES`. See `.env.example`; pass the file to `load_settings(env_file)`.

## Tests

From the repo root:

```bash
ruff check examples/vtex_poc && ruff format --check examples/vtex_poc && pytest examples/vtex_poc -q
```

Tests use `httpx.MockTransport`; no network is called.
