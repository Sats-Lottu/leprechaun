# Fluxo Lightning de Pagamento

Este documento define o contrato entre UI, PLS, RabbitMQ, LNbits e Ledger para
pagamentos Lightning.

## Filas

```text
payment.lightning.commands
payment.lightning.events
payment.lightning.commands.dlq
```

Regra geral:

```text
payment.{network}.*
```

O PLS e o servico da rede `lightning`.

## Envelope

Todas as mensagens internas usam:

```json
{
  "meta": {
    "event_id": "uuid",
    "type": "payment.invoice.create",
    "occurred_at": "2026-04-18T15:00:00+00:00",
    "producer": "hub",
    "schema_version": 1,
    "correlation_id": "uuid",
    "causation_id": null,
    "idempotency_key": "invoice:uuid"
  },
  "data": {}
}
```

Campos extras nos schemas internos do PLS sao recusados.

## Deposito Lightning

### 1. UI pede invoice

Fila:

```text
payment.lightning.commands
```

Tipo:

```text
payment.invoice.create
```

Payload:

```json
{
  "user_id": "uuid-do-usuario",
  "amount_msat": 100000,
  "memo": "deposit",
  "expires_in_sec": 900
}
```

### 2. PLS cria invoice no LNbits

O PLS converte `amount_msat` para satoshis usando divisao inteira:

```text
amount_sat = amount_msat // 1000
```

Se o resultado for zero, o comando falha e vai para DLQ como
`payment.command.failed`.

### 3. PLS publica invoice criada

Fila:

```text
payment.lightning.events
```

Tipo:

```text
payment.invoice.created
```

Payload:

```json
{
  "user_id": "uuid-do-usuario",
  "invoice_id": "lnbits-checking-id",
  "payment_request": "lnbc...",
  "amount_msat": 100000,
  "expires_at": "2026-04-18T15:15:00+00:00"
}
```

### 4. LNbits confirma pagamento

O PLS recebe o evento via WebSocket ou pelo job periodico de reconciliacao.
Quando encontra uma invoice pendente correspondente, publica:

```text
payment.invoice.paid
```

Payload:

```json
{
  "user_id": "uuid-do-usuario",
  "invoice_id": "lnbits-checking-id",
  "payment_hash": "hash",
  "amount_msat": 100000,
  "paid_at": "2026-04-18T15:00:00+00:00"
}
```

### 5. Ledger aplica saldo

O consumidor de eventos do ledger processa `payment.invoice.paid`.

Unidade contabil:

```text
msat
```

Lancamento de fronteira externa:

```text
external_credit conta do usuario amount_msat origin=lightning
```

O Ledger registra a origem e a invoice, mas nao controla wallet, canais ou
liquidez do sistema Lightning. Esses detalhes permanecem no PLS e no provedor.

A conta do usuario e localizada por:

```text
accounts.account_type = user
accounts.owner_id = data.user_id
```

Idempotencia:

```text
payment.invoice.paid:{invoice_id}
```

## Pagamento de Invoice/LNURL

O PLS tambem aceita:

```text
payment.invoice.pay
payment.lnurl.pay
```

Quando o LNbits aceita o pagamento, o PLS publica:

```text
payment.sent
```

Payload:

```json
{
  "user_id": "uuid-do-usuario-ou-null",
  "payment_hash": "hash",
  "checking_id": "lnbits-checking-id",
  "amount_msat": 100000,
  "payment_reference": "uuid-da-retirada",
  "paid_at": "2026-04-18T15:00:00+00:00"
}
```

Para retiradas iniciadas pelo Hub, o Ledger localiza a reserva pela
`payment_reference`, consome o hold e registra um `external_debit` com
`origin=lightning`. O PLS apenas ecoa essa referencia opaca.

## Falhas

Comandos invalidos ou falhas finais publicam:

```text
payment.command.failed
```

Fila:

```text
payment.lightning.commands.dlq
```

Payload:

```json
{
  "reason": "invalid create_invoice payload",
  "command_type": "payment.invoice.create"
}
```

## Ativacao do consumidor

Antes de ligar o consumidor do ledger em ambiente real:

1. criar a conta do usuario com `owner_id` igual ao `user_id` enviado ao PLS;
2. configurar:

```env
PAYMENT_EVENT_CONSUMER_ENABLED=true
```

## Garantias e Limites

Garantias implementadas:

- idempotencia persistente no PLS para comandos;
- outbox transacional no PLS para eventos;
- idempotencia no ledger para `payment.invoice.paid`;
- reconciliacao do PLS com LNbits via WebSocket e job periodico.

Limites atuais:

- RabbitMQ entrega eventos como ao menos uma vez;
- consumidores devem ser idempotentes;
- setup real do LNbits ainda precisa ser endurecido;
- o Ledger nao reconcilia liquidez Lightning ou on-chain; isso pertence ao PLS
  e ao sistema de pagamento externo.
