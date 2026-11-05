"""Did this run harm the cluster? One verdict, computed from two snapshots.

Every claim that SREGym leaves a cluster as it found it rests on a comparison, and until now the
comparison was a line printed by a shell script outside the repository. This module makes it a
verdict the run produces about itself.

The rule:

- an object present in the baseline and **absent** now is a removal;
- an object whose **content** changed is a change, for the small set of objects a run is most
  likely to disturb (node labels and taints, the storage classes, the webhooks, CoreDNS's config);
- an object that appeared is an **addition**: allowed, and listed, because a run that deploys
  observers and an application always adds things.

A removal or a change fails the run. The single tolerance is a removal the teardown itself
performed and reported: `deleted_by_us`. That is deliberate. A name list of things SREGym "may"
delete would excuse any object that happened to carry one of those names, while a list built from
what the teardown actually deleted can only excuse what it actually did.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

CLEAN = "clean"
OWNED = "owned"
DAMAGED = "damaged"

# Name lists compared between the two snapshots.
_NAME_LISTS = (
    ("namespace", "namespaces"),
    ("clusterrole", "cluster_roles"),
    ("clusterrolebinding", "cluster_role_bindings"),
    ("persistentvolume", "persistent_volumes"),
    ("storageclass", "storage_classes"),
    ("customresourcedefinition", "crds"),
    ("validatingwebhookconfiguration", "validating_webhook_configs"),
    ("mutatingwebhookconfiguration", "mutating_webhook_configs"),
    ("validatingadmissionpolicy", "validating_admission_policies"),
    ("validatingadmissionpolicybinding", "validating_admission_policy_bindings"),
)

# Fields compared by content. A name that survives with different content is not survival.
_CONTENT_FIELDS = (
    ("node labels", "node_labels"),
    ("node taints", "node_taints"),
    ("coredns config", "coredns_configmap_data"),
)


@dataclass
class DamageReport:
    """What changed between two snapshots, and what that means for the run."""

    verdict: str
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    owned_removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    survivors: list[str] = field(default_factory=list)
    cluster_uid_changed: bool = False

    @property
    def harmless(self) -> bool:
        return self.verdict in (CLEAN, OWNED)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "removed": self.removed,
            "changed": self.changed,
            "owned_removed": self.owned_removed,
            "added": self.added,
            "survivors": self.survivors,
            "cluster_uid_changed": self.cluster_uid_changed,
        }

    def text(self) -> str:
        """Return the operator-facing summary: one verdict line, then what produced it."""
        lines = [f"Cluster damage verdict: {self.verdict.upper()}"]
        if self.cluster_uid_changed:
            lines.append("  the cluster is not the one the baseline was taken on (cluster uid changed)")
        for label, items in (
            ("removed, not by this harness", self.removed),
            ("changed", self.changed),
            ("removed by this harness", self.owned_removed),
        ):
            if items:
                lines.append(f"  {label}: {', '.join(sorted(items))}")
        if self.added:
            lines.append(f"  added: {', '.join(sorted(self.added))}")
        if self.survivors:
            lines.append(f"  SREGym objects that survived the teardown: {', '.join(sorted(self.survivors))}")
        if self.verdict == DAMAGED:
            if self.survivors:
                lines.append("  this harness left objects behind, so the cluster is not as it was found")
            else:
                lines.append("  a pre-existing object was removed or changed, so this run is not a result")
        return "\n".join(lines)

    def write(self, path: Path) -> None:
        """Store the verdict beside the run's results."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")


def _as_set(value) -> set:
    """Return a comparable set for one snapshot field, tolerant of list, set, dict or None."""
    if value is None:
        return set()
    if isinstance(value, dict):
        return set(value)
    return set(value)


def compare(baseline, current, deleted_by_us=(), owned_namespaces=(), survivors=()) -> DamageReport:
    """Compare two `ClusterBaseline` snapshots and return the verdict.

    `deleted_by_us` names the objects the teardown removed (``"clusterrole/mcp-server"`` and the
    like), and only those removals are tolerated.

    `owned_namespaces` are the namespaces this run deploys into. Their disappearance is the run's
    own cleanup, and a PersistentVolume whose claim lived in one of them went with it. A volume
    whose claim namespace is unknown is treated as not ours, because a verdict must not excuse a
    removal it cannot attribute.

    `survivors` names the objects this harness owns that are still on the cluster after a teardown
    that was asked for. An addition is normally the run's own work and is only reported, but once
    the operator has asked for the cluster back, an object of ours that outlived the teardown is
    exactly the leftover the verdict exists to catch. The caller passes this only when a teardown
    ran: without one, leftovers are the requested state and calling them damage would be wrong.
    """
    tolerated = set(deleted_by_us)
    owned = set(owned_namespaces)
    removed: list[str] = []
    owned_removed: list[str] = []
    added: list[str] = []
    changed: list[str] = []

    for kind, attribute in _NAME_LISTS:
        before = _as_set(getattr(baseline, attribute, None))
        after = _as_set(getattr(current, attribute, None))
        for name in sorted(before - after):
            target = f"{kind}/{name}"
            if kind == "namespace" and name in owned:
                owned_removed.append(target)
                continue
            if kind == "persistentvolume":
                claim_namespace = (getattr(baseline, "persistent_volume_namespaces", None) or {}).get(name)
                if claim_namespace in owned:
                    owned_removed.append(target)
                    continue
            (owned_removed if target in tolerated else removed).append(target)
        added.extend(f"{kind}/{name}" for name in sorted(after - before))

    for label, attribute in _CONTENT_FIELDS:
        before = getattr(baseline, attribute, None)
        after = getattr(current, attribute, None)
        # A field neither snapshot carries is not a change; one the capture failed to read on
        # either side is unknown, and a verdict must not invent a difference from ignorance.
        if before is None or after is None:
            continue
        if before != after:
            changed.append(label)

    baseline_uid = getattr(baseline, "cluster_uid", None)
    current_uid = getattr(current, "cluster_uid", None)
    uid_changed = bool(baseline_uid and current_uid and baseline_uid != current_uid)

    survived = sorted(set(survivors))
    if removed or changed or uid_changed or survived:
        verdict = DAMAGED
    elif owned_removed:
        verdict = OWNED
    else:
        verdict = CLEAN

    return DamageReport(
        verdict=verdict,
        removed=removed,
        changed=changed,
        owned_removed=owned_removed,
        added=added,
        survivors=survived,
        cluster_uid_changed=uid_changed,
    )
