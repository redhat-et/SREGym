import csv
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes.client.rest import ApiException

from sregym import profile
from sregym.conductor.conductor import Conductor
from sregym.service.cluster_state import ClusterBaseline
from sregym.service.helm import Helm
from sregym.service.telemetry import prometheus as prometheus_module
from sregym.service.telemetry.prometheus import Prometheus


@pytest.fixture(autouse=True)
def reset_profile(monkeypatch):
    monkeypatch.setattr(profile, "_profile", None)
    monkeypatch.delenv("SREGYM_PROFILE", raising=False)


def prometheus_server(kind="Deployment"):
    return {
        "kind": kind,
        "metadata": {"generation": 2, "labels": {"app.kubernetes.io/name": "prometheus"}},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": 2,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "currentRevision": "server-v2",
            "updateRevision": "server-v2",
        },
    }


def prometheus_pod(*, ready=True, server=True):
    return {
        "kind": "Pod",
        "metadata": {"labels": {"app.kubernetes.io/name": "prometheus" if server else "prometheus-node-exporter"}},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


@pytest.mark.parametrize("installed", ["full", "svelte"])
@pytest.mark.parametrize("requested", ["full", "svelte"])
def test_prometheus_reuses_only_the_requested_values(monkeypatch, installed, requested):
    """The fingerprint of the other profile refuses reuse; see test_telemetry_release_reuse."""
    prometheus = Prometheus()
    profile.set_profile(requested)
    monkeypatch.setattr(prometheus, "_is_prometheus_running", lambda: True)
    monkeypatch.setattr(prometheus, "_wait_for_namespace_termination", lambda: None)
    monkeypatch.setattr(prometheus, "_delete_retained_pvcs", MagicMock())
    helm = MagicMock(spec=Helm)
    helm.release_matches_requested_values.return_value = installed == requested
    monkeypatch.setattr(prometheus_module, "Helm", helm)

    prometheus.deploy()

    assert helm.install.call_count == (installed != requested)
    assert helm.uninstall.call_count == (installed != requested)
    assert helm.assert_if_deployed.call_count == (installed != requested)
    assert helm.record_values_fingerprint.call_count == (installed != requested)
    if installed != requested:
        extra_args = helm.install.call_args.kwargs.get("extra_args", [])
        assert any("values-svelte.yaml" in arg for arg in extra_args) == (requested == "svelte")
        assert helm.record_values_fingerprint.call_args.args[0] == helm.install.call_args.kwargs


@pytest.mark.parametrize("requested", ["full", "svelte"])
def test_prometheus_fresh_install_does_not_query_a_missing_release(monkeypatch, requested):
    prometheus = Prometheus()
    profile.set_profile(requested)
    monkeypatch.setattr(prometheus, "_is_prometheus_running", lambda: False)
    monkeypatch.setattr(prometheus, "_wait_for_namespace_termination", lambda: None)
    cleanup = MagicMock()
    monkeypatch.setattr(prometheus, "_delete_retained_pvcs", cleanup)
    helm = MagicMock(spec=Helm)
    monkeypatch.setattr(prometheus_module, "Helm", helm)
    prometheus.deploy()
    helm.release_matches_requested_values.assert_not_called()
    cleanup.assert_called_once_with()
    helm.install.assert_called_once()


@pytest.mark.parametrize(
    "pod_conditions,expected",
    [
        ([], False),
        ([[{"type": "Ready", "status": "True"}]], True),
        (
            [
                [{"type": "Ready", "status": "True"}],
                [{"type": "Ready", "status": "False"}],
            ],
            False,
        ),
    ],
)
def test_prometheus_reuse_requires_every_release_pod_ready(monkeypatch, pod_conditions, expected):
    prometheus = Prometheus()
    pods = [prometheus_pod(server=index == 0) for index in range(len(pod_conditions))]
    for pod, conditions in zip(pods, pod_conditions, strict=True):
        pod["status"]["conditions"] = conditions
    payload = {"items": [prometheus_server(), *pods]}
    kubectl = MagicMock()
    kubectl.exec_command.return_value = json.dumps(payload)
    monkeypatch.setattr(prometheus_module, "KubeCtl", MagicMock(return_value=kubectl))

    assert prometheus._is_prometheus_running() is expected
    assert "app.kubernetes.io/instance=prometheus" in kubectl.exec_command.call_args.args[0]


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
@pytest.mark.parametrize(
    "state", ["healthy", "missing", "zero", "old-generation", "old-replicas", "deleting", "no-server-pod"]
)
def test_prometheus_requires_its_current_server(monkeypatch, kind, state):
    server = prometheus_server(kind)
    pods = [prometheus_pod(), prometheus_pod(server=False)]
    if state == "zero":
        server["spec"]["replicas"] = 0
    elif state == "old-generation":
        server["status"]["observedGeneration"] = 1
    elif state == "old-replicas":
        server["status"]["updatedReplicas"] = 0
    elif state == "deleting":
        server["metadata"]["deletionTimestamp"] = "2026-10-26T00:00:00Z"
    elif state == "no-server-pod":
        pods = pods[1:]
    payload = {"items": [*pods, *([] if state == "missing" else [server])]}
    kubectl = MagicMock()
    kubectl.exec_command.return_value = json.dumps(payload)
    monkeypatch.setattr(prometheus_module, "KubeCtl", MagicMock(return_value=kubectl))

    assert Prometheus()._is_prometheus_running() is (state == "healthy")


def test_prometheus_rejects_an_unfinished_statefulset_revision():
    server = prometheus_server("StatefulSet")
    server["status"]["currentRevision"] = "server-v1"
    assert not Prometheus._server_rollout_complete(server)


@pytest.mark.parametrize("response", ["", "not JSON", '{"items": []}'])
def test_prometheus_does_not_reuse_missing_or_unreadable_resources(monkeypatch, response):
    kubectl = MagicMock()
    kubectl.exec_command.return_value = response
    monkeypatch.setattr(prometheus_module, "KubeCtl", MagicMock(return_value=kubectl))
    assert not Prometheus()._is_prometheus_running()


@pytest.mark.parametrize("installed_profile", ["full", "svelte"])
def test_a_profile_switch_changes_the_requested_fingerprint(installed_profile):
    """The profile is part of the requested values, so the other profile's release is not reused."""
    profile.set_profile(installed_profile)
    installed = Helm.requested_values_fingerprint(Prometheus()._requested_helm_configs())

    profile.set_profile("full" if installed_profile == "svelte" else "svelte")
    requested = Helm.requested_values_fingerprint(Prometheus()._requested_helm_configs())

    assert requested != installed


@pytest.mark.parametrize("preexisting", [False, True])
def test_svelte_preserves_original_storage_classes(preexisting):
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.profile")
    conductor.kubectl = MagicMock()
    baseline = ClusterBaseline(storage_classes={"openebs-device"} if preexisting else set())
    conductor.cluster_state = SimpleNamespace(baseline=baseline, storage_v1=MagicMock())
    conductor._trim_openebs_ndm()
    assert conductor.cluster_state.storage_v1.delete_storage_class.call_count == (not preexisting)
    if not preexisting:
        conductor.cluster_state.storage_v1.delete_storage_class.assert_called_once_with("openebs-device")
    assert conductor.kubectl.exec_command.call_count == 4
    assert all("openebs-hostpath" not in call.args[0] for call in conductor.kubectl.exec_command.call_args_list)


@pytest.mark.parametrize("status", [404, 403])
def test_storage_cleanup_ignores_only_missing_class(status):
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.profile")
    conductor.kubectl = MagicMock()
    storage = MagicMock()
    storage.delete_storage_class.side_effect = ApiException(status=status)
    conductor.cluster_state = SimpleNamespace(baseline=ClusterBaseline(), storage_v1=storage)
    if status == 404:
        conductor._trim_openebs_ndm()
        assert conductor.kubectl.exec_command.call_count == 4
    else:
        with pytest.raises(ApiException):
            conductor._trim_openebs_ndm()
        conductor.kubectl.exec_command.assert_not_called()


def test_missing_baseline_does_not_remove_storage_or_workloads():
    conductor = Conductor.__new__(Conductor)
    conductor.kubectl = MagicMock()
    conductor.cluster_state = SimpleNamespace(baseline=None, storage_v1=MagicMock())
    with pytest.raises(RuntimeError, match="baseline"):
        conductor._trim_openebs_ndm()
    conductor.cluster_state.storage_v1.delete_storage_class.assert_not_called()
    conductor.kubectl.exec_command.assert_not_called()


@pytest.mark.parametrize("selected_profile", ["full", "svelte"])
@pytest.mark.parametrize("deploy_failed", [False, True])
def test_driver_records_profile_in_result_rows(tmp_path, monkeypatch, selected_profile, deploy_failed):
    spec = importlib.util.spec_from_file_location(
        "profile_benchmark_test", Path(__file__).resolve().parents[1] / "main.py"
    )
    benchmark = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, benchmark)
    spec.loader.exec_module(benchmark)
    profile.set_profile(selected_profile)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark, "list_agents", lambda **kwargs: {"demo": {}})
    monkeypatch.setattr(benchmark.asyncio, "sleep", AsyncMock())
    launcher = MagicMock()
    launcher._procs = {}
    launcher._container_runner = None
    launcher.internet_policy_result.return_value = {}
    monkeypatch.setattr(benchmark, "LAUNCHER", launcher)
    run = MagicMock()
    run.finalize_and_publish.return_value = None
    monkeypatch.setattr(benchmark.RunArtifacts, "create", lambda **kwargs: run)
    conductor = MagicMock()
    conductor.problem = None
    conductor.problems.get_problem_ids.return_value = ["example"]
    conductor.start_problem = AsyncMock(side_effect=RuntimeError("deploy failed") if deploy_failed else None)
    conductor.wait_for_submission_work = AsyncMock()
    conductor.close_submissions.return_value = False
    conductor.submission_stage = "done"
    conductor.stage_sequence = []
    conductor.phases = None
    conductor.results = {"run_status": "complete", "Diagnosis": {"success": True}, "Mitigation": {"success": True}}
    conductor.finalize_attempt_status.return_value = "complete"

    results = benchmark.driver_loop(conductor, problem_selection=["example"], agent_to_run="demo")

    row = results[0]["demo"][0]
    assert row["deployment_profile"] == selected_profile
    assert bool(row.get("deploy_failed")) == deploy_failed
    csv_files = list(tmp_path.rglob("*.csv"))
    assert csv_files
    for path in csv_files:
        with path.open() as file:
            assert list(csv.DictReader(file))[0]["deployment_profile"] == selected_profile
    if not deploy_failed:
        assert run.finalize_and_publish.call_args.kwargs["snapshot"]["deployment_profile"] == selected_profile
