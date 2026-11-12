"""Ledger-recorded objects left on an OpenShift cluster are removed by UID, and never infrastructure.

`delete_ledger_objects` deletes only what the run's ledger recorded under the same UID that is live
now. An object with the infrastructure or Helm label is never deleted. On OpenShift the cleanup runs
after the app cleanup and before reconcile; kind does not run it.
"""

import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.rest import ApiException

from sregym import cluster_platform
from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.conductor import conductor as conductor_module
from sregym.conductor.conductor import Conductor
from sregym.service import owned_objects
from sregym.service.cluster_state import RECONCILE_ON
from sregym.service.owned_objects import delete_ledger_objects
from sregym.service.ownership_ledger import OwnershipLedger


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


@pytest.fixture(autouse=True)
def typed_clients(monkeypatch):
    """Every typed client is the fake cluster's API of the kubectl under test, never a real one."""
    monkeypatch.setattr(owned_objects, "_typed_api", lambda kubectl, api_name: kubectl.cluster.api)


def _cluster(
    objects: dict[str, list[dict]],
    *,
    fail_delete: set[str] | None = None,
    fail_list: set[str] | None = None,
    replace_after_list: dict[str, str] | None = None,
):
    """A kubectl whose listings return `objects[kind]` and whose typed deletes remove them.

    A typed delete whose precondition UID differs from the live object's answers 409, as the API
    server does; a delete of an absent object answers 404. `fail_delete` names `kind/name` whose
    delete answers 500; `fail_list` names kinds whose listing raises. `replace_after_list` maps
    `kind/name` to a new UID applied right after that kind is listed, so the object changes between
    the listing and the delete. The fake records every typed call in `kubectl.cluster.calls`.
    """
    state = {kind: list(items) for kind, items in objects.items()}
    deleted: list[str] = []
    calls: list[tuple[str, str, str | None]] = []
    fail_delete = fail_delete or set()
    fail_list = fail_list or set()
    replace = dict(replace_after_list or {})

    def run(command: str, timeout=None) -> str:
        parts = command.split()
        if parts[:2] != ["kubectl", "get"]:
            raise AssertionError(f"unexpected command: {command}")
        kind = parts[2]
        if kind in fail_list:
            raise RuntimeError(f"listing {kind} failed")
        listing = json.dumps({"items": state.get(kind, [])})
        for key in [k for k in replace if k.split("/")[0] == kind]:
            target = key.split("/")[1]
            for item in state.get(kind, []):
                if item["metadata"]["name"] == target:
                    item["metadata"]["uid"] = replace.pop(key)
        return listing

    def delete(kind: str, name: str, body) -> None:
        uid = body.preconditions.uid if body.preconditions is not None else None
        calls.append((kind, name, uid))
        named = [item for item in state.get(kind, []) if item["metadata"].get("name") == name]
        if not named:
            raise ApiException(status=404)
        if f"{kind}/{name}" in fail_delete:
            raise ApiException(status=500)
        if uid is not None and named[0]["metadata"].get("uid") != uid:
            raise ApiException(status=409)
        state[kind] = [item for item in state.get(kind, []) if item["metadata"].get("name") != name]
        deleted.append(f"{kind}/{name}")

    class _Api:
        def delete_namespace(self, name, body):
            delete("namespace", name, body)

        def delete_cluster_role(self, name, body):
            delete("clusterrole", name, body)

        def delete_cluster_role_binding(self, name, body):
            delete("clusterrolebinding", name, body)

        def delete_mutating_webhook_configuration(self, name, body):
            delete("mutatingwebhookconfiguration", name, body)

    kubectl = MagicMock()
    kubectl.exec_command_checked.side_effect = run
    kubectl.deleted = deleted
    kubectl.cluster = SimpleNamespace(api=_Api(), calls=calls, state=state)
    return kubectl


def _item(name: str, uid: str, labels: dict | None = None) -> dict:
    metadata = {"name": name, "uid": uid}
    if labels:
        metadata["labels"] = labels
    return {"metadata": metadata}


def _ledger(tmp_path) -> OwnershipLedger:
    return OwnershipLedger(tmp_path / "owned_objects.json")


