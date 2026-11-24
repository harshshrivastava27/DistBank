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
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal, Optional

import psycopg
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from psycopg import sql
from pydantic import BaseModel, Field

from . import config, db, leader, schema

ready = threading.Event()          # set only while this coordinator is the leader and has recovered
recovery_lock = threading.Lock()


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# Leader election (see leader.py). Without ELECTION_ETCD there is a single
# coordinator, which is always the leader.
election = (leader.Election(config.ELECTION_ETCD, config.COORDINATOR_NAME,
                            config.COORDINATOR_ADVERTISE_URL, log=log)
            if config.ELECTION_ETCD else None)


def is_leader() -> bool:
    return election.is_leader() if election else True


class NotLeader(Exception):
    pass


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


class IdempotencyMismatch(BusinessError):
    """The same Idempotency-Key was sent with a different request."""
    status = 422


class DuplicateRequest(Exception):
    """A request with this Idempotency-Key was already executed (found on a branch)."""
    def __init__(self, txn_id):
        self.txn_id = txn_id


# --------------------------------------------------------------------------
# Coordinator log (the durable record of every distributed transaction)
# --------------------------------------------------------------------------
def log_insert(txn_id, kind, src, dst, amount, participants, state, note=None, idem=None):
    with db.connect_coord() as c:
        c.execute(
            "INSERT INTO txn_log (txn_id, kind, from_account, to_account, amount, participants, state, note, "
            "idempotency_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (txn_id, kind, src, dst, amount, participants, state, note, idem))


# --------------------------------------------------------------------------
# Idempotency: a client that lost the response (e.g. during a failover) can
# safely retry with the same Idempotency-Key; the transfer runs at most once.
#   2PC:   the key is stored in the coordinator log row written BEFORE any
#          work, under a unique index, so a duplicate cannot even start.
#   local: the key is inserted on the branch INSIDE the same transaction that
#          moves the money, so "money moved" and "key recorded" are atomic.
# --------------------------------------------------------------------------
def find_by_key(idem):
    with db.connect_coord() as c:
        return c.execute(
            "SELECT txn_id, kind, from_account, to_account, amount, state FROM txn_log "
            "WHERE idempotency_key = %s", (idem,)).fetchone()


def find_by_key_or_none(idem):
    """Like find_by_key, but None if the log database is unreachable: a local
    transfer can still be made retry-safe by its branch alone (request_keys)."""
    try:
        return find_by_key(idem)
    except Exception:
        return None


def replay(idem, src, dst, amount, txn_id=None, kind="local", state="COMMITTED"):
    """The answer to a retried request: the original outcome, nothing executed."""
    row = find_by_key_or_none(idem)
    if row:
        txn_id, kind, r_src, r_dst, r_amount, state = row
        if (r_src, r_dst, Decimal(r_amount)) != (src, dst, Decimal(amount)):
            raise IdempotencyMismatch(
                f"Idempotency-Key {idem!r} was already used for {r_src} -> {r_dst} {r_amount}")
    return {"txn_id": txn_id, "kind": kind, "state": state, "replayed": True,
            "steps": [f"     0 ms  idempotent replay: key {idem!r} was already processed as txn {txn_id} "
                      f"({state}); nothing was executed again"]}


def commit_lsns(branches) -> dict:
    """WAL position on each branch's primary right after our commit. A client can
    pass it back as ?min_lsn= to read its own write from a replica (causal read)."""
    out = {}
    for b in branches:
        try:
            with db.connect_branch(b) as conn:
                out[b] = conn.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0]
        except Exception:
            out[b] = None
    return out


def log_decide(txn_id, state, note=None) -> bool:
    """Record the COMMIT/ABORT decision, but only if nobody decided yet.

    This compare-and-set on the replicated log is the fencing rule between
    coordinators: if a newly elected leader already presumed this transfer
    aborted (state no longer STARTED), the old leader's COMMIT is refused."""
    with db.connect_coord() as c:
        cur = c.execute(
            "UPDATE txn_log SET state = %s, note = COALESCE(%s, note), updated_at = now() "
            "WHERE txn_id = %s AND state = 'STARTED'",
            (state, note, txn_id))
        return cur.rowcount == 1


