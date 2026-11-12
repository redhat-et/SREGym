"""Teardown removes what the harness created, and the verdict proves it did no harm.

Two properties matter more than the mechanics:

- the teardown never deletes an object it does not own: the infrastructure label and Helm release,
  or the run's ownership ledger matched by UID;
- the verdict tolerates exactly what the teardown reported, so a run cannot excuse a removal it
  did not make.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from sregym.conductor.conductor import Conductor
from sregym.service.damage_report import CLEAN, DAMAGED, OWNED
from sregym.service.ownership_ledger import OwnershipLedger


def _conductor():
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.observer_teardown")
    conductor.kubectl = MagicMock()
    conductor.prometheus = SimpleNamespace(namespace="observe", teardown=MagicMock())
    conductor.loki = SimpleNamespace(namespace="observe", teardown=MagicMock())
    conductor.mcp_server = SimpleNamespace(namespace="sregym")
    return conductor


def _cluster(*, kinds: dict[str, list[tuple[str, str]]] | None = None):
    """A kubectl whose listings return (name, uid) objects, and which removes what it is asked to delete.

    `kinds` maps a kind (`clusterrole`, `namespace`, ...) to its (name, uid) objects. Deletes are
    recorded in `.deleted` and drop the object from later listings, as the API server would.
    """
    state = {kind: list(objects) for kind, objects in (kinds or {}).items()}
    deleted: list[str] = []

    def run(command: str, timeout=None) -> str:
        parts = command.split()
        if parts[:2] == ["kubectl", "delete"]:
            kind, name = parts[2], parts[3]
            deleted.append(f"{kind} {name}")
            state[kind] = [(n, uid) for n, uid in state.get(kind, []) if n != name]
            return ""
        if parts[:2] == ["kubectl", "get"] and parts[-1] == "json":
            kind = parts[2]
            items = [{"metadata": {"name": n, "uid": uid}} for n, uid in state.get(kind, [])]
            return json.dumps({"items": items})
        return json.dumps({"items": []})

    kubectl = MagicMock()
    kubectl.exec_command_checked.side_effect = run
    kubectl.deleted = deleted
    return kubectl


def test_teardown_calls_both_releases_and_removes_the_owned_namespaces(monkeypatch):
    conductor = _conductor()
    monkeypatch.setattr(
        "sregym.conductor.conductor.delete_owned_cluster_scoped",
        lambda kubectl, log, ledger=None: ["clusterrole/mcp-server"],
    )
    conductor.kubectl.exec_command_checked.return_value = '{"items": []}'

    removed = conductor.teardown_observers()

    conductor.prometheus.teardown.assert_called_once()
    conductor.loki.teardown.assert_called_once()
    assert "namespace/observe" in removed
    assert "namespace/sregym" in removed
    assert "clusterrole/mcp-server" in removed
    commands = " ".join(str(call.args[0]) for call in conductor.kubectl.exec_command.call_args_list)
    assert "delete namespace observe" in commands
    assert "delete namespace sregym" in commands


def test_a_failing_release_teardown_does_not_stop_the_rest(monkeypatch):
    conductor = _conductor()
    conductor.prometheus.teardown.side_effect = RuntimeError("release not found")
    monkeypatch.setattr("sregym.conductor.conductor.delete_owned_cluster_scoped", lambda kubectl, log, ledger=None: [])
    conductor.kubectl.exec_command_checked.return_value = '{"items": []}'

    removed = conductor.teardown_observers()

    conductor.loki.teardown.assert_called_once()
    assert "namespace/observe" in removed


def test_a_surviving_volume_is_reported_not_finalizer_patched(monkeypatch, caplog):
    conductor = _conductor()
    monkeypatch.setattr("sregym.conductor.conductor.delete_owned_cluster_scoped", lambda kubectl, log, ledger=None: [])
    conductor.kubectl.exec_command_checked.return_value = (
        '{"items": [{"metadata": {"name": "pvc-abc"}, "spec": {"claimRef": {"namespace": "observe"}}},'
        ' {"metadata": {"name": "pvc-platform"}, "spec": {"claimRef": {"namespace": "openshift-monitoring"}}}]}'
    )
    caplog.set_level(logging.WARNING, logger="test.observer_teardown")

    conductor.teardown_observers()

    # SREGym's own volume is reported, the platform's monitoring volume is not mentioned at all.
    messages = [record.getMessage() for record in caplog.records]
    assert any("pvc-abc" in message and "still present" in message for message in messages)
    assert not any("pvc-platform" in message for message in messages)
    # A volume that will not go away is the operator's to remove: no finalizer is patched.
    commands = " ".join(str(call.args[0]) for call in conductor.kubectl.exec_command.call_args_list)
    assert "patch" not in commands
    assert "finalizer" not in commands


def test_the_verdict_tolerates_what_the_teardown_reported(monkeypatch):
    conductor = _conductor()
    conductor.cluster_state = SimpleNamespace(
        baseline=SimpleNamespace(namespaces={"default", "observe"}, cluster_roles={"a"}, cluster_uid="uid-1"),
        capture_baseline=MagicMock(
            return_value=SimpleNamespace(
                namespaces={"default"},
                cluster_roles={"a"},
                cluster_uid="uid-1",
                cluster_role_bindings=set(),
                persistent_volumes=set(),
                storage_classes=set(),
                crds=set(),
                validating_webhook_configs=set(),
                mutating_webhook_configs=set(),
                validating_admission_policies=set(),
                validating_admission_policy_bindings=set(),
                node_labels={},
                node_taints={},
                coredns_configmap_data={},
            )
        ),
    )

    report = conductor.cluster_damage_verdict(deleted_by_us={"namespace/observe"})

    assert report.verdict == OWNED
    assert report.harmless


def test_a_namespace_the_run_deploys_into_is_the_runs_to_remove(monkeypatch):
    """Cleanup removes the namespaces the run deployed into, so their loss is not damage."""
    conductor = _conductor()
    baseline = SimpleNamespace(namespaces={"default", "observe"}, cluster_roles=frozenset(), cluster_uid="uid-1")
    conductor.cluster_state = SimpleNamespace(
        baseline=baseline,
        capture_baseline=MagicMock(
            return_value=SimpleNamespace(namespaces={"default"}, cluster_roles=frozenset(), cluster_uid="uid-1")
        ),
    )

    report = conductor.cluster_damage_verdict()

    assert report.verdict == OWNED
    assert "namespace/observe" in report.owned_removed


def test_the_verdict_fails_a_removal_the_teardown_did_not_make(monkeypatch):
    conductor = _conductor()
    baseline = SimpleNamespace(
        namespaces={"default", "observe", "openshift-monitoring"}, cluster_roles={"a"}, cluster_uid="uid-1"
    )
    conductor.cluster_state = SimpleNamespace(
        baseline=baseline,
        capture_baseline=MagicMock(
            return_value=SimpleNamespace(namespaces={"default"}, cluster_roles=set(), cluster_uid="uid-1")
        ),
    )

    report = conductor.cluster_damage_verdict()

    assert report.verdict == DAMAGED
    assert "clusterrole/a" in report.removed
    assert "namespace/openshift-monitoring" in report.removed
    assert "namespace/observe" not in report.removed, "a namespace the run deploys into is its own"


def test_no_baseline_is_not_a_passing_verdict():
    conductor = _conductor()
    conductor.cluster_state = SimpleNamespace(baseline=None)

    report = conductor.cluster_damage_verdict()

    assert report.verdict == DAMAGED
    assert not report.harmless


def test_a_cluster_that_cannot_be_re_read_is_not_a_passing_verdict():
    conductor = _conductor()
    conductor.cluster_state = SimpleNamespace(
        baseline=SimpleNamespace(namespaces=set(), cluster_uid="uid-1"),
        capture_baseline=MagicMock(side_effect=RuntimeError("api down")),
    )

    report = conductor.cluster_damage_verdict()

    assert report.verdict == DAMAGED
    assert CLEAN not in (report.verdict,)


def test_what_helm_uninstalled_is_reported_as_removed_by_us(monkeypatch):
    """`helm uninstall` deletes the chart's cluster roles without reporting them.

    The damage verdict tolerates exactly what the teardown reports, so those removals must be
    in the list too -- a sweep reuses the observers, so the final run's baseline is captured
    with them already up, and an unreported removal reads as damage.
    """
    conductor = _conductor()
    owned_now = {
        "clusterrole": ["prometheus-server", "mcp-server"],
        "clusterrolebinding": ["mcp-server"],
        "mutatingwebhookconfiguration": [],
    }

    def fake_owned_names(kubectl, kind, log=None, ledger=None):
        # After the Helm uninstalls have run, nothing owned is left.
        return list(owned_now[kind]) if not kubectl.helm_uninstalls_ran else []

    monkeypatch.setattr("sregym.conductor.conductor.owned_names", fake_owned_names)
    monkeypatch.setattr(
        "sregym.conductor.conductor.delete_owned_cluster_scoped",
        lambda kubectl, log, ledger=None: ["clusterrole/mcp-server", "clusterrolebinding/mcp-server"],
    )
    conductor.kubectl.exec_command_checked.return_value = '{"items": []}'
    conductor.kubectl.helm_uninstalls_ran = False

    def prometheus_teardown():
        conductor.kubectl.helm_uninstalls_ran = True  # `helm uninstall` takes its roles with it

    conductor.prometheus.teardown.side_effect = prometheus_teardown

    removed = conductor.teardown_observers()

    assert "clusterrole/prometheus-server" in removed, "the Helm-removed role must be reported"
    assert "clusterrole/mcp-server" in removed
    assert "clusterrolebinding/mcp-server" in removed


def test_a_ledger_owned_scenario_namespace_is_removed_and_reported():
    """`platform-ops` carries no label, so the ledger's UID match is the only thing that removes it."""
    conductor = _conductor()
    ledger = OwnershipLedger()
    ledger.record("namespace", "platform-ops", "uid-ops")
    conductor.ownership_ledger = ledger
    kubectl = _cluster(kinds={"namespace": [("platform-ops", "uid-ops")]})
    conductor.kubectl = kubectl

    removed = conductor.teardown_observers()

    assert "namespace/platform-ops" in removed
    assert "namespace platform-ops" in kubectl.deleted
    assert ledger.uid_of("namespace", "platform-ops") is None


