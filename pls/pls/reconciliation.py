import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pls.contexts import InvoicePaidContext
from pls.models import database
from pls.models.enums import (
    InvoiceStatus,
    PaymentDirection,
    ProviderEventStatus,
)
from pls.models.tables import LightningInvoice, ProviderEvent
from pls.observability import inc_counter
from pls.outbox import enqueue_outbox_event, publish_pending_outbox
from pls.provider import LNbitsWallet
from pls.schemas import Envelope, Failed, PaymentReceivedData, WalletResponse
from pls.settings import get_settings
from pls.utils import iso_to_datetime

settings = get_settings()
log = logging.getLogger(settings.SERVICE_NAME)


async def handle_lnbits_wallet_event(evt: WalletResponse) -> list[Envelope]:
    async with database.session_scope() as session:
        envelopes = await reconcile_lnbits_wallet_event(evt, session)
        for env in envelopes:
            await enqueue_outbox_event(session, env)
        await session.commit()
    log.info(
        "lnbits_wallet_event_handled checking_id=%s payment_hash=%s "
        "domain_events=%s",
        evt.payment.checking_id,
        evt.payment.payment_hash,
        len(envelopes),
    )
    inc_counter(
        "lnbits_wallet_event_handled",
        result="matched" if envelopes else "ignored",
    )
    await publish_pending_outbox()
    return []


async def mark_invoice_paid(ctx: InvoicePaidContext) -> Envelope:
    session = ctx.session
    invoice = ctx.invoice
    provider_event = ctx.provider_event
    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = ctx.paid_at
    provider_event.status = ProviderEventStatus.PROCESSED
    provider_event.processed_at = datetime.now(timezone.utc)

    event = PaymentReceivedData(
        user_id=invoice.user_id,
        invoice_id=ctx.provider_invoice_id or str(invoice.id),
        payment_hash=ctx.payment_hash or "",
        amount_msat=abs(ctx.amount_msat),
        paid_at=ctx.paid_at.isoformat(),
    )
    await session.flush()
    return Envelope(
        meta={
            "type": "payment.invoice.paid",
            "causation_id": str(provider_event.id),
        },
        data=event.model_dump(),
    )


async def reconcile_lnbits_wallet_event(
    evt: WalletResponse, session: AsyncSession
) -> list[Envelope]:
    payment = evt.payment
    provider_invoice_id = payment.checking_id
    payment_hash = payment.payment_hash
    invoice = await session.scalar(
        select(LightningInvoice).where(
            or_(
                LightningInvoice.provider_invoice_id == provider_invoice_id,
                LightningInvoice.payment_hash == payment_hash,
            )
        )
    )

    provider_event = ProviderEvent(
        event_type="lnbits.wallet.payment",
        invoice_id=invoice.id if invoice else None,
        provider_invoice_id=provider_invoice_id,
        payment_hash=payment_hash,
        payload=evt.model_dump(),
    )
    session.add(provider_event)
    log.info(
        "provider_event_received source=lnbits_ws checking_id=%s "
        "payment_hash=%s status=%s matched_invoice=%s",
        provider_invoice_id,
        payment_hash,
        payment.status,
        bool(invoice),
    )

    if invoice is None or payment.status != "success":
        if payment.status != "success":
            provider_event.status = ProviderEventStatus.FAILED
            provider_event.error_detail = (
                f"unhandled payment status: {payment.status}"
            )
        await session.flush()
        log.info(
            "provider_event_ignored source=lnbits_ws checking_id=%s "
            "payment_hash=%s status=%s matched_invoice=%s",
            provider_invoice_id,
            payment_hash,
            payment.status,
            bool(invoice),
        )
        inc_counter("provider_event_ignored", status=payment.status)
        return []

    return [
        await mark_invoice_paid(
            InvoicePaidContext(
                session=session,
                invoice=invoice,
                provider_event=provider_event,
                provider_invoice_id=provider_invoice_id,
                payment_hash=payment_hash,
                amount_msat=payment.amount,
                paid_at=iso_to_datetime(payment.time)
                or datetime.now(timezone.utc),
            )
        )
    ]


async def reconcile_pending_invoices(
    lnbits_wallet: LNbitsWallet,
    *,
    limit: int | None = None,
) -> int:
    batch_size = limit or settings.RECONCILIATION_BATCH_SIZE
    reconciled = 0

    async with database.session_scope() as session:
        invoices = (
            await session.scalars(
                select(LightningInvoice)
                .where(
                    LightningInvoice.direction == PaymentDirection.INCOMING,
                    LightningInvoice.status == InvoiceStatus.PENDING,
                    LightningInvoice.provider_invoice_id.is_not(None),
                )
                .order_by(LightningInvoice.created_at, LightningInvoice.id)
                .limit(batch_size)
            )
        ).all()
        checked_count = len(invoices)
        log.info(
            "reconciliation_batch_started pending_count=%s limit=%s",
            checked_count,
            batch_size,
        )

        for invoice in invoices:
            if invoice.provider_invoice_id is None:
                continue

            payment = await lnbits_wallet.get_payment(
                invoice.provider_invoice_id
            )
            if isinstance(payment, Failed):
                log.warning(
                    "reconciliation_provider_lookup_failed invoice_id=%s "
                    "provider_invoice_id=%s detail=%s",
                    invoice.id,
                    invoice.provider_invoice_id,
                    payment.detail,
                )
                continue

            provider_event = ProviderEvent(
                event_type="lnbits.reconciliation.payment",
                invoice_id=invoice.id,
                provider_invoice_id=invoice.provider_invoice_id,
                payment_hash=payment.payment_hash or invoice.payment_hash,
                payload=payment.model_dump(),
            )
            session.add(provider_event)

            if payment.paid is not True:
                await session.flush()
                log.info(
                    "reconciliation_invoice_still_pending invoice_id=%s "
                    "provider_invoice_id=%s payment_hash=%s",
                    invoice.id,
                    invoice.provider_invoice_id,
                    payment.payment_hash or invoice.payment_hash,
                )
                continue

            env = await mark_invoice_paid(
                InvoicePaidContext(
                    session=session,
                    invoice=invoice,
                    provider_event=provider_event,
                    provider_invoice_id=invoice.provider_invoice_id,
                    payment_hash=payment.payment_hash or invoice.payment_hash,
                    amount_msat=payment.amount or invoice.amount_msat,
                    paid_at=datetime.now(timezone.utc),
                )
            )
            await enqueue_outbox_event(session, env)
            reconciled += 1
            log.info(
                "reconciliation_invoice_paid invoice_id=%s "
                "provider_invoice_id=%s payment_hash=%s",
                invoice.id,
                invoice.provider_invoice_id,
                payment.payment_hash or invoice.payment_hash,
            )

        await session.commit()

    if reconciled:
        await publish_pending_outbox()
        inc_counter("reconciliation_batch_with_paid")

    log.info(
        "reconciliation_batch_completed checked_count=%s reconciled_count=%s",
        checked_count,
        reconciled,
    )
    return reconciled


async def reconcile_pending_invoices_loop(
    lnbits_wallet: LNbitsWallet,
) -> None:
    interval = max(0.1, settings.RECONCILIATION_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(interval)
        try:
            await reconcile_pending_invoices(lnbits_wallet)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("periodic LNbits reconciliation failed")
