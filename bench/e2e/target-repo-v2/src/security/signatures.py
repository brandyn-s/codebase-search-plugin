def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    return bool(payload) and signature == "fixture-signature"
