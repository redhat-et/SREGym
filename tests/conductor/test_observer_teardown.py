"""Teardown removes what the harness created, and the verdict proves it did no harm.

Two properties matter more than the mechanics:

- the teardown never deletes an object it does not own, which is what the ownership label buys;
- the verdict tolerates exactly what the teardown reported, so a run cannot excuse a removal it
  did not make.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from sregym.conductor.conductor import Conductor
from sregym.service.damage_report import CLEAN, DAMAGED, OWNED


def _conductor():
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.observer_teardown")
    conductor.kubectl = MagicMock()
    conductor.prometheus = SimpleNamespace(namespace="observe", teardown=MagicMock())
    conductor.loki = SimpleNamespace(namespace="observe", teardown=MagicMock())
    conductor.mcp_server = SimpleNamespace(namespace="sregym")
    return conductor


def test_teardown_calls_both_releases_and_removes_the_owned_namespaces(monkeypatch):
    conductor = _conductor()
    monkeypatch.setattr(
        "sregym.conductor.conductor.delete_owned_cluster_scoped", lambda kubectl, log: ["clusterrole/mcp-server"]
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
    monkeypatch.setattr("sregym.conductor.conductor.delete_owned_cluster_scoped", lambda kubectl, log: [])
    conductor.kubectl.exec_command_checked.return_value = '{"items": []}'

    removed = conductor.teardown_observers()

    conductor.loki.teardown.assert_called_once()
    assert "namespace/observe" in removed


def test_a_surviving_volume_is_reported_not_finalizer_patched(monkeypatch, caplog):
    conductor = _conductor()
    monkeypatch.setattr("sregym.conductor.conductor.delete_owned_cluster_scoped", lambda kubectl, log: [])
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

    def fake_owned_names(kubectl, kind, log=None):
        # After the Helm uninstalls have run, nothing owned is left.
        return list(owned_now[kind]) if not kubectl.helm_uninstalls_ran else []

    monkeypatch.setattr("sregym.conductor.conductor.owned_names", fake_owned_names)
    monkeypatch.setattr(
        "sregym.conductor.conductor.delete_owned_cluster_scoped",
        lambda kubectl, log: ["clusterrole/mcp-server", "clusterrolebinding/mcp-server"],
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
