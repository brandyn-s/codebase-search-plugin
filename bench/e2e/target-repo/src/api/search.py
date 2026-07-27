from db.query import execute_query


def search(term: str) -> list[dict]:
    return execute_query(f"SELECT * FROM records WHERE name = '{term}'")
