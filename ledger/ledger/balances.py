from ledger.models.tables import Account


def available_balance(account: Account) -> int:
    return account.balance - account.reserved_balance


def credit(account: Account, amount: int) -> None:
    account.balance += amount


def debit_available(account: Account, amount: int) -> None:
    account.balance -= amount


def reserve(account: Account, amount: int) -> None:
    account.reserved_balance += amount


def consume_reserved(account: Account, amount: int) -> None:
    account.reserved_balance -= amount
    account.balance -= amount


def release_reserved(account: Account, amount: int) -> None:
    account.reserved_balance -= amount
