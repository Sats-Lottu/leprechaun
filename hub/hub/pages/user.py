from dataclasses import dataclass
from uuid import UUID

import segno
from nicegui import APIRouter, ui
from sqlalchemy import func, select

from hub.auth import current_roles, current_user, current_user_sub
from hub.checkout_service import (
    CheckoutSessionStateError,
    CheckoutSettlementError,
    get_or_create_user_ledger_account,
    prepare_checkout_payment,
    settle_checkout_payment,
)
from hub.layout import (
    CARD_CLASS,
    MUTED_TEXT_CLASS,
    PANEL_TITLE_CLASS,
    USER_NAV_ITEMS,
    VALUE_TEXT_CLASS,
    app_layout,
)
from hub.ledger_client import (
    LedgerClient,
    LedgerClientError,
    LedgerHoldDetails,
    LedgerStatementEntry,
    LedgerTransactionCreate,
    LedgerTransactionEntryCreate,
)
from hub.models.database import session_scope
from hub.models.enums import CheckoutSessionStatus
from hub.models.tables import CheckoutSession
from hub.rabbitmq import (
    request_invoice_rabbitmq,
    request_pay_invoice_rabbitmq,
)
from hub.schemas import MSATS_PER_SAT
from hub.settings import get_settings

router = APIRouter(prefix='/user')


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
class UserIdentity:
    subject: str
    display_name: str
    preferred_username: str
    email: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class WalletWithdrawalResult:
    transaction_id: UUID
    amount_msat: int
    payment_hash: str
    checking_id: str
    account_id: UUID


class WalletWithdrawalError(RuntimeError):
    pass


def _require_user() -> str | None:
    user_sub = current_user_sub()
    if user_sub is None:
        ui.label('Login is required to continue.').classes(
            'text-white/70 text-base'
        )
        ui.button(
            'Login',
            icon='login',
            on_click=lambda: ui.navigate.to('/auth/login'),
        ).classes('w-full sm:w-auto')
        return None
    return user_sub


@router.page('/wallet')
async def wallet_page() -> None:
    with app_layout(
        title='Wallet',
        active_path='/user/wallet',
        area='User',
        nav_items=USER_NAV_ITEMS,
    ):
        user_sub = _require_user()
        if user_sub is None:
            return
        identity = _current_user_identity()

        balance_label = 'Unavailable'
        reserved_label = 'Unavailable'
        total_label = 'Unavailable'
        invoice_label = '0 open'
        account_label = 'Unavailable'
        snapshot: WalletSnapshot | None = None
        recent_checkouts: tuple[CheckoutSession, ...] = ()
        try:
            async with session_scope() as session:
                snapshot = await load_wallet_snapshot(
                    user_sub=user_sub,
                    session=session,
                    ledger=LedgerClient(),
                )
                balance_label = _format_msat(snapshot.available_balance_msat)
                reserved_label = _format_msat(snapshot.reserved_balance_msat)
                total_label = _format_msat(snapshot.total_balance_msat)
                invoice_label = f'{snapshot.open_invoice_count} open'
                account_label = str(snapshot.account_id)
                recent_checkouts = snapshot.recent_checkouts
        except LedgerClientError:
            ui.notify('Ledger is unavailable.', type='warning')

        with ui.grid(columns=1).classes('w-full gap-4 md:grid-cols-3'):
            _summary_tile('Available Balance', balance_label, 'Ready to use')
            _summary_tile('Pending Holds', reserved_label, 'Reserved funds')
            _summary_tile(
                'Total Balance',
                total_label,
                'Ledger source of truth',
            )

        _identity_panel(identity)
        _account_management_panel(
            user_sub=user_sub,
            account_label=account_label,
            snapshot=snapshot,
        )
        with ui.grid(columns=1).classes('w-full gap-4 lg:grid-cols-2'):
            _wallet_invoice_panel(
                user_sub=user_sub,
                account_label=account_label,
                invoice_label=invoice_label,
            )
            _wallet_withdraw_panel(
                user_sub=user_sub,
                account_label=account_label,
            )
        _active_holds_panel(snapshot.active_holds if snapshot else ())
        _ledger_statement_panel(snapshot.statement_entries if snapshot else ())
        _recent_checkout_panel(recent_checkouts)


