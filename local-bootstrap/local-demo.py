"""Development only: exercise simulated payments. Never run in production."""

import argparse
import json
import os
from urllib.request import Request, urlopen
from uuid import uuid4

HUB = "http://localhost:8000"
LNBITS = "http://localhost:5000"


def request(base, method, path, payload=None, headers=None):
    req = Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def simulator():
    auth = request(
        LNBITS,
        "POST",
        "/api/v1/auth",
        {
            "username": "localadmin",
            "password": "local-lnbits-password",
        },
    )
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    account = request(LNBITS, "GET", "/api/v1/auth", headers=headers)
    wallet = next(
        (w for w in account["wallets"] if w["name"] == "Local simulator"),
        None,
    )
    if wallet is None:
        wallet = request(
            LNBITS, "POST", "/api/v1/wallet", {"name": "Local simulator"}, headers
        )
        request(
            LNBITS,
            "PUT",
            "/users/api/v1/balance",
            {"id": wallet["id"], "amount": 900_000},
            headers,
        )
    return {"X-Api-Key": wallet["adminkey"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    checkout = commands.add_parser("checkout")
    checkout.add_argument("--sats", type=int, default=100)
    pay = commands.add_parser("pay-checkout")
    pay.add_argument("session_id")
    invoice = commands.add_parser("withdrawal-invoice")
    invoice.add_argument("--sats", type=int, default=10)
    args = parser.parse_args()
    headers = {}
    if args.command in {"checkout", "pay-checkout"}:
        key = os.environ.get("LEPRECHAUN_API_KEY")
        if not key:
            parser.error("Set LEPRECHAUN_API_KEY from the application admin screen")
        headers = {"Authorization": f"Bearer {key}"}
    if args.command == "checkout":
        result = request(
            HUB,
            "POST",
            "/api/checkout/sessions",
            {
                "game_id": os.environ.get("LEPRECHAUN_APPLICATION_SLUG", "local-demo"),
                "order_id": str(uuid4()),
                "destination_account_id": "00000000-0000-0000-0000-000000000200",
                "amount_sats": args.sats,
                "description": "Local simulated purchase",
                "return_url": HUB + "/user/wallet",
                "cancel_url": HUB + "/",
            },
            headers,
        )
        print(result["checkout_url"])
    elif args.command == "pay-checkout":
        result = request(HUB, "GET", "/api/checkout/sessions/" + args.session_id, headers=headers)
        if not result["payment_request"]:
            parser.error("Confirm payment in the checkout to generate its invoice")
        request(
            LNBITS,
            "POST",
            "/api/v1/payments",
            {"out": True, "bolt11": result["payment_request"]},
            simulator(),
        )
        print("Simulated payment sent. Refresh checkout to settle.")
    else:
        result = request(
            LNBITS,
            "POST",
            "/api/v1/payments",
            {"out": False, "amount": args.sats, "memo": "Local withdrawal"},
            simulator(),
        )
        print(result["bolt11"])


if __name__ == "__main__":
    main()
