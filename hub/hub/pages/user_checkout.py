from uuid import UUID

from hub.checkout_service import (
    FINAL_CHECKOUT_STATUSES,
    CheckoutCancellationError,
    CheckoutSessionStateError,
)
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
from hub.models.enums import CheckoutSessionStatus
from hub.models.tables import CheckoutSession
from hub.pages import user as _u
from hub.pages.user_components import (
    _format_msat,
    _format_optional_datetime,
    _qr_code_dialog,
)


async def render_checkout_page() -> None:
    with _u.app_layout(
        title='Checkout',
        active_path='/user/checkout',
        area='User',
        nav_items=USER_NAV_ITEMS,
    ):
        session_id = _u._current_session_id()  # noqa: SLF001
        if session_id is None:
            _checkout_message_card('Open a checkout link to start payment.')
            return

        checkout_session = await _u._load_checkout_session(session_id)  # noqa: SLF001
        if checkout_session is None:
            _checkout_message_card(
                'Checkout unavailable. Please check your link.'
            )
            return
        if checkout_session.status == CheckoutSessionStatus.SETTLED:
            _u.ui.label('Payment confirmed. Purchase completed.')
            _u.ui.navigate.to(checkout_session.return_url)
            return

        with _u.ui.row().classes('w-full items-center justify-between gap-2'):
            _u.ui.button(
                'Back to store',
                icon='arrow_back',
                on_click=lambda: _u.ui.navigate.to(
                    checkout_session.cancel_url
                ),
            ).props('flat no-caps dense').classes(
                'text-zinc-300 hover:text-zinc-100'
            )
            _u.ui.label('Choose how you want to pay.').classes(
                f'text-sm {MUTED_TEXT_CLASS}'
            )
        await _u._checkout_content(checkout_session)  # noqa: SLF001
        if _u.current_user_sub() and checkout_session.status in {
            CheckoutSessionStatus.RESERVED,
            CheckoutSessionStatus.AWAITING_PAYMENT,
        }:
            _u._watch_checkout_payment(  # noqa: SLF001
                session_id,
                awaiting_invoice=(
                    checkout_session.status
                    == CheckoutSessionStatus.AWAITING_PAYMENT
                    and not checkout_session.payment_request
                ),
            )


def _checkout_message_card(message: str) -> None:
    with _u.ui.card().classes(f'w-full max-w-md mx-auto {CARD_CLASS} gap-4'):
        _u.ui.label(message).classes(MUTED_TEXT_CLASS)
        _u.ui.button(
            'Go to wallet',
            icon='account_balance_wallet',
            on_click=lambda: _u.ui.navigate.to('/'),
        ).props('outline no-caps').classes('w-full sm:w-auto')


async def _checkout_content(checkout: CheckoutSession) -> None:
    available = await _u._checkout_available_balance(checkout)  # noqa: SLF001
    selected = 'balance' if checkout.internal_amount_msat else 'lightning'
    if checkout.status == CheckoutSessionStatus.CREATED:
        selected = ''
    busy = False

    async def prepare(use_balance: bool):
        nonlocal busy, selected
        if busy:
            return
        user_sub = _u.current_user_sub()
        if user_sub is None:
            _u.ui.navigate.to(f'/auth/login?checkout_session={checkout.id}')
            return
        busy = True
        render.refresh()
        try:
            result = await _u._prepare_checkout_session_for_user(  # noqa: SLF001
                session_id=checkout.id,
                user_sub=user_sub,
                confirm=True,
                use_balance=use_balance,
            )
            if result is not None:
                _u.ui.navigate.to(f'/user/checkout?session_id={checkout.id}')
        finally:
            busy = False
            selected = '' if not use_balance else 'balance'
            render.refresh()

    async def choose(method: str):
        nonlocal selected
        if busy:
            return
        selected = method
        render.refresh()
        if method == 'lightning':
            await prepare(False)

    @_u.ui.refreshable
    def render():
        with _u.ui.grid().classes(
            'w-full grid-cols-1 gap-6 items-start lg:grid-cols-3'
        ):
            with _u.ui.card().classes(
                f'{CARD_CLASS} w-full min-w-0 gap-4 lg:col-span-2'
            ):
                _u.ui.label('Choose payment method').classes(
                    f'text-xl {PANEL_TITLE_CLASS}'
                )
                if checkout.status in FINAL_CHECKOUT_STATUSES:
                    with _u.ui.row().classes('items-center gap-2'):
                        _u.ui.label(f'Checkout {checkout.status}.').classes(
                            MUTED_TEXT_CLASS
                        )
                        _u.ui.label(checkout.status).classes(
                            status_badge_classes(checkout.status)
                        )
                else:
                    _checkout_method_choices(
                        checkout, available, selected, choose, busy
                    )
                    _checkout_payment_details(checkout, selected)
            with _u.ui.card().classes(
                f'{CARD_CLASS} w-full min-w-0 gap-3 lg:sticky lg:top-24'
            ):
                _checkout_order_summary(checkout)
                _checkout_actions(checkout, selected, busy, prepare)

    render()


