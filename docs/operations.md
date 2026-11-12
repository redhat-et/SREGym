# Running and reading a run

This page is what you need to run SREGym on a cluster you do not own, and to answer the only
question that matters afterwards: **did this run harm the cluster?** For the platform itself, see
[openshift.md](openshift.md).

## 1. Before the run

### On OpenShift, first

One command makes a cluster ready for a SREGym Lite run, and everything a run needs from the operator is in
it. Run it once per cluster as a cluster-admin, before any of the commands below:

```bash
scripts/rosa_prerun.sh            # apply, then report whether the cluster is ready
DRY_RUN=1 scripts/rosa_prerun.sh  # print every change, make none
```

It qualifies the submodule image references, grants `anyuid` to the three application namespaces and to `observe` and `sregym`, creates a
cluster-scoped `ClusterRoleBinding` that gives the `observe/promtail` service account the `privileged` SCC,
and then checks what the run needs and does not change it: the storage class, at least two untainted
schedulable workers, the Anthropic credentials, and leftovers from an earlier run.

That Promtail binding is required, not optional: measured on ROSA, without it every promtail pod is refused,
and with `hostmount-anyuid` instead the pods are admitted and cannot read `/var/log/pods`. **It is also a
durable escalation path** — any pod that can use the `observe/promtail` service account may be privileged
cluster-wide, not only in `observe` — so it carries no harness label, a teardown does not remove it,
and the runbook keeps its undo line:

```bash
oc delete clusterrolebinding sregym-promtail-privileged
```