def test_a_recorded_object_with_the_same_uid_is_deleted_and_its_entry_forgotten(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")
    kubectl = _cluster({"clusterrole": [_item("configmap-cleanup-controller", "uid-1")]})

    removed = delete_ledger_objects(kubectl, ledger)

    assert removed == ["clusterrole/configmap-cleanup-controller"]
    assert kubectl.deleted == ["clusterrole/configmap-cleanup-controller"]
    assert ledger.uid_of("clusterrole", "configmap-cleanup-controller") is None


def test_a_same_named_object_with_another_uid_is_not_deleted(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")
    # The recorded object is gone and a platform component now holds the name.
    kubectl = _cluster({"clusterrole": [_item("configmap-cleanup-controller", "uid-platform")]})

    removed = delete_ledger_objects(kubectl, ledger)

    assert removed == []
    assert kubectl.deleted == []
    # The recorded object no longer exists, so its entry is dropped; the live one is not ours.
    assert ledger.uid_of("clusterrole", "configmap-cleanup-controller") is None


def test_an_object_carrying_the_infrastructure_label_is_never_deleted_even_when_recorded(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("clusterrole", "shared-role", "uid-1")
    kubectl = _cluster(
        {"clusterrole": [_item("shared-role", "uid-1", labels={"app.kubernetes.io/instance": "observability-stack"})]}
    )

    removed = delete_ledger_objects(kubectl, ledger)

    assert removed == []
    assert kubectl.deleted == []
    assert ledger.uid_of("clusterrole", "shared-role") == "uid-1"


def test_an_object_carrying_a_helm_release_label_is_never_deleted_even_when_recorded(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("clusterrole", "loki-role", "uid-1")
    metadata = {
        "name": "loki-role",
        "uid": "uid-1",
        "labels": {"app.kubernetes.io/instance": "loki"},
        "annotations": {"meta.helm.sh/release-name": "loki", "meta.helm.sh/release-namespace": "observe"},
    }
    kubectl = _cluster({"clusterrole": [{"metadata": metadata}]})

    assert delete_ledger_objects(kubectl, ledger) == []
    assert kubectl.deleted == []


def test_a_recorded_namespace_with_the_same_uid_is_deleted(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("namespace", "platform-ops", "uid-ns")
    kubectl = _cluster({"namespace": [_item("platform-ops", "uid-ns")]})

    assert delete_ledger_objects(kubectl, ledger) == ["namespace/platform-ops"]
    assert kubectl.deleted == ["namespace/platform-ops"]


def test_every_recorded_kind_is_considered(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("mutatingwebhookconfiguration", "gatekeeper-mutating-webhook-configuration", "uid-w")
    ledger.record("clusterrolebinding", "configmap-cleanup-controller", "uid-b")
    kubectl = _cluster(
        {
            "mutatingwebhookconfiguration": [_item("gatekeeper-mutating-webhook-configuration", "uid-w")],
            "clusterrolebinding": [_item("configmap-cleanup-controller", "uid-b")],
        }
    )

    removed = delete_ledger_objects(kubectl, ledger)

    assert sorted(removed) == [
        "clusterrolebinding/configmap-cleanup-controller",
        "mutatingwebhookconfiguration/gatekeeper-mutating-webhook-configuration",
    ]


def test_an_entry_whose_object_is_gone_is_forgotten(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("clusterrole", "already-gone", "uid-1")
    kubectl = _cluster({"clusterrole": []})

    assert delete_ledger_objects(kubectl, ledger) == []
    assert kubectl.deleted == []
    assert ledger.uid_of("clusterrole", "already-gone") is None


def test_a_failed_delete_keeps_its_entry_for_the_next_try(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("clusterrole", "stuck", "uid-1")
    kubectl = _cluster({"clusterrole": [_item("stuck", "uid-1")]}, fail_delete={"clusterrole/stuck"})

    assert delete_ledger_objects(kubectl, ledger) == []
    assert ledger.uid_of("clusterrole", "stuck") == "uid-1"


def test_a_failed_listing_deletes_nothing_and_forgets_nothing(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("clusterrole", "unseen", "uid-1")
    kubectl = _cluster({"clusterrole": [_item("unseen", "uid-1")]}, fail_list={"clusterrole"})

    assert delete_ledger_objects(kubectl, ledger) == []
    assert kubectl.deleted == []
    assert ledger.uid_of("clusterrole", "unseen") == "uid-1"


def test_an_empty_ledger_lists_nothing(tmp_path):
    kubectl = _cluster({})

    assert delete_ledger_objects(kubectl, _ledger(tmp_path)) == []
    kubectl.exec_command_checked.assert_not_called()


def test_every_delete_carries_the_recorded_uid_as_its_precondition(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("clusterrole", "role", "uid-r")
    ledger.record("namespace", "platform-ops", "uid-ns")
    kubectl = _cluster({"clusterrole": [_item("role", "uid-r")], "namespace": [_item("platform-ops", "uid-ns")]})

    delete_ledger_objects(kubectl, ledger)

    assert sorted(kubectl.cluster.calls) == [
        ("clusterrole", "role", "uid-r"),
        ("namespace", "platform-ops", "uid-ns"),
    ]


def test_an_object_replaced_between_the_listing_and_the_delete_is_refused_and_left(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")
    # Listed as ours, then a platform component takes the name before the delete lands.
    kubectl = _cluster(
        {"clusterrole": [_item("configmap-cleanup-controller", "uid-1")]},
        replace_after_list={"clusterrole/configmap-cleanup-controller": "uid-platform"},
    )

    assert delete_ledger_objects(kubectl, ledger) == []
    assert kubectl.cluster.calls == [("clusterrole", "configmap-cleanup-controller", "uid-1")]
    assert kubectl.deleted == []
    # The API server refused the delete as foreign, so the entry is forgotten and the object kept.
    assert ledger.uid_of("clusterrole", "configmap-cleanup-controller") is None
    assert [item["metadata"]["uid"] for item in kubectl.cluster.state["clusterrole"]] == ["uid-platform"]


def test_a_nameless_item_is_skipped_not_a_key_error(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("clusterrole", "role", "uid-1")
    kubectl = _cluster(
        {
            "clusterrole": [
                {"metadata": {"uid": "uid-nameless"}},
                {"metadata": {}},
                _item("role", "uid-1"),
            ]
        }
    )

    assert delete_ledger_objects(kubectl, ledger) == ["clusterrole/role"]
    assert kubectl.cluster.calls == [("clusterrole", "role", "uid-1")]


# The conductor's ordering and platform gate.


def _conductor(monkeypatch, tmp_path, platform, calls, *, reconcile=RECONCILE_ON):
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.ledger_cleanup")
    conductor.config = SimpleNamespace(enable_noise=False, deploy_loki=True)
    conductor.reconcile = reconcile
    conductor.kubectl = MagicMock()
    conductor.ownership_ledger = _ledger(tmp_path)
    conductor.problem = SimpleNamespace(
        recover_fault=lambda: calls.append("recover_fault"),
        app=SimpleNamespace(cleanup=lambda: calls.append("app_cleanup")),
    )
    conductor.cluster_state = MagicMock()
    conductor.cluster_state.reconcile_to_baseline.side_effect = lambda: calls.append("reconcile_to_baseline") or {}
    conductor._baseline_captured = True
    conductor._baseline_usable = True
    conductor.results = {}
    conductor.submission_stage = "tearing_down"
    conductor._submission_lock = threading.RLock()
    conductor._submission_generation = 1
    conductor._aborted_submission_generations = set()
    conductor._accepting_submissions = False
    conductor._attempt_closed = True
    return conductor


def test_openshift_runs_the_ledger_cleanup_after_the_app_and_before_reconcile(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(
        conductor_module,
        "delete_ledger_objects",
        lambda kubectl, ledger, log=None: calls.append("ledger_cleanup") or ["clusterrole/x"],
    )
    conductor = _conductor(monkeypatch, tmp_path, OPENSHIFT, calls)

    conductor._cleanup_sync()

    assert calls == ["recover_fault", "app_cleanup", "ledger_cleanup", "reconcile_to_baseline"]
    assert "cleanup_error" not in conductor.results


def test_kind_never_runs_the_ledger_cleanup(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(
        conductor_module,
        "delete_ledger_objects",
        lambda kubectl, ledger, log=None: calls.append("ledger_cleanup") or [],
    )
    conductor = _conductor(monkeypatch, tmp_path, KIND, calls)

    conductor._cleanup_sync()

    assert "ledger_cleanup" not in calls
    assert calls == ["recover_fault", "app_cleanup", "reconcile_to_baseline"]


def test_a_failing_ledger_cleanup_is_recorded_and_cleanup_still_reaches_done(monkeypatch, tmp_path):
    calls: list[str] = []

    def boom(kubectl, ledger, log=None):
        raise RuntimeError("API unreachable")

    monkeypatch.setattr(conductor_module, "delete_ledger_objects", boom)
    conductor = _conductor(monkeypatch, tmp_path, OPENSHIFT, calls)

    conductor._cleanup_sync()

    assert conductor.results["cleanup_failed"] is True
    assert "ledger_cleanup: RuntimeError: API unreachable" in conductor.results["cleanup_error"]
    assert "reconcile_to_baseline" in calls
    assert conductor.submission_stage == "done"
