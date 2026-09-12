# Self-hosted operation

Leprechaun runs on infrastructure controlled by the operator. The supplied Compose
files are a development starting point, not a production deployment recipe.
Real-fund operation remains blocked by the recovery and withdrawal limitations
listed in the [README](../README.md#estado-do-projeto).

## Configuration boundaries

- Copy `.env.example` to an ignored `.env` for private configuration. Replace
  placeholder passwords, Ledger service tokens, and both Hub signing secrets.
- `compose.local.yaml` deliberately provisions public demonstration identities,
  FakeWallet funds, and separate local volumes. Do not use it for public hosting.
- The base Compose file also configures LNbits FakeWallet. A real payment backend
  requires a separate operator-reviewed configuration; changing credentials alone
  does not make this stack production-ready.
- Published ports bind to `127.0.0.1`. Use `docker compose port rabbitmq 15672`
  with your usual Compose options to locate the allocated management port.
- Keep Ledger, PLS, PostgreSQL, and RabbitMQ on private networks. Applications
  integrate through the Hub rather than obtaining internal Ledger credentials.

## Identity and credentials

Configure the OIDC issuer, discovery URL, client ID, optional client secret, and
exact callback URL together. The issuer must match the token's `iss` claim.
Production-facing authentication needs TLS and secure cookies
(`SESSION_HTTPS_ONLY=true`), with a reviewed reverse-proxy configuration.

Use separate Ledger tokens per internal service and only the required scopes.
Grant Hub administrative access through the trusted operator console as described
in [application integration](application-integration.md). Do not reuse the local
demonstration user for an actual deployment.

LNbits supports configured API keys or username/password authentication. Configure
the intended wallet explicitly. Runtime keys created by the local bootstrap are
stored in the local configuration volume, not in repository files.

## Data and upgrades

Hub, Ledger, and PLS use separate PostgreSQL databases. LNbits has its own persisted
data, and identity data belongs to the configured OIDC provider. Back up all of
these together with the private deployment configuration using an operator-defined
retention and encryption policy. Restrict backup access and test restoration.

Before upgrading, stop new payment activity, account for in-flight operations,
take verified backups, and review schema migrations and compatibility. Service
entrypoints apply Alembic migrations at startup; do not start multiple migration
writers against the same database. Image rollback alone does not reverse a schema
change. No zero-downtime or automatic recovery guarantee is currently provided.

`docker compose down` preserves named volumes by default. Adding `--volumes`
deletes them and must not be part of a routine update procedure.

## Verification

Check service health, payment processing failures, outbox/reconciliation failures,
and expired holds. Run the Ledger reconciliation command documented in the README
after controlled payment tests and recovery exercises. A healthy HTTP endpoint
does not prove that payments or accounting are correct.

Application delivery must be idempotent and tied to the authoritative `settled`
state. Define how to resolve a paid order whose product or bet was not delivered.
The current administration pages other than application management include basic
summaries/placeholders; they are not a complete financial operations console.

Before publishing a release, run all service tests, inspect migrations and
dependency licenses, perform credential scanning, and review the known production
blockers. CI checks do not certify production readiness.
