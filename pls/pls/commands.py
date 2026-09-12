import logging
from datetime import datetime, timezone
from uuid import uuid4

from faststream import Context, Response
from pydantic import ValidationError

from pls.contexts import CommandContext, PaymentSentContext
from pls.idempotency import (
    finish_operation,
    operation_is_duplicate,
    start_operation,
)
from pls.messaging import broker, publish_failed_command
from pls.models import database
from pls.models.enums import InvoiceStatus, OperationStatus, PaymentDirection
from pls.models.tables import LightningInvoice
from pls.observability import inc_counter
from pls.outbox import (
    enqueue_outbox_event,
    publish_event,
    publish_pending_outbox,
)
from pls.provider import LNbitsWallet
from pls.schemas import (
    CreateInvoice,
    CreateInvoiceData,
    Envelope,
    Failed,
    InvoiceCreatedData,
    PayInvoiceData,
    PayLNURLData,
    PaymentSentData,
)
from pls.settings import get_settings
from pls.utils import (
    expires_at_iso,
    iso_to_datetime,
    msat_to_sat_floor,
    now_utc_iso,
    retry_provider_call,
)

settings = get_settings()
log = logging.getLogger(settings.SERVICE_NAME)


@broker.subscriber(settings.PAYMENT_COMMANDS_QUEUE)
async def handle_payment_commands(
    env: Envelope,
    lnbits_wallet: LNbitsWallet = Context("lnbits_wallet"),  # noqa: B008
    reply_to: str | None = Context("message.reply_to"),
    cor_id: str | None = Context("message.correlation_id"),
):

    msg_type = env.meta.type
    correlation_id = cor_id or env.meta.correlation_id
    idempotency_key = env.meta.idempotency_key
    log.info(
        "payment_command_received type=%s correlation_id=%s "
        "idempotency_key=%s",
        msg_type,
        correlation_id,
        idempotency_key,
    )
    inc_counter("payment_command_received", command_type=msg_type)

    async with database.session_scope() as session:
        idempotency = await start_operation(
            session,
            operation=msg_type,
            idempotency_key=idempotency_key,
            payload=env.data,
        )
        if operation_is_duplicate(idempotency):
            log.info(
                "payment_command_duplicate type=%s correlation_id=%s "
                "idempotency_key=%s",
                msg_type,
                correlation_id,
                idempotency_key,
            )
            inc_counter("payment_command_duplicate", command_type=msg_type)
            return

        ctx = CommandContext(
            env=env,
            lnbits_wallet=lnbits_wallet,
            reply_to=reply_to,
            correlation_id=correlation_id,
            session=session,
            idempotency=idempotency,
        )

        match msg_type:
            case "payment.invoice.create":
                resp = await handle_create_invoice(ctx)
            case "payment.invoice.pay":
                resp = await handle_pay_invoice(ctx)
            case "payment.lnurl.pay":
                resp = await handle_pay_lnurl(ctx)
            case _:
                log.info(
                    "payment_command_unknown type=%s correlation_id=%s "
                    "idempotency_key=%s action=dlq",
                    msg_type,
                    correlation_id,
                    idempotency_key,
                )
                await publish_failed_command(
                    broker,
                    env,
                    reason=f"unknown message type: {msg_type}",
                    correlation_id=correlation_id,
                )
                await finish_operation(
                    session, idempotency, status=OperationStatus.FAILED
                )
                await session.commit()
                inc_counter("payment_command_failed", command_type=msg_type)
                return

        await session.commit()
        await publish_pending_outbox()
        log.info(
            "payment_command_completed type=%s correlation_id=%s "
            "idempotency_key=%s",
            msg_type,
            correlation_id,
            idempotency_key,
        )
        inc_counter("payment_command_completed", command_type=msg_type)
        return resp