def _checkout_actions(
    checkout: CheckoutSession,
    selected: str,
    busy: bool,
    prepare,
) -> None:
    if checkout.status in FINAL_CHECKOUT_STATUSES:
        return
    with _u.ui.column().classes('w-full gap-2 pt-2'):
        if _u.current_user_sub() is None:
            _u.ui.button(
                'Login to continue',
                icon='login',
                on_click=lambda: _u.ui.navigate.to(
                    f'/auth/login?checkout_session={checkout.id}'
                ),
            ).classes('w-full font-medium').props(
                'unelevated no-caps color=primary'
            )
        elif (
            selected == 'balance'
            and checkout.status == CheckoutSessionStatus.CREATED
        ):
            _u.ui.button(
                'Confirm payment',
                icon='check',
                on_click=lambda: prepare(True),
            ).classes('w-full font-medium').props(
                'unelevated no-caps color=primary loading disable'
                if busy
                else 'unelevated no-caps color=primary'
            )
        if _u.current_user_sub():
            _u.ui.button(
                'Cancel payment',
                icon='close',
                on_click=lambda: _u._cancel_checkout(checkout.id),  # noqa: SLF001
            ).classes('w-full text-zinc-400 hover:text-rose-400').props(
                'flat no-caps disable' if busy else 'flat no-caps'
            )


def _checkout_order_summary(checkout: CheckoutSession) -> None:
    _u.ui.label('Order summary').classes(SECTION_LABEL_CLASS)
    _u.ui.label(checkout.description or 'Purchase').classes(
        'text-lg font-semibold text-zinc-100 break-words w-full'
    )
    with _u.ui.column().classes(f'w-full {SUBTLE_CARD_CLASS} gap-1'):
        _u.ui.label(f'Application: {checkout.game_id}').classes(
            f'text-xs {MUTED_TEXT_CLASS} break-all'
        )
        _u.ui.label(f'Order: {checkout.order_id}').classes(
            f'text-xs font-mono {MUTED_TEXT_CLASS} break-all'
        )
    _u.ui.label('Order Amount').classes(f'mt-2 {SECTION_LABEL_CLASS}')
    _u.ui.label(_format_msat(checkout.amount_msat)).classes(
        f'text-3xl font-bold font-mono {ACCENT_TEXT_CLASS} tabular-nums'
    )
    if checkout.status != CheckoutSessionStatus.CREATED:
        _u._checkout_composition(checkout)  # noqa: SLF001


async def _checkout_available_balance(checkout: CheckoutSession) -> int:
    user_sub = _u.current_user_sub()
    if not user_sub or checkout.status != CheckoutSessionStatus.CREATED:
        return 0
    try:
        async with _u.session_scope() as session:
            ledger = _u.LedgerClient()
            account = await _u.get_or_create_user_ledger_account(
                user_sub=user_sub,
                session=session,
                ledger=ledger,
            )
            balance = await ledger.get_balance(account.ledger_account_id)
            return max(0, balance.available_balance_msat)
    except LedgerClientError:
        _u.ui.notify('Wallet balance is unavailable.', type='warning')
        return 0