def log_state(txn_id, state, note=None):
    """Move a transaction forward. Monotonic: a final state (COMMITTED/ABORTED)
    is never overwritten, so a late or deposed writer cannot regress it."""
    with db.connect_coord() as c:
        c.execute(
            "UPDATE txn_log SET state = %s, note = COALESCE(%s, note), updated_at = now() "
            "WHERE txn_id = %s AND state NOT IN ('COMMITTED', 'ABORTED')",
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


def local_transaction(branch: str, txn_id: str, moves: list, idem=None) -> None:
    """Ordinary single-database transaction (both accounts in the same branch)."""
    with db.connect_branch(branch) as conn:
        with conn.transaction():
            if idem:
                got = conn.execute("INSERT INTO request_keys (key, txn_id) VALUES (%s, %s) "
                                   "ON CONFLICT (key) DO NOTHING RETURNING key", (idem, txn_id)).fetchone()
                if not got:          # executed before: roll back this attempt, report the original
                    first = conn.execute("SELECT txn_id FROM request_keys WHERE key = %s", (idem,)).fetchone()
                    raise DuplicateRequest(first[0] if first else None)
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
def do_transfer(src: str, dst: str, amount: Decimal, fault: Optional[str] = None,
                idem: Optional[str] = None) -> dict:
    if src == dst:
        raise BusinessError("source and destination are the same account")
    try:
        src_branch, dst_branch = config.branch_of(src), config.branch_of(dst)
    except ValueError as e:
        raise AccountNotFound(str(e))
    if idem and find_by_key_or_none(idem):
        return replay(idem, src, dst, amount)
    txn_id = uuid.uuid4().hex[:12]
    steps = []
    t0 = time.perf_counter()

    def step(text):
        steps.append(f"{(time.perf_counter() - t0) * 1000:6.0f} ms  {text}")

    # ---------------- same branch: plain local transaction ----------------
    if src_branch == dst_branch:
        try:
            local_transaction(src_branch, txn_id, [(src, -amount), (dst, amount)], idem)
        except DuplicateRequest as d:
            return replay(idem, src, dst, amount, txn_id=d.txn_id)
        except BusinessError as e:
            _log_local(txn_id, src, dst, amount, src_branch, "ABORTED", str(e))
            raise
        step(f"local transaction committed on branch {src_branch}")
        _log_local(txn_id, src, dst, amount, src_branch, "COMMITTED", idem=idem)
        return {"txn_id": txn_id, "kind": "local", "state": "COMMITTED", "steps": steps,
                "lsn": commit_lsns([src_branch])}

    # ---------------- different branches: two-phase commit ----------------
    gid = gid_of(txn_id)
    # Always visit branches in the same order (A before B). Two opposite
    # transfers then queue behind each other instead of deadlocking across
    # the two databases, which neither database could detect on its own.
    work = sorted([(src_branch, src, -amount), (dst_branch, dst, amount)])
    participants = [w[0] for w in work]

    try:
        log_insert(txn_id, "2pc", src, dst, amount, participants, "STARTED", idem=idem)
    except psycopg.errors.UniqueViolation:
        return replay(idem, src, dst, amount)        # a concurrent duplicate got there first
    step("coordinator log: STARTED")

    # Phase 1: prepare everywhere
    try:
        for branch, account, delta in work:
            prepare_on_branch(branch, gid, txn_id, account, delta)
            step(f"phase 1: PREPARE TRANSACTION ok on branch {branch} ({account} {delta:+})")
    except Exception as e:
        reason = str(e).splitlines()[0][:200]
        step(f"phase 1 failed: {reason}")
        # Decide ABORT only if no decision exists yet (a newly elected leader may
        # already have decided). Rolling back on every participant is still right:
        # a branch whose PREPARE was never sent has nothing prepared and returns at
        # once; an unreachable one may or may not have prepared, so recovery waits.
        if not log_decide(txn_id, "ABORTING", reason):
            step("another coordinator already decided this transfer; it finishes it")
            raise HTTPException(503, {"txn_id": txn_id, "error": f"transfer aborted: {reason}", "steps": steps})
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
    # Only the leader may decide, and only if no other coordinator decided first.
    if not is_leader() or not log_decide(txn_id, "COMMITTING"):
        step("decision refused: this coordinator is no longer the leader; "
             "the new leader decides (presumed abort)")
        raise HTTPException(503, {"txn_id": txn_id, "error": "coordinator lost leadership mid-transfer; "
                                  "the new leader rolls it back", "steps": steps})
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
        return {"txn_id": txn_id, "kind": "2pc", "state": "COMMITTED", "steps": steps,
                "lsn": commit_lsns(participants)}
    # The decision is made and durable, so the transfer WILL complete; a branch
    # was just unreachable (e.g. mid-failover). The recovery loop finishes it.
    return {"txn_id": txn_id, "kind": "2pc", "state": "COMMITTING", "steps": steps}


def _log_local(txn_id, src, dst, amount, branch, state, note=None, idem=None):
    try:
        log_insert(txn_id, "local", src, dst, amount, [branch], state, note, idem)
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
            # Don't race a live request still finishing phase 2 (statements may take up to 8 s).
            min_age = "0 seconds" if startup else "15 seconds"
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
        if not gids:
            continue
        # A prepared transaction with NO log row is deliberately left alone: the log
        # always gets its row before PREPARE, so a missing row means it belongs to
        # another log (an older coordinator, or history from before the log moved),
        # and presuming abort could undo half of a transfer committed elsewhere.
        with db.connect_coord() as c:
            for gid in gids:
                row = c.execute("SELECT state FROM txn_log WHERE txn_id = %s", (gid[len("bank-"):],)).fetchone()
                action = {"COMMITTED": "COMMIT", "ABORTED": "ROLLBACK"}.get(row[0] if row else None)
                if action and finish_on_branch(branch, gid, action):
                    log(f"[recovery] orphan {gid} on branch {branch}: log says {row[0]} -> {action} PREPARED")


def take_over() -> bool:
    """Just became leader: whatever the previous leader left half-done is resolved
    from the log before we accept new transfers."""
    log("[startup] running crash recovery" if not election else
        f"[election] {config.COORDINATOR_NAME} is the new leader: running crash recovery from the log")
    try:
        report = recover(startup=True)
        done = sum(1 for r in report if r["now"] != r["was"])
        waiting = len(report) - done
        log(f"[recovery] finished: {done} transaction(s) resolved"
            + (f", {waiting} waiting for an unreachable branch (will retry)" if waiting else "")
            if report else "[recovery] finished, nothing in doubt")
    except Exception as e:
        # Do not accept transfers before recovery has run: retry the takeover.
        log(f"[recovery] failed, will retry before serving: {type(e).__name__}: {e}")
        return False
    ready.set()
    log("[startup] coordinator ready" if not election else
        f"[election] {config.COORDINATOR_NAME} is serving transfers")
    return True


def background() -> None:
    late = schema.init_all(log=log)          # branches that were down while we started
    if election:
        threading.Thread(target=election.run, daemon=True).start()
    was_leader = False
    tick = 0
    while True:
        leading = is_leader()
        if leading and not was_leader:
            leading = take_over()            # False: recovery failed, try again next tick
        elif was_leader and not leading:
            ready.clear()
            log(f"[election] {config.COORDINATOR_NAME} lost leadership: standing by, not accepting transfers")
        was_leader = leading
        time.sleep(2 if leading else 1)
        if not leading or not is_leader():   # re-check: leadership may have gone while we slept
            continue
        tick += 1
        try:   # leader only: keep retrying whatever could not be finished yet
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
    if election:            # planned stop: hand over at once instead of after the 10 s lease
        election.revoke()


app = FastAPI(title="Distributed Bank: coordinator", lifespan=lifespan)


class TransferRequest(BaseModel):
    from_account: str
    to_account: str
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    fault: Optional[Fault] = None


class CashRequest(BaseModel):
    account: str
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


def vault_for(account: str) -> str:
    try:
        return config.vault_of(config.branch_of(account))
    except ValueError as e:
        raise AccountNotFound(str(e))


def require_ready():
    if election and not is_leader():
        ready.clear()
        cur = None
        try:
            cur = leader.get_leader(config.ELECTION_ETCD)
        except Exception:
            pass
        raise HTTPException(503, f"this coordinator ({config.COORDINATOR_NAME}) is a standby; "
                                 + (f"the leader is {cur['name']} at {cur['url']}" if cur else "an election is in progress"))
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
    if election and not is_leader():
        return {"status": "standby", "name": config.COORDINATOR_NAME}
    return {"status": "ok" if ready.is_set() else "starting", "name": config.COORDINATOR_NAME}


@app.post("/transfer")
def transfer(req: TransferRequest, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key",
                                                description="retry-safe: same key = executed at most once")):
    return run(do_transfer, req.from_account.upper(), req.to_account.upper(), req.amount, req.fault,
               idempotency_key)


