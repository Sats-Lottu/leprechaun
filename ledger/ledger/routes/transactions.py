import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ledger.audit import AuditEvent, record_audit_event
from ledger.balances import (
    available_balance,
    consume_reserved,
    credit,
    debit_available,
)
from ledger.errors import ledger_error
from ledger.idempotency import (
    OperationIdempotencyLookup,
    OperationIdempotencyRecord,
    get_operation_idempotency,
    record_operation_idempotency,
)
from ledger.models.database import get_session
from ledger.models.enums import EntryType, TransactionKind, TransactionStatus
from ledger.models.tables import (
    Account,
    BalanceHold,
    LedgerEntry,
    LedgerTransaction,
)
from ledger.observability import (
    TRANSACTIONS_POSTED,
    TRANSACTIONS_REVERSED,
    log_event,
)
from ledger.payload_hash import payload_hash
from ledger.schemas import (
    TransactionCreate,
    TransactionCreated,
    TransactionDetails,
    TransactionEntryCreate,
    TransactionEntryDetails,
    TransactionOperation,
)

router = APIRouter(prefix="/transactions", tags=["transactions"])
logger = logging.getLogger(__name__)

Session = Annotated[AsyncSession, Depends(get_session)]


async def _get_transaction_or_404(
    transaction_id: UUID,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> LedgerTransaction:
    query = (
        select(LedgerTransaction)
        .where(LedgerTransaction.id == transaction_id)
        .options(selectinload(LedgerTransaction.entries))
    )
    if for_update:
        query = query.with_for_update()

    transaction = await session.scalar(query)
    if not transaction:
        raise ledger_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="transaction_not_found",
            message="Transaction not found",
        )
    return transaction


def _transaction_details(
    transaction: LedgerTransaction,
) -> TransactionDetails:
    return TransactionDetails(
        transaction_id=transaction.id,
        status=transaction.status,
        kind=transaction.kind,
        external_origin=transaction.external_origin,
        reference_type=transaction.reference_type,
        reference_id=transaction.reference_id,
        description=transaction.description,
        reversed_transaction_id=transaction.reversed_transaction_id,
        entries=[
            TransactionEntryDetails(
                account_id=entry.account_id,
                entry_type=entry.entry_type,
                amount=entry.amount,
                description=entry.description,
                reference_type=entry.reference_type,
                reference_id=entry.reference_id,
            )
            for entry in transaction.entries
        ],
    )


def _validate_entries(
    entries: list[TransactionEntryCreate], kind: TransactionKind
) -> None:
    if not entries:
        raise ledger_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="transaction_entries_required",
            message="Transaction must have at least one entry",
        )

    debit_total = 0
    credit_total = 0

    for entry in entries:
        if entry.amount <= 0:
            raise ledger_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="invalid_entry_amount",
                message="Entry amount must be greater than zero",
            )
        if entry.entry_type is EntryType.DEBIT:
            debit_total += entry.amount
        if entry.entry_type is EntryType.CREDIT:
            credit_total += entry.amount

    if kind is TransactionKind.TRANSFER and debit_total != credit_total:
        raise ledger_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="transaction_unbalanced",
            message="Transaction entries must be balanced",
        )
    if kind is TransactionKind.EXTERNAL_CREDIT and (
        debit_total or not credit_total
    ):
        raise ledger_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_external_credit_entries",
            message="External credit transactions must contain only credits",
        )
    if kind is TransactionKind.EXTERNAL_DEBIT and (
        credit_total or not debit_total
    ):
        raise ledger_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_external_debit_entries",
            message="External debit transactions must contain only debits",
        )


async def _ensure_accounts_exist(
    entries: list[TransactionEntryCreate],
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> dict[UUID, Account]:
    account_ids = {entry.account_id for entry in entries}
    if not account_ids:
        return {}

    query = (
        select(Account).where(Account.id.in_(account_ids)).order_by(Account.id)
    )
    if for_update:
        query = query.with_for_update()

    accounts = await session.scalars(query)
    accounts_by_id = {account.id: account for account in accounts}
    missing_ids = account_ids - accounts_by_id.keys()
    if missing_ids:
        raise ledger_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="account_not_found",
            message="Account not found",
        )
    return accounts_by_id


