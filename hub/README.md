# Hub

O Hub é a interface e o coordenador de pagamentos do Leprechaun. Aplicações
conectadas criam cobranças; os usuários confirmam no checkout; o Hub coordena
reservas e liquidação no Ledger e solicita invoices ao PLS via RabbitMQ.

## Interfaces

- `/user/wallet`: saldo, extrato e fluxos de depósito e saque.
- `/user/checkout?session_id=UUID`: confirmação e acompanhamento da cobrança.
- `/user/profile`: identidade do usuário e histórico.
- `/admin/applications`: cadastro de aplicações, ativação, desativação, troca de
  chave e histórico administrativo.
- `/admin/*`: demais páginas administrativas ainda incluem resumos básicos e
  espaços reservados. Não são um console financeiro completo.

O login utiliza OIDC. Os papéis administrativos são mantidos no banco do Hub,
separadamente dos papéis do provedor de identidade. O operador pode conceder
acesso com `python -m hub.admin_roles OIDC_SUB`; o usuário precisa entrar novamente
para atualizar o menu. As alterações de aplicações verificam o papel no banco.

## API de aplicações

| Método e rota | Autorização |
| --- | --- |
| `POST /api/checkout/sessions` | Chave da aplicação |
| `GET /api/checkout/sessions/{session_id}` | Chave da aplicação proprietária |
| `GET /api/checkout/orders/{game_id}/{order_id}` | Chave da aplicação proprietária |
| `POST /api/checkout/sessions/{session_id}/cancel` | Chave da aplicação proprietária |
| `POST /api/checkout/sessions/{session_id}/prepare` | Sessão do usuário e origem do checkout |
| `POST /api/checkout/sessions/{session_id}/settle` | Sessão do usuário proprietário e origem do checkout |

A chave é enviada em `Authorization: Bearer CHAVE` e armazenada como hash. O
identificador da aplicação, a conta de destino e a origem das URLs precisam
corresponder ao cadastro. Cada aplicação acessa somente as próprias cobranças.

A criação é idempotente por aplicação/pedido: repetir os mesmos dados retorna a
mesma sessão; reutilizar o pedido com dados conflitantes retorna HTTP 409. Veja
[integração de aplicações](../docs/application-integration.md) para os contratos
e exemplos completos. A API HTTP também possui documentação em `/docs`.

## Confirmação e liquidação

Abrir uma cobrança nova não reserva nem debita saldo. Depois de confirmar, o Hub
reserva o saldo disponível e solicita uma invoice para a diferença, se necessário.
A liquidação debita o usuário e credita a conta de recebimento da aplicação.

Enquanto a página autenticada permanece aberta, o checkout verifica a liquidação
a cada 3 segundos. Quando chega a `settled`, retorna à `return_url`. A aplicação
deve consultar esse estado no servidor antes de liberar a compra. Ainda não existe
webhook de notificação para aplicações.

As cobranças usam os estados `created`, `reserved`, `awaiting_payment`, `settled`,
`canceled`, `expired` e `failed`. O Ledger é a fonte de verdade de saldos e
lançamentos; a confirmação externa é obtida do serviço de pagamentos.

## Desenvolvimento

Use a [stack local](../docs/local-development.md) para executar com OIDC,
PostgreSQL, RabbitMQ e LNbits simulados. Para trabalhar no serviço:

```sh
poetry install
poetry run python -m ruff check .
poetry run python -m pytest -q
```

Os testes com PostgreSQL usam Testcontainers. As configurações são declaradas em
[settings.py](hub/settings.py) e no [.env.example](../.env.example) da raiz. A
configuração do Hub fora do Compose utiliza `DATABASE_URL`; o Compose recebe
`HUB_DATABASE_URL` e faz esse mapeamento.

O entrypoint aplica `alembic upgrade head` antes de iniciar o servidor. As
migrações estão em `migrations/versions`. Mantenha alterações de modelo e migração
na mesma contribuição.

## Limites conhecidos

Os saques ainda não possuem reserva prévia e recuperação durável. Também faltam
validações de concorrência e recuperação de liquidações parciais. Use fundos
simulados. Consulte [segurança](../SECURITY.md) e
[operação self-hosted](../docs/self-hosting.md) antes de planejar uma implantação.
