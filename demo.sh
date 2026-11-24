#!/usr/bin/env bash
# Helper commands for the live demo.  Usage:  ./demo.sh <command> [arguments]
# Works in Linux, macOS, WSL and Git Bash. Run it from the project folder.
set -eu
cd "$(dirname "$0")" || exit 1

usage() {
  cat <<'USAGE'
Setup
  up                      build and start everything, wait until it is ready
  down                    stop everything (data is kept)
  reset                   stop everything and delete all data (fresh start next time)

Look
  status                  show both clusters: who is primary, who is replica, lag
  replication <a|b>       show the primary's view of its replicas (pg_stat_replication)
  total                   sum of all balances across both branches
  in-doubt                list prepared (in-doubt) transactions on both branches
  logs                    follow the coordinator's log
  psql <a|b>              open a SQL prompt on that branch's current primary

Traffic
  transfer <from> <to> <amount>     e.g.  ./demo.sh transfer A-1001 B-1002 500
  load [seconds]                    run the load generator (default: until Ctrl-C)

Break things
  kill-primary <a|b>      hard-kill the branch's primary (like pulling the power plug)
  stop-primary <a|b>      shut the primary down cleanly (fast, planned failover)
  kill <node>             hard-kill one node, e.g.  ./demo.sh kill pg-a2
  crash <before|after|mid>   start a cross-branch transfer and crash the coordinator:
                             before = after PREPARE, before the decision   -> recovery rolls back
                             after  = after the COMMIT decision is logged  -> recovery commits
                             mid    = after committing on one branch only  -> recovery finishes

Repair
  start-all               start every stopped database node again
  recover                 start the coordinator again and show what recovery did
USAGE
}

ALL_NODES="pg-a1 pg-a2 pg-a3 pg-b1 pg-b2 pg-b3"
API="http://localhost:8000"

nodes_of() {
  case "$(echo "${1:-}" | tr 'AB' 'ab')" in
    a) echo "pg-a1 pg-a2 pg-a3" ;;
    b) echo "pg-b1 pg-b2 pg-b3" ;;
    *) echo "please say which branch: a or b" >&2; exit 1 ;;
  esac
}

running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

# Ask each node's Patroni agent whether it is the primary.
primary_of() {
  for n in $(nodes_of "$1"); do
    if running "$n" && [ "$(docker exec "$n" curl -s -o /dev/null -w '%{http_code}' http://localhost:8008/primary 2>/dev/null)" = "200" ]; then
      echo "$n"; return 0
    fi
  done
  echo "branch $1 has no primary right now (failover in progress?)" >&2
  return 1
}

any_running() {
  for n in $(nodes_of "$1"); do if running "$n"; then echo "$n"; return 0; fi; done
  echo "no node of branch $1 is running" >&2; return 1
}

sql() {  # sql <branch> <query>
  docker exec "$(primary_of "$1")" psql -U postgres -d bank -c "$2"
}

cmd="${1:-help}"
[ $# -gt 0 ] && shift

case "$cmd" in
  up)
    docker compose up -d --build
    echo "Waiting for both clusters to elect a primary and for the coordinator to start..."
    i=0
    until curl -s $API/health 2>/dev/null | grep -q '"status":"ok"'; do
      i=$((i + 1))
      if [ $i -gt 90 ]; then echo "Still not ready after 3 minutes. Check: docker compose logs coordinator"; exit 1; fi
      sleep 2
    done
    echo
    echo "Ready."
    echo "  Dashboard        http://localhost:8080"
    echo "  HAProxy stats    http://localhost:8404"
    echo "  Coordinator API  http://localhost:8000/docs"
    ;;
  down)   docker compose stop ;;
  reset)  docker compose --profile tools down -v --remove-orphans ;;

  status)
    for b in a b; do
      n="$(any_running $b)" && docker exec "$n" patronictl list || true
    done
    ;;
  replication)
    sql "${1:-}" "SELECT application_name AS replica, state, sync_state, sent_lsn, replay_lsn, replay_lag FROM pg_stat_replication ORDER BY 1"
    ;;
  total)
    for b in a b; do
      echo "Branch $b:"; sql $b "SELECT count(*) AS accounts, sum(balance) AS total FROM accounts"
    done
    echo "The two totals must add up to 2200000.00"
    ;;
  in-doubt)
    for b in a b; do
      echo "Branch $b:"; sql $b "SELECT gid, prepared, owner, database FROM pg_prepared_xacts ORDER BY prepared" || true
    done
    ;;
  logs)   docker compose logs -f --tail 40 coordinator ;;
  psql)   docker exec -it "$(primary_of "${1:-}")" psql -U postgres -d bank ;;

  transfer)
    [ $# -eq 3 ] || { echo "usage: ./demo.sh transfer <from> <to> <amount>"; exit 1; }
    curl -s -X POST "$API/transfer" -H 'content-type: application/json' \
      -d "{\"from_account\":\"$1\",\"to_account\":\"$2\",\"amount\":$3}"
    echo
    ;;
  load)
    if [ -n "${1:-}" ]; then
      docker compose run --rm loadgen python -m bank.loadgen --seconds "$1"
    else
      docker compose run --rm loadgen
    fi
    ;;

  kill-primary)
    p="$(primary_of "${1:-}")"
    echo "Hard-killing $p (primary of branch $1). A replica should take over within about 30 seconds."
    docker kill "$p" >/dev/null
    ;;
  stop-primary)
    p="$(primary_of "${1:-}")"
    echo "Stopping $p cleanly (primary of branch $1). A replica should take over within a few seconds."
    docker stop "$p" >/dev/null
    ;;
  kill)
    [ -n "${1:-}" ] || { echo "usage: ./demo.sh kill <node>   (one of: $ALL_NODES)"; exit 1; }
    docker kill "$1" >/dev/null && echo "killed $1"
    ;;
  crash)
    case "${1:-}" in
      before) fault=crash_before_decision ;;
      after)  fault=crash_after_decision ;;
      mid)    fault=crash_mid_commit ;;
      *) echo "usage: ./demo.sh crash <before|after|mid>"; exit 1 ;;
    esac
    echo "Starting transfer A-1001 -> B-1001 of 1000 and crashing the coordinator ($fault)..."
    curl -s -X POST "$API/transfer" -H 'content-type: application/json' \
      -d "{\"from_account\":\"A-1001\",\"to_account\":\"B-1001\",\"amount\":1000,\"fault\":\"$fault\"}" || true
    echo
    echo "The coordinator is now down. Look at the dashboard, or run: ./demo.sh in-doubt"
    echo "Bring it back with: ./demo.sh recover"
    ;;

  start-all)
    for n in $ALL_NODES; do
      if ! running "$n"; then echo "starting $n"; docker start "$n" >/dev/null; fi
    done
    echo "Stopped nodes rejoin as replicas and catch up. Check with: ./demo.sh status"
    ;;
  recover)
    # plain "docker start": "docker compose start" would also start every
    # stopped database node, because the coordinator depends on them
    docker start coordinator >/dev/null
    echo "Coordinator started. Waiting for its recovery pass..."
    i=0
    until curl -s $API/health 2>/dev/null | grep -q '"status":"ok"'; do
      i=$((i + 1)); [ $i -gt 30 ] && break; sleep 1
    done
    docker compose logs --tail 12 coordinator | grep -E "recovery|startup|fault" || true
    ;;

  help|-h|--help) usage ;;
  *) echo "unknown command: $cmd"; echo; usage; exit 1 ;;
esac
