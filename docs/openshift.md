# OpenShift

SREGym runs on a local [KIND](https://kind.sigs.k8s.io/) cluster by default. This page states what runs
on OpenShift, what the harness refuses, and what is still unverified. It is the human statement of the
same table the harness refuses from: `PLATFORM_SUPPORT` in `sregym/conductor/problem_sets.py`.

Every measured result on this page comes from one managed ROSA cluster (OpenShift 4.22.18, Kubernetes
1.35.8) reached with a cluster-admin login. [Where the measured facts come from](#where-the-measured-facts-come-from)
states what that does and does not show.

## Platform value

The platform is declared, not guessed, because it changes what the harness is allowed to do.

| Flag | Environment | Meaning |
|---|---|---|
| `--platform kind` | `SREGYM_PLATFORM=kind` | A local kind cluster. The default, and unchanged. |
| `--platform openshift` | `SREGYM_PLATFORM=openshift` | A managed OpenShift cluster. |

The cluster has to confirm the declaration. Declaring `kind` for a cluster that reports the OpenShift
APIs is refused: the kind code path writes cluster-wide objects and is destructive on a managed
cluster. Declaring `openshift` for a cluster that does not report them is refused too, because a result
labelled `openshift` has to come from an OpenShift cluster.

Every default stays as it is, so a `kind` result is unchanged and remains the baseline for comparison.

### One temporary override, for local debugging only

`SREGYM_ALLOW_UNVERIFIED_PLATFORM=1` lifts **only** the second refusal: it lets a run declared
`openshift` proceed on a cluster that does not report the OpenShift APIs, which is how the OpenShift
code paths were exercised on kind. It is a debugging aid with an expiry date — grep for
`TEMPORARY_ESCAPE_HATCH` in `sregym/cluster_platform.py` — and a run started that way is **not** an
OpenShift run: its result must never be reported as one. The override is removed before this branch is
proposed upstream.

Two things to know:

- It cannot weaken the dangerous direction. Declaring `kind` for a real OpenShift cluster is refused
  unconditionally, and the check runs before any cluster write; the override is never consulted on
  that path.
- Unset it before touching a managed cluster. Left exported, it turns a misdirected
  `--platform openshift` into a warning instead of a refusal, so OpenShift code paths would run
  against whatever cluster your kubeconfig points at.

## The storage class

`--storage-class` must name the cluster's own class on OpenShift. The default names OpenEBS, and
selecting OpenEBS applies an operator manifest from the internet: two CRDs, cluster-scoped RBAC, two
StorageClasses and a privileged node-disk-manager DaemonSet. On a restricted cluster the privileged
pods are refused, so the run would die *after* those cluster-scoped objects existed, and nothing in
this harness removes them.

So a run on OpenShift with the default class is refused before it writes anything. Naming
`openebs-hostpath` yourself is allowed and only warns: it is then your call, not a forgotten flag.

## Comparability of results

Filtered internet access needs Calico's `adminnetworkpolicy` tier, which OVN-Kubernetes does not
provide, so an agent run on OpenShift has to pass `--internet-access open`. Without that flag the run
stops before any container work. See [network access](network-access.md) for the filtered mode.

`open` is a weaker isolation guarantee than the boundary a `kind` run builds. An OpenShift result is
therefore **not comparable with a `kind` result on isolation grounds**, and has to be reported as an
OpenShift result with the mode it used.

## Reconcile

Reconcile deletes every object absent from the recorded cluster baseline. On OpenShift its default mode
is **`off`**: the harness does not delete an operator's objects on a shared cluster. `--reconcile
{on,report,off}` selects a mode, and `--confirm-destructive-reconcile` is needed for `on`.

With reconcile off, cleanup is narrower. Each scenario recovers its fault and deletes its own namespace.
`--teardown-observers` removes the observer Helm releases, their retained volumes, the `observe` and `sregym`
namespaces, and the cluster-scoped objects the harness created, which it finds by an ownership label
(`sregym.io/owned`, or the Helm instance label of `prometheus`, `loki` or `promtail`), never by name. An object
without the label is not touched. The damage verdict then compares the cluster with the baseline taken before
the run and reports `clean`, `owned` (only objects this harness removed) or `damaged`; it reports and does not
clean. Drift that carries no label is not reset between scenarios, and the verdict finds it only for the
object kinds it compares. Teardown runs once at the end of a suite run, so observers stay up across its
scenarios.

## Admission prerequisite

Each scenario deploys an application into its own namespace. Those applications pin container users that
`restricted-v2` rejects — Astronomy Shop pins 1001, 101, 1000, 33 and 999, and Hotel Reservation and
Social Network run `mongo` and `memcached` as root with no `securityContext`. The applications create
and delete their namespaces per scenario, so a grant has to outlive the namespace.

The grant is the operator's, never the harness's: a harness that grants itself `anyuid` or `privileged`
is not acceptable. The harness checks the grant read-only and prints the command to run:

```bash
oc adm policy add-scc-to-group anyuid system:serviceaccounts:<namespace>
```

SCC objects and the `system:openshift:scc:*` bindings are cluster-scoped, so an identity that cannot
read them gets "cannot verify" and the same command instead of a refusal. The harness never runs it.

## Per-scenario support

`works` runs without further work, `fixable` is blocked by something named in the reason, and `exclude`
cannot be expressed on this platform without changing the fault. A suite run skips its `exclude`
scenarios with the reason printed and runs the rest; `--problem` runs a scenario regardless, so an
excluded one fails at inject on this platform rather than being refused upfront.

| Scenario | Verdict | Reason |
|---|---|---|
| `cronjob_sidecar_blocks_completion_hotel_reservation` | **works** | The injected sidecar and every other change is namespaced. Needs: anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |
| `edge_request_filter_cpu_saturation` | **works** | `kubectl exec` only; the chart's `runAsUser: 101` is admitted under `anyuid`. Needs: anyuid grant for the app namespace. |
| `network_policy_block` | **works** | A namespaced NetworkPolicy only. Needs: anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |
| `env_variable_shadowing_astronomy_shop` | **works** | Namespaced env edits; the chart's `runAsUser: 1001` is admitted under `anyuid`. Needs: anyuid grant for the app namespace. |
| `mutating_webhook_resource_limits_social_network` | **works** | Five cluster-scoped `MutatingWebhookConfiguration` objects and namespace `platform-ops`, all removed at teardown. Needs: cluster-admin; anyuid grant for the app namespace. |
| `finalizer_deadlock_controller_hotel_reservation` | **works** | Writes a `ClusterRole` and a `ClusterRoleBinding`; recovery keeps the controller the oracle needs and the teardown collects the owned RBAC. Needs: cluster-admin; anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |
| `kafka_poison_pill_hol_block` | **works** | `kubectl exec` into the in-namespace broker. Needs: anyuid grant for the app namespace. |
| `internal_traffic_policy_local_astronomy_shop` | **works** | Pins a pod away from its caller; the node picker skips tainted and cordoned nodes (ROSA's infra workers). Needs: two or more untainted schedulable workers; anyuid grant for the app namespace. |
| `service_dns_resolution_failure_social_network` | **exclude** | Writes `kube-system/coredns` and restarts `deployment coredns`; OpenShift serves DNS from `openshift-dns/dns-default`. |
| `service_wrong_pod_selection_hotel_reservation` | **works** | Label and Service selector edits inside the app namespace only. Needs: anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |
| `namespace_memory_limit` | **works** | A namespaced `ResourceQuota`; it refuses when a memory quota exists, and a fresh project is clean. Needs: anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |
| `valkey_auth_disruption` | **works** | `kubectl exec ... CONFIG SET requirepass` inside the in-namespace Valkey pod. Needs: anyuid grant for the app namespace. |
| `secret_rotation_stale_env_credentials_astronomy_shop` | **works** | Namespaced Secret, ConfigMap and env edits only. Needs: anyuid grant for the app namespace. |
| `unschedulable_incorrect_port_assignment` | **works** | A deliberate `nodeSelector` in the app namespace. Needs: anyuid grant for the app namespace. |
| `duplicate_pvc_mounts_social_network` | **works** | RWO PVC plus anti-affinity, with the injector naming no `storageClassName`. Needs: two or more schedulable workers; anyuid grant for the app namespace. |
| `rolling_update_misconfigured_social_network` | **works** | Passed end to end on ROSA 4.22.18 with a CLEAN damage verdict. The chart's nginx resolver names `kube-dns.kube-system`, which OpenShift lacks; `values/social-network-openshift.yaml` points it at `dns-default.openshift-dns`. |
| `search_rate_retry_collapse_hotel_reservation` | **works** | Env edits only; the load arrives through `kubectl port-forward`. Needs: anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |

The table covers `sregym-lite`: 16 `works`, 1 `exclude`, and no `fixable` row. The 17 `sregym-lite-ia`
ports are not measured. 16 of them are `fixable` and only the DNS fault is `exclude`; `fixable` there
means the verdict comes from a code read of the fault they were adapted from, pending a cluster run. Where
a reason above names a chart pin of the Lite application, the port names the shared application-admission
prerequisite instead: no read of an Incident Arena application under `restricted-v2` exists yet.

`--suite sregym-lite` and `--suite sregym-lite-ia` print this summary and refuse a set that contains an
excluded scenario, naming it, before any cluster work. Neither set can avoid the refusal by naming the
other.

## Not supported on OpenShift

Two features are not supported today. The harness refuses `--noise` itself. SSH and Ansible injection has no
guard yet, so the row states what happens if you run one of those scenarios anyway. Each row says why the
feature does not work now, with the source that shows it, and what allowing it for benchmark runs would mean.
Both columns come from reading the code. None of it was run on a cluster.

| Feature | Not supported today because | Allowing it would mean |
|---|---|---|
| `--noise` (Chaos Mesh) | **Refused on OpenShift**, before any cluster write (`main.py` `noise_refusal`, `sregym/cluster_platform.py` `supports_noise`). Without the refusal the run would degrade silently: `NoiseManager.start()` installs Chaos Mesh with no platform check, the chart's privileged `chaos-daemon` is not admitted, the install error is logged and swallowed (`sregym/generators/noise/manager.py:85-91`, `:396-408`), `_chaos_mesh_ready` stays false, and the run reports a result with no noise in it. The install is `helm upgrade --install --version 2.8.0` from `charts.chaos-mesh.org` (the chart is pinned; the repository index is not). Runtime detection tests for `crio` in the node listing (`manager.py:376`), but CRI-O reports `cri-o://`, so it falls back to the Docker socket; `ChaosInjector` raises on any runtime that is not Docker or containerd (`sregym/generators/noise/impl/stress_injector.py:26-38`). The chaos-daemon being privileged is the chart default and was not read from a vendored copy. | Real support is a separate, measured piece of work: CRI-O detection that reads `cri-o://` (and the test fixture that currently pins the wrong string), an SCC preflight for `chaos-mesh:chaos-daemon` that prints the operator's grant and never runs it, cleanup gated on what this run installed, and one measured ROSA run. Even on a pre-installed Chaos Mesh the harness patches the operator's `chaos-daemon` tolerations (`manager.py:284-306`) and strips finalizers from every chaos resource in every namespace on each stop (`manager.py:228-270`); `ChaosInjector` deletes the Chaos Mesh CRDs when it finds them without a Helm release (`stress_injector.py:60-106`). A successful install leaves CRDs, `ClusterRole`s and webhook configurations behind, and the damage verdict records them as harmless additions, so the run reads `clean`. The faults (pod kill, network delay, network loss) select pods in the scenario namespace only (`sregym/generators/noise/catalog.py`). `--noise` is not part of `sregym-lite`, so no Lite result depends on it. |
| SSH and Ansible fault injection (`kubelet_crash`, `kubelet_eviction_threshold_misconfig`, its `slack_spine` port, `node_clock_drift_hotel_reservation`) | Injection reaches node operating systems over SSH with `scripts/ansible/inventory.yml` (`sregym/generators/fault/inject_remote_os.py:51-105`, `:202-212`, `:282-295`). A managed OpenShift cluster gives the operator no SSH or root on its nodes. No platform check exists on the injection side, and `--problem` bypasses the suite skip (`main.py:1102-1103`). Today on OpenShift: `kubelet_crash` returns early without an inventory (`inject_remote_os.py:203-204`) and the scenario still rolls the application; the eviction fault pins the application to a node, then raises because `inject_disk_pressure` does not check for the inventory (`:289-295`); `node_clock_drift_hotel_reservation` is rejected at its privileged pod and leaves its TLS sidecar patch behind. With an `inventory.yml` present, the first two would act on the listed hosts without checking the platform. The privileged `hostPID` pod with `nsenter` is the clock-drift injector (`sregym/conductor/problems/node_clock_drift.py:346-410`) and restore (`inject_remote_os.py:416-475`). None of the four is in `PLATFORM_SUPPORT` or in a suite. | A refusal at injection for these scenarios on `--platform openshift`, and an inventory check at the top of `inject_disk_pressure`. To run them for benchmarks: a node-level mechanism that is not SSH (a privileged pod or `oc debug node`), written and measured on ROSA; a `privileged` SCC that the operator grants, because the harness never grants one; and acceptance that these faults stop kubelets, change kubelet configuration or step the clock on nodes the cluster owner shares. `kubelet_crash` has a further limit: the agent cannot repair a stopped kubelet through the Kubernetes API, so mitigation may stay unsolvable even when injection works. The `recover_clock_drift` leftover check does run on OpenShift (`sregym/conductor/conductor.py:1872-1876`); it does nothing when no leftover probe pod exists. The kubelet and disk-pressure recoveries are skipped (`conductor.py:1798-1806`). |

Three scenarios reach Chaos Mesh without `--noise`: `missing_stale_cache_fallback_astronomy_shop` calls the
same installer (`sregym/conductor/problems/missing_stale_cache_fallback_astronomy_shop.py:700-710`), and
`capacity_decrease_rpc_retry_storm` and `load_spike_rpc_retry_storm` build a `ChaosInjector` when
`CPU_containment` is set (`sregym/generators/workload/blueprint_hotel_work.py:292`). None is in a Lite suite or in
`PLATFORM_SUPPORT`. Both install paths raise on OpenShift before any API call, so `--problem` on one of them
is refused for the same reason as the flag.

## Loki and Promtail

On OpenShift the harness deploys Loki and Promtail from the same charts as on kind, with overlays
(`sregym/observer/loki/values-openshift.yaml`, `promtail-values-openshift.yaml`):

- The control-plane node pins and tolerations are dropped.
- The Loki gateway is switched off. Its chart hardcodes `kube-dns.kube-system` as the resolver, which OpenShift
  lacks. Every reader (the MCP Loki tools, the telemetry module) already uses the `loki` Service on port 3100.
- Promtail pushes to `http://loki.observe.svc.cluster.local:3100/loki/api/v1/push`. Promtail posts to exactly
  the URL it is given and appends nothing, so the path has to be in it; without it Promtail and Loki both
  report healthy while no log line arrives. A test pins the overlay URL to the kind URL's path, and
  `helm template` of the Promtail release renders it (code-read; the render was not applied to a cluster).
- The Loki chart (7.5.0 at the time of writing) renders no Promtail of its own, so the `promtail:` block in the
  Loki overlay is inert; the only Promtail is the separate release.

Whether log lines arrive is checked by an operator against a live run: `scripts/check_loki_ingest.sh` asks
Loki for its labels and fails if there are none. No log line has been read from Loki on the ROSA cluster, so
log ingestion on OpenShift is not a measured fact until that script has passed against a live run.

## Unverified

Two claims are not proven. This page does not claim either one.

- **H8, Promtail's `privileged`.** The rendered Promtail chart does not set `privileged`. It runs as uid 0 with
  `hostPath` mounts of `/var/log/pods`, `/var/lib/docker/containers` and `/run/promtail`. The SELinux reason for
  a privileged container exists only in a values-file comment. One agent run with the `full` profile deployed Loki
  and Promtail on the ROSA cluster with a cluster-admin login and went through, and the operator's readiness notes
  record Promtail admitted without a binding of ours. Neither shows what a restricted login needs, and neither
  shows that logs were read. The harness checks a `privileged` grant for the `promtail` service account read-only
  and only warns; `scripts/rosa_prerun.sh` applies it only with `PROMTAIL_SCC=privileged`. The default agent
  run, which deploys Loki, is outside the supported scope until it is measured on a restricted login. That is a
  scope statement, not an omission.
- **H17, the chart node-exporter's `hostPort` 9100** against the port OpenShift's own node-exporter
  holds. The overlay disables the chart's DaemonSet, so the collision never arises on OpenShift. The chart's
  DaemonSet uses `hostNetwork`, `hostPID` and a host-root mount, which `anyuid` does not admit, so enabling it
  would need a further operator grant. No Lite scenario or oracle reads node-exporter series; the agent's
  generic `get_metrics` tool sees no `node_*` series on OpenShift. The collision itself was not measured.

## Running it

Once per cluster, as a cluster-admin, run the operator prerequisites. The harness never runs them; it only
checks them and prints the command for a gap. The script qualifies the submodule image references and grants
`anyuid` to the three application namespaces. `PROMTAIL_SCC=privileged` adds the unmeasured Promtail grant,
and only once the `promtail` service account exists, that is after the first Loki deploy.

```bash
scripts/rosa_prerun.sh
```

The three commands, in the order they earn their cost. Substitute the cluster's own storage class.

```bash
# 1. One scenario, end to end, and the platform preflights. Minutes, not a suite.
#    The validator launches no agent container, so it takes no --internet-access.
uv run python tests/integration/validate_problem.py --problem network_policy_block --profile full \
  --platform openshift --storage-class <cluster default>

# 2. The same, then a teardown and a verdict that checks both directions.
uv run python tests/integration/validate_problem.py --problem network_policy_block --profile full \
  --platform openshift --storage-class <cluster default> --teardown-observers

# 3. The suite. 16 of the 17 scenarios run; the excluded one is named and skipped.
uv run python main.py --suite sregym-lite --platform openshift --storage-class <cluster default> \
  --internet-access open --teardown-observers --stages diagnosis mitigation --n-attempts 1
```

During an agent run, before teardown, confirm that Loki holds log lines. Loki being Running is not evidence of
that.

```bash
scripts/check_loki_ingest.sh
```

`--internet-access open` is required on the suite run because filtered access needs Calico's
`adminnetworkpolicy` tier, which OVN-Kubernetes does not provide; it belongs to the agent run, which
is the only place an agent container is launched.

`--allow-unqualified-images` is only needed for the 11 unqualified references that live in the
`SREGym-applications` submodule, which this branch does not touch; the refusal names them and
waiving it is a logged decision, not a default.

A suite run prints the platform summary first and then the skip, for example
`Skipping service_dns_resolution_failure_social_network in sregym-lite: it cannot be expressed on
this platform (...). The rest of the suite runs.` The reason it cannot is on this page's table and
in the support entry.

[operations.md](operations.md) covers the artifacts, the success criteria and the damage check —
including what to do when the verdict is `damaged`.

## Where the measured facts come from

One cluster only: a managed ROSA cluster, OpenShift 4.22.18, Kubernetes 1.35.8, reached with a
**cluster-admin login**. The Kubernetes version is recorded in `logs/rosa-rolling-update/before-nodes-labels.txt`.
The OpenShift version is recorded in the operator's readiness notes, which are not in this repository.
No restricted login has been measured. Rights a restricted operator needs are not known, and every
verdict on this page is for cluster-admin.

The runs used `anyuid` granted to the three application namespaces and unqualified image names qualified
by `scripts/qualify_submodule_images.py`.

The evidence is local. `logs/` and `results/` are git-ignored, so a reader of this branch cannot open
them. The operator holds them. Each `works` reason in `PLATFORM_SUPPORT` cites `logs/rosa-oracle-all`.

| Claim | Source on the ROSA cluster | What it shows |
|---|---|---|
| The 16 runnable Lite scenarios passed the oracle lifecycle (deploy, inject, oracle fails, recover, oracle passes, clean up, damage verdict). | `logs/rosa-oracle-all/<scenario>.json`, `RESULTS.txt`, `damage.json` | Each scenario has a passing final attempt (`platform: openshift`). Four needed a retry: `internal_traffic_policy_local_astronomy_shop` took six attempts, and `network_policy_block`, `finalizer_deadlock_controller_hotel_reservation` and `unschedulable_incorrect_port_assignment` took two each. Retry causes are not in the logs. Group skips after a first failure make the sequence in `RESULTS.txt` interleaved, not one clean run. The final damage verdict is `owned`: only objects the harness created were removed. |
| `rolling_update_misconfigured_social_network` passed end to end. | `logs/rosa-rolling-update/run2-validation-summary.md` and `run2-damage.json` | `validate_problem` passed with damage verdict `clean`, after the nginx resolver overlay. The first attempt (`run1.out`, `validation.json`) failed at deploy with `PodReadinessTimeout` after 825 s. |
| An agent run on that scenario completed the lifecycle. | `results/1106_1310/claudecode_ALL_results.csv`, `damage.json`, and `logs/rosa-rolling-update/cache-run3/cluster_baseline_state.json` (the platform record) | `claudecode` ran with `--internet-access open`. Diagnosis succeeded. Mitigation failed with `failure_class: agent_error` and `reason: fault_still_present`. The damage verdict is `clean`. The result shows that the harness runs the agent lifecycle on the cluster; it does not show agent success, and no log line was read from Loki in it. |

Two consequences when you read a claim here:

- A number from that cluster is a number from **one** cluster with cluster-admin. It is not a
  measurement of what a restricted operator sees. Every `works` verdict means the oracle lifecycle
  passed there. It is not a measurement of agent behaviour, of other clusters, or of a restricted operator.
- The image references in `SREGym-applications` are unqualified. They pulled on that cluster after
  `scripts/qualify_submodule_images.py` qualified them. On a cluster whose short-name table differs they
  may not pull, which is what the image preflight refuses ahead of time.
