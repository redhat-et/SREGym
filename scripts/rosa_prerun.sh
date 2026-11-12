#!/usr/bin/env bash
# Make one OpenShift cluster ready for a SREGym Lite run. Run it once per cluster, as a cluster-admin,
# before `main.py --suite sregym-lite`. Everything a run needs from the operator is here and nothing
# else is: the harness never grants a permission, never installs a prerequisite and never repairs the
# cluster, it only checks and prints the command when it finds a gap.
#
# What it does, in order (every step is idempotent, so re-running is safe):
#
#   1. Preflight. Login answers, the cluster is OpenShift, SREGYM_ALLOW_UNVERIFIED_PLATFORM is unset,
#      no other harness process is running.
#   2. Qualify the unqualified image references in the SREGym-applications submodule
#      (scripts/qualify_submodule_images.py --apply; undo with --revert). Without it every Hotel
#      Reservation scenario is refused by the image preflight.
#   3. Grant the anyuid SCC to the three SREGym Lite application namespaces, in group form so the grant
#      outlives the per-scenario namespace.
#   4. Grant the `privileged` SCC to Promtail's service account in `observe`, through a cluster-scoped
#      ClusterRoleBinding so it survives the namespace being deleted at teardown. Measured on ROSA 4.22.18:
#      without a grant every promtail pod is refused; with `hostmount-anyuid` it is admitted and cannot
#      read `/var/log/pods` (Permission denied: its namespace MCS label has no read on the host log tree),
#      so Loki holds nothing; with `privileged` it runs 3/3 Ready and Loki returns labels.
#   5. Grant the `hostaccess` SCC to Astronomy Shop's own collector service account, which the chart runs
#      as a DaemonSet with `hostPath: /` at `/hostfs` (the hostMetrics preset) and six hostPorts. Keep the
#      upstream shape and let the cluster admit it; no manifest is changed for this.
#   6. Check the run's own requirements and report, without changing them: the storage class exists,
#      two or more untainted schedulable workers, Anthropic credentials in this shell, and no leftovers
#      from an earlier interrupted run.
#   7. Print the SREGym Lite command to run next, and the Loki ingestion check.
#
# Usage:
#   scripts/rosa_prerun.sh                          # apply everything, then report
#   DRY_RUN=1 scripts/rosa_prerun.sh                # print what would change, change nothing
#   PROMTAIL_SCC=none scripts/rosa_prerun.sh        # no grant: Loki gets no log lines, exit 2
#   (hostmount-anyuid is refused: the overlay asks for a privileged container, which that SCC
#    does not admit, so it can never work)
#   STORAGE_CLASS=gp3 scripts/rosa_prerun.sh        # the cluster's own storage class
#
# Exit codes: 0 the cluster is ready for a SREGym Lite run; 1 a step failed; 2 a prerequisite is
# missing and only the operator can supply it. The report names which.
set -u

STORAGE_CLASS="${STORAGE_CLASS:-gp3}"
PROMTAIL_SCC="${PROMTAIL_SCC:-privileged}"
DRY_RUN="${DRY_RUN:-0}"
APP_NAMESPACES="social-network hotel-reservation astronomy-shop"

problems=0
failures=0

step() { printf '\n== %s ==\n' "$1"; }
ok() { printf '  ok: %s\n' "$1"; }
missing() { printf '  MISSING: %s\n' "$1"; problems=$((problems + 1)); }
failed() { printf '  FAILED: %s\n' "$1"; failures=$((failures + 1)); }
note() { printf '  note: %s\n' "$1"; }
plan() { printf '  + %s\n' "$1"; }

run() {
  # Print the command, then run it unless this is a dry run. Nothing here reaches a shell word-split.
  plan "$*"
  [ "$DRY_RUN" = "1" ] && return 0
  "$@"
}

command -v oc >/dev/null || { echo "oc not found"; exit 1; }

# ---------------------------------------------------------------------------------------- 1. preflight

step "Preflight"
if whoami_output="$(oc whoami 2>&1)"; then
  ok "logged in as $whoami_output"
else
  failed "oc cannot reach the cluster: $whoami_output"
  exit 1
fi

if describe_output="$(oc get infrastructure cluster -o jsonpath='{.status.platformStatus.type}' 2>&1)"; then
  ok "OpenShift cluster, platform ${describe_output:-unknown}"