@app.post("/deposit")
def deposit(req: CashRequest, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key",
                                                description="retry-safe: same key = executed at most once")):
    """Cash in: money moves from the branch vault to the customer."""
    acct = req.account.upper()
    return run(lambda: do_transfer(vault_for(acct), acct, req.amount, None, idempotency_key))


@app.post("/withdraw")
def withdraw(req: CashRequest, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key",
                                                description="retry-safe: same key = executed at most once")):
    """Cash out: money moves from the customer to the branch vault."""
    acct = req.account.upper()
    return run(lambda: do_transfer(acct, vault_for(acct), req.amount, None, idempotency_key))


@app.get("/accounts/{account_id}")
def get_account(account_id: str,
                consistent: bool = Query(False, description="read from the primary instead of a replica"),
                min_lsn: Optional[str] = Query(None, description="read-your-writes: the 'lsn' a write "
                                               "returned; a replica serves only once it has replayed it")):
    account_id = account_id.upper()
    if min_lsn and not re.fullmatch(r"[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}", min_lsn):
        raise HTTPException(400, "min_lsn must look like 0/3000060")

    def read():
        try:
            branch = config.branch_of(account_id)
        except ValueError as e:
            raise AccountNotFound(str(e))
        conn = db.connect_branch(branch, "write") if consistent else db.connect_read(branch)
        note = None
        if min_lsn and not consistent:
            # Causal consistency: has this replica replayed the caller's write yet?
            # (On a primary pg_last_wal_replay_lsn() is NULL: it has everything.)
            caught_up = conn.execute(
                "SELECT COALESCE(pg_last_wal_replay_lsn() >= %s::pg_lsn, true)", (min_lsn,)).fetchone()[0]
            if not caught_up:
                conn.close()
                conn = db.connect_branch(branch, "write")
                note = "primary (replica had not replayed min_lsn yet)"
        with conn:
            row = conn.execute(
                "SELECT id, owner, balance, pg_is_in_recovery() FROM accounts WHERE id = %s",
                (account_id,)).fetchone()
        if not row:
            raise AccountNotFound(f"account {account_id} does not exist")
        return {"id": row[0], "owner": row[1], "balance": row[2],
                "served_by": note or ("replica" if row[3] else "primary")}
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


