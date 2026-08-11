from shipping.service import choose_shipping


def shipping_quote(account: dict) -> str:
    return choose_shipping(account)