else
  failed "this is not an OpenShift cluster, so a SREGym Lite OpenShift run cannot be prepared here: $describe_output"
  exit 1
fi

if [ -n "${SREGYM_ALLOW_UNVERIFIED_PLATFORM:-}" ]; then
  missing "SREGYM_ALLOW_UNVERIFIED_PLATFORM is set in this shell; unset it before a managed cluster run, or a misdirected --platform openshift only warns"
else
  ok "SREGYM_ALLOW_UNVERIFIED_PLATFORM is unset"
fi

if pgrep -f "main.py --suite|main.py --problem" >/dev/null 2>&1; then
  missing "a harness process is already running; two of them collide on one cluster"
else
  ok "no other harness process"
fi

# ----------------------------------------------------------------------------- 2. qualify SREGym Lite images

step "Qualify image references in the SREGym-applications submodule"
if [ "$DRY_RUN" = "1" ]; then
  plan "uv run python scripts/qualify_submodule_images.py --apply"
elif command -v uv >/dev/null; then
  if uv run python scripts/qualify_submodule_images.py --apply; then
    ok "every image reference names its registry"
  else
    failed "could not qualify the submodule image references; the Hotel Reservation scenarios will be refused"
  fi
else
  failed "uv not found, so the submodule image references cannot be qualified"
fi

# --------------------------------------------------------------- 3. SREGym Lite application admission grants

step "Grant anyuid to the SREGym Lite application namespaces"
echo "  group form, so the grant outlives the per-scenario namespace"
for ns in $APP_NAMESPACES; do
  run oc adm policy add-scc-to-group anyuid "system:serviceaccounts:$ns" || failed "anyuid grant for $ns"
done

# ----------------------------------------------------------------------------------- 4. Promtail's grant

step "Grant an SCC to Promtail's service account in observe (PROMTAIL_SCC=$PROMTAIL_SCC)"
case "$PROMTAIL_SCC" in
  none)
    missing "PROMTAIL_SCC=none: Promtail's pods stay refused, so Loki holds no log line and the Loki-backed tools return nothing"
    ;;
  privileged)
    echo "  privileged is the only grant that works: the container runs as spc_t, which can read the node's"
    echo "  /var/log/pods. Measured on ROSA 4.22.18: with no grant every pod is refused; with"
    echo "  hostmount-anyuid the pods are admitted and the read is denied; with privileged Promtail is 3/3"
    echo "  Ready and Loki returns labels."
    echo "  The binding is cluster-scoped and names the service account identity, so it needs nothing to"
    echo "  exist yet and survives the namespace being deleted at teardown: apply it once per cluster."
    if [ "$DRY_RUN" = "1" ]; then
      plan "oc apply -f - <<< ClusterRoleBinding sregym-promtail-privileged -> system:openshift:scc:privileged for observe/promtail"
      plan "oc delete clusterrolebinding sregym-promtail-hostmount-anyuid --ignore-not-found"
    else
      if cat <<EOF | oc apply -f - >/dev/null
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: sregym-promtail-privileged
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:openshift:scc:privileged
subjects:
  - kind: ServiceAccount
    name: promtail
    namespace: observe
EOF
      then
        ok "ClusterRoleBinding sregym-promtail-privileged names observe/promtail"
      else
        failed "could not apply the privileged ClusterRoleBinding for observe/promtail"
      fi
      # A grant from an earlier PROMTAIL_SCC choice would otherwise pile up on the same service
      # account. Only this script's own name is touched, never an operator's binding.
      plan "oc delete clusterrolebinding sregym-promtail-hostmount-anyuid --ignore-not-found"
      oc delete clusterrolebinding sregym-promtail-hostmount-anyuid --ignore-not-found >/dev/null 2>&1 \
        || failed "could not prune sregym-promtail-hostmount-anyuid"
      echo "  the binding is not labelled as harness infrastructure on purpose: it is the operator's standing grant,"
      echo "  so a teardown does not remove it. It is also a durable escalation path — any pod that can use"
      echo "  the observe/promtail service account may be privileged cluster-wide. Undo with:"
      echo "    oc delete clusterrolebinding sregym-promtail-privileged"
    fi
    ;;
  hostmount-anyuid)
    failed "PROMTAIL_SCC=hostmount-anyuid cannot work: the OpenShift overlay asks for a privileged container, which that SCC does not admit, so every pod is refused at admission. Use the default 'privileged', or 'none' to record that log ingestion is unsupported"
    ;;
  *)
    failed "PROMTAIL_SCC must be hostmount-anyuid, privileged or none, not '$PROMTAIL_SCC'"
    ;;
