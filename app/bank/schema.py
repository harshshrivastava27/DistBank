"""Creates the databases, tables and seed data. Safe to run repeatedly."""
import time

from psycopg import sql

from . import config, db

BRANCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          text PRIMARY KEY,
    owner       text NOT NULL,
    balance     numeric(14,2) NOT NULL CHECK (balance >= 0),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ledger (
    id          bigserial PRIMARY KEY,
    txn_id      text NOT NULL,
    account_id  text NOT NULL REFERENCES accounts(id),
    delta       numeric(14,2) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
"""

COORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS txn_log (
    txn_id        text PRIMARY KEY,
    kind          text NOT NULL,            -- 'local' or '2pc'
    from_account  text NOT NULL,
    to_account    text NOT NULL,
    amount        numeric(14,2) NOT NULL,
    participants  text[] NOT NULL,          -- branches involved
    state         text NOT NULL,            -- STARTED, COMMITTING, COMMITTED, ABORTING, ABORTED
    note          text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS meta (
    key    text PRIMARY KEY,
    value  text NOT NULL
);
CREATE INDEX IF NOT EXISTS txn_log_created ON txn_log (created_at DESC);
CREATE INDEX IF NOT EXISTS txn_log_open ON txn_log (state)
    WHERE state IN ('STARTED', 'COMMITTING', 'ABORTING');
"""


def init_branch(branch: str) -> None:
    # 1. As superuser: create the application role and database.
    with db.connect_branch(branch, "write", user=config.PG_SUPERUSER,
                           password=config.PG_SUPERUSER_PASSWORD, dbname="postgres") as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (config.BANK_USER,)).fetchone():
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(config.BANK_USER), sql.Literal(config.BANK_PASSWORD)))
            conn.execute(sql.SQL("GRANT pg_monitor TO {}").format(
                sql.Identifier(config.BANK_USER)))
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (config.BANK_DB,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(config.BANK_DB), sql.Identifier(config.BANK_USER)))

    # 2. As the application user: tables and seed rows.
    with db.connect_branch(branch, "write") as conn:
        conn.execute(BRANCH_SCHEMA)
        if conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0:
            with conn.transaction():
                conn.execute("INSERT INTO accounts (id, owner, balance) VALUES (%s, %s, %s)",
                             (config.vault_of(branch), f"{config.BRANCHES[branch]['name']} vault",
                              config.VAULT_BALANCE))
                for i, name in enumerate(config.CUSTOMERS, start=1):
                    conn.execute("INSERT INTO accounts (id, owner, balance) VALUES (%s, %s, %s)",
                                 (f"{branch}-{1000 + i}", name, config.CUSTOMER_BALANCE))


def init_coord() -> None:
    if config.COORD_LOG_BRANCH:
        # The log lives in its own database inside a replicated branch cluster.
        # The bank role must exist first (init_branch creates it), then the database.
        init_branch(config.COORD_LOG_BRANCH)
        with db.connect_branch(config.COORD_LOG_BRANCH, "write", user=config.PG_SUPERUSER,
                               password=config.PG_SUPERUSER_PASSWORD, dbname="postgres") as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                                (config.COORD_LOG_DB,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(config.COORD_LOG_DB), sql.Identifier(config.BANK_USER)))
    with db.connect_coord() as conn:
        conn.execute(COORD_SCHEMA)


def branch_was_initialised(branch: str) -> bool:
    with db.connect_coord() as conn:
        return conn.execute("SELECT 1 FROM meta WHERE key = %s", (f"initialised:{branch}",)).fetchone() is not None


def init_branch_and_record(branch: str) -> None:
    init_branch(branch)
    with db.connect_coord() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (%s, 'yes') ON CONFLICT DO NOTHING",
                     (f"initialised:{branch}",))


def init_all(log=print, attempts: int = 120, delay: float = 3.0) -> list:
    """Wait for the coordinator log database and the clusters, then create everything.

    A branch that was set up on an earlier run and is unreachable right now
    (for example its whole cluster is down) does not hold up the start: the
    coordinator must be able to restart and run recovery during an outage.
    Returns the branches that still need a check once they are back."""
    todo = {"coord": init_coord, **{b: (lambda b=b: init_branch_and_record(b)) for b in config.BRANCHES}}
    skipped = []
    for attempt in range(1, attempts + 1):
        for name, fn in list(todo.items()):
            if name != "coord" and "coord" in todo:
                continue                      # the log database comes first
            try:
                fn()
                del todo[name]
                log(f"[init] {name}: ready")
            except Exception as e:            # cluster still starting, primary not elected yet, ...
                reason = f"{type(e).__name__}: {str(e).splitlines()[0][:100]}"
                if name != "coord" and attempt >= 3 and branch_was_initialised(name):
                    del todo[name]
                    skipped.append(name)
                    log(f"[init] {name}: unreachable ({reason}); it was set up earlier, starting without it")
                else:
                    log(f"[init] {name}: not ready yet ({reason})")
        if not todo:
            return skipped
        time.sleep(delay)
    raise RuntimeError(f"gave up waiting for: {', '.join(todo)}")


if __name__ == "__main__":
    init_all()
