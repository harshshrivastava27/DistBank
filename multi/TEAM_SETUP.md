# Team setup: switching all three laptops to the shared code

Instructions for the AI agent (or person) on each laptop. Follow only the
section for **your** laptop, in order, and stop at every **WAIT** until your
human confirms the other laptops are ready.

| Laptop | Owner | IP | Runs |
|---|---|---|---|
| Christy | Christy | 10.60.1.152 | etcd member, pg-a3, pg-b3, coordinator candidate |
| Yash | Yash | 10.60.14.88 | etcd member, pg-a1, pg-b1, coordinator candidate |
| Harsh | Harsh | 10.60.13.12 | etcd member, pg-a2, pg-b2, coordinator candidate |

## What is changing, and why

Today etcd runs only on Christy's laptop. When that laptop was switched off,
nothing could elect a new database primary or a new coordinator, so the bank
stopped. After this switch:

- **etcd runs on all three laptops** (one member each). Elections keep working
  while any 2 of the 3 are up, so any one laptop can be switched off.
- **Every laptop runs a coordinator candidate.** They elect one leader through
  etcd; if it dies, another takes over in about 10 s and finishes any
  half-done transfer from the shared log.
- **All three laptops run the same code**, from
  https://github.com/harshshrivastava27/DistBank

Where the data comes from: Harsh's pg-a2 and pg-b2 are the **synchronous**
replicas of both branches, so they hold every committed transaction. They are
switched first and become the primaries; the new nodes on Christy's and
Yash's laptops start empty and copy everything from them. Christy's old data
volumes are **kept** (not deleted) as a backup.

## Rules for the agent

- Use **only** the commands written here. Do not edit `cluster.sh`, the
  compose files or `cluster.env`, and do not "fix" things with commands of
  your own. If a step fails, **stop** and show your human the exact output.
- Never run `docker volume rm`, `docker system prune`, `./cluster.sh ... reset`
  or `docker compose down -v`. They delete data.
- Run every `./cluster.sh` command from the `multi` folder of the fresh clone
  (`~/DistBank/multi`), not from any older copy of the project.
- Do not open other copies of the project (for example `~/Downloads/distbank`
  or `~/Desktop/distbank`) and do not run their scripts any more.

## Order of the whole switch (all three laptops)

| Step | Who | What |
|---|---|---|
| 0 | everyone | Stop all demo traffic. Get the code. Check prerequisites. |
| 1 | Christy | Remove the old containers (old etcd, coordinator, dashboard, HAProxy, coord-db, pg-a3, pg-b3). |
| 2 | everyone | Start your etcd member. Wait until all 3 are healthy. |
| 3 | Harsh | Restart pg-a2, pg-b2 on the new etcd. Wait until both are primary. |
| 4 | Yash, Christy | Start your database nodes (they copy the data). |
| 5 | everyone | Start your coordinator candidate. |
| 6 | anyone | Check everything. |

---

## Step 0. Everyone: get the code and check prerequisites

```bash
cd ~
git clone https://github.com/harshshrivastava27/DistBank.git     # first time
# or, if ~/DistBank already exists:
cd ~/DistBank && git pull
cd ~/DistBank/multi
chmod +x cluster.sh ../demo.sh
git log --oneline -1          # must be the same commit as on Harsh's laptop
```

Check:

1. **Docker is running:** `docker ps` works (on Linux without `sudo`; if it
   needs sudo, ask your human to add the user to the `docker` group).
   `docker compose version` must work.
2. **This laptop's IP matches the table above:**
   - macOS: `ipconfig getifaddr en0`
   - Linux: `ip route get 1.1.1.1` (the address after `src`)

   If it is different, **STOP**: every laptop's `cluster.env` must be
   updated first. Tell your human.
3. **You can reach the others:** `./cluster.sh check-etcd` must at least
   print one line per laptop (it is fine that they say "not reachable" before
   step 2).
4. **Firewall (Linux only):** if `ufw` is active, allow the ports:
   ```bash
   sudo ufw allow 2379:2380/tcp; sudo ufw allow 5411:5423/tcp; sudo ufw allow 8011:8023/tcp
   sudo ufw allow 8000/tcp; sudo ufw allow 8080/tcp; sudo ufw allow 8404/tcp
   ```
   On macOS, click **Allow** if a "accept incoming connections" dialog appears.

Tell your human: **"Step 0 done on <laptop>, commit <hash>."**

---

## Christy's laptop (10.60.1.152)

### Step 1 (Christy goes first)

**WAIT** until Harsh's human confirms: "Harsh's pg-a2 and pg-b2 are sync
replicas with lag 0" (Harsh checks this right before you start).

List the old containers:

```bash
docker ps -a --format '{{.Names}}\t{{.Status}}'
```

Remove the old setup's containers. **Containers only, not volumes**: the old
data stays on disk as a backup.

```bash
docker rm -f etcd coordinator dashboard haproxy coord-db pg-a3 pg-b3 loadgen 2>/dev/null
docker ps -a --format '{{.Names}}'      # none of those names may be left
```

If containers with other names from the old project are still listed and
use port 2379, 8000, 8080, 8404, 5411-5423 or 8011-8023, show them to your
human before removing anything else.

Tell your human: **"Step 1 done: old containers removed on Christy."**

