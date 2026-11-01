# SmallTimeTrader

A self-hosted crypto monitoring and Binance **spot** trading platform with a responsive control website, mandatory password + authenticator MFA, WhatsApp notifications and three deterministic agents.

**Initial state: paper mode, new trades paused, WhatsApp disabled.** No credentials or users are included. No subscription or real-money trading is activated by installing the repository.

## What runs

| Component | Job |
|---|---|
| Master agent | Persistent 10-minute cycles, coordinates agents, reconciles uncertain orders before trading |
| Monitoring agent | Reads Binance, Coinbase Exchange and Kraken APIs; measures USDT/USD; validates three-source agreement and freshness |
| Trading agent | Momentum/reversal rules, managed-position risk checks, paper fills or Binance IOC limit orders |
| FastAPI website | MFA login, administrator/viewer access, validated configuration, pause switch, prices, order history, notification submissions and audit events |
| SQLite | Durable quotes, portfolio ledger, orders, sessions, MFA replay counters, cooldowns and audit records |

The agents are independently testable Python components coordinated in one service. They do not require an LLM, CrewAI or API inference costs. Numeric trading rules are deterministic.

## Quick start (Python 3.11+)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m smalltimetrader.cli key
```

Keep the generated key private, then configure it in your shell (replace the placeholder with the actual generated key):

```powershell
$env:APP_ENCRYPTION_KEY = '<generated key>'
$env:COOKIE_SECURE = 'false' # localhost HTTP only
python -m smalltimetrader.cli create-user --username admin
python -m uvicorn smalltimetrader.app:app --host 127.0.0.1 --port 8000 --workers 1
```

Open http://127.0.0.1:8000. The CLI prompts for a password and displays a TOTP enrolment secret/URI. Add it to an authenticator app and prove possession before the account is created. There are no default passwords, no MFA bypass and no public registration. For a read-only user, use `create-user --username analyst --role viewer`. A TOTP code cannot be replayed, including after service restart.

The app reads **environment variables**, not `.env` automatically. Docker Compose below loads `.env`. Save the encryption key in your deployment's secret store; changing or losing it makes existing MFA secrets unreadable. Store enrolment secrets in an operator password vault; application recovery codes are not implemented. Operators can revoke access by deleting a user's sessions and user row during maintenance, then enrolling a replacement through the CLI. Back up the database and encryption key separately.

## HTTPS deployment with Docker Compose

1. Copy `.env.example` to `.env`; generate and set `APP_ENCRYPTION_KEY`.
2. Set `DOMAIN=trader.your-domain.example`, `ALLOWED_HOSTS=trader.your-domain.example` and `COOKIE_SECURE=true`. Point DNS to your server. Permit inbound TCP 80/443.
3. Run `docker compose build`.
4. Enrol a user: `docker compose run --rm app python -m smalltimetrader.cli create-user --username admin`.
5. Run `docker compose up -d`.

Caddy terminates HTTPS. Only Caddy is published to the host; the backend stays on the internal Docker network. Keep **one application worker, one replica and one local database volume**. An OS lock rejects duplicate workers using the same database file; this is not a distributed multi-host lock. Apply host backups, disk encryption, service monitoring and firewall restrictions before production use.

This is a repository-hosted full backend, rather than a published Sites page. Hosting the trading worker continuously requires a server/container; a static website alone cannot execute the schedule. No website has been deployed automatically.

## Price and signal definitions

Defaults: BTC and ETH; sample every 600 seconds; comparison window 600 seconds.

`change_pct = 100 × (current normalised USD price / prior normalised USD price − 1)`

Each exchange is compared with **its own** stored sample closest to the target baseline within ±45 seconds. The service needs one full comparison window after first startup. Missing baselines do not create signals. A changed coin set or comparison window clears samples to force a new warm-up. A window must be a multiple of the sampling interval. Downtime creates gaps, never fabricated baselines.

Binance uses USDT-quoted markets. Coinbase and Kraken use USD. Kraken's observed USDT/USD trade converts Binance prices to USD; a 1:1 conversion is never assumed. The conversion must itself be recent and within the implemented sanity band 0.8–1.2 USD. A stale/missing conversion blocks that cycle.

All three venues must respond with finite, positive prices and recent trade timestamps. Future timestamps and crossed books are rejected. Default maximum trade age is 60 seconds. The normalised price range divided by the median must be no more than the configured 0.75%. Last-trade freshness is deliberately strict, so illiquid coins may be blocked even if their HTTP endpoint responds.

The median venue change must cross the threshold and at least two venues must cross it in the same direction:

* Alert: absolute change **≥ 1%**.
* Trade: absolute change **> 2%**.
* Momentum (default): buy on a positive signal, sell on a negative signal.
* Reversal: buy on a negative signal, sell on a positive signal.

Spot sells use only positions acquired by this application in the current execution mode; they do not open short positions or liquidate unrelated holdings. Use a dedicated Binance account/subaccount and do not manually trade its managed assets. Futures, margin and withdrawals are not implemented.

Alerts have per-asset, per-direction persistent cooldowns. Trades have per-asset, per-mode cooldowns. Defaults are 30 minutes. A new qualifying window after cooldown can trigger another action. Changes are not measured against the last alert or a 24-hour price.

Polling can miss moves between observations. This version does not implement continuous WebSocket capture.

## Risk and execution

All operational thresholds are editable through the administrator website. Secrets and deployment identity remain server configuration.

* Default order: 25 USDT; maximum managed exposure: 100 USDT.
* Daily managed-portfolio loss cap: 10 USDT, using mark-to-market equity against the first valuation of each UTC day. Buys stop at the cap; reductions remain eligible.
* Daily executed turnover cap: 200 USDT. Both buy and sell orders count. The cap can prevent an exit; configure sufficient headroom.
* Spread cap: 0.3%; slippage price allowance: 0.2%.
* Stop-loss: 3%; take-profit: 6%, evaluated against the managed position's average acquisition cost at polling time. These generate a sell tranche up to the configured order value and bypass normal signal cooldown. **They are not exchange-resident stop orders**, do not guarantee full liquidation and do not run while trading is paused. Data validation and execution/turnover restrictions still apply.
* Paper cash starts at 1,000 USDT, with a simulated 0.1% fee. Starting cash only applies before a mode's ledger is first created; changing the setting never resets existing money/positions.
* Paper fills assume full liquidity at observed bid/ask. They exclude network latency, market impact and order-book depth and are explicitly labelled simulated.
* Binance execution uses `LIMIT` + `IOC`, not unbounded market orders. Partial fills are recorded; an unfilled remainder expires. Quantity, price and notional filters and actual free balances are checked before submission. Binance remains the final authority for all exchange filters.
* A durable client order ID and cooldown reservation precede submission. Unknown responses block all new trades across modes until reconciled. The service queries the original order; it never blindly resubmits the POST.
* Query failures, including an order-not-found response after an uncertain submission, remain blocked for operator investigation. There is intentionally no web button that discards an unknown order. Restarting does not remove the block.
* Fill reconciliation accounts for commissions charged in USDT or the base asset. Disable BNB fee payment. Third-asset commission or an inconsistent fill enters `NEEDS_REVIEW` and blocks trading until an operator reconciles the ledger against Binance.
* Live/testnet ledger cash records managed cash flows, not your entire exchange wallet balance. Risk limits cover positions created by the application, not outside holdings. Live/testnet modes have separate ledgers and keys; paper fills never reach Binance.

The implementation stores ledger quantities as SQLite floating-point values; Binance order rounding uses Decimal. Complete independent security review, financial precision review and realistic strategy backtesting before funding a production account. A 2% movement does not establish a profitable strategy.

## Binance configuration

Testnet: set `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET`. Pause, select `testnet`, save, then separately unpause. Market signals still come from production public markets; **testnet execution prices and liquidity can differ substantially**.

Live: set `BINANCE_API_KEY`, `BINANCE_API_SECRET` and `ENABLE_LIVE_TRADING=true` on the server. Give the key only the necessary spot trading/account-read permissions, disable withdrawals, restrict it to the worker's outbound IP and verify account/region eligibility yourself. Keep your server clock synchronised. Pause, select `live`, save, then separately unpause. No live trades were placed during development.

Live mode cannot be selected when the server gate is disabled. Password + TOTP are required for all users, including viewers. One-hour opaque sessions use HttpOnly/SameSite cookies and server-side revocation; state-changing endpoints require the session's CSRF token. Login throttling persists per account and IP. Put additional rate limiting in front of the service for internet deployment. Do not trust incoming forwarding headers; the included proxy passes the request but the app uses its direct client address for throttling (shared proxy-IP throttling is conservative).

Pause stops **new** trades. It does not reverse a fill, interrupt an in-flight IOC or cancel exchange orders manually created outside the application. The pause request waits for the active agent cycle to finish; reconciliation and monitoring continue.

## WhatsApp

Configure a Twilio WhatsApp sender and an approved message template with variables:

1. Asset, e.g. BTC.
2. Signed median change, e.g. +1.25%.
3. Comparison window in minutes.
4. Observation time in UTC.

Example template: `SmallTimeTrader: {{1}} moved {{2}} over {{3}} minutes. Observed {{4}}.`

Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` (e.g. `whatsapp:+...`) and `TWILIO_CONTENT_SID`. Enter the destination in international E.164 format on the website and enable WhatsApp after recipient opt-in. Confirm provider/Meta eligibility and template approval for the crypto notification use case; availability is not guaranteed.

