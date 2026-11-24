"""Bank API and two-phase commit (2PC) coordinator.

A transfer between two accounts in the SAME branch is an ordinary local
transaction. A transfer between DIFFERENT branches touches two independent
database clusters, so it runs as a distributed transaction:

    Phase 1 (prepare)  on each branch:  BEGIN; UPDATE ...; PREPARE TRANSACTION 'gid'
                       Each branch promises it can commit and keeps the row
                       locks, even across a crash or failover.
    Decision           the coordinator writes COMMITTING (or ABORTING) to its
                       own durable log. This single write is the commit point.
    Phase 2 (commit)   on each branch:  COMMIT PREPARED 'gid'

If the coordinator dies at any point, the log tells the recovery routine what
to do with the transactions left "in doubt" on the branches:

    log says STARTED     -> no decision was made  -> ROLLBACK PREPARED (presumed abort)
    log says COMMITTING  -> decision was commit   -> COMMIT PREPARED wherever still pending
    log says ABORTING    -> decision was abort    -> ROLLBACK PREPARED wherever still pending
"""
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal, Optional

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from psycopg import sql
from pydantic import BaseModel, Field

from . import config, db, schema

ready = threading.Event()
recovery_lock = threading.Lock()


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class BusinessError(Exception):
    """The request itself is invalid (unknown account, not enough money)."""
    status = 400


class AccountNotFound(BusinessError):
    status = 404


class InsufficientFunds(BusinessError):
    status = 409


