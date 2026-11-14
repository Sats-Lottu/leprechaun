from dataclasses import dataclass

from hub.layout import (
    ACCENT_TEXT_CLASS,
    CARD_CLASS,
    MUTED_TEXT_CLASS,
    PANEL_TITLE_CLASS,
    SECTION_LABEL_CLASS,
    SUBTLE_CARD_CLASS,
    USER_NAV_ITEMS,
    status_badge_classes,
)
from hub.ledger_client import LedgerClientError
from hub.pages import user as _u
from hub.pages.user_components import (
    _fetch_wallet_snapshot,
    _format_msat,
    _qr_code_dialog,
    _require_user,
    _snapshot_signature,
    _watch_wallet_snapshot,
)
from hub.wallet_service import WalletSnapshot, WalletWithdrawalError


@dataclass(frozen=True)
class _TransferPageConfig:
    title: str
    subtitle: str
    heading: str
    methods: tuple[tuple[str, str, str], ...]


DEPOSIT_METHODS: tuple[tuple[str, str, str], ...] = (
    ('lightning', 'Lightning', 'bolt'),
)
WITHDRAW_METHODS: tuple[tuple[str, str, str], ...] = (
    ('lightning', 'Lightning', 'bolt'),
)
_TRANSFER_PAGE_CONFIG = {
    'deposit': _TransferPageConfig(
        title='Deposit',
        subtitle='Choose how you want to add funds.',
        heading='Choose deposit method',
        methods=DEPOSIT_METHODS,
    ),
    'withdraw': _TransferPageConfig(
        title='Withdraw',
        subtitle='Choose how you want to withdraw funds.',
        heading='Choose withdrawal method',
        methods=WITHDRAW_METHODS,
    ),
}


def _credit_signature(snapshot: WalletSnapshot | None) -> tuple:
    if snapshot is None:
        return ()
    return tuple(
        (
            entry.amount_msat,
            entry.description,
            entry.reference_type,
            entry.reference_id,
        )
        for entry in snapshot.statement_entries
        if entry.entry_type.lower() == 'credit'
    )


def _deposit_confirmed(
    latest: WalletSnapshot,
    awaiting_deposit: dict[str, object],
) -> bool:
    if not awaiting_deposit['active']:
        return False
    baseline_total = int(awaiting_deposit['baseline_total_msat'])
    baseline_entries = int(awaiting_deposit['baseline_entries'])
    baseline_credits = awaiting_deposit['baseline_credits']
    return (
        latest.total_balance_msat > baseline_total
        or len(latest.statement_entries) > baseline_entries
        or _credit_signature(latest) != baseline_credits
    )


async def _render_transfer_page(*, mode: str) -> None:
    config = _TRANSFER_PAGE_CONFIG[mode]
    methods = config.methods
    with _u.app_layout(
        title=config.title,
        active_path='/',
        area='User',
        nav_items=USER_NAV_ITEMS,
    ):
        user_sub = _require_user()
        if user_sub is None:
            return

        state: dict[str, WalletSnapshot | None] = {
            'snapshot': await _fetch_wallet_snapshot(user_sub),
        }
        awaiting_deposit: dict[str, object] = {
            'active': False,
            'baseline_total_msat': 0,
            'baseline_entries': 0,
            'baseline_credits': (),
            'dialog': None,
        }
        selected = {'method': methods[0][0]}

        @_u.ui.refreshable
        def render_summary() -> None:
            _wallet_side_summary(state['snapshot'], mode=mode)

        def on_invoice_created(dialog) -> None:
            current = state['snapshot']
            awaiting_deposit.update(
                active=True,
                baseline_total_msat=(
                    current.total_balance_msat if current else 0
                ),
                baseline_entries=(
                    len(current.statement_entries) if current else 0
                ),
                baseline_credits=_credit_signature(current),
                dialog=dialog,
            )

        async def refresh_wallet_state(*, notify_error: bool = False) -> None:
            latest = await _fetch_wallet_snapshot(
                user_sub,
                notify_error=notify_error,
            )
            if latest is None:
                return
            if mode == 'deposit' and _deposit_confirmed(
                latest, awaiting_deposit
            ):
                awaiting_deposit['active'] = False
                dialog = awaiting_deposit['dialog']
                if dialog is not None:
                    dialog.close()
                _u.ui.notify('Lightning deposit confirmed!', type='positive')
                _u.ui.navigate.to('/')
                return
            if _snapshot_signature(latest) == _snapshot_signature(
                state['snapshot']
            ):
                return
            state['snapshot'] = latest
            render_summary.refresh()

        def select_method(method: str) -> None:
            if selected['method'] == method:
                return
            selected['method'] = method
            render_method_section.refresh()

        @_u.ui.refreshable
        def render_method_section() -> None:
            _u.ui.label(config.heading).classes(f'text-xl {PANEL_TITLE_CLASS}')
            _transfer_method_choices(
                methods,
                selected['method'],
                select_method,
            )
            _render_transfer_method_panel(
                mode=mode,
                method=selected['method'],
                user_sub=user_sub,
                on_change=refresh_wallet_state,
                on_invoice_created=on_invoice_created,
            )

        _transfer_header_bar(config.subtitle)
        with _u.ui.grid().classes(
            'w-full grid-cols-1 gap-6 items-start lg:grid-cols-3'
        ):
            with _u.ui.card().classes(
                f'{CARD_CLASS} w-full min-w-0 gap-4 lg:col-span-2'
            ):
                render_method_section()
            with _u.ui.card().classes(
                f'{CARD_CLASS} w-full min-w-0 gap-3 lg:sticky lg:top-24'
            ):
                render_summary()
        _watch_wallet_snapshot(refresh_wallet_state)


