# Self-hosted operation

Leprechaun runs on infrastructure controlled by the operator. The supplied Compose
files are a development starting point, not a production deployment recipe.
See the [README](../README.md#estado-do-projeto) for the remaining requirements
before operating with real funds.

## Container images

After the service checks pass, GitHub Actions publishes Linux AMD64 images to
GitHub Container Registry on pushes to `main` and version tags:

- `ghcr.io/sats-lottu/leprechaun-hub`
- `ghcr.io/sats-lottu/leprechaun-ledger`
- `ghcr.io/sats-lottu/leprechaun-pls`

Each image has a `sha-<commit>` tag. Pushes to `main` also update `latest`,
and SemVer tags produce matching image tags such as `v1.2.3`. Pin the same
version or commit tag for all three services in a deployment.

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

Register the Hub as an OpenID Connect client in Keycloak with the exact public
callback URL `https://<hub-host>/auth/callback`, the `authorization_code` grant, `code`
response type, and `openid profile` scopes. Set `OIDC_ISSUER` to
`https://<keycloak-host>/realms/<realm>`, `OIDC_SERVER_METADATA_URL` to the realm's
`/.well-known/openid-configuration`, and `OIDC_CLIENT_ID` to the registered ID.
Use `client_secret_basic` and `OIDC_CLIENT_SECRET` for a confidential client,
or `none` with an empty secret for a public client. Both use PKCE S256.

Install the Sats Lottu theme and the `lnurl-auth` and `nostr` providers in
Keycloak. Enable both providers and select the `sats-lottu` login theme in the
realm. Use a stable public HTTPS hostname for LNURL-auth because wallets derive
their identity keys from the hostname.

When Keycloak uses a dynamic backchannel, set `OIDC_SERVER_METADATA_URL` to the
internal discovery URL and keep the public realm URL as `OIDC_ISSUER`. If
discovery advertises public endpoints that the Hub cannot reach, set
`OIDC_INTERNAL_BASE_URL` to the full private realm URL. The override applies to
token, UserInfo, and JWKS endpoints under the public issuer. The Hub validates
the issuer and client ID on signed ID tokens. Hub logout clears the Hub session.
Production-facing authentication needs TLS and secure cookies
(`SESSION_HTTPS_ONLY=true`), with a reviewed reverse-proxy configuration.

Use separate Ledger tokens per internal service and only the required scopes.
Grant Hub administrative access through the trusted operator console as described
in [application integration](application-integration.md). Do not reuse the local
demonstration user for an actual deployment.

When moving an existing deployment to Keycloak, review identity mapping before
allowing payments: Ledger ownership and Hub roles are derived from the subject.
Existing users are not automatically matched by email or login. Only the isolated
local demo uses a fixed subject; real identity data requires an operator-managed
migration and backup.

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
