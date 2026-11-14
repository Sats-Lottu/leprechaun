from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hub.checkout_service import get_or_create_user_ledger_account
from hub.ledger_client import (
    LedgerClient,
    LedgerClientError,
    LedgerHoldCreate,
    LedgerHoldDetails,
    LedgerStatementEntry,
)
from hub.models.enums import CheckoutSessionStatus
from hub.models.tables import CheckoutSession, WalletWithdrawal
from hub.rabbitmq import request_invoice_rabbitmq, request_pay_invoice_rabbitmq
from hub.schemas import MSATS_PER_SAT

MAX_PAYMENT_REQUEST_LENGTH = 4096


@dataclass(frozen=True)
class WalletSnapshot:
    account_id: UUID
    available_balance_msat: int
    reserved_balance_msat: int
    total_balance_msat: int
    open_invoice_count: int
    active_holds: tuple[LedgerHoldDetails, ...]
    statement_entries: tuple[LedgerStatementEntry, ...]
    recent_checkouts: tuple[CheckoutSession, ...]


@dataclass(frozen=True)
class WalletWithdrawalResult:
    hold_id: UUID
    payment_reference: UUID
    amount_msat: int
    payment_hash: str
    checking_id: str
    account_id: UUID


class WalletWithdrawalError(RuntimeError):
    pass


async def load_wallet_snapshot(
    *,
    user_sub: str,
    session,
    ledger: LedgerClient,
) -> WalletSnapshot:
    account = await get_or_create_user_ledger_account(
        user_sub=user_sub,
        session=session,
        ledger=ledger,
    )
    balance = await ledger.get_balance(account.ledger_account_id)
    holds = await ledger.list_holds(
        account.ledger_account_id, hold_status='active'
    )
    statement_entries = await ledger.get_statement(
        account.ledger_account_id, limit=10
    )
    recent_checkouts = tuple(
        await session.scalars(
            select(CheckoutSession)
            .where(CheckoutSession.user_id == user_sub)
            .order_by(CheckoutSession.created_at.desc())
            .limit(5)
        )
    )
    open_invoice_count = int(
        await session.scalar(
            select(func.count(CheckoutSession.id)).where(
                CheckoutSession.user_id == user_sub,
                CheckoutSession.invoice_id.is_not(None),
                (
                    CheckoutSession.status
                    == CheckoutSessionStatus.AWAITING_PAYMENT
                ),
            )
        )
        or 0
    )
    return WalletSnapshot(
        account_id=account.ledger_account_id,
        available_balance_msat=balance.available_balance_msat,
        reserved_balance_msat=balance.reserved_balance_msat,
        total_balance_msat=balance.balance_msat,
        open_invoice_count=open_invoice_count,
        active_holds=tuple(holds),
        statement_entries=tuple(statement_entries),
        recent_checkouts=recent_checkouts,
    )


async def request_deposit_invoice(
    *,
    user_sub: str,
    amount_sats: int,
    memo: str = '',
    invoice_requester=request_invoice_rabbitmq,
) -> dict[str, str] | None:
    if amount_sats <= 0:
        raise ValueError('amount_sats must be positive')
    response = await invoice_requester(
        user_id=user_sub,
        amount_msat=amount_sats * MSATS_PER_SAT,
        memo=memo.strip() or 'deposit',
    )
    if not response:
        return None
    return {
        'invoice_id': str(response.get('invoice_id') or ''),
        'payment_request': str(response.get('payment_request') or ''),
        'expires_at': str(response.get('expires_at') or ''),
    }