def test_a_namespace_with_the_same_name_and_another_uid_is_never_deleted():
    conductor = _conductor()
    ledger = OwnershipLedger()
    ledger.record("namespace", "platform-ops", "uid-ops")
    conductor.ownership_ledger = ledger
    kubectl = _cluster(kinds={"namespace": [("platform-ops", "uid-platform")]})
    conductor.kubectl = kubectl

    removed = conductor.teardown_observers()

    assert "namespace/platform-ops" not in removed
    assert "namespace platform-ops" not in kubectl.deleted


def test_a_ledger_owned_clusterrole_is_deleted_by_uid_and_forgotten():
    conductor = _conductor()
    ledger = OwnershipLedger()
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-cr")
    conductor.ownership_ledger = ledger
    kubectl = _cluster(kinds={"clusterrole": [("configmap-cleanup-controller", "uid-cr")]})
    conductor.kubectl = kubectl

    removed = conductor.teardown_observers()

    assert removed.count("clusterrole/configmap-cleanup-controller") == 1
    assert "clusterrole configmap-cleanup-controller" in kubectl.deleted
    assert ledger.uid_of("clusterrole", "configmap-cleanup-controller") is None


def test_the_old_ownership_label_alone_deletes_nothing_without_a_ledger_entry():
    conductor = _conductor()
    conductor.ownership_ledger = OwnershipLedger()
    kubectl = MagicMock()
    kubectl.exec_command_checked.side_effect = lambda command, timeout=None: (
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {
                            "name": "configmap-cleanup-controller",
                            "uid": "uid-cr",
                            "labels": {"sregym.io/owned": "true"},
                        }
                    }
                ]
            }
        )
        if "get clusterrole " in command
        else json.dumps({"items": []})
    )
    conductor.kubectl = kubectl

    removed = conductor.teardown_observers()

    assert not any("clusterrole" in item for item in removed)
    assert not any("delete clusterrole" in str(call.args[0]) for call in kubectl.exec_command_checked.call_args_list)
