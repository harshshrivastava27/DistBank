"""Live status page. Runs as its own service so that it keeps working when
the coordinator is crashed on purpose during the demo.

It only observes: node roles come from Patroni's REST API, traffic counters
from HAProxy, balances and in-doubt transactions from the databases, and the
transaction history from the coordinator's log.
"""
import csv
import io
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import config, db, leader

app = FastAPI(title="Distributed Bank: dashboard")
pool = ThreadPoolExecutor(max_workers=24)
probe_pool = ThreadPoolExecutor(max_workers=12)     # separate: probes run inside pool tasks
PAGE = Path(__file__).parent / "static" / "dashboard.html"


def safe(fn, *args, default=None):
    try:
        return fn(*args)
    except Exception:
        return default


# ---------------------------------------------------------------- Patroni
def lsn_to_int(lsn) -> int:
    if isinstance(lsn, int):
        return lsn
    hi, lo = str(lsn).split("/")
    return (int(hi, 16) << 32) + int(lo, 16)


def probe_node(node: str) -> dict:
    """Ask one node's Patroni agent what it currently is."""
    host, _, api_port = config.node_endpoint(node)
    shown_host = host if host != node else None     # an IP in multi-laptop mode
    try:
        r = httpx.get(f"http://{host}:{api_port}/patroni", timeout=1.5)
        d = r.json()
    except Exception:
        return {"name": node, "host": shown_host, "role": "down"}
    xlog = d.get("xlog") or {}
    role = d.get("role", "unknown")
    if role in ("master", "leader"):
        role = "primary"
    return {
        "name": node,
        "host": shown_host,
        "role": role if d.get("state") == "running" else d.get("state", "starting"),
        "timeline": d.get("timeline"),
        "position": xlog.get("location") or xlog.get("replayed_location"),
        "sync": {r.get("application_name"): r.get("sync_state") for r in d.get("replication", [])},
    }


def cluster_nodes(branch: str) -> list:
    nodes = list(probe_pool.map(probe_node, config.BRANCHES[branch]["nodes"]))
    primary = next((n for n in nodes if n["role"] == "primary"), None)
    for n in nodes:
        n["lag_bytes"] = None
        if n["role"] == "replica" and primary:
            if primary["sync"].get(n["name"]) in ("sync", "quorum"):
                n["role"] = "sync replica"
            if n.get("position") is not None and primary.get("position") is not None:
                n["lag_bytes"] = max(0, lsn_to_int(primary["position"]) - lsn_to_int(n["position"]))
    for n in nodes:
        n.pop("sync", None)
        n.pop("position", None)
    return nodes


# ---------------------------------------------------------------- databases
def branch_accounts(branch: str):
    with db.connect_read(branch) as conn:
        rows = conn.execute("SELECT id, owner, balance FROM accounts ORDER BY id").fetchall()
    return [{"id": r[0], "owner": r[1], "balance": str(r[2])} for r in rows]


def branch_primary_view(branch: str):
    """Authoritative numbers, read from the primary."""
    with db.connect_branch(branch, "write") as conn:
        total = conn.execute("SELECT COALESCE(sum(balance), 0) FROM accounts").fetchone()[0]
        prepared = conn.execute(
            "SELECT gid, prepared FROM pg_prepared_xacts "
            "WHERE database = current_database() ORDER BY prepared").fetchall()
    return {"total": total, "prepared": [{"gid": g, "since": t.isoformat()} for g, t in prepared]}


def coord_view():
    try:
        return coord_view_db()
    except Exception:
        # Dashboard running on another laptop: the log database isn't exposed,
        # but the coordinator's API serves the same log.
        return coord_view_api()


def coord_view_api():
    rows = httpx.get(f"{coordinator_url()}/transactions", params={"limit": 200}, timeout=1.5).json()
    counts = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    recent = [{"txn_id": r["txn_id"], "kind": r["kind"], "from": r["from_account"], "to": r["to_account"],
               "amount": str(r["amount"]), "state": r["state"], "note": r["note"], "time": r["created_at"]}
              for r in rows[:14]]
    open_count = sum(1 for r in rows if r["state"] in ("STARTED", "COMMITTING", "ABORTING"))
    return {"recent": recent, "open": open_count, "counts": counts}


