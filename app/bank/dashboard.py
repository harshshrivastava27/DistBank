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

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import config, db

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


def coordinator_status() -> str:
    try:
        return httpx.get(f"{config.COORDINATOR_URL}/health", timeout=0.8).json().get("status", "down")
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
    return state


async def forward(path: str, request: Request):
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{config.COORDINATOR_URL}{path}", content=body,
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


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE.read_text(encoding="utf-8")
