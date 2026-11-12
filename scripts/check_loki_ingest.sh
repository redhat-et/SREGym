#!/usr/bin/env bash
# Does Loki hold log lines? "Loki is Running" is not enough, and neither is Promtail: on a managed
# OpenShift cluster Promtail's DaemonSet can exist with zero pods because no security context
# constraint admits it. This script separates the two questions.
#
# The assertion is the label query against Loki's own API, made from this workstation through
# `oc port-forward`. It is deliberately not made with `oc exec ... wget` inside the Loki pod:
# `grafana/loki` ships no `wget` and no `curl`, so that check could only ever report a failure of
# the tool, not an absence of log lines.
#
# Run it as the operator, while a run is live and before teardown.
#
# Exit 0: Loki returns at least one label, so something pushed.
# Exit 1: Loki answers and has no labels, so nothing pushed.
# Exit 2: the query could not be made, so nothing is known.
set -u

NS="${LOKI_NAMESPACE:-observe}"
PORT="${LOKI_PORT:-13100}"

command -v oc >/dev/null || { echo "exit 2: oc not found"; exit 2; }

echo "== Promtail DaemonSet =="
DESIRED="$(oc -n "$NS" get ds promtail -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null)"
CURRENT="$(oc -n "$NS" get ds promtail -o jsonpath='{.status.currentNumberScheduled}' 2>/dev/null)"
READY="$(oc -n "$NS" get ds promtail -o jsonpath='{.status.numberReady}' 2>/dev/null)"
if [ -z "${DESIRED:-}" ]; then
  echo "no promtail DaemonSet in $NS: it was never deployed, so nothing can push"
elif [ "${CURRENT:-0}" = "0" ]; then
  echo "promtail: desired ${DESIRED}, current ${CURRENT}, ready ${READY:-0}"
  echo "no Promtail pod exists: the admission controller refused every one of them."
  echo "a label query below can only be empty until the operator grants an SCC to its service account:"
  echo "  oc adm policy add-scc-to-user privileged -z promtail -n $NS"
  echo "  oc -n $NS get events --field-selector reason=FailedCreate | tail -3   # what was refused"
else
  echo "promtail: desired ${DESIRED}, current ${CURRENT}, ready ${READY:-0}"
  echo "== Promtail log scan (informational: a Promtail that never pushed also prints nothing) =="
  oc -n "$NS" logs -l app.kubernetes.io/name=promtail --tail=200 --prefix 2>&1 \
    | grep -Ei 'error|warn|404|5[0-9][0-9]' | tail -n 10 || echo "no matching lines"
fi

echo "== Loki label query (the assertion) =="
POD="$(oc -n "$NS" get pod -l app.kubernetes.io/name=loki,app.kubernetes.io/component=single-binary -o name 2>/dev/null | head -n 1)"
[ -n "$POD" ] || POD="$(oc -n "$NS" get pod -l app.kubernetes.io/name=loki -o name 2>/dev/null | head -n 1)"
[ -n "$POD" ] || { echo "exit 2: no Loki pod in $NS"; exit 2; }

oc -n "$NS" port-forward "$POD" "$PORT:3100" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill "$PF_PID" 2>/dev/null' EXIT

BODY=""
for _ in $(seq 1 30); do
  if command -v curl >/dev/null; then
    BODY="$(curl -s --max-time 3 "http://127.0.0.1:$PORT/loki/api/v1/labels" 2>/dev/null)" && [ -n "$BODY" ] && break
  else
    BODY="$(python3 -c "
import urllib.request
print(urllib.request.urlopen('http://127.0.0.1:$PORT/loki/api/v1/labels', timeout=3).read().decode())
" 2>/dev/null)" && [ -n "$BODY" ] && break
  fi
  sleep 1
done

if [ -z "$BODY" ]; then
  echo "exit 2: Loki's API did not answer on $POD through a port-forward; nothing is known"
  exit 2
fi

echo "$BODY"
if echo "$BODY" | grep -q '"status":"success"' && echo "$BODY" | grep -Eq '"data":\[[^]]+\]'; then
  echo "OK: Loki returns labels, so log lines are arriving."
  exit 0
fi
echo "FAIL: Loki answered and returned no labels, so nothing reached it. Check the Promtail push URL and the lines above."
exit 1
