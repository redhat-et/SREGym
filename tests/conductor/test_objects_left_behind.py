"""What a teardown left behind is found by ownership, not by a list of expected names.

The snapshot comparison cannot see a leftover: an object this harness created and failed to
remove is an addition, indistinguishable from the work it was supposed to do. So after a
teardown the conductor asks the API which objects still belong to this harness -- by the
infrastructure label and Helm release, or, for scenario-created objects, by the run's ownership
ledger matched by UID -- and the verdict treats those as damage. These tests pin the ways that
probe can go wrong: reporting a foreign object, trusting a bare label, inventing survivors from an
unreadable API, and missing that the observer namespaces are also the harness's.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from sregym.conductor.conductor import Conductor
from sregym.service.ownership_ledger import OwnershipLedger


def _listing(*objects: tuple[str, dict, str]) -> str:
    """A `kubectl get -o json` listing of (name, labels, uid) objects."""
    return json.dumps(
        {"items": [{"metadata": {"name": n, "labels": labels, "uid": uid}} for n, labels, uid in objects]}
    )


def _conductor(
    *,
    cluster_roles: tuple[tuple[str, dict, str], ...] = (),
    namespace_items: tuple[tuple[str, str], ...] = (),
    namespaces_present: tuple[str, ...] = (),
    volumes: tuple[dict, ...] = (),
    unreadable: bool = False,
    ledger: OwnershipLedger | None = None,
):
    """A conductor whose kubectl answers the reads the survivor probe performs.

    `namespace_items` are the (name, uid) pairs a namespace listing returns; `namespaces_present`
    are the observer namespaces a `kubectl get namespace <name>` finds.
    """
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.objects_left_behind")
    conductor.kubectl = MagicMock()
    conductor.prometheus = SimpleNamespace(namespace="observe")
    conductor.loki = SimpleNamespace(namespace="observe")
    conductor.mcp_server = SimpleNamespace(namespace="sregym")
    conductor.ownership_ledger = ledger if ledger is not None else OwnershipLedger()

    def run(command: str, timeout=None) -> str:
        if unreadable:
            raise RuntimeError("connection refused")
        if command == "kubectl get namespace -o json":
            return json.dumps({"items": [{"metadata": {"name": n, "uid": uid}} for n, uid in namespace_items]})
        if command.startswith("kubectl get namespace "):
            name = command.split()[3]
            if name not in namespaces_present:
                raise RuntimeError(f'namespaces "{name}" not found')
            return f"namespace/{name}\n"
        if command.startswith("kubectl get clusterrole "):
            return _listing(*cluster_roles)
        if command.startswith("kubectl get pv "):
            return json.dumps({"items": list(volumes)})
        return json.dumps({"items": []})

    conductor.kubectl.exec_command_checked.side_effect = run
    return conductor


def test_a_ledger_owned_clusterrole_that_survived_is_reported():
    ledger = OwnershipLedger()
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")
    conductor = _conductor(cluster_roles=(("configmap-cleanup-controller", {}, "uid-1"),), ledger=ledger)

    left = conductor._objects_left_behind()

    assert "clusterrole/configmap-cleanup-controller" in left


def test_a_same_named_clusterrole_with_another_uid_is_not_reported():
    """The ledger matches by UID: an object of the same name that replaced ours is not ours."""
    ledger = OwnershipLedger()
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")
    conductor = _conductor(cluster_roles=(("configmap-cleanup-controller", {}, "uid-2"),), ledger=ledger)

    assert conductor._objects_left_behind() == []


def test_the_old_ownership_label_alone_is_not_reported():
    """`sregym.io/owned` is no longer ownership: without a ledger entry the object is not ours."""
    conductor = _conductor(cluster_roles=(("configmap-cleanup-controller", {"sregym.io/owned": "true"}, "uid-1"),))

    assert conductor._objects_left_behind() == []


def test_a_foreign_object_with_the_same_name_is_not_reported():
    """Ownership is read from the object, so a same-named foreign object is excluded."""
    conductor = _conductor(
        cluster_roles=(("configmap-cleanup-controller", {"app.kubernetes.io/managed-by": "someone-else"}, "uid-9"),)
    )

    assert conductor._objects_left_behind() == []


def test_a_ledger_owned_namespace_still_present_is_reported():
    ledger = OwnershipLedger()
    ledger.record("namespace", "platform-ops", "uid-ops")
    conductor = _conductor(namespace_items=(("platform-ops", "uid-ops"),), ledger=ledger)

    left = conductor._objects_left_behind()

    assert "namespace/platform-ops" in left


def test_a_namespace_with_the_same_name_and_another_uid_is_not_reported():
    ledger = OwnershipLedger()
    ledger.record("namespace", "platform-ops", "uid-ops")
    conductor = _conductor(namespace_items=(("platform-ops", "uid-platform"),), ledger=ledger)

    assert "namespace/platform-ops" not in conductor._objects_left_behind()


def test_a_namespace_still_present_after_teardown_is_reported():
    conductor = _conductor(namespaces_present=("observe",))

    left = conductor._objects_left_behind()

    assert "namespace/observe" in left
    assert "namespace/sregym" not in left


def test_an_unreadable_api_yields_no_invented_survivors():
    conductor = _conductor(unreadable=True)

    assert conductor._objects_left_behind() == []


def test_a_surviving_volume_is_reported():
    conductor = _conductor(
        namespaces_present=("observe",),
        volumes=(
            {
                "metadata": {"name": "pvc-left", "labels": {"sregym.io/owned": "true"}},
                "spec": {"claimRef": {"namespace": "observe"}},
                "status": {"phase": "Released"},
            },
        ),
    )

    assert "persistentvolume/pvc-left" in conductor._objects_left_behind()


def test_the_survivor_check_is_off_unless_a_teardown_ran(monkeypatch):
    """A plain run's own additions must never be read as leftovers."""
    conductor = _conductor(namespaces_present=("observe",))
    conductor.cluster_state = SimpleNamespace(baseline=None)
    asked = []

    monkeypatch.setattr(conductor, "_objects_left_behind", lambda: asked.append(True) or ["clusterrole/x"])

    conductor.cluster_damage_verdict()
    assert asked == []

    conductor.cluster_state = MagicMock()
    conductor.cluster_state.capture_baseline.return_value = SimpleNamespace()
    monkeypatch.setattr(
        "sregym.conductor.conductor.compare", lambda *a, **kw: SimpleNamespace(text=lambda: "", harmless=True)
    )
    conductor.cluster_damage_verdict(after_teardown=True)
    assert asked == [True]
