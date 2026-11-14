from functools import partial

from fastapi import HTTPException
from nicegui import APIRouter, ui
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hub.applications import (
    ApplicationCreate,
    change_application,
    create_application,
    require_admin,
)
from hub.layout import (
    ADMIN_NAV_ITEMS,
    CARD_CLASS,
    MUTED_TEXT_CLASS,
    PANEL_TITLE_CLASS,
    SECTION_LABEL_CLASS,
    SUBTLE_CARD_CLASS,
    app_layout,
    status_badge_classes,
)
from hub.ledger_client import LedgerClientError
from hub.models.database import session_scope
from hub.models.tables import ApplicationAudit, ConnectedApplication
from hub.pages.admin import _require_admin

router = APIRouter(prefix='/admin')


def show_key(key: str):
    # Keep the one-time secret outside the list refreshed after rotation.
    with ui.context.client.content:
        with (
            ui.dialog() as dialog,
            ui.card().classes(f'w-full max-w-xl {CARD_CLASS} gap-4'),
        ):
            ui.label('Save the application API key').classes(
                f'text-xl {PANEL_TITLE_CLASS}'
            )
            ui.label(
                'This key is shown only now. '
                'Store it on your application server.'
            ).classes(f'text-sm {MUTED_TEXT_CLASS}')
            ui.input(
                'API key',
                value=key,
                password=True,
                password_toggle_button=True,
            ).props('readonly outlined dense dark').classes(
                'w-full font-mono text-xs'
            )
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button(
                    'Close',
                    on_click=dialog.close,
                ).props('flat no-caps').classes('text-zinc-300')
                ui.button(
                    'Copy key',
                    icon='content_copy',
                    on_click=lambda: _copy_api_key(key),
                ).props('unelevated no-caps color=primary')
    dialog.open()


def _copy_api_key(key: str) -> None:
    ui.clipboard.write(key)
    ui.notify('API key copied to clipboard.', type='positive')


def _application_card(application: ConnectedApplication, refresh) -> None:
    status_label = 'Active' if application.is_active else 'Disabled'
    action = 'disabled' if application.is_active else 'enabled'

    async def change(selected_action, application_id):
        try:
            async with session_scope() as session:
                key = await change_application(
                    application_id, selected_action, session
                )
            if key:
                show_key(key)
            ui.notify('Application updated.', type='positive')
            refresh()
        except HTTPException as exc:
            ui.notify(str(exc.detail), type='negative')

    with ui.card().classes(f'w-full {CARD_CLASS} gap-3'):
        with ui.row().classes('w-full items-start justify-between gap-3'):
            with ui.column().classes('gap-0.5 min-w-0'):
                ui.label(application.name).classes(
                    f'text-lg {PANEL_TITLE_CLASS}'
                )
                ui.label(application.slug).classes(
                    f'text-xs font-mono {MUTED_TEXT_CLASS}'
                )
            ui.label(status_label).classes(status_badge_classes(status_label))

        with ui.column().classes(f'w-full {SUBTLE_CARD_CLASS} gap-1.5'):
            ui.label(application.website_url).classes(
                'text-sm text-zinc-200 break-all'
            )
            ui.label(
                f'Receiving account: {application.destination_account_id}'
            ).classes(f'text-xs font-mono {MUTED_TEXT_CLASS} break-all')
            ui.label(f'Key: {application.key_prefix}…').classes(
                f'text-xs font-mono {MUTED_TEXT_CLASS}'
            )

        with ui.row().classes('w-full items-center gap-2 pt-1'):
            ui.button(
                'Disable' if application.is_active else 'Enable',
                icon='power_settings_new',
                on_click=partial(change, action, application.id),
            ).props('outline no-caps dense')
            ui.button(
                'Rotate API key',
                icon='key',
                on_click=partial(change, 'rotate_key', application.id),
            ).props('outline no-caps dense')


