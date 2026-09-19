import asyncio
import time
from typing import Any
from uuid import uuid4

from faststream import Context
from faststream.rabbit import RabbitBroker

from hub.identity import ledger_owner_id
from hub.schemas import Envelope, Meta  # seus schemas
from hub.settings import get_settings

settings = get_settings()
broker = RabbitBroker(settings.RABBITMQ_URL)

REPLY_QUEUE = 'payment.response'

# cor_id -> queue
pending_responses: dict[str, asyncio.Queue[dict[str, Any]]] = {}

# (opcional) cor_id -> created_at (pra limpeza defensiva)
_pending_created_at: dict[str, float] = {}
_PENDING_TTL_SEC = 60.0  # só pra segurança; timeout real é do wait_for()


@broker.subscriber(REPLY_QUEUE)
async def consume_payment_responses(
    msg: dict[str, Any],
    cor_id: str | None = Context('message.correlation_id'),
) -> None:
    # msg aqui é dict do payload publicado pelo payment-service (Response.body)
    if not cor_id:
        return

    q = pending_responses.get(cor_id)
    if q is None:
        return

    # entrega a resposta
    await q.put(msg)

    # opcional: já limpa aqui para evitar acumular se ninguém
    #  der pop no finally
    pending_responses.pop(cor_id, None)
    _pending_created_at.pop(cor_id, None)


def _cleanup_stale_pending() -> None:
    """Limpeza defensiva; evita vazamento se algo der errado no fluxo."""
    now = time.time()
    stale = [
        cid
        for cid, ts in _pending_created_at.items()
        if (now - ts) > _PENDING_TTL_SEC
    ]
    for cid in stale:
        pending_responses.pop(cid, None)
        _pending_created_at.pop(cid, None)


async def request_invoice_rabbitmq(
    user_id: str,
    amount_msat: int,
    memo: str = 'deposit',
    *,
    expires_in_sec: int = 900,
    timeout_sec: float = 10.0,
) -> dict[str, Any] | None:
    return await _request_payment_command(
        command_type='payment.invoice.create',
        data={
            'user_id': str(ledger_owner_id(user_id)),
            'amount_msat': amount_msat,
            'memo': memo,
            'expires_in_sec': expires_in_sec,
        },
        timeout_sec=timeout_sec,
    )


async def request_pay_invoice_rabbitmq(
    *,
    user_id: str,
    payment_request: str,
    amount_msat: int,
    payment_reference: str,
    timeout_sec: float = 20.0,
) -> dict[str, Any] | None:
    return await _request_payment_command(
        command_type='payment.invoice.pay',
        data={
            'user_id': str(ledger_owner_id(user_id)),
            'payment_request': payment_request,
            'amount_msat': amount_msat,
            'payment_reference': payment_reference,
        },
        timeout_sec=timeout_sec,
    )


async def _request_payment_command(
    *,
    command_type: str,
    data: dict[str, Any],
    timeout_sec: float,
) -> dict[str, Any] | None:
    correlation_id = str(uuid4())
    response_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # limpeza defensiva (barata)
    _cleanup_stale_pending()

    pending_responses[correlation_id] = response_queue
    _pending_created_at[correlation_id] = time.time()

    try:
        # payload no formato Envelope(meta,data)
        cmd = Envelope(
            meta=Meta(
                type=command_type,
                correlation_id=correlation_id,
                idempotency_key=f'{command_type}:{correlation_id}',
                producer='hub',  # opcional sobrescrever
            ),
            data=data,
        )

        await broker.publish(
            cmd.model_dump(),
            queue=settings.PAYMENT_COMMANDS_QUEUE,  # ex: "payment.commands"
            reply_to=REPLY_QUEUE,
            correlation_id=correlation_id,
            priority=1,
        )

        try:
            resp = await asyncio.wait_for(
                response_queue.get(), timeout=timeout_sec
            )
            return resp
        except asyncio.TimeoutError:
            return None

    finally:
        pending_responses.pop(correlation_id, None)
        _pending_created_at.pop(correlation_id, None)
