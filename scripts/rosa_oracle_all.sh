#!/usr/bin/env bash
# Oracle-only lifecycle (inject -> oracle fails -> recover -> oracle passes) for the 16 Lite scenarios
# that OpenShift can express, one scenario at a time, with a damage verdict after each.
# The 17th, service_dns_resolution_failure_social_network, is excluded: it writes kube-system/coredns.
# A failure does not stop the loop, except that after the first failure inside an app group the
# rest of that group is skipped (the same defect would repeat for each scenario).
# Before the loop it runs scripts/rosa_prerun.sh, which makes the cluster ready: it qualifies the
# unqualified image references in the submodule manifests, grants the anyuid SCC to the app namespaces
# and an SCC to Promtail's service account, then reports the rest. Those are cluster/tree changes made by
# YOU running this script; the harness never makes them.
# Usage: scripts/rosa_oracle_all.sh [output-dir]      (default: logs/rosa-oracle-all)
# Env:   KEEP_GOING=1  keep running a group after a failure (default: skip the rest of the group)
#        ONLY="hotel"  run only the named groups (social hotel astronomy), e.g. ONLY="hotel astronomy"
#        RERUN_PASSED=1  rerun scenarios already marked exit=0 in RESULTS.txt (default: skip them)
set -u
OUT="${1:-logs/rosa-oracle-all}"
mkdir -p "$OUT"

echo "== Operator prerequisites (scripts/rosa_prerun.sh) =="
bash "$(dirname "$0")/rosa_prerun.sh"
prerun_rc=$?
if [ "$prerun_rc" -eq 1 ]; then
  echo "rosa_prerun failed a step; not starting the sweep" | tee -a "$OUT/RESULTS.txt"
  exit 1
fi
if [ "$prerun_rc" -eq 2 ]; then
  echo "rosa_prerun reports prerequisites that only an operator can supply; the oracle sweep needs the" \
       "images and the anyuid grants only, so it continues. See the report above." | tee -a "$OUT/RESULTS.txt"
fi

echo "== Platform support check (no cluster access) =="
SREGYM_PLATFORM=openshift uv run python -c "import main; main.platform_support_skips('sregym-lite')" | grep Skipping

GROUPS_LIST=(${ONLY:-social hotel astronomy})
social=(
  duplicate_pvc_mounts_social_network
  mutating_webhook_resource_limits_social_network
  rolling_update_misconfigured_social_network
)
hotel=(
  network_policy_block
  namespace_memory_limit
  cronjob_sidecar_blocks_completion_hotel_reservation
  finalizer_deadlock_controller_hotel_reservation
  service_wrong_pod_selection_hotel_reservation
  search_rate_retry_collapse_hotel_reservation
)
astronomy=(
  env_variable_shadowing_astronomy_shop
  internal_traffic_policy_local_astronomy_shop
  secret_rotation_stale_env_credentials_astronomy_shop
  kafka_poison_pill_hol_block
  valkey_auth_disruption
  edge_request_filter_cpu_saturation
  unschedulable_incorrect_port_assignment
)

for group in "${GROUPS_LIST[@]}"; do
  unset -n scenarios 2>/dev/null; declare -n scenarios="$group"
  for p in "${scenarios[@]}"; do
    if [ -z "${RERUN_PASSED:-}" ] && grep -qE " $p exit=0$" "$OUT/RESULTS.txt" 2>/dev/null; then
      echo "$group $p already passed; skipping"
      continue
    fi
    # A fresh cache per run: a stale baseline from an earlier attempt would be compared against.
    rm -rf "$OUT/cache-$p"
    SREGYM_CACHE_DIR="$OUT/cache-$p" \
    uv run python tests/integration/validate_problem.py --problem "$p" --profile full \
      --platform openshift --storage-class gp3 --teardown-observers \
      --summary "$OUT/$p.md" --json-summary "$OUT/$p.json" > "$OUT/$p.log" 2>&1
    rc=$?
    echo "$group $p exit=$rc" | tee -a "$OUT/RESULTS.txt"
    if [ "$rc" -ne 0 ] && [ -z "${KEEP_GOING:-}" ]; then
      echo "$group: first failure at $p; skipping the rest of this group" | tee -a "$OUT/RESULTS.txt"
      break
    fi
  done
done
echo "Done. See $OUT/RESULTS.txt"
