"""Which cluster objects belong to SREGym, and how to remove exactly those.

A run creates objects outside its namespaces, and those survive a campaign for two reasons.
`ClusterStateManager` deletes only cluster-scoped objects *added since the current run's
baseline*, so an object created by an interrupted earlier attempt looks like platform
infrastructure; and reconcile, the only path that deletes anything, defaults to `off` on a
managed platform. Three groups of objects need removing:

- Harness infrastructure, the MCP server manifests. They carry one neutral label,
  `app.kubernetes.io/instance: observability-stack`, which the harness adds when it applies them.
- The observer Helm releases (Prometheus, Loki, Promtail). Helm puts `app.kubernetes.io/instance`
  on every object it creates, and records the release name and namespace in annotations; both
  must match before an object counts as ours.
- Scenario-created objects (mutating webhooks, `platform-ops`, the finalizer scenario's RBAC).
  No label marks these. The run's ownership ledger (`sregym.service.ownership_ledger`) records
  each one by name and UID at creation time, and an object is ours only when its live UID matches.

An object that matches none of these is never touched, so a cluster where a platform component
happens to share a name is not at risk. Deletion is by ownership, not by name.
"""

import json
import logging

from sregym.service.ownership_ledger import OwnershipLedger

logger = logging.getLogger("all.sregym.owned_objects")

# The label the harness puts on the infrastructure manifests it applies itself, and its value.
INFRA_LABEL = "app.kubernetes.io/instance"
INFRA_VALUE = "observability-stack"

# The Helm releases this harness deploys into the observe namespace. Helm stamps every object
# it creates with `app.kubernetes.io/instance: <release>` and the release name and namespace
# annotations, so an object of the same label but another release or namespace is not ours.
HELM_INSTANCES = frozenset({"prometheus", "loki", "promtail"})
HELM_NAMESPACE = "observe"

# The cluster-scoped kinds the harness can create. A namespace and a PersistentVolume are
# handled by the namespace and PV paths, which know how to wait; these have no finalizers a run
# must respect. A mutating webhook configuration is here because one scenario installs five of
# them. A crashed run would otherwise leave webhooks that intercept every matching write on the
# cluster with nothing to collect them, so they are recorded in the ledger and removed by UID.
OWNED_CLUSTER_SCOPED_KINDS = ("clusterrole", "clusterrolebinding", "mutatingwebhookconfiguration")


def label_infrastructure(resources: list[dict]) -> list[dict]:
    """Return `resources` with the harness infrastructure label set on every object.

    Called on the manifests the harness applies itself, so that the deletion path can find them
    without a name list. A resource that already carries the label is left alone, and a resource
    whose metadata cannot carry a label is returned unchanged rather than dropped: this must not
    be the reason a deploy fails.
    """
    labelled = []
    for body in resources:
        metadata = body.get("metadata") if isinstance(body, dict) else None
        if not isinstance(metadata, dict) or "name" not in metadata:
            labelled.append(body)
            continue
        metadata.setdefault("labels", {})[INFRA_LABEL] = INFRA_VALUE
        labelled.append(body)
    return labelled


def is_owned(metadata: dict | None, kind: str | None = None, ledger: OwnershipLedger | None = None) -> bool:
    """Return whether this object's metadata says SREGym created it.

    `metadata` is the object's metadata block, not the object. The infrastructure label or a
    matching Helm release is enough. Otherwise, when a `ledger` and the object's `kind` are given,
    the object is ours only if the ledger recorded this exact name and UID.
    """
    metadata = metadata or {}
    labels = metadata.get("labels") or {}
    instance = labels.get(INFRA_LABEL)
    if instance == INFRA_VALUE:
        return True
    if instance in HELM_INSTANCES:
        annotations = metadata.get("annotations") or {}
        if (
            annotations.get("meta.helm.sh/release-name") == instance
            and annotations.get("meta.helm.sh/release-namespace") == HELM_NAMESPACE
        ):
            return True
    if ledger is not None and kind:
        return ledger.owns(kind, metadata.get("name"), metadata.get("uid"))
    return False


def _list_items(kubectl, kind: str, log) -> list[dict] | None:
    """Return the items of `kubectl get <kind> -o json`, or None when the listing cannot be read.

    None, rather than an empty list, is how a caller tells a failed listing from an empty cluster:
    a failed listing must never be taken as proof that a ledger entry is gone.
    """
    try:
        listing = kubectl.exec_command_checked(f"kubectl get {kind} -o json", timeout=60)
    except Exception as exc:
        log.warning("Could not list %s, so nothing is matched from it: %s", kind, exc)
        return None
    try:
        return json.loads(listing).get("items", [])
    except (ValueError, AttributeError):
        log.warning("Could not read the %s listing, so nothing is matched from it", kind)
        return None


