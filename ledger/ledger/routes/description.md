# Ledger Service - Documentacao das Rotas

## Visao Geral

O pacote `ledger.routes` expoe a API HTTP do ledger-service.

As rotas sao responsaveis por:

* criar e consultar contas
* consultar saldo e extrato
* criar, postar e reverter transacoes
* criar, consultar, consumir, liberar e expirar holds

> Nenhum endpoint deve alterar saldo fora das regras do ledger.
> Toda mudanca financeira deve preservar consistencia entre conta, hold,
> transacao e lancamentos.

---

## Organizacao

### `accounts.py`

Rotas de contas.

Prefixo:

```text
/accounts
```

Responsabilidades:

* criar contas
* consultar saldo
* consultar extrato

---

### `transactions.py`

Rotas de transacoes contabeis.

Prefixo:

```text
/transactions
```

Responsabilidades:

* criar transacoes pendentes
* consultar transacoes
* postar transacoes
* reverter transacoes postadas

---

### `holds.py`

Rotas de reserva de saldo.

Prefixo:

```text
/holds
```

Responsabilidades:

* criar hold
* consultar hold
* listar holds de uma conta
* consumir hold
* liberar hold
* expirar hold

---

## Rotas de Contas

### `POST /accounts`

Cria uma nova conta.

Parametros:

* `account_type`: tipo da conta. Padrao: `user`
* `owner_id`: identificador externo do dono da conta
* `name`: nome amigavel da conta

Regra:

* contas do tipo `user` exigem `owner_id`

Resposta:

```json
{
  "account_id": "uuid",
  "account_type": "user",
  "owner_id": "uuid",
  "name": "Conta"
}
```

Erros:

* `400`: `owner_id` ausente para conta `user`

---

### `GET /accounts/{account_id}/balance`

Consulta o saldo de uma conta.

Resposta:

```json
{
  "balance": 100,
  "reserved_balance": 40,
  "available_balance": 60
}
```

Regra:

```text
available_balance = max(balance - reserved_balance, 0)
```

Erros:

* `404`: conta nao encontrada

---

### `GET /accounts/{account_id}/statement`

Lista os lancamentos contabeis da conta.

Resposta:

```json
{
  "entries": []
}
```

---

## Rotas de Transacoes

### `POST /transactions`

Cria uma transacao pendente.

Payload:

```json
{
  "kind": "transfer",
  "external_origin": "",
  "reference_type": "payment_order",
  "reference_id": "uuid",
  "idempotency_key": "txn-1",
  "description": "Pagamento",
  "entries": [
    {
      "account_id": "uuid",
      "entry_type": "debit",
      "amount": 100
    },
    {
      "account_id": "uuid",
      "entry_type": "credit",
      "amount": 100
    }
  ]
}
```

Regras:

* a transacao deve ter pelo menos um lancamento
* cada lancamento deve ter `amount` maior que zero
* `transfer` exige que a soma dos debitos seja igual a soma dos creditos
* `external_credit` aceita somente creditos e exige `external_origin`
* `external_debit` aceita somente debitos e exige `external_origin`
* todas as contas referenciadas devem existir
* `idempotency_key` reutilizada retorna a transacao existente

Resposta:

```json
{
  "transaction_id": "uuid",
  "status": "pending"
}
```

Erros:

* `400`: valor invalido ou transacao desbalanceada
* `404`: conta nao encontrada
* `409`: conflito de idempotencia

---

### `GET /transactions/{transaction_id}`

Consulta uma transacao.

Resposta:

```json
{
  "transaction_id": "uuid",
  "status": "pending",
  "kind": "transfer",
  "external_origin": "",
  "reference_type": "payment_order",
  "reference_id": "uuid",
  "description": "Pagamento",
  "reversed_transaction_id": null,
  "entries": []
}
```

Erros:

* `404`: transacao nao encontrada

---

### `POST /transactions/{transaction_id}/post`

Efetiva uma transacao pendente.

Efeito:

* lancamento `debit`: reduz `balance` da conta
* lancamento `credit`: aumenta `balance` da conta
* transacao muda para `posted`
* `posted_at` recebe a data da efetivacao

Regras:

* somente transacoes `pending` podem ser postadas
* debitos usam saldo disponivel:

```text
available_balance = balance - reserved_balance
```

Erros:

