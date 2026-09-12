# PLS - Modelo de Dados

O banco do PLS guarda somente estado de integracao com LNbits. Ele nao e
ledger, nao guarda saldo de usuario e nao cria lancamentos contabeis.

## Tabelas

### `lightning_invoices`

Representa invoices e pagamentos acompanhados pelo PLS.

Campos principais:

- `id`: identificador interno.
- `provider`: provider externo, atualmente `lnbits`.
- `direction`: `incoming` para deposito, `outgoing` para pagamento/saque.
- `status`: `pending`, `paid`, `expired`, `failed` ou `cancelled`.
- `user_id`: identificador interno do usuario.
- `amount_msat`: valor em millisatoshis.
- `memo`: descricao curta da invoice.
- `provider_invoice_id`: id do LNbits, normalmente `checking_id`.
- `payment_hash`: hash Lightning.
- `payment_request`: BOLT11.
- `idempotency_key`: chave do comando que criou a invoice/pagamento.
- `expires_at`: vencimento calculado localmente.
- `paid_at`: confirmacao de pagamento.
- `raw_response`: resposta bruta do LNbits para diagnostico.

Uso:

- `payment.invoice.create` cria invoice `incoming`;
- `payment.invoice.pay` e `payment.lnurl.pay` criam registro `outgoing`;
- reconciliacao marca invoices `incoming` como `paid`.

### `provider_events`

Registra eventos brutos recebidos do LNbits, principalmente via WebSocket.

Campos principais:

- `id`: identificador interno.
- `provider`: provider externo.
- `event_type`: tipo do evento do provider ou rotina interna.
- `status`: estado de processamento.
- `invoice_id`: link opcional com `lightning_invoices`.
- `provider_invoice_id`: id de invoice no provider.
- `payment_hash`: hash Lightning.
- `payload`: payload bruto validado.
- `error_detail`: motivo de falha ou ignorado.
- `processed_at`: momento em que o evento foi aplicado.

Eventos com `status=success` e invoice correspondente geram
`payment.invoice.paid`.

### `operation_idempotencies`

Guarda idempotencia persistente dos comandos recebidos.

Campos principais:

- `operation`: tipo do comando, como `payment.invoice.create`.
- `idempotency_key`: chave enviada pelo produtor.
- `resource_id`: recurso criado quando aplicavel.
- `status`: `started`, `succeeded` ou `failed`.
- `payload_hash`: hash do payload original.

Regra:

- mesma operacao + mesma chave + mesmo payload pode ser repetida;
- se ja concluiu com sucesso, o comando duplicado e ignorado;
- se a chave for reutilizada com payload diferente, o PLS registra warning.

### `outbox_events`

Implementa outbox transacional para eventos de dominio.

Campos principais:

- `id`: identificador interno.
- `event_type`: tipo do evento, como `payment.invoice.created`.
- `payload`: envelope serializado.
- `status`: `pending`, `published` ou `failed`.
- `attempts`: tentativas de publicacao.
- `error_detail`: ultima falha de publicacao.
- `published_at`: momento da publicacao.

Regra:

- mudanca no banco e evento sao gravados na mesma transacao;
- depois do commit, o publicador envia eventos pendentes ao RabbitMQ;
- consumidores ainda precisam ser idempotentes, pois a entrega e ao menos uma vez.

## Regras de Escopo

- O PLS persiste estado operacional de integracao.
- O PLS traduz comandos internos para chamadas LNbits.
- O PLS traduz confirmacoes LNbits para eventos internos.
- O PLS nao aplica saldo, hold ou lancamento contabil.
- O ledger e a fonte da verdade financeira.
