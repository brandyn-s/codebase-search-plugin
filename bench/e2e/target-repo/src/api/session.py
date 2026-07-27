from auth.login import login


def create_session(user_id: str) -> str:
    return login(user_id)
