import os
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool = None


def pool():
    global _pool
    if _pool is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is missing")
        _pool = ConnectionPool(
            conninfo=url,
            min_size=0,
            max_size=3,
            open=True,
            kwargs={"autocommit": True, "row_factory": dict_row},
            max_idle=120,
            timeout=10,
        )
    return _pool


@contextmanager
def connection():
    with pool().connection() as conn:
        yield conn
