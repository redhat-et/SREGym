"""Capacity preflight: refuse before a full run is deployed onto a cluster that cannot hold it.

`OPENSHIFT-READINESS.md` §13.4 records the arithmetic for one run on one cluster:
19 application pods plus roughly 15 observer pods on three untainted workers, with
Loki deployed. A cluster with fewer or smaller workers accepts the deploy and then
fails pod by pod, which costs one readiness timeout per pod and reads like an
application fault. This module compares that footprint with what the workers can
allocate and with any `ResourceQuota` in the namespaces the run uses, and refuses
with the dimensions that do not fit named.

The cluster's storage capacity is not compared directly. The classes a run can use
(`openebs-hostpath`, or whatever `--storage-class` names) advertise no capacity, and
what a claim is granted is decided by the provisioner against node disk, which no
cheap `kubectl` call reports. Only a `ResourceQuota` is compared -- `requests.storage`
and `persistentvolumeclaims` -- and a namespace without one stays unchecked.

Footprint source: a constant table per deployment profile, not the manifests the run
is about to apply. Reading those manifests is neither cheap nor informative: most of
them declare no resource request at all (`observer/prometheus/prometheus/values.yaml`
leaves `resources: {}`), a pod without a request is scheduled as if it were free, the
application manifests live in the `SREGym-applications` submodule and are rendered by
Helm charts at deploy time, and the fault injectors add pods later. A sum over them
would be a sum of zeroes. The table therefore takes the pod counts from the one
measurement there is -- one cluster, not a bound -- and applies one documented
allowance per pod so the comparison has numbers at all.

The storage footprint is that same table:

- 8 GiB for Prometheus: the chart's own default,
  `observer/prometheus/prometheus/values.yaml` `server.persistentVolume.size: 8Gi`.
- 2 GiB for Alertmanager: `alertmanager.persistentVolume.size: 2Gi` in the same file,
  confirmed by the measured PV list. `--profile svelte` turns Alertmanager off
  (`observer/prometheus/values-svelte.yaml`, `alertmanager.enabled: false`), so a
  svelte run claims no such volume -- the same reason its pod count is two lower.
- 10 GiB for Loki: `observer/loki/loki-values.yaml` `persistence.size: 10Gi`. Only a
  run that deploys Loki claims it; the conductor's own condition is
  `ConductorConfig.deploy_loki`.
- 1 GiB per application volume: the measured PV list has six of them, and
  `SREGym-applications/hotelReservation/knative` declares six PVCs (geo, profile,
  rate, recommendation, reservation, user). The profile selects the observer stack,
  never the application manifests, so both profiles share the number; counting PVCs
  per problem would mean parsing the submodule's charts and their values at preflight
  time, for the same reasons the pod counts are not read from them.

A node that is not counted is a node this run cannot use: the workloads it deploys
carry no toleration, so a taint keeps every one of them off it.
"""

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from sregym.profile import FULL, SVELTE, get_profile

logger = logging.getLogger("all.sregym.capacity_preflight")

# Above this share of an available capacity the run still fits, but a second one
# beside it will not, which is worth a line in the log.
WARN_RATIO = 0.8

_QUANTITY = re.compile(r"^([0-9.]+)([A-Za-z]*)$")
_MIB = 1024**2
_GIB = 1024**3
_BYTES_PER_SUFFIX = {
    "": 1,
    "Ki": 1024,
    "Mi": _MIB,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}

# Pods one run deploys, per profile. 34 is §13.4's 19 application pods plus roughly
# 15 observer pods, measured on one cluster; `svelte` drops Alertmanager and
# Pushgateway (observer/prometheus/values-svelte.yaml), two of those observer pods.
_PODS_PER_PROFILE = {FULL: 34, SVELTE: 32}

# What one pod is allowed to request. These are the requests `jaeger.yaml` and
# `observer/loki/loki-values.yaml` do declare, used as one allowance for every pod
# because most of the others declare none.
_POD_CPU_MILLICORES = 100
_POD_MEMORY_MIB = 256

# Volumes the observer stack claims, in GiB, per profile and one PVC each: the chart
# defaults of `observer/prometheus/prometheus/values.yaml`, minus Alertmanager, which
# `observer/prometheus/values-svelte.yaml` disables.
_OBSERVER_VOLUMES_GIB = {FULL: (("Prometheus", 8), ("Alertmanager", 2)), SVELTE: (("Prometheus", 8),)}
# `observer/loki/loki-values.yaml` `persistence.size: 10Gi`, in one claim.
_LOKI_GIB = 10
# One 1 GiB volume per application PVC, six of them in the measured run; see the module
# docstring for why the count is a constant and not read from the problem's manifests.
_APP_VOLUME_GIB = 1
_APP_VOLUMES_PER_PROFILE = {FULL: 6, SVELTE: 6}


