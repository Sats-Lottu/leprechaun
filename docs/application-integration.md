# Aplicacoes conectadas

O servidor da aplicacao cria uma cobranca autenticada e encaminha o usuario ao
checkout do Leprechaun. Abrir o checkout nao reserva nem debita saldo: o usuario
precisa clicar em **Confirm payment**. O saldo disponivel e utilizado primeiro;
uma invoice Lightning cobre a diferenca quando necessario.

Enquanto o checkout autenticado estiver aberto, ele verifica o pagamento a cada
3 segundos. Apos a liquidacao (`settled`), redireciona automaticamente para a
`return_url` cadastrada na cobranca, preservando seus parametros. Cobrancas
pendentes, canceladas, expiradas ou com falha nao acionam esse retorno.

## Cadastro administrativo

Acesse `/admin/applications` com um usuario que tenha a funcao interna `admin`.
Cadastre nome, identificador unico, URL do site e UUID da conta de recebimento
existente no Ledger. A tela permite listar, ativar, desativar e trocar a chave
das aplicacoes, com historico das alteracoes administrativas.

A chave completa aparece somente ao cadastrar ou trocar a chave. Guarde-a no
servidor da aplicacao, nunca no JavaScript entregue ao navegador. O Hub armazena
apenas seu hash. A troca invalida a chave anterior imediatamente. A desativacao
bloqueia o acesso da aplicacao e o preparo/liquidacao de suas cobrancas pendentes;
nao estorna compras concluidas nem libera automaticamente reservas existentes.

O operador pode conceder acesso administrativo pelo console confiavel do Hub:

```sh
python -m hub.admin_roles OIDC_SUB_DO_ADMINISTRADOR
```

No ambiente local, use:

```sh
docker compose --env-file .env.example -f compose.yaml -f compose.local.yaml exec hub poetry run python -m hub.admin_roles 00000000-0000-0000-0000-000000000300
```

Saia e entre novamente no Hub para carregar a funcao no menu. Esse comando
destina-se ao operador com acesso ao servidor; nao ha cadastro publico de admins.

## Criar e acompanhar uma cobranca

Exemplo Python executado no servidor da aplicacao:

```python
import os
import httpx

client = httpx.Client(
    base_url='http://localhost:8000',
    headers={'Authorization': f"Bearer {os.environ['LEPRECHAUN_API_KEY']}"},
)
response = client.post('/api/checkout/sessions', json={
    'game_id': 'local-demo',  # identificador cadastrado
    'order_id': 'purchase-123',  # identificador unico do pedido
    'destination_account_id': '00000000-0000-0000-0000-000000000200',
    'amount_sats': 100,
    'description': 'Compra de ingresso',
    'return_url': 'http://localhost:8000/user/wallet',
    'cancel_url': 'http://localhost:8000/',
})
response.raise_for_status()
checkout = response.json()
print(checkout['checkout_url'])  # encaminhe o usuario para este endereco

status = client.get(f"/api/checkout/sessions/{checkout['session_id']}")
status.raise_for_status()
paid = status.json()['status'] == 'settled'
```

`game_id` e a conta de destino precisam corresponder ao cadastro. As URLs de
retorno e cancelamento devem ter o mesmo protocolo, host e porta do site
cadastrado. `user_id` e opcional; se informado, deve ser o subject OIDC do usuario
que confirmara a compra. O login iniciado pelo checkout retorna a mesma cobranca.

Reenviar o mesmo pedido retorna a mesma sessao (HTTP 200; a criacao retorna 201).
Reutilizar o pedido com valor, destino, descricao ou URLs diferentes retorna 409.
Cada aplicacao pode consultar e cancelar apenas suas proprias cobrancas:

- `GET /api/checkout/sessions/{session_id}`
- `GET /api/checkout/orders/{game_id}/{order_id}`
- `POST /api/checkout/sessions/{session_id}/cancel`

Todos exigem `Authorization: Bearer CHAVE`. Sem chave valida, a resposta e 401;
uma aplicacao desativada recebe 403. As operacoes `prepare` e `settle` exigem
sessao do usuario e origem do checkout; a chave da aplicacao nao autoriza debitos.

Entregue o produto somente depois de consultar `status == settled` no servidor.
A navegacao para a URL de retorno nao comprova pagamento. Nao ha webhook de
notificacao para aplicacoes nesta versao: consulte o estado do pedido.

O ambiente local usa pagamentos simulados. Os limites operacionais para fundos
reais continuam descritos em [desenvolvimento local](local-development.md).
