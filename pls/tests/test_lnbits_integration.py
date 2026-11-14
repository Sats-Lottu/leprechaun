from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from pls import commands
from pls.models.tables import OutboxEvent
from pls.schemas import CreateInvoice, Failed, Payment, PaymentDecoded


@pytest.mark.asyncio
async def test_lnbits_wallet_creates_invoice_against_container(wallet):
    invoice = await wallet.create_invoice(
        CreateInvoice(amount=1, memo="integration-test", expiry=300)
    )

    assert isinstance(invoice, Payment)
    assert not isinstance(invoice, Failed)
    assert invoice.best_invoice_id()
    assert invoice.best_payment_request().startswith("ln")


@pytest.mark.asyncio
async def test_lnbits_wallet_decodes_invoice_against_container(wallet):
    invoice = await wallet.create_invoice(
        CreateInvoice(amount=1, memo="decode-test", expiry=300)
    )
    assert isinstance(invoice, Payment)

    decoded = await wallet.decode_invoice(invoice.best_payment_request())

    assert isinstance(decoded, PaymentDecoded)
    assert not isinstance(decoded, Failed)
    assert decoded.payment_hash


@pytest.mark.asyncio
async def test_lnbits_wallet_gets_payment_against_container(wallet):
    invoice = await wallet.create_invoice(
        CreateInvoice(amount=1, memo="get-payment-test", expiry=300)
    )
    assert isinstance(invoice, Payment)

    payment = await wallet.get_payment(invoice.best_invoice_id())

    assert isinstance(payment, Payment)
    assert not isinstance(payment, Failed)
    assert payment.best_invoice_id()


@pytest.mark.asyncio
async def test_withdrawal_recovers_lost_lnbits_reply(
    wallet,
    lnbits,
    session,
    make_envelope,
    monkeypatch,
):
    async with httpx.AsyncClient(base_url=lnbits.url) as http:
        response = await http.post(
            '/api/v1/wallet',
            json={'name': 'Withdrawal recipient'},
            headers={'Authorization': f'Bearer {lnbits.access_token}'},
        )
        response.raise_for_status()
        response = await http.post(
            '/api/v1/payments',
            json={'out': False, 'amount': 21, 'memo': 'withdrawal replay'},
            headers={'X-Api-Key': response.json()['inkey']},
        )
        response.raise_for_status()
        invoice = Payment(**response.json())

    env = make_envelope(
        {
            'payment_request': invoice.best_payment_request(),
            'user_id': 'user-1',
            'amount_msat': 21_000,
            'payment_reference': 'withdrawal-test',
        },
        msg_type='payment.invoice.pay',
    )
    monkeypatch.setattr(commands, 'publish_pending_outbox', AsyncMock())
    original_pay = wallet.pay_invoice

    async def lose_response(*args, **kwargs):
        payment = await original_pay(*args, **kwargs)
        assert isinstance(payment, Payment), payment
        return Failed(detail='Timeout!')

    pay = AsyncMock(side_effect=lose_response)
    monkeypatch.setattr(wallet, 'pay_invoice', pay)
    handle = commands.handle_payment_commands._original_call
    first = await handle(env, wallet, 'reply', 'first')
    assert first.body['status'] == 'pending'
    assert await session.scalar(select(OutboxEvent)) is None

    await handle(env, wallet, 'reply', 'retry')
    replay = await handle(env, wallet, 'reply', 'replay')
    assert replay.body['status'] == 'paid'
    pay.assert_awaited_once()
    events = (await session.scalars(select(OutboxEvent))).all()
    assert [event.event_type for event in events] == ['payment.sent']
    assert events[0].payload['data']['payment_reference'] == 'withdrawal-test'
