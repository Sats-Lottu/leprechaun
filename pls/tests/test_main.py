import asyncio

import pytest
from faststream import Response
from sqlalchemy import select

from pls import (
    commands,
    contexts,
    idempotency,
    lifecycle,
    main,
    messaging,
    observability,
    outbox,
    reconciliation,
    utils,
)
from pls.models.enums import (
    InvoiceStatus,
    OperationStatus,
    OutboxStatus,
    PaymentDirection,
    ProviderEventStatus,
)
from pls.models.tables import (
    LightningInvoice,
    OperationIdempotency,
    OutboxEvent,
    ProviderEvent,
)
from pls.provider import LNbitsConfig
from pls.schemas import Failed, Payment, WalletResponse


class FakePayWallet:
    async def pay_invoice(self, payment_request):
        self.payment_request = payment_request
        return Payment(
            payment_hash="hash-pay", checking_id="chk-pay", paid=True
        )

    async def pay_lnurl(self, lnurl, amount, comment=""):
        self.lnurl = lnurl
        self.amount = amount
        self.comment = comment
        return Payment(
            payment_hash="hash-lnurl", checking_id="chk-lnurl", paid=True
        )


class FakeReconciliationWallet:
    def __init__(self, result):
        self.result = result
        self.checked = []

    async def get_payment(self, checking_id):
        self.checked.append(checking_id)
        return self.result


class FailingBroker:
    @staticmethod
    async def publish(_body, *, queue):
        assert queue == outbox.settings.PAYMENT_EVENTS_QUEUE
        raise RuntimeError("rabbit down")


def command_context(env, wallet, **overrides):
    return contexts.CommandContext(
        env=env,
        lnbits_wallet=wallet,
        reply_to=overrides.get("reply_to"),
        correlation_id=overrides.get("correlation_id", "corr-1"),
        session=overrides.get("session"),
        idempotency=overrides.get("idempotency"),
    )


def test_msat_to_sat_floor_never_returns_negative():
    assert utils.msat_to_sat_floor(1999) == 1
    assert utils.msat_to_sat_floor(999) == 0
    assert utils.msat_to_sat_floor(-1000) == 0


def test_time_helpers_return_utc_iso_strings():
    now = utils.now_utc_iso()
    expires_now = utils.expires_at_iso(None)
    expires_later = utils.expires_at_iso(60)

    assert "+00:00" in now
    assert "+00:00" in expires_now
    assert "+00:00" in expires_later
    assert expires_later > expires_now


def test_observability_renders_counters():
    observability.inc_counter("test_event", result="ok")

    metrics = observability.render_metrics()

    assert 'pls_events_total{event="test_event",result="ok"}' in metrics


def test_observability_handler_routes_requests(monkeypatch):
    sent = []
    handler = object.__new__(observability.ObservabilityHandler)

    def fake_send_text(body, status):
        sent.append((body, status))

    monkeypatch.setattr(handler, "_send_text", fake_send_text)

    handler.path = "/health"
    observability.ObservabilityHandler.do_GET(handler)
    handler.path = "/metrics"
    observability.ObservabilityHandler.do_GET(handler)
    handler.path = "/missing"
    observability.ObservabilityHandler.do_GET(handler)
    observability.ObservabilityHandler.log_message(handler, "ignored")

    assert sent[0] == ("ok\n", observability.HTTPStatus.OK)
    assert sent[1][0].startswith("# HELP pls_events_total")
    assert sent[1][1] == observability.HTTPStatus.OK
    assert sent[2] == ("not found\n", observability.HTTPStatus.NOT_FOUND)


def test_observability_send_text_writes_http_response():
    calls = []

    class FakeWriter:
        @staticmethod
        def write(payload):
            calls.append(("write", payload))

    handler = object.__new__(observability.ObservabilityHandler)
    handler.wfile = FakeWriter()
    handler.send_response = lambda status: calls.append(("status", status))
    handler.send_header = lambda key, value: calls.append(
        ("header", key, value)
    )
    handler.end_headers = lambda: calls.append(("end",))

    observability.ObservabilityHandler._send_text(
        handler,
        "ok\n",
        observability.HTTPStatus.OK,
    )

    assert not observability._labels_text(())
    assert calls == [
        ("status", observability.HTTPStatus.OK),
        ("header", "Content-Type", "text/plain; charset=utf-8"),
        ("header", "Content-Length", "3"),
        ("end",),
        ("write", b"ok\n"),
    ]


