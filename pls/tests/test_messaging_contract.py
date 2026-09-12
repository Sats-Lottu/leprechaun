import asyncio

import pytest
from faststream import FastStream
from faststream.rabbit import RabbitBroker, TestRabbitBroker

from pls import commands, messaging
from pls.contexts import CommandContext
from pls.schemas import Envelope, Meta, Payment


@pytest.mark.asyncio
async def test_invoice_create_command_publishes_invoice_created_event(
    monkeypatch,
):
    broker = RabbitBroker()
    app = FastStream(broker)
    published_event = asyncio.Event()
    events = []

    class Wallet:
        @staticmethod
        async def create_invoice(_invoice):
            return Payment(checking_id='chk-1', payment_request='lnbc1')

    @broker.subscriber(messaging.settings.PAYMENT_COMMANDS_QUEUE)
    async def command_handler(env: Envelope) -> None:
        await commands.handle_create_invoice(
            CommandContext(
                env=env,
                lnbits_wallet=Wallet(),
                reply_to=None,
                correlation_id=env.meta.correlation_id,
            )
        )

    @broker.subscriber(messaging.settings.PAYMENT_EVENTS_QUEUE)
    async def event_handler(env: Envelope) -> None:
        events.append(env)
        published_event.set()

    async def publish_event(_broker, env):
        await broker.publish(
            env.model_dump(),
            queue=messaging.settings.PAYMENT_EVENTS_QUEUE,
        )

    monkeypatch.setattr(commands, 'publish_event', publish_event)

    async with TestRabbitBroker(app.broker):
        await broker.publish(
            Envelope(
                meta=Meta(
                    type='payment.invoice.create',
                    correlation_id='corr-1',
                    idempotency_key='idem-1',
                ),
                data={'user_id': 'user-1', 'amount_msat': 1000},
            ).model_dump(),
            queue=messaging.settings.PAYMENT_COMMANDS_QUEUE,
        )
        await asyncio.wait_for(published_event.wait(), timeout=1)

    assert events[0].meta.type == 'payment.invoice.created'
    assert events[0].meta.correlation_id == 'corr-1'
    assert events[0].data == {
        'user_id': 'user-1',
        'invoice_id': 'chk-1',
        'payment_request': 'lnbc1',
        'amount_msat': 1000,
        'expires_at': events[0].data['expires_at'],
    }
