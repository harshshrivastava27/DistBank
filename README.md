# Distributed Bank

A small bank whose accounts are split across two independent PostgreSQL clusters ("Branch A" and "Branch B"). It is built to demonstrate six topics live, by breaking things on purpose and watching the system cope:

| Topic | Where it is in this project | How you show it |
|---|---|---|
| Replication | Each branch is 1 primary + 2 replicas using PostgreSQL streaming replication, one of them synchronous | Balances change on the replicas as transfers run; `./demo.sh replication a` |
| Fault tolerance | Patroni + etcd detect a dead primary and promote a replica automatically | `./demo.sh kill-primary a` |
| Availability | Clients connect through HAProxy, never to a node directly, so they follow the new primary; replicas keep serving reads throughout | The load generator's failure counters during a kill |
| Distributed transactions | Transfers between branches use two-phase commit (`PREPARE TRANSACTION` / `COMMIT PREPARED`) driven by a coordinator service | Any A-to-B transfer on the dashboard shows each protocol step |
| Recovery | The coordinator keeps a durable log and uses it after a crash to commit or roll back in-doubt transactions; a crashed database node rejoins and catches up from the write-ahead log | `./demo.sh crash before` / `after` / `mid`, then `./demo.sh recover` |
| Load balancing | HAProxy sends writes to the primary and spreads reads round-robin over the replicas | Per-node "served" counters on the dashboard and http://localhost:8404 |

One number ties it together: **the total money in the bank must always be ₹22,00,000.00**. The dashboard shows it at the top. Whatever you kill, it must come back to that figure.

---

## 1. What you need

- **Docker Desktop** (Windows or macOS) or Docker Engine with the Compose plugin (Linux). Check with `docker compose version`.
- About **4 GB of free RAM** for the 12 containers.
- These ports free on your machine: 8080, 8000, 8404, 5500 to 5503.
- For the `demo.sh` helper: any bash shell. On Windows use **Git Bash** or **WSL**. If you only have PowerShell, section 5 lists the plain `docker` commands that do the same things.

Nothing else. You do not need Python or PostgreSQL installed.

## 2. Start it

Unzip the project, open a terminal in the `distbank` folder, then:

```bash
./demo.sh up
```

or, without bash:

```
docker compose up -d --build
```

The first run downloads images and builds two small ones, which takes a few minutes depending on your connection. After that a start takes about 10 to 40 seconds. `./demo.sh up` waits and prints "Ready." when everything is up. With plain `docker compose`, wait until the dashboard shows a primary in both branches and "coordinator: running".

Then open:

| What | Address |
|---|---|
| **Dashboard** (keep this on the projector) | http://localhost:8080 |
| HAProxy statistics | http://localhost:8404 |
| Coordinator API (interactive docs) | http://localhost:8000/docs |

To stop: `./demo.sh down` (keeps data). To wipe everything and start fresh: `./demo.sh reset`, then `./demo.sh up`. All data lives in named Docker volumes, so even `docker compose down` keeps it; only `reset` (which is `down -v`) deletes it.

### Check that everything works on your machine

```bash
./tests/verify.sh quick     # about 1 minute, breaks nothing except the coordinator
./tests/verify.sh           # about 6 minutes, kills and restarts nodes
```

This runs 19 automated tests (9 in quick mode) and prints PASS or FAIL for each: the API, replication, load balancing, concurrency, all three coordinator-crash recoveries, failover under load, node rejoin, and outages of etcd, HAProxy, the coordinator's log database and a whole branch. Run it once after your first start, and again on the morning of the demo. It needs only bash and docker.

## 3. What is running

