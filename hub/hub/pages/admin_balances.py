from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from nicegui import ui

from hub.auth import current_user_sub
from hub.layout import CARD_CLASS, MUTED_TEXT_CLASS, PANEL_TITLE_CLASS
from hub.ledger_client import (
    LedgerAccountDetails,
    LedgerClient,
    LedgerClientError,
    LedgerHoldCreate,
    LedgerTransactionCreate,
    LedgerTransactionEntryCreate,
)
from hub.schemas import MSATS_PER_SAT

ADMIN_ORIGIN = 'hub_admin'
ADMIN_REFERENCE_TYPE = 'admin_balance_operation'


@dataclass(frozen=True)
class AdminBalanceOperationResult:
    resource_id: UUID
    status: str


async def render_admin_balances() -> None:
    ledger = LedgerClient()
    try:
        accounts = await ledger.list_accounts()
    except LedgerClientError:
        ui.notify('Ledger is unavailable.', type='negative')
        ui.label('Could not load Ledger accounts.').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
        return

    _balance_summary(accounts)
    _accounts_table(accounts)
    _operation_panel(accounts, ledger)


async def adjust_account_balance(  # noqa: PLR0913
    *,
    account_id: UUID,
    amount_msat: int,
    operation: str,
    reason: str,
    actor_sub: str,
    ledger: LedgerClient,
) -> AdminBalanceOperationResult:
    if operation not in {'credit', 'debit'}:
        raise ValueError('operation must be credit or debit')
    if amount_msat <= 0:
        raise ValueError('amount_msat must be greater than zero')
    if not reason.strip():
        raise ValueError('reason is required')

    operation_id = uuid4()
    kind = 'external_credit' if operation == 'credit' else 'external_debit'
    entry_type = 'credit' if operation == 'credit' else 'debit'
    transaction = await ledger.create_transaction(
        LedgerTransactionCreate(
            kind=kind,
            external_origin=ADMIN_ORIGIN,
            reference_type=ADMIN_REFERENCE_TYPE,
            reference_id=operation_id,
            idempotency_key=f'admin:{operation_id}:transaction',
            description=(
                f'Admin {operation} by {actor_sub}: {reason.strip()}'
            ),
            entries=(
                LedgerTransactionEntryCreate(
                    account_id=account_id,
                    entry_type=entry_type,
                    amount_msat=amount_msat,
                    description=reason.strip(),
                    reference_type=ADMIN_REFERENCE_TYPE,
                    reference_id=operation_id,
                ),
            ),
        )
    )
    posted = await ledger.post_transaction(
        transaction.transaction_id,
        idempotency_key=f'admin:{operation_id}:post',
    )
    return AdminBalanceOperationResult(
        resource_id=posted.transaction_id,
        status=posted.status,
    )


async def reserve_account_balance(  # noqa: PLR0913
    *,
    account_id: UUID,
    amount_msat: int,
    reason: str,
    actor_sub: str,
    expires_in_hours: int | None,
    ledger: LedgerClient,
) -> AdminBalanceOperationResult:
    if amount_msat <= 0:
        raise ValueError('amount_msat must be greater than zero')
    if not reason.strip():
        raise ValueError('reason is required')
    if expires_in_hours is not None and expires_in_hours <= 0:
        raise ValueError('expires_in_hours must be greater than zero')

    operation_id = uuid4()
    expires_at = (
        datetime.now(UTC) + timedelta(hours=expires_in_hours)
        if expires_in_hours is not None
        else None
    )
    hold = await ledger.create_hold(
        LedgerHoldCreate(
            account_id=account_id,
            amount_msat=amount_msat,
            reason=f'Admin hold by {actor_sub}: {reason.strip()}',
            reference_type=ADMIN_REFERENCE_TYPE,
            reference_id=operation_id,
            idempotency_key=f'admin:{operation_id}:hold',
            expires_at=expires_at,
        )
    )
    return AdminBalanceOperationResult(
        resource_id=hold.hold_id,
        status=hold.status,
    )


def _balance_summary(accounts: list[LedgerAccountDetails]) -> None:
    total_balance = sum(account.balance_msat for account in accounts)
    total_reserved = sum(account.reserved_balance_msat for account in accounts)
    with ui.grid(columns=1).classes('w-full gap-4 md:grid-cols-3'):
        _summary_tile('Accounts', str(len(accounts)), 'Ledger accounts')
        _summary_tile(
            'Total Balance',
            _format_msat(total_balance),
            'Across all accounts',
        )
        _summary_tile(
            'Blocked Balance',
            _format_msat(total_reserved),
            'Active holds',
        )


def _summary_tile(label: str, value: str, helper: str) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS}'):
        ui.label(label).classes(f'text-sm {MUTED_TEXT_CLASS}')
        ui.label(value).classes('text-2xl font-bold text-[#f5b400]')
        ui.label(helper).classes(f'text-xs {MUTED_TEXT_CLASS}')


def _accounts_table(accounts: list[LedgerAccountDetails]) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS} gap-4'):
        ui.label('Ledger accounts').classes(f'text-lg {PANEL_TITLE_CLASS}')
        if not accounts:
            ui.label('No Ledger accounts found.').classes(MUTED_TEXT_CLASS)
            return

        rows = [
            {
                'id': str(account.account_id),
                'name': account.name or '-',
                'type': account.account_type,
                'balance': _format_msat(account.balance_msat),
                'reserved': _format_msat(account.reserved_balance_msat),
                'available': _format_msat(account.available_balance_msat),
                'status': 'Active' if account.is_active else 'Inactive',
            }
            for account in accounts
        ]
        columns = [
            {'name': 'name', 'label': 'Account', 'field': 'name'},
            {'name': 'type', 'label': 'Type', 'field': 'type'},
            {'name': 'balance', 'label': 'Balance', 'field': 'balance'},
            {'name': 'reserved', 'label': 'Blocked', 'field': 'reserved'},
            {'name': 'available', 'label': 'Available', 'field': 'available'},
            {'name': 'status', 'label': 'Status', 'field': 'status'},
            {'name': 'id', 'label': 'Ledger ID', 'field': 'id'},
        ]
        ui.table(
            columns=columns,
            rows=rows,
            row_key='id',
            pagination=10,
        ).classes('w-full').props('flat bordered wrap-cells')


