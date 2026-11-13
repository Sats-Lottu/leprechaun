import asyncio
import logging
from uuid import UUID

from sqlalchemy import select

from hub.checkout_service import expire_unconfirmed_checkouts, resume_checkout
from hub.ledger_client import LedgerClient
from hub.models.database import session_scope
from hub.models.enums import CheckoutSessionStatus
from hub.models.tables import CheckoutSession, WalletWithdrawal
from hub.wallet_service import WalletWithdrawalError, resume_withdrawal

log = logging.getLogger(__name__)
PAYMENT_RETRY_INTERVAL = 3
PAYMENT_BATCH_SIZE = 20


async def recover_payment(
    payment_id: UUID, *, withdrawal: bool = False
) -> None:
    try:
        async with session_scope() as session:
            if withdrawal:
                await resume_withdrawal(payment_id, session, LedgerClient())
            else:
                await resume_checkout(payment_id, session)
    except WalletWithdrawalError:
        return
    except Exception:
        log.exception('payment_recovery_failed payment_id=%s', payment_id)


async def recover_payments() -> None:
    async with session_scope() as session:
        await expire_unconfirmed_checkouts(session)
    for model, statuses in (
        (
            CheckoutSession,
            (
                CheckoutSessionStatus.RESERVED,
                CheckoutSessionStatus.AWAITING_PAYMENT,
            ),
        ),
        (WalletWithdrawal, ('pending', 'awaiting_ledger')),
    ):
        after = None
        while True:
            query = (
                select(model.id)
                .where(model.status.in_(statuses))
                .order_by(model.id)
                .limit(PAYMENT_BATCH_SIZE)
            )
            if after is not None:
                query = query.where(model.id > after)
            async with session_scope() as session:
                ids = (await session.scalars(query)).all()
            if not ids:
                break
            await asyncio.gather(
                *(
                    recover_payment(
                        payment_id, withdrawal=model is WalletWithdrawal
                    )
                    for payment_id in ids
                )
            )
            after = ids[-1]


async def recover_payments_loop() -> None:
    while True:
        try:
            await recover_payments()
        except Exception:
            log.exception('payment_recovery_cycle_failed')
        await asyncio.sleep(PAYMENT_RETRY_INTERVAL)