The outbox distinguishes submission from delivery. `ACCEPTED` means Twilio returned a message SID, **not delivered**. Check delivery in the provider console; delivery-status webhooks are not implemented. `UNKNOWN` messages are not automatically resent because that could duplicate a notification. A crash can leave `SENDING`; inspect Twilio before retrying manually. Alerts remain in the local audit log if WhatsApp is disabled or unavailable. No message was sent during development.

## Tests

```powershell
python -m pytest -q
node --check smalltimetrader/static/app.js
```

Tests use temporary databases, deterministic inputs and mocked HTTP/execution. They cover threshold inclusivity, stale/outlier data, conversion and adapter parsing, baseline warm-up, WhatsApp cooldowns, password+MFA/replay/lockout, CSRF/roles/live gating, paper accounting, restart cooldowns, loss/exposure controls, spot-only sells, stop-loss exits, unknown-order blocking and idempotent settlement. They do not place real orders, send WhatsApp messages or prove live connectivity/profitability.

On a restricted Windows host where Python's standard event-loop wake-up socket is blocked, set `STT_INPROCESS_TEST_LOOP=1` and supply a writable pytest `--basetemp`. This enables a polling-only, socket-free **test** loop that explicitly rejects real descriptor I/O. It is not used by the application or normal CI and does not validate network behaviour.

## Official references

* Binance Spot API: https://developers.binance.com/en/docs/binance-spot-api-docs
* Binance Spot Testnet: https://testnet.binance.vision/
* Coinbase Exchange API: https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductticker
* Kraken REST API: https://docs.kraken.com/api/docs/rest-api/get-ticker-information/
* Twilio WhatsApp templates: https://www.twilio.com/docs/whatsapp/tutorial/send-whatsapp-notification-messages-templates

## Scope remaining before production

This repository is an initial deployable implementation, not an independently audited trading product. Production exchange/WhatsApp connectivity, real fill behaviour, internet-hosted TLS and browser compatibility must be verified in the chosen hosting environment. Production hardening should add automated encrypted backups, external health alerts, provider delivery webhooks, managed identity/recovery workflows, realistic historical backtesting and stronger multi-user administration if needed. No managed cloud resources or Sites deployment are provisioned here.
