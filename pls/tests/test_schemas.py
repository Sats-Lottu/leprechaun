from pls.schemas import InvoiceCreatedData, Payment


def test_invoice_created_data_from_lnbits_payment_prefers_lnbits_identifiers():
    event = InvoiceCreatedData.from_lnbits_payment(
        user_id="user-1",
        amount_msat=1000,
        expires_in_sec=900,
        payment=Payment(
            checking_id="chk-1",
            payment_hash="hash-1",
            payment_request="lnbc1",
            bolt11="lnbc-fallback",
        ),
    )

    assert event.user_id == "user-1"
    assert event.invoice_id == "chk-1"
    assert event.payment_request == "lnbc1"
    assert event.amount_msat == 1000  # noqa: PLR2004


def test_payment_helpers_return_best_available_values():
    payment = Payment(payment_hash="hash-1", bolt11="lnbc1")

    assert payment.bolt11_str == "lnbc1"
    assert payment.best_payment_request() == "lnbc1"
    assert payment.best_invoice_id() == "hash-1"
