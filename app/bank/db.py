"""Connection helpers.

Every operation opens a fresh connection through HAProxy. That is slower than
a pool, but it means that after a failover the very next request is routed to
the new primary with no stale connections to clean up.
"""
import time

import psycopg

from . import config


def branch_dsn(branch: str, mode: str = "write", user: str | None = None,
               password: str | None = None, dbname: str | None = None) -> str:
    b = config.BRANCHES[branch]
    port = b["write_port"] if mode == "write" else b["read_port"]
    # For writes, make the driver itself refuse any server that is not a
    # primary. HAProxy's health checks normally guarantee this, but for a
    # moment after HAProxy restarts, or in the middle of a failover, a
    # connection can still land on a standby.
    role = " target_session_attrs=primary" if mode == "write" else ""
    return (
        f"host={config.HAPROXY_HOST} port={port} "
        f"dbname={dbname or config.BANK_DB} "
        f"user={user or config.BANK_USER} password={password or config.BANK_PASSWORD} "
        f"connect_timeout=3 client_encoding=utf8{role}"
    )


def connect_branch(branch: str, mode: str = "write", **kw) -> psycopg.Connection:
    """autocommit=True: we send BEGIN / PREPARE TRANSACTION / COMMIT PREPARED
    ourselves so that every step of two-phase commit is explicit in the code."""
    dsn = branch_dsn(branch, mode, **kw)
    attempts = 3 if mode == "write" else 1       # a retry gets routed to the next node
    for attempt in range(1, attempts + 1):
        try:
            conn = psycopg.connect(dsn, autocommit=True)
            break
        except psycopg.OperationalError:
            if attempt == attempts:
                raise
            time.sleep(0.1)
    # Never wait forever on a row locked by an in-doubt transaction or on a dead node.
    conn.execute("SET lock_timeout = '3s'")
    conn.execute("SET statement_timeout = '8s'")
    return conn


def connect_read(branch: str) -> psycopg.Connection:
    """Read from a replica; fall back to the primary if no replica is available."""
    try:
        return connect_branch(branch, "read")
    except psycopg.OperationalError:
        return connect_branch(branch, "write")


def connect_coord() -> psycopg.Connection:
    """The coordinator's durable log."""
    if config.COORD_LOG_BRANCH:          # replicated: lives in a branch cluster, follows its failovers
        return connect_branch(config.COORD_LOG_BRANCH, "write", dbname=config.COORD_LOG_DB)
    return psycopg.connect(config.COORD_DB_DSN, autocommit=True)
