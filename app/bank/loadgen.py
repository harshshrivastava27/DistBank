"""Traffic generator: continuous random transfers and balance reads.

    docker compose run --rm loadgen                  (runs until Ctrl-C)
    docker compose run --rm loadgen python -m bank.loadgen --seconds 60 --workers 6

Prints one line per second, so you can see exactly how many requests fail
while you kill a database node, and for how long.
"""
import argparse
import random
import threading
import time

import httpx

from . import config

ACCOUNTS = [f"{b}-{1000 + i}" for b in config.BRANCHES for i in range(1, len(config.CUSTOMERS) + 1)]


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset_window()
        self.total = dict(self.win)
        self.last_error = ""

    def reset_window(self):
        self.win = {"t_ok": 0, "t_rej": 0, "t_fail": 0, "r_ok": 0, "r_fail": 0}

    def add(self, key, error=""):
        with self.lock:
            self.win[key] += 1
            self.total[key] += 1
            if error:
                self.last_error = error


def worker(url: str, stats: Stats, cross: float, stop: threading.Event):
    client = httpx.Client(base_url=url, timeout=20)
    while not stop.is_set():
        src = random.choice(ACCOUNTS)
        if random.random() < cross:
            dst = random.choice([a for a in ACCOUNTS if a[0] != src[0]])     # other branch: 2PC
        else:
            dst = random.choice([a for a in ACCOUNTS if a[0] == src[0] and a != src])
        try:
            r = client.post("/transfer", json={"from_account": src, "to_account": dst,
                                               "amount": random.randint(1, 200)})
            if r.status_code == 200:
                stats.add("t_ok")
            elif r.status_code in (400, 404, 409):
                stats.add("t_rej")              # e.g. insufficient funds: a correct refusal
            else:
                stats.add("t_fail", f"{r.status_code} {r.text[:90]}")
        except Exception as e:
            stats.add("t_fail", type(e).__name__)
        for _ in range(2):
            try:
                r = client.get(f"/accounts/{random.choice(ACCOUNTS)}")
                stats.add("r_ok" if r.status_code == 200 else "r_fail",
                          "" if r.status_code == 200 else f"{r.status_code} {r.text[:90]}")
            except Exception as e:
                stats.add("r_fail", type(e).__name__)
        time.sleep(random.uniform(0.02, 0.08))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=config.COORDINATOR_URL)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seconds", type=int, default=0, help="0 = run until Ctrl-C")
    ap.add_argument("--cross", type=float, default=0.7, help="share of transfers that cross branches")
    args = ap.parse_args()

    stats, stop = Stats(), threading.Event()
    for _ in range(args.workers):
        threading.Thread(target=worker, args=(args.url, stats, args.cross, stop), daemon=True).start()
    print(f"load generator: {args.workers} workers -> {args.url}  (Ctrl-C to stop)", flush=True)
    print("  time | transfers ok  refused  FAILED | reads ok  FAILED | last error", flush=True)
    start = time.time()
    try:
        while not args.seconds or time.time() - start < args.seconds:
            time.sleep(1)
            with stats.lock:
                w, err = stats.win, stats.last_error
                stats.reset_window()
                stats.last_error = ""
            print(f"{int(time.time() - start):5d}s | {w['t_ok']:12d} {w['t_rej']:8d} {w['t_fail']:7d} |"
                  f" {w['r_ok']:8d} {w['r_fail']:7d} | {err}", flush=True)
    except KeyboardInterrupt:
        pass
    stop.set()
    t = stats.total
    print(f"\nTOTAL  transfers: {t['t_ok']} ok, {t['t_rej']} refused, {t['t_fail']} failed"
          f"   reads: {t['r_ok']} ok, {t['r_fail']} failed", flush=True)


if __name__ == "__main__":
    main()