esac

# ------------------------------------- 4b. Astronomy Shop's own collector (the chart's DaemonSet)

step "Grant hostaccess to Astronomy Shop's collector service account"
echo "  The vendored chart runs the app's collector as a DaemonSet with hostPath / -> /hostfs and hostPorts"
echo "  6831, 14250, 14268, 4317, 4318, 9411; every component's OTLP export passes through it, so Loki and"
echo "  Jaeger see nothing without it. Its pod pins no uid, no fsGroup and no seccomp, so hostaccess is the"
echo "  narrowest SCC that admits it: hostPath and host ports yes, privileged container no, ids from the"
echo "  namespace's own ranges. This keeps the public benchmark manifests unchanged."
if [ "$DRY_RUN" = "1" ]; then
  plan "oc apply -f - <<< ClusterRoleBinding sregym-astronomy-collector-hostaccess -> system:openshift:scc:hostaccess for astronomy-shop/otel-collector"
else
  if cat <<EOF | oc apply -f - >/dev/null
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: sregym-astronomy-collector-hostaccess
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:openshift:scc:hostaccess
subjects:
  - kind: ServiceAccount
    name: otel-collector
    namespace: astronomy-shop
EOF
  then
    ok "ClusterRoleBinding sregym-astronomy-collector-hostaccess names astronomy-shop/otel-collector"
    echo "  it is cluster-scoped, so the per-scenario namespace being deleted does not remove it."
    echo "  Undo with: oc delete clusterrolebinding sregym-astronomy-collector-hostaccess"
  else
    failed "could not apply the hostaccess ClusterRoleBinding for astronomy-shop/otel-collector"
  fi
fi

# ------------------------------------------------------- 5. what the SREGym Lite run needs from the cluster

step "Storage class"
if oc get storageclass "$STORAGE_CLASS" >/dev/null 2>&1; then
  ok "found, pass --storage-class $STORAGE_CLASS"
else
  missing "no storage class named $STORAGE_CLASS; name the cluster's own class with STORAGE_CLASS=..."
fi