@router.page('/checkout')
async def checkout_page() -> None:
    with app_layout(
        title='Checkout',
        active_path='/user/checkout',
        area='User',
        nav_items=USER_NAV_ITEMS,
    ):
        session_id = _current_session_id()
        if session_id is None:
            ui.label('Open a checkout link to start payment.').classes(
                MUTED_TEXT_CLASS
            )
            return

        checkout_session = await _load_checkout_session(session_id)

        ui.label(
            'Start anonymous, then login when balance or settlement requires '
            'it.'
        ).classes(MUTED_TEXT_CLASS)

        order_amount = (
            _format_msat(checkout_session.amount_msat)
            if checkout_session
            else '0 sats'
        )
        external_amount = (
            _format_msat(checkout_session.external_amount_msat)
            if checkout_session
            else '0 sats'
        )
        with ui.grid(columns=1).classes('w-full gap-4 md:grid-cols-2'):
            _summary_tile(
                'Order Amount',
                order_amount,
                str(checkout_session.status)
                if checkout_session
                else 'Order unavailable',
            )
            _summary_tile(
                'External Payment',
                external_amount,
                'No invoice created'
                if not checkout_session or not checkout_session.invoice_id
                else 'Invoice ready',
            )

        if checkout_session is not None:
            ui.label(f'Application: {checkout_session.game_id}').classes(
                'text-lg'
            )
            _checkout_composition(checkout_session)
            if checkout_session.status == CheckoutSessionStatus.SETTLED:
                ui.label('Payment confirmed. Purchase completed.').classes(
                    'text-green-400'
                )
                ui.navigate.to(checkout_session.return_url)
            elif current_user_sub() is None:
                ui.button(
                    'Login to continue',
                    icon='login',
                    on_click=lambda: ui.navigate.to(
                        f'/auth/login?checkout_session={session_id}'
                    ),
                )
            elif checkout_session.status == CheckoutSessionStatus.CREATED:

                async def confirm_payment():
                    user_sub = current_user_sub()
                    if user_sub is None:
                        ui.navigate.to(
                            f'/auth/login?checkout_session={session_id}'
                        )
                        return
                    await _prepare_checkout_session_for_user(
                        session_id=session_id,
                        user_sub=user_sub,
                        confirm=True,
                    )
                    ui.navigate.to(f'/user/checkout?session_id={session_id}')

                ui.label(
                    'Confirm to use your wallet balance. '
                    'Any remaining amount is paid by Lightning.'
                )
                ui.button(
                    'Confirm payment', icon='check', on_click=confirm_payment
                )
            if (
                checkout_session.payment_request
                and checkout_session.status
                == CheckoutSessionStatus.AWAITING_PAYMENT
            ):
                _payment_request_qr(checkout_session.payment_request)
            if current_user_sub() and checkout_session.status in {
                CheckoutSessionStatus.RESERVED,
                CheckoutSessionStatus.AWAITING_PAYMENT,
            }:
                _watch_checkout_payment(session_id)


def _watch_checkout_payment(session_id: UUID) -> None:
    in_flight = False

    async def check_payment():
        nonlocal in_flight
        if in_flight:
            return
        if current_user_sub() is None:
            timer.cancel()
            return
        in_flight = True
        try:
            checkout = await _load_checkout_session(session_id)
            if checkout is None:
                timer.cancel()
            elif checkout.status == CheckoutSessionStatus.SETTLED:
                timer.cancel()
                ui.navigate.to(checkout.return_url)
            elif checkout.status not in {
                CheckoutSessionStatus.RESERVED,
                CheckoutSessionStatus.AWAITING_PAYMENT,
            }:
                timer.cancel()
        finally:
            in_flight = False

    timer = ui.timer(3, check_payment)


