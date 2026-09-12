# Ambiente local com pagamentos simulados

> **NÃO USE O BOOTSTRAP EM PRODUÇÃO.** A pasta `local-bootstrap/` cria
> identidades de demonstração e saldos fictícios, com credenciais públicas.
> Execute somente em ambiente local isolado, sem fundos reais.


Este ambiente usa Keycloak e LNbits FakeWallet. Os saldos sao ficticios.
Requer Docker Compose e Python 3.11+ para o comando de demonstracao.

## Iniciar

Na raiz do repositorio:

```sh
docker compose --env-file .env.example -f compose.yaml -f compose.local.yaml up -d --build
```

O projeto Compose `leprechaun-local` usa volumes separados. A inicializacao cria
os bancos, aplica migracoes, configura a wallet LNbits e cria as contas locais.
Reexecutar o comando preserva os saldos existentes. O script de bancos somente
e executado pelo PostgreSQL quando o volume esta vazio.

- Hub: http://localhost:8000
- OIDC: http://localhost:8080
- LNbits: http://localhost:5000
- Prometheus: http://localhost:9090

Login no Hub: `player` / `local-player-password`.
Administracao local do Keycloak: `admin` / `local-admin-password`.
LNbits local: `localadmin` / `local-lnbits-password`.
Essas credenciais sao exclusivas do ambiente de desenvolvimento.

## Demonstrar uma compra

Antes da primeira compra, conceda a funcao admin ao usuario local conforme
[aplicacoes conectadas](application-integration.md), entre novamente e cadastre
`local-demo` em `/admin/applications`, com site `http://localhost:8000` e conta
`00000000-0000-0000-0000-000000000200`. Guarde a chave apresentada e configure no
PowerShell: `$env:LEPRECHAUN_API_KEY = 'CHAVE_EXIBIDA'`.

1. Entre no Hub usando o login acima.
2. Execute `python local-bootstrap/local-demo.py checkout --sats 100`.
3. Abra o endereco retornado e clique em **Confirm payment**. Somente apos
   confirmar o checkout reserva saldo e solicita a invoice ao PLS.
4. Copie o `session_id` da URL e execute:

```sh
python local-bootstrap/local-demo.py pay-checkout SESSION_ID
```

5. Mantenha o checkout aberto: ele verifica o pagamento e retorna a URL
   configurada depois da liquidacao (`settled`).
   Se a sessao OIDC expirou, entre novamente e abra o mesmo checkout.
6. Abra a wallet para verificar o extrato.

O simulador cria sua propria wallet LNbits e usa saldo ficticio para pagar a
invoice. O evento percorre LNbits, PLS, RabbitMQ e Ledger. A liquidacao debita
a conta do usuario e credita a conta do jogo
`00000000-0000-0000-0000-000000000200`.

A reserva Lightning local e a conta
`00000000-0000-0000-0000-000000000100`, criada com saldo inicial simulado de
1.000.000 sats. O bootstrap nao recarrega automaticamente esse saldo.

Para gerar uma invoice destinada a um teste de saque:

```sh
python local-bootstrap/local-demo.py withdrawal-invoice --sats 10
```

O usuario precisa ter saldo disponivel no Ledger antes de solicitar o saque.

## Verificar e parar

```sh
docker compose --env-file .env.example -f compose.yaml -f compose.local.yaml ps -a
docker compose --env-file .env.example -f compose.yaml -f compose.local.yaml logs --tail 50 hub ledger pls
docker compose --env-file .env.example -f compose.yaml -f compose.local.yaml exec ledger poetry run python -m ledger.reconcile
docker compose --env-file .env.example -f compose.yaml -f compose.local.yaml down
```

`lnbits-init` terminar com codigo zero e esperado. `down` preserva os volumes.
O Keycloak local reimporta o realm quando seu container e recriado; use-o apenas
para as identidades de demonstracao.

## Testes

Em cada pasta `hub`, `ledger` e `pls`:

```sh
poetry install
poetry run python -m pytest -q
```

O Docker deve estar acessivel para Testcontainers. Em ambientes Windows movidos
de outra pasta, os executaveis de console da virtualenv podem conter caminhos
antigos; `python -m pytest` evita esses launchers.

## Limites atuais

O ambiente local valida a integracao, mas nao representa prontidao de producao.
Antes de usar fundos reais ainda e necessario concluir reserva e recuperacao duravel do saque, recuperacao de falhas entre
liquidacoes parciais e testes de concorrencia e redelivery. O saque atual envia
o pagamento antes de registrar o debito e nao deve operar com fundos reais.
As confirmacoes externas e a reconciliacao contabil precisam continuar sendo
as fontes de verificacao.

Referencias de configuracao:
[Keycloak em containers](https://www.keycloak.org/server/containers) e
[LNbits FakeWallet](https://docs.lnbits.org/guide/wallets.html).
