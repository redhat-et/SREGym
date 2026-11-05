"""Which cluster-scoped objects belong to SREGym, and how to remove exactly those.

A run creates objects outside its namespaces: the observer charts render ClusterRoles and
ClusterRoleBindings, and the MCP server manifest creates its own. Those survive a campaign for
two reasons. `ClusterStateManager` deletes only cluster-scoped objects *added since the current
run's baseline*, so an object created by an interrupted earlier attempt looks like platform
infrastructure; and reconcile, the only path that deletes anything, defaults to `off` on a
managed platform.

Ownership is a label, not a name list. A name list needs maintaining whenever a chart is
upgraded and cannot tell SREGym's `prometheus-server` from someone else's. Two labels qualify an
object:

- `app.kubernetes.io/instance` set to one of the release names the harness deploys, which Helm
  puts on every object it creates;
- `sregym.io/owned: "true"`, which the harness adds to the manifests it applies itself.

An object of the same name without either label is never touched. That is the whole safety
argument: deletion is by ownership, so a cluster where a platform component happens to share a
name is not at risk.
"""

import json
import logging

import yaml

logger = logging.getLogger("all.sregym.owned_objects")

OWNED_LABEL = "sregym.io/owned"
OWNED_VALUE = "true"

# The Helm releases this harness deploys into the observe namespace. Helm stamps every object
# it creates with `app.kubernetes.io/instance: <release>`.
HELM_INSTANCES = frozenset({"prometheus", "loki", "promtail"})

# The cluster-scoped kinds the harness can create. A namespace and a PersistentVolume are
# handled by the namespace and PV paths, which know how to wait; these have no finalizers a run
# must respect. A mutating webhook configuration is here because one scenario installs five of
# them, and the ones it installs carry the ownership label; a crashed run would otherwise leave
# webhooks that intercept every matching write on the cluster with nothing to collect them.
OWNED_CLUSTER_SCOPED_KINDS = ("clusterrole", "clusterrolebinding", "mutatingwebhookconfiguration")


def label_owned(resources: list[dict]) -> list[dict]:
    """Return `resources` with the ownership label set on every object.

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
        metadata.setdefault("labels", {})[OWNED_LABEL] = OWNED_VALUE
        labelled.append(body)
    return labelled


def is_owned(metadata: dict | None) -> bool:
    """Return whether this object's metadata says SREGym created it.

    `metadata` is the object's metadata block, not the object. Either label is enough: a Helm
    release name proves the harness deployed it, and the explicit label proves it for the objects
    the harness applies directly.
    """
    labels = (metadata or {}).get("labels") or {}
    if labels.get(OWNED_LABEL) == OWNED_VALUE:
        return True
    return labels.get("app.kubernetes.io/instance") in HELM_INSTANCES


def owned_names(kubectl, kind: str, log=None) -> list[str]:
    """Return the names of `kind` that `kubectl` reports and that SREGym owns.

    A read failure returns an empty list with one warning: the caller is a teardown, and a
    teardown that raises because the API server hiccuped leaves more behind, not less.
    """
    log = log if log is not None else logger
    try:
        listing = kubectl.exec_command_checked(f"kubectl get {kind} -o json", timeout=60)
    except Exception as exc:
        log.warning("Could not list %s, so none will be removed by name: %s", kind, exc)
        return []
    try:
        items = json.loads(listing).get("items", [])
    except (ValueError, AttributeError):
        log.warning("Could not read the %s listing, so none will be removed by name", kind)
        return []
    return sorted(item["metadata"]["name"] for item in items if is_owned(item.get("metadata")))


def foreign_names(kubectl, kind: str) -> list[str]:
    """Return the names of `kind` that SREGym did not create, for the log line that says so."""
    try:
        listing = kubectl.exec_command_checked(f"kubectl get {kind} -o json", timeout=60)
        items = json.loads(listing).get("items", [])
    except Exception:
        return []
    return sorted(item["metadata"]["name"] for item in items if not is_owned(item.get("metadata")))


def delete_owned_cluster_scoped(kubectl, log=None) -> list[str]:
    """Delete every cluster-scoped object SREGym owns, whatever its age, and return their names.

    Each deletion is logged, and a same-named object carrying another owner's labels is left
    alone and named in a warning, so an operator can see that something exists which the harness
    deliberately did not remove.
    """
    log = log if log is not None else logger
    removed: list[str] = []
    for kind in OWNED_CLUSTER_SCOPED_KINDS:
        for name in owned_names(kubectl, kind, log=log):
            log.info("[TEARDOWN] Deleting %s/%s, owned by this harness", kind, name)
            try:
                kubectl.exec_command_checked(f"kubectl delete {kind} {name} --ignore-not-found", timeout=120)
            except Exception as exc:
                log.warning("[TEARDOWN] Could not delete %s/%s: %s", kind, name, exc)
                continue
            removed.append(f"{kind}/{name}")
        # One line per kind, not one per object: a cluster has hundreds of platform roles, and a
        # teardown that logs each of them buries the deletions it did make.
        kept = foreign_names(kubectl, kind)
        if kept:
            log.info("[TEARDOWN] Keeping %d %s object(s) not labelled as this harness's", len(kept), kind)
    return removed


def yaml_documents(text: str) -> list[dict]:
    """Return the YAML documents in `text` that are mappings, for callers that render manifests."""
    return [body for body in yaml.safe_load_all(text) if isinstance(body, dict)]
