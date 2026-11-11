#!/usr/bin/env bash
# Operator prerequisites for an SREGym run on OpenShift. Run once per cluster, as a cluster-admin.
# The harness never runs these commands; it only checks them (sregym/service/admission_preflight.py)
# and prints them when it finds a gap.
#
#   1. Qualify the unqualified image references in the SREGym-applications manifests
#      (scripts/qualify_submodule_images.py --apply; undo with --revert).
#   2. Grant the anyuid SCC to the three application namespaces (group form, so the grant outlives
#      the per-scenario namespace).
#   3. Optional, off by default: grant the privileged SCC to Promtail's service account.
#      PROMTAIL_SCC=privileged scripts/rosa_prerun.sh
#      This is an unmeasured prerequisite. The only ROSA evidence shows Promtail admitted without a
#      binding of ours on a cluster-admin login. The promtail service account does not exist until the
#      first Loki deploy, so on a fresh cluster the step prints what to do and continues.
#
# Idempotent. Needs `oc` logged in and `uv`.
set -u

echo "== Qualify image references in the submodule manifests =="
uv run python scripts/qualify_submodule_images.py --apply || exit 1

echo "== Grant anyuid to the app namespaces (idempotent; the grant outlives the namespace) =="
command -v oc >/dev/null || { echo "oc not found"; exit 1; }
for ns in social-network hotel-reservation astronomy-shop; do
  oc adm policy add-scc-to-group anyuid "system:serviceaccounts:$ns" || exit 1
done

if [ "${PROMTAIL_SCC:-}" = "privileged" ]; then
  echo "== Grant privileged to the promtail service account in observe =="
  if oc -n observe get serviceaccount promtail >/dev/null 2>&1; then
    echo "+ oc adm policy add-scc-to-user privileged -z promtail -n observe"
    oc adm policy add-scc-to-user privileged -z promtail -n observe || exit 1
  else
    echo "The promtail service account does not exist yet (the first Loki deploy creates it)."
    echo "Re-run with PROMTAIL_SCC=privileged after the first Loki deploy."
  fi
fi

echo "Done. Next: run the benchmark, then scripts/check_loki_ingest.sh to confirm Loki holds log lines."
