# Ambiente local com pagamentos simulados

> **NÃO USE O BOOTSTRAP EM PRODUÇÃO.** A pasta `local-bootstrap/` cria
> identidades de demonstração e saldos fictícios, com credenciais públicas.
> Execute somente em ambiente local isolado, sem fundos reais.


Este ambiente usa SatOIDC e LNbits FakeWallet. Os saldos sao ficticios.
Requer Docker Compose e Python 3.11+ para o comando de demonstracao.

O codigo do SatOIDC deve estar em `../satoidc`, ao lado deste repositorio.
Para outro local, configure `SATOIDC_SOURCE_DIR` no arquivo de ambiente usado
pelo Compose. O build utiliza `satoidc/DockerFile` desse checkout.

## Iniciar

Na raiz do repositorio:

```sh
docker compose --env-file .env.example -f compose.yaml -f compose.local.yaml up -d --build --remove-orphans
```

O projeto Compose `leprechaun-local` usa volumes separados. A inicializacao cria
os bancos, aplica migracoes, configura a wallet LNbits e cria as contas locais.
O bootstrap do SatOIDC cria o administrador, o usuario de demonstracao e o
cliente publico `leprechaun-hub`, com Authorization Code e PKCE S256.
Reexecutar o comando preserva os saldos existentes. O script de bancos somente
e executado pelo PostgreSQL quando o volume esta vazio.

- Hub: http://localhost:8000
- Ledger API: http://localhost:8001
- Ledger OpenAPI: http://localhost:8001/docs
- SatOIDC: http://localhost:8080
- LNbits: http://localhost:5000
- Prometheus: http://localhost:9090

Login no Hub via SatOIDC: `player` / `Local-player-123!`.
Administracao local do SatOIDC: `localadmin` / `Local-admin-123!`.
LNbits local: `localadmin` / `local-lnbits-password`.
Essas credenciais sao exclusivas do ambiente de desenvolvimento.

Use `http://localhost:8000` para abrir o Hub: o callback cadastrado no cliente
e exatamente `http://localhost:8000/auth/callback`. Na primeira entrada, autorize
o compartilhamento de identidade e perfil na tela de consentimento do SatOIDC.
O usuario de demonstracao tem subject `00000000-0000-0000-0000-000000000300`.
Os papeis administrativos do Hub continuam sendo concedidos no banco do Hub;
ser administrador no SatOIDC nao concede acesso administrativo ao Leprechaun.

O issuer publico e `http://localhost:8080`. Dentro do Docker, o Hub usa
`http://satoidc:8000` para discovery, token, UserInfo e JWKS. A configuracao
`OIDC_INTERNAL_BASE_URL` altera somente os endpoints de servidor; o issuer,
o endereco de autorizacao e a validacao da assinatura permanecem publicos.
Discovery com issuer diferente do configurado e rejeitado.
O override local habilita `AUTHLIB_INSECURE_TRANSPORT=1` somente no SatOIDC
para aceitar HTTP na rede isolada de desenvolvimento. Em producao, use HTTPS
e nao habilite essa opcao.

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

Entradas e saidas Lightning sao registradas no Ledger como operacoes de
fronteira externa. O saldo da wallet FakeWallet e a liquidez simulada pertencem
ao LNbits/PLS e nao sao representados por uma conta de reserva no Ledger.

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
O volume `local_satoidcdata` preserva identidades, cadastro do cliente e chaves
de assinatura do SatOIDC. O bootstrap preserva registros existentes, sem trocar
senhas ou recriar identidades a cada inicializacao.

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
Antes de usar fundos reais ainda e necessario concluir a recuperacao duravel de
timeout e falhas no saque, a recuperacao entre liquidacoes parciais e os testes
de concorrencia e redelivery. O saque reserva saldo antes de chamar o PLS e o
Ledger consome a reserva ao receber `payment.sent`, mas interrupcoes entre esses
passos ainda precisam de tratamento operacional.
As confirmacoes externas e a reconciliacao contabil precisam continuar sendo
as fontes de verificacao.

Referencias de configuracao:
[SatOIDC](http://localhost:8080/.well-known/openid-configuration) e
[LNbits FakeWallet](https://docs.lnbits.org/guide/wallets.html).
