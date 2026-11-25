# Demo kit

Helpers for presenting the multi-laptop setup (`multi/`) from any team laptop.

| File | What it does |
|---|---|
| `helpers.zsh` | `pg HOST PORT "SQL" [db]`, `pgA` / `pgB` (SQL on the current primary), `primA` / `primB`, `leader`. Load with `source <repo>/demo/helpers.zsh` (zsh or bash). psql runs inside one of this laptop's own `pg-*` containers, so nothing has to be installed. |
| `transfer.sh FROM TO AMOUNT` | A transfer through the leader coordinator, printing every two-phase-commit step. |
| `watch-db.sh` | Live view: node roles, coordinator leader, each branch's primary, total and in-doubt count (refreshes every 3 s). |
| `DEMO-COMMANDS-Harsh.txt` | The full demo script with commands and expected output, written for Harsh's laptop (10.60.13.12). On another laptop, replace the repo path (`~/Desktop/ADBMS/distbank` -> e.g. `~/DistBank`) and run `patronictl` in that laptop's own node container (e.g. `docker exec pg-a3 patronictl list`). |

The scripts read the laptop IPs from `multi/cluster.env`, so they follow any change there.
