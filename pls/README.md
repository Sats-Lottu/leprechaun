# PLS - Payment Lightning Service

O `pls` e o servico de integracao Lightning do Micro Lotto.

Ele deve permanecer pequeno: sua responsabilidade e traduzir comandos internos de pagamento para chamadas no LNbits e publicar de volta os eventos relevantes no barramento. Regras de negocio do produto, ledger, saldos de usuario e conciliacao nao devem viver aqui.

Contrato ponta a ponta com UI e ledger:

- [`../docs/payment-lightning-flow.md`](../docs/payment-lightning-flow.md)

## Responsabilidade

O servico hoje faz:

- consome comandos da fila `payment.lightning.commands`;
- integra com o LNbits por HTTP e WebSocket;
- publica eventos na fila `payment.lightning.events`.

O fluxo implementado no momento e a criacao de invoice:

1. recebe `payment.invoice.create`;
2. valida o envelope e o payload;
3. converte `amount_msat` para satoshis;
4. chama `POST /api/v1/payments` no LNbits com `out=false`;
5. publica `payment.invoice.created`;
6. opcionalmente responde via request/reply quando a mensagem AMQP tiver `reply_to`.

Os comandos `payment.invoice.pay` e `payment.lnurl.pay` tambem sao tratados e publicam `payment.sent` quando o LNbits aceita o pagamento.

## Arquitetura

```text
outros servicos
    |
    | payment.invoice.create
    v
RabbitMQ: payment.lightning.commands
    |
    v
PLS
    | HTTP
    v
LNbits
    |
    | payment.invoice.created
    v
RabbitMQ: payment.lightning.events
```

Tambem existe uma ponte WebSocket:

```text
LNbits WebSocket
    |
    v
LNbitsWSBridge
    |
    v
RabbitMQ: payment.lightning.events
```

No lifecycle do servico, essa ponte persiste o payload em `provider_events`, reconcilia com `lightning_invoices` e publica `payment.invoice.paid` quando encontra uma invoice salva e o LNbits informa pagamento com `status=success`.

Sem `event_handler`, o bridge ainda pode publicar o payload bruto validado como `WalletResponse`; isso fica como comportamento de fallback/teste.

## Modulos

- `pls/main.py`: composicao da aplicacao FastStream e ponto de entrada.
- `pls/commands.py`: subscriber RabbitMQ e handlers dos comandos de pagamento.
- `pls/outbox.py`: persistencia e publicacao dos eventos do outbox transacional.
- `pls/reconciliation.py`: reconciliacao por WebSocket e job periodico com LNbits.
- `pls/lifecycle.py`: inicializacao e encerramento do cliente LNbits, WebSocket e reconciliador.
- `pls/messaging.py`: broker RabbitMQ, publisher do WebSocket e eventos de falha.
- `pls/observability.py`: servidor operacional `/health` e `/metrics`.
- `pls/idempotency.py`: controle persistente de idempotencia de comandos.
- `pls/utils.py`: conversoes de tempo/valor, hash de payload e retry de chamadas ao provider.
- `pls/provider.py`: cliente HTTP do LNbits, autenticacao, resolucao de wallet e wrappers de API.
- `pls/lnbits_ws_bridge.py`: conexao WebSocket com LNbits e republicacao dos eventos de wallet.
- `pls/schemas.py`: envelopes internos, comandos/eventos de dominio e schemas externos do LNbits.
- `pls/settings.py`: configuracao por variaveis de ambiente.

`main.py` nao reexporta mais helpers internos de compatibilidade. Testes e outros modulos devem importar diretamente o modulo dono da funcionalidade, por exemplo `pls.commands`, `pls.outbox`, `pls.reconciliation`, `pls.idempotency` ou `pls.utils`.

## Filas

As filas seguem o padrao `payment.{network}.*`. O PLS e o servico da rede Lightning, entao usa `payment.lightning.*`.

