import asyncio
import json
import logging
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from pls.schemas import (
    Account,
    CreateInvoice,
    Failed,
    LNURLDecoded,
    Payment,
    PaymentDecoded,
    Token,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# =========================
# Config
# =========================


@dataclass(frozen=True, slots=True)
class LNbitsConfig:
    base_url: str
    wallet_id: str | None = None
    username: str = ''
    password: str = ''
    invoice_read_key: str = ''
    admin_key: str = ''

    invoice_ttl_sec: int = 900
    timeout_sec: float = 20.0


# =========================
# Helpers
# =========================


def normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def failed_from_response(resp: httpx.Response, default_detail: str) -> Failed:
    try:
        data = resp.json()
        if isinstance(data, dict):
            if "detail" in data or "status" in data:
                return Failed(**data)
            return Failed(
                detail=json.dumps(data, ensure_ascii=False), status="failed"
            )
        return Failed(detail=str(data), status="failed")
    except ValidationError:
        return Failed(detail=(resp.text or default_detail), status="failed")


# =========================
# LNbits Auth (simplified)
# =========================


class LNbitsAuth:
    """Somente auth + fetch de Account. Não instancia wallets."""

    def __init__(
        self, http: httpx.AsyncClient, username: str, password: str
    ) -> None:
        self._http = http
        self._username = username
        self._password = password

    async def login(self) -> Token | Failed:
        payload = {"username": self._username, "password": self._password}
        try:
            resp = await self._http.post("/api/v1/auth", json=payload)

            if resp.status_code != HTTPStatus.OK:
                return failed_from_response(
                    resp, f"login failed ({resp.status_code})"
                )

            data = resp.json()
            if not isinstance(data, dict):
                return Failed(detail="Invalid response type!", status="failed")

            try:
                return Token(**data)
            except ValidationError:
                return Failed(
                    detail="Invalid response schema!", status="failed"
                )

        except httpx.TimeoutException:
            return Failed(detail="Timeout!", status="failed")
        except httpx.ConnectError:
            return Failed(detail="Connect error!", status="failed")

    async def get_account_info(self, token: Token) -> Account | Failed:
        headers = {"Authorization": f"Bearer {token.access_token}"}
        try:
            resp = await self._http.get("/api/v1/auth", headers=headers)

            if resp.status_code != HTTPStatus.OK:
                return failed_from_response(
                    resp, f"get_account_info failed ({resp.status_code})"
                )

            data = resp.json()
            if not isinstance(data, dict):
                return Failed(detail="Invalid response type!", status="failed")

            try:
                return Account(**data)
            except ValidationError:
                return Failed(
                    detail="Invalid response schema!", status="failed"
                )

        except httpx.TimeoutException:
            return Failed(detail="Timeout!", status="failed")
        except httpx.ConnectError:
            return Failed(detail="Connect error!", status="failed")


# =========================
# CallSpec (reduz args e evita PLR0913)
# =========================


@dataclass(frozen=True, slots=True)
class CallSpec:
    method: str
    path: str
    headers: dict[str, str]
    model: type[BaseModel]

    json_body: dict[str, Any] | None = None
    ok: set[int] = field(default_factory=lambda: {HTTPStatus.OK})
    default_error: str = "Request failed"


# =========================
# LNbitsWallet
# =========================


class LNbitsWallet:
    """Adapter LNbits (HTTP + WS no-op).

    - HTTP centralizado em _call() que sempre retorna Model|Failed (nunca dict)
    - stop_ws() existe (no-op) para não quebrar o lifecycle
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        invoice_read_key: str,
        admin_key: str,
        wallet_id: str | None = None,
        cfg: LNbitsConfig | None = None,
    ) -> None:
        self._http = http
        self.base_url = (
            str(http.base_url) if http.base_url is not None else None
        )
        self.invoice_read_key = invoice_read_key
        self.admin_key = admin_key
        self.wallet_id = wallet_id
        self._cfg = cfg

        # WS placeholders (se você implementar depois)
        self._ws_task: asyncio.Task | None = None
        self._ws_lock = asyncio.Lock()

    # ---------- Headers (centralizados) ----------

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        return {"X-Api-Key": api_key}

    @property
    def headers_invoice(self) -> dict[str, str]:
        return self._headers(self.invoice_read_key)

    @property
    def headers_admin(self) -> dict[str, str]:
        return self._headers(self.admin_key)

    # ---------- HTTP Core (Model|Failed, sem dict) ----------

    async def _call(self, spec: CallSpec) -> BaseModel | Failed:
        result: BaseModel | Failed

        try:
            resp = await self._http.request(
                spec.method,
                spec.path,
                headers=spec.headers,
                json=spec.json_body,
            )

            if resp.status_code not in spec.ok:
                result = failed_from_response(
                    resp, f"{spec.default_error} ({resp.status_code})"
                )
            else:
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    result = Failed(
                        detail="Invalid JSON response!", status="failed"
                    )
                else:
                    if not isinstance(data, dict):
                        result = Failed(
                            detail="Invalid response type!", status="failed"
                        )
                    else:
                        try:
                            result = spec.model(**data)
                        except ValidationError:
                            result = Failed(
                                detail="Invalid response schema!",
                                status="failed",
                            )

        except httpx.TimeoutException:
            result = Failed(detail="Timeout!", status="failed")
        except httpx.ConnectError:
            result = Failed(detail="Connect error!", status="failed")

        return result

    # ---------- LNbits HTTP Wrappers (thin) ----------

    async def decode_lnurl(self, lnurl: str) -> LNURLDecoded | Failed:
        out = await self._call(
            CallSpec(
                method="GET",
                path=f"/api/v1/lnurlscan/{lnurl}",
                headers=self.headers_admin,
                json_body=None,
                ok={HTTPStatus.OK},
                model=LNURLDecoded,
                default_error="lnurlscan failed",
            )
        )
        return (
            out
            if isinstance(out, (LNURLDecoded, Failed))
            else Failed(detail="Unexpected response!", status="failed")
        )

    async def pay_lnurl(
        self, lnurl: str, amount: int, comment: str = ""
    ) -> Payment | Failed:
        decoded = await self.decode_lnurl(lnurl)
        if isinstance(decoded, Failed):
            return decoded

        base = {
            "callback": decoded.callback,
            "amount": amount * 1000,  # sat -> msat
            "comment": comment,
        }
        optional = {
            "description_hash": getattr(decoded, "description_hash", None),
            "description": getattr(decoded, "description", None),
        }
        payload: dict[str, Any] = {
            **base,
            **{k: v for k, v in optional.items() if v},
        }

        out = await self._call(
            CallSpec(
                method="POST",
                path="/api/v1/payments/lnurl",
                headers=self.headers_admin,
                json_body=payload,
                ok={HTTPStatus.OK},
                model=Payment,
                default_error="LNURL payment failed",
            )
        )
        return (
            out
            if isinstance(out, (Payment, Failed))
            else Failed(detail="Unexpected response!", status="failed")
        )

    async def create_invoice(
        self, invoice_data: CreateInvoice
    ) -> Payment | Failed:
        out = await self._call(
            CallSpec(
                method="POST",
                path="/api/v1/payments",
                headers=self.headers_invoice,
                json_body=invoice_data.to_payload(),
                ok={HTTPStatus.OK, HTTPStatus.CREATED},
                model=Payment,
                default_error="create_invoice failed",
            )
        )
        return (
            out
            if isinstance(out, (Payment, Failed))
            else Failed(detail="Unexpected response!", status="failed")
        )

    async def pay_invoice(
        self, bolt11: str, out: bool = True
    ) -> Payment | Failed:
        outp = await self._call(
            CallSpec(
                method="POST",
                path="/api/v1/payments",
                headers=self.headers_admin,
                json_body={"out": out, "bolt11": bolt11},
                ok={HTTPStatus.OK, HTTPStatus.CREATED},
                model=Payment,
                default_error="pay_invoice failed",
            )
        )
        return (
            outp
            if isinstance(outp, (Payment, Failed))
            else Failed(detail="Unexpected response!", status="failed")
        )

    async def get_payment(self, checking_id: str) -> Payment | Failed:
        out = await self._call(
            CallSpec(
                method="GET",
                path=f"/api/v1/payments/{checking_id}",
                headers=self.headers_invoice,
                ok={HTTPStatus.OK},
                model=Payment,
                default_error="get_payment failed",
            )
        )
        return (
            out
            if isinstance(out, (Payment, Failed))
            else Failed(detail="Unexpected response!", status="failed")
        )

    async def decode_invoice(self, invoice: str) -> PaymentDecoded | Failed:
        out = await self._call(
            CallSpec(
                method="POST",
                path="/api/v1/payments/decode",
                headers=self.headers_invoice,
                json_body={"data": invoice},
                ok={HTTPStatus.OK},
                model=PaymentDecoded,
                default_error="decode_invoice failed",
            )
        )
        return (
            out
            if isinstance(out, (PaymentDecoded, Failed))
            else Failed(detail="Unexpected response!", status="failed")
        )

    # ---------- WS lifecycle (no-op seguro) ----------

    async def stop_ws(self) -> None:
        """No-op seguro. Mantém compatibilidade com lifecycle.
        Se você implementar WS depois, finalize task e feche conexões aqui.
        """
        async with self._ws_lock:
            task = self._ws_task
            self._ws_task = None

        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# =========================
# LNbitsClient (FastStream/FastAPI lifespan)
# =========================


class LNbitsClient:
    """Gerencia lifecycle (HTTP pool + resolve wallet). Use no lifespan."""

    def __init__(self, cfg: LNbitsConfig) -> None:
        self.cfg = cfg
        self.http: httpx.AsyncClient | None = None
        self.account_info: Account | None = None
        self._wallet: LNbitsWallet | None = None
        self._token: Token | None = None

    async def start(self) -> None:
        if not self.cfg.invoice_read_key and not (
            self.cfg.username and self.cfg.password
        ):
            raise RuntimeError(
                'Config requires API keys or LNbits credentials'
            )
        self.http = httpx.AsyncClient(
            base_url=normalize_base_url(self.cfg.base_url),
            timeout=self.cfg.timeout_sec,
            headers={"accept": "application/json"},
        )

        if self.cfg.invoice_read_key:
            self._wallet = LNbitsWallet(
                http=self.http,
                invoice_read_key=self.cfg.invoice_read_key,
                admin_key=self.cfg.admin_key,
                wallet_id=self.cfg.wallet_id,
                cfg=self.cfg,
            )
            return

        auth = LNbitsAuth(self.http, self.cfg.username, self.cfg.password)

        token = await auth.login()
        if isinstance(token, Failed):
            raise TypeError(f"Falha no login do LNbits: {token.detail}")
        self._token = token

        info = await auth.get_account_info(token)
        if isinstance(info, Failed):
            raise TypeError(
                f"Falha ao obter account info do LNbits: {info.detail}"
            )
        self.account_info = info

        if not info.wallets:
            raise RuntimeError(
                "Falha ao obter wallets do LNbits (lista vazia)."
            )

        wallet = next(
            (
                item
                for item in info.wallets
                if item.id == self.cfg.wallet_id
            ),
            None,
        )
        if not self.cfg.wallet_id and len(info.wallets) == 1:
            wallet = info.wallets[0]
        if wallet is None:
            raise RuntimeError(
                "Falha ao localizar wallet do LNbits para "
                f"wallet_id={self.cfg.wallet_id}."
            )
        self._wallet = LNbitsWallet(
            http=self.http,
            invoice_read_key=wallet.inkey,
            admin_key=wallet.adminkey,
            wallet_id=wallet.id,
            cfg=self.cfg,
        )

    async def stop(self) -> None:
        if self._wallet is not None:
            await self._wallet.stop_ws()
            self._wallet = None

        if self.http is not None:
            await self.http.aclose()
            self.http = None

        self.account_info = None
        self._token = None

    def wallet(self) -> LNbitsWallet:
        if self._wallet is None:
            raise RuntimeError(
                "LNbitsClient não inicializado. Use lifespan para"
                " chamar start()."
            )
        return self._wallet
