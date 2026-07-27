from orders.service import process_order


def create_order(payload: dict) -> str:
    return process_order(payload)