async def handle_create_invoice(ctx: CommandContext) -> Response | None:
    env = ctx.env
    session = ctx.session
    idempotency = ctx.idempotency

    try:
        cmd = CreateInvoiceData(**env.data)
    except ValidationError as e:
        log.warning(
            "payment_command_invalid_payload type=%s correlation_id=%s "
            "error=%s data=%r",
            env.meta.type,
            ctx.correlation_id,
            e,
            env.data,
        )
        if session is not None:
            await publish_failed_command(
                broker,
                env,
                reason="invalid create_invoice payload",
                correlation_id=ctx.correlation_id,
            )
            await finish_operation(
                session, idempotency, status=OperationStatus.FAILED
            )
        return None

    amount_sat = msat_to_sat_floor(cmd.amount_msat)
    if amount_sat <= 0:
        log.warning(
            "payment_command_invalid_amount type=%s correlation_id=%s "
            "amount_msat=%s",
            env.meta.type,
            ctx.correlation_id,
            cmd.amount_msat,
        )
        if session is not None:
            await publish_failed_command(
                broker,
                env,
                reason="amount_msat below one satoshi",
                correlation_id=ctx.correlation_id,
            )
            await finish_operation(
                session, idempotency, status=OperationStatus.FAILED
            )
        return None

    inv = await retry_provider_call(
        ctx.lnbits_wallet.create_invoice,
        CreateInvoice(
            amount=amount_sat,
            memo=cmd.memo,
            expiry=cmd.expires_in_sec,
            out=False,
        ),
    )
    if isinstance(inv, Failed):
        log.warning(
            "provider_create_invoice_failed correlation_id=%s "
            "idempotency_key=%s detail=%s",
            ctx.correlation_id,
            env.meta.idempotency_key,
            inv.detail,
        )
        if session is not None:
            await publish_failed_command(
                broker,
                env,
                reason=inv.detail,
                correlation_id=ctx.correlation_id,
            )
            await finish_operation(
                session, idempotency, status=OperationStatus.FAILED
            )
        return None

    payment_request = inv.bolt11_str
    invoice_id = (
        inv.checking_id or inv.payment_hash or f"inv_{uuid4().hex[:10]}"
    )
    expires_at = expires_at_iso(cmd.expires_in_sec)

    event = InvoiceCreatedData(
        user_id=cmd.user_id,
        invoice_id=invoice_id,
        payment_request=payment_request,
        amount_msat=cmd.amount_msat,
        expires_at=expires_at,
    )

    if session is not None:
        invoice_row = LightningInvoice(
            user_id=cmd.user_id,
            amount_msat=cmd.amount_msat,
            memo=cmd.memo,
            provider_invoice_id=invoice_id,
            payment_hash=inv.payment_hash,
            payment_request=payment_request,
            idempotency_key=env.meta.idempotency_key,
            expires_at=iso_to_datetime(expires_at),
            raw_response=inv.model_dump(),
        )
        session.add(invoice_row)
        await session.flush()
        await finish_operation(
            session,
            idempotency,
            status=OperationStatus.SUCCEEDED,
            resource_id=invoice_row.id,
        )
        log.info(
            "invoice_created_persisted correlation_id=%s "
            "idempotency_key=%s invoice_id=%s provider_invoice_id=%s "
            "payment_hash=%s amount_msat=%s",
            ctx.correlation_id,
            env.meta.idempotency_key,
            invoice_row.id,
            invoice_id,
            inv.payment_hash,
            cmd.amount_msat,
        )

    out_env = Envelope(
        meta=env.meta.model_copy(
            update={
                "type": "payment.invoice.created",
                "correlation_id": ctx.correlation_id,
                "causation_id": env.meta.event_id,
            }
        ),
        data=event.model_dump(),
    )
    if session is not None:
        await enqueue_outbox_event(session, out_env)
    else:
        await publish_event(broker, out_env)

    if ctx.reply_to:
        return Response(
            body=event.model_dump(), correlation_id=ctx.correlation_id
        )

    return None


async def publish_payment_sent(ctx: PaymentSentContext) -> Response | None:
    command = ctx.command
    event = PaymentSentData(
        user_id=ctx.user_id,
        payment_hash=ctx.payment.payment_hash,
        checking_id=ctx.payment.checking_id,
        amount_msat=ctx.amount_msat,
        paid_at=now_utc_iso(),
    )
    out_env = Envelope(
        meta=command.env.meta.model_copy(
            update={
                "type": "payment.sent",
                "correlation_id": command.correlation_id,
                "causation_id": command.env.meta.event_id,
            }
        ),
        data=event.model_dump(),
    )
    if command.session is not None:
        await enqueue_outbox_event(command.session, out_env)
    else:
        await publish_event(broker, out_env)
    log.info(
        "payment_sent_event_ready type=%s correlation_id=%s "
        "payment_hash=%s checking_id=%s amount_msat=%s",
        out_env.meta.type,
        command.correlation_id,
        ctx.payment.payment_hash,
        ctx.payment.checking_id,
        ctx.amount_msat,
    )
    if command.reply_to:
        return Response(
            body=event.model_dump(), correlation_id=command.correlation_id
        )
    return None


