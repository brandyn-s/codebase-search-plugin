from .audit import record_login


def login(user_id: str) -> str:
    record_login(user_id)
    return f"session:{user_id}"
