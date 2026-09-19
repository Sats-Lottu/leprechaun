# Ledger - Modelo de Dados

O ledger guarda a verdade financeira interna do Micro Lotto. Nenhum outro
servico deve alterar saldo diretamente.

O modelo usa:

- contas (`accounts`);
- transacoes (`ledger_transactions`);
- lancamentos (`ledger_entries`);
- reservas (`balance_holds`);
- idempotencia operacional (`operation_idempotencies`);
- auditoria persistida (`ledger_audit_events`).

## Conceitos

### Conta

Uma conta representa uma entidade financeira.

Tipos:

- `user`
- `game`
- `application`
- `platform`
- `escrow`
- `fee`

Campos de saldo:

```text
balance
reserved_balance
available_balance = balance - reserved_balance
```

### Transacao

Agrupa uma operacao financeira completa. Uma transacao so e valida quando seus
lancamentos sao balanceados:

```text
soma(debitos) == soma(creditos)
```

Estados:

- `pending`
- `posted`
- `reversed`
- `failed`
- `cancelled`

### Lancamento

Movimento individual de debito ou credito em uma conta.

Regras:

- `amount` sempre positivo;
- `entry_type=debit` reduz saldo;
- `entry_type=credit` aumenta saldo;
- cada lancamento referencia uma transacao.

### Hold

Reserva temporaria de saldo. O hold reduz o saldo disponivel, mas nao reduz
`balance` ate ser consumido.

Estados:

- `active`
- `consumed`
- `released`
- `expired`
- `cancelled`

## Tabelas

### `accounts`

Campos principais:

- `id`
- `account_type`
- `owner_id`
- `name`
- `balance`
- `reserved_balance`
- `is_active`

Constraints:

- `balance >= 0`
- `reserved_balance >= 0`
- `reserved_balance <= balance`
- `account_type` precisa ser enum valido.

### `ledger_transactions`

Campos principais:

- `id`
- `status`
- `kind` (`transfer`, `external_credit` ou `external_debit`)
- `external_origin`
- `reference_type`
- `reference_id`
- `idempotency_key`
- `idempotency_payload_hash`
- `description`
- `reversed_transaction_id`
- `posted_at`

Uso:

- transacoes criadas por API iniciam como `pending`;
- eventos externos, como `payment.invoice.paid`, podem criar transacoes
  diretamente como `posted`;
- reversoes criam uma nova transacao com lancamentos inversos.

### `ledger_entries`

Campos principais:

- `id`
- `transaction_id`
- `account_id`
- `entry_type`
- `amount`
- `description`
- `reference_type`
- `reference_id`

Transacoes `transfer` devem ter debitos e creditos equivalentes.
`external_credit` contem somente creditos e `external_debit` somente debitos;
essas modalidades representam a fronteira do saldo interno, nao o saldo do
sistema de pagamento externo.

### `balance_holds`

Campos principais:

- `id`
- `account_id`
- `amount`
- `status`
- `reason`
- `reference_type`
- `reference_id`
- `idempotency_key`
- `idempotency_payload_hash`
- `expires_at`
- `consumed_at`
- `released_at`

Efeitos:

- criar hold aumenta `reserved_balance`;
- consumir hold reduz `reserved_balance` e `balance`;
- liberar/expirar hold reduz somente `reserved_balance`.

### `operation_idempotencies`

Registra idempotencia de operacoes que mudam estado de recursos existentes.

Campos principais:

- `operation`
- `idempotency_key`
- `resource_id`
- `status`
- `payload_hash`

Reutilizar a mesma chave com payload diferente retorna conflito.

### `ledger_audit_events`

Auditoria persistida de eventos financeiros.

Campos principais:

- `event_type`
- `resource_type`
- `resource_id`
- `service_name`
- `idempotency_key`
- `event_metadata`

Eventos comuns:

- `account_created`
- `hold_created`
- `hold_consumed`
- `hold_released`
- `hold_expired`
- `transaction_created`
- `transaction_posted`
- `transaction_reversed`
- `payment_invoice_paid_applied`

## Evento `payment.invoice.paid`

Quando o ledger consome `payment.invoice.paid`, ele cria uma transacao `posted`
com:

```text
external_credit conta do usuario amount_msat origin=lightning
```

Regras:

- unidade contabil: `msat`;
- conta do usuario: `account_type=user` e `owner_id=data.user_id`;
- idempotencia: `payment.invoice.paid:{invoice_id}`.

O evento `payment.sent` associado a uma `payment_reference` consome o hold de
retirada e cria um `external_debit` na conta do usuario.

## Reconciliacao

O comando:

```bash
poetry run task reconcile
```

verifica:

- reserva da conta contra holds ativos;
- saldo e reserva nao negativos;
- reserva menor ou igual ao saldo;
- transferencias internas postadas/revertidas balanceadas;
- timestamps coerentes com estados fechados.