| Variavel | Padrao | Uso |
| --- | --- | --- |
| `PAYMENT_COMMANDS_QUEUE` | `payment.lightning.commands` | Entrada de comandos de pagamento Lightning. |
| `PAYMENT_EVENTS_QUEUE` | `payment.lightning.events` | Saida de eventos de pagamento Lightning e eventos vindos do WebSocket do LNbits. |

## Envelope interno

Todas as mensagens de comando usam `Envelope`:

```json
{
  "meta": {
    "event_id": "3fc3f1c1-5d06-4d72-b923-b8d4fd615ae7",
    "type": "payment.invoice.create",
    "occurred_at": "2026-04-18T15:00:00+00:00",
    "producer": "ledger",
    "schema_version": 1,
    "correlation_id": "89d64e8f-c465-49e8-95cb-9b7f235ecabb",
    "causation_id": null,
    "idempotency_key": "invoice:user-123:deposit-456"
  },
  "data": {
    "user_id": "user-123",
    "amount_msat": 100000,
    "memo": "deposit",
    "expires_in_sec": 900
  }
}
```

Campos extras sao recusados nos schemas internos (`extra="forbid"`).

## Comando: `payment.invoice.create`

Payload:

| Campo | Tipo | Obrigatorio | Observacao |
| --- | --- | --- | --- |
| `user_id` | `string` | sim | Identificador interno do usuario dono da invoice. |
| `amount_msat` | `int` | sim | Valor em millisatoshis. Precisa ser maior que zero. |
| `memo` | `string` | nao | Padrao: `deposit`. |
| `expires_in_sec` | `int` | nao | Padrao: `900`. Precisa ser maior que zero. |

O LNbits recebe o valor em satoshis. A conversao atual usa divisao inteira:

```text
amount_sat = amount_msat // 1000
```

Se o resultado for `0`, o comando e ignorado.

## Evento: `payment.invoice.created`

Payload publicado:

```json
{
  "meta": {
    "type": "payment.invoice.created",
    "correlation_id": "89d64e8f-c465-49e8-95cb-9b7f235ecabb",
    "causation_id": "3fc3f1c1-5d06-4d72-b923-b8d4fd615ae7"
  },
  "data": {
    "user_id": "user-123",
    "invoice_id": "lnbits-checking-id",
    "payment_request": "lnbc...",
    "amount_msat": 100000,
    "expires_at": "2026-04-18T15:15:00+00:00"
  }
}
```

`invoice_id` e escolhido nesta ordem:

1. `checking_id` retornado pelo LNbits;
2. `payment_hash` retornado pelo LNbits;
3. id local no formato `inv_<random>`.

`payment_request` usa `payment_request` ou `bolt11`, dependendo do campo retornado pelo LNbits.

## Evento: `payment.invoice.paid`

Publicado quando o LNbits confirma pagamento de uma invoice `incoming` que o
PLS conhece.

Payload:

```json
{
  "meta": {
    "type": "payment.invoice.paid",
    "correlation_id": "89d64e8f-c465-49e8-95cb-9b7f235ecabb",
    "causation_id": "provider-event-id"
  },
  "data": {
    "user_id": "user-123",
    "invoice_id": "lnbits-checking-id",
    "payment_hash": "hash",
    "amount_msat": 100000,
    "paid_at": "2026-04-18T15:00:00+00:00"
  }
}
```

O Ledger consome esse evento e registra a entrada externa:

```text
external_credit conta do usuario amount_msat origin=lightning
```

O PLS nao conhece contas do Ledger nem controla saldos internos.

## Evento: `payment.sent`

Publicado quando o PLS executa `payment.invoice.pay` ou `payment.lnurl.pay`
com sucesso no LNbits.

Payload:

```json
{
  "user_id": "user-123",
  "payment_hash": "hash",
  "checking_id": "lnbits-checking-id",
  "amount_msat": 100000,
  "payment_reference": "uuid-opaco-ou-null",
  "paid_at": "2026-04-18T15:00:00+00:00"
}
```

`payment_reference` e apenas ecoada pelo PLS. Quando presente, permite ao
Ledger correlacionar o resultado com uma reserva criada antes da retirada.

