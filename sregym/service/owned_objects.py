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
happens to share a name is not at risk. Deletion is by ownership, not by name: every delete is a
typed Kubernetes API call that carries a UID precondition, so the API server refuses it when the
object at that name has been replaced since it was listed.
"""

import json
import logging

from kubernetes import client
from kubernetes.client import V1DeleteOptions, V1Preconditions
from kubernetes.client.rest import ApiException

from sregym.service.ownership_ledger import KINDS as LEDGER_KINDS
from sregym.service.ownership_ledger import OwnershipLedger, delete_owned

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

# The typed client class and the delete method that removes one object of each kind by name.
_TYPED_DELETE = {
    "namespace": ("CoreV1Api", "delete_namespace"),
    "clusterrole": ("RbacAuthorizationV1Api", "delete_cluster_role"),
    "clusterrolebinding": ("RbacAuthorizationV1Api", "delete_cluster_role_binding"),
    "mutatingwebhookconfiguration": ("AdmissionregistrationV1Api", "delete_mutating_webhook_configuration"),
}


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


def _named(items: list[dict]) -> list[tuple[str, dict]]:
    """Return (name, metadata) for each listed item that has a name; an item without one is skipped."""
    named = []
    for item in items:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        if not isinstance(metadata, dict):
            continue
        name = metadata.get("name")
        if isinstance(name, str) and name:
            named.append((name, metadata))
    return named


def _owned_in(items: list[dict], kind: str, ledger: OwnershipLedger | None) -> list[str]:
    return sorted(name for name, metadata in _named(items) if is_owned(metadata, kind, ledger))


def _foreign_in(items: list[dict], kind: str, ledger: OwnershipLedger | None) -> list[str]:
    return sorted(name for name, metadata in _named(items) if not is_owned(metadata, kind, ledger))


def _typed_api(kubectl, api_name: str):
    """Return the typed client `api_name`, on the same ApiClient KubeCtl uses.

    `CoreV1Api` is KubeCtl's own `core_v1_api`. The others are built from that ApiClient, so they
    share its kubeconfig, credentials and TLS settings.
    """
    if api_name == "CoreV1Api":
        return kubectl.core_v1_api
    return getattr(client, api_name)(kubectl.core_v1_api.api_client)


def _deleter(kubectl, kind: str, name: str):
    """Return `delete(body=...)`, the typed call that deletes `kind/name` with the body it is given."""
    api_name, method = _TYPED_DELETE[kind]
    delete = getattr(_typed_api(kubectl, api_name), method)
    return lambda body: delete(name=name, body=body)


def _delete_by_uid(kubectl, kind: str, name: str, uid: str) -> str:
    """Delete `kind/name` only while it still has `uid`, and return "deleted", "absent" or "foreign"."""
    body = V1DeleteOptions(preconditions=V1Preconditions(uid=uid))
    try:
        _deleter(kubectl, kind, name)(body=body)
    except ApiException as exc:
        if exc.status == 404:
            return "absent"
        if exc.status == 409:
            return "foreign"
        raise
    return "deleted"


def _delete_listed(kubectl, kind: str, name: str, uid: str | None, ledger: OwnershipLedger | None, log, tag) -> bool:
    """Delete `kind/name` as listed with `uid`, and return whether this call removed it.

    An object the ledger recorded goes through `delete_owned`, which also forgets its entry. An
    object owned by its label has no ledger entry, so it is deleted with the listed UID as the
    precondition. Either way a same-named object that replaced it is refused by the API server.
    A failed delete is logged and returns False, so its ledger entry is kept for the next try.
    """
    if not uid:
        log.warning("%s Not deleting %s/%s: the listing gave no UID to check it against", tag, kind, name)
        return False
    try:
        if ledger is not None and ledger.owns(kind, name, uid):
            outcome = delete_owned(ledger, kind, name, _deleter(kubectl, kind, name))
        else:
            outcome = _delete_by_uid(kubectl, kind, name, uid)
    except Exception as exc:
        log.warning("%s Could not delete %s/%s: %s", tag, kind, name, exc)
        return False
    if outcome == "foreign":
        log.warning("%s Not deleting %s/%s: it was replaced after it was listed, so it is not ours", tag, kind, name)
    return outcome == "deleted"


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

    Every delete carries a UID precondition set to the UID listed for that object, so an object
    replaced after the listing is refused and left alone. Each deletion is logged, and a
    same-named object SREGym does not own is left alone and counted in a line per kind, so an
    operator can see that something exists which the harness deliberately did not remove. A
    deleted object's ledger entry is forgotten. A kind whose listing succeeds has its ledger
    entries for objects no longer present forgotten too; a failed listing prunes nothing.
    """
    log = log if log is not None else logger
    removed: list[str] = []
    for kind in OWNED_CLUSTER_SCOPED_KINDS:
        items = _list_items(kubectl, kind, log)
        if items is None:
            continue
        live = dict(_named(items))
        for name in _owned_in(items, kind, ledger):
            log.info("[TEARDOWN] Deleting %s/%s, owned by this harness", kind, name)
            if _delete_listed(kubectl, kind, name, live[name].get("uid"), ledger, log, "[TEARDOWN]"):
                removed.append(f"{kind}/{name}")
        if ledger is not None:
            present = set(live)
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

    Each delete carries the recorded UID as a precondition. A namespace replaced after the listing
    is refused by the API server (409) and its entry forgotten. A ledger entry for a namespace absent
    from a successful listing is forgotten. A failed deletion keeps its entry, so the next teardown
    tries again.
    """
    log = log if log is not None else logger
    items = _list_items(kubectl, "namespace", log)
    if items is None:
        return []
    removed: list[str] = []
    live = dict(_named(items))
    for name in _ledger_namespaces_in(items, ledger):
        log.info("[TEARDOWN] Deleting namespace/%s, created by this run", name)
        if _delete_listed(kubectl, "namespace", name, live[name].get("uid"), ledger, log, "[TEARDOWN]"):
            removed.append(f"namespace/{name}")
    present = set(live)
    for entry in ledger.entries("namespace"):
        if entry.name not in present:
            ledger.forget("namespace", entry.name)
    return removed


def delete_ledger_objects(kubectl, ledger: OwnershipLedger, log=None) -> list[str]:
    """Delete every ledger-recorded object still on the cluster under its recorded UID, and return their names.

    Covers every kind the ledger records, so a scenario object its own recovery did not remove
    (a webhook, a ClusterRole or binding, `platform-ops`) is still collected here. An object is
    deleted only when its live UID equals the recorded one, and the delete itself carries that UID
    as a precondition, so an object replaced between the listing and the delete is refused by the
    API server (409) and left in place. An object that carries the infrastructure or Helm label is
    never deleted, whatever the ledger says. An entry whose object is gone, or whose name now holds
    a different UID, or whose delete was refused as foreign, is forgotten; any other failed deletion
    keeps its entry for the next try.
    """
    log = log if log is not None else logger
    removed: list[str] = []
    for kind in LEDGER_KINDS:
        entries = ledger.entries(kind)
        if not entries:
            continue
        items = _list_items(kubectl, kind, log)
        if items is None:
            continue
        live = dict(_named(items))
        for entry in entries:
            metadata = live.get(entry.name)
            if metadata is None or not ledger.owns(kind, entry.name, metadata.get("uid")):
                ledger.forget(kind, entry.name)
                continue
            if is_owned(metadata):
                log.warning(
                    "[CLEANUP] Not deleting %s/%s: it carries the harness infrastructure label", kind, entry.name
                )
                continue
            log.info("[CLEANUP] Deleting %s/%s, recorded by this run", kind, entry.name)
            if _delete_listed(kubectl, kind, entry.name, metadata.get("uid"), ledger, log, "[CLEANUP]"):
                removed.append(f"{kind}/{entry.name}")
    return removed


def _ledger_namespaces_in(items: list[dict], ledger: OwnershipLedger) -> list[str]:
    return sorted(name for name, metadata in _named(items) if ledger.owns("namespace", name, metadata.get("uid")))
