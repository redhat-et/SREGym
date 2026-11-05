"""The API proxy hides the platform's own namespaces, and refuses to start blind.

A managed cluster's namespaces are not the harness's business, and they are not
knowable ahead of time, so they are read from the cluster. An unreadable list is
an error: starting the proxy with an unknown hidden set would show the agent
namespaces it was never meant to see.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sregym import cluster_platform
from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.conductor.conductor import Conductor
from sregym.service.k8s_proxy import HIDDEN_NAMESPACES

PLATFORM_NAMESPACES = ["openshift-monitoring", "openshift-dns", "kube-system", "kube-public"]
APPLICATION_NAMESPACE = "astronomy-shop"
HIDING_LOG = f"[PROXY] Hiding {len(PLATFORM_NAMESPACES)} openshift*/kube-* namespaces from the agent"


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


def _namespace(name):
    return SimpleNamespace(metadata=SimpleNamespace(name=name))


def _conductor(monkeypatch, platform, namespaces):
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.proxy_hidden_openshift_namespaces")
    conductor.config = SimpleNamespace(internet_policy=SimpleNamespace(is_filtered=False))
    conductor.cluster_egress = MagicMock()
    conductor.k8s_proxy = MagicMock()
    conductor.k8s_proxy.hidden_namespaces = set(HIDDEN_NAMESPACES)
    conductor.k8s_proxy.generate_agent_kubeconfig.return_value = "/tmp/agent-kubeconfig"
    conductor._agent_kubeconfig_path = None
    conductor.kubectl = MagicMock()
    if isinstance(namespaces, Exception):
        conductor.kubectl.list_namespaces.side_effect = namespaces
    else:
        conductor.kubectl.list_namespaces.return_value = SimpleNamespace(
            items=[_namespace(name) for name in namespaces]
        )
    return conductor


def test_openshift_hides_the_platform_namespaces_alongside_the_static_ones(monkeypatch, caplog):
    conductor = _conductor(monkeypatch, OPENSHIFT, [*PLATFORM_NAMESPACES, APPLICATION_NAMESPACE])
    # The proxy copies the attribute into closure locals in start(), so only the set
    # visible at that moment is what the running proxy hides.
    hidden_at_start = []
    conductor.k8s_proxy.start.side_effect = lambda: hidden_at_start.append(set(conductor.k8s_proxy.hidden_namespaces))

    with caplog.at_level(logging.INFO, logger="test.proxy_hidden_openshift_namespaces"):
        conductor.start_k8s_proxy()

    assert hidden_at_start == [set(HIDDEN_NAMESPACES) | set(PLATFORM_NAMESPACES)]
    assert APPLICATION_NAMESPACE not in hidden_at_start[0]
    conductor.k8s_proxy.start.assert_called_once_with()
    assert HIDING_LOG in [record.getMessage() for record in caplog.records]


def test_openshift_without_a_readable_namespace_list_refuses_to_start_the_proxy(monkeypatch):
    conductor = _conductor(monkeypatch, OPENSHIFT, [])

    with pytest.raises(RuntimeError, match="proxy will not start with an unknown hidden set"):
        conductor.start_k8s_proxy()

    conductor.k8s_proxy.start.assert_not_called()
    assert conductor.k8s_proxy.hidden_namespaces == set(HIDDEN_NAMESPACES)
    assert conductor.cluster_egress.stop.called


def test_openshift_with_a_failing_namespace_read_refuses_to_start_the_proxy(monkeypatch):
    conductor = _conductor(monkeypatch, OPENSHIFT, RuntimeError("connection refused"))

    with pytest.raises(RuntimeError, match="Could not read the cluster's namespaces") as error:
        conductor.start_k8s_proxy()

    assert "connection refused" in str(error.value)
    assert "unknown hidden set" in str(error.value)
    conductor.k8s_proxy.start.assert_not_called()


def test_kind_leaves_the_hidden_set_untouched(monkeypatch, caplog):
    conductor = _conductor(monkeypatch, KIND, [])

    with caplog.at_level(logging.INFO, logger="test.proxy_hidden_openshift_namespaces"):
        conductor.start_k8s_proxy()

    conductor.kubectl.list_namespaces.assert_not_called()
    assert conductor.k8s_proxy.hidden_namespaces == set(HIDDEN_NAMESPACES)
    conductor.k8s_proxy.start.assert_called_once_with()
    assert not [record for record in caplog.records if record.getMessage().startswith("[PROXY] Hiding")]