O contrato contabil de saida ainda deve ser fechado antes de usar pagamentos
externos com saldo interno.

## Idempotencia

A idempotencia do fluxo principal e persistida em `operation_idempotencies`.

- `operation` + `idempotency_key` formam uma chave unica;
- `payload_hash` registra o payload associado a chave;
- operacoes concluidas com sucesso sao ignoradas quando chegam novamente;
- falhas ficam registradas para auditoria operacional.

## Outbox transacional

Eventos de dominio gerados depois de uma escrita no banco sao salvos em `outbox_events` na mesma transacao da mudanca persistente. Depois do commit, o publicador busca eventos `pending`, publica em `PAYMENT_EVENTS_QUEUE` e marca cada item como `published`.

Isso evita perder eventos se o processo cair entre atualizar o banco e publicar no RabbitMQ. A entrega ainda deve ser consumida como ao menos uma vez: consumidores precisam manter idempotencia propria, porque RabbitMQ e reconexoes podem produzir republicacao em cenarios de falha.

## Reconciliacao

Existem dois caminhos de reconciliacao com o LNbits:

- WebSocket: cada `WalletResponse` e persistido em `provider_events`; quando corresponde a uma `LightningInvoice` pendente e vem com `status=success`, a invoice vira `paid` e um `payment.invoice.paid` e enfileirado no outbox.
- Periodica: o lifecycle inicia um job que consulta `GET /api/v1/payments/{checking_id}` para invoices pendentes salvas e tambem publica `payment.invoice.paid` via outbox quando o LNbits confirma `paid=true`.

## Observabilidade

O servico emite logs operacionais com chaves estaveis para rastrear o fluxo ponta a ponta:

- comandos: `payment_command_received`, `payment_command_completed`, `payment_command_duplicate`, `payment_command_unknown`;
- falhas de comando: `payment_command_failed_published`;
- LNbits: `provider_create_invoice_failed`, `provider_pay_invoice_failed`, `provider_pay_lnurl_failed`;
- persistencia: `invoice_created_persisted`, `outgoing_payment_persisted`;
- outbox: `outbox_event_enqueued`, `outbox_event_published`, `outbox_publish_batch_started`, `outbox_publish_batch_completed`;
- reconciliacao: `provider_event_received`, `provider_event_ignored`, `reconciliation_batch_started`, `reconciliation_invoice_paid`, `reconciliation_batch_completed`.

Os logs incluem campos como `correlation_id`, `idempotency_key`, `invoice_id`, `provider_invoice_id`, `payment_hash`, `outbox_event_id` e contadores de lote. O servico evita logar `payment_request` completo para reduzir exposicao de dados sensiveis.

## Configuracao

O `pls` usa `pydantic-settings` e le um arquivo `.env` quando existir.

| Variavel | Padrao | Descricao |
| --- | --- | --- |
| `RABBITMQ_URL` | `amqp://user:password@localhost:5672/` | URL de conexao com RabbitMQ. |
| `PAYMENT_COMMANDS_QUEUE` | `payment.lightning.commands` | Fila de comandos da rede Lightning. |
| `PAYMENT_EVENTS_QUEUE` | `payment.lightning.events` | Fila de eventos da rede Lightning. |
| `PAYMENT_COMMANDS_DLQ` | `payment.lightning.commands.dlq` | Fila de comandos Lightning invalidos ou falhas finais. |
| `COMMAND_RETRY_ATTEMPTS` | `3` | Tentativas para chamadas transientes ao LNbits. |
| `COMMAND_RETRY_BACKOFF_SEC` | `0.1` | Backoff incremental entre tentativas transientes. |
| `OUTBOX_PUBLISH_BATCH_SIZE` | `100` | Quantidade maxima de eventos publicados por varredura do outbox. |
| `RECONCILIATION_INTERVAL_SECONDS` | `60.0` | Intervalo do job periodico de reconciliacao com LNbits. |
| `RECONCILIATION_BATCH_SIZE` | `100` | Quantidade maxima de invoices pendentes verificadas por ciclo. |
| `SERVICE_NAME` | `payment-lightning-service` | Nome usado em logs e envelopes. |
| `SCHEMA_VERSION` | `1` | Versao do schema interno. |
| `LOG_LEVEL` | `INFO` | Nivel de log. |
| `OBSERVABILITY_ENABLED` | `True` | Habilita endpoint operacional HTTP. |
| `OBSERVABILITY_HOST` | `0.0.0.0` | Host do endpoint operacional. |
| `OBSERVABILITY_PORT` | `8001` | Porta do endpoint operacional. |
| `DATABASE_URL` | `sqlite+aiosqlite:///database.db` | Banco usado para invoices, eventos do provider, idempotencia e outbox. |
| `LNBITS_URL` | `http://localhost:5000` | URL base do LNbits. |
| `LNBITS_USERNAME` | `empty` | Usuario usado para login no LNbits quando chaves diretas nao forem fornecidas. |
| `LNBITS_PASSWORD` | `empty` | Senha usada para login no LNbits. |
| `LNBITS_INVOICE_READ_KEY` | `None` | Chave de leitura/invoice para modo sem login. |
| `LNBITS_ADMIN_KEY` | `None` | Chave admin para modo sem login. |
| `LNBITS_INVOICE_TTL` | `3600` | TTL default configurado no cliente. |