```
                                   browser
                                      |
              +-----------------------+----------------------+
              |                                              |
      dashboard :8080  (observes everything)       coordinator :8000
              |                                    bank API + two-phase commit
              |                                       |              |
              |                                       |        coord-db (the
              |                                       |        coordinator's log)
              +------------------+--------------------+
                                 |
                          HAProxy  (stats :8404)
              A writes :5500   A reads :5501   B writes :5502   B reads :5503
                   |                |               |                |
            +------+----------------+--+     +------+----------------+--+
            |        Branch A          |     |        Branch B          |
            |  pg-a1    pg-a2   pg-a3  |     |  pg-b1    pg-b2   pg-b3  |
            |  1 primary + 2 replicas  |     |  1 primary + 2 replicas  |
            +------------+-------------+     +------------+-------------+
                         |                                |
                         +------------- etcd -------------+
                              (leader election for Patroni)
```

- **pg-a1..3, pg-b1..3**: PostgreSQL 16, each managed by a Patroni agent. Patroni sets up replication, holds a leader lock in etcd, and promotes a replica when the lock expires.
- **etcd**: the small consistent store Patroni uses to agree on who the primary is.
- **HAProxy**: the port numbers in the diagram are the ones on your machine; inside the Docker network the same listeners are 5000 to 5003 and 7000, which is what the config files use. It asks every node's Patroni agent "are you the primary?" / "are you a replica?" every 2 seconds and routes accordingly.
- **coordinator**: the bank's API. Routes each account to its branch by the first letter of the account id (`A-1001` lives in Branch A). Runs two-phase commit for cross-branch transfers and records every step in **coord-db**.
- **dashboard**: a separate read-only service, so it keeps working when you crash the coordinator.
- **loadgen**: optional traffic generator, started on demand.

Accounts: each branch has ten customers (`A-1001` to `A-1010`, `B-1001` to `B-1010`) with ₹10,000 each, plus a vault (`A-VAULT`, `B-VAULT`) with ₹10,00,000. Deposits and withdrawals move money between a customer and the vault, so the grand total never changes.

## 4. The demo, step by step (about 10 minutes)

Keep the dashboard visible the whole time. Run commands in a terminal next to it.

### Step 1. Healthy system (1 min)

Point out on the dashboard: one **Primary**, one **Sync replica** and one **Replica** per branch, lag 0, and the total at the top.

```bash
./demo.sh status          # Patroni's own view of both clusters
./demo.sh replication a   # the primary's list of replicas: one sync, one async
```

### Step 2. A distributed transaction (1 min)

On the dashboard, send ₹500 from `A-1001` to `B-1001`. The black box prints the protocol as it happened:

```
coordinator log: STARTED
phase 1: PREPARE TRANSACTION ok on branch A (A-1001 -500)
phase 1: PREPARE TRANSACTION ok on branch B (B-1001 +500)
coordinator log: decision = COMMIT (durable)
phase 2: COMMIT PREPARED on branch A: ok
phase 2: COMMIT PREPARED on branch B: ok
coordinator log: COMMITTED
```

Then send from `A-1001` to `A-1002`: same branch, so it is a single local transaction with no two-phase commit. Try an amount larger than the balance to show a clean abort: the branch that already prepared is rolled back.

### Step 3. Replication and load balancing (1 min)

Start traffic in a second terminal and leave it running:

```bash
./demo.sh load
```

Balances flash on the dashboard as they change. The account tables are read from replicas, so you are watching replication happen. The "served" counters under each node show writes going only to the primary and reads spread evenly over the two replicas. http://localhost:8404 shows the same thing in HAProxy's own page.

### Step 4. Kill a primary: fault tolerance and availability (2 min)

```bash
./demo.sh kill-primary a
```

What to watch:

- The killed node turns **Down**. For a few seconds Branch A has no primary and the total shows "unreachable".
- A replica becomes **Primary** and the timeline number goes up by one.
- In the load generator, the `FAILED` transfer column is non-zero only during the gap, and the reads `FAILED` column stays at **0** the whole time, because replicas keep answering. Transfers inside Branch B never stop.
- When it settles, the total is back to ₹22,00,000.00.

A hard kill takes about 20 to 30 seconds to fail over, because the others must wait for the dead primary's leader lock to expire (20 s). To show a planned switchover instead, use `./demo.sh stop-primary a`: the primary hands over the lock as it shuts down and the gap is about 5 to 10 seconds.