def _apply_entries_to_balances(
    entries: list[LedgerEntry], accounts_by_id: dict[UUID, Account]
) -> None:
    for entry in entries:
        account = accounts_by_id[entry.account_id]
        if entry.entry_type == EntryType.DEBIT:
            if available_balance(account) < entry.amount:
                raise ledger_error(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code="insufficient_available_balance",
                    message="Insufficient available balance",
                )
            debit_available(account, entry.amount)
        if entry.entry_type == EntryType.CREDIT:
            credit(account, entry.amount)


async def post_transaction_using_reserved_hold(
    transaction_id: UUID,
    hold: BalanceHold,
    session: AsyncSession,
) -> LedgerTransaction:
    transaction = await _get_transaction_or_404(
        transaction_id, session, for_update=True
    )
    if transaction.status != TransactionStatus.PENDING:
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="transaction_not_pending",
            message="Only pending transactions can be posted",
        )

    hold_debit_entries = [
        entry
        for entry in transaction.entries
        if entry.account_id == hold.account_id
        and entry.entry_type == EntryType.DEBIT
    ]
    if (
        len(hold_debit_entries) != 1
        or hold_debit_entries[0].amount != hold.amount
    ):
        raise ledger_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="hold_transaction_debit_mismatch",
            message="Transaction must debit the hold account"
            " for the hold amount",
        )

    account_entries = [
        TransactionEntryCreate(
            account_id=entry.account_id,
            entry_type=entry.entry_type,
            amount=entry.amount,
        )
        for entry in transaction.entries
    ]
    accounts_by_id = await _ensure_accounts_exist(
        account_entries, session, for_update=True
    )

    for entry in transaction.entries:
        account = accounts_by_id[entry.account_id]
        if entry.id == hold_debit_entries[0].id:
            consume_reserved(account, hold.amount)
            continue
        if entry.entry_type == EntryType.DEBIT:
            if available_balance(account) < entry.amount:
                raise ledger_error(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code="insufficient_available_balance",
                    message="Insufficient available balance",
                )
            debit_available(account, entry.amount)
        if entry.entry_type == EntryType.CREDIT:
            credit(account, entry.amount)

    transaction.status = TransactionStatus.POSTED
    transaction.posted_at = datetime.now(UTC)
    TRANSACTIONS_POSTED.inc()
    log_event(
        logger,
        logging.INFO,
        "transaction_posted",
        transaction_id=transaction.id,
        entry_count=len(transaction.entries),
        source="hold.consume",
    )
    record_audit_event(
        session,
        AuditEvent(
            event_type="transaction_posted",
            resource_type="transaction",
            resource_id=transaction.id,
            metadata={
                "entry_count": len(transaction.entries),
                "source": "hold.consume",
                "hold_id": str(hold.id),
            },
        ),
    )
    return transaction


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=TransactionCreated,
)
async def create_transaction(
    session: Session,
    payload: Annotated[TransactionCreate | None, Body()] = None,
):
    payload = payload or TransactionCreate()
    idempotency_payload_hash = payload_hash(payload)

    if payload.idempotency_key:
        existing_transaction = await session.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.idempotency_key == payload.idempotency_key
            )
        )
        if existing_transaction:
            existing_payload_hash = getattr(
                existing_transaction,
                "idempotency_payload_hash",
                None,
            )
            if existing_payload_hash and (
                existing_payload_hash != idempotency_payload_hash
            ):
                raise ledger_error(
                    status_code=status.HTTP_409_CONFLICT,
                    code="idempotency_payload_mismatch",
                    message=(
                        "Idempotency key was reused with a different payload"
                    ),
                )
            return TransactionCreated(
                transaction_id=existing_transaction.id,
                status=existing_transaction.status,
            )

    _validate_entries(payload.entries, payload.kind)
    if payload.kind is not TransactionKind.TRANSFER and not (
        payload.external_origin.strip()
    ):
        raise ledger_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="external_origin_required",
            message="External transactions require an external origin",
        )
    await _ensure_accounts_exist(payload.entries, session)

    transaction = LedgerTransaction(
        kind=payload.kind,
        external_origin=payload.external_origin.strip(),
        reference_type=payload.reference_type,
        reference_id=payload.reference_id,
        idempotency_key=payload.idempotency_key,
        idempotency_payload_hash=idempotency_payload_hash,
        description=payload.description,
    )
    session.add(transaction)
    await session.flush()

    for entry in payload.entries:
        session.add(
            LedgerEntry(
                transaction_id=transaction.id,
                account_id=entry.account_id,
                entry_type=entry.entry_type,
                amount=entry.amount,
                description=entry.description,
                reference_type=entry.reference_type,
                reference_id=entry.reference_id,
            )
        )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if payload.idempotency_key:
            existing_transaction = await session.scalar(
                select(LedgerTransaction).where(
                    LedgerTransaction.idempotency_key
                    == payload.idempotency_key
                )
            )
            if existing_transaction:
                existing_payload_hash = getattr(
                    existing_transaction,
                    "idempotency_payload_hash",
                    None,
                )
                if existing_payload_hash and (
                    existing_payload_hash != idempotency_payload_hash
                ):
                    raise ledger_error(
                        status_code=status.HTTP_409_CONFLICT,
                        code="idempotency_payload_mismatch",
                        message=(
                            "Idempotency key was reused with a different "
                            "payload"
                        ),
                    ) from exc
                return TransactionCreated(
                    transaction_id=existing_transaction.id,
                    status=existing_transaction.status,
                )
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="transaction_idempotency_conflict",
            message="Transaction idempotency key already exists",
        ) from exc

    await session.refresh(transaction)
    record_audit_event(
        session,
        AuditEvent(
            event_type="transaction_created",
            resource_type="transaction",
            resource_id=transaction.id,
            idempotency_key=payload.idempotency_key,
            metadata={
                "entry_count": len(payload.entries),
                "kind": transaction.kind,
                "external_origin": transaction.external_origin,
                "reference_type": transaction.reference_type,
                "reference_id": str(transaction.reference_id)
                if transaction.reference_id
                else None,
            },
        ),
    )
    await session.commit()
    log_event(
        logger,
        logging.INFO,
        "transaction_created",
        transaction_id=transaction.id,
        entry_count=len(payload.entries),
        reference_type=transaction.reference_type,
        reference_id=transaction.reference_id,
    )
    return TransactionCreated(
        transaction_id=transaction.id,
        status=transaction.status,
    )


