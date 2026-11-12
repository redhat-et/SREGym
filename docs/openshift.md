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
| `--platform kind` | `SREGYM_PLATFORM=kind` | A local kind cluster. The default. |
| `--platform openshift` | `SREGYM_PLATFORM=openshift` | A managed OpenShift cluster. |

The cluster has to confirm the declaration. Declaring `kind` for a cluster that reports the OpenShift
APIs is refused: the kind code path writes cluster-wide objects and is destructive on a managed
cluster. Declaring `openshift` for a cluster that does not report them is refused too, because a result
labelled `openshift` has to come from an OpenShift cluster.

A `kind` run behaves as it does on `main`. Only these differ:

- The platform-discovery refusal above (`sregym/cluster_platform.py`).
- The ownership ledger's refusal guards: a scenario refuses to overwrite or delete a same-named cluster object that the run did not create (`sregym/service/ownership_ledger.py`, `require_owned`).
- Result rows carry provenance columns: `run.git_sha`, `run.git_dirty`, `run.submodule_sha`, `run.submodule_dirty`, `run.submodule_diff_sha` and `run.platform` (`sregym/run_artifacts.py`, `run_provenance()`). They are data only; the run does the same work.
- The `docker.io/` image-name prefixes are in the oracle, injector and problem files (for example `sregym/conductor/oracles/` and `sregym/generators/fault/inject_virtual.py`). The Helm commands are argv lists rather than shell strings (`sregym/service/helm.py`). Every caller in the repository passes one flag or value per list element, so Helm receives the same arguments as on `main`; the one JSON value that contains spaces (`sregym/conductor/problems/stale_hostaliases_dns_poisoning_astronomy_shop.py`) was shell-quoted on `main` and is a single element now.
- The `duplicate_pvc_mounts` root-cause text is platform-gated: kind keeps `main`'s wording (`sregym/conductor/problems/duplicate_pvc_mounts.py`).
- Kind takes the values-fingerprint release path, not `main`'s, unless the storage class is OpenEBS. A kind run with an explicit non-OpenEBS `--storage-class` therefore differs from `main` in how it reuses Loki, Prometheus and Promtail (`sregym/service/telemetry/release_pvcs.py`, `use_main_release_handling`).
- Inert additions, none of which changes what a kind run deploys or scores: the MCP objects carry the neutral label `app.kubernetes.io/instance: observability-stack` (`sregym/service/mcp_server.py`, `label_infrastructure`; `sregym/service/owned_objects.py`), extra read-only calls and log lines, an `internet_access` field in the phase ledger (`main.py`, `bind_phase_ledger`), a `persistent_volume_namespaces` field in the baseline snapshot (`sregym/service/cluster_state.py`), and Hotel Reservation PV cleanup matching the claim's namespace (`sregym/service/apps/hotel_reservation.py`).

### What changes under `--platform openshift`

Each item is a branch that runs only when the declared platform is `openshift`. On kind the same code
path is `main`'s.

- A persisted baseline that names a namespace the cluster no longer has is not used for reconcile.
  The load checks for stale namespaces only on OpenShift; kind loads the snapshot without that check
  (`sregym/service/cluster_state.py` `load_baseline_state`, `check_staleness`; `sregym/conductor/conductor.py`
  `deploy_app`).
- The cluster's default StorageClass is never changed. Kind makes `openebs-hostpath` the default
  (`sregym/conductor/conductor.py` `_make_openebs_hostpath_default`).
- Failed pods are deleted only in the problem's namespace. Kind deletes Failed pods in every namespace
  except the kubelet-eviction one (`sregym/conductor/conductor.py` `fix_kubernetes`).
- Observer readiness waits use `WAIT_FOR_POD_READY_TIMEOUT`. Kind keeps 120 s
  (`sregym/observer/ingress_nginx.py`, `sregym/observer/otel_collector/otel_collector.py`, `_wait_for_ready`).
- The network probe pod runs with restricted security contexts (`sregym/service/kubectl.py`, `assert_pod_networking`).
- Loki, Prometheus and Promtail are reused when their values fingerprint matches, and their retained
  PVCs are deleted on redeploy. Kind keeps `main`'s release handling only with the OpenEBS storage class; any
  other class takes the fingerprint path (`sregym/service/telemetry/release_pvcs.py`, `use_main_release_handling`; `loki.py`; `prometheus.py`).
- The `internal_traffic_policy_local` node picker skips tainted and cordoned nodes
  (`sregym/conductor/problems/internal_traffic_policy_local.py`, `_accepts_pods`).