### Step 5. Bring the node back: database recovery (1 min)

```bash
./demo.sh start-all
./demo.sh status
```

The old primary comes back as a **Replica**: it notices it is no longer the leader, rewinds any writes the new primary never saw, and replays the write-ahead log until its lag is 0. Nobody copied any data by hand.

### Step 6. Crash the coordinator mid-transaction: distributed recovery (3 min)

Stop the load generator first (Ctrl-C) so the screen is calm. There are three variants. Each can also be triggered from the dashboard's "Failure to inject" drop-down.

**a) Crash after PREPARE, before any decision**

```bash
./demo.sh crash before
```

The dashboard shows "coordinator: down" and both branches list the same prepared transaction in the yellow "in-doubt" box. The money has not moved, but those two rows are locked: this is the well-known blocking problem of two-phase commit. `./demo.sh in-doubt` shows the same from `pg_prepared_xacts`.

```bash
./demo.sh recover
```

The coordinator restarts, reads its log, finds a transaction with no decision, and rolls it back on both branches ("presumed abort").

**b) Crash after the COMMIT decision is logged, before telling the branches**

```bash
./demo.sh crash after
./demo.sh recover
```

Same stuck state, opposite outcome: the log says COMMITTING, so recovery **commits** on both branches. The durable decision record is what makes the difference.

**c) Crash after committing on one branch only**

```bash
./demo.sh crash mid
```

This is the scary one: Branch A has committed, Branch B has not. The total at the top is ₹1,000 short and the banner turns amber ("settling").

```bash
./demo.sh recover
```

Recovery commits the remaining half and the total returns to ₹22,00,000.00.

### Step 7. Close

End on the green banner. Optional: run `./demo.sh total` to show the same sum straight from SQL on both primaries.

### Extras, if you have time or get asked

