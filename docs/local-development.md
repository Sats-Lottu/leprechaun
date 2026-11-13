# Ambiente local com pagamentos simulados

> **NÃO USE O BOOTSTRAP EM PRODUÇÃO.** A pasta `local-bootstrap/` cria
> identidades de demonstração e saldos fictícios, com credenciais públicas.
> Execute somente em ambiente local isolado, sem fundos reais.


Este ambiente usa Keycloak com LNURL-auth, Nostr Auth e o tema Sats Lottu.
Os pagamentos usam LNbits FakeWallet.
Requer Docker Compose e Python 3.11+ para o comando de demonstracao.

Mantenha o checkout do tema em `../keycloak-sats-lottu-theme`, ao lado deste
repositório. Para outro local, configure `KEYCLOAK_THEME_DIR` no arquivo de
ambiente usado pelo Compose. O build compila o tema e verifica o SHA-256 dos
JARs publicados das duas extensões antes de instalá-los.

## Iniciar

Na raiz do repositorio:

```sh
docker compose --env-file .env.example -f compose.yaml -f compose.local.yaml up -d --build --remove-orphans
```

O projeto Compose `leprechaun-local` usa volumes separados. A inicializacao cria
os bancos, aplica migracoes, configura a wallet LNbits e cria as contas locais.
O Keycloak importa o realm `leprechaun` com os provedores `lnurl-auth` e `nostr`,
o usuário de demonstração e o cliente público `leprechaun-hub`. O cliente usa
Authorization Code com PKCE S256.
Reexecutar o comando preserva os saldos existentes. O script de bancos somente
e executado pelo PostgreSQL quando o volume esta vazio.

- Hub: http://localhost:8000
- Ledger API: http://localhost:8001
- Ledger OpenAPI: http://localhost:8001/docs
- Keycloak: http://localhost:8080
- LNbits: http://localhost:5000
- Prometheus: http://localhost:9090

Se as portas padrão estiverem ocupadas, ajuste `HUB_PORT`, `LEDGER_PORT` e
`KEYCLOAK_PORT` no ambiente do Compose. Por exemplo, no PowerShell:

```powershell
$env:HUB_PORT = '18000'
$env:LEDGER_PORT = '18001'
$env:KEYCLOAK_PORT = '18080'
docker compose --env-file .env.example -f compose.yaml -f compose.local.yaml up -d --build --remove-orphans
```

Use a nova porta do Hub ao cadastrar a aplicação de demonstração. Execute
`local-demo.py` com o mesmo `HUB_PORT` no ambiente. Se o realm já existir,
atualize as URLs de callback do cliente `leprechaun-hub` no console do Keycloak.

Login de demonstração no Hub: `player` / `Local-player-123!`.
Administração local do Keycloak: `localadmin` / `Local-admin-123!`.
LNbits local: `localadmin` / `local-lnbits-password`.
Essas credenciais sao exclusivas do ambiente de desenvolvimento.

Abra `http://localhost:8000` para iniciar o login. O tema exibe as opções de
senha, carteira Lightning e Nostr. Para testar LNURL-auth com uma carteira em
outro dispositivo, use um Keycloak com hostname HTTPS acessível pela carteira.
O Nostr Auth aceita extensões de navegador NIP-07 e assinadores remotos NIP-46.
O usuário de demonstração tem o subject `00000000-0000-0000-0000-000000000300`.
O Hub concede seus papéis administrativos no próprio banco.

O issuer público é `http://localhost:8080/realms/leprechaun`. Dentro do Docker,
o Hub consulta o discovery em `http://keycloak:8080/realms/leprechaun`.
O backchannel dinâmico do Keycloak anuncia os endpoints internos de token,
UserInfo e JWKS para o Hub. A autorização no navegador usa o hostname público.

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
O volume `local_keycloakdata` preserva identidades, cadastro do cliente e chaves
de assinatura. O Keycloak pula a importação quando o realm já existe. Edite o
realm existente no console para aplicar ajustes sem recriar as identidades.

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
[Hostname e backchannel do Keycloak](https://www.keycloak.org/server/hostname) e
[LNbits FakeWallet](https://docs.lnbits.org/guide/wallets.html).
