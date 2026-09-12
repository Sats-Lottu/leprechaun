import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import pytest

from hub import rabbitmq

AMOUNT_MSAT = 1000


@pytest.fixture(autouse=True)
def clear_pending_state() -> None:
    rabbitmq.pending_responses.clear()
    rabbitmq._pending_created_at.clear()  # noqa: SLF001


@pytest.mark.asyncio
async def test_consume_payment_responses_ignores_missing_correlation() -> None:
    await rabbitmq.consume_payment_responses({'status': 'ok'}, cor_id=None)

    assert rabbitmq.pending_responses == {}


@pytest.mark.asyncio
async def test_consume_payment_responses_delivers_pending_response() -> None:
    queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    rabbitmq.pending_responses['cor-1'] = queue
    rabbitmq._pending_created_at['cor-1'] = 1.0  # noqa: SLF001

    await rabbitmq.consume_payment_responses({'status': 'ok'}, cor_id='cor-1')

    assert await queue.get() == {'status': 'ok'}
    assert 'cor-1' not in rabbitmq.pending_responses
    assert 'cor-1' not in rabbitmq._pending_created_at  # noqa: SLF001


def test_cleanup_stale_pending_removes_expired_entries(monkeypatch) -> None:
    rabbitmq.pending_responses['fresh'] = asyncio.Queue()
    rabbitmq.pending_responses['stale'] = asyncio.Queue()
    rabbitmq._pending_created_at['fresh'] = 95.0  # noqa: SLF001
    rabbitmq._pending_created_at['stale'] = 1.0  # noqa: SLF001
    monkeypatch.setattr(rabbitmq.time, 'time', lambda: 100.0)

    rabbitmq._cleanup_stale_pending()  # noqa: SLF001

    assert 'fresh' in rabbitmq.pending_responses
    assert 'stale' not in rabbitmq.pending_responses


@pytest.mark.asyncio
async def test_request_invoice_returns_response(monkeypatch) -> None:
    async def publish(
        payload,
        *,
        queue,
        reply_to,
        correlation_id,
        priority,
    ) -> None:
        assert payload['meta']['type'] == 'payment.invoice.create'
        assert payload['meta']['correlation_id'] == 'cor-1'
        assert payload['data']['user_id'] == str(
            uuid5(NAMESPACE_URL, 'leprechaun:hub:user:user-1')
        )
        assert payload['data']['amount_msat'] == AMOUNT_MSAT
        assert queue == 'payment.lightning.commands'
        assert reply_to == rabbitmq.REPLY_QUEUE
        assert correlation_id == 'cor-1'
        assert priority == 1
        await rabbitmq.pending_responses['cor-1'].put(
            {'invoice_id': 'invoice-1'}
        )

    monkeypatch.setattr(rabbitmq, 'uuid4', lambda: 'cor-1')
    monkeypatch.setattr(rabbitmq, 'broker', SimpleNamespace(publish=publish))
    monkeypatch.setattr(
        rabbitmq,
        'settings',
        SimpleNamespace(PAYMENT_COMMANDS_QUEUE='payment.lightning.commands'),
    )

    response = await rabbitmq.request_invoice_rabbitmq(
        user_id='user-1',
        amount_msat=AMOUNT_MSAT,
        timeout_sec=0.1,
    )

    assert response == {'invoice_id': 'invoice-1'}
    assert rabbitmq.pending_responses == {}


@pytest.mark.asyncio
async def test_request_invoice_times_out_and_cleans_up(monkeypatch) -> None:
    publish = AsyncMock()
    monkeypatch.setattr(rabbitmq, 'uuid4', lambda: 'cor-1')
    monkeypatch.setattr(rabbitmq, 'broker', SimpleNamespace(publish=publish))
    monkeypatch.setattr(
        rabbitmq,
        'settings',
        SimpleNamespace(PAYMENT_COMMANDS_QUEUE='payment.lightning.commands'),
    )

    response = await rabbitmq.request_invoice_rabbitmq(
        user_id='user-1',
        amount_msat=AMOUNT_MSAT,
        timeout_sec=0.001,
    )

    assert response is None
    assert rabbitmq.pending_responses == {}
    assert rabbitmq._pending_created_at == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_request_pay_invoice_returns_response(monkeypatch) -> None:
    async def publish(
        payload,
        *,
        queue,
        reply_to,
        correlation_id,
        priority,
    ) -> None:
        assert payload['meta']['type'] == 'payment.invoice.pay'
        assert payload['data']['user_id'] == str(
            uuid5(NAMESPACE_URL, 'leprechaun:hub:user:user-1')
        )
        assert payload['data']['payment_request'] == 'lnbc1withdraw'
        assert payload['data']['amount_msat'] == AMOUNT_MSAT
        assert queue == 'payment.lightning.commands'
        assert reply_to == rabbitmq.REPLY_QUEUE
        assert correlation_id == 'cor-1'
        assert priority == 1
        await rabbitmq.pending_responses['cor-1'].put(
            {'checking_id': 'chk-1', 'payment_hash': 'hash-1'}
        )

    monkeypatch.setattr(rabbitmq, 'uuid4', lambda: 'cor-1')
    monkeypatch.setattr(rabbitmq, 'broker', SimpleNamespace(publish=publish))
    monkeypatch.setattr(
        rabbitmq,
        'settings',
        SimpleNamespace(PAYMENT_COMMANDS_QUEUE='payment.lightning.commands'),
    )

    response = await rabbitmq.request_pay_invoice_rabbitmq(
        user_id='user-1',
        payment_request='lnbc1withdraw',
        amount_msat=AMOUNT_MSAT,
        timeout_sec=0.1,
    )

    assert response == {'checking_id': 'chk-1', 'payment_hash': 'hash-1'}
    assert rabbitmq.pending_responses == {}
