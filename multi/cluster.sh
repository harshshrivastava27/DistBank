#!/usr/bin/env bash
# Multi-laptop helper. Run from the distbank folder on every laptop, with the
# SAME multi/cluster.env everywhere.
#
#   ./multi/cluster.sh check                    can this laptop reach all the others?
#   ./multi/cluster.sh etcd up|down|reset       this laptop's member of the 3-laptop etcd cluster
#   ./multi/cluster.sh check-etcd               are the etcd members reachable and healthy?
#   ./multi/cluster.sh coord up|down|reset      coordinator laptop
#   ./multi/cluster.sh node pg-a1 pg-b2 up      database laptop (list its nodes)
#   ./multi/cluster.sh node pg-a1 pg-b2 down|reset
#   ./multi/cluster.sh status                   every node's role, from any laptop
#   ./multi/cluster.sh load [seconds]           traffic generator (coordinator laptop)
#   ./multi/cluster.sh kill <node>              hard-kill a node running on THIS laptop
#   ./multi/cluster.sh start <node>             start it again
#   ./multi/cluster.sh candidate up|down        coordinator candidate (HAProxy + coordinator + dashboard)
#                                               on 2+ laptops: they elect one leader through etcd
#   ./multi/cluster.sh leader                   which coordinator leads, and the standbys
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

# etcd members: one per laptop in ETCD_IPS (3 recommended). Without ETCD_IPS,
# the old single etcd on the coordinator laptop is used.
ETCD_IPS="${ETCD_IPS:-$COORD_IP}"
ETCD_HOSTS=""; ETCD_URLS=""; ETCD_INITIAL_CLUSTER=""
for ip in $ETCD_IPS; do
  ETCD_HOSTS="${ETCD_HOSTS:+$ETCD_HOSTS,}$ip:2379"
  ETCD_URLS="${ETCD_URLS:+$ETCD_URLS,}http://$ip:2379"
  ETCD_INITIAL_CLUSTER="${ETCD_INITIAL_CLUSTER:+$ETCD_INITIAL_CLUSTER,}etcd-$(echo "$ip" | tr . -)=http://$ip:2380"
done
export ETCD_IPS ETCD_HOSTS ETCD_URLS ETCD_INITIAL_CLUSTER LAPTOP_NAMES

ALL="pg-a1 pg-a2 pg-a3 pg-b1 pg-b2 pg-b3"
ip_of()  { v="$(echo "$1" | tr 'a-z-' 'A-Z_')_IP"; eval echo "\${$v:-}"; }
port_of() { case "$1" in pg-a1) echo "5411 8011";; pg-a2) echo "5412 8012";; pg-a3) echo "5413 8013";;
                         pg-b1) echo "5421 8021";; pg-b2) echo "5422 8022";; pg-b3) echo "5423 8023";;
                         *) echo "unknown node $1" >&2; exit 1;; esac; }
tcp() {  # tcp <host> <port>  -> 0 if something answers within 3 s
  if [ "$(uname)" = Darwin ]; then nc -z -G 3 -w 3 "$1" "$2" >/dev/null 2>&1   # macOS nc needs -G for the connect timeout
  elif command -v nc >/dev/null 2>&1; then nc -z -w 3 "$1" "$2" >/dev/null 2>&1
  else (exec 3<>"/dev/tcp/$1/$2") >/dev/null 2>&1; fi
}
dc_coord() { docker compose --env-file "$ENV" -f docker-compose.coord.yml "$@"; }
dc_nodes() { docker compose --env-file "$ENV" -f docker-compose.nodes.yml "$@"; }
dc_cand()  { docker compose --env-file "$ENV" -f docker-compose.candidate.yml "$@"; }
dc_etcd()  { docker compose --env-file "$ENV" -f docker-compose.etcd.yml "$@"; }
etcd_any() {  # first etcd member that answers
  for ip in $ETCD_IPS; do tcp "$ip" 2379 && { echo "$ip"; return 0; }; done; return 1
}
this_ip() {  # this laptop's Wi-Fi address (override with THIS_IP=... in the environment)
  [ -n "${THIS_IP:-}" ] && { echo "$THIS_IP"; return; }
  ipconfig getifaddr en0 2>/dev/null && return                                  # macOS Wi-Fi
  ipconfig getifaddr en1 2>/dev/null && return
  ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | grep . && return   # Linux: the address used to go out
  hostname -I 2>/dev/null | awk '{print $1}' | grep . && return
  # Windows (Git Bash): ask PowerShell for the address of the default route
  powershell.exe -NoProfile -Command "(Get-NetIPConfiguration | Where-Object { \$_.IPv4DefaultGateway }).IPv4Address.IPAddress" 2>/dev/null \
    | tr -d '\r' | head -1
}
etcd_get() {  # etcd_get <key-or-prefix> [prefix]  -> JSON values, one per line
  k=$(printf '%s' "$1" | base64)
  if [ -n "${2:-}" ]; then e=$(printf '%s' "${1%?}$(printf '%s' "${1: -1}" | tr '/' '0')" | base64); body="{\"key\":\"$k\",\"range_end\":\"$e\"}"
  else body="{\"key\":\"$k\"}"; fi
  ip=$(etcd_any) || { echo "  (no etcd member reachable)"; return; }
  curl -s -m 3 -X POST "http://$ip:2379/v3/kv/range" -d "$body" \
    | grep -o '"value":"[^"]*"' | cut -d'"' -f4 | while read -r v; do printf '%s' "$v" | base64 -d; echo; done
}

