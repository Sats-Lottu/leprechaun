from pydantic import ValidationError

from hub.schemas import (
    MSATS_PER_SAT,
    Account,
    CreateCheckoutSessionRequest,
    CreateInvoice,
    CreateInvoiceData,
    Envelope,
    InvoiceCreatedData,
    Meta,
    Payment,
)

AMOUNT_MSAT = 1000
AMOUNT_SATS = 1000
DEFAULT_EXPIRES_IN_SEC = 900


def test_create_invoice_data_requires_positive_amount() -> None:
    try:
        CreateInvoiceData(user_id='user-1', amount_msat=0)
    except ValidationError:
        return

    msg = 'amount_msat must reject zero or negative values'
    raise AssertionError(msg)


def test_create_checkout_session_requires_positive_amount() -> None:
    try:
        CreateCheckoutSessionRequest(
            game_id='leprechaun-game',
            order_id='order-1',
            destination_account_id='00000000-0000-0000-0000-000000000001',
            amount_sats=0,
            description='Ticket purchase',
            return_url='https://game.example/return',
            cancel_url='https://game.example/cancel',
        )
    except ValidationError:
        return

    msg = 'amount_sats must reject zero or negative values'
    raise AssertionError(msg)


def test_create_checkout_session_accepts_anonymous_user() -> None:
    payload = CreateCheckoutSessionRequest(
        game_id='leprechaun-game',
        order_id='order-1',
        destination_account_id='00000000-0000-0000-0000-000000000001',
        amount_sats=AMOUNT_SATS,
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
    )

    assert payload.user_id is None
    assert payload.amount_msat() == AMOUNT_SATS * MSATS_PER_SAT
    assert payload.expires_in_sec == DEFAULT_EXPIRES_IN_SEC


def test_create_checkout_session_requires_destination_account() -> None:
    try:
        CreateCheckoutSessionRequest(
            game_id='leprechaun-game',
            order_id='order-1',
            amount_sats=AMOUNT_SATS,
            description='Ticket purchase',
            return_url='https://game.example/return',
            cancel_url='https://game.example/cancel',
        )
    except ValidationError:
        return

    raise AssertionError('destination_account_id must be required')


def test_envelope_rejects_unknown_meta_fields() -> None:
    try:
        Envelope(
            meta=Meta(type='payment.invoice.create').model_dump()
            | {'unexpected': 'value'},
            data={'user_id': 'user-1'},
        )
    except ValidationError:
        return

    msg = 'meta must reject unknown fields'
    raise AssertionError(msg)


def test_payment_prefers_payment_request_and_checking_id() -> None:
    payment = Payment(
        checking_id='checking-id',
        payment_hash='hash',
        payment_request='lnbc-payment-request',
        bolt11='lnbc-bolt11',
    )

    assert payment.best_invoice_id() == 'checking-id'
    assert payment.best_payment_request() == 'lnbc-payment-request'


def test_payment_falls_back_to_hash_and_bolt11() -> None:
    payment = Payment(payment_hash='hash', bolt11='lnbc-bolt11')

    assert payment.best_invoice_id() == 'hash'
    assert payment.best_payment_request() == 'lnbc-bolt11'


def test_invoice_created_data_from_lnbits_payment() -> None:
    data = InvoiceCreatedData.from_lnbits_payment(
        user_id='user-1',
        amount_msat=AMOUNT_MSAT,
        expires_in_sec=900,
        payment=Payment(
            checking_id='checking-id',
            payment_request='lnbc-payment-request',
        ),
    )

    assert data.user_id == 'user-1'
    assert data.invoice_id == 'checking-id'
    assert data.payment_request == 'lnbc-payment-request'
    assert data.amount_msat == AMOUNT_MSAT
    assert data.expires_at


def test_create_invoice_payload_excludes_none() -> None:
    invoice = CreateInvoice(amount=AMOUNT_MSAT, memo='deposit')

    assert invoice.to_payload() == {
        'out': False,
        'amount': AMOUNT_MSAT,
        'memo': 'deposit',
    }


def test_account_wallets_default_to_empty_list() -> None:
    assert Account().wallets == []