def _owned_in(items: list[dict], kind: str, ledger: OwnershipLedger | None) -> list[str]:
    return sorted(item["metadata"]["name"] for item in items if is_owned(item.get("metadata"), kind, ledger))


def _foreign_in(items: list[dict], kind: str, ledger: OwnershipLedger | None) -> list[str]:
    return sorted(item["metadata"]["name"] for item in items if not is_owned(item.get("metadata"), kind, ledger))


def owned_names(kubectl, kind: str, log=None, ledger: OwnershipLedger | None = None) -> list[str]:
    """Return the names of `kind` that `kubectl` reports and that SREGym owns.

    A read failure returns an empty list with one warning: the caller is a teardown, and a
    teardown that raises because the API server hiccuped leaves more behind, not less.
    """
    log = log if log is not None else logger
    items = _list_items(kubectl, kind, log)
    if items is None:
        return []
    return _owned_in(items, kind, ledger)


def foreign_names(kubectl, kind: str, ledger: OwnershipLedger | None = None) -> list[str]:
    """Return the names of `kind` that SREGym did not create, for the log line that says so."""
    items = _list_items(kubectl, kind, logger)
    if items is None:
        return []
    return _foreign_in(items, kind, ledger)


def delete_owned_cluster_scoped(kubectl, log=None, ledger: OwnershipLedger | None = None) -> list[str]:
    """Delete every cluster-scoped object SREGym owns, whatever its age, and return their names.

    Each deletion is logged, and a same-named object SREGym does not own is left alone and
    counted in a line per kind, so an operator can see that something exists which the harness
    deliberately did not remove. A deleted object's ledger entry is forgotten. A kind whose
    listing succeeds has its ledger entries for objects no longer present forgotten too; a
    failed listing prunes nothing.
    """
    log = log if log is not None else logger
    removed: list[str] = []
    for kind in OWNED_CLUSTER_SCOPED_KINDS:
        items = _list_items(kubectl, kind, log)
        if items is None:
            continue
        for name in _owned_in(items, kind, ledger):
            log.info("[TEARDOWN] Deleting %s/%s, owned by this harness", kind, name)
            try:
                kubectl.exec_command_checked(f"kubectl delete {kind} {name} --ignore-not-found", timeout=120)
            except Exception as exc:
                log.warning("[TEARDOWN] Could not delete %s/%s: %s", kind, name, exc)
                continue
            removed.append(f"{kind}/{name}")
            if ledger is not None:
                ledger.forget(kind, name)
        if ledger is not None:
            present = {item["metadata"]["name"] for item in items if item.get("metadata")}
            for entry in ledger.entries(kind):
                if entry.name not in present:
                    ledger.forget(kind, entry.name)
        # One line per kind, not one per object: a cluster has hundreds of platform roles, and a
        # teardown that logs each of them buries the deletions it did make.
        kept = _foreign_in(items, kind, ledger)
        if kept:
            log.info("[TEARDOWN] Keeping %d %s object(s) not owned by this harness", len(kept), kind)
    return removed


def ledger_namespaces_present(kubectl, ledger: OwnershipLedger, log=None) -> list[str]:
    """Return the names of namespaces that exist now and whose UID the ledger recorded."""
    log = log if log is not None else logger
    items = _list_items(kubectl, "namespace", log)
    if items is None:
        return []
    return _ledger_namespaces_in(items, ledger)


def delete_ledger_namespaces(kubectl, ledger: OwnershipLedger, log=None) -> list[str]:
    """Delete each namespace the ledger recorded with a matching UID, and return their names.

    A ledger entry for a namespace absent from a successful listing is forgotten. A failed
    deletion keeps its entry, so the next teardown tries again.
    """
    log = log if log is not None else logger
    items = _list_items(kubectl, "namespace", log)
    if items is None:
        return []
    removed: list[str] = []
    for name in _ledger_namespaces_in(items, ledger):
        log.info("[TEARDOWN] Deleting namespace/%s, created by this run", name)
        try:
            kubectl.exec_command_checked(f"kubectl delete namespace {name} --ignore-not-found", timeout=120)
        except Exception as exc:
            log.warning("[TEARDOWN] Could not delete namespace/%s: %s", name, exc)
            continue
        ledger.forget("namespace", name)
        removed.append(f"namespace/{name}")
    present = {item["metadata"]["name"] for item in items if item.get("metadata")}
    for entry in ledger.entries("namespace"):
        if entry.name not in present:
            ledger.forget("namespace", entry.name)
    return removed


def _ledger_namespaces_in(items: list[dict], ledger: OwnershipLedger) -> list[str]:
    owned = []
    for item in items:
        metadata = item.get("metadata") or {}
        if ledger.owns("namespace", metadata.get("name"), metadata.get("uid")):
            owned.append(metadata["name"])
    return sorted(owned)
