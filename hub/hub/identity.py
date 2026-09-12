from uuid import NAMESPACE_URL, UUID, uuid5


def ledger_owner_id(user_sub: str) -> UUID:
    """Use the same owner identity for ledger accounts and payment events."""
    return uuid5(NAMESPACE_URL, f'leprechaun:hub:user:{user_sub}')
