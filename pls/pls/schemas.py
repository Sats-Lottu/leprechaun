from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from pls.settings import get_settings

settings = get_settings()

# =========================
# Message Envelope (domínio)
# =========================


class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    type: str
    occurred_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    producer: str = settings.SERVICE_NAME
    schema_version: int = settings.SCHEMA_VERSION

    correlation_id: str = Field(default_factory=lambda: str(uuid4()))
    causation_id: str | None = None
    idempotency_key: str | None = None


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: Meta
    data: dict[str, Any]


# =========================
# Commands / Events (domínio)
# =========================


class CreateInvoiceData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    amount_msat: int = Field(gt=0)
    memo: str = "deposit"
    expires_in_sec: int = Field(default=900, gt=0)


class PayInvoiceData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_request: str
    user_id: str | None = None
    amount_msat: int | None = Field(default=None, gt=0)


class PayLNURLData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lnurl: str
    amount_msat: int = Field(gt=0)
    user_id: str | None = None
    comment: str = ""


class InvoiceCreatedData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    invoice_id: str
    payment_request: str
    amount_msat: int
    expires_at: str

    @staticmethod
    def from_lnbits_payment(
        *,
        user_id: str,
        amount_msat: int,
        expires_in_sec: int,
        payment: "Payment",
    ) -> "InvoiceCreatedData":
        """
        Constrói o evento de domínio a partir do retorno do LNbits.
        - invoice_id: preferimos checking_id, depois payment_hash, se não
          gera um id local.
        - payment_request: preferimos payment_request, depois bolt11.
        - expires_at: calculado localmente (LNbits pode não devolver).
        """
        invoice_id = (
            payment.checking_id
            or payment.payment_hash
            or f"inv_{uuid4().hex[:10]}"
        )
        payment_request = payment.payment_request or payment.bolt11 or ""
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(expires_in_sec))
        ).isoformat()

        return InvoiceCreatedData(
            user_id=user_id,
            invoice_id=invoice_id,
            payment_request=payment_request,
            amount_msat=amount_msat,
            expires_at=expires_at,
        )


class PaymentReceivedData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    invoice_id: str
    payment_hash: str
    amount_msat: int
    paid_at: str


class PaymentSentData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    payment_hash: str | None = None
    checking_id: str | None = None
    amount_msat: int | None = None
    paid_at: str


# ================================================
# LNbits API Schemas (externo; payload variável)
# ================================================


class Failed(BaseModel):
    model_config = ConfigDict(extra="ignore")
    detail: str
    status: str = "failed"


class Token(BaseModel):
    model_config = ConfigDict(extra="ignore")
    access_token: str


class AccountWallet(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    inkey: str
    adminkey: str


class Account(BaseModel):
    model_config = ConfigDict(extra="ignore")
    wallets: list[AccountWallet] = Field(default_factory=list)


class Payment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    payment_hash: str | None = None
    checking_id: str | None = None
    bolt11: str | None = None
    payment_request: str | None = None
    amount: int | None = None
    paid: bool | None = None
    pending: bool | None = None
    details: Any | None = None

    @property
    def bolt11_str(self) -> str:
        return self.payment_request or self.bolt11 or ""

    def best_payment_request(self) -> str:
        return self.payment_request or self.bolt11 or ""

    def best_invoice_id(self) -> str:
        return (
            self.checking_id or self.payment_hash or f"inv_{uuid4().hex[:10]}"
        )


class PaymentDecoded(BaseModel):
    model_config = ConfigDict(extra="ignore")
    payment_hash: str | None = None
    description: str | None = None
    amount_msat: int | None = None
    expiry: int | None = None


class LNURLDecoded(BaseModel):
    model_config = ConfigDict(extra="ignore")
    callback: str
    description_hash: str | None = None
    description: str | None = None


class CreateInvoice(BaseModel):
    """
    Compatível com o endpoint padrão do LNbits:
    POST /api/v1/payments (out=false)
    """

    model_config = ConfigDict(extra="ignore")
    out: bool = False
    amount: int  # satoshis
    memo: str = ""
    expiry: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class WalletPaymentEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str
    checking_id: str | None = None
    payment_hash: str | None = None
    preimage: str | None = None
    payment_request: str | None = None
    bolt11: str | None = None
    amount: int
    fee: int
    memo: str | None = None
    time: str
    created_at: str
    updated_at: str


class WalletResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    payment: WalletPaymentEvent
    wallet_balance: int
