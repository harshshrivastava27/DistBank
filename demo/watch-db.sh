#!/usr/bin/env bash
# Live database view for the demo (refreshes every 3 s; Ctrl-C to stop).
# shellcheck disable=SC1091
. "$(dirname "$0")/helpers.zsh" >/dev/null
cd "$DISTBANK_MULTI" || exit 1
while true; do
  out=$(
    echo "DistBank live view · $(date +%T) · $(hostname -s)"
    echo
    ./cluster.sh status
    echo
    printf 'Coordinator leader: %s\n' "$(leader)"
    for b in A B; do
      echo
      sum=$("pg$b" "select sum(balance) from accounts" 2>/dev/null | sed -n '4p' | tr -d ' ')
      doubt=$("pg$b" "select count(*) from pg_prepared_xacts" 2>/dev/null | sed -n '4p' | tr -d ' ')
      echo "Branch $b  primary $(prim$b)   total ${sum:-?}   in doubt ${doubt:-?}"
    done
  )
  clear; echo "$out"
  sleep 3
done