@router.page('/profile')
async def profile_page() -> None:
    with app_layout(
        title='Profile',
        active_path='/user/profile',
        area='User',
        nav_items=USER_NAV_ITEMS,
    ):
        user_sub = _require_user()
        if user_sub is None:
            return
        identity = _current_user_identity()

        _identity_panel(identity)
        try:
            async with session_scope() as session:
                snapshot = await load_wallet_snapshot(
                    user_sub=user_sub,
                    session=session,
                    ledger=LedgerClient(),
                )
        except LedgerClientError:
            snapshot = None
            ui.notify('Ledger is unavailable.', type='warning')

        if snapshot is not None:
            with ui.grid(columns=1).classes('w-full gap-4 md:grid-cols-3'):
                _summary_tile(
                    'Ledger Account',
                    str(snapshot.account_id),
                    'Mapped internal account',
                )
                _summary_tile(
                    'Available Balance',
                    _format_msat(snapshot.available_balance_msat),
                    'Spendable funds',
                )
                _summary_tile(
                    'Open Invoices',
                    str(snapshot.open_invoice_count),
                    'Awaiting external payment',
                )
            _active_holds_panel(snapshot.active_holds)
            _ledger_statement_panel(snapshot.statement_entries)
            _recent_checkout_panel(snapshot.recent_checkouts)
        else:
            _empty_panel(
                'Detailed history',
                'Profile and ledger history will appear here.',
            )


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
    holds = await ledger.list_holds(account.ledger_account_id)
    statement_entries = await ledger.get_statement(account.ledger_account_id)
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
        active_holds=tuple(hold for hold in holds if hold.status == 'active'),
        statement_entries=tuple(statement_entries[:10]),
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
    session,
    payout_requester=request_pay_invoice_rabbitmq,
) -> WalletWithdrawalResult | None:  # noqa: PLR0913
    if amount_sats <= 0:
        raise ValueError('amount_sats must be positive')

    normalized_payment_request = payment_request.strip()
    if not normalized_payment_request:
        raise ValueError('payment_request is required')

    settings = get_settings()
    settlement_account_id = settings.LIGHTNING_SETTLEMENT_ACCOUNT_ID
    if settlement_account_id is None:
        raise WalletWithdrawalError(
            'LIGHTNING_SETTLEMENT_ACCOUNT_ID must be configured in hub'
        )

    account = await get_or_create_user_ledger_account(
        user_sub=user_sub,
        session=session,
        ledger=ledger,
    )
    amount_msat = amount_sats * MSATS_PER_SAT
    balance = await ledger.get_balance(account.ledger_account_id)
    if balance.available_balance_msat < amount_msat:
        raise WalletWithdrawalError('insufficient available balance')

    payout = await payout_requester(
        user_id=user_sub,
        payment_request=normalized_payment_request,
        amount_msat=amount_msat,
    )
    if not payout:
        return None

    payment_hash = str(payout.get('payment_hash') or '')
    checking_id = str(payout.get('checking_id') or '')
    reference_key = checking_id or payment_hash
    if not reference_key:
        raise WalletWithdrawalError('PLS did not return a payout reference')

    transaction = await ledger.create_transaction(
        LedgerTransactionCreate(
            reference_type='wallet_withdrawal',
            reference_id=None,
            idempotency_key=f'wallet-withdrawal:{reference_key}',
            description='Wallet Lightning withdrawal',
            entries=(
                LedgerTransactionEntryCreate(
                    account_id=account.ledger_account_id,
                    entry_type='debit',
                    amount_msat=amount_msat,
                    description='User wallet withdrawal debit',
                    reference_type='wallet_withdrawal',
                ),
                LedgerTransactionEntryCreate(
                    account_id=settlement_account_id,
                    entry_type='credit',
                    amount_msat=amount_msat,
                    description='Lightning settlement replenishment credit',
                    reference_type='wallet_withdrawal',
                ),
            ),
        )
    )
    posted_transaction = await ledger.post_transaction(
        transaction.transaction_id,
        idempotency_key=f'wallet-withdrawal:post:{reference_key}',
    )
    return WalletWithdrawalResult(
        transaction_id=posted_transaction.transaction_id,
        amount_msat=amount_msat,
        payment_hash=payment_hash,
        checking_id=checking_id,
        account_id=account.ledger_account_id,
    )


def _summary_tile(title: str, value: str, description: str) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS}'):
        ui.label(title).classes(f'text-sm {MUTED_TEXT_CLASS}')
        ui.label(value).classes(f'text-2xl {VALUE_TEXT_CLASS}')
        ui.label(description).classes(f'text-sm {MUTED_TEXT_CLASS}')


def _empty_panel(title: str, message: str) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS}'):
        ui.label(title).classes(f'text-lg {PANEL_TITLE_CLASS}')
        ui.label(message).classes(f'text-sm {MUTED_TEXT_CLASS}')


def _identity_panel(identity: UserIdentity) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS} gap-3'):
        ui.label('User identity').classes(f'text-lg {PANEL_TITLE_CLASS}')
        with ui.grid(columns=1).classes('w-full gap-3 md:grid-cols-2'):
            _identity_row('Display name', identity.display_name)
            _identity_row('Preferred username', identity.preferred_username)
            _identity_row('Email', identity.email)
            _identity_row('OIDC subject', identity.subject)
            _identity_row(
                'Internal roles',
                ', '.join(identity.roles) if identity.roles else 'none',
            )


