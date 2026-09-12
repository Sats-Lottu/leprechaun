import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

import websockets
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, wait_exponential_jitter
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from pls.schemas import WalletResponse

log = logging.getLogger(__name__)

WSRetryExc = (ConnectionClosedError, ConnectionClosedOK, OSError)


def lnbits_ws_url(base_url: str, invoice_read_key: str) -> str:
    base_url = base_url.rstrip('/')
    if base_url.startswith('https://'):
        scheme = 'wss://'
        domain = base_url.removeprefix('https://')
    elif base_url.startswith('http://'):
        scheme = 'ws://'
        domain = base_url.removeprefix('http://')
    else:
        scheme = 'wss://'
        domain = base_url
    return f'{scheme}{domain}/api/v1/ws/{invoice_read_key}'


def _parse_event(raw: str) -> WalletResponse | None:
    try:
        return WalletResponse(**json.loads(raw))
    except ValidationError:
        log.debug(f'LNbits WS parse failures: {raw}')
        return None


@dataclass(frozen=True, slots=True)
class LNbitsWSBridgeConfig:
    ping_interval: float = 20.0
    ping_timeout: float = 20.0
    close_timeout: float = 10.0
    backoff_initial: float = 1.0
    backoff_max: float = 30.0


class LNbitsWSBridge:
    """Conecta no WS do LNbits e publica cada evento no broker
    (raw ou filtrado)."""

    def __init__(
        self,
        *,
        url: str,
        invoice_read_key: str,
        publisher,
        # FastStream publisher object: broker.publisher("queue-or-topic")
        event_handler: Callable[[WalletResponse], Awaitable[list | None]]
        | None = None,
        cfg: LNbitsWSBridgeConfig = None,
    ) -> None:
        self._url = url
        self._invoice_read_key = invoice_read_key
        self._publisher = publisher
        self._event_handler = event_handler
        self._cfg = LNbitsWSBridgeConfig() if cfg is None else cfg

        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(
                    self._run(), name='lnbits-ws-bridge'
                )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _run(self) -> None:
        # loop simples: tenacity cuida do retry
        await self._connect_and_consume()

    @retry(
        retry=retry_if_exception_type(WSRetryExc),
        wait=wait_exponential_jitter(
            initial=1.0, max=30.0
        ),  # pode parametrizar abaixo
        reraise=True,
    )
    async def _connect_and_consume(self) -> None:
        ws_url = lnbits_ws_url(self._url, self._invoice_read_key)
        log.info('LNbits WS connecting: %s', ws_url)

        async with websockets.connect(
            ws_url,
            ping_interval=self._cfg.ping_interval,
            ping_timeout=self._cfg.ping_timeout,
            close_timeout=self._cfg.close_timeout,
        ) as ws:
            log.info('LNbits WS connected')

            async for raw in ws:
                evt = _parse_event(raw)
                if evt is None:
                    continue

                # Publica no broker (RAW)
                # Você pode publicar dict, str, bytes; aqui envia dict:
                if self._event_handler is None:
                    log.info(
                        'Publishing LNbits WS Event to broker: %s',
                        evt.model_dump(),
                    )
                    await self._publisher.publish(evt.model_dump())
                    continue

                for out_event in await self._event_handler(evt) or []:
                    body = (
                        out_event.model_dump()
                        if hasattr(out_event, 'model_dump')
                        else out_event
                    )
                    log.info('Publishing domain payment event: %s', body)
                    await self._publisher.publish(body)