@router.get("/{transaction_id}", response_model=TransactionDetails)
async def get_transaction(transaction_id: UUID, session: Session):
    transaction = await _get_transaction_or_404(transaction_id, session)
    return _transaction_details(transaction)


@router.post("/{transaction_id}/post", response_model=TransactionDetails)
async def post_transaction(
    transaction_id: UUID,
    session: Session,
    payload: Annotated[TransactionOperation | None, Body()] = None,
):
    payload = payload or TransactionOperation()
    operation_payload_hash = payload_hash(payload)
    existing_operation = await get_operation_idempotency(
        session,
        OperationIdempotencyLookup(
            operation="transaction.post",
            resource_id=transaction_id,
            idempotency_key=payload.idempotency_key,
            payload_hash=operation_payload_hash,
        ),
        for_update=True,
    )
    if existing_operation:
        transaction = await _get_transaction_or_404(transaction_id, session)
        return _transaction_details(transaction)

    transaction = await _get_transaction_or_404(
        transaction_id, session, for_update=True
    )

    if transaction.status != TransactionStatus.PENDING:
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="transaction_not_pending",
            message="Only pending transactions can be posted",
        )

    account_entries = [
        TransactionEntryCreate(
            account_id=entry.account_id,
            entry_type=entry.entry_type,
            amount=entry.amount,
        )
        for entry in transaction.entries
    ]
    accounts_by_id = await _ensure_accounts_exist(
        account_entries, session, for_update=True
    )
    _apply_entries_to_balances(transaction.entries, accounts_by_id)

    transaction.status = TransactionStatus.POSTED
    transaction.posted_at = datetime.now(UTC)
    record_operation_idempotency(
        session,
        OperationIdempotencyRecord(
            operation="transaction.post",
            resource_id=transaction.id,
            idempotency_key=payload.idempotency_key,
            result_status=TransactionStatus.POSTED,
            payload_hash=operation_payload_hash,
        ),
    )
    record_audit_event(
        session,
        AuditEvent(
            event_type="transaction_posted",
            resource_type="transaction",
            resource_id=transaction.id,
            idempotency_key=payload.idempotency_key,
            metadata={"entry_count": len(transaction.entries)},
        ),
    )
    await session.commit()
    TRANSACTIONS_POSTED.inc()
    log_event(
        logger,
        logging.INFO,
        "transaction_posted",
        transaction_id=transaction.id,
        entry_count=len(transaction.entries),
    )
    await session.refresh(transaction, attribute_names=["entries"])
    return _transaction_details(transaction)


