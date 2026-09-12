"""Development only: initialize FakeWallet. Never run in production."""

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE_URL = "http://lnbits:5000"
CREDENTIALS = {"username": "localadmin", "password": "local-lnbits-password"}


def request(method, path, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(
        BASE_URL + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    with urlopen(req, timeout=30) as response:
        return json.load(response)


try:
    auth = request("POST", "/api/v1/auth", CREDENTIALS)
except HTTPError as exc:
    if exc.code not in {400, 401, 403} and not (
        exc.code == 307 and exc.headers.get("Location", "").endswith("/first_install")
    ):
        raise
    auth = request(
        "PUT",
        "/api/v1/auth/first_install",
        {
            **CREDENTIALS,
            "password_repeat": CREDENTIALS["password"],
        },
    )
account = request("GET", "/api/v1/auth", token=auth["access_token"])
config_path = Path("/local/lnbits.json")
saved_id = (
    json.loads(config_path.read_text())["LNBITS_WALLET_ID"]
    if config_path.exists()
    else None
)
wallet = next(
    (item for item in account["wallets"] if item["id"] == saved_id),
    None,
)
if wallet is None:
    if len(account["wallets"]) != 1:
        raise RuntimeError("Cannot choose the service wallet unambiguously")
    wallet = account["wallets"][0]
config_path.write_text(
    json.dumps(
        {
            "LNBITS_WALLET_ID": wallet["id"],
            "LNBITS_INVOICE_READ_KEY": wallet["inkey"],
            "LNBITS_ADMIN_KEY": wallet["adminkey"],
        }
    ),
    encoding="utf-8",
)
print("Local LNbits account ready (FakeWallet).")
