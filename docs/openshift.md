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
| `cronjob_sidecar_blocks_completion_hotel_reservation` | fixable | The injected sidecar runs unqualified `busybox:1.36`. |
| `edge_request_filter_cpu_saturation` | fixable | The chart pins `runAsUser: 101`, which `restricted-v2` rejects. |
| `network_policy_block` | fixable | A namespaced NetworkPolicy only. Passed on ROSA with in-place branch edits. |
| `env_variable_shadowing_astronomy_shop` | fixable | The chart pins `runAsUser: 1001`, which `restricted-v2` rejects. |
| `mutating_webhook_resource_limits_social_network` | fixable | Five cluster-scoped `MutatingWebhookConfiguration` objects and namespace `platform-ops`. |
| `finalizer_deadlock_controller_hotel_reservation` | fixable | The fault writes a `ClusterRole` and a `ClusterRoleBinding`. |
| `kafka_poison_pill_hol_block` | fixable | `kubectl exec` into the in-namespace broker. The images are already qualified. |
| `internal_traffic_policy_local_astronomy_shop` | fixable | Needs two or more schedulable workers. |
| `service_dns_resolution_failure_social_network` | **exclude** | Writes `kube-system/coredns` and restarts `deployment coredns`; OpenShift serves DNS from `openshift-dns/dns-default`. |
| `service_wrong_pod_selection_hotel_reservation` | fixable | Label and Service selector edits inside the app namespace only. |
| `namespace_memory_limit` | fixable | A namespaced `ResourceQuota`. A fresh project is clean. |
| `valkey_auth_disruption` | fixable | `kubectl exec … CONFIG SET requirepass` inside the in-namespace Valkey pod. |
| `secret_rotation_stale_env_credentials_astronomy_shop` | fixable | Namespaced Secret, ConfigMap and env edits only. |
| `unschedulable_incorrect_port_assignment` | fixable | A deliberate `nodeSelector` in the app namespace. It works the same. |
| `duplicate_pvc_mounts_social_network` | fixable | The injector's PVC names no `storageClassName`, and RWO plus anti-affinity needs two or more workers. |
| `rolling_update_misconfigured_social_network` | fixable | The injector writes `python:3.9-slim` and `busybox:1.36`, both unqualified. |
| `search_rate_retry_collapse_hotel_reservation` | fixable | Env edits only. The load arrives through `kubectl port-forward`. |

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

## Where the measured facts come from

Everything measured so far is in `OPENSHIFT-READINESS.md`: **one** ROSA cluster, OpenShift 4.22.18,
Kubernetes 1.35.8, reached with a **cluster-admin login**, and with unqualified image names edited in
place. One scenario, `network_policy_block`, passed there. A restricted login has not been measured, so
do not claim that SREGym needs no extra rights until it is.