async def render_application_list(refresh):
    async with session_scope() as session:
        await require_admin(session)
        applications = list(
            await session.scalars(
                select(ConnectedApplication).order_by(
                    ConnectedApplication.created_at.desc()
                )
            )
        )
        audit = list(
            await session.scalars(
                select(ApplicationAudit)
                .order_by(ApplicationAudit.created_at.desc())
                .limit(20)
            )
        )

    with ui.row().classes('w-full items-center justify-between gap-2'):
        ui.label(f'{len(applications)} connected applications').classes(
            SECTION_LABEL_CLASS
        )

    if not applications:
        with ui.card().classes(f'w-full {CARD_CLASS} gap-1.5'):
            ui.label('No applications registered yet.').classes(
                f'text-base {PANEL_TITLE_CLASS}'
            )
            ui.label(
                'Use the form on the right to register your first application.'
            ).classes(f'text-sm {MUTED_TEXT_CLASS}')

    for application in applications:
        _application_card(application, refresh)

    if audit:
        with ui.card().classes(f'w-full {CARD_CLASS} gap-3'):
            ui.label('Recent administration activity').classes(
                f'text-lg {PANEL_TITLE_CLASS}'
            )
            ui.table(
                rows=[
                    {
                        'application': str(item.application_id),
                        'action': item.action,
                        'actor': item.actor_sub,
                        'time': item.created_at.isoformat(),
                    }
                    for item in audit
                ]
            ).props('flat bordered dense dark').classes('w-full')


@router.page('/applications')
async def applications_page():
    with app_layout(
        title='Connected applications',
        active_path='/admin/applications',
        area='Admin',
        nav_items=ADMIN_NAV_ITEMS,
    ):
        if not _require_admin():
            return
        async with session_scope() as session:
            try:
                await require_admin(session)
            except HTTPException:
                ui.label('Administrator access required.').classes(
                    'text-rose-400 text-base font-medium'
                )
                return
        ui.label(
            'Applications create charges. Users confirm payment in Leprechaun.'
        ).classes(f'text-sm {MUTED_TEXT_CLASS}')

        application_list = ui.refreshable(render_application_list)
        with ui.grid().classes(
            'w-full grid-cols-1 lg:grid-cols-3 gap-6 items-start'
        ):
            with ui.column().classes('w-full min-w-0 lg:col-span-2 gap-4'):
                await application_list(application_list.refresh)
            with ui.column().classes(
                'w-full min-w-0 lg:col-span-1 lg:sticky lg:top-24'
            ):
                registration_form(application_list.refresh)


def registration_form(refresh):
    with ui.card().classes(f'w-full {CARD_CLASS} gap-4'):
        ui.label('Register application').classes(
            f'text-lg {PANEL_TITLE_CLASS}'
        )
        name = (
            ui
            .input('Application name')
            .props('outlined dense dark')
            .classes('w-full')
        )
        slug = (
            ui
            .input('Application identifier', placeholder='my-game')
            .props('outlined dense dark')
            .classes('w-full font-mono')
        )
        website = (
            ui
            .input('Website URL', placeholder='https://game.example')
            .props('outlined dense dark')
            .classes('w-full')
        )
        account = (
            ui
            .input('Receiving ledger account UUID')
            .props('outlined dense dark')
            .classes('w-full font-mono')
        )
        ui.label(
            'Return and cancel URLs must belong to this website. '
            'The receiving account must already exist.'
        ).classes(f'text-xs {MUTED_TEXT_CLASS}')

        async def register():
            try:
                payload = ApplicationCreate(
                    name=name.value or '',
                    slug=slug.value or '',
                    website_url=website.value or '',
                    destination_account_id=account.value or '',
                )
                async with session_scope() as session:
                    _, key = await create_application(payload, session)
                show_key(key)
                name.value = slug.value = website.value = account.value = ''
                refresh()
            except ValidationError:
                ui.notify(
                    'Enter a name, lowercase identifier, '
                    'valid website and account UUID.',
                    type='negative',
                )
            except LedgerClientError:
                ui.notify(
                    'Receiving account was not found '
                    'or Ledger is unavailable.',
                    type='negative',
                )
            except IntegrityError:
                ui.notify(
                    'This application identifier is already registered.',
                    type='negative',
                )
            except HTTPException as exc:
                ui.notify(str(exc.detail), type='negative')

        ui.button(
            'Register application',
            icon='add',
            on_click=register,
        ).props('unelevated no-caps color=primary').classes(
            'w-full font-medium'
        )
