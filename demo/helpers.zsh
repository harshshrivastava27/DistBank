# DistBank demo helpers. Load in each terminal window (zsh or bash):
#   source <path to the repo>/demo/helpers.zsh
#   e.g. Harsh:  source ~/Desktop/ADBMS/distbank/demo/helpers.zsh
#        others: source ~/DistBank/demo/helpers.zsh
#
#   pg HOST PORT "SQL" [db]   run SQL on one specific node (default db: bank)
#   pgA "SQL" [db]            run SQL on Branch A's current primary
#   pgB "SQL" [db]            run SQL on Branch B's current primary
#   primA / primB             print host and port of the current primary
#   leader                    print IP of the leader coordinator; use as $(leader)
#
# psql runs inside a database container on THIS laptop (its own pg-* node),
# so nothing has to be installed.

# The multi/ folder of the repo this file lives in (works from any laptop/path)
if [ -n "${BASH_SOURCE:-}" ]; then _distbank_here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
else _distbank_here=$(cd "$(dirname "${(%):-%x}")" && pwd); fi
DISTBANK_MULTI="${DISTBANK_MULTI:-$(cd "$_distbank_here/../multi" && pwd)}"
DISTBANK_PGPASSWORD="postgres-demo"

# Laptop IPs per node, from the shared cluster.env (so an IP change is picked up)
_db_ip() {  # _db_ip pg-a1  -> 10.60.14.88
  local var
  var="PG_$(echo "$1" | cut -d- -f2 | tr 'a-z' 'A-Z')_HOST"
  grep -E "^(${var}|PG_$(echo "$1" | cut -d- -f2 | tr 'a-z' 'A-Z')_IP)=" "$DISTBANK_MULTI/cluster.env" | head -1 | cut -d= -f2 | tr -d '"'
}
_db_ports() {  # _db_ports pg-a1 -> "5411 8011"
  case "$1" in
    pg-a1) echo "5411 8011";; pg-a2) echo "5412 8012";; pg-a3) echo "5413 8013";;
    pg-b1) echo "5421 8021";; pg-b2) echo "5422 8022";; pg-b3) echo "5423 8023";;
  esac
}
_db_container() {  # a running local database container to run psql in
  local c
  for c in pg-a1 pg-a2 pg-a3 pg-b1 pg-b2 pg-b3; do
    [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] && { echo "$c"; return 0; }
  done
  echo "no database container (pg-*) is running on this laptop" >&2; return 1
}

pg() {  # pg HOST PORT "SQL" [db]
  local c
  c=$(_db_container) || return 1
  docker exec -e PGPASSWORD="$DISTBANK_PGPASSWORD" "$c" \
    psql -h "$1" -p "$2" -U postgres -d "${4:-bank}" -c "$3"
}

_prim() {  # _prim a|b -> "host port" of the current primary
  local n ip ports pgport api
  for n in pg-$1"1" pg-$1"2" pg-$1"3"; do
    ip=$(_db_ip "$n"); ports=$(_db_ports "$n")
    pgport=${ports%% *}; api=${ports##* }
    if [ "$(curl -s -o /dev/null -m 2 -w '%{http_code}' "http://$ip:$api/primary")" = "200" ]; then
      echo "$ip $pgport"; return 0
    fi
  done
  echo "NONE"; return 1
}
primA() { _prim a; }
primB() { _prim b; }

_pgbranch() {  # _pgbranch a|b LABEL "SQL" [db]
  local p host port
  p=$(_prim "$1") || { echo "[Branch $2: no primary right now (failover in progress?)]"; return 1; }
  host=${p%% *}; port=${p##* }
  echo "[Branch $2 primary = $host:$port]"
  pg "$host" "$port" "$3" "${4:-bank}"
}
pgA() { _pgbranch a A "$1" "${2:-bank}"; }
pgB() { _pgbranch b B "$1" "${2:-bank}"; }

leader() {  # IP of the leader coordinator, or NONE during an election
  local v
  v=$(docker exec etcd-member etcdctl get --print-value-only /distbank/coordinator/leader 2>/dev/null)
  if [ -z "$v" ]; then echo "NONE"; return 1; fi
  echo "$v" | python3 -c "import json,sys,urllib.parse;print(urllib.parse.urlparse(json.load(sys.stdin)['url']).hostname)"
}

echo "DistBank helpers loaded: pg, pgA, pgB, primA, primB, leader"
