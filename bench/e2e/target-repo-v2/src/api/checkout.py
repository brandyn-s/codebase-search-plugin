from inventory.store import reserve_inventory


def submit_order(sku: str, quantity: int) -> str:
    if not reserve_inventory(sku, quantity):
        raise ValueError("inventory unavailable")
    return f"accepted:{sku}"