* `400`: saldo disponivel insuficiente
* `404`: transacao ou conta nao encontrada
* `409`: transacao nao esta pendente

---

### `POST /transactions/{transaction_id}/reverse`

Reverte uma transacao postada.

Efeito:

* cria uma nova transacao `posted`
* cria lancamentos inversos
* marca a transacao original como `reversed`

Regras:

* somente transacoes `posted` podem ser revertidas
* uma transacao so pode ser revertida uma vez

Erros:

* `404`: transacao nao encontrada
* `409`: transacao nao postada ou ja revertida

---

## Rotas de Holds

### `POST /holds`

Cria uma reserva de saldo.

Payload:

```json
{
  "account_id": "uuid",
  "amount": 40,
  "reason": "checkout",
  "reference_type": "payment_order",
  "reference_id": "uuid",
  "idempotency_key": "hold-1",
  "expires_at": "2026-04-17T18:00:00Z"
}
```

Efeito:

* cria um registro em `balance_holds`
* aumenta `reserved_balance`
* nao altera `balance`

Regras:

* `amount` deve ser maior que zero
* conta deve existir
* conta deve ter saldo disponivel suficiente
* `idempotency_key` reutilizada retorna o hold existente

Resposta:

```json
{
  "hold_id": "uuid",
  "status": "active"
}
```

Erros:

* `400`: valor invalido ou saldo disponivel insuficiente
* `404`: conta nao encontrada
* `409`: conflito de idempotencia

---

### `GET /holds/{hold_id}`

Consulta um hold.

Resposta:

```json
{
  "hold_id": "uuid",
  "account_id": "uuid",
  "amount": 40,
  "status": "active",
  "reason": "checkout",
  "reference_type": "payment_order",
  "reference_id": "uuid",
  "expires_at": null,
  "consumed_at": null,
  "released_at": null
}
```

Erros:

* `404`: hold nao encontrado

---

### `GET /holds/account/{account_id}`

Lista os holds de uma conta.

Resposta:

```json
{
  "account_id": "uuid",
  "holds": []
}
```

Erros:

* `404`: conta nao encontrada

---

### `POST /holds/expire-due`

Expira todos os holds ativos com `expires_at` menor ou igual ao horario atual.

Uso:

* scheduler operacional
* worker de manutencao
* rotina manual de recuperacao

Efeito:

* busca holds `active` vencidos
* usa lock transacional com `SKIP LOCKED`
* reduz `reserved_balance`
* nao altera `balance`
* muda cada hold vencido para `expired`
* define `released_at`

Resposta:

```json
{
  "expired_count": 3
}
```

Erros:

* `404`: conta associada ao hold nao encontrada

---

### `POST /holds/{hold_id}/expire`

Expira um hold ativo especifico.

Uso:

* cancelamento operacional
* timeout de checkout
* compensacao de processo externo

Payload opcional:

```json
{
  "idempotency_key": "expire-hold-1"
}
```

Efeito:

* reduz `reserved_balance`
* nao altera `balance`
* muda o hold para `expired`
* define `released_at`
* registra idempotencia da operacao quando a chave for enviada

Regras:

* somente holds `active` podem ser expirados
* `idempotency_key` reutilizada retorna o resultado da primeira chamada

Resposta:

```json
{
  "hold_id": "uuid",
  "status": "expired"
}
```

Erros:

* `404`: hold ou conta nao encontrada
* `409`: hold nao esta ativo

---

### `POST /holds/{hold_id}/consume`

Converte um hold ativo em debito real postando uma transacao contabil pendente.

Uso:

* pagamento externo confirmado
* checkout aprovado

Efeito:

* reduz `reserved_balance`
* reduz `balance`
* posta a transacao informada em `transaction_id`
* aplica os creditos e debitos da transacao
* muda o hold para `consumed`
* define `consumed_at`

Regras:

* somente holds `active` podem ser consumidos
* holds vencidos sao expirados e retornam erro `hold_expired`
* `transaction_id` e obrigatorio
* a transacao deve estar `pending`
* a transacao deve debitar a conta do hold exatamente pelo valor reservado
* debitos adicionais da transacao usam saldo disponivel normal

Payload:

```json
{
  "transaction_id": "uuid",
  "idempotency_key": "consume-hold-1"
}
```

Resposta:

```json
{
  "hold_id": "uuid",
  "status": "consumed"
}
```

Erros:

