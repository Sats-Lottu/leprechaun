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
from hub.layout import ADMIN_NAV_ITEMS, CARD_CLASS, app_layout
from hub.ledger_client import LedgerClientError
from hub.models.database import session_scope
from hub.models.tables import ApplicationAudit, ConnectedApplication
from hub.pages.admin import _require_admin

router = APIRouter(prefix='/admin')


def show_key(key: str):
    # Keep the one-time secret outside the list refreshed after rotation.
    with ui.context.client.content:
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-xl'):
            ui.label('Save the application API key').classes(
                'text-xl font-bold'
            )
            ui.label(
                'This key is shown only now. '
                'Store it on your application server.'
            )
            ui.input(
                'API key',
                value=key,
                password=True,
                password_toggle_button=True,
            ).props('readonly').classes('w-full')
            ui.button(
                'Copy key',
                icon='content_copy',
                on_click=lambda: ui.clipboard.write(key),
            )
            ui.button('Close', on_click=dialog.close)
    dialog.open()


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
    ui.label(f'{len(applications)} connected applications').classes('text-lg')
    if not applications:
        ui.label('Register your first application below.')
    for application in applications:
        with ui.card().classes(f'w-full {CARD_CLASS}'):
            ui.label(application.name).classes('text-lg font-bold')
            ui.label(
                f'{application.slug} · '
                f'{"Active" if application.is_active else "Disabled"}'
            )
            ui.label(application.website_url)
            ui.label(
                f'Receiving account: {application.destination_account_id}'
            ).classes('break-all')
            ui.label(f'Key: {application.key_prefix}…')
            with ui.row():

                async def change(action, application_id):
                    try:
                        async with session_scope() as session:
                            key = await change_application(
                                application_id, action, session
                            )
                        if key:
                            show_key(key)
                        ui.notify('Application updated.', type='positive')
                        refresh()
                    except HTTPException as exc:
                        ui.notify(str(exc.detail), type='negative')

                action = 'disabled' if application.is_active else 'enabled'
                ui.button(
                    'Disable' if application.is_active else 'Enable',
                    icon='power_settings_new',
                    on_click=partial(change, action, application.id),
                )
                ui.button(
                    'Rotate API key',
                    icon='key',
                    on_click=partial(change, 'rotate_key', application.id),
                )
    if audit:
        ui.label('Recent administration activity').classes('text-lg')
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
        ).classes('w-full')


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
                ui.label('Administrator access required.')
                return
        ui.label(
            'Applications create charges. Users confirm payment in Leprechaun.'
        ).classes('text-white/70')

        application_list = ui.refreshable(render_application_list)
        await application_list(application_list.refresh)
        registration_form(application_list.refresh)


def registration_form(refresh):
    with ui.card().classes(f'w-full {CARD_CLASS}'):
        ui.label('Register application').classes('text-xl')
        name = ui.input('Application name').classes('w-full')
        slug = ui.input(
            'Application identifier', placeholder='my-game'
        ).classes('w-full')
        website = ui.input(
            'Website URL', placeholder='https://game.example'
        ).classes('w-full')
        account = ui.input('Receiving ledger account UUID').classes('w-full')
        ui.label(
            'Return and cancel URLs must belong to this website. '
            'The receiving account must already exist.'
        )

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

        ui.button('Register application', icon='add', on_click=register)