def test_start_observability_server_respects_settings(monkeypatch):
    started = []

    class FakeServer:
        def __init__(self, address, handler):
            self.address = address
            self.handler = handler

        def serve_forever(self):
            started.append(self.address)

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            self.target()

    settings = observability.get_settings()
    monkeypatch.setattr(observability, "_server", None)
    monkeypatch.setattr(settings, "OBSERVABILITY_ENABLED", False)

    assert observability.start_observability_server() is None

    monkeypatch.setattr(settings, "OBSERVABILITY_ENABLED", True)
    monkeypatch.setattr(settings, "OBSERVABILITY_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "OBSERVABILITY_PORT", 0)
    monkeypatch.setattr(
        observability,
        "ThreadingHTTPServer",
        FakeServer,
    )
    monkeypatch.setattr(observability.threading, "Thread", FakeThread)

    server = observability.start_observability_server()

    assert server is observability.start_observability_server()
    assert server.address == ("127.0.0.1", 0)
    assert server.handler is observability.ObservabilityHandler
    assert started == [("127.0.0.1", 0)]


def test_iso_to_datetime_returns_none_for_empty_values():
    assert utils.iso_to_datetime(None) is None
    assert utils.iso_to_datetime("") is None


@pytest.mark.asyncio
async def test_retry_provider_call_retries_transient_failures(monkeypatch):
    calls = []
    sleeps = []

    async def flaky_call(value):
        calls.append(value)
        if len(calls) == 1:
            return Failed(detail="Timeout!")
        return Payment(payment_hash="hash-retry")

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(utils.settings, "COMMAND_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(utils.settings, "COMMAND_RETRY_BACKOFF_SEC", 0.5)
    monkeypatch.setattr(utils.asyncio, "sleep", fake_sleep)

    result = await utils.retry_provider_call(flaky_call, "arg")

    assert result == Payment(payment_hash="hash-retry")
    assert calls == ["arg", "arg"]
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_retry_provider_call_returns_final_transient_failure(
    monkeypatch,
):
    calls = 0

    async def failing_call():
        nonlocal calls
        calls += 1
        return Failed(detail="Connect error!")

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(utils.settings, "COMMAND_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(utils.asyncio, "sleep", fake_sleep)

    result = await utils.retry_provider_call(failing_call)

    assert result == Failed(detail="Connect error!")
    assert calls == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_persistent_idempotency_helpers_track_seen_keys(session):
    record = await idempotency.start_operation(
        session,
        operation="payment.invoice.create",
        idempotency_key="key-1",
        payload={"amount_msat": 1000},
    )
    await idempotency.finish_operation(
        session,
        record,
        status=OperationStatus.SUCCEEDED,
        resource_id=None,
    )

    duplicate = await idempotency.start_operation(
        session,
        operation="payment.invoice.create",
        idempotency_key="key-1",
        payload={"amount_msat": 1000},
    )

    assert duplicate is record
    assert idempotency.operation_is_duplicate(duplicate)
    assert (
        await idempotency.start_operation(
            session,
            operation="payment.invoice.create",
            idempotency_key=None,
            payload={},
        )
        is None
    )


@pytest.mark.asyncio
async def test_idempotency_load_returns_none_without_key(session):
    result = await idempotency.load_operation(
        session,
        operation="payment.invoice.create",
        idempotency_key=None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_idempotency_reused_key_with_different_payload_returns_existing(
    session,
):
    first = await idempotency.start_operation(
        session,
        operation="payment.invoice.create",
        idempotency_key="same-key",
        payload={"amount_msat": 1000},
    )
    second = await idempotency.start_operation(
        session,
        operation="payment.invoice.create",
        idempotency_key="same-key",
        payload={"amount_msat": 2000},
    )

    assert second is first


@pytest.mark.asyncio
async def test_publish_event_sends_envelope_to_events_queue(
    fake_broker,
    make_envelope,
):
    broker = fake_broker
    env = make_envelope({"user_id": "user-1", "amount_msat": 1000})

    await messaging.publish_event(broker, env)

    assert broker.published == [
        (env.model_dump(), messaging.settings.PAYMENT_EVENTS_QUEUE)
    ]


@pytest.mark.asyncio
async def test_publish_failed_command_sends_failed_envelope_to_dlq(
    fake_broker,
    make_envelope,
):
    env = make_envelope(
        {"payment_request": "lnbc1"},
        msg_type="payment.invoice.pay",
    )

    await messaging.publish_failed_command(
        fake_broker,
        env,
        reason="bad command",
        correlation_id="corr-failed",
    )

    body, queue = fake_broker.published[0]
    assert queue == messaging.settings.PAYMENT_COMMANDS_DLQ
    assert body["meta"]["type"] == "payment.command.failed"
    assert body["meta"]["correlation_id"] == "corr-failed"
    assert body["meta"]["causation_id"] == "event-1"
    assert body["data"] == {
        "reason": "bad command",
        "command_type": "payment.invoice.pay",
    }


@pytest.mark.asyncio
async def test_outbox_persists_and_publishes_pending_events(
    fake_broker,
    make_envelope,
    monkeypatch,
    session,
):
    env = make_envelope({"user_id": "user-1", "amount_msat": 1000})

    await outbox.enqueue_outbox_event(session, env)
    await session.commit()

    published = await outbox.publish_pending_outbox(broker_=fake_broker)

    stored = await session.scalar(select(OutboxEvent))
    assert published == 1
    assert stored is not None
    assert stored.status == OutboxStatus.PUBLISHED
    assert stored.attempts == 1
    assert stored.published_at is not None
    assert fake_broker.published == [
        (env.model_dump(mode="json"), outbox.settings.PAYMENT_EVENTS_QUEUE)
    ]


@pytest.mark.asyncio
async def test_publish_outbox_event_records_publish_failure(
    make_envelope,
    session,
):
    env = make_envelope({"user_id": "user-1", "amount_msat": 1000})
    event = await outbox.enqueue_outbox_event(session, env)

    with pytest.raises(RuntimeError, match="rabbit down"):
        await outbox.publish_outbox_event(FailingBroker(), event)

    assert event.status == OutboxStatus.PENDING
    assert event.attempts == 1
    assert event.error_detail == "rabbit down"


@pytest.mark.asyncio
async def test_publish_pending_outbox_continues_after_publish_failure(
    make_envelope,
    monkeypatch,
    session,
):
    first = await outbox.enqueue_outbox_event(
        session,
        make_envelope({"user_id": "user-1", "amount_msat": 1000}),
    )
    second = await outbox.enqueue_outbox_event(
        session,
        make_envelope({"user_id": "user-2", "amount_msat": 2000}),
    )
    await session.commit()
    calls = []

    async def fake_publish_outbox_event(_broker, event):
        calls.append(event.id)
        if event.id == first.id:
            event.attempts += 1
            raise RuntimeError("first failed")
        event.status = OutboxStatus.PUBLISHED
        event.attempts += 1

    monkeypatch.setattr(
        outbox,
        "publish_outbox_event",
        fake_publish_outbox_event,
    )

    published = await outbox.publish_pending_outbox(broker_=object())

    assert published == 1
    assert set(calls) == {first.id, second.id}
    assert first.status == OutboxStatus.PENDING
    assert second.status == OutboxStatus.PUBLISHED


@pytest.mark.asyncio
async def test_lnbits_lifespan_starts_injects_and_stops_dependencies(
    monkeypatch,
    fake_context,
):
    calls = []

    class FakeLifecycleWallet:
        invoice_read_key = "wallet-inkey"

    wallet = FakeLifecycleWallet()

    class FakeClient:
        def __init__(self, cfg):
            self.cfg = cfg

        @staticmethod
        async def start():
            calls.append("client.start")

        @staticmethod
        def wallet():
            return wallet

        @staticmethod
        async def stop():
            calls.append("client.stop")

    class FakeBridge:
        def __init__(self, **kwargs):
            calls.append(("bridge.init", kwargs))

        @staticmethod
        async def start():
            calls.append("bridge.start")

        @staticmethod
        async def stop():
            calls.append("bridge.stop")

    monkeypatch.setattr(lifecycle, "LNbitsClient", FakeClient)
    monkeypatch.setattr(lifecycle, "LNbitsWSBridge", FakeBridge)
    cfg = LNbitsConfig(base_url="http://lnbits.test", invoice_read_key="inkey")

    async with lifecycle.create_lnbits_lifespan(cfg)(fake_context):
        assert fake_context.values["lnbits_wallet"] is wallet
        assert "lnbits_ws_bridge" in fake_context.values
        assert calls[:3] == [
            "client.start",
            (
                "bridge.init",
                {
                    "url": "http://lnbits.test",
                    "invoice_read_key": "wallet-inkey",
                    "publisher": lifecycle.ws_publisher,
                    "event_handler": (lifecycle.handle_lnbits_wallet_event),
                },
            ),
            "bridge.start",
        ]

    assert calls[-2:] == ["bridge.stop", "client.stop"]


@pytest.mark.asyncio
async def test_handle_payment_commands_dispatches_create_invoice_and_marks_key(
    fake_wallet_factory,
    make_envelope,
    monkeypatch,
    session,
):
    env = make_envelope({"user_id": "user-1", "amount_msat": 1000})
    wallet = fake_wallet_factory(Payment(checking_id="chk-1"))
    calls = []
    expected_response = object()

    async def fake_handle_create_invoice(ctx):
        calls.append(ctx)
        return expected_response

    monkeypatch.setattr(
        commands,
        "handle_create_invoice",
        fake_handle_create_invoice,
    )

    result = await commands.handle_payment_commands._original_call(
        env,
        wallet,
        "reply.queue",
        "corr-from-amqp",
    )

    assert result is expected_response
    stored = await session.get(OperationIdempotency, calls[0].idempotency.id)
    assert stored is not None
    assert calls[0] == contexts.CommandContext(
        env=env,
        lnbits_wallet=wallet,
        reply_to="reply.queue",
        correlation_id="corr-from-amqp",
        session=session,
        idempotency=stored,
    )


@pytest.mark.asyncio
async def test_handle_payment_commands_ignores_duplicate_idempotency_key(
    fake_wallet_factory,
    make_envelope,
    monkeypatch,
    session,
):
    session.add(
        OperationIdempotency(
            operation="payment.invoice.create",
            idempotency_key="idem-1",
            status=OperationStatus.SUCCEEDED,
            payload_hash=utils.payload_hash(
                {"user_id": "user-1", "amount_msat": 1000}
            ),
        )
    )
    await session.commit()
    env = make_envelope({"user_id": "user-1", "amount_msat": 1000})
    wallet = fake_wallet_factory(Payment(checking_id="chk-1"))

    async def fail_if_called(_ctx):
        pytest.fail("_handle_create_invoice should not be called")

    monkeypatch.setattr(commands, "handle_create_invoice", fail_if_called)

    result = await commands.handle_payment_commands._original_call(
        env,
        wallet,
        None,
        None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_handle_payment_commands_ignores_unknown_type(
    fake_wallet_factory,
    make_envelope,
    monkeypatch,
    session,
):
    wallet = fake_wallet_factory(Payment(checking_id="chk-1"))

    failed = []

    async def fake_publish_failed_command(_broker, failed_env, **kwargs):
        failed.append((failed_env, kwargs))

    monkeypatch.setattr(
        commands, "publish_failed_command", fake_publish_failed_command
    )
    unknown_env = make_envelope({}, msg_type="payment.unknown")
    unknown_env.meta.idempotency_key = "unknown"
    unknown = await commands.handle_payment_commands._original_call(
        unknown_env,
        wallet,
        None,
        None,
    )

    assert unknown is None
    assert failed[0][1]["reason"] == "unknown message type: payment.unknown"


@pytest.mark.asyncio
async def test_handle_payment_commands_dispatches_pay_invoice(
    make_envelope,
    monkeypatch,
    session,
):
    wallet = FakePayWallet()
    published = []
    env = make_envelope(
        {
            "payment_request": "lnbc1",
            "user_id": "user-1",
            "amount_msat": 1000,
            "payment_reference": "withdrawal-1",
        },
        msg_type="payment.invoice.pay",
    )

    async def fake_publish_pending_outbox():
        published.append("called")
        return 1

    monkeypatch.setattr(
        commands,
        "publish_pending_outbox",
        fake_publish_pending_outbox,
    )

    response = await commands.handle_payment_commands._original_call(
        env,
        wallet,
        "reply.queue",
        "corr-pay",
    )

    invoice = await session.scalar(select(LightningInvoice))
    assert isinstance(response, Response)
    assert wallet.payment_request == "lnbc1"
    assert invoice is not None
    assert invoice.direction == PaymentDirection.OUTGOING
    assert published == ["called"]


@pytest.mark.asyncio
async def test_handle_payment_commands_dispatches_pay_lnurl(
    make_envelope,
    monkeypatch,
    session,
):
    wallet = FakePayWallet()
    published = []
    env = make_envelope(
        {
            "lnurl": "lnurl1",
            "user_id": "user-1",
            "amount_msat": 3000,
            "comment": "tip",
        },
        msg_type="payment.lnurl.pay",
    )

    async def fake_publish_pending_outbox():
        published.append("called")
        return 1

    monkeypatch.setattr(
        commands,
        "publish_pending_outbox",
        fake_publish_pending_outbox,
    )

    response = await commands.handle_payment_commands._original_call(
        env,
        wallet,
        "reply.queue",
        "corr-lnurl",
    )

    invoice = await session.scalar(select(LightningInvoice))
    assert isinstance(response, Response)
    assert wallet.lnurl == "lnurl1"
    assert wallet.amount == 3  # noqa: PLR2004
    assert wallet.comment == "tip"
    assert invoice is not None
    assert invoice.direction == PaymentDirection.OUTGOING
    assert published == ["called"]


@pytest.mark.asyncio
async def test_handle_create_invoice_publishes_event_and_returns_reply(
    create_invoice_payload,
    fake_wallet_factory,
    make_envelope,
    published_events,
):
    wallet = fake_wallet_factory(
        Payment(checking_id="chk-1", payment_request="lnbc1")
    )
    env = make_envelope(create_invoice_payload)

    response = await commands.handle_create_invoice(
        command_context(
            env,
            wallet,
            reply_to="reply.queue",
            correlation_id="corr-from-amqp",
        )
    )

    assert isinstance(response, Response)
    assert wallet.created[0].amount == 21  # noqa: PLR2004
    assert wallet.created[0].memo == "deposit"
    assert wallet.created[0].expiry == 600  # noqa: PLR2004
    assert published_events[0].meta.type == "payment.invoice.created"
    assert published_events[0].meta.correlation_id == "corr-from-amqp"
    assert published_events[0].meta.causation_id == "event-1"
    assert published_events[0].data["invoice_id"] == "chk-1"
    assert published_events[0].data["payment_request"] == "lnbc1"
    assert published_events[0].data["amount_msat"] == 21000  # noqa: PLR2004
    assert response.body["invoice_id"] == "chk-1"
    assert response.correlation_id == "corr-from-amqp"


@pytest.mark.asyncio
async def test_handle_create_invoice_persists_invoice_and_idempotency(
    create_invoice_payload,
    fake_wallet_factory,
    make_envelope,
    session,
):
    wallet = fake_wallet_factory(
        Payment(
            checking_id="chk-persist",
            payment_hash="hash-persist",
            payment_request="lnbc1",
        )
    )
    env = make_envelope(create_invoice_payload)
    idempotency_record = await idempotency.start_operation(
        session,
        operation=env.meta.type,
        idempotency_key=env.meta.idempotency_key,
        payload=env.data,
    )

    await commands.handle_create_invoice(
        command_context(
            env,
            wallet,
            correlation_id="corr-from-amqp",
            session=session,
            idempotency=idempotency_record,
        )
    )
    await session.commit()

    invoice = await session.scalar(select(LightningInvoice))
    operation = await session.scalar(select(OperationIdempotency))
    outbox = await session.scalar(select(OutboxEvent))

    assert invoice is not None
    assert invoice.user_id == "user-1"
    assert invoice.provider_invoice_id == "chk-persist"
    assert invoice.payment_hash == "hash-persist"
    assert invoice.payment_request == "lnbc1"
    assert invoice.idempotency_key == env.meta.idempotency_key
    assert operation is not None
    assert operation.status == OperationStatus.SUCCEEDED
    assert operation.resource_id == invoice.id
    assert outbox is not None
    assert outbox.status == OutboxStatus.PENDING
    assert outbox.payload["data"]["invoice_id"] == "chk-persist"


@pytest.mark.asyncio
async def test_handle_create_invoice_without_reply_returns_none(
    create_invoice_payload,
    fake_wallet_factory,
    make_envelope,
    published_events,
):
    wallet = fake_wallet_factory(
        Payment(payment_hash="hash-1", bolt11="lnbc1")
    )

    result = await commands.handle_create_invoice(
        command_context(
            make_envelope(create_invoice_payload),
            wallet,
            correlation_id="corr-from-envelope",
        )
    )

    assert result is None
    assert published_events[0].data["invoice_id"] == "hash-1"
    assert published_events[0].data["payment_request"] == "lnbc1"


@pytest.mark.asyncio
async def test_handle_create_invoice_ignores_invalid_payload(
    fake_wallet_factory,
    make_envelope,
    published_events,
):
    wallet = fake_wallet_factory(
        Payment(checking_id="chk-1", payment_request="lnbc1")
    )

    result = await commands.handle_create_invoice(
        command_context(
            make_envelope(
                {"user_id": "user-1", "amount_msat": 1000, "extra": True}
            ),
            wallet,
            correlation_id="corr-1",
        )
    )

    assert result is None
    assert wallet.created == []
    assert published_events == []


@pytest.mark.asyncio
async def test_handle_create_invoice_ignores_amounts_below_one_sat(
    fake_wallet_factory,
    make_envelope,
    published_events,
):
    wallet = fake_wallet_factory(
        Payment(checking_id="chk-1", payment_request="lnbc1")
    )

    result = await commands.handle_create_invoice(
        command_context(
            make_envelope({"user_id": "user-1", "amount_msat": 999}),
            wallet,
            correlation_id="corr-1",
        )
    )

    assert result is None
    assert wallet.created == []
    assert published_events == []


@pytest.mark.asyncio
async def test_handle_create_invoice_ignores_lnbits_failure(
    fake_wallet_factory,
    make_envelope,
    published_events,
):
    wallet = fake_wallet_factory(Failed(detail="lnbits down"))

    result = await commands.handle_create_invoice(
        command_context(
            make_envelope({"user_id": "user-1", "amount_msat": 1000}),
            wallet,
            reply_to="reply.queue",
            correlation_id="corr-1",
        )
    )

    assert result is None
    assert published_events == []


@pytest.mark.asyncio
async def test_handle_create_invoice_marks_invalid_payload_failed_with_session(
    fake_wallet_factory,
    make_envelope,
    monkeypatch,
    session,
):
    wallet = fake_wallet_factory(Payment(checking_id="chk-1"))
    env = make_envelope({"user_id": "user-1", "amount_msat": 1000})
    idempotency_record = await idempotency.start_operation(
        session,
        operation=env.meta.type,
        idempotency_key=env.meta.idempotency_key,
        payload=env.data,
    )
    failed = []
    env.data["extra"] = True

    async def fake_publish_failed_command(_broker, _env, **kwargs):
        failed.append(kwargs["reason"])

    monkeypatch.setattr(
        commands,
        "publish_failed_command",
        fake_publish_failed_command,
    )

    result = await commands.handle_create_invoice(
        command_context(
            env,
            wallet,
            session=session,
            idempotency=idempotency_record,
        )
    )

    assert result is None
    assert idempotency_record.status == OperationStatus.FAILED
    assert failed == ["invalid create_invoice payload"]


@pytest.mark.asyncio
async def test_handle_create_invoice_marks_tiny_amount_failed_with_session(
    fake_wallet_factory,
    make_envelope,
    monkeypatch,
    session,
):
    wallet = fake_wallet_factory(Payment(checking_id="chk-1"))
    env = make_envelope({"user_id": "user-1", "amount_msat": 999})
    idempotency_record = await idempotency.start_operation(
        session,
        operation=env.meta.type,
        idempotency_key=env.meta.idempotency_key,
        payload=env.data,
    )
    failed = []

    async def fake_publish_failed_command(_broker, _env, **kwargs):
        failed.append(kwargs["reason"])

    monkeypatch.setattr(
        commands,
        "publish_failed_command",
        fake_publish_failed_command,
    )

    result = await commands.handle_create_invoice(
        command_context(
            env,
            wallet,
            session=session,
            idempotency=idempotency_record,
        )
    )

    assert result is None
    assert idempotency_record.status == OperationStatus.FAILED
    assert failed == ["amount_msat below one satoshi"]


@pytest.mark.asyncio
async def test_handle_create_invoice_marks_provider_failure_with_session(
    fake_wallet_factory,
    make_envelope,
    monkeypatch,
    session,
):
    wallet = fake_wallet_factory(Failed(detail="lnbits down"))
    env = make_envelope({"user_id": "user-1", "amount_msat": 1000})
    idempotency_record = await idempotency.start_operation(
        session,
        operation=env.meta.type,
        idempotency_key=env.meta.idempotency_key,
        payload=env.data,
    )
    failed = []

    async def fake_publish_failed_command(_broker, _env, **kwargs):
        failed.append(kwargs["reason"])

    monkeypatch.setattr(
        commands,
        "publish_failed_command",
        fake_publish_failed_command,
    )

    result = await commands.handle_create_invoice(
        command_context(
            env,
            wallet,
            session=session,
            idempotency=idempotency_record,
        )
    )

    assert result is None
    assert idempotency_record.status == OperationStatus.FAILED
    assert failed == ["lnbits down"]


@pytest.mark.asyncio
async def test_handle_pay_invoice_publishes_payment_sent(
    published_events, make_envelope
):
    wallet = FakePayWallet()
    env = make_envelope(
        {
            "payment_request": "lnbc1",
            "user_id": "user-1",
            "amount_msat": 1000,
            "payment_reference": "withdrawal-1",
        },
        msg_type="payment.invoice.pay",
    )

    response = await commands.handle_pay_invoice(
        command_context(
            env,
            wallet,
            reply_to="reply.queue",
            correlation_id="corr-1",
        )
    )

    assert isinstance(response, Response)
    assert wallet.payment_request == "lnbc1"
    assert published_events[0].meta.type == "payment.sent"
    assert published_events[0].data["payment_hash"] == "hash-pay"
    assert (
        published_events[0].data["payment_reference"] == "withdrawal-1"
    )
    assert response.body["checking_id"] == "chk-pay"


@pytest.mark.asyncio
async def test_handle_pay_invoice_marks_invalid_payload_failed_with_session(
    make_envelope,
    monkeypatch,
    session,
):
    wallet = FakePayWallet()
    env = make_envelope({}, msg_type="payment.invoice.pay")
    idempotency_record = await idempotency.start_operation(
        session,
        operation=env.meta.type,
        idempotency_key=env.meta.idempotency_key,
        payload=env.data,
    )
    failed = []

    async def fake_publish_failed_command(_broker, _env, **kwargs):
        failed.append(kwargs["reason"])

    monkeypatch.setattr(
        commands,
        "publish_failed_command",
        fake_publish_failed_command,
    )

    result = await commands.handle_pay_invoice(
        command_context(
            env,
            wallet,
            session=session,
            idempotency=idempotency_record,
        )
    )

    assert result is None
    assert idempotency_record.status == OperationStatus.FAILED
    assert failed == ["invalid pay_invoice payload"]


@pytest.mark.asyncio
async def test_handle_pay_invoice_marks_provider_failure_with_session(
    make_envelope,
    monkeypatch,
    session,
):
    class FailedPayWallet:
        @staticmethod
        async def pay_invoice(_payment_request):
            return Failed(detail="invoice rejected")

    env = make_envelope(
        {"payment_request": "lnbc1"},
        msg_type="payment.invoice.pay",
    )
    idempotency_record = await idempotency.start_operation(
        session,
        operation=env.meta.type,
        idempotency_key=env.meta.idempotency_key,
        payload=env.data,
    )
    failed = []

    async def fake_publish_failed_command(_broker, _env, **kwargs):
        failed.append(kwargs["reason"])

    monkeypatch.setattr(
        commands,
        "publish_failed_command",
        fake_publish_failed_command,
    )

    result = await commands.handle_pay_invoice(
        command_context(
            env,
            FailedPayWallet(),
            session=session,
            idempotency=idempotency_record,
        )
    )

    assert result is None
    assert idempotency_record.status == OperationStatus.FAILED
    assert failed == ["invoice rejected"]


@pytest.mark.asyncio
async def test_handle_pay_lnurl_converts_msat_and_publishes_payment_sent(
    published_events,
    make_envelope,
):
    wallet = FakePayWallet()
    env = make_envelope(
        {
            "lnurl": "lnurl1",
            "user_id": "user-1",
            "amount_msat": 2500,
            "comment": "ok",
        },
        msg_type="payment.lnurl.pay",
    )

    response = await commands.handle_pay_lnurl(
        command_context(
            env,
            wallet,
            reply_to="reply.queue",
            correlation_id="corr-1",
        )
    )

    assert isinstance(response, Response)
    assert wallet.lnurl == "lnurl1"
    assert wallet.amount == 2  # noqa: PLR2004
    assert wallet.comment == "ok"
    assert published_events[0].meta.type == "payment.sent"
    assert published_events[0].data["payment_hash"] == "hash-lnurl"


@pytest.mark.asyncio
async def test_handle_pay_lnurl_marks_invalid_payload_failed_with_session(
    make_envelope,
    monkeypatch,
    session,
):
    wallet = FakePayWallet()
    env = make_envelope({}, msg_type="payment.lnurl.pay")
    idempotency_record = await idempotency.start_operation(
        session,
        operation=env.meta.type,
        idempotency_key=env.meta.idempotency_key,
        payload=env.data,
    )
    failed = []

    async def fake_publish_failed_command(_broker, _env, **kwargs):
        failed.append(kwargs["reason"])

    monkeypatch.setattr(
        commands,
        "publish_failed_command",
        fake_publish_failed_command,
    )

    result = await commands.handle_pay_lnurl(
        command_context(
            env,
            wallet,
            session=session,
            idempotency=idempotency_record,
        )
    )

    assert result is None
    assert idempotency_record.status == OperationStatus.FAILED
    assert failed == ["invalid pay_lnurl payload"]


@pytest.mark.asyncio
async def test_handle_pay_lnurl_marks_tiny_amount_failed_with_session(
    make_envelope,
    monkeypatch,
    session,
):
    wallet = FakePayWallet()
    env = make_envelope(
        {"lnurl": "lnurl1", "amount_msat": 999},
        msg_type="payment.lnurl.pay",
    )
    idempotency_record = await idempotency.start_operation(
        session,
        operation=env.meta.type,
        idempotency_key=env.meta.idempotency_key,
        payload=env.data,
    )
    failed = []

    async def fake_publish_failed_command(_broker, _env, **kwargs):
        failed.append(kwargs["reason"])

    monkeypatch.setattr(
        commands,
        "publish_failed_command",
        fake_publish_failed_command,
    )

    result = await commands.handle_pay_lnurl(
        command_context(
            env,
            wallet,
            session=session,
            idempotency=idempotency_record,
        )
    )

    assert result is None
    assert idempotency_record.status == OperationStatus.FAILED
    assert failed == ["amount_msat below one satoshi"]


@pytest.mark.asyncio
async def test_handle_pay_lnurl_marks_provider_failure_with_session(
    make_envelope,
    monkeypatch,
    session,
):
    class FailedLnurlWallet:
        @staticmethod
        async def pay_lnurl(_lnurl, _amount, _comment=""):
            return Failed(detail="lnurl rejected")

    env = make_envelope(
        {"lnurl": "lnurl1", "amount_msat": 1000},
        msg_type="payment.lnurl.pay",
    )
    idempotency_record = await idempotency.start_operation(
        session,
        operation=env.meta.type,
        idempotency_key=env.meta.idempotency_key,
        payload=env.data,
    )
    failed = []

    async def fake_publish_failed_command(_broker, _env, **kwargs):
        failed.append(kwargs["reason"])

    monkeypatch.setattr(
        commands,
        "publish_failed_command",
        fake_publish_failed_command,
    )

    result = await commands.handle_pay_lnurl(
        command_context(
            env,
            FailedLnurlWallet(),
            session=session,
            idempotency=idempotency_record,
        )
    )

    assert result is None
    assert idempotency_record.status == OperationStatus.FAILED
    assert failed == ["lnurl rejected"]


@pytest.mark.asyncio
async def test_pay_lnurl_persistence_uses_outgoing_direction(
    make_envelope,
    session,
):
    wallet = FakePayWallet()
    env = make_envelope(
        {"lnurl": "lnurl1", "amount_msat": 2000},
        msg_type="payment.lnurl.pay",
    )

    await commands.handle_pay_lnurl(
        command_context(
            env,
            wallet,
            correlation_id="corr-1",
            session=session,
        )
    )
    await session.commit()

    invoice = await session.scalar(select(LightningInvoice))
    outbox = await session.scalar(select(OutboxEvent))
    assert invoice is not None
    assert invoice.direction == PaymentDirection.OUTGOING
    assert invoice.status == InvoiceStatus.PAID
    assert outbox is not None
    assert outbox.event_type == "payment.sent"


@pytest.mark.asyncio
async def test_reconcile_lnbits_wallet_event_marks_invoice_paid(session):
    invoice = LightningInvoice(
        user_id="user-1",
        amount_msat=20000,
        provider_invoice_id="chk-paid",
        payment_hash="hash-paid",
        payment_request="lnbc1",
    )
    session.add(invoice)
    await session.flush()
    evt = WalletResponse(
        wallet_balance=20000,
        payment={
            "checking_id": "chk-paid",
            "payment_hash": "hash-paid",
            "amount": 20000,
            "fee": 0,
            "status": "success",
            "time": "2026-04-18T15:00:00+00:00",
            "created_at": "2026-04-18T15:00:00+00:00",
            "updated_at": "2026-04-18T15:00:01+00:00",
        },
    )

    events = await reconciliation.reconcile_lnbits_wallet_event(evt, session)
    await session.commit()

    assert invoice.status == InvoiceStatus.PAID
    assert invoice.paid_at is not None
    assert events[0].meta.type == "payment.invoice.paid"
    assert events[0].data["user_id"] == "user-1"
    assert events[0].data["invoice_id"] == "chk-paid"
    assert events[0].data["payment_hash"] == "hash-paid"


@pytest.mark.asyncio
async def test_reconcile_lnbits_wallet_event_ignores_unmatched_payment(
    session,
):
    evt = WalletResponse(
        wallet_balance=20000,
        payment={
            "checking_id": "chk-missing",
            "payment_hash": "hash-missing",
            "amount": 20000,
            "fee": 0,
            "status": "success",
            "time": "2026-04-18T15:00:00+00:00",
            "created_at": "2026-04-18T15:00:00+00:00",
            "updated_at": "2026-04-18T15:00:01+00:00",
        },
    )

    events = await reconciliation.reconcile_lnbits_wallet_event(evt, session)

    provider_event = await session.scalar(select(ProviderEvent))
    assert events == []
    assert provider_event is not None
    assert provider_event.status == ProviderEventStatus.RECEIVED


@pytest.mark.asyncio
async def test_reconcile_lnbits_wallet_event_records_failed_status(session):
    invoice = LightningInvoice(
        user_id="user-1",
        amount_msat=20000,
        provider_invoice_id="chk-failed",
        payment_hash="hash-failed",
        payment_request="lnbc1",
    )
    session.add(invoice)
    await session.flush()
    evt = WalletResponse(
        wallet_balance=20000,
        payment={
            "checking_id": "chk-failed",
            "payment_hash": "hash-failed",
            "amount": 20000,
            "fee": 0,
            "status": "failed",
            "time": "2026-04-18T15:00:00+00:00",
            "created_at": "2026-04-18T15:00:00+00:00",
            "updated_at": "2026-04-18T15:00:01+00:00",
        },
    )

    events = await reconciliation.reconcile_lnbits_wallet_event(evt, session)

    provider_event = await session.scalar(select(ProviderEvent))
    assert events == []
    assert invoice.status == InvoiceStatus.PENDING
    assert provider_event is not None
    assert provider_event.status == ProviderEventStatus.FAILED
    assert provider_event.error_detail == "unhandled payment status: failed"


@pytest.mark.asyncio
async def test_handle_lnbits_wallet_event_enqueues_paid_event(
    monkeypatch,
    session,
):
    invoice = LightningInvoice(
        user_id="user-1",
        amount_msat=20000,
        provider_invoice_id="chk-paid",
        payment_hash="hash-paid",
        payment_request="lnbc1",
    )
    session.add(invoice)
    await session.commit()
    evt = WalletResponse(
        wallet_balance=20000,
        payment={
            "checking_id": "chk-paid",
            "payment_hash": "hash-paid",
            "amount": 20000,
            "fee": 0,
            "status": "success",
            "time": "2026-04-18T15:00:00+00:00",
            "created_at": "2026-04-18T15:00:00+00:00",
            "updated_at": "2026-04-18T15:00:01+00:00",
        },
    )
    published = []

    async def fake_publish_pending_outbox():
        published.append("called")
        return 1

    monkeypatch.setattr(
        reconciliation,
        "publish_pending_outbox",
        fake_publish_pending_outbox,
    )

    result = await reconciliation.handle_lnbits_wallet_event(evt)

    outbox = await session.scalar(select(OutboxEvent))
    assert result == []
    assert invoice.status == InvoiceStatus.PAID
    assert outbox is not None
    assert outbox.event_type == "payment.invoice.paid"
    assert published == ["called"]


@pytest.mark.asyncio
async def test_reconcile_pending_invoices_queries_lnbits_and_enqueues_event(
    monkeypatch,
    session,
):
    invoice = LightningInvoice(
        user_id="user-1",
        amount_msat=20000,
        provider_invoice_id="chk-reconcile",
        payment_hash="hash-local",
        payment_request="lnbc1",
    )
    session.add(invoice)
    await session.commit()
    wallet = FakeReconciliationWallet(
        Payment(
            checking_id="chk-reconcile",
            payment_hash="hash-provider",
            amount=20000,
            paid=True,
        )
    )
    published = []

    async def fake_publish_pending_outbox():
        published.append("called")
        return 1

    monkeypatch.setattr(
        reconciliation,
        "publish_pending_outbox",
        fake_publish_pending_outbox,
    )

    reconciled = await reconciliation.reconcile_pending_invoices(
        wallet, limit=10
    )

    outbox = await session.scalar(select(OutboxEvent))
    assert reconciled == 1
    assert wallet.checked == ["chk-reconcile"]
    assert invoice.status == InvoiceStatus.PAID
    assert outbox is not None
    assert outbox.event_type == "payment.invoice.paid"
    assert outbox.payload["data"]["payment_hash"] == "hash-provider"
    assert published == ["called"]


@pytest.mark.asyncio
async def test_reconcile_pending_invoices_skips_provider_failure(session):
    invoice = LightningInvoice(
        user_id="user-1",
        amount_msat=20000,
        provider_invoice_id="chk-failed-lookup",
        payment_hash="hash-local",
        payment_request="lnbc1",
    )
    session.add(invoice)
    await session.commit()
    wallet = FakeReconciliationWallet(Failed(detail="provider down"))

    reconciled = await reconciliation.reconcile_pending_invoices(
        wallet,
        limit=10,
    )

    assert reconciled == 0
    assert wallet.checked == ["chk-failed-lookup"]
    assert invoice.status == InvoiceStatus.PENDING


@pytest.mark.asyncio
async def test_reconcile_pending_invoices_keeps_unpaid_invoice_pending(
    session,
):
    invoice = LightningInvoice(
        user_id="user-1",
        amount_msat=20000,
        provider_invoice_id="chk-pending",
        payment_hash="hash-local",
        payment_request="lnbc1",
    )
    session.add(invoice)
    await session.commit()
    wallet = FakeReconciliationWallet(
        Payment(
            checking_id="chk-pending",
            payment_hash="hash-local",
            amount=20000,
            paid=False,
        )
    )

    reconciled = await reconciliation.reconcile_pending_invoices(
        wallet,
        limit=10,
    )

    provider_event = await session.scalar(select(ProviderEvent))
    assert reconciled == 0
    assert wallet.checked == ["chk-pending"]
    assert invoice.status == InvoiceStatus.PENDING
    assert provider_event is not None
    assert provider_event.status == ProviderEventStatus.RECEIVED


@pytest.mark.asyncio
async def test_reconcile_pending_invoices_skips_invoice_without_provider_id(
    monkeypatch,
):
    class FakeScalarResult:
        @staticmethod
        def all():
            return [
                LightningInvoice(
                    user_id="user-1",
                    amount_msat=1000,
                    provider_invoice_id=None,
                )
            ]

    class FakeSession:
        @staticmethod
        async def scalars(_query):
            return FakeScalarResult()

        @staticmethod
        async def commit():
            return None

    class FakeSessionScope:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *_args):
            return False

    class WalletThatMustNotBeCalled:
        @staticmethod
        async def get_payment(_checking_id):
            pytest.fail("get_payment should not be called")

    monkeypatch.setattr(
        reconciliation.database,
        "session_scope",
        FakeSessionScope,
    )

    reconciled = await reconciliation.reconcile_pending_invoices(
        WalletThatMustNotBeCalled(),
        limit=10,
    )

    assert reconciled == 0


@pytest.mark.asyncio
async def test_reconcile_pending_invoices_loop_logs_and_continues(
    monkeypatch,
):
    sleeps = 0
    reconciliations = 0

    async def fake_sleep(_interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    async def fake_reconcile_pending_invoices(_wallet):
        nonlocal reconciliations
        reconciliations += 1
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(reconciliation.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        reconciliation,
        "reconcile_pending_invoices",
        fake_reconcile_pending_invoices,
    )

    with pytest.raises(asyncio.CancelledError):
        await reconciliation.reconcile_pending_invoices_loop(object())

    assert sleeps == 2  # noqa: PLR2004
    assert reconciliations == 1


@pytest.mark.asyncio
async def test_reconcile_pending_invoices_loop_propagates_cancellation(
    monkeypatch,
):
    async def fake_sleep(_interval):
        return None

    async def fake_reconcile_pending_invoices(_wallet):
        raise asyncio.CancelledError

    monkeypatch.setattr(reconciliation.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        reconciliation,
        "reconcile_pending_invoices",
        fake_reconcile_pending_invoices,
    )

    with pytest.raises(asyncio.CancelledError):
        await reconciliation.reconcile_pending_invoices_loop(object())


@pytest.mark.asyncio
async def test_pay_invoice_persistence_uses_outgoing_direction(
    make_envelope,
    session,
):
    wallet = FakePayWallet()
    env = make_envelope(
        {"payment_request": "lnbc1", "amount_msat": 1000},
        msg_type="payment.invoice.pay",
    )

    await commands.handle_pay_invoice(
        command_context(
            env,
            wallet,
            correlation_id="corr-1",
            session=session,
        )
    )
    await session.commit()

    invoice = await session.scalar(select(LightningInvoice))
    outbox = await session.scalar(select(OutboxEvent))
    assert invoice is not None
    assert invoice.direction == PaymentDirection.OUTGOING
    assert invoice.status == InvoiceStatus.PAID
    assert outbox is not None
    assert outbox.event_type == "payment.sent"


@pytest.mark.asyncio
async def test_main_logs_and_runs_app(monkeypatch):
    called = []

    class FakeApp:
        @staticmethod
        async def run():
            called.append("run")

    monkeypatch.setattr(main, "app", FakeApp())
    monkeypatch.setattr(main, "start_observability_server", lambda: None)

    await main.main()

    assert called == ["run"]