def coord_view_db():
    with db.connect_coord() as c:
        rows = c.execute(
            "SELECT txn_id, kind, from_account, to_account, amount, state, note, "
            "created_at FROM txn_log ORDER BY created_at DESC LIMIT 14").fetchall()
        open_count = c.execute(
            "SELECT count(*) FROM txn_log WHERE state IN ('STARTED', 'COMMITTING', 'ABORTING')").fetchone()[0]
        counts = dict(c.execute("SELECT state, count(*) FROM txn_log GROUP BY state").fetchall())
    keys = ["txn_id", "kind", "from", "to", "amount", "state", "note", "time"]
    return {"recent": [dict(zip(keys, [str(v) if isinstance(v, Decimal) else v.isoformat() if hasattr(v, "isoformat") else v for v in r])) for r in rows],
            "open": open_count, "counts": counts}


# ---------------------------------------------------------------- HAProxy
def haproxy_view():
    text = httpx.get(config.HAPROXY_STATS_URL, timeout=1.0).text.lstrip("# ")
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row["svname"] in ("FRONTEND", "BACKEND"):
            continue
        out.setdefault(row["pxname"], {})[row["svname"]] = {
            "status": row["status"], "sessions": int(row["stot"] or 0)}
    return out


def coordinator_url() -> str:
    """Where to send transfers: the elected leader, if elections are on."""
    if config.ELECTION_ETCD:
        cur = safe(leader.get_leader, config.ELECTION_ETCD)
        if cur:
            return cur["url"]
    return config.COORDINATOR_URL


def coordinators_view():
    """Every coordinator candidate and who leads (None if elections are off)."""
    if not config.ELECTION_ETCD:
        return None
    cur = leader.get_leader(config.ELECTION_ETCD)
    members = leader.get_members(config.ELECTION_ETCD)
    for m in members:
        m["host"] = urlparse(m["url"]).hostname
        m["leader"] = bool(cur) and cur["name"] == m["name"]
    return {"leader": cur["name"] if cur else None, "members": members}


def coordinator_status() -> str:
    try:
        if config.ELECTION_ETCD and not safe(leader.get_leader, config.ELECTION_ETCD):
            return "electing"                   # nobody holds the leader key right now
        return httpx.get(f"{coordinator_url()}/health", timeout=0.8).json().get("status", "down")
    except Exception:
        return "down"


# ---------------------------------------------------------------- API
def collect() -> dict:
    f_coord = pool.submit(safe, coord_view)
    f_hap = pool.submit(safe, haproxy_view, default={})
    f_status = pool.submit(coordinator_status)
    f_nodes = {b: pool.submit(cluster_nodes, b) for b in config.BRANCHES}
    f_accts = {b: pool.submit(safe, branch_accounts, b) for b in config.BRANCHES}
    f_prim = {b: pool.submit(safe, branch_primary_view, b) for b in config.BRANCHES}

    coord, hap = f_coord.result(), f_hap.result()
    branches = {}
    for b, cfg in config.BRANCHES.items():
        prim = f_prim[b].result()
        nodes = f_nodes[b].result()
        for n in nodes:
            n["writes"] = hap.get(cfg["haproxy_write"], {}).get(n["name"], {}).get("sessions")
            n["reads"] = hap.get(cfg["haproxy_read"], {}).get(n["name"], {}).get("sessions")
        branches[b] = {
            "name": cfg["name"],
            "nodes": nodes,
            "accounts": f_accts[b].result(),
            "total": str(prim["total"]) if prim else None,
            "prepared": prim["prepared"] if prim else None,
        }
    return {"coordinator": f_status.result(), "branches": branches, "coord": coord,
            "expected_total": str(config.EXPECTED_TOTAL)}


def judge(state: dict) -> str:
    """Compare the money actually in the bank with the amount that must be there."""
    totals = [b["total"] for b in state["branches"].values()]
    if any(t is None for t in totals):
        return "unknown"                       # a primary is unreachable (failover in progress)
    if sum(Decimal(t) for t in totals) == config.EXPECTED_TOTAL:
        return "ok"
    in_doubt = any(b["prepared"] for b in state["branches"].values())
    busy = state["coord"] is None or state["coord"]["open"] > 0
    # 2PC is atomic but the two commits are not simultaneous: while a transfer
    # is between "committed on A" and "committed on B", the sum is off by its amount.
    return "settling" if (in_doubt or busy) else "violation"


