import asyncio
from http import HTTPStatus

import httpx
import pytest

from pls import provider
from pls.provider import (
    LNbitsAuth,
    LNbitsClient,
    LNbitsConfig,
    LNbitsWallet,
    failed_from_response,
    normalize_base_url,
)
from pls.schemas import (
    Account,
    CreateInvoice,
    Failed,
    LNURLDecoded,
    Payment,
    PaymentDecoded,
    Token,
)


def test_normalize_base_url_removes_trailing_slash():
    assert (
        normalize_base_url("https://lnbits.example/")
        == "https://lnbits.example"
    )


def test_failed_from_response_uses_detail_from_json_body():
    resp = httpx.Response(
        HTTPStatus.BAD_REQUEST,
        json={"detail": "bad invoice", "status": "failed"},
    )

    failed = failed_from_response(resp, "fallback")

    assert failed == Failed(detail="bad invoice", status="failed")


def test_failed_from_response_handles_other_response_shapes():
    dict_resp = httpx.Response(HTTPStatus.BAD_REQUEST, json={"error": "boom"})
    list_resp = httpx.Response(HTTPStatus.BAD_REQUEST, json=["boom"])
    invalid_failed_resp = httpx.Response(
        HTTPStatus.BAD_REQUEST,
        json={"status": "failed"},
    )

    assert failed_from_response(dict_resp, "fallback") == Failed(
        detail='{"error": "boom"}',
        status="failed",
    )
    assert failed_from_response(list_resp, "fallback") == Failed(
        detail="['boom']",
        status="failed",
    )
    assert failed_from_response(invalid_failed_resp, "fallback") == Failed(
        detail='{"status":"failed"}',
        status="failed",
    )


@pytest.mark.asyncio
async def test_auth_login_and_get_account_info_use_lnbits_auth_api():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/api/v1/auth":
            return httpx.Response(
                HTTPStatus.OK, json={"access_token": "token-123"}
            )
        if request.method == "GET" and request.url.path == "/api/v1/auth":
            assert request.headers["Authorization"] == "Bearer token-123"
            return httpx.Response(
                HTTPStatus.OK,
                json={
                    "wallets": [
                        {
                            "id": "wallet-1",
                            "inkey": "invoice-key",
                            "adminkey": "admin-key",
                        }
                    ]
                },
            )
        return httpx.Response(
            HTTPStatus.NOT_FOUND, json={"detail": "not found"}
        )

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(handler),
    ) as http:
        auth = LNbitsAuth(http, "alice", "secret")

        token = await auth.login()
        account = await auth.get_account_info(token)

    assert token == Token(access_token="token-123")
    assert isinstance(account, Account)
    assert account.wallets[0].id == "wallet-1"
    assert [request.method for request in requests] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_auth_methods_return_failed_for_http_errors_and_invalid_types():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                HTTPStatus.UNAUTHORIZED,
                json={"detail": "bad login"},
            )
        return httpx.Response(HTTPStatus.OK, json=[])

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(handler),
    ) as http:
        auth = LNbitsAuth(http, "alice", "secret")

        login = await auth.login()
        account = await auth.get_account_info(Token(access_token="token-123"))

    assert login == Failed(detail="bad login", status="failed")
    assert account == Failed(detail="Invalid response type!", status="failed")


@pytest.mark.asyncio
async def test_auth_login_returns_failed_for_invalid_response_schema():
    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                HTTPStatus.OK, json={"unexpected": True}
            )
        ),
    ) as http:
        auth = LNbitsAuth(http, "alice", "secret")

        result = await auth.login()

    assert result == Failed(detail="Invalid response schema!", status="failed")


@pytest.mark.asyncio
async def test_auth_methods_return_failed_for_invalid_response_type_and_schema():  # noqa: E501
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(HTTPStatus.OK, json=[])
        return httpx.Response(
            HTTPStatus.OK,
            json={"wallets": [{"id": "wallet-without-keys"}]},
        )

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(handler),
    ) as http:
        auth = LNbitsAuth(http, "alice", "secret")

        login = await auth.login()
        account = await auth.get_account_info(Token(access_token="token-123"))

    assert login == Failed(detail="Invalid response type!", status="failed")
    assert account == Failed(
        detail="Invalid response schema!", status="failed"
    )


@pytest.mark.asyncio
async def test_auth_methods_return_failed_for_timeout_and_connect_error():
    async def timeout_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    async def connect_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(timeout_handler),
    ) as http:
        auth = LNbitsAuth(http, "alice", "secret")

        login_timeout = await auth.login()
        account_timeout = await auth.get_account_info(
            Token(access_token="token-123")
        )

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(connect_handler),
    ) as http:
        auth = LNbitsAuth(http, "alice", "secret")

        login_connect = await auth.login()
        account_connect = await auth.get_account_info(
            Token(access_token="token-123")
        )

    assert login_timeout == Failed(detail="Timeout!", status="failed")
    assert account_timeout == Failed(detail="Timeout!", status="failed")
    assert login_connect == Failed(detail="Connect error!", status="failed")
    assert account_connect == Failed(detail="Connect error!", status="failed")


