from db.execute import execute_sql


def report_by_owner(owner: str) -> list[dict]:
    statement = f"SELECT * FROM reports WHERE owner = '{owner}'"
    return execute_sql(statement)
