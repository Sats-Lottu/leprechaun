# Ledger Service

O `ledger-service` e o servico financeiro interno do Micro Lotto.

Ele guarda a verdade sobre saldo, reservas e movimentacoes. Outros servicos
podem pedir pagamentos, criar invoices ou exibir carteira, mas o saldo confiavel
sempre deve estar aqui.

---

## Para que ele serve

O ledger resolve quatro problemas centrais:

* criar contas financeiras
* consultar saldo total, reservado e disponivel
* reservar saldo temporariamente com holds
* registrar transacoes contabeis com debitos e creditos

Ele foi desenhado para fluxos sincrononos, como uma transferencia interna, e
fluxos assincronos, como um checkout que depende de confirmacao externa.

---

## Modelo mental

### Conta

Uma conta representa uma entidade que possui saldo.

Exemplos:

* usuario
* jogo
* plataforma
* escrow
* conta de taxas

Cada conta possui:

```text
balance
reserved_balance
available_balance = balance - reserved_balance
```

---

### Transacao

Uma transacao agrupa uma operacao financeira completa.

Toda transacao contabil deve ser balanceada:

```text
soma(debitos) == soma(creditos)
```

Exemplo: usuario paga 100 para um jogo.

```text
debit  100 conta_usuario
credit 100 conta_jogo
```

---

### Hold

Um hold reserva saldo sem debitar imediatamente.

Exemplo:

```text
balance = 100
reserved_balance = 0
available_balance = 100
```

Depois de criar um hold de 40:

```text
balance = 100
reserved_balance = 40
available_balance = 60
```

Se o hold for consumido por uma transacao contabil `posted`:

```text
balance = 60
reserved_balance = 0
```

Se o hold for liberado:

```text
balance = 100
reserved_balance = 0
```

---

## O que o ledger faz

* cria contas
* consulta saldo
* lista extrato de lancamentos
* cria transacoes pendentes
* posta transacoes, aplicando saldo
* reverte transacoes postadas
* cria holds
* consome holds postando uma transacao contabil associada
* libera holds
* expira holds vencidos por endpoint ou worker operacional
* aplica idempotencia em operacoes que podem sofrer retry

---

## O que o ledger nao faz

O ledger nao decide regra de jogo e nao integra diretamente com provider externo.

Ele nao deve:

* criar invoice Lightning
* conversar diretamente com LNbits
* decidir se uma aposta venceu
* renderizar UI
* aplicar regra de negocio fora do saldo financeiro

Essas responsabilidades ficam em outros servicos. O ledger apenas registra e
protege a verdade financeira interna.

---

## API

A aplicacao FastAPI registra tres grupos de rotas:

* `/accounts`
* `/holds`
* `/transactions`

Documentacao detalhada das rotas:

* [`ledger/routes/description.md`](ledger/routes/description.md)

Documentacao do modelo de dados:

* [`ledger/models/description.md`](ledger/models/description.md)

---

## Endpoints principais

### Contas

```text
POST /accounts
GET  /accounts/{account_id}/balance
GET  /accounts/{account_id}/statement
```

### Holds

```text
POST /holds
GET  /holds/{hold_id}
GET  /holds/account/{account_id}
POST /holds/expire-due
POST /holds/{hold_id}/expire
POST /holds/{hold_id}/consume
POST /holds/{hold_id}/release
```

### Transacoes

```text
POST /transactions
GET  /transactions/{transaction_id}
POST /transactions/{transaction_id}/post
POST /transactions/{transaction_id}/reverse
```

---

## Exemplos rapidos

### Criar uma conta

```bash
curl -X POST "http://localhost:8000/accounts?account_type=user&owner_id=00000000-0000-0000-0000-000000000001&name=Alice"
```

Resposta:

```json
{
  "account_id": "uuid",
  "account_type": "user",
  "owner_id": "00000000-0000-0000-0000-000000000001",
  "name": "Alice"
}
```

---

### Consultar saldo

```bash
curl "http://localhost:8000/accounts/{account_id}/balance"
```

Resposta:

```json
{
  "balance": 100,
  "reserved_balance": 40,
  "available_balance": 60
}
```

---