def _identity_row(label: str, value: str) -> None:
    with ui.column().classes('gap-1'):
        ui.label(label).classes(f'text-xs {MUTED_TEXT_CLASS}')
        ui.label(value).classes('text-sm text-white break-all')


def _account_management_panel(
    *,
    user_sub: str,
    account_label: str,
    snapshot: WalletSnapshot | None,
) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS} gap-4'):
        ui.label('Wallet account').classes(f'text-lg {PANEL_TITLE_CLASS}')
        ui.label(
            'The ledger account is created on first authenticated access and '
            'can be synced again from here.'
        ).classes(f'text-sm {MUTED_TEXT_CLASS}')
        account_value = ui.label(account_label).classes(
            'text-sm text-white break-all'
        )
        status_label = ui.label(
            'Status: ready' if snapshot else 'Status: waiting for ledger'
        ).classes(f'text-sm {MUTED_TEXT_CLASS}')

        async def on_sync_account() -> None:
            status_label.text = 'Status: syncing account...'
            try:
                async with session_scope() as session:
                    account = await get_or_create_user_ledger_account(
                        user_sub=user_sub,
                        session=session,
                        ledger=LedgerClient(),
                    )
            except LedgerClientError:
                status_label.text = 'Status: ledger unavailable'
                ui.notify(
                    'Could not sync the ledger account.',
                    type='negative',
                )
                return

            account_value.text = str(account.ledger_account_id)
            status_label.text = 'Status: account ready'
            ui.notify('Ledger account is ready.', type='positive')

        with ui.row().classes('w-full gap-3'):
            ui.button(
                'Sync Ledger Account',
                icon='account_balance_wallet',
                on_click=on_sync_account,
            ).classes('w-full sm:w-auto')
            ui.button(
                'Refresh Wallet',
                icon='refresh',
                on_click=lambda: ui.navigate.to('/user/wallet'),
            ).props('outline').classes('w-full sm:w-auto')


def _wallet_invoice_panel(
    *,
    user_sub: str,
    account_label: str,
    invoice_label: str,
) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS} gap-4'):
        ui.label('Lightning deposit via PLS').classes(
            f'text-lg {PANEL_TITLE_CLASS}'
        )
        ui.label(f'Ledger account: {account_label}').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
        ui.label(f'Open checkout invoices: {invoice_label}').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
        amount_input = ui.number('Amount (sats)', value=200, min=1).classes(
            'w-full sm:w-56'
        )
        memo_input = ui.input('Memo', value='deposit').classes('w-full')
        status_label = ui.label('Status: idle').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
        invoice_id_label = ui.label('Invoice ID: -').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
        payment_request_input = (
            ui
            .textarea(
                'BOLT11',
                value='',
            )
            .props('readonly rows=3')
            .classes('w-full')
        )

        async def on_request_invoice() -> None:
            try:
                amount_sats = int(amount_input.value or 0)
                status_label.text = 'Status: requesting invoice...'
                invoice = await request_deposit_invoice(
                    user_sub=user_sub,
                    amount_sats=amount_sats,
                    memo=str(memo_input.value or ''),
                )
            except ValueError:
                status_label.text = 'Status: invalid amount'
                ui.notify('Enter a positive amount in sats.', type='warning')
                return

            if invoice is None:
                status_label.text = 'Status: timeout waiting for PLS'
                ui.notify('PLS did not answer in time.', type='negative')
                return

            invoice_id_label.text = (
                f'Invoice ID: {invoice["invoice_id"] or "-"}'
            )
            payment_request_input.value = invoice['payment_request']
            status_label.text = 'Status: invoice ready'
            ui.notify('Lightning invoice created.', type='positive')

        ui.button(
            'Create Lightning Invoice',
            icon='bolt',
            on_click=on_request_invoice,
        ).classes('w-full sm:w-auto')


