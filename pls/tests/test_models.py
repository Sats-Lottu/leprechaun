import pytest
from sqlalchemy import select

from pls.models.enums import (
    InvoiceStatus,
    OutboxStatus,
    PaymentDirection,
    PaymentProvider,
    ProviderEventStatus,
)
from pls.models.tables import LightningInvoice, OutboxEvent, ProviderEvent

pytestmark = pytest.mark.asyncio


async def test_lightning_invoice_model_defaults(session):
    invoice = LightningInvoice(user_id='user-1', amount_msat=1000)

    session.add(invoice)
    await session.commit()

    stored = await session.scalar(select(LightningInvoice))

    assert stored is not None
    assert stored.provider == PaymentProvider.LNBITS
    assert stored.direction == PaymentDirection.INCOMING
    assert stored.status == InvoiceStatus.PENDING
    assert stored.raw_response == {}


async def test_provider_event_can_reference_invoice(session):
    invoice = LightningInvoice(
        user_id='user-1',
        amount_msat=1000,
        provider_invoice_id='checking-id',
    )
    session.add(invoice)
    await session.flush()

    event = ProviderEvent(
        event_type='wallet.payment',
        invoice_id=invoice.id,
        provider_invoice_id='checking-id',
        payload={'payment': {'checking_id': 'checking-id'}},
    )
    session.add(event)
    await session.commit()

    stored = await session.scalar(select(ProviderEvent))

    assert stored is not None
    assert stored.provider == PaymentProvider.LNBITS
    assert stored.status == ProviderEventStatus.RECEIVED
    assert stored.invoice_id == invoice.id


async def test_outbox_event_model_defaults(session):
    event = OutboxEvent(
        event_type='payment.invoice.created',
        payload={'meta': {'type': 'payment.invoice.created'}, 'data': {}},
    )
    session.add(event)
    await session.commit()

    stored = await session.scalar(select(OutboxEvent))

    assert stored is not None
    assert stored.status == OutboxStatus.PENDING
    assert stored.attempts == 0
    assert not stored.error_detail
    assert stored.published_at is None
