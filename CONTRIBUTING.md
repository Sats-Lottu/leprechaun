# Contributing to Leprechaun

Leprechaun is an MIT-licensed, self-hosted balance and checkout platform. It is
under development; use simulated funds when developing and reviewing changes.
See [README](README.md), [local setup](docs/local-development.md), and
[application integration](docs/application-integration.md).

## Development

Use Python 3.11, Poetry 2, and Docker with Compose. Each of `hub`, `ledger`, and
`pls` has its own `pyproject.toml` and committed `poetry.lock`. From the service
directory:

```sh
poetry install
poetry run python -m ruff check .
poetry run python -m pytest -q
```

Integration tests create temporary containers. Docker must be running and
accessible. Tests must not use production credentials or databases. When changing
dependencies, update and include the corresponding lockfile. Keep migrations with
the code that requires them; never edit an already released migration in place.

## Pull requests

Keep each change focused. Explain the problem, behavior, validation, and remaining
risks. Add regression tests for changes to balances, payment states, authentication,
authorization, idempotency, or recovery. Document public API and deployment changes.

Use English Conventional Commit messages, such as
`fix(checkout): Preserve payment confirmation`. Keep formatting-only changes
separate from behavior changes when practical. Do not submit generated reports,
local databases, session stores, secrets, or personal editor settings.

Before staging, run from the repository root:

```sh
git diff --check
git status --short
```

After selecting files, inspect `git diff --cached` and run
`git diff --cached --check`. Review selected files for secrets and local data
before committing. Use a dedicated secret scanner during publication review.

## Security and licensing

Report vulnerabilities using [SECURITY.md](SECURITY.md). Never include real payment
credentials, session cookies, private keys, or user records in issues or fixtures.
Use synthetic identities and clearly marked development credentials.

Submit only work you have the right to contribute under the [MIT license](LICENSE).
Preserve third-party notices. Dependency licenses remain applicable independently
of this repository's license.

## AI-assisted contributions

Human contributors are responsible for understanding and reviewing all submitted
code, tests, and documentation, including AI-assisted changes. Disclose meaningful
assistance in the pull request or an `Assisted-by:` commit trailer. Describe what
was assisted and how it was checked. Do not claim human review before it occurred.

AI tools are not authors, signers, or approvers. They must not add `Signed-off-by`
trailers or certify licensing or DCO compliance. Changes affecting payments,
identity, secrets, or deployment require particular care during human review.

Treat contributors respectfully and discuss technical disagreements with concrete
examples and reproducible evidence.