- The observer stack is reset after each scenario when reconcile is off and Loki is deployed
  (`sregym/conductor/conductor.py`, `_reset_observers`; see [Reconcile](#reconcile)).
- The agent's API proxy also hides every `openshift*` namespace the cluster lists (`sregym/conductor/conductor.py`,
  `_hide_platform_namespaces`). Those names are added to the set the proxy already hides on kind, `chaos-mesh` and `khaos`
  (`HIDDEN_NAMESPACES` in `sregym/service/agent_visibility_policy.py`).

### No override

The platform declaration is always confirmed by the cluster; there is no override, so OpenShift code
paths cannot be exercised on kind. `verify_platform_matches_cluster` in `sregym/cluster_platform.py`
reads no environment variable that skips the check.

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

Other differences a reader of OpenShift results must carry:

- 16 of 17 Lite scenarios run (DNS excluded), so suite totals are not 1:1 with published kind numbers.
- One cluster, cluster-admin login.
- On OpenShift, with reconcile off and Loki deployed by the run, the observer stack is torn down after each scenario and the `observe` namespace is deleted, so each scenario redeploys Prometheus, Loki and Promtail. That costs one observer deploy per scenario (`sregym/conductor/conductor.py`, `_reset_observers` and `_cleanup_sync`). The reset starts the `observe` namespace deletion without waiting for it; the next scenario's deploy waits for `observe` to finish terminating before it installs Prometheus (`sregym/service/telemetry/prometheus.py`, `_wait_for_namespace_termination`). A failed reset is logged as a warning and does not stop the campaign. Under `--use-external-harness` Loki is not deployed, and nothing is reset (`main.py`, `deploy_loki=not args.use_external_harness`).
- No `node_*` series (node-exporter disabled, see Unverified).
- On OpenShift the agent's API proxy also hides the `openshift*` namespaces, on top of `chaos-mesh` and `khaos`, which kind hides too (`sregym/conductor/conductor.py` `_hide_platform_namespaces`). Kind does not call that method.
- Each result row records the code and platform that produced it: `run.git_sha`, `run.git_dirty` (tracked files only), `run.submodule_sha`, `run.submodule_dirty`, `run.submodule_diff_sha` and `run.platform`. They come from `run_provenance()` in `sregym/run_artifacts.py` and are merged into the rows in `main.py`. A row with `run.git_dirty` or `run.submodule_dirty` set was not produced from a clean checkout.

## Reconcile

Reconcile deletes every object absent from the recorded cluster baseline. On OpenShift its default mode
is **`off`**: the harness does not delete an operator's objects on a shared cluster. `--reconcile
{on,report,off}` selects a mode, and `--confirm-destructive-reconcile` is needed for `on`.

With reconcile off, cleanup is narrower. Each scenario recovers its fault and deletes its own namespace. On OpenShift, cleanup also deletes the cluster-scoped objects its run recorded in the ownership ledger, matched by UID (`delete_ledger_objects` in `sregym/service/owned_objects.py`, called from `_cleanup_sync`). Ledger cleanup and ledger namespace deletes send the recorded UID as a delete precondition, so an object that was replaced under the same name is never removed.
`--teardown-observers` removes the observer Helm releases, their retained volumes, the `observe` and `sregym`
namespaces, and the cluster-scoped objects the harness created, which it finds by an ownership label
(the infrastructure label `app.kubernetes.io/instance=observability-stack`, or the Helm release of `prometheus`, `loki` or `promtail` in `observe`, identified by its `app.kubernetes.io/instance` label and `meta.helm.sh/release-name` and `meta.helm.sh/release-namespace` annotations), never by name. An object
matching none of these is not touched. Scenario-created cluster objects are recorded in the run's ownership ledger, `$SREGYM_CACHE_DIR/owned_objects.json` (override with `SREGYM_OWNERSHIP_LEDGER`), by kind, name and UID, and are matched by UID (`sregym/service/ownership_ledger.py`). A fresh cache directory starts with an empty ledger, so a scenario refuses a leftover from an earlier attempt and the operator removes it by hand. The damage verdict then compares the cluster with the baseline taken before
the run and reports `clean`, `owned` (only objects this harness removed) or `damaged`; it reports and does not
clean. Drift that carries no label is not reset between scenarios, and the verdict finds it only for the
object kinds it compares. Teardown runs once at the end of a suite run. With reconcile off on OpenShift, the observers are also
reset after each scenario (see [Comparability of results](#comparability-of-results)).

The verdict compares the object kinds it knows, node labels and taints, and the CoreDNS configuration.
OpenShift has no `kube-system/coredns` ConfigMap, so that last comparison is empty on both sides and a change
to the cluster's DNS configuration is invisible to the verdict. Each run on OpenShift logs
`CoreDNS ConfigMap not found` when it takes the baseline (`sregym/service/cluster_state.py`
`_get_coredns_configmap_data` returns an empty mapping on a 404).

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
scenarios with the reason printed and runs the rest; `--problem` of an excluded scenario is refused
before any cluster work (`main.py`, `platform_problem_refusal`).

| Scenario | Verdict | Reason |
|---|---|---|
| `cronjob_sidecar_blocks_completion_hotel_reservation` | **works** | The injected sidecar and every other change is namespaced. Needs: anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |
| `edge_request_filter_cpu_saturation` | **works** | `kubectl exec` only; the chart's `runAsUser: 101` is admitted under `anyuid`. Needs: anyuid grant for the app namespace. |
| `network_policy_block` | **works** | A namespaced NetworkPolicy only. Needs: anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |
| `env_variable_shadowing_astronomy_shop` | **works** | Namespaced env edits; the chart's `runAsUser: 1001` is admitted under `anyuid`. Needs: anyuid grant for the app namespace. |
| `mutating_webhook_resource_limits_social_network` | **works** | Five cluster-scoped `MutatingWebhookConfiguration` objects (the real one and four decoys) and namespace `platform-ops`. Recovery deletes all five and the namespace by name, each only when the ownership ledger records it and its UID still matches (`sregym/conductor/problems/mutating_webhook_resource_limits.py`, `recover_fault`). Nothing on the cluster marks them; the ledger is the only record. Needs: cluster-admin; anyuid grant for the app namespace. |
| `finalizer_deadlock_controller_hotel_reservation` | **works** | Writes a `ClusterRole` and a `ClusterRoleBinding`; recovery keeps the controller the oracle needs and the teardown collects the owned RBAC. Needs: cluster-admin; anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |
| `kafka_poison_pill_hol_block` | **works** | `kubectl exec` into the in-namespace broker. Needs: anyuid grant for the app namespace. |
| `internal_traffic_policy_local_astronomy_shop` | **works** | Pins a pod away from its caller; the node picker skips tainted and cordoned nodes (ROSA's infra workers). Needs: two or more untainted schedulable workers; anyuid grant for the app namespace. |
| `service_dns_resolution_failure_social_network` | **exclude** | Writes `kube-system/coredns` and restarts `deployment coredns`; OpenShift serves DNS from `openshift-dns/dns-default`. |
| `service_wrong_pod_selection_hotel_reservation` | **works** | Label and Service selector edits inside the app namespace only. Needs: anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |
| `namespace_memory_limit` | **works** | A namespaced `ResourceQuota`; it refuses when a memory quota exists, and a fresh project is clean. Needs: anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |
| `valkey_auth_disruption` | **works** | `kubectl exec ... CONFIG SET requirepass` inside the in-namespace Valkey pod. Needs: anyuid grant for the app namespace. |
| `secret_rotation_stale_env_credentials_astronomy_shop` | **works** | Namespaced Secret, ConfigMap and env edits only. Needs: anyuid grant for the app namespace. |
| `unschedulable_incorrect_port_assignment` | **works** | A deliberate `nodeSelector` in the app namespace. Needs: anyuid grant for the app namespace. |
| `duplicate_pvc_mounts_social_network` | **works** | RWO PVC plus anti-affinity, with the injector naming the run's storage class (`sregym/generators/fault/inject_virtual.py`, `get_storage_class()`). With node- or zone-bound storage such as `gp3`, another replica stays Pending or is stuck in ContainerCreating with a volume attach or Multi-Attach error, so the deployment is only partially Ready; on OpenShift the scenario's root-cause text names both symptoms, and on kind it keeps main's wording (`sregym/conductor/problems/duplicate_pvc_mounts.py`). Both symptoms are reasoned from the volume type and were not observed in a run. Needs: two or more schedulable workers; anyuid grant for the app namespace. |
| `rolling_update_misconfigured_social_network` | **works** | Passed end to end on ROSA 4.22.18 with a CLEAN damage verdict. The chart's nginx resolver names `kube-dns.kube-system`, which OpenShift lacks; `values/social-network-openshift.yaml` points it at `dns-default.openshift-dns`. |
| `search_rate_retry_collapse_hotel_reservation` | **works** | Env edits only; the load arrives through `kubectl port-forward`. Needs: anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py). |

The table covers `sregym-lite`: 16 `works`, 1 `exclude`, and no `fixable` row. The 17 `sregym-lite-ia`
ports are not measured. 16 of them are `fixable` and only the DNS fault is `exclude`; `fixable` there
means the verdict comes from a code read of the fault they were adapted from, pending a cluster run. Where
a reason above names a chart pin of the Lite application, the port names the shared application-admission
prerequisite instead: no read of an Incident Arena application under `restricted-v2` exists yet.

`--suite sregym-lite` and `--suite sregym-lite-ia` print this summary, skip each excluded scenario by name, and run the rest. A set is refused only when every one of its scenarios would be skipped.

## Not supported on OpenShift

Two features are not supported today. The harness refuses `--noise` itself. SSH and Ansible injection has no
guard yet, so the row states what happens if you run one of those scenarios anyway. Each row says why the
feature does not work now, with the source that shows it, and what allowing it for benchmark runs would mean.
Both columns come from reading the code. None of it was run on a cluster.

| Feature | Not supported today because | Allowing it would mean |
|---|---|---|
| `--noise` (Chaos Mesh) | **Refused on OpenShift**, before any cluster write (`main.py` `noise_refusal`, `sregym/cluster_platform.py` `supports_noise`). Without the refusal the run would degrade silently: `NoiseManager.start()` would install Chaos Mesh if the guard were removed. `require_noise_support()` is its first call, in `_ensure_chaos_mesh_installed`, outside the `try` that logs and swallows install errors (`sregym/generators/noise/manager.py`). Behind that guard the chart's privileged `chaos-daemon` is not admitted, the install error is logged and swallowed (`_ensure_chaos_mesh_installed`), `_chaos_mesh_ready` stays false, and the run reports a result with no noise in it. The install is `helm upgrade --install --version 2.8.0` from `charts.chaos-mesh.org` (the chart is pinned; the repository index is not). Runtime detection tests for `crio` in the node listing (`manager.py:379`), but CRI-O reports `cri-o://`, so it falls back to the Docker socket; `ChaosInjector` raises on any runtime that is not Docker or containerd (`sregym/generators/noise/impl/stress_injector.py:26-38`). The chaos-daemon being privileged is the chart default and was not read from a vendored copy. | Real support is a separate, measured piece of work: CRI-O detection that reads `cri-o://` (and the test fixture that currently pins the wrong string), an SCC preflight for `chaos-mesh:chaos-daemon` that prints the operator's grant and never runs it, cleanup gated on what this run installed, and one measured ROSA run. Even on a pre-installed Chaos Mesh the harness patches the operator's `chaos-daemon` tolerations (`manager.py:284-306`) and strips finalizers from every chaos resource in every namespace on each stop (`manager.py:228-270`); `ChaosInjector` deletes the Chaos Mesh CRDs when it finds them without a Helm release (`stress_injector.py:60-106`). A successful install leaves CRDs, `ClusterRole`s and webhook configurations behind, and the damage verdict records them as harmless additions, so the run reads `clean`. The faults (pod kill, network delay, network loss) select pods in the scenario namespace only (`sregym/generators/noise/catalog.py`). `--noise` is not part of `sregym-lite`, so no Lite result depends on it. |
| SSH and Ansible fault injection (`kubelet_crash`, `kubelet_eviction_threshold_misconfig`, its `slack_spine` port, `node_clock_drift_hotel_reservation`) | Injection reaches node operating systems over SSH with `scripts/ansible/inventory.yml` (`sregym/generators/fault/inject_remote_os.py:51-105`, `:202-212`, `:282-295`). A managed OpenShift cluster gives the operator no SSH or root on its nodes. No platform check exists on the injection side, and `--problem` bypasses the suite skip (`main.py`, `_run_benchmark`, the `problem_selection` assignment). Today on OpenShift: `kubelet_crash` returns early without an inventory (`inject_remote_os.py:203-204`) and the scenario still rolls the application; the eviction fault pins the application to a node, then raises because `inject_disk_pressure` does not check for the inventory (`:289-295`); `node_clock_drift_hotel_reservation` is rejected at its privileged pod and leaves its TLS sidecar patch behind. With an `inventory.yml` present, the first two would act on the listed hosts without checking the platform. The privileged `hostPID` pod with `nsenter` is the clock-drift injector (`sregym/conductor/problems/node_clock_drift.py:346-410`) and restore (`inject_remote_os.py:416-475`). None of the four is in `PLATFORM_SUPPORT` or in a suite. | A refusal at injection for these scenarios on `--platform openshift`, and an inventory check at the top of `inject_disk_pressure`. To run them for benchmarks: a node-level mechanism that is not SSH (a privileged pod or `oc debug node`), written and measured on ROSA; a `privileged` SCC that the operator grants, because the harness never grants one; and acceptance that these faults stop kubelets, change kubelet configuration or step the clock on nodes the cluster owner shares. `kubelet_crash` has a further limit: the agent cannot repair a stopped kubelet through the Kubernetes API, so mitigation may stay unsolvable even when injection works. The `recover_clock_drift` leftover check does run on OpenShift (`sregym/conductor/conductor.py`, `fix_kubernetes`, the `recover_clock_drift` call at line 1985, after the platform branch on both platforms); it does nothing when no leftover probe pod exists (`sregym/generators/fault/inject_remote_os.py`, `recover_clock_drift`). The kubelet and disk-pressure recoveries are in `fix_kubernetes`' non-OpenShift branch (`recover_kubelet_crash` at line 1926, `recover_disk_pressure_all` at line 1929), which OpenShift skips. |

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
- The Loki gateway is switched off. Its resolver defaults to `kube-dns.kube-system`, which OpenShift
  lacks. The chart's `global.dnsService` and `global.dnsNamespace` values could point it at `dns-default.openshift-dns`; that route is not taken yet. Every reader (the MCP Loki tools, the telemetry module) already uses the `loki` Service on port 3100.
- Promtail pushes to `http://loki.observe.svc.cluster.local:3100/loki/api/v1/push`. Promtail posts to exactly
  the URL it is given and appends nothing, so the path has to be in it; without it Promtail and Loki both
  report healthy while no log line arrives. A test pins the overlay URL to the kind URL's path, and
  `helm template` of the Promtail release renders it. In early test runs on the cluster, Loki returned labels.
- The Loki chart, which is not version-pinned (7.6.0 rendered on 2026-11-12), renders no Promtail of its own, so the `promtail:` block in the
  Loki overlay is inert; the only Promtail is the separate release.

Whether log lines arrive is checked by an operator against a live run with `scripts/check_loki_ingest.sh`.
It reports the Promtail DaemonSet's counts and then asks Loki's own API for its labels through
`oc port-forward` from the operator's workstation. It exits 0 when Loki returns labels, 1 when Loki answers
and has none, and 2 when the query could not be made. The earlier version asked `wget` inside the Loki pod,
which ships neither `wget` nor `curl`, so it could only ever report a failure of the tool.

### What the cluster does with these releases

Everything in this subsection is one ROSA cluster on 2026-11-11 and 2026-11-12, cluster-admin login, during early tests. A fact not backed by a command or test shown here is labelled as an operator observation.

**Loki runs.** Its API answers on port 3100 of `loki-0`.

**Promtail needs a grant, and only one grant works.** Three states, as the operator observed them; the `privileged` row was also confirmed with `scripts/check_loki_ingest.sh`:

| Promtail's grant | What happened |
|---|---|
| none | the DaemonSet reads `desired 3, current 0, ready 0`: every pod is refused, with `hostPath volumes are not allowed` from `anyuid` and `restricted-v2` and `provider "privileged": Forbidden: not usable by user or serviceaccount` (operator observation) |
| `hostmount-anyuid` | measured with the overlay's positions mount backed by an emptyDir and **without** a privileged container, the pods are admitted and `ls /var/log/pods` from inside answers `Permission denied`, because the container carries the `observe` namespace's MCS label (`s0:c34,c19`) and has no read on the host log tree; Loki received nothing. With the overlay as it ships today that SCC is not an option at all, because that SCC refuses a privileged container at admission (operator observation; no saved artifact) |
| `privileged` | 3/3 Running and Ready under `scc=privileged`, `privileged=true`, and Loki answers with labels: `{"status":"success","data":["app","component","container","filename","instance","job","namespace","node_name","pod","service_name","stream", …]}` |

So log ingestion on OpenShift works with the `privileged` SCC and does not work without it. A privileged
container runs as `spc_t`, which is how OpenShift's own node log collectors read these files; no narrower
SCC can be given that read. The grant is the operator's, never the harness's, and the harness only reports
its absence:

```bash
oc adm policy add-scc-to-user privileged -z promtail -n observe
```

The pre-step writes that grant as a **cluster-scoped `ClusterRoleBinding` naming the service account
identity**, so it survives `--teardown-observers` deleting `observe` and needs applying once per cluster.
`oc adm policy add-scc-to-user privileged -z promtail -n observe` also works but writes a RoleBinding
*inside* `observe`, which the teardown then removes; both shapes admit the pods (operator observation, no saved artifact: after the
namespace-scoped binding was removed, a pod created with only the cluster-scoped one came back
`scc=privileged`, ready). The harness's read-only check asks in `observe` because a namespaced binding is
invisible to a cluster-scoped question (`sregym/service/admission_preflight.py`,
`check_promtail_privileged`).

Two limits a reader should carry. Promtail tails every namespace on the node, so Loki holds platform logs
(`openshift-ovn-kubernetes`, `openshift-multus`) as well as the scenario's, exactly as it does on `kind` with
the same values. And a fresh Promtail replays existing files from the start, which Loki answers `400` for
entries older than its `reject_old_samples_max_age`; those batches are logged and dropped, and the newer
lines arrive.

**The same admission gap takes a second observer component.** `kube-state-metrics` from the Prometheus chart
had no pod: its ReplicaSet was in `ReplicaFailure`/`FailedCreate`. Two constraints reject it. `anyuid`, the
grant `observe` already has and what the rest of the stack runs under, carries `seccompProfiles: null` and
answers `metadata.annotations[seccomp.security.alpha.kubernetes.io/pod]: Forbidden: seccomp may not be set`,
because this is the only workload of the five that sets `seccompProfile: RuntimeDefault`. `restricted-v2`
sets the UID from the namespace's range (`runAsUser: MustRunAsRange`) and the fsGroup from its group range
(`fsGroup: MustRunAs`), and the chart's 65534 is in neither.

`values-openshift.yaml` now unsets `runAsUser`, `runAsGroup` and `fsGroup` for that subchart, which leaves
them for `restricted-v2` to supply and keeps the seccomp profile; `tests/test_prometheus_overlay_render.py`
pins the rendered result. **Measured on the same ROSA cluster afterwards:** the pod runs 1/1 under
`restricted-v2` with `fsGroup: 1001160000` supplied by the constraint, `seccompProfile: RuntimeDefault`
kept, and its Service has an endpoint at `10.128.4.15:8080` for Prometheus to scrape (operator observation; no saved artifact). Whether the agent's
`get_metrics` returns `kube_*` series in a scored run is not yet measured.

**The app's own collector keeps its shipped shape; the cluster adapts.** Astronomy Shop's chart runs its
collector as a DaemonSet that mounts `hostPath: /` at `/hostfs` (the `hostMetrics` preset) and declares
hostPorts 6831, 14250, 14268, 4317, 4318 and 9411. That is the public benchmark's topology and it stays:
no values file is changed for it. No SCC on the cluster admitted it, so the DaemonSet read zero pods, the
`otel-collector` Service it backs had no endpoints, and **every component's OTLP export was refused** —
Astronomy Shop had no traces and no span metrics, while `kind` has them, and the broken workload produced one
wrong agent diagnosis. The operator grant is `hostaccess`, the narrowest SCC that admits this pod: it allows a
host directory and host ports and no privileged container, and it assigns uids from the namespace's own
ranges, which the pod accepts because it pins no uid, fsGroup or seccomp. An early test run made before this grant showed why it matters: its seven Astronomy Shop scenarios ran without the app's collector, and in one of them the agent chased the refused DaemonSet instead of the injected fault.

```bash
oc create clusterrolebinding sregym-astronomy-collector-hostaccess \
  --clusterrole=system:openshift:scc:hostaccess --serviceaccount=astronomy-shop:otel-collector
```

`rosa_prerun.sh` applies it, cluster-scoped so the per-scenario namespace being deleted does not remove it.
`tests/test_astronomy_shop_overlay_render.py` pins both halves: the overlay must not touch the collector, and
the shipped shape must still carry the host mount and the six host ports that justify the grant.

**Measured on ROSA 4.22.18, 2026-11-12**: with the grant in place the DaemonSet reads `3 3 3 3 3`, every pod
Running with `openshift.io/scc: hostaccess` (a server-side dry run of the pod had already shown the uid and
fsGroup taken from the namespace range, `1001070000`), the collector logs `Everything is ready`, the
`otel-collector` Service carries three pod addresses (operator observation), and a validator run of
`env_variable_shadowing_astronomy_shop` passed in 323 s with a `CLEAN` damage verdict. Two receiver-level errors remain and are not
admission problems: the `hostmetrics` filesystem scraper reports missing paths under `/hostfs`, and the
`kubeletstats` receiver cannot reach `/stats/summary`. Whether `kind` shows the same two is not measured.

A release whose pods are refused used to be fatal: the next run called `helm install` on a release that still
existed and Helm refused it with `cannot reuse a name that is still in use`, which failed the deploy of every
scenario. The harness now removes a Promtail release that does not run before it installs again
(`sregym/service/telemetry/loki.py`, `_deploy_promtail`), the same way it already handled Loki.

## Unverified

The chart node-exporter's port conflict is now measured. OpenShift's own node-exporter runs with `hostNetwork` and port 9100 on every node
(`oc get ds -n openshift-monitoring node-exporter`), so the chart's DaemonSet would collide. The overlay
disables that DaemonSet, so the collision does not arise on OpenShift. No Lite scenario or oracle reads
node-exporter series.

### Known differences from kind, not yet changed

- The vendored Prometheus chart holds tracked subchart directories (kube-state-metrics 5.15.2, node-exporter 4.24.0) next to the `Chart.lock` versions that `Helm.ensure_dependencies` fetches (`sregym/service/helm.py`), so Helm renders either copy and kube-state-metrics runs as v2.9.2 or v2.10.1. This holds on kind and OpenShift alike, as on `main`. No Lite oracle reads `kube_*` series.
These are stated, not fixed. Each one is a difference a reader of OpenShift results has to carry.

- kube-state-metrics and Grafana have their user IDs unset, so `restricted-v2` assigns them, instead of
  a `nonroot-v2` grant (`sregym/observer/prometheus/values-openshift.yaml`, the `kube-state-metrics`
  `securityContext`; `sregym/service/apps/values/astronomy-shop-openshift.yaml`, the `grafana` block).
- No `node_*` series: the node-exporter DaemonSet is disabled, because OpenShift's own node-exporter holds
  hostNetwork port 9100 on every node (`sregym/observer/prometheus/values-openshift.yaml`).
- The Loki gateway is off, Promtail pushes to the `loki` Service, and Promtail's positions file is an
  emptyDir (`sregym/observer/loki/values-openshift.yaml`, `sregym/observer/loki/promtail-values-openshift.yaml`).
  See [Loki and Promtail](#loki-and-promtail) for the gateway's DNS resolver.

Not proven:

- whether `kube_*` series reach the agent in a scored run;
- whether kind shows the two collector receiver errors;
- what a restricted (non-cluster-admin) operator needs;
- whether granting `nonroot-v2` instead of unsetting uids admits kube-state-metrics and Grafana when their
  service accounts also hold `anyuid`;
- whether gp3 volume-attach latency changes app startup races;
- run-to-run variance (one attempt per scenario).

## Running it

**The pre-step: one command, once per cluster, before any run.** This is step 0 of every command further
down, and the only place that changes a cluster on the operator's behalf. Run it as a cluster-admin. The
harness never runs it; the harness only checks and prints the command for a gap.

```bash
scripts/rosa_prerun.sh            # apply everything, then report whether the cluster is ready
DRY_RUN=1 scripts/rosa_prerun.sh  # print every change, make none
```

What it does, in order, and each step is idempotent:

| Step | What it changes, or checks |
|---|---|
| Preflight | `oc whoami` answers, the cluster is OpenShift, no other harness process runs |
| Images | Qualifies the unqualified references in `SREGym-applications`; without it every Hotel Reservation scenario is refused by the image preflight |
| Admission | Grants `anyuid` to `system:serviceaccounts:<namespace>` for `social-network`, `hotel-reservation`, `astronomy-shop`, `observe` and `sregym` (`ANYUID_NAMESPACES`), in group form so the grant outlives the per-scenario namespace. Also grants `observe` and `sregym`, which the harness preflight checks. |
| Promtail | Applies a cluster-scoped `ClusterRoleBinding` named `sregym-promtail-privileged` that gives the `observe/promtail` service account the `privileged` SCC, the only grant under which Promtail can read the node's pod logs (see [Loki and Promtail](#loki-and-promtail)). It needs no namespace to exist, survives a teardown, and carries no harness label, because it is the operator's standing grant rather than the harness's object. On that path it also deletes the legacy `ClusterRoleBinding` `sregym-promtail-hostmount-anyuid`, the script's only delete, so a grant from an earlier choice does not pile up. `PROMTAIL_SCC=none` skips the grant and records it as missing, so the script exits 2; `hostmount-anyuid` is refused, because the overlay asks for a privileged container and that SCC does not admit one, so it can never work. The undo line is printed |
| Collector | Applies a cluster-scoped `ClusterRoleBinding` named `sregym-astronomy-collector-hostaccess`, with `roleRef` `system:openshift:scc:hostaccess` and the subject `ServiceAccount` `otel-collector` in `astronomy-shop`. It is cluster-scoped, so the per-scenario namespace being deleted does not remove it. It carries no harness label. The undo line is printed |
| Storage | Checks that a storage class named `STORAGE_CLASS` (default `gp3`) exists; if not, it is reported as missing |
| Capacity | Counts untainted, schedulable, Ready workers. Below two it reports `MISSING`, which `internal_traffic_policy_local_astronomy_shop` and `duplicate_pvc_mounts_social_network` need; the script then exits 2 (unless another step failed). Without `jq` the count is not made and the step reports `ok` |
| Credentials | Checks that `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` is set in the shell that will run `main.py` |
| Leftovers | Reports namespaces and cluster-scoped objects that exist before the run: those labelled `app.kubernetes.io/instance=observability-stack`, legacy objects labelled `sregym.io/owned` (with the command to remove them), and the fixed names the scenarios create (`platform-ops`, the five webhook configurations, `configmap-cleanup-controller`). It removes nothing itself; the harness refuses to adopt an object its ledger did not record, so a leftover is removed by hand |
| Next | Prints the `--suite sregym-lite` command with the flags an OpenShift run requires, and the Loki ingestion check |

It exits 0 when the cluster is ready, 1 when a step failed, and 2 when something only the operator can supply
is missing. Which one it is, is printed. The script grants nothing on the harness's behalf: it is the
operator's, and the harness still only reports a gap. Its one delete is the legacy `sregym-promtail-hostmount-anyuid` ClusterRoleBinding, removed when the
privileged grant is applied; every other leftover is only named, with the command that removes it. `scripts/rosa_oracle_all.sh` calls it as well, and runs its sweep even
when the answer is 2, because the oracle needs only the images and the `anyuid` grants.

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

`--allow-unqualified-images` is only needed for the 15 unqualified `image:` references (15 manifest files, 5 distinct images) in the committed
`SREGym-applications` submodule; `scripts/qualify_submodule_images.py` rewrites them in your working tree and does not commit them; the refusal names them and
waiving it is a logged decision, not a default.

A suite run prints the platform summary first and then the skip, for example
`Skipping service_dns_resolution_failure_social_network in sregym-lite: it cannot be expressed on
this platform (...). The rest of the suite runs.` The reason it cannot is on this page's table and
in the support entry.

[operations.md](operations.md) covers the artifacts, the success criteria and the damage check —
including what to do when the verdict is `damaged`.

## Where the measured facts come from

One cluster only: a managed ROSA cluster, OpenShift 4.22.18, Kubernetes 1.35.8, reached with a
**cluster-admin login**. The versions were read from the cluster with `oc get clusterversion` and
`oc get nodes` on 2026-11-12.
No restricted login has been measured. Rights a restricted operator needs are not known, and every
verdict on this page is for cluster-admin.

The early tests used `anyuid` granted to the three application namespaces and to `observe` and `sregym` (one ClusterRoleBinding, read with `oc get clusterrolebinding system:openshift:scc:anyuid -o wide` on 2026-11-12) and unqualified image names qualified
by `scripts/qualify_submodule_images.py`.

These facts come from early tests on that cluster. Their logs are kept by the operator and are not part of this repository.

| Claim | What was observed |
|---|---|
| The 16 runnable Lite scenarios passed the oracle lifecycle (deploy, inject, oracle fails, recover, oracle passes, clean up, damage verdict). | Each scenario has a passing final attempt (`platform: openshift`). Four needed a retry: `internal_traffic_policy_local_astronomy_shop` took six attempts, and `network_policy_block`, `finalizer_deadlock_controller_hotel_reservation` and `unschedulable_incorrect_port_assignment` took two each. Retry causes were not recorded. The final damage verdict is `owned`: only objects the harness created were removed. |
| The 16 runnable Lite scenarios ran once each with an agent (`claude-haiku-5-5`, judge `claude-opus-5-5`): 9 solved diagnosis and mitigation, 7 did not. | An early test: ran on commit 163b98d2 plus uncommitted changes, with the qualified submodule in the working tree, and before the Astronomy Shop collector grant, so its seven Astronomy Shop rows ran without the app's collector. That run predates the per-scenario observer reset. Damage verdict `owned`: the only removals were the `mcp-server` ClusterRole and ClusterRoleBinding, which were already in the run's baseline after an earlier aborted run had added them. It is not a result of this branch's head. |
| `rolling_update_misconfigured_social_network` passed end to end. | `validate_problem` passed with damage verdict `clean`, after the nginx resolver overlay. The first attempt failed at deploy with `PodReadinessTimeout` after 825 s. |
| An agent run on that scenario completed the lifecycle. | `claudecode` ran with `--internet-access open`. Diagnosis succeeded and mitigation failed with `failure_class: agent_error`, `reason: fault_still_present`: the agent removes the blocking init container and leaves `maxUnavailable: 100%` in place. The result shows that the harness runs the agent lifecycle for this scenario on the cluster; it does not show agent success. |
| Observer admission (measured 2026-11-11): `kube-state-metrics` fixed, Promtail needs `privileged`. | Promtail's DaemonSet reads `desired 3, current 0, ready 0`: every promtail pod is refused by every security context constraint, so Loki holds no log line. `kube-state-metrics` had no pod at all: its ReplicaSet is in `ReplicaFailure`/`FailedCreate` on `restricted-v2` group and uid rules, so the agent's metrics view has no `kube_*` series. The same state had made every scenario's deploy fail, because `helm install` on the refused Promtail release returns `cannot reuse a name that is still in use`; that is fixed in `_deploy_promtail`. |

Two consequences when you read a claim here:

- A number from that cluster is a number from **one** cluster with cluster-admin. It is not a
  measurement of what a restricted operator sees. Every `works` verdict means the oracle lifecycle
  passed there. It is not a measurement of agent behaviour, of other clusters, or of a restricted operator.
- The image references in `SREGym-applications` are unqualified. They pulled on that cluster after
  `scripts/qualify_submodule_images.py` qualified them. On a cluster whose short-name table differs they
  may not pull, which is what the image preflight refuses ahead of time.
