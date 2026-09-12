#!/bin/sh
set -eu

# Executa as migrações do banco de dados
poetry run alembic upgrade head

# Inicia a aplicação
exec poetry run python -m pls.main
