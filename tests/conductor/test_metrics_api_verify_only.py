"""The harness verifies a metrics API it does not own, and installs its own only on kind.

OpenShift serves metrics.k8s.io from openshift-monitoring. Applying
components.yaml there replaces the platform's APIService and ClusterRoleBinding,
so the guarded path must be a wait, not an install.
"""

import logging
from unittest.mock import MagicMock, call

import pytest

from sregym import cluster_platform
from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.conductor.conductor import Conductor


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


def _conductor(monkeypatch, platform):
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.metrics_api_verify_only")
    conductor.kubectl = MagicMock()
    conductor._metrics_server_configured = MagicMock(return_value=False)
    conductor._wait_for_infrastructure_ready = MagicMock()
    return conductor


def test_openshift_waits_for_the_platform_metrics_api_instead_of_installing(monkeypatch):
    conductor = _conductor(monkeypatch, OPENSHIFT)

    conductor._ensure_metrics_api()

    conductor.kubectl.wait_for_metrics_api.assert_called_once_with()
    assert conductor.kubectl.mock_calls == [call.wait_for_metrics_api()]


def test_openshift_touches_nothing_in_kube_system(monkeypatch):
    conductor = _conductor(monkeypatch, OPENSHIFT)

    conductor._ensure_metrics_api()

    for untouched in (
        conductor.kubectl.exec_command,
        conductor.kubectl.wait_for_ready,
        conductor.kubectl.core_v1_api.patch_namespaced_service,
        conductor._metrics_server_configured,
        conductor._wait_for_infrastructure_ready,
    ):
        untouched.assert_not_called()


def test_openshift_says_why_components_yaml_is_not_applied(monkeypatch, caplog):
    conductor = _conductor(monkeypatch, OPENSHIFT)

    with caplog.at_level(logging.INFO, logger="test.metrics_api_verify_only"):
        conductor._ensure_metrics_api()

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "serves metrics.k8s.io itself" in message and "APIService" in message and "RBAC" in message
        for message in messages
    )


def test_kind_installs_and_patches_metrics_server(monkeypatch):
    conductor = _conductor(monkeypatch, KIND)
    conductor._metrics_server_configured.side_effect = [False, True]

    conductor._ensure_metrics_api()

    commands = [call.args[0] for call in conductor.kubectl.exec_command.call_args_list]
    assert any("releases/latest/download/components.yaml" in command for command in commands)
    assert any("kubectl -n kube-system patch deployment metrics-server" in command for command in commands)
    conductor.kubectl.core_v1_api.patch_namespaced_service.assert_called_once()
    conductor.kubectl.wait_for_ready.assert_called_once_with("kube-system")
    conductor._wait_for_infrastructure_ready.assert_called_once_with(
        "metrics-server", conductor._metrics_server_configured
    )
    conductor.kubectl.wait_for_metrics_api.assert_not_called()


def test_kind_reuses_a_configured_metrics_server(monkeypatch):
    conductor = _conductor(monkeypatch, KIND)
    conductor._metrics_server_configured.return_value = True

    conductor._ensure_metrics_api()

    conductor.kubectl.exec_command.assert_not_called()
    conductor.kubectl.core_v1_api.patch_namespaced_service.assert_not_called()
    conductor.kubectl.wait_for_ready.assert_called_once_with("kube-system")
    conductor.kubectl.wait_for_metrics_api.assert_not_called()