async def withdraw_wallet_balance(  # noqa: PLR0913
    *,
    user_sub: str,
    amount_sats: int,
    payment_request: str,
    ledger: LedgerClient,
    session: AsyncSession,
    payout_requester=request_pay_invoice_rabbitmq,
) -> WalletWithdrawalResult | None:
    if amount_sats <= 0:
        raise ValueError('amount_sats must be positive')
    payment_request = (
        payment_request.strip().lower().removeprefix('lightning:')
    )
    if (
        not payment_request
        or len(payment_request) > MAX_PAYMENT_REQUEST_LENGTH
    ):
        raise ValueError('Invalid Lightning invoice')

    digest = sha256(payment_request.encode()).hexdigest()
    amount_msat = amount_sats * MSATS_PER_SAT
    lookup = select(WalletWithdrawal).where(
        WalletWithdrawal.user_sub == user_sub,
        WalletWithdrawal.invoice_digest == digest,
    )
    intent = await session.scalar(lookup)
    if intent is None:
        account = await get_or_create_user_ledger_account(
            user_sub=user_sub,
            session=session,
            ledger=ledger,
        )
        if (
            await ledger.get_balance(account.ledger_account_id)
        ).available_balance_msat < amount_msat:
            raise WalletWithdrawalError('insufficient available balance')
        intent = WalletWithdrawal(
            user_sub=user_sub,
            invoice_digest=digest,
            payment_request=payment_request,
            amount_msat=amount_msat,
            account_id=account.ledger_account_id,
        )
        insert = (
            pg_insert
            if session.bind.dialect.name == 'postgresql'
            else sqlite_insert
        )
        await session.execute(
            insert(WalletWithdrawal)
            .values(
                id=intent.id,
                user_sub=user_sub,
                invoice_digest=digest,
                payment_request=payment_request,
                amount_msat=amount_msat,
                account_id=account.ledger_account_id,
                status=intent.status,
                payment_hash='',
                checking_id='',
            )
            .on_conflict_do_nothing(
                index_elements=['user_sub', 'invoice_digest']
            )
        )
        await session.commit()
        intent = (await session.scalars(lookup)).one()

    if intent.amount_msat != amount_msat:
        raise WalletWithdrawalError(
            'invoice already submitted with a different amount'
        )
    return await resume_withdrawal(
        intent.id, session, ledger, payout_requester
    )


async def resume_withdrawal(
    reference: UUID,
    session: AsyncSession,
    ledger: LedgerClient,
    payout_requester=request_pay_invoice_rabbitmq,
) -> WalletWithdrawalResult | None:
    intent = await session.scalar(
        select(WalletWithdrawal)
        .where(WalletWithdrawal.id == reference)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if intent is None:
        return None
    if intent.status == 'pending':
        await advance_withdrawal(intent, session, ledger, payout_requester)
    elif intent.status == 'awaiting_ledger':
        if await ledger.withdrawal_status(intent.id) == 'paid':
            intent.status = 'paid'
            await session.commit()

    if intent.status == 'failed':
        raise WalletWithdrawalError('Lightning payment failed')
    if intent.status != 'paid':
        return None
    return WalletWithdrawalResult(
        hold_id=intent.hold_id,
        payment_reference=intent.id,
        amount_msat=intent.amount_msat,
        payment_hash=intent.payment_hash,
        checking_id=intent.checking_id,
        account_id=intent.account_id,
    )


async def advance_withdrawal(
    intent: WalletWithdrawal,
    session: AsyncSession,
    ledger: LedgerClient,
    payout_requester,
) -> None:
    if intent.hold_id is None:
        await reserve_withdrawal(intent, session, ledger)
    if await ledger.withdrawal_status(intent.id) == 'paid':
        intent.status = 'paid'
    else:
        payout = await payout_requester(
            user_id=intent.user_sub,
            payment_request=intent.payment_request,
            amount_msat=intent.amount_msat,
            payment_reference=str(intent.id),
        )
        if payout and payout.get('status') == 'failed':
            await reject_withdrawal(intent, ledger)
        elif payout:
            intent.payment_hash = str(payout.get('payment_hash') or '')
            intent.checking_id = str(payout.get('checking_id') or '')
            if await ledger.withdrawal_status(intent.id) == 'paid':
                intent.status = 'paid'
            elif payout.get('status') == 'paid':
                intent.status = 'awaiting_ledger'
    await session.commit()


async def reserve_withdrawal(
    intent: WalletWithdrawal, session: AsyncSession, ledger: LedgerClient
) -> None:
    try:
        hold = await ledger.create_hold(
            LedgerHoldCreate(
                account_id=intent.account_id,
                amount_msat=intent.amount_msat,
                reason='wallet withdrawal',
                reference_type='wallet_withdrawal',
                reference_id=intent.id,
                idempotency_key=f'wallet-withdrawal:{intent.id}:hold',
                expires_at=None,
            )
        )
    except LedgerClientError as exc:
        if exc.code != 'insufficient_available_balance':
            raise
        intent.status = 'failed'
        await session.commit()
        raise WalletWithdrawalError('insufficient available balance') from exc
    intent.hold_id = hold.hold_id


async def reject_withdrawal(
    intent: WalletWithdrawal, ledger: LedgerClient
) -> None:
    await ledger.release_hold(
        intent.hold_id,
        idempotency_key=f'wallet-withdrawal:{intent.id}:release',
    )
    intent.status = 'failed'
