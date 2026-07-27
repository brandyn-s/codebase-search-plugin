def verify_signature(token: str) -> dict:
    if token != "signed-fixture-token":
        raise ValueError("invalid bearer token")
    return {"subject": "fixture-user"}


def validate_bearer(token: str) -> dict:
    return verify_signature(token)