@pytest.mark.asyncio
async def test_wallet_create_invoice_posts_expected_payload_and_headers():
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["api_key"] = request.headers["X-Api-Key"]
        seen["json"] = request.read()
        return httpx.Response(
            HTTPStatus.CREATED,
            json={"checking_id": "chk-1", "payment_request": "lnbc1"},
        )

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(handler),
    ) as http:
        wallet = LNbitsWallet(
            http=http,
            invoice_read_key="invoice-key",
            admin_key="admin-key",
        )

        result = await wallet.create_invoice(
            CreateInvoice(amount=21, memo="deposit", expiry=900)
        )

    assert result == Payment(checking_id="chk-1", payment_request="lnbc1")
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/payments"
    assert seen["api_key"] == "invoice-key"
    assert (
        seen["json"]
        == b'{"out":false,"amount":21,"memo":"deposit","expiry":900}'
    )


@pytest.mark.asyncio
async def test_wallet_payment_wrappers_return_typed_models():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v1/lnurlscan/lnurl-value":
            return httpx.Response(
                HTTPStatus.OK,
                json={
                    "callback": "https://callback.test",
                    "description": "coffee",
                },
            )
        if request.url.path == "/api/v1/payments/lnurl":
            return httpx.Response(
                HTTPStatus.OK,
                json={"payment_hash": "hash-lnurl", "paid": True},
            )
        if request.url.path == "/api/v1/payments/decode":
            return httpx.Response(
                HTTPStatus.OK,
                json={"payment_hash": "hash-decode", "amount_msat": 1000},
            )
        if request.url.path == "/api/v1/payments/checking-id":
            return httpx.Response(
                HTTPStatus.OK,
                json={
                    "checking_id": "checking-id",
                    "payment_hash": "hash-get",
                    "amount": 1000,
                    "paid": True,
                },
            )
        if request.url.path == "/api/v1/payments":
            return httpx.Response(
                HTTPStatus.OK,
                json={"payment_hash": "hash-pay", "paid": True},
            )
        return httpx.Response(
            HTTPStatus.NOT_FOUND, json={"detail": "not found"}
        )

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(handler),
    ) as http:
        wallet = LNbitsWallet(
            http=http,
            invoice_read_key="invoice-key",
            admin_key="admin-key",
        )

        decoded_lnurl = await wallet.decode_lnurl("lnurl-value")
        paid_lnurl = await wallet.pay_lnurl(
            "lnurl-value", amount=10, comment="ok"
        )
        payment_status = await wallet.get_payment("checking-id")
        decoded_invoice = await wallet.decode_invoice("lnbc1")
        paid_invoice = await wallet.pay_invoice("lnbc1")

    assert decoded_lnurl == LNURLDecoded(
        callback="https://callback.test",
        description="coffee",
    )
    assert paid_lnurl == Payment(payment_hash="hash-lnurl", paid=True)
    assert payment_status == Payment(
        checking_id="checking-id",
        payment_hash="hash-get",
        amount=1000,
        paid=True,
    )
    assert decoded_invoice == PaymentDecoded(
        payment_hash="hash-decode",
        amount_msat=1000,
    )
    assert paid_invoice == Payment(payment_hash="hash-pay", paid=True)
    assert calls == [
        ("GET", "/api/v1/lnurlscan/lnurl-value"),
        ("GET", "/api/v1/lnurlscan/lnurl-value"),
        ("POST", "/api/v1/payments/lnurl"),
        ("GET", "/api/v1/payments/checking-id"),
        ("POST", "/api/v1/payments/decode"),
        ("POST", "/api/v1/payments"),
    ]


@pytest.mark.asyncio
async def test_wallet_wrappers_return_failed_for_bad_lnbits_responses():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/lnurlscan/bad-lnurl":
            return httpx.Response(
                HTTPStatus.BAD_REQUEST,
                json={"detail": "bad lnurl"},
            )
        if request.url.path == "/api/v1/lnurlscan/invalid-schema":
            return httpx.Response(HTTPStatus.OK, json={"not_callback": True})
        if request.url.path == "/api/v1/payments/decode":
            return httpx.Response(HTTPStatus.OK, json=[])
        if request.url.path == "/api/v1/payments":
            return httpx.Response(HTTPStatus.OK, content=b"not-json")
        return httpx.Response(HTTPStatus.NOT_FOUND, json={"detail": "missing"})

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(handler),
    ) as http:
        wallet = LNbitsWallet(
            http=http,
            invoice_read_key="invoice-key",
            admin_key="admin-key",
        )

        lnurl_failure = await wallet.pay_lnurl("bad-lnurl", amount=10)
        invalid_lnurl = await wallet.decode_lnurl("invalid-schema")
        invalid_decode = await wallet.decode_invoice("lnbc1")
        invalid_invoice = await wallet.create_invoice(CreateInvoice(amount=1))

    assert lnurl_failure == Failed(detail="bad lnurl", status="failed")
    assert invalid_lnurl == Failed(
        detail="Invalid response schema!",
        status="failed",
    )
    assert invalid_decode == Failed(
        detail="Invalid response type!",
        status="failed",
    )
    assert invalid_invoice == Failed(
        detail="Invalid JSON response!",
        status="failed",
    )