async def handle_pay_invoice(ctx: CommandContext) -> Response | None:
    env = ctx.env
    session = ctx.session
    idempotency = ctx.idempotency
    try:
        cmd = PayInvoiceData(**env.data)
    except ValidationError:
        log.warning(
            "payment_command_invalid_payload type=%s correlation_id=%s",
            env.meta.type,
            ctx.correlation_id,
        )
        if session is not None:
            await publish_failed_command(
                broker,
                env,
                reason="invalid pay_invoice payload",
                correlation_id=ctx.correlation_id,
            )
            await finish_operation(
                session, idempotency, status=OperationStatus.FAILED
            )
        return None

    payment = await retry_provider_call(
        ctx.lnbits_wallet.pay_invoice, cmd.payment_request
    )
    if isinstance(payment, Failed):
        log.warning(
            "provider_pay_invoice_failed correlation_id=%s "
            "idempotency_key=%s detail=%s",
            ctx.correlation_id,
            env.meta.idempotency_key,
            payment.detail,
        )
        if session is not None:
            await publish_failed_command(
                broker,
                env,
                reason=payment.detail,
                correlation_id=ctx.correlation_id,
            )
            await finish_operation(
                session, idempotency, status=OperationStatus.FAILED
            )
        return None

    if session is not None:
        invoice = LightningInvoice(
            user_id=cmd.user_id or "",
            amount_msat=cmd.amount_msat or 1,
            direction=PaymentDirection.OUTGOING,
            status=InvoiceStatus.PAID
            if payment.paid
            else InvoiceStatus.PENDING,
            provider_invoice_id=payment.best_invoice_id(),
            payment_hash=payment.payment_hash,
            payment_request=cmd.payment_request,
            idempotency_key=env.meta.idempotency_key,
            paid_at=datetime.now(timezone.utc) if payment.paid else None,
            raw_response=payment.model_dump(),
        )
        session.add(invoice)
        await session.flush()
        await finish_operation(
            session,
            idempotency,
            status=OperationStatus.SUCCEEDED,
            resource_id=invoice.id,
        )
        log.info(
            "outgoing_payment_persisted command_type=%s correlation_id=%s "
            "idempotency_key=%s invoice_id=%s provider_invoice_id=%s "
            "payment_hash=%s status=%s",
            env.meta.type,
            ctx.correlation_id,
            env.meta.idempotency_key,
            invoice.id,
            invoice.provider_invoice_id,
            invoice.payment_hash,
            invoice.status,
        )

    return await publish_payment_sent(
        PaymentSentContext(
            command=ctx,
            payment=payment,
            user_id=cmd.user_id,
            amount_msat=cmd.amount_msat,
        )
    )


async def handle_pay_lnurl(ctx: CommandContext) -> Response | None:
    env = ctx.env
    session = ctx.session
    idempotency = ctx.idempotency
    try:
        cmd = PayLNURLData(**env.data)
    except ValidationError:
        log.warning(
            "payment_command_invalid_payload type=%s correlation_id=%s",
            env.meta.type,
            ctx.correlation_id,
        )
        if session is not None:
            await publish_failed_command(
                broker,
                env,
                reason="invalid pay_lnurl payload",
                correlation_id=ctx.correlation_id,
            )
            await finish_operation(
                session, idempotency, status=OperationStatus.FAILED
            )
        return None

    amount_sat = msat_to_sat_floor(cmd.amount_msat)
    if amount_sat <= 0:
        log.warning(
            "payment_command_invalid_amount type=%s correlation_id=%s "
            "amount_msat=%s",
            env.meta.type,
            ctx.correlation_id,
            cmd.amount_msat,
        )
        if session is not None:
            await publish_failed_command(
                broker,
                env,
                reason="amount_msat below one satoshi",
                correlation_id=ctx.correlation_id,
            )
            await finish_operation(
                session, idempotency, status=OperationStatus.FAILED
            )
        return None

    payment = await retry_provider_call(
        ctx.lnbits_wallet.pay_lnurl, cmd.lnurl, amount_sat, cmd.comment
    )
    if isinstance(payment, Failed):
        log.warning(
            "provider_pay_lnurl_failed correlation_id=%s "
            "idempotency_key=%s detail=%s",
            ctx.correlation_id,
            env.meta.idempotency_key,
            payment.detail,
        )
        if session is not None:
            await publish_failed_command(
                broker,
                env,
                reason=payment.detail,
                correlation_id=ctx.correlation_id,
            )
            await finish_operation(
                session, idempotency, status=OperationStatus.FAILED
            )
        return None

    if session is not None:
        invoice = LightningInvoice(
            user_id=cmd.user_id or "",
            amount_msat=cmd.amount_msat,
            direction=PaymentDirection.OUTGOING,
            status=InvoiceStatus.PAID
            if payment.paid
            else InvoiceStatus.PENDING,
            provider_invoice_id=payment.best_invoice_id(),
            payment_hash=payment.payment_hash,
            payment_request=payment.best_payment_request(),
            idempotency_key=env.meta.idempotency_key,
            paid_at=datetime.now(timezone.utc) if payment.paid else None,
            raw_response=payment.model_dump(),
        )
        session.add(invoice)
        await session.flush()
        await finish_operation(
            session,
            idempotency,
            status=OperationStatus.SUCCEEDED,
            resource_id=invoice.id,
        )
        log.info(
            "outgoing_payment_persisted command_type=%s correlation_id=%s "
            "idempotency_key=%s invoice_id=%s provider_invoice_id=%s "
            "payment_hash=%s status=%s",
            env.meta.type,
            ctx.correlation_id,
            env.meta.idempotency_key,
            invoice.id,
            invoice.provider_invoice_id,
            invoice.payment_hash,
            invoice.status,
        )

    return await publish_payment_sent(
        PaymentSentContext(
            command=ctx,
            payment=payment,
            user_id=cmd.user_id,
            amount_msat=cmd.amount_msat,
        )
    )
