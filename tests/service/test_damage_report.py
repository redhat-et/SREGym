"""The verdict has three outcomes, and only one of them can excuse a disappearance.

`deleted_by_us` is the tolerance, and it is an argument rather than a name list on purpose: a
verdict that recognised names could excuse an object it merely recognised.
"""

from dataclasses import dataclass, field

from sregym.service.damage_report import CLEAN, DAMAGED, OWNED, compare


@dataclass
class Snapshot:
    """The subset of `ClusterBaseline` the comparison reads, so the test does not need a cluster."""

    cluster_uid: str = "uid-1"
    namespaces: frozenset = frozenset({"default", "kube-system", "observe", "hotel-reservation"})
    cluster_roles: frozenset = frozenset({"system:metrics-server", "prometheus-server", "mcp-server"})
    cluster_role_bindings: frozenset = frozenset({"system:metrics-server"})
    persistent_volumes: frozenset = frozenset({"pvc-1", "pvc-2"})
    storage_classes: frozenset = frozenset({"gp3", "openebs-hostpath"})
    crds: frozenset = frozenset({"prometheuses.monitoring.coreos.com"})
    validating_webhook_configs: frozenset = frozenset({"webhook-a"})
    mutating_webhook_configs: frozenset = frozenset()
    validating_admission_policies: frozenset = frozenset()
    validating_admission_policy_bindings: frozenset = frozenset()
    node_labels: dict = field(default_factory=lambda: {"worker-1": {"zone": "a"}})
    node_taints: dict = field(default_factory=lambda: {"worker-1": []})
    coredns_configmap_data: dict = field(default_factory=lambda: {"a": "1"})


def test_nothing_missing_is_clean():
    report = compare(Snapshot(), Snapshot())

    assert report.verdict == CLEAN
    assert report.harmless


def test_a_run_that_adds_things_is_clean_and_lists_them():
    after = Snapshot(
        namespaces=Snapshot().namespaces | {"generator"}, cluster_roles=Snapshot().cluster_roles | {"extra"}
    )

    report = compare(Snapshot(), after)

    assert report.verdict == CLEAN
    assert "namespace/generator" in report.added
    assert "clusterrole/extra" in report.added


def test_a_pre_existing_object_that_disappeared_fails_the_run():
    after = Snapshot(cluster_roles=frozenset({"prometheus-server", "mcp-server"}))

    report = compare(Snapshot(), after, deleted_by_us=frozenset({"clusterrole/mcp-server"}))

    assert report.verdict == DAMAGED
    assert report.removed == ["clusterrole/system:metrics-server"]
    assert not report.harmless


def test_what_the_teardown_reported_deleting_is_tolerated():
    after = Snapshot(namespaces=frozenset({"default", "kube-system", "hotel-reservation"}))
    deleted = frozenset({"namespace/observe"})

    report = compare(Snapshot(), after, deleted_by_us=deleted)

    assert report.verdict == OWNED
    assert report.owned_removed == ["namespace/observe"]
    assert report.harmless


def test_a_change_of_content_is_damage_even_when_the_name_survives():
    after = Snapshot()
    after.node_labels = {"worker-1": {"zone": "b"}}

    report = compare(Snapshot(), after)

    assert report.verdict == DAMAGED
    assert report.changed == ["node labels"]


def test_a_different_cluster_is_damage():
    report = compare(Snapshot(), Snapshot(cluster_uid="uid-2"))

    assert report.verdict == DAMAGED
    assert report.cluster_uid_changed


def test_the_text_names_the_verdict_and_the_culprits():
    after = Snapshot(storage_classes=frozenset({"gp3"}))

    text = compare(Snapshot(), after).text()

    assert "DAMAGED" in text
    assert "storageclass/openebs-hostpath" in text
    assert "not a result" in text


def test_the_verdict_is_written_beside_the_run(tmp_path):
    report = compare(Snapshot(), Snapshot())

    report.write(tmp_path / "damage.json")

    assert '"verdict": "clean"' in (tmp_path / "damage.json").read_text()


def test_a_leftover_after_a_teardown_is_damage():
    """The direction snapshots cannot see: our own object surviving a teardown we ran."""
    before = Snapshot()
    after = Snapshot()

    report = compare(before, after, survivors=["clusterrole/configmap-cleanup-controller"])

    assert report.verdict == DAMAGED
    assert "clusterrole/configmap-cleanup-controller" in report.survivors
    assert "left objects behind" in report.text()


def test_a_leftover_is_named_not_merely_counted():
    before = Snapshot()
    after = Snapshot()

    report = compare(before, after, survivors=["persistentvolume/pvc-abc"])

    assert "persistentvolume/pvc-abc" in report.text()
    assert report.to_dict()["survivors"] == ["persistentvolume/pvc-abc"]


def test_without_a_teardown_a_leftover_is_not_damage():
    """No teardown asked for, so the objects staying is the requested state."""
    report = compare(Snapshot(), Snapshot())

    assert report.verdict == CLEAN
    assert report.survivors == []


def test_additions_are_named_and_still_harmless():
    """A run adding its own namespaces is normal, so it is reported, not failed."""
    before = Snapshot()
    after = Snapshot(namespaces=Snapshot().namespaces | {"prometheus", "sregym"})

    report = compare(before, after)

    assert report.verdict == CLEAN
    assert "added: namespace/prometheus, namespace/sregym" in report.text()