@pytest.mark.asyncio
async def test_wallet_call_returns_failed_for_connect_error():
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(handler),
    ) as http:
        wallet = LNbitsWallet(
            http=http,
            invoice_read_key="invoice-key",
            admin_key="admin-key",
        )

        result = await wallet.create_invoice(CreateInvoice(amount=1))

    assert result == Failed(detail="Connect error!", status="failed")


@pytest.mark.asyncio
async def test_wallet_call_returns_failed_for_timeout_and_non_ok_status():
    async def timeout_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(timeout_handler),
    ) as http:
        wallet = LNbitsWallet(
            http=http,
            invoice_read_key="invoice-key",
            admin_key="admin-key",
        )

        timeout = await wallet.create_invoice(CreateInvoice(amount=1))

    async with httpx.AsyncClient(
        base_url="http://lnbits.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                HTTPStatus.BAD_REQUEST,
                json={"detail": "bad payment"},
            )
        ),
    ) as http:
        wallet = LNbitsWallet(
            http=http,
            invoice_read_key="invoice-key",
            admin_key="admin-key",
        )

        non_ok = await wallet.create_invoice(CreateInvoice(amount=1))

    assert timeout == Failed(detail="Timeout!", status="failed")
    assert non_ok == Failed(detail="bad payment", status="failed")


@pytest.mark.asyncio
async def test_wallet_stop_ws_cancels_existing_task():
    async with httpx.AsyncClient(base_url="http://lnbits.test") as http:
        wallet = LNbitsWallet(
            http=http,
            invoice_read_key="invoice-key",
            admin_key="admin-key",
        )
        task = asyncio.create_task(asyncio.Event().wait())
        wallet._ws_task = task

        await wallet.stop_ws()

    assert wallet._ws_task is None
    assert task.cancelled()


@pytest.mark.asyncio
async def test_client_start_with_direct_keys_creates_wallet_without_login():
    client = LNbitsClient(
        LNbitsConfig(
            base_url="http://lnbits.test/",
            invoice_read_key="invoice-key",
            admin_key="admin-key",
        )
    )

    await client.start()
    wallet = client.wallet()

    assert wallet.base_url == "http://lnbits.test"
    assert wallet.invoice_read_key == "invoice-key"
    assert wallet.admin_key == "admin-key"

    await client.stop()

    with pytest.raises(RuntimeError, match="LNbitsClient"):
        client.wallet()


def async_client_factory(handler, real_async_client=httpx.AsyncClient):
    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    return _factory


@pytest.mark.asyncio
async def test_client_start_rejects_missing_auth_config():
    client = LNbitsClient(LNbitsConfig(base_url="http://lnbits.test"))

    with pytest.raises(RuntimeError, match="Config"):
        await client.start()

    await client.stop()


@pytest.mark.asyncio
async def test_client_start_raises_when_login_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/auth"
        return httpx.Response(
            HTTPStatus.UNAUTHORIZED,
            json={"detail": "bad login"},
        )

    monkeypatch.setattr(
        provider.httpx,
        "AsyncClient",
        async_client_factory(handler),
    )
    client = LNbitsClient(
        LNbitsConfig(
            base_url="http://lnbits.test",
            username="alice",
            password="secret",
        )
    )

    with pytest.raises(TypeError, match="bad login"):
        await client.start()

    await client.stop()


@pytest.mark.asyncio
async def test_client_start_raises_when_account_info_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                HTTPStatus.OK,
                json={"access_token": "token-123"},
            )
        return httpx.Response(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            json={"detail": "account down"},
        )

    monkeypatch.setattr(
        provider.httpx,
        "AsyncClient",
        async_client_factory(handler),
    )
    client = LNbitsClient(
        LNbitsConfig(
            base_url="http://lnbits.test",
            username="alice",
            password="secret",
        )
    )

    with pytest.raises(TypeError, match="account down"):
        await client.start()

    await client.stop()


@pytest.mark.asyncio
async def test_client_start_raises_when_account_has_no_wallets(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                HTTPStatus.OK,
                json={"access_token": "token-123"},
            )
        return httpx.Response(HTTPStatus.OK, json={"wallets": []})

    monkeypatch.setattr(
        provider.httpx,
        "AsyncClient",
        async_client_factory(handler),
    )
    client = LNbitsClient(
        LNbitsConfig(
            base_url="http://lnbits.test",
            username="alice",
            password="secret",
        )
    )

    with pytest.raises(RuntimeError, match="lista vazia"):
        await client.start()

    await client.stop()