def _wallet_withdraw_panel(
    *,
    user_sub: str,
    account_label: str,
) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS} gap-4'):
        ui.label('Lightning withdrawal').classes(
            f'text-lg {PANEL_TITLE_CLASS}'
        )
        ui.label(f'Debit source account: {account_label}').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
        ui.label(
            'The user account is debited only after the PLS confirms the '
            'outgoing Lightning payment request.'
        ).classes(f'text-sm {MUTED_TEXT_CLASS}')
        amount_input = ui.number('Amount (sats)', value=100, min=1).classes(
            'w-full sm:w-56'
        )
        bolt11_input = ui.textarea('BOLT11 invoice', value='').classes(
            'w-full'
        )
        status_label = ui.label('Status: idle').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
        payout_label = ui.label('Payment reference: -').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
        transaction_label = ui.label('Ledger transaction: -').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )

        async def on_withdraw() -> None:
            status_label.text = 'Status: paying Lightning invoice...'
            try:
                async with session_scope() as session:
                    result = await withdraw_wallet_balance(
                        user_sub=user_sub,
                        amount_sats=int(amount_input.value or 0),
                        payment_request=str(bolt11_input.value or ''),
                        ledger=LedgerClient(),
                        session=session,
                    )
            except ValueError as exc:
                status_label.text = 'Status: invalid withdrawal request'
                ui.notify(str(exc), type='warning')
                return
            except WalletWithdrawalError as exc:
                status_label.text = 'Status: withdrawal rejected'
                ui.notify(str(exc), type='negative')
                return
            except LedgerClientError:
                status_label.text = 'Status: ledger unavailable'
                ui.notify(
                    'Ledger is unavailable for withdrawal.',
                    type='negative',
                )
                return

            if result is None:
                status_label.text = 'Status: timeout waiting for PLS'
                ui.notify(
                    'PLS did not confirm the withdrawal in time.',
                    type='negative',
                )
                return

            payout_reference = result.checking_id or result.payment_hash or '-'
            payout_label.text = f'Payment reference: {payout_reference}'
            transaction_label.text = (
                f'Ledger transaction: {result.transaction_id}'
            )
            status_label.text = 'Status: withdrawal sent'
            ui.notify('Lightning withdrawal sent.', type='positive')

        ui.button(
            'Send Lightning Withdrawal',
            icon='north_east',
            on_click=on_withdraw,
        ).classes('w-full sm:w-auto')


def _recent_checkout_panel(
    recent_checkouts: tuple[CheckoutSession, ...],
) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS}'):
        ui.label('Recent activity').classes(f'text-lg {PANEL_TITLE_CLASS}')
        if not recent_checkouts:
            ui.label('No checkout activity yet.').classes(
                f'text-sm {MUTED_TEXT_CLASS}'
            )
            return
        for checkout_session in recent_checkouts:
            ui.label(
                f'{checkout_session.game_id} / {checkout_session.order_id}'
            ).classes('text-sm text-white')
            ui.label(
                ' | '.join([
                    f'Status: {checkout_session.status}',
                    (f'Amount: {_format_msat(checkout_session.amount_msat)}'),
                    (
                        'Internal: '
                        f'{_format_msat(checkout_session.internal_amount_msat)}'
                    ),
                    (
                        'External: '
                        f'{_format_msat(checkout_session.external_amount_msat)}'
                    ),
                ])
            ).classes(f'text-xs {MUTED_TEXT_CLASS}')


def _active_holds_panel(active_holds: tuple[LedgerHoldDetails, ...]) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS}'):
        ui.label('Active holds').classes(f'text-lg {PANEL_TITLE_CLASS}')
        if not active_holds:
            ui.label('No active holds in the ledger.').classes(
                f'text-sm {MUTED_TEXT_CLASS}'
            )
            return
        for hold in active_holds:
            ui.label(
                f'{_format_msat(hold.amount_msat)} | {hold.reason or "hold"}'
            ).classes('text-sm text-white')
            ui.label(
                ' | '.join([
                    f'Status: {hold.status}',
                    f'Reference: {hold.reference_type or "-"}',
                    (f'Expires: {_format_optional_datetime(hold.expires_at)}'),
                ])
            ).classes(f'text-xs {MUTED_TEXT_CLASS}')


def _ledger_statement_panel(
    statement_entries: tuple[LedgerStatementEntry, ...],
) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS}'):
        ui.label('Ledger statement').classes(f'text-lg {PANEL_TITLE_CLASS}')
        if not statement_entries:
            ui.label('No ledger entries yet.').classes(
                f'text-sm {MUTED_TEXT_CLASS}'
            )
            return
        for entry in statement_entries:
            ui.label(
                f'{entry.entry_type.title()} {_format_msat(entry.amount_msat)}'
            ).classes('text-sm text-white')
            ui.label(
                ' | '.join([
                    f'Account: {entry.account_id}',
                    f'Reference: {entry.reference_type or "-"}',
                    f'Description: {entry.description or "-"}',
                ])
            ).classes(f'text-xs {MUTED_TEXT_CLASS}')