- **A prepared transaction survives a failover.** Run `./demo.sh crash after`, then `./demo.sh kill-primary a`. Wait for the new primary: the yellow in-doubt box is still there, because the prepared transaction was replicated synchronously. Then `./demo.sh recover` commits it on the new primary, and `./demo.sh start-all` brings the old one back.
- **etcd is not needed for normal operation.** `docker stop etcd`, wait 30 seconds, and transfers still work (Patroni's failsafe mode). Failover is impossible until you `docker start etcd`.
- **Losing both replicas.** `./demo.sh kill pg-b2` and `./demo.sh kill pg-b3` (pick the two that are not primary). Writes pause for a second or two while Patroni switches off synchronous replication, then continue; reads fall back to the primary.
- **A whole branch down.** Kill all three nodes of Branch A. Branch B keeps serving its own customers; cross-branch transfers are refused cleanly with no money moved.

## 5. Without bash (PowerShell or cmd)

Everything can be done with the dashboard plus a few `docker` commands. Use the dashboard to see which node is the primary.

| Action | Command |
|---|---|
| Start | `docker compose up -d --build` |
| Cluster status | `docker exec pg-a1 patronictl list` (use any running node of that branch) |
| Kill a node hard | `docker kill pg-a1` |
| Stop a node cleanly | `docker stop pg-a1` |
| Start a node again | `docker start pg-a1` |
| Load generator | `docker compose run --rm loadgen` |
| Crash the coordinator | Dashboard: pick a failure in "Failure to inject", then Send transfer |
| Restart the coordinator | `docker start coordinator` |
| What recovery did | `docker compose logs --tail 15 coordinator` |
| In-doubt transactions | `docker exec pg-a1 psql -U postgres -d bank -c "select * from pg_prepared_xacts"` (run on the primary) |
| Stop / wipe | `docker compose stop` / `docker compose --profile tools down -v` |
| Run the tests | needs bash: `./tests/verify.sh` in Git Bash or WSL |

## 6. How it works

### Sharding

`app/bank/config.py`: the first letter of the account id is the shard key. All of a branch's accounts live in that branch's cluster.

### Replication and failover

`patroni/patroni.yml` is shared by all six nodes. The important settings:

- `synchronous_mode: true`. The primary only acknowledges a commit, or a `PREPARE TRANSACTION`, after one replica has it too. Failover therefore never loses an acknowledged transaction. Without this, a failover could silently drop a prepared transaction that the coordinator has already decided to commit.
- `max_prepared_transactions: 64`. PostgreSQL ships with 0, which disables two-phase commit.
- `ttl: 20`. The leader lock in etcd expires 20 seconds after a primary stops renewing it. That is what bounds failover time after a hard kill.
- `use_pg_rewind: true`. Lets a crashed ex-primary rejoin as a replica without a full re-copy.
- `failsafe_mode: true`. If etcd itself is unreachable, a primary stays primary as long as it can still reach every other node of its cluster, so an etcd outage alone does not stop writes.

`haproxy/haproxy.cfg`: write ports health-check `GET /primary` on each node's Patroni agent, read ports check `GET /replica`. When roles change, routing follows within a couple of seconds.

`app/bank/db.py`: the application never trusts routing blindly. Write connections ask the driver for `target_session_attrs=primary`, so a connection that lands on a standby (possible for a moment after HAProxy restarts or mid-failover) is rejected and retried instead of failing a transfer. Reads go to the replica port and fall back to the primary if no replica is available.

### Two-phase commit

`app/bank/coordinator.py`, function `do_transfer`. For a cross-branch transfer:

1. Write `STARTED` to the coordinator log.
2. **Phase 1.** On each branch: `BEGIN`, update the balance, `PREPARE TRANSACTION 'bank-<id>'`. A prepared transaction is on disk, survives crashes and failover, and keeps its row locks.
3. **Decision.** Write `COMMITTING` to the log. This one write is the commit point of the whole distributed transaction. If phase 1 failed anywhere, write `ABORTING` instead.
4. **Phase 2.** `COMMIT PREPARED` (or `ROLLBACK PREPARED`) on each branch, then write the final state.

Branches are always visited in the same order (A, then B). Two opposite transfers therefore queue behind each other instead of deadlocking across the two databases, which neither database could detect by itself.

### Recovery

Function `recover`, run when the coordinator starts and every 2 seconds:

| Log state found | Meaning | Action |
|---|---|---|
| `STARTED` | Crashed before deciding | `ROLLBACK PREPARED` wherever it exists |
| `COMMITTING` | Decision was commit | `COMMIT PREPARED` wherever still pending |
| `ABORTING` | Decision was abort | `ROLLBACK PREPARED` wherever still pending |

Phase 2 is idempotent: if the prepared transaction is no longer listed on a branch, an earlier attempt finished it. If a branch is unreachable (mid-failover), the transaction simply stays in its state and is retried.

Two details worth knowing, because they are the kind of thing that goes wrong in real systems:

- "Not listed, so already done" is only trusted on a fully promoted primary. During a failover a connection can briefly land on a node that is still a standby, and standbys do not list prepared transactions. The code checks `pg_is_in_recovery()` first. This was a real bug found while testing this project under repeated failovers.
- `sweep_orphans` is a safety net from the participants' side: every 10 seconds it asks each branch for prepared transactions older than 15 seconds and applies the coordinator log's final verdict if there is one. A prepared transaction can never hold locks forever.

The coordinator can restart while a branch is completely down. It remembers in its log database which branches were set up, starts without the missing one, resolves what it can, and retries the rest when the branch returns.

## 7. Project layout

```
distbank/
  docker-compose.yml        all 12 services
  demo.sh                   helper commands for the demo
  patroni/
    Dockerfile              PostgreSQL 16 + Patroni
    patroni.yml             replication / failover settings (shared by all nodes)
  haproxy/
    haproxy.cfg             write routing, read load balancing, stats page
  app/
    Dockerfile
    requirements.txt
    bank/
      config.py             branches, ports, seed data, shard routing rule
      db.py                 connection helpers (always through HAProxy)
      schema.py             creates databases, tables and seed accounts
      coordinator.py        bank API, two-phase commit, crash injection, recovery
      dashboard.py          data feed for the status page
      loadgen.py            traffic generator
      checks.py             assertions used by the test suite
      static/dashboard.html the status page
  tests/
    verify.sh               19 end-to-end tests (see section 2)
```

## 8. API

Base address http://localhost:8000. Try it interactively at `/docs`.

| Call | Purpose |
|---|---|
| `POST /transfer` `{"from_account":"A-1001","to_account":"B-1002","amount":500}` | Transfer. Optional `"fault"`: `crash_before_decision`, `crash_after_decision`, `crash_mid_commit` |
| `POST /deposit`, `POST /withdraw` `{"account":"A-1001","amount":100}` | Cash in / out against the branch vault |
| `GET /accounts/A-1001` | Balance, read from a replica. Add `?consistent=true` to read from the primary |
| `GET /accounts` | All accounts |
| `GET /transactions?limit=20` | Recent coordinator log entries |
| `POST /recover` | Run a recovery pass now |

You can also connect any SQL client straight to the clusters through HAProxy: host `localhost`, ports 5500 (A writes), 5501 (A reads), 5502 (B writes), 5503 (B reads), database `bank`, user `bank`, password `bank-demo`.

## 9. Troubleshooting

- **"port is already allocated"**: something else uses 8080, 8000, 8404 or 5500 to 5503. Stop it, or change the left-hand number of that port mapping in `docker-compose.yml` (if you move 8404, the "HAProxy statistics" link on the dashboard will point to the old port).
- **`./demo.sh up` says not ready after 3 minutes**: run `docker compose ps` and `docker compose logs coordinator`. The coordinator prints what it is still waiting for.
- **A node will not rejoin, or things look confused after many experiments**: `./demo.sh reset` then `./demo.sh up` gives a clean system in under a minute.
- **`./demo.sh psql a` fails in Git Bash with a TTY error**: run `winpty ./demo.sh psql a`.
- **`demo.sh: bad interpreter` on Windows**: the file got Windows line endings. Run `sed -i 's/\r$//' demo.sh`.
- **Laptop is slow**: remove one replica per branch (delete `pg-a3` and `pg-b3` from `docker-compose.yml` and `haproxy/haproxy.cfg`). Reads then all go to the single replica.

## 10. Known limits (good answers for question time)

- **The coordinator is a single point of failure for cross-branch transfers.** While it is down, prepared transactions stay locked. This is the classic blocking weakness of two-phase commit; real systems replace the single coordinator with a replicated one (Paxos/Raft commit, as in Spanner or CockroachDB). Local transfers inside one branch do not depend on it at the database level.
- **etcd is a single node here.** If it dies, the databases keep running (failsafe mode) but no failover can happen until it is back. Production uses 3 or 5 etcd nodes.
- **HAProxy is a single node too.** Restarting it is harmless, but while it is down nothing can reach the databases. Production runs two with a floating IP.
- **Two-phase commit gives atomicity, not cross-shard isolation.** Between "committed on A" and "committed on B" (normally a few milliseconds) a reader that sums both branches sees the money missing. The dashboard labels this "settling". A globally consistent snapshot needs more machinery (global timestamps or snapshot coordination).
- **Replica reads can be slightly stale.** One replica is synchronous for durability, but "received" is not "applied", and the other replica is asynchronous. `?consistent=true` reads from the primary.
- **Failover is not instant.** Writes to the affected branch fail for about 5 to 10 s (clean stop) or 20 to 30 s (hard kill). The client is expected to retry.
- **Passwords are hard-coded demo values** in `patroni.yml`, `config.py` and `docker-compose.yml`.

## 11. How this was tested

Everything in this folder was run as a real 12-container Docker Compose stack, using the unmodified `docker-compose.yml` and building both Dockerfiles.

**Automated suite (`tests/verify.sh`): 19 of 19 pass on three consecutive full runs**, the last one from a freshly unzipped copy with the images rebuilt from nothing.

| Area | What the tests establish |
|---|---|
| API | Cross-branch and local transfers move exactly the right amounts; 10 kinds of invalid request are rejected with no balance changed; deposits, withdrawals, replica and primary reads |
| Replication | A committed write is on both replicas within about 10 ms; replicas refuse writes; one replica is synchronous, one asynchronous |
| Load balancing | Reads split evenly over the two replicas (20 / 21 of 41), none to the primary; writes only to the primary |
| Concurrency | 150 transfers from 10 parallel clients on the same accounts in opposite directions: all commit, every balance equals start plus exactly the committed transfers |
| Recovery | Coordinator crash at three points: rolled back, committed, and completed respectively; locked rows stay locked while in doubt and are released afterwards |
| Fault tolerance | Hard-kill of a primary under load: new primary after about 20 s, **0 failed reads**, old node rejoins as a replica in about 5 s. Clean stop: about 6 s |
| Combined failures | An in-doubt transaction survives a primary failover and is committed on the new primary; another survives stopping all 12 containers |
| Availability | Branch keeps working with both replicas dead; etcd outage of 30 s does not interrupt reads or writes; HAProxy restart is transparent |
| Outages | Coordinator log database down: cross-branch refused cleanly, local transfers continue. Whole branch down: other branch unaffected and the coordinator can restart |
| Persistence | Data survives `docker compose down` followed by `up` |

**Soak test.** Six primary failovers in a row (three clean, three hard kills) under continuous load from six clients: 2,198 transfers committed, 5,087 reads with 0 failures, total correct at the end, and the safety-net sweep never had to repair anything.

**Dashboard in a real browser (headless Chrome): 24 of 24 checks pass.** Rendering, transfers, error display, crash injection from the drop-down, the in-doubt boxes, the recovery button, a node shown as Down and returning, no JavaScript errors, no overflow on a phone-width screen.

**Bugs found and fixed during this testing:**

1. During a failover the coordinator could reach a node that was still a standby, see no prepared transaction there, and wrongly mark a transfer as finished, leaving half of it uncommitted. Fixed by refusing to draw that conclusion on a node in recovery, plus the orphan sweep as a second line of defence.
2. For a moment after an HAProxy restart, write connections could be routed to a replica and fail. Fixed with `target_session_attrs=primary` and a short retry.
3. The coordinator refused to start while any branch was down. It now starts without it.
4. `./demo.sh recover` also restarted stopped database nodes as a side effect. It now starts only the coordinator.
5. Host ports 5000 and 7000 clash with AirPlay on macOS. Moved to 5500 to 5503 and 8404.
6. Container data was lost on `docker compose down`. All data is now in named volumes.

**What could not be tested, and why.** The test environment is not allowed to download from Docker Hub, Quay or the Debian package servers. So the four base images named in the project (`postgres:16`, `python:3.12-slim`, `haproxy:2.8`, `quay.io/coreos/etcd:v3.5.17`) were replaced by local stand-ins with the same names, built on Ubuntu 24.04 with the same layout: PostgreSQL 16 with the official image's own entrypoint script, Python 3.12, HAProxy 2.8 with its official entrypoint script, and the real etcd 3.5.17 release binary. The project's own Dockerfiles were then built on top of those, unchanged. What remains unverified is therefore narrow:

- the real base images are Debian-based, so the `apt-get install python3 python3-venv curl` line in `patroni/Dockerfile` has run on Ubuntu but not on Debian;
- Docker Desktop on Windows or macOS (tested on Linux only);
- `demo.sh` and `tests/verify.sh` under Git Bash and under macOS's older bash 3.2 (they pass `shellcheck` and avoid newer bash features, but were only run with bash 5).

`./tests/verify.sh` exists to close that gap: run it on your own machine and you have the same evidence there.