def _transfer_header_bar(subtitle: str) -> None:
    with _u.ui.row().classes('w-full items-center justify-between gap-2'):
        _u.ui.button(
            'Back to wallet',
            icon='arrow_back',
            on_click=lambda: _u.ui.navigate.to('/'),
        ).props('flat no-caps dense').classes(
            'text-zinc-300 hover:text-zinc-100'
        )
        _u.ui.label(subtitle).classes(f'text-sm {MUTED_TEXT_CLASS}')


def _transfer_method_choices(
    methods: tuple[tuple[str, str, str], ...],
    selected_method: str,
    on_select,
) -> None:
    with _u.ui.grid().classes('w-full grid-cols-1 gap-3 sm:grid-cols-2'):
        for key, label, icon in methods:
            active = key == selected_method
            button = (
                _u.ui
                .button(
                    label,
                    icon=icon,
                    on_click=lambda method=key: on_select(method),
                )
                .classes(
                    'w-full min-h-20 rounded-xl border border-zinc-700/80 '
                    'transition-all text-base font-medium'
                )
                .props(f'no-caps outline aria-pressed={str(active).lower()}')
            )
            if active:
                button.classes(
                    'bg-amber-500/10 border-amber-400 '
                    'ring-2 ring-amber-400/30 text-amber-300'
                )


def _render_transfer_method_panel(  # noqa: PLR0913
    *,
    mode: str,
    method: str,
    user_sub: str,
    on_change,
    on_invoice_created=None,
) -> None:
    if method != 'lightning':
        return
    if mode == 'deposit':
        _wallet_invoice_panel(
            user_sub=user_sub,
            on_invoice_created=on_invoice_created,
        )
    else:
        _wallet_withdraw_panel(user_sub=user_sub, on_change=on_change)


def _wallet_side_summary(
    snapshot: WalletSnapshot | None,
    *,
    mode: str,
) -> None:
    available_label = (
        _format_msat(snapshot.available_balance_msat)
        if snapshot is not None
        else 'Unavailable'
    )
    reserved_label = (
        _format_msat(snapshot.reserved_balance_msat)
        if snapshot is not None
        else 'Unavailable'
    )
    total_label = (
        _format_msat(snapshot.total_balance_msat)
        if snapshot is not None
        else 'Unavailable'
    )
    note = (
        'Lightning deposits credit your wallet automatically once paid.'
        if mode == 'deposit'
        else 'Withdrawals reserve funds until the Lightning payout settles.'
    )
    _u.ui.label('Wallet summary').classes(SECTION_LABEL_CLASS)
    _u.ui.label('Available Balance').classes(f'mt-1 {SECTION_LABEL_CLASS}')
    _u.ui.label(available_label).classes(
        f'text-3xl font-bold font-mono {ACCENT_TEXT_CLASS} tabular-nums'
    )
    with _u.ui.column().classes(f'w-full {SUBTLE_CARD_CLASS} gap-2'):
        with _u.ui.row().classes('w-full items-center justify-between gap-2'):
            _u.ui.label('Pending Holds').classes(f'text-xs {MUTED_TEXT_CLASS}')
            _u.ui.label(reserved_label).classes(
                'text-xs font-mono text-zinc-200 tabular-nums'
            )
        with _u.ui.row().classes('w-full items-center justify-between gap-2'):
            _u.ui.label('Total Balance').classes(f'text-xs {MUTED_TEXT_CLASS}')
            _u.ui.label(total_label).classes(
                'text-xs font-mono text-zinc-200 tabular-nums'
            )
    _u.ui.label(note).classes(f'text-xs {MUTED_TEXT_CLASS}')