Se `LNBITS_INVOICE_READ_KEY` e `LNBITS_ADMIN_KEY` forem definidos, o cliente usa chaves diretas e nao faz login com usuario/senha.

## Autenticacao no LNbits

O cliente suporta dois modos:

- chaves diretas: `invoice_read_key` e `admin_key`;
- login: `username` e `password`, seguido de `GET /api/v1/auth` para pegar a primeira wallet da conta.

O lifecycle do FastStream inicializa o cliente, resolve a wallet e injeta `lnbits_wallet` no contexto global.

## WebSocket do LNbits

A URL e montada assim:

```text
<ws-ou-wss>://<host>/api/v1/ws/<invoice_read_key>
```

Regras:

- `https://` vira `wss://`;
- `http://` vira `ws://`;
- sem scheme usa `wss://`.

O bridge reconecta usando `tenacity` com backoff exponencial com jitter. No lifecycle do servico, cada mensagem valida como `WalletResponse` e enviada para o reconciliador, que persiste `ProviderEvent`, atualiza `LightningInvoice` quando encontra correspondencia e publica `payment.invoice.paid`.

Sem reconciliador/event handler, o bridge ainda publica o payload bruto em `PAYMENT_EVENTS_QUEUE`, que e util para testes e fallback.

## Executando localmente

Instale as dependencias dentro de `pls`:

```bash
cd pls
poetry install
```

Suba as dependencias externas necessarias, pelo menos RabbitMQ e LNbits, e configure o `.env`.

Depois rode:

```bash
poetry run task run
```

ou:

```bash
poetry run python pls/main.py
```

## Testes

```bash
cd pls
poetry run task test
```

Os testes usam `testcontainers` para Postgres e LNbits, alem de mocks HTTP para cobrir os wrappers do provider sem rede externa.

## Endpoints operacionais

Quando `OBSERVABILITY_ENABLED=true`, o PLS sobe um servidor HTTP separado:

```text
GET /health  -> ok
GET /metrics -> metricas em formato Prometheus text
```

O Compose expoe esse servidor internamente em `pls:8001` e o Prometheus coleta
`/metrics`.

Metrica exposta:

```text
pls_events_total
```

Labels principais:

- `event`: nome operacional interno;
- `command_type`: tipo do comando quando aplicavel;
- `event_type`: tipo do evento publicado quando aplicavel;
- `result`: resultado agregado quando aplicavel.

## Estado atual e proximos passos

O servico ja tem a base de integracao com LNbits, persistencia de invoices, idempotencia, outbox e reconciliacao por WebSocket/job periodico. Pontos importantes antes de tratar como producao:

- endurecer o setup do LNbits no compose para primeiro uso ou exigir chaves diretas;
- ampliar testes de contrato de mensageria com RabbitMQ real.
