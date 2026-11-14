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

Quando o LNbits confirma que o saque do Hub foi pago, o PLS publica:

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

O Hub registra a retirada antes de reservar o saldo e reutiliza a
`payment_reference` após timeout. O PLS confere o valor da BOLT11, registra a
referência antes de chamar o LNbits e consulta o provedor para resolver
respostas pendentes ou perdidas. O Ledger consome a reserva ao receber
`payment.sent` e registra um `external_debit` com `origin=lightning`. O Hub
libera a reserva se o provedor confirmar que o saque falhou.

## Checkout

Após a confirmação do usuário, o Hub retoma a cobrança em segundo plano. O
Ledger verifica se a invoice da cobrança foi creditada na conta do comprador,
confere o valor e vincula esse crédito a uma única liquidação. A transferência,
o consumo da reserva e o vínculo com a invoice são gravados juntos. A cobrança
mista aguarda até cinco minutos após o vencimento para receber a confirmação de
um pagamento feito dentro do prazo. Um pagamento tardio fica no saldo do usuário.

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

A fila de eventos agora é durável. Para atualizar uma instalação que usa a
fila antiga, interrompa a publicação, aguarde o Ledger consumir as mensagens
pendentes e configure um novo nome em `PAYMENT_EVENTS_QUEUE` para Ledger e PLS
antes de reiniciar os serviços atualizados. RabbitMQ não permite mudar a
durabilidade de uma fila existente.

Antes de ligar o consumidor do ledger em ambiente real:

1. criar a conta do usuario com `owner_id` igual ao `user_id` enviado ao PLS;
2. configurar:

```env
PAYMENT_EVENT_CONSUMER_ENABLED=true
```

## Garantias e Limites

Garantias implementadas:

- idempotencia persistente no PLS para comandos;
- outbox transacional no PLS para eventos, com publicação periódica mesmo sem
  novos comandos ou confirmações;
- idempotencia no ledger para `payment.invoice.paid`;
- reconciliacao do PLS com LNbits via WebSocket e job periodico.

Limites atuais:

- RabbitMQ entrega eventos como ao menos uma vez;
- consumidores devem ser idempotentes;
- setup real do LNbits ainda precisa ser endurecido;
- o Ledger nao reconcilia liquidez Lightning ou on-chain; isso pertence ao PLS
  e ao sistema de pagamento externo.