### Criar um hold

```bash
curl -X POST "http://localhost:8000/holds" \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": "uuid",
    "amount": 40,
    "reason": "checkout",
    "reference_type": "payment_order",
    "reference_id": "00000000-0000-0000-0000-000000000002",
    "idempotency_key": "hold-1"
  }'
```

Resposta:

```json
{
  "hold_id": "uuid",
  "status": "active"
}
```

---

### Consumir um hold

```bash
curl -X POST "http://localhost:8000/holds/{hold_id}/consume" \
  -H "Content-Type: application/json" \
  -d '{
    "transaction_id": "uuid",
    "idempotency_key": "consume-hold-1"
  }'
```

O `transaction_id` deve apontar para uma transacao `pending`, balanceada, com
um debito da conta do hold exatamente no valor reservado. O ledger posta essa
transacao de forma atomica com o consumo do hold: o debito do hold usa
`reserved_balance`, e os demais debitos usam saldo disponivel normal.

Resposta:

```json
{
  "hold_id": "uuid",
  "status": "consumed"
}
```

---

### Liberar um hold

```bash
curl -X POST "http://localhost:8000/holds/{hold_id}/release"
```

Resposta:

```json
{
  "hold_id": "uuid",
  "status": "released"
}
```

---

### Expirar holds vencidos

Para expirar todos os holds `active` com `expires_at` vencido:

```bash
curl -X POST "http://localhost:8000/holds/expire-due"
```

Resposta:

```json
{
  "expired_count": 3
}
```

Para expirar um hold especifico:

```bash
curl -X POST "http://localhost:8000/holds/{hold_id}/expire"
```

Resposta:

```json
{
  "hold_id": "uuid",
  "status": "expired"
}
```

---

### Criar uma transacao

```bash
curl -X POST "http://localhost:8000/transactions" \
  -H "Content-Type: application/json" \
  -d '{
    "reference_type": "payment_order",
    "reference_id": "00000000-0000-0000-0000-000000000003",
    "idempotency_key": "txn-1",
    "description": "Pagamento de jogo",
    "entries": [
      {
        "account_id": "uuid-conta-usuario",
        "entry_type": "debit",
        "amount": 100
      },
      {
        "account_id": "uuid-conta-jogo",
        "entry_type": "credit",
        "amount": 100
      }
    ]
  }'
```

Resposta:

```json
{
  "transaction_id": "uuid",
  "status": "pending"
}
```

---

### Postar uma transacao

```bash
curl -X POST "http://localhost:8000/transactions/{transaction_id}/post"
```

---

### Reverter uma transacao

```bash
curl -X POST "http://localhost:8000/transactions/{transaction_id}/reverse"
```

---

## Fluxos comuns

### Pagamento direto com saldo

1. Criar uma transacao com debito na conta do usuario.
2. Criar um credito na conta de destino.
3. Postar a transacao.
4. Consultar o saldo final.

---

### Checkout com hold

1. Criar um hold para bloquear saldo.
2. Iniciar o pagamento externo em outro servico.
3. Se o pagamento confirmar, criar uma transacao pendente balanceada para a
   liquidacao.
4. Consumir o hold informando o `transaction_id`; o ledger posta a transacao.
5. Se o pagamento falhar ou expirar, liberar o hold.

---

### Estorno

1. Localizar a transacao original.
2. Chamar `/transactions/{transaction_id}/reverse`.
3. O ledger cria lancamentos inversos.
4. A transacao original fica com status `reversed`.

---

## Estados

### Transacao

```text
pending
posted
reversed
failed
cancelled
```

### Hold

```text
active
consumed
released
expired
cancelled
```

---

## Configuracao

O servico usa `pydantic-settings`.

Variavel principal:

```text
DATABASE_URL
RABBITMQ_URL
PAYMENT_EVENTS_QUEUE
HOLD_EXPIRATION_INTERVAL_SECONDS
HOLD_EXPIRATION_BATCH_SIZE
LEDGER_INTERNAL_API_TOKENS
LOG_LEVEL
SERVICE_NAME
```

Valor padrao:

