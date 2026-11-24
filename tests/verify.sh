#!/usr/bin/env bash
# Verifies the whole system end to end and prints PASS/FAIL per test.
#
#   ./tests/verify.sh          everything, including killing nodes (about 6 minutes)
#   ./tests/verify.sh quick    only the non-disruptive part (about 1 minute)
#
# Needs: the stack running (./demo.sh up), bash, docker. Nothing else.
# The assertions themselves live in app/bank/checks.py and run inside the
# dashboard container; this script breaks things and calls them.
set -u
cd "$(dirname "$0")/.." || exit 1

MODE="${1:-full}"
PASS=0
FAIL=0
FAILED=""
DB_NODES="pg-a1 pg-a2 pg-a3 pg-b1 pg-b2 pg-b3"
ALL="etcd coord-db $DB_NODES haproxy coordinator dashboard"

check() { docker exec dashboard python -m bank.checks "$@"; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

# Bring every container back and wait for a healthy system, whatever a test left behind.
heal() {
  for n in $ALL; do running "$n" || docker start "$n" >/dev/null 2>&1; done
  local i=0
  until running dashboard && check ready 240 >/dev/null 2>&1; do
    i=$((i + 1)); [ $i -gt 3 ] && { echo "  !! system did not become healthy again"; return 1; }
    sleep 5
  done
}

run() {  # run "<title>" <function>
  echo
  echo "== $1"
  if "$2"; then
    echo "   PASS"; PASS=$((PASS + 1))
  else
    echo "   FAIL"; FAIL=$((FAIL + 1)); FAILED="$FAILED\n   - $1"
  fi
  heal
}

# ---------------------------------------------------------------- tests
t_ready()       { check ready 240; }
t_api()         { check api; }
t_replication() { check replication A && check replication B; }
t_lb()          { check loadbalancing A && check loadbalancing B; }
t_concurrency() { check concurrency; }

crash_and_recover() {  # <fault>
  local out b
  out="$(check crash "$1" A-1001 B-1001 1000)" || { echo "$out"; return 1; }
  echo "$out" | grep -v '^BEFORE'
  b="$(echo "$out" | grep '^BEFORE')"
  docker start coordinator >/dev/null
  # shellcheck disable=SC2086
  check recovered "$1" A-1001 B-1001 1000 ${b#BEFORE }
}
t_crash_before() { crash_and_recover crash_before_decision; }
t_crash_after()  { crash_and_recover crash_after_decision; }
t_crash_mid()    { crash_and_recover crash_mid_commit; }

t_failover_under_load() {
  local p log total reads_failed transfers_ok
  p="$(check primary A)" || return 1
  log="$(mktemp)"
  docker compose run --rm -T loadgen python -m bank.loadgen --seconds 70 >"$log" 2>&1 &
  sleep 10
  echo "  load running; hard-killing $p (primary of branch A)"
  docker kill "$p" >/dev/null
  check failover A "$p" 90 || { wait; return 1; }
  wait
  total="$(grep '^TOTAL' "$log")"
  echo "  load generator: ${total#TOTAL  }"
  reads_failed="$(echo "$total" | sed -n 's/.*reads: [0-9]* ok, \([0-9]*\) failed.*/\1/p')"
  transfers_ok="$(echo "$total" | sed -n 's/.*transfers: \([0-9]*\) ok.*/\1/p')"
  rm -f "$log"
  [ -n "$reads_failed" ] && [ -n "$transfers_ok" ] || { echo "  could not read the load generator summary"; return 1; }
  [ "$transfers_ok" -ge 100 ] || { echo "  too few successful transfers"; return 1; }
  [ "$reads_failed" -le 20 ] || { echo "  reads were not kept available during the failover"; return 1; }
  echo "  reads stayed available during the failover ($reads_failed failed)"
  docker start "$p" >/dev/null
  check rejoined A "$p" 150 && check invariant 90
}

t_switchover() {
  local p
  p="$(check primary B)" || return 1
  echo "  stopping $p cleanly (primary of branch B)"
  docker stop "$p" >/dev/null
  check failover B "$p" 60 || return 1
  docker start "$p" >/dev/null
  check rejoined B "$p" 150 && check invariant 60
}

t_indoubt_survives_failover() {
  local out b p
  out="$(check crash crash_after_decision A-1001 B-1001 1000)" || { echo "$out"; return 1; }
  echo "$out" | grep -v '^BEFORE'
  b="$(echo "$out" | grep '^BEFORE')"
  p="$(check primary A)" || return 1
  echo "  hard-killing $p (primary of branch A) while the transfer is in doubt"
  docker kill "$p" >/dev/null
  check failover A "$p" 90 || return 1
  docker start coordinator >/dev/null
  # shellcheck disable=SC2086
  check recovered crash_after_decision A-1001 B-1001 1000 ${b#BEFORE } || return 1
  echo "  the prepared transaction survived the failover and was committed on the new primary"
  docker start "$p" >/dev/null
  check rejoined A "$p" 150
}

t_indoubt_survives_shutdown() {
  local out b
  out="$(check crash crash_before_decision A-1001 B-1001 1000)" || { echo "$out"; return 1; }
  echo "$out" | grep -v '^BEFORE'
  b="$(echo "$out" | grep '^BEFORE')"
  echo "  stopping every container, then starting them all again"
  # shellcheck disable=SC2086
  docker stop $ALL >/dev/null 2>&1
  for n in $ALL; do docker start "$n" >/dev/null; done
  sleep 10
  # shellcheck disable=SC2086
  check recovered crash_before_decision A-1001 B-1001 1000 ${b#BEFORE } && check ready 240 >/dev/null
}

t_replica_loss() {
  local p n
  p="$(check primary B)" || return 1
  for n in pg-b1 pg-b2 pg-b3; do [ "$n" = "$p" ] || docker kill "$n" >/dev/null; done
  echo "  both replicas of branch B killed; only $p is left"
  sleep 6
  check writes B ok && check reads B && check cross-branch ok
}

t_etcd_outage() {
  docker stop etcd >/dev/null
  echo "  etcd stopped; waiting 30 s (longer than the 20 s leader lock)"
  sleep 30
  check writes A ok && check writes B ok && check cross-branch ok || return 1
  docker start etcd >/dev/null
  sleep 10
  check ready 120 >/dev/null && echo "  etcd back; clusters healthy"
}

t_haproxy_restart() {
  docker restart haproxy >/dev/null
  echo "  HAProxy restarted"
  check cross-branch ok && check reads A && check reads B
}

t_coord_db_outage() {
  docker stop coord-db >/dev/null
  echo "  coordinator log database stopped"
  check cross-branch down && check writes A ok && check writes B ok || return 1
  docker start coord-db >/dev/null
  sleep 5
  check cross-branch ok
}

t_branch_outage() {
  docker kill pg-a1 pg-a2 pg-a3 >/dev/null
  echo "  all three nodes of branch A killed"
  sleep 5
  check writes B ok && check reads B && check cross-branch down || return 1
  docker restart coordinator >/dev/null
  echo "  coordinator restarted while branch A is still down"
  check coordinator ok && check writes B ok
}

t_persistence() {
  local before after
  before="$(check balance A-1001) $(check balance B-1001)"
  echo "  removing all containers (docker compose down), then creating them again"
  docker compose down >/dev/null 2>&1
  docker compose up -d >/dev/null 2>&1
  local i=0
  until running dashboard && check ready 240 >/dev/null 2>&1; do i=$((i + 1)); [ $i -gt 5 ] && return 1; sleep 5; done
  after="$(check balance A-1001) $(check balance B-1001)"
  echo "  balances before: $before   after: $after"
  [ "$before" = "$after" ]
}

t_final() { check invariant 120; }

# ---------------------------------------------------------------- run
running dashboard || { echo "The stack is not running. Start it first: ./demo.sh up"; exit 2; }

run "System is healthy: 1 primary + 2 replicas per branch, total correct"   t_ready
run "Bank API: valid and invalid transfers, deposits, reads"                 t_api
run "Replication: writes reach both replicas; replicas are read-only"        t_replication
run "Load balancing: reads spread over replicas, writes only to primary"     t_lb
run "Concurrency: 150 parallel transfers, no lost updates, no deadlock"      t_concurrency
run "Recovery: coordinator crash before the decision -> rolled back"         t_crash_before
run "Recovery: coordinator crash after the decision -> committed"            t_crash_after
run "Recovery: coordinator crash between the two commits -> completed"       t_crash_mid

if [ "$MODE" != "quick" ]; then
  run "Fault tolerance: hard-kill a primary under load, automatic failover"  t_failover_under_load
  run "Planned switchover: clean stop of a primary, node rejoins"            t_switchover
  run "In-doubt transaction survives a primary failover"                     t_indoubt_survives_failover
  run "In-doubt transaction survives a full shutdown of all containers"      t_indoubt_survives_shutdown
  run "Availability: branch keeps working with both replicas dead"           t_replica_loss
  run "Availability: etcd outage does not stop reads or writes"              t_etcd_outage
  run "Availability: HAProxy restart"                                        t_haproxy_restart
  run "Coordinator log database outage: refused cleanly, local work goes on" t_coord_db_outage
  run "Whole branch down: other branch unaffected, coordinator can restart"  t_branch_outage
  run "Persistence: data survives removing and recreating all containers"    t_persistence
fi
run "Final state: total correct, nothing in doubt"                           t_final

echo
echo "================================================"
echo " $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] || printf " Failed:$FAILED\n"
echo "================================================"
[ $FAIL -eq 0 ]
