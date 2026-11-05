"""What a teardown left behind is found by ownership, not by a list of expected names.

The snapshot comparison cannot see a leftover: an object this harness created and failed to
remove is an addition, indistinguishable from the work it was supposed to do. So after a
teardown the conductor asks the API which objects still carry this harness's ownership label,
and the verdict treats those as damage. These tests pin the three ways that probe can go wrong:
reporting a foreign object, inventing survivors from an unreadable API, and missing that the
observer namespaces are also the harness's.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from sregym.conductor.conductor import Conductor


def _listing(*objects: tuple[str, dict]) -> str:
    return json.dumps({"items": [{"metadata": {"name": n, "labels": labels}} for n, labels in objects]})


def _conductor(
    *,
    cluster_roles: tuple[tuple[str, dict], ...] = (),
    namespaces_present: tuple[str, ...] = (),
    volumes: tuple[dict, ...] = (),
    unreadable: bool = False,
):
    """A conductor whose kubectl answers the four reads the survivor probe performs."""
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.objects_left_behind")
    conductor.kubectl = MagicMock()
    conductor.prometheus = SimpleNamespace(namespace="observe")
    conductor.loki = SimpleNamespace(namespace="observe")
    conductor.mcp_server = SimpleNamespace(namespace="sregym")

    def run(command: str, timeout=None) -> str:
        if unreadable:
            raise RuntimeError("connection refused")
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


def test_an_owned_clusterrole_that_survived_is_reported():
    conductor = _conductor(cluster_roles=(("configmap-cleanup-controller", {"sregym.io/owned": "true"}),))

    left = conductor._objects_left_behind()

    assert "clusterrole/configmap-cleanup-controller" in left


def test_a_foreign_object_with_the_same_name_is_not_reported():
    """Ownership is read from the object, so a same-named foreign object is excluded."""
    conductor = _conductor(
        cluster_roles=(("configmap-cleanup-controller", {"app.kubernetes.io/managed-by": "someone-else"}),)
    )

    assert conductor._objects_left_behind() == []


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
