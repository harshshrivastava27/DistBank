"""Leader election for the coordinator, using etcd (the same store Patroni uses
to elect database primaries).

Several coordinators run, on different laptops. Exactly one is the LEADER and
accepts transfers; the others are STANDBYS that wait.

The algorithm (etcd lease + compare-and-set):

  1. Each coordinator asks etcd for a LEASE with a 10 s time-to-live and keeps
     renewing it every 2 s. A lease that is not renewed expires, and etcd
     deletes every key attached to it.
  2. To become leader, a coordinator atomically creates the key
     /distbank/coordinator/leader, attached to its lease, *only if the key
     does not exist yet* (etcd transaction: create_revision == 0).
     etcd is linearizable, so at most one coordinator can win this.
  3. If the leader crashes or loses its network, it stops renewing. 10 s later
     etcd deletes the key, and the next standby to try (every 2 s) wins.
  4. Safety: a leader that cannot renew its lease for 6 s (TTL minus a 4 s
     margin) steps down on its own, before etcd could possibly hand the key to
     someone else. So two coordinators never both believe they are leader.

The new leader then runs crash recovery from the replicated coordinator log,
finishing or rolling back whatever the old leader left half-done.

etcd is spoken to through its JSON/HTTP gateway (/v3/...), so no extra library
is needed. Keys and values are base64 encoded; 64-bit ids travel as strings.

etcd itself runs as a cluster of 3 members (one per laptop) and stays usable
while any 2 are up. ELECTION_ETCD lists them all, comma-separated; requests go
to the member that last answered and move on to the next if it stops.
"""
import base64
import json
import threading
import time

import httpx

LEADER_KEY = "/distbank/coordinator/leader"
MEMBERS_PREFIX = "/distbank/coordinator/members/"
TTL = 10            # seconds a lease lives without renewal
RENEW_EVERY = 2     # seconds between renewals / election attempts
SAFETY = 4          # step down if not renewed for TTL - SAFETY seconds


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _unb64(s: str) -> str:
    return base64.b64decode(s).decode()


_last_good = {}      # etcd endpoint list -> index of the member that last answered


def _post(etcd: str, path: str, body: dict, timeout: float = 2.0) -> dict:
    """POST to one etcd member; if it is down (or cut off from the majority),
    try the next. `etcd` is one URL or several, comma-separated."""
    endpoints = [e.strip().rstrip("/") for e in etcd.split(",") if e.strip()]
    start = _last_good.get(etcd, 0)
    error = None
    for i in range(len(endpoints)):
        n = (start + i) % len(endpoints)
        try:
            r = httpx.post(f"{endpoints[n]}{path}", json=body, timeout=timeout)
            r.raise_for_status()
            _last_good[etcd] = n
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            error = e
    raise error


# ---------------------------------------------------------------- read-only helpers
def get_leader(etcd: str):
    """{"name", "url", "since"} of the current leader, or None during an election."""
    kvs = _post(etcd, "/v3/kv/range", {"key": _b64(LEADER_KEY)}, timeout=1.5).get("kvs") or []
    return json.loads(_unb64(kvs[0]["value"])) if kvs else None


def get_members(etcd: str) -> list:
    """Every coordinator currently alive (leader and standbys)."""
    end = MEMBERS_PREFIX[:-1] + chr(ord(MEMBERS_PREFIX[-1]) + 1)
    kvs = _post(etcd, "/v3/kv/range", {"key": _b64(MEMBERS_PREFIX), "range_end": _b64(end)},
                timeout=1.5).get("kvs") or []
    return sorted((json.loads(_unb64(kv["value"])) for kv in kvs), key=lambda m: m["name"])


# ---------------------------------------------------------------- the election
class Election:
    def __init__(self, etcd: str, name: str, url: str, log=print):
        self.etcd, self.name, self.url, self.log = etcd, name, url, log
        self.lease = None
        self.last_renew = 0.0
        self._leader = threading.Event()

    # -- state
    def is_leader(self) -> bool:
        """True only while we hold the key AND our lease is certainly still alive."""
        return self._leader.is_set() and time.monotonic() - self.last_renew < TTL - SAFETY

    # -- etcd operations
    def _grant(self) -> str:
        return _post(self.etcd, "/v3/lease/grant", {"TTL": TTL})["ID"]

    def _keepalive(self) -> bool:
        res = _post(self.etcd, "/v3/lease/keepalive", {"ID": self.lease}).get("result", {})
        return int(res.get("TTL", 0)) > 0          # 0 / missing = lease already expired

    def _register(self) -> None:
        """Membership key, so the dashboard can list leader and standbys."""
        _post(self.etcd, "/v3/kv/put", {"key": _b64(MEMBERS_PREFIX + self.name),
                                        "value": _b64(json.dumps({"name": self.name, "url": self.url})),
                                        "lease": self.lease})

    def _try_acquire(self) -> bool:
        value = json.dumps({"name": self.name, "url": self.url, "since": time.time()})
        r = _post(self.etcd, "/v3/kv/txn", {
            # IF the leader key has never been created (nobody is leader) ...
            "compare": [{"key": _b64(LEADER_KEY), "result": "EQUAL", "target": "CREATE",
                         "create_revision": "0"}],
            # ... THEN create it, owned by our lease. Atomic: only one can win.
            "success": [{"request_put": {"key": _b64(LEADER_KEY), "value": _b64(value),
                                         "lease": self.lease}}],
        })
        return bool(r.get("succeeded"))

    def _still_mine(self) -> bool:
        kvs = _post(self.etcd, "/v3/kv/range", {"key": _b64(LEADER_KEY)}).get("kvs") or []
        return bool(kvs) and kvs[0].get("lease") == self.lease

    def _step_down(self, why: str) -> None:
        if self._leader.is_set():
            self._leader.clear()
            self.log(f"[election] {self.name}: stepping down ({why}); now a standby")

    def revoke(self) -> None:
        """Planned shutdown: give the lease back so etcd deletes our keys now and a
        standby takes over within one election round (~2 s) instead of waiting
        out the 10 s TTL. A crash cannot do this, which is why the TTL exists."""
        self._leader.clear()
        if self.lease is not None:
            try:
                _post(self.etcd, "/v3/lease/revoke", {"ID": self.lease}, timeout=1.0)
                self.log(f"[election] {self.name}: shutting down, lease revoked (instant handover)")
            except Exception:
                pass
            self.lease = None

    # -- main loop (runs in its own thread)
    def run(self) -> None:
        self.log(f"[election] {self.name}: joining the election via etcd {self.etcd} "
                 f"(lease TTL {TTL} s, renew every {RENEW_EVERY} s)")
        while True:
            try:
                if self.lease is None:
                    self.lease = self._grant()
                    self.last_renew = time.monotonic()
                    self._register()
                elif self._keepalive():
                    self.last_renew = time.monotonic()
                else:
                    self._step_down("lease expired")
                    self.lease = None
                    continue

                if self._leader.is_set():
                    if not self._still_mine():
                        self._step_down("leader key no longer ours")
                elif self._try_acquire():
                    self._leader.set()
                    self.log(f"[election] {self.name}: WON the election, I am the leader")
            except Exception as e:
                self.log(f"[election] {self.name}: no etcd member answering ({type(e).__name__})")

            if self._leader.is_set() and not self.is_leader():
                self._step_down(f"could not renew the lease for {TTL - SAFETY} s")
                self.lease = None                   # start over with a fresh lease
            time.sleep(RENEW_EVERY)
