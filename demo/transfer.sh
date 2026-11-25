#!/usr/bin/env bash
# Cross-branch (or local) transfer through the LEADER coordinator, printing
# every two-phase-commit step.   Usage: transfer.sh FROM TO AMOUNT
set -u
[ $# -eq 3 ] || { echo "usage: $0 FROM TO AMOUNT   e.g. $0 A-1001 B-1001 500"; exit 2; }
# shellcheck disable=SC1091
. "$(dirname "$0")/helpers.zsh" >/dev/null
L=$(leader) || { echo "No leader coordinator right now (election in progress); retry in 15 s."; exit 1; }
echo "Leader coordinator: $(curl -s -m 3 "http://$L:8000/health") at $L"
out=$(curl -s -m 60 -w $'\n%{http_code}' -X POST "http://$L:8000/transfer" \
  -H 'content-type: application/json' \
  -d "{\"from_account\":\"$1\",\"to_account\":\"$2\",\"amount\":$3}")
code=${out##*$'\n'}; body=${out%$'\n'*}
echo "HTTP   : $code"
printf '%s' "$body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("error  : no JSON reply (coordinator crashed or unreachable)"); sys.exit()
det = d.get("detail")
if isinstance(det, dict): d = {**d, **det}
elif isinstance(det, str): print("error  :", det)
for k in ("txn_id", "kind", "state", "error"):
    if d.get(k): print(f"{k:<7}: {d[k]}")
if d.get("steps"):
    print("steps  :")
    for s in d["steps"]: print("       ", s)
'
