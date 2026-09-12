import pytest

from pls.schemas import CreateInvoice, Failed, Payment, PaymentDecoded


@pytest.mark.asyncio
async def test_lnbits_wallet_creates_invoice_against_container(wallet):
    invoice = await wallet.create_invoice(
        CreateInvoice(amount=1, memo="integration-test", expiry=300)
    )

    assert isinstance(invoice, Payment)
    assert not isinstance(invoice, Failed)
    assert invoice.best_invoice_id()
    assert invoice.best_payment_request().startswith("ln")


@pytest.mark.asyncio
async def test_lnbits_wallet_decodes_invoice_against_container(wallet):
    invoice = await wallet.create_invoice(
        CreateInvoice(amount=1, memo="decode-test", expiry=300)
    )
    assert isinstance(invoice, Payment)

    decoded = await wallet.decode_invoice(invoice.best_payment_request())

    assert isinstance(decoded, PaymentDecoded)
    assert not isinstance(decoded, Failed)
    assert decoded.payment_hash


@pytest.mark.asyncio
async def test_lnbits_wallet_gets_payment_against_container(wallet):
    invoice = await wallet.create_invoice(
        CreateInvoice(amount=1, memo="get-payment-test", expiry=300)
    )
    assert isinstance(invoice, Payment)

    payment = await wallet.get_payment(invoice.best_invoice_id())

    assert isinstance(payment, Payment)
    assert not isinstance(payment, Failed)
    assert payment.best_invoice_id()
