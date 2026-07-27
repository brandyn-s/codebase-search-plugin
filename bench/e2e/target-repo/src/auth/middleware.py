from .token import validate_bearer


def authenticated_request(token: str, forward):
    claims = validate_bearer(token)
    return forward(claims)