def _reverse_entry_type(entry_type: EntryType) -> EntryType:
    if entry_type == EntryType.DEBIT:
        return EntryType.CREDIT
    return EntryType.DEBIT


async def _create_reversal_transaction(
    transaction: LedgerTransaction, session: AsyncSession
) -> LedgerTransaction:
    reversal_kind = {
        TransactionKind.EXTERNAL_CREDIT: TransactionKind.EXTERNAL_DEBIT,
        TransactionKind.EXTERNAL_DEBIT: TransactionKind.EXTERNAL_CREDIT,
    }.get(TransactionKind(transaction.kind), TransactionKind.TRANSFER)
    reversal = LedgerTransaction(
        status=TransactionStatus.POSTED,
        kind=reversal_kind,
        external_origin=transaction.external_origin,
        reference_type=transaction.reference_type,
        reference_id=transaction.reference_id,
        description=f"Reversal of transaction {transaction.id}",
        posted_at=datetime.now(UTC),
    )
    reversal.reversed_transaction = transaction
    session.add(reversal)
    await session.flush()

    for entry in transaction.entries:
        session.add(
            LedgerEntry(
                transaction_id=reversal.id,
                account_id=entry.account_id,
                entry_type=_reverse_entry_type(entry.entry_type),
                amount=entry.amount,
                description=f"Reversal of entry {entry.id}",
                reference_type=entry.reference_type,
                reference_id=entry.reference_id,
            )
        )
    await session.flush()
    await session.refresh(reversal, attribute_names=["entries"])
    return reversal


@router.post("/{transaction_id}/reverse", response_model=TransactionDetails)
async def reverse_transaction(
    transaction_id: UUID,
    session: Session,
    payload: Annotated[TransactionOperation | None, Body()] = None,
):
    payload = payload or TransactionOperation()
    operation_payload_hash = payload_hash(payload)
    existing_operation = await get_operation_idempotency(
        session,
        OperationIdempotencyLookup(
            operation="transaction.reverse",
            resource_id=transaction_id,
            idempotency_key=payload.idempotency_key,
            payload_hash=operation_payload_hash,
        ),
        for_update=True,
    )
    if existing_operation:
        transaction = await _get_transaction_or_404(transaction_id, session)
        return _transaction_details(transaction)

    transaction = await _get_transaction_or_404(
        transaction_id, session, for_update=True
    )

    if transaction.status != TransactionStatus.POSTED:
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="transaction_not_posted",
            message="Only posted transactions can be reversed",
        )

    existing_reversal = await session.scalar(
        select(LedgerTransaction).where(
            LedgerTransaction.reversed_transaction_id == transaction.id
        )
    )
    if existing_reversal:
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="transaction_already_reversed",
            message="Transaction already reversed",
        )

    try:
        reversal = await _create_reversal_transaction(transaction, session)
    except IntegrityError as exc:
        await session.rollback()
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="transaction_already_reversed",
            message="Transaction already reversed",
        ) from exc
    accounts_by_id = await _ensure_accounts_exist(
        [
            TransactionEntryCreate(
                account_id=entry.account_id,
                entry_type=entry.entry_type,
                amount=entry.amount,
            )
            for entry in reversal.entries
        ],
        session,
        for_update=True,
    )
    _apply_entries_to_balances(reversal.entries, accounts_by_id)

    transaction.status = TransactionStatus.REVERSED
    record_operation_idempotency(
        session,
        OperationIdempotencyRecord(
            operation="transaction.reverse",
            resource_id=transaction.id,
            idempotency_key=payload.idempotency_key,
            result_status=TransactionStatus.REVERSED,
            payload_hash=operation_payload_hash,
        ),
    )
    record_audit_event(
        session,
        AuditEvent(
            event_type="transaction_reversed",
            resource_type="transaction",
            resource_id=transaction.id,
            idempotency_key=payload.idempotency_key,
            metadata={"reversal_transaction_id": str(reversal.id)},
        ),
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="transaction_already_reversed",
            message="Transaction already reversed",
        ) from exc

    TRANSACTIONS_REVERSED.inc()
    log_event(
        logger,
        logging.INFO,
        "transaction_reversed",
        transaction_id=transaction.id,
        reversal_transaction_id=reversal.id,
    )
    await session.refresh(transaction, attribute_names=["entries"])
    return _transaction_details(transaction)
