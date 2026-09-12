import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from faststream import ContextRepo

from pls.lnbits_ws_bridge import LNbitsWSBridge
from pls.messaging import ws_publisher
from pls.provider import LNbitsClient, LNbitsConfig
from pls.reconciliation import (
    handle_lnbits_wallet_event,
    reconcile_pending_invoices_loop,
)


def create_lnbits_lifespan(cfg: LNbitsConfig):
    @asynccontextmanager
    async def lnbits_lifespan(context: ContextRepo) -> AsyncIterator[None]:
        client = LNbitsClient(cfg)
        await client.start()
        wallet = client.wallet()

        context.set_global("lnbits_wallet", wallet)

        ws_bridge = LNbitsWSBridge(
            url=cfg.base_url,
            invoice_read_key=wallet.invoice_read_key,
            publisher=ws_publisher,
            event_handler=handle_lnbits_wallet_event,
        )
        await ws_bridge.start()
        context.set_global("lnbits_ws_bridge", ws_bridge)

        reconciliation_task = asyncio.create_task(
            reconcile_pending_invoices_loop(wallet),
            name="lnbits-reconciliation",
        )
        context.set_global("lnbits_reconciliation_task", reconciliation_task)

        try:
            yield
        finally:
            reconciliation_task.cancel()
            with suppress(asyncio.CancelledError):
                await reconciliation_task
            await ws_bridge.stop()
            await client.stop()

    return lnbits_lifespan
