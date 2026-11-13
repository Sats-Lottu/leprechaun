import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from faststream.asgi import make_asyncapi_asgi
from faststream.specification import AsyncAPI
from nicegui import app as nicegui_app
from nicegui import ui
from starlette.middleware.sessions import SessionMiddleware

from hub.auth import callback, login, logout
from hub.ledger_client import ledger_http_lifespan
from hub.pages.admin import router as admin_router
from hub.pages.applications import router as applications_router
from hub.pages.index import router as index_router
from hub.pages.user import router as user_router
from hub.payment_recovery import recover_payments_loop
from hub.rabbitmq import broker
from hub.routes.checkout import router as checkout_router
from hub.settings import get_settings


@asynccontextmanager
async def broker_lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with broker, ledger_http_lifespan():
        await broker.start()
        task = asyncio.create_task(
            recover_payments_loop(), name='payment-recovery'
        )
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(lifespan=broker_lifespan)
settings = get_settings()
static_directory = Path(__file__).parent / 'static'
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET_KEY,
    https_only=settings.SESSION_HTTPS_ONLY,
)
app.mount('/docs/asyncapi', make_asyncapi_asgi(AsyncAPI(broker)))
app.include_router(checkout_router)
app.get('/auth/login', name='oidc_login')(login)
app.get(settings.OIDC_REDIRECT_PATH, name='oidc_callback')(callback)
app.get('/auth/logout', name='oidc_logout')(logout)
nicegui_app.add_static_files('/static', static_directory)

nicegui_app.include_router(router=index_router, tags=['index'])
nicegui_app.include_router(router=user_router, tags=['user'])
nicegui_app.include_router(router=admin_router, tags=['admin'])
nicegui_app.include_router(router=applications_router, tags=['applications'])


ui.run_with(
    app,
    title='Leprechaun',
    favicon=static_directory / 'leprechaun-logo.png',
    storage_secret=settings.HUB_STORAGE_SECRET,
)
