from security.signatures import verify_webhook_signature


def accept_webhook(payload: bytes, signature: str) -> str:
    if not verify_webhook_signature(payload, signature):
        raise ValueError("invalid webhook signature")
    return "accepted"