* `404`: hold ou conta nao encontrada
* `400`: `transaction_id` ausente
* `400`: transacao nao debita a conta do hold pelo valor reservado
* `409`: hold nao esta ativo
* `409`: hold expirado
* `409`: transacao nao esta pendente

---

### `POST /holds/{hold_id}/release`

Libera um hold ativo.

Uso:

* pagamento cancelado
* checkout expirado
* falha externa

Efeito:

* reduz `reserved_balance`
* nao altera `balance`
* muda o hold para `released`
* define `released_at`

Regras:

* somente holds `active` podem ser liberados
* holds vencidos sao expirados e retornam erro `hold_expired`

Resposta:

```json
{
  "hold_id": "uuid",
  "status": "released"
}
```

Erros:

* `404`: hold ou conta nao encontrada
* `409`: hold nao esta ativo
* `409`: hold expirado

---

## Rotina Operacional de Expiracao

O endpoint `POST /holds/expire-due` existe para integracao com scheduler
externo, mas o projeto tambem fornece um worker CLI.

Uma passada unica:

```bash
poetry run task expire_holds_once
```

Worker continuo:

```bash
poetry run task expire_holds
```

Com parametros explicitos:

```bash
poetry run python -m ledger.hold_expiration_worker --interval 30 --batch-size 500
```

Configuracao:

* `HOLD_EXPIRATION_INTERVAL_SECONDS`: segundos entre lotes
* `HOLD_EXPIRATION_BATCH_SIZE`: maximo de holds processados por lote

O processamento usa `FOR UPDATE SKIP LOCKED`, entao instancias concorrentes
podem dividir lotes sem processar o mesmo hold ao mesmo tempo.

---

## Fluxos Tipicos

### Pagamento com saldo direto

1. `POST /transactions`
2. `POST /transactions/{transaction_id}/post`
3. consultar saldo em `GET /accounts/{account_id}/balance`

---

### Checkout com hold

1. `POST /holds`
2. iniciar pagamento externo
3. se o pagamento confirmar: criar transacao pendente balanceada
4. consumir o hold com `POST /holds/{hold_id}/consume` informando `transaction_id`
5. se o pagamento falhar: `POST /holds/{hold_id}/release`
6. se o hold vencer: worker ou `POST /holds/expire-due`

---

### Estorno

1. localizar transacao original
2. `POST /transactions/{transaction_id}/reverse`
3. consultar transacao e saldo

---

## Regras Importantes

* `balance` representa o saldo total.
* `reserved_balance` representa a soma bloqueada por holds ativos.
* Saldo disponivel e calculado como `balance - reserved_balance`.
* Holds ativos protegem saldo contra uso concorrente.
* Consumo de hold reduz saldo reservado e saldo total ao postar a transacao contabil associada.
* Liberacao de hold reduz somente saldo reservado.
* Transacoes postadas devem preservar debitos e creditos balanceados.
* Rotas com `idempotency_key` devem ser seguras contra repeticao.
* Rotas financeiras podem exigir token interno quando
  `LEDGER_INTERNAL_API_TOKENS` estiver configurado.

---

## Operacao HTTP

### `GET /metrics`

Expoe metricas Prometheus do ledger.

Metricas principais:

* `ledger_http_requests_total`
* `ledger_http_request_duration_seconds`
* `ledger_errors_total`
* `ledger_holds_expired_total`
* `ledger_hold_expiration_batches_total`
* `ledger_transactions_posted_total`
* `ledger_transactions_reversed_total`
* `ledger_payment_events_total`

Eventos de pagamento Lightning nao entram pela API HTTP. Eles sao processados
pelo worker `ledger.payment_consumer`, documentado em
[`../../README.md`](../../README.md).

### Autenticacao Interna

Quando `LEDGER_INTERNAL_API_TOKENS` esta configurado, as rotas financeiras
exigem token interno.

Formato:

```text
service_name:token:scope1,scope2
```

Escopos:

* `read`: leitura
* `write`: mudanca de estado
* `admin`: leitura e escrita

Headers aceitos:

```http
X-Internal-Service-Token: token
Authorization: Bearer token
```

---

## Resumo

As rotas formam a camada HTTP do ledger-service.

Elas traduzem chamadas externas em operacoes financeiras internas,
mantendo:

* consistencia de saldo
* rastreabilidade por referencias externas
* idempotencia
* separacao entre reserva, consumo e transacao contabil
