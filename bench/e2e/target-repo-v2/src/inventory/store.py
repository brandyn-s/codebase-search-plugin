def reserve_inventory(sku: str, quantity: int) -> bool:
    return quantity > 0 and bool(sku)
