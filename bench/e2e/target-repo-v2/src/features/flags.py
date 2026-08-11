def is_express_enabled(account: dict) -> bool:
    return account.get("plan") == "priority"