@dataclass(frozen=True)
class RequestFootprint:
    """What one run asks the cluster for, summed over every pod and volume it deploys."""

    pods: int
    cpu_millicores: int
    memory_mib: int
    persistentvolumeclaims: int
    storage_gib: int
    # (component, GiB) in the order they are summed, so the refusal can show the sum.
    storage_components: tuple[tuple[str, int], ...]

    @property
    def storage_sentence(self) -> str:
        """Return e.g. "8Gi Prometheus + 2Gi Alertmanager + 6Gi application volumes = 16Gi"."""
        terms = " + ".join(f"{gib}Gi {name}" for name, gib in self.storage_components)
        return f"{terms} = {self.storage_gib}Gi"


@dataclass(frozen=True)
class Allocatable:
    """Capacity the scheduler can use, summed over the nodes it may place a pod on."""

    nodes: tuple[str, ...]
    cpu_millicores: int
    memory_mib: int


@dataclass(frozen=True)
class Limit:
    """One dimension of one capacity source, with the two numbers the message needs."""

    source: str
    dimension: str
    unit: str
    requested: int
    available: int

    def sentence(self) -> str:
        """Return e.g. "CPU 3400m of 2400m available (the 3 untainted worker nodes)"."""
        return f"{self.dimension} {self.requested}{self.unit} of {self.available}{self.unit} available ({self.source})"

    @property
    def is_exceeded(self) -> bool:
        """Return True when the run asks for more than this dimension allows."""
        return self.requested > self.available

    @property
    def is_tight(self) -> bool:
        """Return True when the run uses at least `WARN_RATIO` of what this dimension allows."""
        return self.available > 0 and self.requested >= self.available * WARN_RATIO


class CapacityShortfall(RuntimeError):
    """The run's request footprint does not fit the cluster, or a namespace's quota."""


def footprint(profile: str | None = None, *, deploy_loki: bool = True) -> RequestFootprint:
    """Return the documented footprint of one run under `profile` (default: the active one).

    `deploy_loki` is the conductor's own condition for deploying Loki
    (`ConductorConfig.deploy_loki`), so a run that skips it claims no Loki volume.
    """
    profile = profile or get_profile()
    pods = _PODS_PER_PROFILE[profile]
    components = list(_OBSERVER_VOLUMES_GIB[profile])
    claims = len(components)
    app_volumes = _APP_VOLUMES_PER_PROFILE[profile]
    components.append(("application volumes", app_volumes * _APP_VOLUME_GIB))
    claims += app_volumes
    if deploy_loki:
        components.append(("Loki", _LOKI_GIB))
        claims += 1
    return RequestFootprint(
        pods=pods,
        cpu_millicores=pods * _POD_CPU_MILLICORES,
        memory_mib=pods * _POD_MEMORY_MIB,
        persistentvolumeclaims=claims,
        storage_gib=sum(gib for _name, gib in components),
        storage_components=tuple(components),
    )


def untainted_allocatable(kubectl) -> Allocatable | None:
    """Sum `status.allocatable` over the nodes that carry no taint.

    A tainted node is not counted because this run's workloads carry no toleration,
    so the scheduler cannot place a pod on it; that is the rule §13.4 applies when it
    counts three untainted workers.

    Returns None when the cluster does not answer with a node list: the preflight may
    refuse a run that will not fit, never one it could not see. A node list whose
    every node is tainted is a different answer, and it sums to zero capacity, which
    the footprint then exceeds.
    """
    try:
        nodes = json.loads(kubectl.exec_command("kubectl get nodes -o json")).get("items")
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(nodes, list) or not nodes:
        return None

    names: list[str] = []
    cpu_millicores = 0
    memory_mib = 0
    for node in nodes:
        if not isinstance(node, dict) or (node.get("spec") or {}).get("taints"):
            continue
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        node_cpu = _cpu_millicores(allocatable.get("cpu"))
        node_memory = _memory_mib(allocatable.get("memory"))
        if node_cpu is None or node_memory is None:
            return None
        names.append((node.get("metadata") or {}).get("name") or "unnamed")
        cpu_millicores += node_cpu
        memory_mib += node_memory
    return Allocatable(tuple(names), cpu_millicores, memory_mib)