@app.get("/api/state")
def api_state():
    state = collect()
    verdict = judge(state)
    if verdict == "violation":                 # the two sums are read at slightly different
        time.sleep(0.25)                       # moments; look again before raising the alarm
        state = collect()
        verdict = judge(state)
    totals = [b["total"] for b in state["branches"].values()]
    state["total"] = str(sum(Decimal(t) for t in totals)) if all(totals) else None
    state["invariant"] = verdict
    state["coordinators"] = safe(coordinators_view)
    state["laptops"] = config.LAPTOP_NAMES
    host = urlparse(coordinator_url()).hostname
    state["coordinator_host"] = None if host == "coordinator" else host
    return state


# ---------------------------------------------------------------- demo helpers
@app.get("/api/replication")
def api_replication(account: str = "A-1001"):
    """Replication demo: read one account directly from EVERY node of its branch."""
    account = account.upper()
    branch = config.branch_of(account)

    def read_node(node):
        host, pg_port, _ = config.node_endpoint(node)
        out = {"name": node, "host": host if host != node else None}
        try:
            with psycopg.connect(
                    f"host={host} port={pg_port} dbname={config.BANK_DB} user={config.BANK_USER} "
                    f"password={config.BANK_PASSWORD} connect_timeout=2", autocommit=True) as c:
                row = c.execute("SELECT balance, pg_is_in_recovery() FROM accounts WHERE id = %s",
                                (account,)).fetchone()
            out["balance"] = str(row[0]) if row else None
            out["role"] = "replica" if row and row[1] else "primary"
        except Exception as e:
            out["error"] = type(e).__name__
        return out

    return {"account": account, "nodes": list(probe_pool.map(read_node, config.BRANCHES[branch]["nodes"]))}


class LoadTest(BaseModel):
    branch: str = "A"
    reads: int = 30
    writes: int = 5


@app.post("/api/loadtest")
def api_loadtest(req: LoadTest):
    """Load-balancing demo: send reads and writes through HAProxy, then report
    how many connections each node received (from HAProxy's own counters)."""
    b = req.branch.upper()
    cfg = config.BRANCHES[b]
    before = safe(haproxy_view, default={})
    reads_ok = 0
    for _ in range(max(0, min(req.reads, 100))):
        try:
            with db.connect_read(b) as c:
                c.execute("SELECT count(*) FROM accounts").fetchone()
            reads_ok += 1
        except Exception:
            pass
    writes_ok = 0
    for _ in range(max(0, min(req.writes, 20))):          # touches a timestamp only: no money moves
        try:
            with db.connect_branch(b, "write") as c:
                c.execute("UPDATE accounts SET updated_at = now() WHERE id = %s", (config.vault_of(b),))
            writes_ok += 1
        except Exception:
            pass
    after = safe(haproxy_view, default={})

    def delta(proxy, node):
        get = lambda v: v.get(proxy, {}).get(node, {}).get("sessions", 0)
        return max(0, get(after) - get(before))

    return {"branch": b, "reads_ok": reads_ok, "writes_ok": writes_ok,
            "nodes": [{"name": n, "reads": delta(cfg["haproxy_read"], n),
                       "writes": delta(cfg["haproxy_write"], n)} for n in cfg["nodes"]]}


@app.get("/api/account/{account_id}")
def api_account(account_id: str):
    """Availability demo: a customer's balance read, through the coordinator API."""
    try:
        r = httpx.get(f"{coordinator_url()}/accounts/{account_id}", timeout=5)
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"detail": f"coordinator unreachable ({type(e).__name__})"}, status_code=502)


async def forward(path: str, request: Request):
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{coordinator_url()}{path}", content=body,
                                  headers={"content-type": "application/json"})
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse(
            {"detail": f"No answer from the coordinator ({type(e).__name__}). "
                       f"It is down or it crashed while handling this request."}, status_code=502)


@app.post("/api/transfer")
async def api_transfer(request: Request):
    return await forward("/transfer", request)


@app.post("/api/recover")
async def api_recover(request: Request):
    return await forward("/recover", request)


@app.post("/api/deposit")
async def api_deposit(request: Request):
    return await forward("/deposit", request)


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE.read_text(encoding="utf-8")