# --------------------------------------------------------------------------
# Coordinator log (the durable record of every distributed transaction)
# --------------------------------------------------------------------------
def log_insert(txn_id, kind, src, dst, amount, participants, state, note=None):
    with db.connect_coord() as c:
        c.execute(
            "INSERT INTO txn_log (txn_id, kind, from_account, to_account, amount, participants, state, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (txn_id, kind, src, dst, amount, participants, state, note))


def log_state(txn_id, state, note=None):
    with db.connect_coord() as c:
        c.execute(
            "UPDATE txn_log SET state = %s, note = COALESCE(%s, note), updated_at = now() WHERE txn_id = %s",
            (state, note, txn_id))


# --------------------------------------------------------------------------
# Work on a single branch
# --------------------------------------------------------------------------
def apply_move(conn, txn_id: str, account: str, delta: Decimal) -> None:
    """Add delta (positive or negative) to one account inside the open transaction."""
    try:
        cur = conn.execute(
            "UPDATE accounts SET balance = balance + %s, updated_at = now() WHERE id = %s",
            (delta, account))
    except psycopg.errors.CheckViolation:
        raise InsufficientFunds(f"insufficient funds in {account}")
    if cur.rowcount == 0:
        raise AccountNotFound(f"account {account} does not exist")
    conn.execute("INSERT INTO ledger (txn_id, account_id, delta) VALUES (%s, %s, %s)",
                 (txn_id, account, delta))


def local_transaction(branch: str, txn_id: str, moves: list) -> None:
    """Ordinary single-database transaction (both accounts in the same branch)."""
    with db.connect_branch(branch) as conn:
        with conn.transaction():
            for account, delta in sorted(moves):      # fixed lock order avoids deadlocks
                apply_move(conn, txn_id, account, delta)


def prepare_on_branch(branch: str, gid: str, txn_id: str, account: str, delta: Decimal) -> None:
    """Phase 1 on one participant."""
    conn = db.connect_branch(branch)
    try:
        conn.execute("BEGIN")
        apply_move(conn, txn_id, account, delta)
        # From here on the branch guarantees it can commit. The transaction is
        # written to disk (and to the synchronous replica) and survives crashes.
        conn.execute(sql.SQL("PREPARE TRANSACTION {}").format(sql.Literal(gid)))
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


class BranchNotWritable(Exception):
    pass


def finish_on_branch(branch: str, gid: str, action: str) -> bool:
    """Phase 2 on one participant: COMMIT PREPARED or ROLLBACK PREPARED.

    Idempotent: if the prepared transaction is no longer listed, an earlier
    attempt already finished it. Returns False if the branch cannot be reached
    right now (the recovery loop will try again)."""
    assert action in ("COMMIT", "ROLLBACK")
    try:
        with db.connect_branch(branch) as conn:
            # "Not listed, so already finished" is only a valid conclusion on a
            # fully promoted primary. During a failover we can briefly reach a
            # node that is still a standby, and a standby does not list
            # prepared transactions at all. Concluding "done" there would
            # leave one half of a transfer uncommitted.
            if conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
                raise BranchNotWritable("node is still in recovery (failover in progress)")
            if conn.execute("SELECT 1 FROM pg_prepared_xacts WHERE gid = %s", (gid,)).fetchone():
                conn.execute(sql.SQL(action + " PREPARED {}").format(sql.Literal(gid)))
        return True
    except psycopg.errors.UndefinedObject:
        return True                                   # finished concurrently by someone else
    except Exception as e:
        log(f"[2pc] {action} PREPARED {gid} on branch {branch} failed, will retry: "
            f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
        return False


def gid_of(txn_id: str) -> str:
    return f"bank-{txn_id}"


# --------------------------------------------------------------------------
# Fault injection for the demo
# --------------------------------------------------------------------------
Fault = Literal["crash_before_decision", "crash_after_decision", "crash_mid_commit"]


def crash(where: str) -> None:
    log(f"[fault] simulating a coordinator crash: {where}")
    sys.stdout.flush()
    os._exit(1)        # die immediately, like a power cut: no cleanup, no goodbye


# --------------------------------------------------------------------------
# The transfer
# --------------------------------------------------------------------------
def do_transfer(src: str, dst: str, amount: Decimal, fault: Optional[str] = None) -> dict:
    if src == dst:
        raise BusinessError("source and destination are the same account")
    try:
        src_branch, dst_branch = config.branch_of(src), config.branch_of(dst)
    except ValueError as e:
        raise AccountNotFound(str(e))
    txn_id = uuid.uuid4().hex[:12]
    steps = []
    t0 = time.perf_counter()

    def step(text):
        steps.append(f"{(time.perf_counter() - t0) * 1000:6.0f} ms  {text}")

    # ---------------- same branch: plain local transaction ----------------
    if src_branch == dst_branch:
        try:
            local_transaction(src_branch, txn_id, [(src, -amount), (dst, amount)])
        except BusinessError as e:
            _log_local(txn_id, src, dst, amount, src_branch, "ABORTED", str(e))
            raise
        step(f"local transaction committed on branch {src_branch}")
        _log_local(txn_id, src, dst, amount, src_branch, "COMMITTED")
        return {"txn_id": txn_id, "kind": "local", "state": "COMMITTED", "steps": steps}

    # ---------------- different branches: two-phase commit ----------------
    gid = gid_of(txn_id)
    # Always visit branches in the same order (A before B). Two opposite
    # transfers then queue behind each other instead of deadlocking across
    # the two databases, which neither database could detect on its own.
    work = sorted([(src_branch, src, -amount), (dst_branch, dst, amount)])
    participants = [w[0] for w in work]

    log_insert(txn_id, "2pc", src, dst, amount, participants, "STARTED")
    step("coordinator log: STARTED")

    # Phase 1: prepare everywhere
    try:
        for branch, account, delta in work:
            prepare_on_branch(branch, gid, txn_id, account, delta)
            step(f"phase 1: PREPARE TRANSACTION ok on branch {branch} ({account} {delta:+})")
    except Exception as e:
        reason = str(e).splitlines()[0][:200]
        step(f"phase 1 failed: {reason}")
        log_state(txn_id, "ABORTING", reason)
        if all([finish_on_branch(b, gid, "ROLLBACK") for b in participants]):
            log_state(txn_id, "ABORTED")
            step("rolled back on all branches: ABORTED")
        else:
            step("a branch is unreachable; recovery will finish the rollback")
        if isinstance(e, BusinessError):
            raise
        raise HTTPException(503, {"txn_id": txn_id, "error": f"transfer aborted: {reason}", "steps": steps})

    if fault == "crash_before_decision":
        crash("both branches PREPARED, but no decision logged yet")

    # Decision: this write is the commit point of the whole distributed transaction.
    log_state(txn_id, "COMMITTING")
    step("coordinator log: decision = COMMIT (durable)")

    if fault == "crash_after_decision":
        crash("decision COMMIT logged, but no branch told yet")

    # Phase 2: commit everywhere
    done = True
    for i, branch in enumerate(participants):
        ok = finish_on_branch(branch, gid, "COMMIT")
        step(f"phase 2: COMMIT PREPARED on branch {branch}: {'ok' if ok else 'unreachable, recovery will retry'}")
        done = done and ok
        if fault == "crash_mid_commit" and i == 0:
            crash(f"committed on branch {branch} only")

    if done:
        log_state(txn_id, "COMMITTED")
        step("coordinator log: COMMITTED")
        return {"txn_id": txn_id, "kind": "2pc", "state": "COMMITTED", "steps": steps}
    # The decision is made and durable, so the transfer WILL complete; a branch
    # was just unreachable (e.g. mid-failover). The recovery loop finishes it.
    return {"txn_id": txn_id, "kind": "2pc", "state": "COMMITTING", "steps": steps}


def _log_local(txn_id, src, dst, amount, branch, state, note=None):
    try:
        log_insert(txn_id, "local", src, dst, amount, [branch], state, note)
    except Exception as e:          # history only; a local transfer does not depend on it
        log(f"[log] could not record local txn {txn_id}: {type(e).__name__}")


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------
def recover(startup: bool = False) -> list:
    """Finish every distributed transaction that is not in a final state."""
    report = []
    with recovery_lock:
        with db.connect_coord() as c:
            if startup:
                # Nothing is in flight right after a restart, so any STARTED row
                # belongs to a previous run that died before deciding.
                rows = c.execute(
                    "UPDATE txn_log SET state = 'ABORTING', updated_at = now(), "
                    "note = 'coordinator restarted before a decision was logged: presumed abort' "
                    "WHERE state = 'STARTED' RETURNING txn_id").fetchall()
            else:
                rows = c.execute(
                    "UPDATE txn_log SET state = 'ABORTING', updated_at = now(), "
                    "note = 'no decision after 60 s: presumed abort' "
                    "WHERE state = 'STARTED' AND created_at < now() - interval '60 seconds' "
                    "RETURNING txn_id").fetchall()
            for (txn_id,) in rows:
                log(f"[recovery] txn {txn_id}: STARTED with no decision -> ABORTING (presumed abort)")
            min_age = "0 seconds" if startup else "3 seconds"   # don't race live requests
            pending = c.execute(
                "SELECT txn_id, state, participants FROM txn_log "
                "WHERE state IN ('COMMITTING', 'ABORTING') AND updated_at <= now() - %s::interval "
                "ORDER BY created_at", (min_age,)).fetchall()
        for txn_id, state, participants in pending:
            action, final = ("COMMIT", "COMMITTED") if state == "COMMITTING" else ("ROLLBACK", "ABORTED")
            results = {b: finish_on_branch(b, gid_of(txn_id), action) for b in participants}
            if all(results.values()):
                log_state(txn_id, final, f"resolved by recovery: {action} PREPARED on {', '.join(participants)}")
                log(f"[recovery] txn {txn_id}: log says {state} -> {action} PREPARED on "
                    f"{', '.join(participants)} -> {final}")
                report.append({"txn_id": txn_id, "was": state, "now": final})
            else:
                down = [b for b, ok in results.items() if not ok]
                report.append({"txn_id": txn_id, "was": state, "now": state, "unreachable": down})
    return report


def sweep_orphans() -> None:
    """Safety net, driven from the participants' side.

    Ask every branch for prepared transactions that have been sitting there for
    a while, and look each one up in the coordinator log. If the log already
    holds a final verdict, apply it. This catches anything the normal paths
    missed, so a prepared transaction can never hold its row locks forever."""
    for branch in config.BRANCHES:
        try:
            with db.connect_branch(branch) as conn:
                if conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
                    continue
                gids = [r[0] for r in conn.execute(
                    "SELECT gid FROM pg_prepared_xacts WHERE gid LIKE 'bank-%%' "
                    "AND database = current_database() AND prepared < now() - interval '15 seconds'")]
        except Exception:
            continue                                  # branch unreachable: next pass
        for gid in gids:
            with db.connect_coord() as c:
                row = c.execute("SELECT state FROM txn_log WHERE txn_id = %s", (gid[len("bank-"):],)).fetchone()
            action = {"COMMITTED": "COMMIT", "ABORTED": "ROLLBACK"}.get(row[0] if row else None)
            if action and finish_on_branch(branch, gid, action):
                log(f"[recovery] orphan {gid} on branch {branch}: log says {row[0]} -> {action} PREPARED")


def background() -> None:
    late = schema.init_all(log=log)          # branches that were down while we started
    log("[startup] running crash recovery")
    try:
        report = recover(startup=True)
        done = sum(1 for r in report if r["now"] != r["was"])
        waiting = len(report) - done
        log(f"[startup] recovery finished: {done} transaction(s) resolved"
            + (f", {waiting} waiting for an unreachable branch (will retry)" if waiting else "")
            if report else "[startup] recovery finished, nothing in doubt")
    except Exception as e:
        log(f"[startup] recovery deferred: {type(e).__name__}: {e}")
    ready.set()
    log("[startup] coordinator ready")
    tick = 0
    while True:                 # keep retrying whatever could not be finished yet
        time.sleep(2)
        tick += 1
        try:
            recover()
            if tick % 5 == 0:
                sweep_orphans()
                for branch in list(late):    # make sure their schema is really there
                    try:
                        schema.init_branch_and_record(branch)
                        late.remove(branch)
                        log(f"[init] {branch}: reachable again, schema verified")
                    except Exception:
                        pass
        except Exception as e:
            log(f"[recovery] pass failed: {type(e).__name__}: {str(e).splitlines()[0][:120]}")


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=background, daemon=True).start()
    yield


app = FastAPI(title="Distributed Bank: coordinator", lifespan=lifespan)


class TransferRequest(BaseModel):
    from_account: str
    to_account: str
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    fault: Optional[Fault] = None


class CashRequest(BaseModel):
    account: str
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


def require_ready():
    if not ready.is_set():
        raise HTTPException(503, "coordinator is still starting (waiting for the database clusters)")


def run(fn, *args):
    require_ready()
    try:
        return fn(*args)
    except BusinessError as e:
        raise HTTPException(e.status, str(e))
    except HTTPException:
        raise
    except Exception as e:      # database unreachable, lock timeout, failover in progress...
        raise HTTPException(503, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")


@app.get("/health")
def health():
    return {"status": "ok" if ready.is_set() else "starting"}


@app.post("/transfer")
def transfer(req: TransferRequest):
    return run(do_transfer, req.from_account.upper(), req.to_account.upper(), req.amount, req.fault)


@app.post("/deposit")
def deposit(req: CashRequest):
    """Cash in: money moves from the branch vault to the customer."""
    acct = req.account.upper()
    return run(lambda: do_transfer(config.vault_of(config.branch_of(acct)), acct, req.amount))


@app.post("/withdraw")
def withdraw(req: CashRequest):
    """Cash out: money moves from the customer to the branch vault."""
    acct = req.account.upper()
    return run(lambda: do_transfer(acct, config.vault_of(config.branch_of(acct)), req.amount))


@app.get("/accounts/{account_id}")
def get_account(account_id: str,
                consistent: bool = Query(False, description="read from the primary instead of a replica")):
    account_id = account_id.upper()

    def read():
        try:
            branch = config.branch_of(account_id)
        except ValueError as e:
            raise AccountNotFound(str(e))
        conn = db.connect_branch(branch, "write") if consistent else db.connect_read(branch)
        with conn:
            row = conn.execute(
                "SELECT id, owner, balance, pg_is_in_recovery() FROM accounts WHERE id = %s",
                (account_id,)).fetchone()
        if not row:
            raise AccountNotFound(f"account {account_id} does not exist")
        return {"id": row[0], "owner": row[1], "balance": row[2],
                "served_by": "replica" if row[3] else "primary"}
    return run(read)


@app.get("/accounts")
def list_accounts():
    def read():
        out = []
        for branch in config.BRANCHES:
            with db.connect_read(branch) as conn:
                out += [{"id": r[0], "owner": r[1], "balance": r[2]} for r in conn.execute(
                    "SELECT id, owner, balance FROM accounts ORDER BY id")]
        return out
    return run(read)


@app.get("/transactions")
def transactions(limit: int = Query(20, ge=1, le=200)):
    def read():
        with db.connect_coord() as c:
            rows = c.execute(
                "SELECT txn_id, kind, from_account, to_account, amount, state, note, created_at "
                "FROM txn_log ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
        keys = ["txn_id", "kind", "from_account", "to_account", "amount", "state", "note", "created_at"]
        return [dict(zip(keys, r)) for r in rows]
    return run(read)


@app.post("/recover")
def recover_now():
    """Run a recovery pass immediately (it also runs at startup and every 2 s)."""
    return {"resolved": run(recover)}