```text
sqlite+aiosqlite:///database.db
amqp://user:password@localhost:5672/
payment.lightning.events
60
100
vazio
INFO
ledger
```

Exemplo de `.env`:

```env
DATABASE_URL=sqlite+aiosqlite:///database.db
RABBITMQ_URL=amqp://user:password@localhost:5672/
PAYMENT_EVENTS_QUEUE=payment.lightning.events
HOLD_EXPIRATION_INTERVAL_SECONDS=60
HOLD_EXPIRATION_BATCH_SIZE=100
LEDGER_INTERNAL_API_TOKENS=pls:change-me-ledger-token:read,write
LOG_LEVEL=INFO
SERVICE_NAME=ledger
```

---

## Seguranca Interna

O ledger pode exigir autenticacao servico-para-servico por token interno.
Quando `LEDGER_INTERNAL_API_TOKENS` esta vazio, a autenticacao fica desligada
para facilitar desenvolvimento local e testes. Quando configurado, todas as
rotas financeiras exigem token.

Formato:

```text
service_name:token:scope1,scope2;other_service:other_token:admin
```

Escopos:

* `read`: chamadas `GET`, `HEAD` e `OPTIONS`
* `write`: chamadas que alteram estado
* `admin`: libera leitura e escrita

Headers aceitos:

```http
X-Internal-Service-Token: change-me-ledger-token
Authorization: Bearer change-me-ledger-token
```

Rotas publicas para operacao:

* `GET /`
* `GET /metrics`

---

## Observabilidade

O ledger expoe metricas Prometheus em:

```text
GET /metrics
```

Metricas principais:

```text
ledger_http_requests_total
ledger_http_request_duration_seconds
ledger_errors_total
ledger_holds_expired_total
ledger_hold_expiration_batches_total
ledger_transactions_posted_total
ledger_transactions_reversed_total
ledger_payment_events_total
```

No Compose do projeto, o Prometheus usa
[`../prometheus/prometheus.yml`](../prometheus/prometheus.yml) e coleta o
ledger em `ledger:8000/metrics`.

---

## Logs Estruturados

Os logs do ledger sao emitidos em JSON para facilitar coleta por Docker,
Prometheus/Loki, ELK ou outro agregador.

Eventos financeiros logados:

* `account_created`
* `hold_created`
* `hold_consumed`
* `hold_released`
* `hold_expired`
* `holds_expired_batch`
* `transaction_created`
* `transaction_posted`
* `transaction_reversed`

Exemplo:

```json
{"level":"INFO","service":"ledger","event":"hold_consumed","hold_id":"uuid","account_id":"uuid","amount":40}
```

---

## Rodando localmente

Instale as dependencias:

```bash
poetry install
```

Suba a API em modo desenvolvimento:

```bash
poetry run task run
```

Ou diretamente:

```bash
poetry run fastapi dev ledger/main.py
```

Health check simples:

```bash
curl http://localhost:8000/
```

Resposta:

```json
{
  "message": "Ledger service"
}
```

---

## Expiracao operacional de holds

Holds com `expires_at` vencido nao devem ficar segurando
`reserved_balance`. O ledger oferece duas formas de expirar essas reservas:

* `POST /holds/expire-due`, para chamada manual ou por scheduler externo
* worker CLI, para rodar continuamente junto da stack operacional

Rodar uma passada unica:

```bash
poetry run task expire_holds_once
```

Rodar continuamente:

```bash
poetry run task expire_holds
```

Configurar intervalo e tamanho do lote:

```bash
poetry run python -m ledger.hold_expiration_worker --interval 30 --batch-size 500
```

O worker usa `SELECT ... FOR UPDATE SKIP LOCKED` ao buscar holds vencidos.
Isso permite rodar mais de uma instancia sem que elas tentem processar o mesmo
hold ao mesmo tempo. Ainda assim, em producao, prefira uma instancia dedicada
ou um scheduler central para facilitar observabilidade.

---

## Eventos de pagamento Lightning

O ledger pode consumir eventos publicados pelo PLS na fila
`payment.lightning.events`. O evento aplicado hoje e:

```text
payment.invoice.paid
```

Contrato ponta a ponta:

* [`../docs/payment-lightning-flow.md`](../docs/payment-lightning-flow.md)

