#!/usr/bin/env bash
# Multi-laptop helper. Run from the distbank folder on every laptop, with the
# SAME multi/cluster.env everywhere.
#
#   ./multi/cluster.sh check                    can this laptop reach all the others?
#   ./multi/cluster.sh check-etcd               is etcd on the coordinator laptop reachable/healthy?
#   ./multi/cluster.sh coord up|down|reset      coordinator laptop
#   ./multi/cluster.sh node pg-a1 pg-b2 up      database laptop (list its nodes)
#   ./multi/cluster.sh node pg-a1 pg-b2 down|reset
#   ./multi/cluster.sh status                   every node's role, from any laptop
#   ./multi/cluster.sh load [seconds]           traffic generator (coordinator laptop)
#   ./multi/cluster.sh kill <node>              hard-kill a node running on THIS laptop
#   ./multi/cluster.sh start <node>             start it again
set -eu
cd "$(dirname "$0")" || exit 1
ENV=cluster.env
# shellcheck disable=SC1090
. "./$ENV"

# Accept both naming styles in cluster.env: COORD_IP / PG_A1_IP  or  CONTROL_IP / PG_A1_HOST.
# Exported values override the env-file inside docker compose, so either works.
COORD_IP="${COORD_IP:-${CONTROL_IP:-}}"; export COORD_IP
for _n in A1 A2 A3 B1 B2 B3; do
  eval "PG_${_n}_IP=\"\${PG_${_n}_IP:-\${PG_${_n}_HOST:-}}\"; export PG_${_n}_IP"
done
[ -n "$COORD_IP" ] || { echo "cluster.env: set COORD_IP (or CONTROL_IP)" >&2; exit 1; }

ALL="pg-a1 pg-a2 pg-a3 pg-b1 pg-b2 pg-b3"
ip_of()  { v="$(echo "$1" | tr 'a-z-' 'A-Z_')_IP"; eval echo "\${$v:-}"; }
port_of() { case "$1" in pg-a1) echo "5411 8011";; pg-a2) echo "5412 8012";; pg-a3) echo "5413 8013";;
                         pg-b1) echo "5421 8021";; pg-b2) echo "5422 8022";; pg-b3) echo "5423 8023";;
                         *) echo "unknown node $1" >&2; exit 1;; esac; }
tcp() {  # tcp <host> <port>  -> 0 if something answers
  if command -v nc >/dev/null 2>&1; then nc -z -w 3 "$1" "$2" >/dev/null 2>&1
  else (exec 3<>"/dev/tcp/$1/$2") >/dev/null 2>&1; fi
}
dc_coord() { docker compose --env-file "$ENV" -f docker-compose.coord.yml "$@"; }
dc_nodes() { docker compose --env-file "$ENV" -f docker-compose.nodes.yml "$@"; }

cmd="${1:-help}"; [ $# -gt 0 ] && shift
case "$cmd" in
  check-etcd)
    echo "etcd on the coordinator laptop: http://$COORD_IP:2379"
    if ! tcp "$COORD_IP" 2379; then
      echo "  NOT reachable. Is './multi/cluster.sh coord up' running on $COORD_IP? Firewall / Wi-Fi isolation?"; exit 1
    fi
    echo "  port 2379 open"
    printf '  health:  '; curl -s -m 3 "http://$COORD_IP:2379/health"; echo
    printf '  version: '; curl -s -m 3 "http://$COORD_IP:2379/version"; echo
    # members' advertised client URL must be the coordinator's IP, not "etcd" or localhost
    printf '  members: '; curl -s -m 3 -X POST "http://$COORD_IP:2379/v3/cluster/member/list" -d '{}' \
      | grep -o '"clientURLs":\[[^]]*\]' || echo "(could not list)"
    ;;

  check)
    echo "From this laptop, testing every address in $ENV (needs the other laptops already up)"
    printf '  %-12s %-16s ' coordinator "$COORD_IP"
    for p in 2379 5500 8000 8080; do tcp "$COORD_IP" "$p" && printf '%s ok  ' "$p" || printf '%s --  ' "$p"; done; echo
    for n in $ALL; do
      ip=$(ip_of "$n"); set -- $(port_of "$n")
      printf '  %-12s %-16s ' "$n" "$ip"
      tcp "$ip" "$1" && printf 'postgres ok  ' || printf 'postgres --  '
      tcp "$ip" "$2" && printf 'patroni ok' || printf 'patroni --'; echo
    done
    echo "'--' = not reachable: that service isn't up yet, the laptop's firewall blocks it,"
    echo "or the Wi-Fi isolates devices (use a phone hotspot or Tailscale then)."
    ;;

  coord)
    action="${1:-up}"
    case "$action" in
      up)
        dc_coord up -d --build
        echo "Dashboard: http://$COORD_IP:8080   API: http://$COORD_IP:8000/docs   HAProxy: http://$COORD_IP:8404"
        echo "Now start the database nodes on the other laptops."
        echo "(it waits for them on its own)" ;;
      down)  dc_coord --profile tools down ;;
      reset) dc_coord --profile tools down -v ;;
      logs)  docker logs -f coordinator ;;
      *) echo "coord up|down|reset|logs" >&2; exit 1 ;;
    esac ;;

  node)
    nodes=""; action=""
    for a in "$@"; do case "$a" in pg-*) nodes="$nodes $a";; *) action="$a";; esac; done
    [ -n "$nodes" ] || { echo "say which nodes run here, e.g.  node pg-a1 pg-b2 up" >&2; exit 1; }
    profiles=""; for n in $nodes; do profiles="$profiles --profile $n"; done
    case "${action:-up}" in
      up)
        tcp "$COORD_IP" 2379 || echo "WARNING: etcd at $COORD_IP:2379 not reachable yet. Start the coordinator laptop first."
        # shellcheck disable=SC2086
        dc_nodes $profiles up -d --build $nodes ;;
      down)  dc_nodes $profiles stop $nodes ;;
      reset) dc_nodes $profiles down -v ;;
      *) echo "node <nodes...> up|down|reset" >&2; exit 1 ;;
    esac ;;

  status)
    for n in $ALL; do
      ip=$(ip_of "$n"); set -- $(port_of "$n")
      info=$(curl -s -m 2 "http://$ip:$2/patroni" 2>/dev/null || true)
      # first match only: the "replication" list repeats "state"/"role" per replica
      role=$(echo "$info" | grep -o '"role": *"[a-z_]*"' | head -1 | sed 's/.*"\([a-z_]*\)"$/\1/')
      state=$(echo "$info" | grep -o '"state": *"[a-z ]*"' | head -1 | sed 's/.*"\([a-z ]*\)"$/\1/')
      [ "$role" = "master" ] && role=primary
      printf '  %-6s %-16s %-10s %s\n' "$n" "$ip" "${role:-DOWN}" "$state"
    done ;;

  load)
    if [ -n "${1:-}" ]; then dc_coord --profile tools run --rm loadgen python -m bank.loadgen --seconds "$1"
    else dc_coord --profile tools run --rm loadgen; fi ;;

  kill)  docker kill "${1:?which node}" ;;
  start) docker start "${1:?which node}" ;;

  *) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
