from unittest.mock import Mock

import pytest
from kubernetes import client

from sregym import cluster_platform
from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.service.kubectl import KubeCtl


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


def _node(name):
    return client.V1Node(
        metadata=client.V1ObjectMeta(name=name, labels={"kubernetes.io/os": "linux"}),
        spec=client.V1NodeSpec(),
    )


@pytest.fixture
def networking(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("sregym.service.kubectl.time.monotonic", lambda: now[0])
    monkeypatch.setattr("sregym.service.kubectl.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    subject = KubeCtl.__new__(KubeCtl)
    api = subject.core_v1_api = Mock()
    api.list_node.return_value = client.V1NodeList(items=[_node("worker-a")])
    api.list_namespaced_event.return_value = client.CoreV1EventList(items=[])
    pods = {}

    def create(namespace, body, **kwargs):
        pods[body.metadata.name] = body
        body.metadata.uid = f"uid-{body.metadata.name}"
        body.status = client.V1PodStatus(
            phase="Running", pod_ip="10.0.0.7", conditions=[client.V1PodCondition(type="Ready", status="True")]
        )
        return body

    api.create_namespaced_pod.side_effect = create
    api.read_namespaced_pod.side_effect = lambda name, namespace, **kwargs: pods[name]
    monkeypatch.delenv("SREGYM_NETWORK_PROBE_IMAGE", raising=False)
    return subject, api


def _created_probe(api):
    assert api.create_namespaced_pod.call_count == 1
    return api.create_namespaced_pod.call_args.kwargs["body"]


def test_probe_pod_and_container_have_no_security_context_on_kind(networking):
    # Kind keeps main's bare probe spec.
    subject, api = networking
    assert cluster_platform.get_platform() == KIND

    subject.assert_pod_networking(timeout=1)

    pod = _created_probe(api)
    assert pod.spec.security_context is None
    assert pod.spec.containers[0].security_context is None


def test_probe_pod_carries_a_restricted_pod_security_context_on_openshift(networking, monkeypatch):
    monkeypatch.setenv("SREGYM_PLATFORM", OPENSHIFT)
    subject, api = networking

    subject.assert_pod_networking(timeout=1)

    security_context = _created_probe(api).spec.security_context
    assert security_context.run_as_non_root is True
    assert security_context.seccomp_profile.type == "RuntimeDefault"


def test_probe_container_lets_restricted_admission_assign_the_uid(networking, monkeypatch):
    monkeypatch.setenv("SREGYM_PLATFORM", OPENSHIFT)
    subject, api = networking

    subject.assert_pod_networking(timeout=1)

    pod = _created_probe(api)
    # restricted-v2 uses MustRunAsRange against the namespace's uid range and refuses a
    # pod that pins a uid, so the SCC assigns one instead.
    assert pod.spec.containers[0].security_context.run_as_user is None
    assert pod.spec.security_context.run_as_non_root is True
    assert pod.spec.containers[0].security_context.run_as_non_root is True
    assert pod.spec.containers[0].security_context.seccomp_profile.type == "RuntimeDefault"
    assert pod.spec.security_context.seccomp_profile.type == "RuntimeDefault"
    assert pod.spec.containers[0].security_context.capabilities.drop == ["ALL"]
    assert pod.spec.containers[0].security_context.allow_privilege_escalation is False


def test_probe_manifest_survives_the_hardening_unchanged(networking):
    subject, api = networking

    subject.assert_pod_networking(timeout=1)

    pod = _created_probe(api)
    container = pod.spec.containers[0]
    assert pod.metadata.labels == {"app": "sregym-cni-probe"}
    assert pod.spec.restart_policy == "Never"
    assert pod.spec.automount_service_account_token is False
    assert pod.spec.node_name == "worker-a"
    assert [(toleration.operator, toleration.effect) for toleration in pod.spec.tolerations] == [("Exists", None)]
    assert container.image == "registry.k8s.io/pause:3.10"
    assert container.image_pull_policy == "IfNotPresent"


NAMESPACED_CALLS = ("create_namespaced_pod", "read_namespaced_pod", "delete_namespaced_pod", "list_namespaced_event")


def _namespaced_calls(api):
    calls = [call for call in api.method_calls if call[0] in NAMESPACED_CALLS]
    assert calls
    return calls


def test_probe_namespace_defaults_to_kube_system_on_every_call(networking):
    subject, api = networking

    subject.assert_pod_networking(timeout=1)

    for call in _namespaced_calls(api):
        assert call.kwargs["namespace"] == "kube-system"


def test_probe_namespace_argument_reaches_every_call(networking):
    subject, api = networking

    def read(name, namespace, **kwargs):
        return client.V1Pod(
            metadata=client.V1ObjectMeta(name=name, uid="uid-1"),
            spec=client.V1PodSpec(containers=[]),
            status=client.V1PodStatus(phase="Pending"),
        )

    api.read_namespaced_pod.side_effect = read
    with pytest.raises(RuntimeError, match="did not reach Ready"):
        subject.assert_pod_networking(namespace="sregym-probe", timeout=1)

    calls = _namespaced_calls(api)
    assert "list_namespaced_event" in [call[0] for call in calls]
    for call in calls:
        assert call.kwargs["namespace"] == "sregym-probe"