cmd="${1:-help}"; [ $# -gt 0 ] && shift
case "$cmd" in
  check-etcd)
    up=0; n=0
    for ip in $ETCD_IPS; do
      n=$((n+1))
      printf '  %-16s ' "$ip"
      if tcp "$ip" 2379; then
        h=$(curl -s -m 3 "http://$ip:2379/health"); echo "$h" | grep -q '"true"' && up=$((up+1))
        echo "$h"
      else echo "not reachable"; fi
    done
    q=$((n/2+1))
    echo "  $up of $n members healthy (needs $q for elections to work)"
    ip=$(etcd_any) && curl -s -m 3 -X POST "http://$ip:2379/v3/cluster/member/list" -d '{}' \
      | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | tr '\n' ' ' | sed 's/^/  members: /'; echo
    [ "$up" -ge "$q" ]
    ;;

  etcd)
    THIS_IP=$(this_ip); export THIS_IP
    ETCD_MEMBER="etcd-$(echo "$THIS_IP" | tr . -)"; export ETCD_MEMBER
    case " $ETCD_IPS " in *" $THIS_IP "*) ;; *) echo "this laptop ($THIS_IP) is not in ETCD_IPS ($ETCD_IPS)" >&2; exit 1;; esac
    case "${1:-up}" in
      up)    dc_etcd up -d
             echo "etcd member $ETCD_MEMBER started. It needs a majority of ETCD_IPS up to work:"
             echo "start it on every laptop in ETCD_IPS, then:  ./multi/cluster.sh check-etcd" ;;
      down)  dc_etcd stop ;;
      reset) dc_etcd down -v ;;
      *) echo "etcd up|down|reset" >&2; exit 1 ;;
    esac ;;

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
        etcd_any >/dev/null || echo "WARNING: no etcd member reachable ($ETCD_IPS). Start etcd first:  ./multi/cluster.sh etcd up"
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

  candidate)
    THIS_IP=$(this_ip); export THIS_IP
    [ -n "$THIS_IP" ] || { echo "could not find this laptop's IP; run with THIS_IP=x.x.x.x" >&2; exit 1; }
    COORD_NAME=""
    for pair in ${LAPTOP_NAMES:-}; do [ "${pair%%=*}" = "$THIS_IP" ] && COORD_NAME="${pair#*=}"; done
    COORD_NAME="${COORD_NAME:-coord-$THIS_IP}"; export COORD_NAME LAPTOP_NAMES
    case "${1:-up}" in
      up)
        etcd_any >/dev/null || { echo "no etcd member reachable ($ETCD_IPS)" >&2; exit 1; }
        dc_cand up -d --build
        echo "Coordinator candidate $COORD_NAME ($THIS_IP) started. Dashboard: http://$THIS_IP:8080"
        echo "Who leads:  ./multi/cluster.sh leader" ;;
      down) dc_cand down ;;
      *) echo "candidate up|down" >&2; exit 1 ;;
    esac ;;

  leader)
    echo "Leader:";   etcd_get /distbank/coordinator/leader | sed 's/^/  /'
    echo "Alive:";    etcd_get /distbank/coordinator/members/ prefix | sed 's/^/  /' ;;

  kill)  docker kill "${1:?which node}" ;;
  start) docker start "${1:?which node}" ;;

  *) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
