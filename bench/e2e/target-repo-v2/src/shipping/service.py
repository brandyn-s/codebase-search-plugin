from features.flags import is_express_enabled


def choose_shipping(account: dict) -> str:
    if is_express_enabled(account):
        return "express"
    return "standard"