Contrato esperado:

```json
{
  "meta": {
    "type": "payment.invoice.paid",
    "event_id": "uuid",
    "correlation_id": "uuid"
  },
  "data": {
    "user_id": "uuid-do-usuario",
    "invoice_id": "id-da-invoice-no-lnbits",
    "payment_hash": "hash",
    "amount_msat": 100000,
    "paid_at": "2026-04-18T15:00:00+00:00"
  }
}
```

Unidade contabil: `msat`.

Lancamento criado:

```text
external_credit conta do usuario amount_msat origin=lightning
```

A conta do usuario e localizada por `accounts.owner_id == data.user_id` e
`account_type == user`. O Ledger guarda a origem e as referencias externas,
mas nao controla a wallet ou a liquidez Lightning do PLS.

Idempotencia:

```text
payment.invoice.paid:{invoice_id}
```

O consumidor roda no entrypoint quando
`PAYMENT_EVENT_CONSUMER_ENABLED=true`.

O modulo responsavel e `ledger.payment_consumer`, que consome
`PAYMENT_EVENTS_QUEUE` via FastStream/RabbitMQ e delega a aplicacao contabil
para `ledger.payment_events`.

O mesmo consumidor processa `payment.sent` quando o evento possui
`payment_reference`: ele localiza o hold de retirada, registra um
`external_debit` e consome a reserva do usuario.

---

## Reconciliacao financeira

O ledger possui uma rotina de reconciliacao para validar invariantes internas.

Rodar:

```bash
poetry run task reconcile
```

A reconciliacao verifica:

* `reserved_balance` contra a soma dos holds `active`
* contas com saldo ou reserva negativa
* contas com reserva maior que saldo
* transacoes `posted`/`reversed` com debitos e creditos balanceados
* transacoes postadas sem `posted_at`
* holds encerrados sem `consumed_at` ou `released_at`

Se encontrar divergencia, o comando retorna codigo de erro e lista os recursos
afetados.

---

## Testes

Rodar testes:

```bash
poetry run task test
```

Ou:

```bash
poetry run pytest -s -x --cov=ledger -vv
```

Os testes usam Testcontainers com PostgreSQL. Para a suite completa funcionar,
o Docker precisa estar rodando e acessivel para o usuario atual.

---

## Estrutura

```text
ledger/
  ledger/
    main.py
    audit.py
    balances.py
    hold_expiration.py
    hold_expiration_worker.py
    idempotency.py
    payload_hash.py
    payment_consumer.py
    payment_events.py
    reconcile.py
    schemas.py
    settings.py
    models/
      database.py
      enums.py
      tables.py
      description.md
    routes/
      accounts.py
      holds.py
      transactions.py
      description.md
  tests/
    conftest.py
    test_accounts.py
    test_db.py
    test_holds.py
    test_payment_events.py
    test_reconcile.py
    test_transactions.py
  pyproject.toml
  README.md
```

---

## Regras de seguranca financeira

* Nunca consumir ou liberar hold que nao esteja `active`.
* Nunca postar transacao que nao esteja `pending`.
* Nunca reverter transacao que nao esteja `posted`.
* Nunca aceitar transacao sem lancamentos.
* Nunca aceitar transacao desbalanceada.
* Nunca permitir debito maior que o saldo disponivel.
* Repeticoes com a mesma `idempotency_key` devem ser seguras.
* Reutilizar `idempotency_key` com payload diferente deve retornar conflito.
* Mutacoes de saldo devem passar por helpers de dominio em `balances.py`.
* Constraints no banco devem impedir saldo negativo, reserva maior que saldo, valores zerados e enums invalidos.
* Eventos financeiros relevantes devem ser persistidos em `ledger_audit_events`.
* A reconciliacao operacional deve passar sem divergencias.

---

## Resumo

O `ledger-service` e o nucleo financeiro do Micro Lotto.

Ele existe para garantir que o dinheiro do sistema seja:

* consistente
* rastreavel
* protegido contra duplicacao
* protegido contra double-spend
* facil de auditar

Se houver duvida sobre saldo, o ledger e a fonte da verdade.