Exit `0` means ready, `1` a step failed, `2` something only the operator can supply is missing; the output
names which, and every step is listed in [openshift.md](openshift.md#running-it). The harness itself grants
nothing: it checks and prints.

On kind, which is the default:

```bash
uv run python tests/integration/validate_problem.py --problem network_policy_block --profile full
```

On a managed OpenShift cluster, the same check needs the platform named and the cluster's own
storage class. `--storage-class` is not optional there: the default names OpenEBS, and selecting
OpenEBS installs a provisioner, cluster-scoped RBAC and a privileged node-disk-manager DaemonSet
that nothing in this harness removes, so a run on OpenShift with the default class is refused
before it writes anything:

```bash
uv run python tests/integration/validate_problem.py --problem network_policy_block --profile full \
  --platform openshift --storage-class <the cluster default>
```

Add `--teardown-observers` to remove the observer stack at the end and have the verdict check, in
both directions, that the cluster is as it was found. The image preflight refuses the unqualified
references that live in the `SREGym-applications` submodule; `--allow-unqualified-images` overrides
that refusal and logs every reference it waived. See [openshift.md](openshift.md) for the flag
table and for the one scenario a managed cluster cannot express.

The validator is the cheap end-to-end check: it deploys the observers and the application, injects
the fault, runs the oracle, and cleans up. If this fails, the campaign will fail too, and it costs
minutes instead of a suite.

Four preflights refuse a run before it touches the cluster:

| Preflight | Refuses when | Where |
|---|---|---|
| platform | the cluster does not confirm the declared `--platform` | `sregym/cluster_platform.py` |
| admission | a namespace is provably missing the SCC its manifests need | `sregym/service/admission_preflight.py` |
| capacity | the profile's documented footprint does not fit the untainted workers or the quota | `sregym/service/capacity_preflight.py` |
| images | an image reference names no registry host, so the cluster's short-name table decides | `sregym/service/image_preflight.py` |

Each one prints what it checked and, where a permission is missing, the exact `oc` command that
grants it. **SREGym never grants a permission itself**: a benchmark that grants the privilege it
measures cannot answer the question it was run for.

## 2. Where everything lands

```text
logs/sregym_<MMDD>_<HHMM>.log                  the whole run, DEBUG level, one file per process
results/<MMDD>_<HHMM>/                          one directory per campaign
├── <agent>/<problem_id>/<problem_id>_<agent>_results.csv    one row per attempt
├── <agent>/<problem_id>/phases_attempt<N>.jsonl             the phase ledger, one line per phase
├── <agent>/<problem_id>/run_<attempt>/                      ATIF trajectory and the run's artifacts
└── damage.json                                  the cluster verdict, when one was given
$SREGYM_CACHE_DIR/cluster_baseline_state.json   the baseline snapshot (default: ~/cache_dir)
```

`--agent-logs-dir` / `AGENT_LOGS_DIR` move the log directory; `SREGYM_CACHE_DIR` moves the cache,
and therefore the baseline snapshot.

**The CSV row is the result.** Its columns are flat: one stage per prefix, so `Diagnosis.success`
and `Mitigation.success` sit beside `problem_id`, `attempt` and `run_status`. A row with
`deploy_failed: true` means the application never came up and no stage ran — read the log at that
timestamp, not the row.

**Each row names the code that produced it.** The provenance columns are `run.git_sha`, `run.git_dirty`
(tracked files only), `run.submodule_sha`, `run.submodule_dirty`, `run.submodule_diff_sha` and `run.platform`
(`sregym/run_artifacts.py`, `run_provenance()`; merged into the rows in `main.py`). A result from a dirty
tree is not a result of the commit named in `run.git_sha`.

**The phase ledger is the timeline.** `phases_attempt<N>.jsonl` records what happened when, which is
what turns "the run took 412 s" into "the run spent 212 s here". It also records the `internet_access`
mode, because a result from an `open` run is not comparable with one from a `filtered` run and the
CSV alone cannot tell them apart.

## 3. Reading a run

```bash
uv run python -m sregym.viewer results          # http://127.0.0.1:8765
```

The viewer browses trajectories and the phase ledger without changing any source file.
`uv run python -m sregym.viewer results/<stamp>/<agent>/<problem_id>` opens one problem.

## 4. The four success criteria

A run is **verified** only when all four hold. This is the definition, so a partial run is not
reported as a pass.

1. **The validator passed** with the profile the campaign uses — including `--with-loki` if the
   campaign deploys Loki, since that is a different manifest set, a different SCC requirement and
   10 GiB more storage (`_LOKI_GIB` in `sregym/service/capacity_preflight.py`).
2. **The agent run finished**: `run_status` is `complete`, not `incomplete` and not `deploy_failed`.
   `incomplete` names its reason in `incomplete_reason`, and that reason is the finding.
3. **The damage verdict is clean**: no pre-existing object was removed and no measured object
   changed (see below).
4. **Nothing of SREGym's is left behind**: no SREGym namespace, no SREGym release, no SREGym
   PersistentVolume. With `--teardown-observers` this criterion is checked for you and a leftover
   makes the verdict `damaged`; without it, nothing checks it and you are reading the cluster by
   hand.

Criteria 3 and 4 are about the cluster, not the model. A run that produces a perfect score by
degrading the cluster is not a result.

## 5. The damage check

The cluster is snapshotted before the first write and compared at the end. Two lists are compared
by name, and a documented subset is compared by content, because a name surviving with different
content is not survival:

- **Names**: namespaces, ClusterRoles, ClusterRoleBindings, PersistentVolumes, StorageClasses,
  CRDs, validating and mutating webhook configurations, and validating admission policies and their
  bindings.
- **Content**: node labels, node taints, and the `kube-system/coredns` ConfigMap data.

That list is what the comparison reads. Anything outside it — an APIService, an SCC, a
PriorityClass, or a namespaced object outside the run's own namespaces — is not compared, and a
change to it is not reported. The list is `_NAME_LISTS` and `_CONTENT_FIELDS` in
`sregym/service/damage_report.py`; extend them there before claiming more here.

The verdict is one of three:

| Verdict | Meaning |
|---|---|
| `clean` | nothing pre-existing was removed or changed; anything added is named in the report |
| `owned` | only objects SREGym created are gone, which is what cleanup is supposed to do |
| `damaged` | a pre-existing object was removed or changed, the cluster is not the one the baseline was taken on, or — when a teardown ran — an object SREGym owns survived it |

Additions alone are not damage: a run is supposed to create objects, and without a teardown the
observer stack staying up is the requested state. That is why the survivor check runs only when
`--teardown-observers` was passed: with a teardown requested, anything still owned by the harness
(the infrastructure label, a Helm release it deployed, or a ledger entry matched by UID) is a leftover, and leftovers are what this check exists to catch.

A `damaged` verdict fails the run regardless of its score, and `damage.json` is written beside the
results before the run stops.

## 6. If the verdict is `damaged`

The harness does not clean up after itself on the way out, on purpose: a half-finished cleanup on
a cluster someone else owns is worse than a reported leftover. So:

1. Read `damage.json` in the run's results directory — the same directory as the CSV. `removed`
   and `changed` name what was harmed; `survivors` names what SREGym left behind.
2. Do not re-run. A second run takes a fresh baseline, so the damage becomes invisible and the
   second verdict reads clean.
3. A `removed` or `changed` entry is a finding about the harness: report it with the run's
   `damage.json` and log. Nothing in the harness puts a pre-existing object back.
4. A `survivors` entry is removable by hand, and only by hand: `kubectl delete <kind> <name>` for
   whatever is listed. A surviving PersistentVolume is usually a `Retain` reclaim policy; its
   claim is already gone, so `kubectl delete pv <name>` is safe once the log says the claim was
   removed.

## 7. Cost

One measured campaign on a managed cluster with three workers: about 34 pods, and about 26 GiB of
block storage — 8 GiB Prometheus, 2 GiB Alertmanager, 10 GiB Loki and 1 GiB per application volume.
That number, not the pod count, is what a sweep runs out of first, which is why the capacity
preflight checks storage.

Observers are **not** torn down by default: an iterate-fast loop reuses them. On OpenShift with reconcile
off, the observers are reset after every scenario instead, so each scenario pays one observer deploy
(`sregym/conductor/conductor.py`, `_reset_observers`). The reset starts the `observe` namespace deletion
without waiting for it; the next scenario's deploy waits for `observe` to finish terminating, and a failed
reset is logged as a warning rather than stopping the campaign. Pass
`--teardown-observers` to remove the stack at the end of a campaign — the Helm releases, the
volumes Helm retains on purpose (the Prometheus one is 8 GiB), and the cluster-scoped objects this
harness created, which are found by the infrastructure label or the Helm release, and by ledger UID, and never by name. Reuse is safe only while
the chart version and the values are unchanged; a changed fingerprint forces a redeploy, so a
stale observer stack cannot silently serve a different configuration.

## 8. What has not been measured

Be careful with numbers from this repository. The OpenShift claims behind `--platform openshift`
come from **one** managed cluster, reached with a cluster-admin login, with one scenario. A
restricted login, a second cluster and a full sweep have not been measured, so the
platform table's `fixable` verdicts are readings of the code, not observations. When you run one,
say which cluster and which login produced the number.