def _checkout_method_choices(  # noqa: PLR0913
    checkout, available, selected, choose, busy
):
    methods = [('lightning', 'Lightning', 'bolt')]
    if available > 0 or checkout.internal_amount_msat:
        methods.insert(
            0, ('balance', 'Wallet balance', 'account_balance_wallet')
        )
    with _u.ui.grid().classes('w-full grid-cols-1 gap-3 sm:grid-cols-2'):
        for key, label, icon in methods:
            active = key == selected
            button = (
                _u.ui
                .button(
                    label,
                    icon=icon,
                    on_click=lambda method=key: choose(method),
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
            if busy or checkout.status != CheckoutSessionStatus.CREATED:
                button.props('disable')
    if available:
        _u.ui.label(f'Available balance: {_format_msat(available)}').classes(
            f'text-xs font-mono {MUTED_TEXT_CLASS}'
        )


def _checkout_payment_details(checkout, selected):
    if checkout.status == CheckoutSessionStatus.AWAITING_PAYMENT:
        _checkout_awaiting_payment_details(checkout)
    elif checkout.status == CheckoutSessionStatus.RESERVED:
        _u.ui.label('Processing payment. Please wait.').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
    elif selected == 'balance':
        _u.ui.label(
            'Confirm to use your wallet balance. Any remaining amount '
            'is paid by Lightning.'
        ).classes(f'text-sm {MUTED_TEXT_CLASS}')
    elif selected == 'lightning':
        _u.ui.label('Preparing your Lightning invoice…').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
    else:
        _u.ui.label(
            'Select a method to continue. Lightning payments are '
            'confirmed automatically.'
        ).classes(f'text-sm {MUTED_TEXT_CLASS}')


def _checkout_awaiting_payment_details(checkout) -> None:
    with _u.ui.column().classes('w-full border-t border-zinc-800 pt-4 gap-3'):
        _u.ui.label('Pay with Lightning').classes(
            f'text-lg {PANEL_TITLE_CLASS}'
        )
        _u.ui.label('Payment is confirmed automatically.').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
        with _u.ui.column().classes(f'w-full {SUBTLE_CARD_CLASS} gap-2.5'):
            _u.ui.label('Amount to pay').classes(SECTION_LABEL_CLASS)
            _u.ui.label(_format_msat(checkout.external_amount_msat)).classes(
                f'text-2xl font-bold font-mono '
                f'{ACCENT_TEXT_CLASS} tabular-nums'
            )
            _u.ui.label(
                'Open your Lightning wallet, scan the QR code '
                'or copy the invoice, then pay in your wallet.'
            ).classes('text-sm text-zinc-300')
            _u.ui.label(
                'You will return to the store after confirmation.'
            ).classes(f'text-xs {MUTED_TEXT_CLASS}')
            _u.ui.label(
                'Expires: '
                + _format_optional_datetime(
                    checkout.invoice_expires_at or checkout.expires_at
                )
            ).classes(f'text-xs font-mono break-all {MUTED_TEXT_CLASS}')
        if checkout.payment_request:
            dialog = _qr_code_dialog(
                title='Pay with Lightning',
                amount_label=_format_msat(checkout.external_amount_msat),
                payment_request=checkout.payment_request,
                subtitle='Payment is confirmed automatically.',
            )
            dialog.open()
            _u.ui.button(
                'Show QR Code',
                icon='qr_code_2',
                on_click=dialog.open,
            ).props('outline no-caps').classes('w-full sm:w-auto')
        else:
            _u.ui.label('Preparing your Lightning invoice…').classes(
                MUTED_TEXT_CLASS
            )


def _checkout_composition(checkout_session: CheckoutSession) -> None:
    with _u.ui.column().classes(
        'w-full border-t border-zinc-800 pt-4 mt-2 gap-1.5'
    ):
        _u.ui.label('Payment composition').classes(SECTION_LABEL_CLASS)
        _u.ui.label(
            f'Internal balance: '
            f'{_format_msat(checkout_session.internal_amount_msat)}'
        ).classes(f'text-xs font-mono {MUTED_TEXT_CLASS}')
        _u.ui.label(
            f'External invoice: '
            f'{_format_msat(checkout_session.external_amount_msat)}'
        ).classes(f'text-xs font-mono {MUTED_TEXT_CLASS}')
        _u.ui.label(f'Status: {checkout_session.status}').classes(
            f'text-xs font-mono {MUTED_TEXT_CLASS}'
        )


async def _cancel_checkout(session_id: UUID) -> None:
    user_sub = _u.current_user_sub()
    if not user_sub:
        _u.ui.notify('Login is required to cancel payment.', type='warning')
        return
    try:
        async with _u.session_scope() as session:
            checkout = await session.get(CheckoutSession, session_id)
            if checkout is None or checkout.user_id not in {None, user_sub}:
                _u.ui.notify('Checkout unavailable.', type='negative')
                return
            result = await _u.cancel_checkout_payment(
                checkout_session_id=session_id,
                session=session,
                user_sub=user_sub,
            )
            _u.ui.navigate.to(result.cancel_url)
    except (
        CheckoutCancellationError,
        CheckoutSessionStateError,
        LedgerClientError,
        LookupError,
    ):
        _u.ui.notify(
            'Could not cancel payment. Please refresh and try again.',
            type='negative',
        )


def _watch_checkout_payment(
    session_id: UUID, *, awaiting_invoice: bool = False
) -> None:
    in_flight = False

    async def check_payment():
        nonlocal in_flight
        if in_flight:
            return
        if _u.current_user_sub() is None:
            timer.cancel()
            return
        in_flight = True
        try:
            checkout = await _u._load_checkout_session(session_id)  # noqa: SLF001
            if checkout is None:
                timer.cancel()
            elif checkout.status == CheckoutSessionStatus.SETTLED:
                timer.cancel()
                _u.ui.notify('Payment confirmed!', type='positive')
                _u.ui.navigate.to(checkout.return_url)
            elif (
                awaiting_invoice and checkout.payment_request
            ) or checkout.status not in {
                CheckoutSessionStatus.RESERVED,
                CheckoutSessionStatus.AWAITING_PAYMENT,
            }:
                timer.cancel()
                _u.ui.navigate.to(f'/user/checkout?session_id={session_id}')
        finally:
            in_flight = False

    timer = _u.ui.timer(3, check_payment)


def _current_session_id() -> UUID | None:
    try:
        raw_session_id = _u.ui.context.client.request.query_params.get(
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


async def _load_checkout_session(
    session_id: UUID,
) -> CheckoutSession | None:
    async with _u.session_scope() as session:
        return await _u.read_checkout(
            session_id, session, user_sub=_u.current_user_sub()
        )


async def _prepare_checkout_session_for_user(  # noqa: PLR0911
    *,
    session_id: UUID,
    user_sub: str,
    confirm: bool = False,
    use_balance: bool = True,
) -> CheckoutSession | None:
    try:
        async with _u.session_scope() as session:
            if not confirm:
                return await _u.read_checkout(
                    session_id, session, user_sub=user_sub
                )
            preparation = await _u.prepare_checkout_payment(
                checkout_session_id=session_id,
                user_sub=user_sub,
                session=session,
                use_balance=use_balance,
            )
            return preparation.session
    except CheckoutSessionStateError as exc:
        _u.ui.notify(f'Checkout unavailable: {exc.status}.', type='negative')
    except (LedgerClientError, LookupError):
        _u.ui.notify('Could not prepare checkout.', type='negative')
    return None
