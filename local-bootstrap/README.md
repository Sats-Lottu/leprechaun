# Bootstrap local para testes

> **NÃO USE EM PRODUÇÃO.** Estes arquivos preparam identidades de demonstração,
> credenciais públicas e saldos fictícios para testar o Leprechaun.
> Execute somente em um ambiente local isolado, sem fundos reais.

| Arquivo | Finalidade |
| --- | --- |
| `init-databases.sql` | Cria os bancos dos serviços na primeira inicialização do PostgreSQL. Não insere saldo; também é utilizado pelo Compose base. |
| `keycloak.Dockerfile` | Instala os JARs verificados de LNURL-auth e Nostr Auth e compila o tema Sats Lottu. |
| `leprechaun-realm.json` | Importa o realm local com os dois provedores, o cliente OIDC do Hub e o usuário de demonstração. |
| `bootstrap-lnbits.py` | Prepara o LNbits FakeWallet local e grava a configuração gerada no volume local. |
| `bootstrap-ledger.py` | Cria contas de demonstração e a reserva com saldo fictício. |
| `start-pls-local.py` | Inicia o PLS com as credenciais geradas pelo bootstrap local. |
| `local-demo.py` | Cria cobranças e simula pagamentos para testar compras e saques. |

Mantenha o checkout do tema em `../keycloak-sats-lottu-theme` ou configure
`KEYCLOAK_THEME_DIR` no ambiente do Compose. Na raiz do repositório:

```sh
docker compose --env-file .env.example -f compose.yaml -f compose.local.yaml up -d --build --remove-orphans
```

Depois de cadastrar a aplicação de demonstração e configurar sua chave de API:

```sh
python local-bootstrap/local-demo.py checkout --sats 100
```

O [guia local](../docs/local-development.md) descreve autenticação, cadastro da
aplicação e o fluxo completo de pagamento simulado.

Os volumes locais preservam os dados existentes; reiniciar o bootstrap não
recarrega automaticamente a reserva. O SQL de inicialização só roda quando o
volume do PostgreSQL está vazio. Não reutilize essas credenciais em produção.