def _operation_panel(
    accounts: list[LedgerAccountDetails], ledger: LedgerClient
) -> None:
    active_accounts = [account for account in accounts if account.is_active]
    options = {
        str(account.account_id): _account_label(account)
        for account in active_accounts
    }
    with ui.card().classes(f'w-full {CARD_CLASS} gap-4'):
        ui.label('Balance operation').classes(f'text-lg {PANEL_TITLE_CLASS}')
        ui.label(
            'Credit and debit change the total balance. Reserve creates a '
            'hold and reduces only the available balance.'
        ).classes(f'text-sm {MUTED_TEXT_CLASS}')

        account_input = ui.select(
            options,
            label='Ledger account',
            with_input=True,
            clearable=True,
        ).props('outlined dense options-dense').classes('w-full')
        with ui.grid(columns=1).classes('w-full gap-4 md:grid-cols-3'):
            operation_input = ui.select(
                {
                    'credit': 'Add balance',
                    'debit': 'Remove balance',
                    'hold': 'Reserve / block balance',
                },
                value='credit',
                label='Operation',
            ).props('outlined dense').classes('w-full')
            amount_input = ui.number(
                'Amount (sats)',
                min=1,
                step=1,
                precision=0,
            ).props('outlined dense').classes('w-full')
            expiry_input = ui.select(
                {
                    '1': '1 hour',
                    '24': '24 hours',
                    '168': '7 days',
                    'none': 'No expiration',
                },
                value='24',
                label='Hold expiration',
            ).props('outlined dense').classes('w-full')
        reason_input = ui.input('Reason / audit note').props(
            'outlined dense maxlength=200'
        ).classes('w-full')

        with ui.dialog() as confirmation:
            with ui.card().classes(f'w-full max-w-lg {CARD_CLASS} gap-4'):
                ui.label('Confirm balance operation').classes(
                    f'text-lg {PANEL_TITLE_CLASS}'
                )
                ui.label(
                    'This action changes financial state and will be recorded '
                    'in the Ledger audit trail.'
                ).classes(f'text-sm {MUTED_TEXT_CLASS}')
                with ui.row().classes('w-full justify-end gap-2'):
                    ui.button('Cancel', on_click=confirmation.close).props(
                        'flat no-caps'
                    )
                    ui.button(
                        'Confirm operation',
                        icon='verified_user',
                        on_click=lambda: _execute_operation(
                            account_input=account_input,
                            operation_input=operation_input,
                            amount_input=amount_input,
                            expiry_input=expiry_input,
                            reason_input=reason_input,
                            ledger=ledger,
                            confirmation=confirmation,
                        ),
                    ).props('unelevated no-caps color=primary')

        ui.button(
            'Review operation',
            icon='fact_check',
            on_click=confirmation.open,
        ).props('unelevated no-caps color=primary').classes(
            'w-full sm:w-auto'
        )


async def _execute_operation(  # noqa: PLR0913
    *,
    account_input,
    operation_input,
    amount_input,
    expiry_input,
    reason_input,
    ledger: LedgerClient,
    confirmation,
) -> None:
    try:
        account_id = UUID(str(account_input.value))
        amount_sats = int(amount_input.value)
    except (TypeError, ValueError):
        ui.notify(
            'Select an account and enter a valid amount.',
            type='warning',
        )
        return

    reason = str(reason_input.value or '').strip()
    if not reason:
        ui.notify('An audit reason is required.', type='warning')
        return
    if amount_sats <= 0:
        ui.notify('Amount must be greater than zero.', type='warning')
        return

    operation = str(operation_input.value)
    actor_sub = current_user_sub() or 'unknown-admin'
    amount_msat = amount_sats * MSATS_PER_SAT
    try:
        if operation == 'hold':
            expiry_value = str(expiry_input.value)
            expires_in_hours = (
                None if expiry_value == 'none' else int(expiry_value)
            )
            result = await reserve_account_balance(
                account_id=account_id,
                amount_msat=amount_msat,
                reason=reason,
                actor_sub=actor_sub,
                expires_in_hours=expires_in_hours,
                ledger=ledger,
            )
        else:
            result = await adjust_account_balance(
                account_id=account_id,
                amount_msat=amount_msat,
                operation=operation,
                reason=reason,
                actor_sub=actor_sub,
                ledger=ledger,
            )
    except (LedgerClientError, ValueError):
        ui.notify(
            'The Ledger rejected the operation. Check the balance and data.',
            type='negative',
        )
        return

    confirmation.close()
    ui.notify(
        f'Operation completed: {result.status}.',
        type='positive',
    )
    ui.navigate.to('/admin/balances')


def _account_label(account: LedgerAccountDetails) -> str:
    name = account.name or str(account.account_id)
    balance = _format_msat(account.balance_msat)
    return f'{name} · {account.account_type} · {balance}'


def _format_msat(amount_msat: int) -> str:
    return f'{amount_msat // MSATS_PER_SAT:,} sats'
