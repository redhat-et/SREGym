# OpenShift

SREGym runs on a local [KIND](https://kind.sigs.k8s.io/) cluster by default. This page states what runs
on OpenShift, what the harness refuses, and what is still unverified. It is the human statement of the
same table the harness refuses from: `PLATFORM_SUPPORT` in `sregym/conductor/problem_sets.py`.

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
OpenShift run: its result must never be reported as one.

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

The `sregym-lite-ia` ports carry the verdict of the fault they were adapted from, and only the DNS fault
is excluded there. Where a reason above names a chart pin of the Lite application, the port names the
shared application-admission prerequisite instead: no read of an Incident Arena application under
`restricted-v2` exists yet.

`--suite sregym-lite` and `--suite sregym-lite-ia` print this summary and refuse a set that contains an
excluded scenario, naming it, before any cluster work. Neither set can avoid the refusal by naming the
other.

## Not supported on OpenShift

| Feature | Why |
|---|---|
| `--noise` (chaos-mesh) | It installs a privileged daemon from a floating chart repository. |
| SSH and Ansible fault injection | The injection side creates a privileged pod and has no platform guard. |

## Unverified

Two claims from the earlier readiness work are still unproven, and neither is encoded as a guess:

- **H8, Promtail's `privileged`.** The SELinux reason for it exists only in a values-file comment.
  Until it is measured, the default agent run, which deploys Loki, is unsupported on OpenShift. That is
  a scope statement, not an omission.
- **H17, the chart node-exporter's `hostPort` 9100** against the port OpenShift's own node-exporter
  holds. The overlay disables the chart's DaemonSet and records the reason; proving the collision needs
  a cluster.

## Running it

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
**cluster-admin login**. All 16 runnable Lite scenarios passed the oracle-only lifecycle there (deploy, inject,
oracle fails, recover, oracle passes, clean up, damage verdict), with the unqualified image names
qualified by `scripts/qualify_submodule_images.py` and `anyuid` granted to the three app namespaces. The log
of each is under `logs/rosa-oracle-all/`. An agent run has passed for one scenario,
`rolling_update_misconfigured_social_network`. A restricted login has not been measured, so do not claim that
SREGym needs no extra rights until it is. Every `works` verdict above means the oracle lifecycle passed on that
single cluster; it is not a measurement of agent behaviour, of other clusters, or of a restricted operator.

Two consequences to keep in mind when you read a claim here:

- A number from that cluster is a number from **one** cluster with cluster-admin. It is not a
  measurement of what a restricted operator sees.
- The image references in `SREGym-applications` are unqualified. They pull on that cluster through its
  own short-name table; on a cluster whose table differs they may not, which is what the image
  preflight refuses ahead of time.
