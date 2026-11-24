"""Assertions used by tests/verify.sh.

Runs inside the dashboard container, where every service is reachable:

    docker exec dashboard python -m bank.checks <check> [arguments]

Each check prints what it verified and exits 0 on success, 1 on failure.
"""
import sys
import threading
import time
from decimal import Decimal

import httpx
import psycopg

from . import config

API = config.COORDINATOR_URL
DASH = "http://localhost:8080"
http = httpx.Client(timeout=30)


class Failed(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise Failed(msg)


def state():
    return http.get(f"{DASH}/api/state").json()


def wait_for(what, fn, timeout=60, every=0.5):
    """Poll fn() until it returns something truthy; fail after timeout seconds."""
    start, last = time.time(), None
    while time.time() - start < timeout:
        try:
            last = fn()
            if last:
                return last, time.time() - start
        except Exception as e:           # services may be mid-restart
            last = e
        time.sleep(every)
    raise Failed(f"timed out after {timeout}s waiting for: {what} (last: {last!r})")


def roles(branch):
    nodes = state()["branches"][branch]["nodes"]
    primary = [n["name"] for n in nodes if n["role"] == "primary"]
    replicas = [n["name"] for n in nodes if n["role"] in ("replica", "sync replica") and n["lag_bytes"] == 0]
    sync = [n["name"] for n in nodes if n["role"] == "sync replica"]
    return primary, replicas, sync


def balance(account):
    r = http.get(f"{API}/accounts/{account}", params={"consistent": "true"})
    need(r.status_code == 200, f"cannot read {account}: {r.status_code} {r.text}")
    return Decimal(str(r.json()["balance"]))


def transfer(src, dst, amount, fault=None):
    body = {"from_account": src, "to_account": dst, "amount": str(amount)}
    if fault:
        body["fault"] = fault
    return http.post(f"{API}/transfer", json=body)


def settled():
    s = state()
    clean = all(b["prepared"] == [] for b in s["branches"].values())
    return s["invariant"] == "ok" and clean and s["coord"] and s["coord"]["open"] == 0


# ------------------------------------------------------------------ checks
def check_ready(timeout="180"):
    """Coordinator up, each branch has 1 primary + 2 in-sync replicas (one synchronous), total correct."""
    def ok():
        s = state()
        if s["coordinator"] != "ok" or s["invariant"] != "ok":
            return False
        for b in config.BRANCHES:
            p, r, sy = roles(b)
            if len(p) != 1 or len(r) != 2 or len(sy) != 1:
                return False
        return True
    _, took = wait_for("healthy stack", ok, int(timeout), 1)
    s = state()
    for b in config.BRANCHES:
        p, r, sy = roles(b)
        print(f"  branch {b}: primary {p[0]}, replicas {', '.join(r)} (synchronous: {sy[0]})")
    print(f"  total {s['total']} = expected {s['expected_total']}  (ready after {took:.0f}s)")


def check_invariant(timeout="60"):
    """Total money correct, nothing in doubt on any branch, no unresolved log entries."""
    _, took = wait_for("invariant ok and nothing in doubt", settled, int(timeout), 1)
    s = state()
    print(f"  total {s['total']} = expected {s['expected_total']}, no in-doubt transactions, "
          f"log: {s['coord']['counts']}")


def check_api():
    """Bank API behaviour: every outcome moves exactly the right amount of money, or none."""
    a1, a2, b1 = "A-1001", "A-1002", "B-1001"
    before = {x: balance(x) for x in (a1, a2, b1)}

    r = transfer(a1, b1, "250.50")
    need(r.status_code == 200 and r.json()["kind"] == "2pc" and r.json()["state"] == "COMMITTED", f"2PC transfer: {r.text}")
    need(balance(a1) == before[a1] - Decimal("250.50") and balance(b1) == before[b1] + Decimal("250.50"),
         "2PC transfer moved the wrong amount")
    print("  cross-branch transfer: two-phase commit, COMMITTED, both balances exact")

    r = transfer(b1, a1, "250.50")
    need(r.status_code == 200, f"reverse transfer: {r.text}")
    r = transfer(a1, a2, 40)
    need(r.status_code == 200 and r.json()["kind"] == "local", f"local transfer: {r.text}")
    r = transfer(a2, a1, 40)
    need(r.status_code == 200, f"local transfer back: {r.text}")
    need({x: balance(x) for x in before} == before, "balances did not return to their starting values")
    print("  same-branch transfer: single local transaction; round trips restore balances")

    for label, src, dst, amt, code in [
        ("insufficient funds, cross-branch (debit side prepared second)", b1, a1, 9999999, 409),
        ("insufficient funds, cross-branch (debit side prepared first)", a1, b1, 9999999, 409),
        ("insufficient funds, same branch", a1, a2, 9999999, 409),
        ("unknown destination account", a1, "B-9999", 5, 404),
        ("unknown source account", "A-9999", b1, 5, 404),
        ("unknown branch", a1, "Z-1001", 5, 404),
        ("same account", a1, a1, 5, 400),
        ("zero amount", a1, b1, 0, 422),
        ("negative amount", a1, b1, -5, 422),
        ("three decimal places", a1, b1, "1.005", 422),
    ]:
        r = transfer(src, dst, amt)
        need(r.status_code == code, f"{label}: expected HTTP {code}, got {r.status_code} {r.text[:120]}")
    need({x: balance(x) for x in before} == before, "a rejected transfer changed a balance")
    print("  10 kinds of invalid transfer rejected with the right status; no balance changed")

    r = transfer("a-1001", "b-1001", 1)
    need(r.status_code == 200, f"lower-case ids: {r.text}")
    transfer(b1, a1, 1)

    vault = balance("A-VAULT")
    r = http.post(f"{API}/deposit", json={"account": a1, "amount": "75"})
    need(r.status_code == 200 and balance(a1) == before[a1] + 75 and balance("A-VAULT") == vault - 75, f"deposit: {r.text}")
    r = http.post(f"{API}/withdraw", json={"account": a1, "amount": "75"})
    need(r.status_code == 200 and balance(a1) == before[a1] and balance("A-VAULT") == vault, f"withdraw: {r.text}")
    print("  deposit and withdraw move money between customer and vault")

    r = http.get(f"{API}/accounts/{a1}")
    need(r.status_code == 200 and r.json()["served_by"] == "replica", f"default read should come from a replica: {r.text}")
    r = http.get(f"{API}/accounts/{a1}", params={"consistent": "true"})
    need(r.json()["served_by"] == "primary", "consistent read should come from the primary")
    need(len(http.get(f"{API}/accounts").json()) == 22, "expected 22 accounts")
    need(http.get(f"{API}/transactions", params={"limit": 5}).status_code == 200, "transactions endpoint")
    print("  reads: replica by default, primary with ?consistent=true; listings work")
    wait_for("settled", settled, 20)


def node_conn(node):
    host, pg_port, _ = config.node_endpoint(node)
    return psycopg.connect(
        f"host={host} port={pg_port} dbname={config.BANK_DB} user={config.BANK_USER} "
        f"password={config.BANK_PASSWORD} connect_timeout=3", autocommit=True)


def check_replication(branch):
    """A committed write appears on both replicas; replicas refuse writes."""
    primary, replicas, sync = roles(branch)
    need(len(primary) == 1 and len(replicas) == 2, f"branch {branch} not healthy: {primary} {replicas}")
    acct, other = f"{branch}-1009", f"{branch}-1010"
    r = transfer(acct, other, "12.34")
    need(r.status_code == 200, f"transfer failed: {r.text}")
    want = balance(acct)
    for node in replicas:
        def arrived(node=node):
            with node_conn(node) as c:
                rec, bal = c.execute("SELECT pg_is_in_recovery(), balance FROM accounts WHERE id = %s", (acct,)).fetchone()
                return rec and bal == want
        _, took = wait_for(f"write to reach {node}", arrived, 5, 0.05)
        try:
            with node_conn(node) as c:
                c.execute("UPDATE accounts SET balance = balance WHERE id = %s", (acct,))
            raise Failed(f"{node} accepted a write but it is a replica")
        except psycopg.errors.ReadOnlySqlTransaction:
            pass
        print(f"  {node}: has the new balance {want} after {took * 1000:.0f} ms; rejects writes (read-only)")
    with node_conn(primary[0]) as c:
        rows = c.execute("SELECT application_name, state, sync_state FROM pg_stat_replication ORDER BY 1").fetchall()
    need(len(rows) == 2 and all(r[1] == "streaming" for r in rows), f"pg_stat_replication: {rows}")
    need(sorted(r[2] for r in rows) == ["async", "sync"], f"expected one sync and one async replica: {rows}")
    print(f"  {primary[0]} streams to: " + ", ".join(f"{r[0]} ({r[2]})" for r in rows))
    transfer(other, acct, "12.34")


def check_loadbalancing(branch):
    """Reads are spread across the replicas; writes only reach the primary."""
    primary, replicas, _ = roles(branch)

    def counters():
        return {n["name"]: (n["writes"], n["reads"]) for n in state()["branches"][branch]["nodes"]}
    before = counters()
    acct = f"{branch}-1005"
    for _ in range(40):
        need(http.get(f"{API}/accounts/{acct}").status_code == 200, "read failed")
    for _ in range(10):
        need(transfer(acct, f"{branch}-1006", 1).status_code == 200, "write failed")
        need(transfer(f"{branch}-1006", acct, 1).status_code == 200, "write failed")
    time.sleep(1.5)
    after = counters()
    delta = {n: (after[n][0] - before[n][0], after[n][1] - before[n][1]) for n in after}
    for n, (w, r) in sorted(delta.items()):
        print(f"  {n}: +{w} write connections, +{r} read connections")
    for n in replicas:
        need(delta[n][0] == 0, f"replica {n} received write connections")
        need(delta[n][1] >= 15, f"replica {n} received too few reads ({delta[n][1]})")
    need(delta[primary[0]][1] == 0, "primary received read connections although replicas are healthy")
    need(delta[primary[0]][0] >= 20, "primary received too few write connections")
    shares = [delta[n][1] for n in replicas]
    need(max(shares) - min(shares) <= 6, f"reads not evenly balanced: {shares}")


def check_concurrency():
    """Many simultaneous transfers on the same accounts, including opposite directions."""
    accounts = ["A-1003", "A-1004", "B-1003", "B-1004"]
    before = {a: balance(a) for a in accounts}
    expected = dict(before)
    lock = threading.Lock()
    outcome = {"ok": 0, "refused": 0, "failed": 0}
    plan = []
    for i in range(25):                                   # opposite directions on the same two accounts
        plan += [("A-1003", "B-1003", 7), ("B-1003", "A-1003", 5),
                 ("A-1004", "B-1003", 3), ("B-1004", "A-1003", 2), ("A-1003", "A-1004", 1), ("B-1004", "B-1003", 1)]

    def run(chunk):
        client = httpx.Client(timeout=60)
        for src, dst, amt in chunk:
            r = client.post(f"{API}/transfer", json={"from_account": src, "to_account": dst, "amount": amt})
            with lock:
                if r.status_code == 200 and r.json()["state"] == "COMMITTED":
                    outcome["ok"] += 1
                    expected[src] -= amt
                    expected[dst] += amt
                elif r.status_code == 409:
                    outcome["refused"] += 1
                else:
                    outcome["failed"] += 1

    threads = [threading.Thread(target=run, args=(plan[i::10],)) for i in range(10)]
    t0 = time.time()
    [t.start() for t in threads]
    [t.join() for t in threads]
    took = time.time() - t0
    wait_for("settled", settled, 30)
    after = {a: balance(a) for a in accounts}
    print(f"  {len(plan)} transfers from 10 parallel clients in {took:.1f}s: "
          f"{outcome['ok']} committed, {outcome['refused']} refused, {outcome['failed']} failed")
    need(after == expected, f"balances do not match the committed transfers: {after} vs {expected}")
    print("  every account balance equals start + exactly the committed transfers (no lost or double updates)")
    need(outcome["failed"] <= len(plan) * 0.05, f"too many failures under contention: {outcome}")
    need(outcome["ok"] >= len(plan) * 0.9, f"too few successes: {outcome}")


def check_crash(fault, src, dst, amount):
    """Start a transfer that kills the coordinator; verify the in-doubt state it leaves behind."""
    amount = Decimal(amount)
    b_src, b_dst = balance(src), balance(dst)
    print(f"BEFORE {b_src} {b_dst}")                       # parsed by verify.sh
    try:
        r = transfer(src, dst, amount, fault)
        raise Failed(f"coordinator answered ({r.status_code}) but should have crashed")
    except httpx.HTTPError:
        pass
    wait_for("coordinator down", lambda: state()["coordinator"] == "down", 10)
    s = state()
    prepared = {b: len(v["prepared"]) for b, v in s["branches"].items()}
    want = {"crash_before_decision": {"A": 1, "B": 1}, "crash_after_decision": {"A": 1, "B": 1},
            "crash_mid_commit": {"A": 0, "B": 1}}[fault]
    need(prepared == want, f"in-doubt transactions per branch: expected {want}, found {prepared}")
    print(f"  coordinator is down; prepared transactions waiting: A={prepared['A']} B={prepared['B']}")
    if fault == "crash_mid_commit":
        need(s["invariant"] == "settling" and Decimal(s["total"]) != config.EXPECTED_TOTAL,
             f"expected the total to be off while half-committed, got {s['invariant']} {s['total']}")
        print(f"  half committed: total is {s['total']} instead of {s['expected_total']} (dashboard says 'settling')")
    else:
        need(s["invariant"] == "ok", "money moved before any commit")
        print("  no balance has changed yet")
    # Rows touched by the prepared transaction must stay locked for everyone else.
    branch = "B"
    locked = dst if config.branch_of(dst) == branch else src
    try:
        with psycopg.connect(f"host={config.HAPROXY_HOST} port={config.BRANCHES[branch]['write_port']} "
                             f"dbname={config.BANK_DB} user={config.BANK_USER} password={config.BANK_PASSWORD}",
                             autocommit=True) as c:
            c.execute("SET lock_timeout = '500ms'")
            c.execute("UPDATE accounts SET balance = balance WHERE id = %s", (locked,))
        raise Failed(f"{locked} was not locked by the prepared transaction")
    except psycopg.errors.LockNotAvailable:
        print(f"  {locked} is locked by the in-doubt transaction (other writers must wait)")


def check_recovered(fault, src, dst, amount, b_src, b_dst):
    """After the coordinator restarts: the in-doubt transfer is fully committed or fully rolled back."""
    amount, b_src, b_dst = Decimal(amount), Decimal(b_src), Decimal(b_dst)
    wait_for("coordinator back", lambda: state()["coordinator"] == "ok", 60)
    wait_for("nothing in doubt", settled, 30)
    moved = fault != "crash_before_decision"
    want = (b_src - amount, b_dst + amount) if moved else (b_src, b_dst)
    got = (balance(src), balance(dst))
    need(got == want, f"after recovery expected {want}, got {got}")
    verdict = "COMMITTED on both branches" if moved else "ROLLED BACK on both branches"
    print(f"  recovery outcome: {verdict}; balances {got[0]} / {got[1]}; total correct; locks released")
    if moved:                                   # put the money back so the test can be repeated
        need(transfer(dst, src, amount).status_code == 200, "could not transfer the money back")


def check_primary(branch):
    primary, _, _ = roles(branch)
    need(len(primary) == 1, f"branch {branch} has no single primary: {primary}")
    print(primary[0])


def check_failover(branch, old_primary, timeout="90"):
    """A different node becomes primary and accepts writes."""
    def promoted():
        p, _, _ = roles(branch)
        return p and p[0] != old_primary and p[0]
    new, took = wait_for(f"a new primary for branch {branch}", promoted, int(timeout), 0.5)

    if state()["coordinator"] != "ok":         # e.g. failover while the coordinator is crashed
        print(f"  {new} promoted {took:.0f}s after {old_primary} went away")
        return

    def writable():
        return transfer(f"{branch}-1007", f"{branch}-1008", 1).status_code == 200
    _, took2 = wait_for("writes accepted again", writable, 30, 0.5)
    print(f"  {new} promoted {took:.0f}s after {old_primary} went away; writes accepted again after {took + took2:.0f}s")


def check_rejoined(branch, node, timeout="120"):
    """A restarted node comes back as an in-sync replica."""
    def back():
        p, r, sy = roles(branch)
        return len(p) == 1 and node in r and len(r) == 2 and len(sy) == 1
    _, took = wait_for(f"{node} to rejoin as replica", back, int(timeout), 1)
    print(f"  {node} rejoined as a replica and caught up (lag 0) after {took:.0f}s")


def check_writes(branch, expect="ok"):
    """Writes to a branch succeed (ok) or are refused (down)."""
    r = transfer(f"{branch}-1007", f"{branch}-1008", 1)
    if expect == "ok":
        need(r.status_code == 200, f"write to branch {branch} failed: {r.status_code} {r.text[:150]}")
    else:
        need(r.status_code == 503, f"expected 503 from branch {branch}, got {r.status_code}")
    print(f"  write to branch {branch}: HTTP {r.status_code}")


def check_reads(branch):
    r = http.get(f"{API}/accounts/{branch}-1001")
    need(r.status_code == 200, f"read from branch {branch} failed: {r.status_code} {r.text[:150]}")
    print(f"  read from branch {branch}: HTTP 200, served by {r.json()['served_by']}")


def check_cross_branch(expect="ok"):
    """A cross-branch transfer succeeds (ok) or is refused cleanly without moving money (down)."""
    before = None
    try:
        before = (balance("A-1006"), balance("B-1006"))
    except Failed:
        pass                                    # a branch is down: cannot compare balances
    r = transfer("A-1006", "B-1006", 1)
    if expect == "ok":
        need(r.status_code == 200 and r.json()["state"] == "COMMITTED", f"cross-branch transfer failed: {r.status_code} {r.text[:150]}")
        transfer("B-1006", "A-1006", 1)
    else:
        need(r.status_code == 503, f"expected a clean 503, got {r.status_code} {r.text[:150]}")
        if before:
            need((balance("A-1006"), balance("B-1006")) == before, "a refused transfer moved money")
    print(f"  cross-branch transfer: HTTP {r.status_code}" + ("" if expect == "ok" else " (refused, no money moved)"))


def check_balance(account):
    print(balance(account))


def check_coordinator(expect):
    wait_for(f"coordinator {expect}", lambda: state()["coordinator"] == expect, 60)
    print(f"  coordinator: {expect}")


CHECKS = {name[len("check_"):].replace("_", "-"): fn for name, fn in globals().items() if name.startswith("check_")}

if __name__ == "__main__":
    name, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if name not in CHECKS:
        print("checks: " + ", ".join(sorted(CHECKS)))
        sys.exit(2)
    try:
        CHECKS[name](*args)
    except Failed as e:
        print(f"  FAILED: {e}")
        sys.exit(1)
