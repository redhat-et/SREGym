#!/usr/bin/env bash
# Does Loki hold log lines? "Loki is Running" is not enough: Promtail reports Ready even when its push
# URL is wrong, and then ingests nothing. Run this against a live run (before teardown), as the operator.
#
# The assertion is the label query: it must return at least one label. The Promtail log scan is
# informational only, because a Promtail that never pushed also prints nothing.
# Exit 0: Loki has labels. Exit 1: it has none, or it could not be queried.
set -u
NS="${LOKI_NAMESPACE:-observe}"
command -v oc >/dev/null || { echo "oc not found"; exit 1; }

echo "== Promtail log scan (informational) =="
oc -n "$NS" logs -l app.kubernetes.io/name=promtail --tail=200 --prefix 2>&1 \
  | grep -Ei 'error|warn|404|5[0-9][0-9]' | tail -n 10 || echo "no matching lines"

echo "== Loki label query (the assertion) =="
POD="$(oc -n "$NS" get pod -l app.kubernetes.io/name=loki,app.kubernetes.io/component=single-binary -o name 2>/dev/null | head -n 1)"
[ -n "$POD" ] || POD="pod/loki-0"
BODY="$(oc -n "$NS" exec "$POD" -- wget -qO- "http://localhost:3100/loki/api/v1/labels" 2>&1)"
echo "$BODY"
if echo "$BODY" | grep -q '"status":"success"' && echo "$BODY" | grep -Eq '"data":\[[^]]+\]'; then
  echo "OK: Loki returns labels, so Promtail's pushes are arriving."
  exit 0
fi
echo "FAIL: Loki returned no labels. Check the Promtail push URL and Promtail's logs."
exit 1
