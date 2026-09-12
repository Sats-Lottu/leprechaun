# Security policy

## Current status

Leprechaun is pre-release software. There is no production-supported release yet.
The development stack uses simulated funds and public demonstration credentials.
Do not expose it to the internet or use it for real funds.

Known blockers include withdrawal reservation and recovery, partial settlement
recovery, and concurrency/redelivery validation. See
[the project status](README.md#estado-do-projeto).

## Reporting a vulnerability

Do not disclose an unpatched vulnerability or credentials in a public issue.
If this repository's hosting platform provides private vulnerability reporting,
use its **Security â†’ Report a vulnerability** flow. If unavailable, request a
private reporting channel from a maintainer without publishing exploit details.
No response-time guarantee is currently offered.

Include affected versions or commits, a minimal reproduction with synthetic data,
expected and observed behavior, and impact. Omit real tokens, invoices containing
private information, and database exports.

## Credentials and exposure

- `.env.example` contains placeholders; `compose.local.yaml`, local bootstrap
  scripts, and the imported realm contain intentional demonstration credentials.
- Store real configuration in ignored `.env` files or a secret manager. Never
  reuse demonstration credentials, signing secrets, or service tokens.
- Keep Ledger, PLS, databases, and RabbitMQ private. Default published Compose
  ports bind to loopback. Remote deployment needs an explicit network and TLS plan.
- Application API keys belong on application servers. Browser redirects are not
  payment receipts; the application must verify `settled` through the API.
- Rotate exposed credentials at their issuer first. Removing a string from the
  latest revision does not invalidate a credential or remove it from history.
- Protect backups and logs. Test restoration without using production data in
  development. Do not attach local databases or session stores to bug reports.

CI checks do not replace secret scanning, dependency review, or an independent
security assessment. Inspect selected files before publishing.