@app.get("/audit/reconcile")
def audit_reconcile():
    """Double-entry audit of the whole bank, read from the branch primaries.

    1. Every account: balance == opening balance + sum of its ledger rows
       (no money moved without a ledger record, and none recorded without moving).
    2. Every transaction's ledger rows sum to zero across all branches
       (money only ever moves, it is never created or destroyed).
    3. Every COMMITTED transfer in the coordinator log has its debit and credit
       in the ledger; no ABORTED transfer left any ledger row.
    Transfers still in flight (not yet final in the log) are skipped."""
    def audit():
        problems, n_accounts, per_txn = [], 0, {}
        # Read the log FIRST: whatever it already calls COMMITTED finished on every
        # branch before this point, so the ledgers read next must show all of it.
        with db.connect_coord() as c:
            logged = {r[0]: r[1:] for r in c.execute(
                "SELECT txn_id, state, kind, amount, participants FROM txn_log")}
        for branch in config.BRANCHES:
            with db.connect_branch(branch) as conn:
                rows = conn.execute(
                    "SELECT a.id, a.balance, COALESCE(sum(l.delta), 0) FROM accounts a "
                    "LEFT JOIN ledger l ON l.account_id = a.id GROUP BY a.id, a.balance").fetchall()
                for acct, bal, moved in rows:
                    n_accounts += 1
                    opening = config.VAULT_BALANCE if acct.endswith("VAULT") else config.CUSTOMER_BALANCE
                    if bal != opening + moved:
                        problems.append(f"{acct}: balance {bal} but opening {opening} + ledger {moved} = {opening + moved}")
                for txn_id, total, count, settled in conn.execute(
                        "SELECT txn_id, sum(delta), count(*), max(created_at) < now() - interval '30 seconds' "
                        "FROM ledger GROUP BY txn_id"):
                    t = per_txn.setdefault(txn_id, {"sum": Decimal(0), "rows": 0, "branches": set(), "settled": True})
                    t["sum"] += total
                    t["rows"] += count
                    t["branches"].add(branch)
                    t["settled"] = t["settled"] and settled
        n_2pc = 0
        for txn_id, (state, kind, amount, participants) in logged.items():
            t = per_txn.get(txn_id)
            if state == "ABORTED" and t:
                problems.append(f"txn {txn_id}: ABORTED but left {t['rows']} ledger row(s)")
            if state == "COMMITTED":
                n_2pc += kind == "2pc"
                if not t:
                    problems.append(f"txn {txn_id}: COMMITTED but has no ledger rows")
                elif kind == "2pc" and t["branches"] != set(participants):
                    problems.append(f"txn {txn_id}: COMMITTED but ledger only on {sorted(t['branches'])}")
        for txn_id, t in per_txn.items():
            if txn_id in logged:
                in_flight = logged[txn_id][0] not in ("COMMITTED", "ABORTED")
            else:            # started after we read the log, or older history from another log
                in_flight = not t["settled"]
            if t["sum"] != 0 and not in_flight:
                problems.append(f"txn {txn_id}: ledger rows sum to {t['sum']}, not 0")
        return {"ok": not problems, "checked_accounts": n_accounts, "checked_ledger_txns": len(per_txn),
                "checked_2pc_txns": n_2pc, "mismatches": problems[:50]}
    return run(audit)


@app.post("/recover")
def recover_now():
    """Run a recovery pass immediately (it also runs at startup and every 2 s)."""
    return {"resolved": run(recover)}