def _checkout_composition(checkout_session: CheckoutSession) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS}'):
        ui.label('Payment composition').classes(f'text-lg {PANEL_TITLE_CLASS}')
        ui.label(
            f'Internal balance: '
            f'{_format_msat(checkout_session.internal_amount_msat)}'
        ).classes(f'text-sm {MUTED_TEXT_CLASS}')
        ui.label(
            f'External invoice: '
            f'{_format_msat(checkout_session.external_amount_msat)}'
        ).classes(f'text-sm {MUTED_TEXT_CLASS}')
        ui.label(
            f'Destination account: '
            f'{checkout_session.destination_account_id or "-"}'
        ).classes(f'text-sm {MUTED_TEXT_CLASS}')
        ui.label(f'Status: {checkout_session.status}').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )


def _payment_request_qr(payment_request: str) -> None:
    qrcode = segno.make_qr(payment_request, error='l')
    with ui.link(target=f'lightning:{payment_request}').tooltip(
        'Open in Lightning Wallet'
    ):
        ui.image(qrcode.svg_data_uri(light='white', border=1)).classes(
            'w-64'
        ).tooltip('Scan with your Lightning Wallet')
    ui.label(payment_request).classes(
        'mt-2 w-full break-all text-xs text-center'
    ).on(
        'click',
        lambda e: ui.clipboard.write(payment_request),
    ).on(
        'click',
        lambda e: ui.notify('Invoice copied to clipboard!', type='positive'),
    ).tooltip('Click to copy')


def _current_session_id() -> UUID | None:
    try:
        raw_session_id = ui.context.client.request.query_params.get(
            'session_id'
        )
    except RuntimeError:
        return None
    if not raw_session_id:
        return None
    try:
        return UUID(str(raw_session_id))
    except ValueError:
        return None


def _format_msat(amount_msat: int) -> str:
    return f'{amount_msat // 1000} sats'


def _format_optional_datetime(value) -> str:
    return value.isoformat() if value else '-'


async def _load_checkout_session(
    session_id: UUID,
) -> CheckoutSession | None:
    user_sub = current_user_sub()
    if user_sub is not None:
        return await _prepare_checkout_session_for_user(
            session_id=session_id,
            user_sub=user_sub,
        )

    async with session_scope() as db_session:
        return await db_session.scalar(
            select(CheckoutSession).where(CheckoutSession.id == session_id)
        )


async def _prepare_checkout_session_for_user(  # noqa: PLR0911
    *,
    session_id: UUID,
    user_sub: str,
    confirm: bool = False,
) -> CheckoutSession | None:
    try:
        async with session_scope() as db_session:
            existing = await db_session.get(CheckoutSession, session_id)
            if existing is None:
                return None
            if existing.user_id not in {None, user_sub}:
                ui.notify(
                    'This checkout belongs to another user.', type='negative'
                )
                return None
            if (
                existing.status == CheckoutSessionStatus.CREATED
                and not confirm
            ):
                return existing
            preparation = await prepare_checkout_payment(
                checkout_session_id=session_id,
                user_sub=user_sub,
                session=db_session,
            )
            checkout_session = preparation.session
            if checkout_session.status not in {
                CheckoutSessionStatus.RESERVED,
                CheckoutSessionStatus.AWAITING_PAYMENT,
            }:
                return checkout_session
            try:
                return await settle_checkout_payment(
                    checkout_session_id=session_id,
                    session=db_session,
                )
            except (
                CheckoutSessionStateError,
                CheckoutSettlementError,
                LedgerClientError,
            ):
                return checkout_session
    except CheckoutSessionStateError:
        async with session_scope() as db_session:
            return await db_session.scalar(
                select(CheckoutSession).where(CheckoutSession.id == session_id)
            )
    except (LedgerClientError, LookupError):
        ui.notify('Could not prepare checkout.', type='negative')
        return None


def _current_user_identity() -> UserIdentity:
    user_info = current_user() or {}
    user_sub = current_user_sub() or str(user_info.get('sub') or 'unknown')
    display_name = (
        str(user_info.get('name') or '')
        or str(user_info.get('preferred_username') or '')
        or user_sub
    )
    preferred_username = str(
        user_info.get('preferred_username') or user_info.get('nickname') or '-'
    )
    email = str(user_info.get('email') or '-')
    return UserIdentity(
        subject=user_sub,
        display_name=display_name,
        preferred_username=preferred_username,
        email=email,
        roles=tuple(sorted(current_roles())),
    )
