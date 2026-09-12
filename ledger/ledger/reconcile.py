import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.database import engine
from ledger.models.enums import EntryType, HoldStatus, TransactionStatus
from ledger.models.tables import (
    Account,
    BalanceHold,
    LedgerEntry,
    LedgerTransaction,
)


@dataclass
class ReconciliationIssue:
    code: str
    message: str
    resource_type: str
    resource_id: UUID | None = None


@dataclass
class ReconciliationReport:
    issues: list[ReconciliationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


async def reconcile_session(session: AsyncSession) -> ReconciliationReport:
    report = ReconciliationReport()

    await check_account_balances(session, report)
    await _check_reserved_balances(session, report)
    await _check_posted_transactions_balance(session, report)
    await _check_state_timestamps(session, report)

    return report


async def check_account_balances(
    session: AsyncSession, report: ReconciliationReport
) -> None:
    accounts = await session.scalars(select(Account))
    for account in accounts:
        if account.balance < 0:
            report.issues.append(
                ReconciliationIssue(
                    code='negative_balance',
                    message='Account balance is negative',
                    resource_type='account',
                    resource_id=account.id,
                )
            )
        if account.reserved_balance < 0:
            report.issues.append(
                ReconciliationIssue(
                    code='negative_reserved_balance',
                    message='Account reserved balance is negative',
                    resource_type='account',
                    resource_id=account.id,
                )
            )
        if account.reserved_balance > account.balance:
            report.issues.append(
                ReconciliationIssue(
                    code='reserved_balance_exceeds_balance',
                    message='Account reserved balance exceeds balance',
                    resource_type='account',
                    resource_id=account.id,
                )
            )


async def _check_reserved_balances(
    session: AsyncSession, report: ReconciliationReport
) -> None:
    accounts = await session.scalars(select(Account))
    for account in accounts:
        active_holds_total = await session.scalar(
            select(func.coalesce(func.sum(BalanceHold.amount), 0)).where(
                BalanceHold.account_id == account.id,
                BalanceHold.status == HoldStatus.ACTIVE,
            )
        )
        if account.reserved_balance != active_holds_total:
            report.issues.append(
                ReconciliationIssue(
                    code='reserved_balance_mismatch',
                    message='Account reserved balance differs'
                    ' from active holds',
                    resource_type='account',
                    resource_id=account.id,
                )
            )


async def _check_posted_transactions_balance(
    session: AsyncSession, report: ReconciliationReport
) -> None:
    posted_transactions = await session.scalars(
        select(LedgerTransaction).where(
            LedgerTransaction.status.in_([
                TransactionStatus.POSTED,
                TransactionStatus.REVERSED,
            ])
        )
    )
    for transaction in posted_transactions:
        debit_total = await session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
                LedgerEntry.transaction_id == transaction.id,
                LedgerEntry.entry_type == EntryType.DEBIT,
            )
        )
        credit_total = await session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
                LedgerEntry.transaction_id == transaction.id,
                LedgerEntry.entry_type == EntryType.CREDIT,
            )
        )
        if debit_total != credit_total:
            report.issues.append(
                ReconciliationIssue(
                    code='transaction_not_balanced',
                    message='Transaction debit total differs'
                    ' from credit total',
                    resource_type='transaction',
                    resource_id=transaction.id,
                )
            )


async def _check_state_timestamps(
    session: AsyncSession, report: ReconciliationReport
) -> None:
    posted_without_timestamp = await session.scalars(
        select(LedgerTransaction).where(
            LedgerTransaction.status.in_([
                TransactionStatus.POSTED,
                TransactionStatus.REVERSED,
            ]),
            LedgerTransaction.posted_at.is_(None),
        )
    )
    for transaction in posted_without_timestamp:
        report.issues.append(
            ReconciliationIssue(
                code='posted_transaction_missing_posted_at',
                message='Posted transaction is missing posted_at',
                resource_type='transaction',
                resource_id=transaction.id,
            )
        )

    closed_holds_without_timestamp = await session.scalars(
        select(BalanceHold).where(
            BalanceHold.status.in_([
                HoldStatus.CONSUMED,
                HoldStatus.RELEASED,
                HoldStatus.EXPIRED,
                HoldStatus.CANCELLED,
            ]),
            BalanceHold.consumed_at.is_(None),
            BalanceHold.released_at.is_(None),
        )
    )
    for hold in closed_holds_without_timestamp:
        report.issues.append(
            ReconciliationIssue(
                code='closed_hold_missing_timestamp',
                message='Closed hold is missing consumed_at or released_at',
                resource_type='hold',
                resource_id=hold.id,
            )
        )


async def run_reconciliation() -> ReconciliationReport:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await reconcile_session(session)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Validate ledger financial invariants.'
    )
    return parser.parse_args(argv)


def main() -> None:  # pragma: no cover
    parse_args()
    report = asyncio.run(run_reconciliation())
    if report.ok:
        print('Ledger reconciliation passed.')
        return

    for issue in report.issues:
        print(
            f'{issue.code}: {issue.message} '
            f'resource={issue.resource_type}:{issue.resource_id}'
        )
    raise SystemExit(1)


if __name__ == '__main__':  # pragma: no cover
    main()
