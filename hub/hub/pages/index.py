from nicegui import APIRouter

from hub.pages.user import render_wallet_page

router = APIRouter()


@router.page('/')
async def page():
    await render_wallet_page(active_path='/')