def check_capacity(kubectl, namespaces: Iterable[str], *, deploy_loki: bool = True, log=None) -> None:
    """Refuse when one run's footprint does not fit, and warn above `WARN_RATIO`.

    `namespaces` are the namespaces the run deploys into; a `ResourceQuota` in any of
    them is checked as well. `deploy_loki` is the conductor's own condition for
    deploying Loki. Raises `CapacityShortfall` naming every dimension that does not
    fit, and restating the storage estimate with its components, so the run stops
    before the pods are created instead of failing one readiness timeout per pod. A
    capacity the cluster does not report is a warning, not a refusal: this preflight
    must never stop a run because it could not see.
    """
    log = log if log is not None else logger
    need = footprint(deploy_loki=deploy_loki)
    allocatable = untainted_allocatable(kubectl)
    if allocatable is None:
        log.warning(
            "Cannot check the capacity for this run: 'kubectl get nodes -o json' did not answer with a node list, "
            "so what the workers can allocate is unknown. Continuing without the capacity preflight."
        )
        return

    limits = _worker_limits(need, allocatable)
    for namespace in dict.fromkeys(namespaces):
        limits.extend(_quota_limits(kubectl, namespace, need, log))

    exceeded = [limit.sentence() for limit in limits if limit.is_exceeded]
    tight = [limit.sentence() for limit in limits if not limit.is_exceeded and limit.is_tight]
    if exceeded:
        raise CapacityShortfall(
            f"Refusing to deploy: this run asks for {need.pods} pods, {need.cpu_millicores}m of CPU, "
            f"{need.memory_mib}Mi of memory and {need.storage_gib}Gi of storage in "
            f"{need.persistentvolumeclaims} persistent volume claims, and it does not fit -- "
            + "; ".join(exceeded)
            + f". The storage estimate is {need.storage_sentence}."
        )
    if tight:
        log.warning(
            "Capacity is above %d%% of what this cluster has: %s. One run fits, two concurrent runs will not.",
            round(WARN_RATIO * 100),
            "; ".join(tight),
        )


def _worker_limits(need: RequestFootprint, allocatable: Allocatable) -> list[Limit]:
    """Return the CPU and memory dimensions of the untainted workers."""
    source = f"the {len(allocatable.nodes)} untainted worker node(s)"
    return [
        Limit(source, "CPU", "m", need.cpu_millicores, allocatable.cpu_millicores),
        Limit(source, "memory", "Mi", need.memory_mib, allocatable.memory_mib),
    ]


def _quota_limits(kubectl, namespace: str, need: RequestFootprint, log) -> list[Limit]:
    """Return the dimensions of `namespace`'s `ResourceQuota` that this run is measured against.

    A namespace with no quota, or one this identity may not read, contributes
    nothing: a namespace the run has not created yet cannot have a quota, and a
    preflight must not stop a run over a quota it could not see.

    The whole footprint is compared with each namespace's quota, which is
    conservative when the run splits across namespaces: the quota has to hold the
    whole run for the check to pass. That is recorded here rather than worked around,
    because a per-namespace split would be a guess about which pod goes where.
    """
    try:
        quotas = kubectl.get_resource_quotas(namespace) or []
    except Exception as exc:  # noqa: BLE001 - an unreadable quota is a warning, never a refusal
        log.debug("Could not read the ResourceQuota in %s (%s); its quota is not checked.", namespace, exc)
        return []
    limits: list[Limit] = []
    for quota in quotas:
        name = getattr(getattr(quota, "metadata", None), "name", None) or "unnamed"
        hard = getattr(getattr(quota, "spec", None), "hard", None) or {}
        for key, dimension, unit, field_name, reader in _QUOTA_DIMENSIONS:
            available = reader(hard.get(key))
            if available is None:
                continue
            limits.append(
                Limit(f"ResourceQuota {name} in {namespace}", dimension, unit, getattr(need, field_name), available)
            )
    return limits


def _number(text) -> tuple[float, str] | None:
    """Split a Kubernetes quantity into its number and its suffix, or None."""
    match = _QUANTITY.match(str(text or "").strip())
    return (float(match.group(1)), match.group(2)) if match else None


def _cpu_millicores(text) -> int | None:
    """Return `text` as millicores, or None when it is not a CPU quantity."""
    parsed = _number(text)
    if parsed is None:
        return None
    number, suffix = parsed
    if suffix == "m":
        return int(number)
    return int(number * 1000) if suffix == "" else None


def _memory_mib(text) -> int | None:
    """Return `text` as MiB, or None when it is not a memory quantity."""
    parsed = _number(text)
    if parsed is None:
        return None
    number, suffix = parsed
    return int(number * _BYTES_PER_SUFFIX[suffix] / _MIB) if suffix in _BYTES_PER_SUFFIX else None


def _storage_gib(text) -> int | None:
    """Return `text` as whole GiB, rounded down, or None when it is not a storage quantity."""
    parsed = _number(text)
    if parsed is None:
        return None
    number, suffix = parsed
    return int(number * _BYTES_PER_SUFFIX[suffix] / _GIB) if suffix in _BYTES_PER_SUFFIX else None


def _count(text) -> int | None:
    """Return `text` as a whole number, or None when it is not one."""
    parsed = _number(text)
    return int(parsed[0]) if parsed is not None and parsed[1] == "" else None


# (quota key, dimension label, unit, request footprint field, reader)
_QUOTA_DIMENSIONS = (
    ("requests.cpu", "CPU", "m", "cpu_millicores", _cpu_millicores),
    ("requests.memory", "memory", "Mi", "memory_mib", _memory_mib),
    ("requests.storage", "storage", "Gi", "storage_gib", _storage_gib),
    ("persistentvolumeclaims", "persistent volume claims", "", "persistentvolumeclaims", _count),
    ("pods", "pods", "", "pods", _count),
)