def _render_invoice_status(invoice_state: dict[str, object]) -> None:
    status = str(invoice_state['status'])
    if status == 'idle':
        return
    with _u.ui.column().classes(f'w-full {SUBTLE_CARD_CLASS} gap-3'):
        with _u.ui.row().classes('w-full items-center justify-between gap-2'):
            _u.ui.label(str(invoice_state['message'])).classes(
                'text-xs font-medium text-zinc-200'
            )
            _u.ui.label(status).classes(status_badge_classes(status))
        if invoice_state['invoice_id']:
            _u.ui.label(f'Invoice ID: {invoice_state["invoice_id"]}').classes(
                f'text-xs font-mono {MUTED_TEXT_CLASS} break-all'
            )
        dialog = invoice_state.get('dialog')
        if dialog is not None:
            _u.ui.button(
                'Show QR Code',
                icon='qr_code_2',
                on_click=dialog.open,
            ).props('outline no-caps dense').classes('w-full sm:w-auto')


def _wallet_invoice_panel(*, user_sub: str, on_invoice_created=None) -> None:
    invoice_state: dict[str, object] = {
        'status': 'idle',
        'message': '',
        'invoice_id': '',
        'payment_request': '',
        'dialog': None,
    }

    with _u.ui.column().classes('w-full border-t border-zinc-800 pt-4 gap-4'):
        with _u.ui.column().classes('gap-0.5'):
            _u.ui.label('Deposit via Lightning').classes(
                f'text-lg {PANEL_TITLE_CLASS}'
            )
            _u.ui.label(
                'Generate a BOLT11 invoice to add funds to your wallet.'
            ).classes(f'text-xs {MUTED_TEXT_CLASS}')
        with _u.ui.grid().classes('w-full grid-cols-1 gap-3 sm:grid-cols-2'):
            amount_input = (
                _u.ui
                .number('Amount (sats)', value=200, min=1)
                .props('outlined dense dark')
                .classes('w-full')
            )
            memo_input = (
                _u.ui
                .input('Memo', value='deposit')
                .props('outlined dense dark')
                .classes('w-full')
            )

        @_u.ui.refreshable
        def render_result() -> None:
            _render_invoice_status(invoice_state)

        async def _submit_invoice_request() -> None:
            amount_sats = _parse_positive_sats(amount_input.value)
            if amount_sats <= 0:
                msg = 'Enter a positive amount in sats.'
                invoice_state.update(
                    status='failed',
                    message=msg,
                    invoice_id='',
                    payment_request='',
                    dialog=None,
                )
                _u.ui.notify(msg, type='warning')
                return
            try:
                invoice = await _u.request_deposit_invoice(
                    user_sub=user_sub,
                    amount_sats=amount_sats,
                    memo=str(memo_input.value or ''),
                )
            except ValueError:
                msg = 'Enter a positive amount in sats.'
                invoice_state.update(status='failed', message=msg, dialog=None)
                _u.ui.notify(msg, type='warning')
                return
            if invoice is None:
                invoice_state.update(
                    status='failed',
                    message='Timeout waiting for payment service.',
                    dialog=None,
                )
                _u.ui.notify('PLS did not answer in time.', type='negative')
                return

            dialog = _qr_code_dialog(
                title='Lightning Deposit',
                amount_label=f'{amount_sats:,} sats',
                payment_request=invoice['payment_request'],
                subtitle=(
                    'Scan with your Lightning wallet. This window will '
                    'close automatically once paid.'
                ),
            )
            if on_invoice_created is not None:
                on_invoice_created(dialog)
            invoice_state.update(
                status='active',
                message='Invoice ready to pay',
                invoice_id=invoice['invoice_id'] or '-',
                payment_request=invoice['payment_request'],
                dialog=dialog,
            )
            dialog.open()
            _u.ui.notify('Lightning invoice created.', type='positive')

        async def on_request_invoice() -> None:
            invoice_state.update(
                status='pending',
                message='Requesting Lightning invoice...',
                invoice_id='',
                payment_request='',
                dialog=None,
            )
            render_result.refresh()
            await _submit_invoice_request()
            render_result.refresh()

        _u.ui.button(
            'Create Lightning Invoice',
            icon='bolt',
            on_click=on_request_invoice,
        ).props('unelevated no-caps color=primary').classes(
            'w-full sm:w-auto font-medium'
        )
        render_result()


