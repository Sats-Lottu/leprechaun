from nicegui import APIRouter, ui

from hub.auth import current_roles, current_user, current_user_sub
from hub.layout import (
    CARD_CLASS,
    MUTED_TEXT_CLASS,
    PANEL_TITLE_CLASS,
    PROJECT_NAME,
    USER_NAV_ITEMS,
    VALUE_TEXT_CLASS,
    app_layout,
    logo_placeholder,
)

router = APIRouter()


@router.page('/')
async def page():
    user = current_user()

    with app_layout(
        title='Hub',
        active_path='/',
        area='Home',
        nav_items=USER_NAV_ITEMS,
    ):
        with ui.card().classes(f'w-full {CARD_CLASS}'):
            with ui.row().classes('items-center gap-3'):
                logo_placeholder(size='md')
                ui.label(PROJECT_NAME).classes(f'text-3xl {VALUE_TEXT_CLASS}')
            ui.label('Hybrid checkout for Leprechaun games.').classes(
                f'text-base {MUTED_TEXT_CLASS}'
            )

            with ui.row().classes('w-full gap-3 mt-3'):
                if user is None:
                    ui.button(
                        'Login',
                        icon='login',
                        on_click=lambda: ui.navigate.to('/auth/login'),
                    ).classes('w-full sm:w-auto')
                    ui.button(
                        'Continue to checkout',
                        icon='shopping_cart_checkout',
                        on_click=lambda: ui.navigate.to('/user/checkout'),
                    ).props('outline').classes('w-full sm:w-auto')
                else:
                    ui.button(
                        'Wallet',
                        icon='account_balance_wallet',
                        on_click=lambda: ui.navigate.to('/user/wallet'),
                    ).classes('w-full sm:w-auto')
                    ui.button(
                        'Checkout',
                        icon='shopping_cart_checkout',
                        on_click=lambda: ui.navigate.to('/user/checkout'),
                    ).props('outline').classes('w-full sm:w-auto')

        if user is None:
            _info_panel(
                'Anonymous checkout is available',
                'Login is required only when wallet balance or settlement '
                'needs a user identity.',
            )
            return

        _info_panel(
            f'Welcome {current_user_sub() or "user"}!',
            'Your wallet, checkout and profile are available in the drawer.',
        )
        _info_panel(
            'Signed in user',
            ' | '.join([
                f'Name: {user.get("name") or current_user_sub() or "-"}',
                f'Email: {user.get("email") or "-"}',
                f'Roles: {", ".join(sorted(current_roles())) or "none"}',
            ]),
        )


def _info_panel(title: str, message: str) -> None:
    with ui.card().classes(f'w-full {CARD_CLASS}'):
        ui.label(title).classes(f'text-lg {PANEL_TITLE_CLASS}')
        ui.label(message).classes(f'text-sm {MUTED_TEXT_CLASS}')