step "Worker capacity"
if command -v jq >/dev/null; then
  workers="$(oc get nodes -o json 2>/dev/null | jq -r '
    [.items[]
     | select(.metadata.labels["node-role.kubernetes.io/worker"] != null)
     | select([(.spec.taints // [])[] | select(.effect == "NoSchedule")] | length == 0)
     | select([.status.conditions[] | select(.type == "Ready") | .status] == ["True"])] | length')"
  if [ "${workers:-0}" -ge 2 ] 2>/dev/null; then
    ok "$workers untainted, schedulable, Ready workers; internal_traffic_policy_local_astronomy_shop and duplicate_pvc_mounts_social_network need two"
  else
    missing "${workers:-0} untainted schedulable Ready worker(s); two scenarios need two or more"
  fi
else
  ok "jq is not installed, so the worker count was not checked"
fi

step "Credentials for the agent and the judge"
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  ok "ANTHROPIC_API_KEY is set in this shell"
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  ok "CLAUDE_CODE_OAUTH_TOKEN is set in this shell"
else
  missing "neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set; the claudecode agent and the api judge both need credentials"
fi

step "Leftovers from an earlier run"
leftovers="$(oc get ns -o name 2>/dev/null | grep -E 'namespace/(observe|sregym|hotel-reservation|social-network|astronomy-shop|platform-ops)$' || true)"
if [ -n "$leftovers" ]; then
  note "these namespaces exist before the run: $(echo "$leftovers" | tr '\n' ' ')"
  echo "  the run reuses what it can and its own --teardown-observers removes the rest; delete them by hand"
  echo "  only together with the PVs they hold, or the volumes are stranded:"
  echo "    helm -n observe uninstall loki prometheus promtail; oc -n observe delete pvc --all"
  echo "    oc delete ns observe sregym; oc get pv | grep -v openshift-monitoring"
else
  ok "no application or observer namespace is left over"
fi

if infra="$(oc get clusterrole,clusterrolebinding -l app.kubernetes.io/instance=observability-stack -o name 2>/dev/null)" && [ -n "$infra" ]; then
  note "cluster-scoped harness infrastructure exists before the run: $(echo "$infra" | tr '\n' ' ')"
  echo "  the run reuses or replaces these by label; delete them by hand only if no run is using them"
else
  ok "no cluster-scoped harness infrastructure is left over"
fi

# Legacy: an earlier harness version labelled its objects sregym.io/owned. The harness no longer reads that
# label, so these are reported and never collected.
if legacy="$(oc get clusterrole,clusterrolebinding -l sregym.io/owned=true -o name 2>/dev/null)" && [ -n "$legacy" ]; then
  note "legacy: cluster-scoped objects labelled sregym.io/owned from an earlier harness version exist: $(echo "$legacy" | tr '\n' ' ')"
  echo "  the harness no longer collects them; oc delete clusterrole,clusterrolebinding -l sregym.io/owned=true removes them by hand"
else
  ok "no legacy sregym.io/owned object is left over"
fi

# Fixed names the scenarios create. Read-only: the harness records what it creates in its own ledger and
# refuses to adopt or replace an object the ledger did not record, so a leftover here stops that scenario.
fixed=""
if oc get ns platform-ops >/dev/null 2>&1; then fixed="$fixed namespace/platform-ops"; fi
for webhook in gatekeeper-mutating-webhook-configuration cert-manager-webhook istio-sidecar-injector \
  kyverno-resource-mutating-webhook-cfg linkerd-proxy-injector-webhook-config; do
  if oc get mutatingwebhookconfiguration "$webhook" >/dev/null 2>&1; then fixed="$fixed mutatingwebhookconfiguration/$webhook"; fi
done
for kind in clusterrole clusterrolebinding; do
  if oc get "$kind" configmap-cleanup-controller >/dev/null 2>&1; then fixed="$fixed $kind/configmap-cleanup-controller"; fi
done
if [ -n "$fixed" ]; then
  note "these exist before the run:$fixed"
  echo "  the harness will refuse rather than adopt an object its ledger did not record; remove it by hand if it is a leftover."
else
  ok "none of the fixed names the scenarios create is already on the cluster"
fi

# ------------------------------------------------------------------------------------------------ 6. next

step "SREGym Lite: what to run next"
cat <<EOF
  One process per scenario, so a failed deploy cannot abort the rest. 16 of the 17 Lite scenarios run;
  service_dns_resolution_failure_social_network is skipped by name because it writes kube-system/coredns.

    cd $(cd "$(dirname "$0")/.." && pwd)
    unset SREGYM_ALLOW_UNVERIFIED_PLATFORM
    mkdir -p logs
    SREGYM_CACHE_DIR=logs/rosa-sregym-lite-cache-\$(date -u +%H%M) uv run main.py --suite sregym-lite \\
      --agent claudecode --model claude-haiku-5-5 --judge-model anthropic/claude-opus-5-5 \\
      --platform openshift --storage-class $STORAGE_CLASS --internet-access open \\
      --teardown-observers --n-attempts 1 \\
      2>&1 | tee logs/rosa-sregym-lite-\$(date -u +%H%M).log

  While the first scenario runs, before its teardown, prove that Loki holds log lines:

    scripts/check_loki_ingest.sh

  A fresh SREGYM_CACHE_DIR per attempt: a cache directory whose saved cluster baseline names namespace
  that no longer exist disables the damage verdict for the whole run.
EOF

step "Result"
if [ "$failures" -gt 0 ]; then
  echo "  $failures step(s) failed and $problems prerequisite(s) are missing: fix the failures first."
  exit 1
fi
if [ "$problems" -gt 0 ]; then
  echo "  $problems prerequisite(s) still missing; the ones above named MISSING. A SREGym Lite run started"
  echo "  now would lose the corresponding scenario, log ingestion, or the damage verdict."
  exit 2
fi
echo "  ready: this cluster can run SREGym Lite (platform openshift, storage class $STORAGE_CLASS)."
exit 0