### Step 2

**WAIT** for "Step 1 done".

```bash
cd ~/DistBank/multi
./cluster.sh etcd up
./cluster.sh check-etcd
```

Repeat `./cluster.sh check-etcd` every ~10 s until it says
**"3 of 3 members healthy"** (it needs the other two laptops too).

Tell your human: **"Step 2 done on Christy: etcd 3 of 3 healthy."**

### Step 4

**WAIT** until Harsh's human confirms: "Step 3 done: pg-a2 and pg-b2 are primary".

```bash
./cluster.sh node up pg-a3 pg-b3
./cluster.sh status
```

Repeat `./cluster.sh status` every ~15 s. pg-a3 and pg-b3 go through
"creating replica" / "starting" and must end as **replica running**
(copying the data can take a minute).

Tell your human: **"Step 4 done on Christy: pg-a3, pg-b3 are replicas."**

### Step 5

```bash
./cluster.sh candidate up
./cluster.sh leader
```

`leader` lists the leader and every coordinator that is alive; **Christy**
must appear under "Alive". Tell your human: **"Step 5 done on Christy."**

---

## Yash's laptop (10.60.14.88)

### Step 2

**WAIT** for "Step 1 done: old containers removed on Christy".

```bash
cd ~/DistBank/multi
./cluster.sh etcd up
./cluster.sh check-etcd
```

If `etcd up` says it cannot find this laptop's IP, run it as
`THIS_IP=10.60.14.88 ./cluster.sh etcd up` (and use the same prefix for
`candidate up` later).

Repeat `./cluster.sh check-etcd` every ~10 s until **"3 of 3 members
healthy"**. Tell your human: **"Step 2 done on Yash: etcd 3 of 3 healthy."**

### Step 4

**WAIT** until Harsh's human confirms: "Step 3 done: pg-a2 and pg-b2 are primary".

Yash's old pg-a1 / pg-b1 containers come from the old copy of the project.
Remove the **containers** (not volumes); the new ones copy the data from the
primaries:

```bash
docker rm -f pg-a1 pg-b1 2>/dev/null
cd ~/DistBank/multi
./cluster.sh node up pg-a1 pg-b1
./cluster.sh status
```

Repeat `./cluster.sh status` every ~15 s until pg-a1 and pg-b1 are
**replica running**. Tell your human: **"Step 4 done on Yash."**

### Step 5

If anything else on this laptop already uses port 8000, 8080 or 8404
(`docker ps` shows it), show it to your human first.

```bash
./cluster.sh candidate up            # or: THIS_IP=10.60.14.88 ./cluster.sh candidate up
./cluster.sh leader
```

**Yash** must appear under "Alive". Tell your human: **"Step 5 done on Yash."**

---

## Harsh's laptop (10.60.13.12)

### Before step 1

Confirm both of Harsh's nodes are synchronous replicas, fully caught up:

```bash
cd ~/Desktop/ADBMS/distbank/multi       # Harsh's working copy
curl -s http://10.60.1.152:8013/patroni | grep -o '"application_name": *"pg-a2"[^}]*'
curl -s http://10.60.1.152:8023/patroni | grep -o '"application_name": *"pg-b2"[^}]*'
```

Both must show `"sync_state": "sync"`. Then tell Christy's human:
**"Harsh's pg-a2 and pg-b2 are sync replicas with lag 0, go ahead."**

### Step 2

**WAIT** for "Step 1 done".

```bash
./cluster.sh etcd up
./cluster.sh check-etcd       # until "3 of 3 members healthy"
```

### Step 3

**WAIT** until all three laptops report "etcd 3 of 3 healthy".

```bash
./cluster.sh node up pg-a2 pg-b2       # recreates them on the new etcd; data is kept
./cluster.sh status
```

Repeat until pg-a2 and pg-b2 are both **primary running** (about 10-30 s).
Then tell everyone: **"Step 3 done: pg-a2 and pg-b2 are primary."**

### Step 5

```bash
./cluster.sh candidate up
./cluster.sh leader
```

---

## Step 6. Final check (any laptop)

```bash
./cluster.sh check-etcd       # 3 of 3 members healthy
./cluster.sh status           # one primary + two replicas per branch, all "running"
./cluster.sh leader           # one leader; Christy, Yash and Harsh all alive
```

Open any laptop's dashboard (`http://<its IP>:8080`): six nodes, three
coordinators, and a green banner at **₹22,00,000.00**. In tab 3, press
**Audit the ledger**: it must say the books balance.

## Optional: test that any one laptop can go

1. Start customer traffic (dashboard tab 5).
2. Switch off one laptop's Wi-Fi.
3. Within about 30 s: new primaries where needed, a new coordinator leader if
   it was the leader, transfers green again, total ₹22,00,000.00.
4. Turn the Wi-Fi back on: its nodes rejoin as replicas, its coordinator as a
   standby. Nothing to run.

## If something goes wrong

Stop and show your human the exact output of the failing command plus:

```bash
./cluster.sh check-etcd
./cluster.sh status
docker ps -a --format '{{.Names}}\t{{.Status}}'
docker logs --tail 40 <the container that failed>
```

Do not delete anything to "start over": Harsh's laptop holds the
authoritative data, and Christy's old volumes are the backup.
