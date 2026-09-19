import pytest

from hub.pages import index


@pytest.mark.asyncio
async def test_index_page_renders_wallet(monkeypatch) -> None:
    active_paths = []

    async def fake_render_wallet_page(*, active_path: str) -> None:
        active_paths.append(active_path)

    monkeypatch.setattr(
        index,
        'render_wallet_page',
        fake_render_wallet_page,
    )

    await index.page()

    assert active_paths == ['/']