def _parse_positive_sats(raw_value: object) -> int:
    try:
        return int(raw_value or 0)
    except (TypeError, ValueError):
        return 0


def _render_withdraw_status(withdraw_state: dict[str, str]) -> None:
    status = withdraw_state['status']
    if status == 'idle':
        return
    with _u.ui.column().classes(f'w-full {SUBTLE_CARD_CLASS} gap-1.5'):
        with _u.ui.row().classes('w-full items-center justify-between gap-2'):
            _u.ui.label(withdraw_state['message']).classes(
                'text-xs font-medium text-zinc-200'
            )
            _u.ui.label(status).classes(status_badge_classes(status))
        if withdraw_state['payout_ref']:
            _u.ui.label(
                f'Payment reference: {withdraw_state["payout_ref"]}'
            ).classes(f'text-xs font-mono {MUTED_TEXT_CLASS} break-all')
        if withdraw_state['hold_id']:
            _u.ui.label(f'Ledger hold: {withdraw_state["hold_id"]}').classes(
                f'text-xs font-mono {MUTED_TEXT_CLASS} break-all'
            )


def _wallet_withdraw_panel(*, user_sub: str, on_change=None) -> None:
    withdraw_state = {
        'status': 'idle',
        'message': '',
        'payout_ref': '',
        'hold_id': '',
    }

    with _u.ui.column().classes('w-full border-t border-zinc-800 pt-4 gap-4'):
        with _u.ui.column().classes('gap-0.5'):
            _u.ui.label('Withdraw via Lightning').classes(
                f'text-lg {PANEL_TITLE_CLASS}'
            )
            _u.ui.label(
                'Pay an external BOLT11 Lightning invoice from your '
                'available balance.'
            ).classes(f'text-xs {MUTED_TEXT_CLASS}')
        amount_input = (
            _u.ui
            .number('Amount (sats)', value=100, min=1)
            .props('outlined dense dark')
            .classes('w-full sm:w-56')
        )
        bolt11_input = (
            _u.ui
            .textarea('BOLT11 invoice', value='')
            .props('outlined dense dark rows=2')
            .classes('w-full font-mono text-xs')
        )

        @_u.ui.refreshable
        def render_status() -> None:
            _render_withdraw_status(withdraw_state)

        async def _submit_withdrawal() -> bool:
            async with _u.session_scope() as session:
                result = await _u.withdraw_wallet_balance(
                    user_sub=user_sub,
                    amount_sats=int(amount_input.value or 0),
                    payment_request=str(bolt11_input.value or ''),
                    ledger=_u.LedgerClient(),
                    session=session,
                )
            if result is None:
                withdraw_state.update(
                    status='pending',
                    message='Withdrawal pending background confirmation.',
                )
                _u.ui.notify(
                    'The withdrawal is being processed in the background.',
                    type='info',
                )
            else:
                payout_ref = result.checking_id or result.payment_hash or '-'
                withdraw_state.update(
                    status='paid',
                    message='Withdrawal completed',
                    payout_ref=payout_ref,
                    hold_id=str(result.hold_id),
                )
                _u.ui.notify(
                    'Lightning withdrawal completed.', type='positive'
                )
            if on_change is not None:
                await on_change()
            return True

        async def on_withdraw() -> None:
            withdraw_state.update(
                status='pending',
                message='Paying Lightning invoice...',
                payout_ref='',
                hold_id='',
            )
            render_status.refresh()
            succeeded = False
            try:
                succeeded = await _submit_withdrawal()
            except ValueError as exc:
                withdraw_state.update(status='failed', message=str(exc))
                _u.ui.notify(str(exc), type='warning')
            except WalletWithdrawalError as exc:
                withdraw_state.update(status='failed', message=str(exc))
                _u.ui.notify(str(exc), type='negative')
            except LedgerClientError:
                message = 'Ledger is unavailable for withdrawal.'
                withdraw_state.update(status='failed', message=message)
                _u.ui.notify(message, type='negative')
            render_status.refresh()
            if succeeded:
                _u.ui.navigate.to('/')

        _u.ui.button(
            'Send Lightning Withdrawal',
            icon='north_east',
            on_click=on_withdraw,
        ).props('unelevated no-caps color=primary').classes(
            'w-full sm:w-auto font-medium'
        )
        render_status()
