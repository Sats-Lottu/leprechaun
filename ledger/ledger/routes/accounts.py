import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.audit import AuditEvent, record_audit_event
from ledger.models.database import get_session
from ledger.models.enums import AccountType
from ledger.models.tables import Account, LedgerEntry
from ledger.observability import log_event
from ledger.schemas import (
    AccountBalance,
    AccountCreated,
    AccountStatement,
    TransactionEntryDetails,
)

router = APIRouter(prefix='/accounts', tags=['accounts'])
logger = logging.getLogger(__name__)

Session = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    '',
    status_code=status.HTTP_201_CREATED,
    response_model=AccountCreated,
)
async def create_account(
    session: Session,
    account_type: AccountType = AccountType.USER,
    owner_id: UUID | None = None,
    name: str | None = None,
):
    if account_type is AccountType.USER and not owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='owner_id is required when account_type is provided',
        )
    account = Account(
        account_type=account_type,
        owner_id=owner_id,
        name=name,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    record_audit_event(
        session,
        AuditEvent(
            event_type='account_created',
            resource_type='account',
            resource_id=account.id,
            metadata={
                'account_type': str(account.account_type),
                'owner_id': (
                    str(account.owner_id) if account.owner_id else None
                ),
            },
        ),
    )
    await session.commit()
    log_event(
        logger,
        logging.INFO,
        'account_created',
        account_id=account.id,
        account_type=account.account_type,
        owner_id=account.owner_id,
    )
    return AccountCreated(
        account_id=account.id,
        account_type=account.account_type,
        owner_id=account.owner_id,
        name=account.name,
    )


@router.get('/{account_id}/balance', response_model=AccountBalance)
async def get_account_balance(account_id: UUID, session: Session):
    account = await session.scalar(
        select(Account).where(Account.id == account_id)
    )
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Account not found'
        )
    return AccountBalance(
        balance=account.balance, reserved_balance=account.reserved_balance
    )


@router.get('/{account_id}/statement', response_model=AccountStatement)
async def get_account_statement(account_id: UUID, session: Session):
    entries = await session.scalars(
        select(LedgerEntry).where(LedgerEntry.account_id == account_id)
    )
    return AccountStatement(
        entries=[
            TransactionEntryDetails.model_validate(
                entry, from_attributes=True
            ).model_dump(mode='json')
            for entry in entries
        ]
    )
