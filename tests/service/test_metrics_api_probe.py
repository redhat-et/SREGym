import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from kubernetes.client.exceptions import ApiException

from sregym.service.kubectl import KubeCtl

NOT_SERVED = "Error from server (NotFound): the server could not find the requested resource"


def _kubectl(*bodies):
    subject = KubeCtl.__new__(KubeCtl)
    exec_command = Mock(side_effect=list(bodies) if bodies else None)
    if not bodies:
        exec_command.return_value = NOT_SERVED
    subject.exec_command = exec_command
    return subject, exec_command


def _resource_list(kind, items=None):
    body = {"kind": kind, "apiVersion": "v1"}
    if items is not None:
        body["items"] = items
    return json.dumps(body)


def test_metrics_api_answers_when_nodes_report_usage():
    subject, exec_command = _kubectl(_resource_list("NodeMetricsList", [{"metadata": {"name": "worker-a"}}]))

    assert subject.metrics_api_answers() is True
    assert "--raw /apis/metrics.k8s.io/v1beta1/nodes" in exec_command.call_args.args[0]


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({}),
        NOT_SERVED,
        json.dumps({"kind": "NodeMetricsList", "items": []}),
        json.dumps({"kind": "Status", "status": "Failure"}),
    ],
)
def test_metrics_api_does_not_answer_for_anything_else(body):
    subject, _ = _kubectl(body)

    assert subject.metrics_api_answers() is False


def test_wait_for_metrics_api_returns_once_the_cluster_answers():
    subject, exec_command = _kubectl(NOT_SERVED, _resource_list("NodeMetricsList", [{"metadata": {}}]), NOT_SERVED)

    subject.wait_for_metrics_api(timeout=30, sleep=0)

    assert exec_command.call_count == 2


def test_wait_for_metrics_api_names_cluster_monitoring_when_the_deadline_passes(monkeypatch):
    subject, _ = _kubectl()
    now = [0.0]
    # Each clock read steps past the deadline, so the poll path runs without real sleeping.
    monkeypatch.setattr("sregym.service.kubectl.time.monotonic", lambda: now.__setitem__(0, now[0] + 0.5) or now[0])

    with pytest.raises(RuntimeError, match="cluster-monitoring") as error:
        subject.wait_for_metrics_api(timeout=1, sleep=0)

    assert "metrics.k8s.io/v1beta1" in str(error.value)


def test_wait_for_metrics_api_keeps_the_clusters_last_answer(monkeypatch):
    # A 403 for a restricted identity and an APIService that is still starting are
    # different problems, and both look like "no metrics API" when reduced to a bool.
    forbidden = (
        "Error from server (Forbidden): nodes.metrics.k8s.io is forbidden: User "
        '"system:serviceaccount:sregym:runner" cannot list resource "nodes"'
    )
    subject, _ = _kubectl(forbidden)
    now = [0.0]
    monkeypatch.setattr("sregym.service.kubectl.time.monotonic", lambda: now.__setitem__(0, now[0] + 0.5) or now[0])

    with pytest.raises(RuntimeError) as error:
        subject.wait_for_metrics_api(timeout=1, sleep=0)

    assert "nodes.metrics.k8s.io is forbidden" in str(error.value)
    assert "Last answer from the cluster" in str(error.value)


def test_wait_for_metrics_api_keeps_the_last_answer_on_one_line(monkeypatch):
    subject, _ = _kubectl("Error from server (ServiceUnavailable):\n  the server is currently\n\n" + "x" * 5000)
    now = [0.0]
    monkeypatch.setattr("sregym.service.kubectl.time.monotonic", lambda: now.__setitem__(0, now[0] + 0.5) or now[0])

    with pytest.raises(RuntimeError) as error:
        subject.wait_for_metrics_api(timeout=1, sleep=0)

    message = str(error.value)
    assert "\n" not in message
    assert "the server is currently" in message
    assert "x" * 400 not in message


def _discovery(*names):
    subject, _exec_command = _kubectl()
    subject.apis_api = MagicMock()
    subject.apis_api.get_api_versions.return_value = SimpleNamespace(
        groups=[SimpleNamespace(name=name) for name in names]
    )
    return subject


def test_openshift_api_present_when_both_groups_are_served():
    subject = _discovery("apps", "project.openshift.io", "route.openshift.io")

    assert subject.openshift_api_present() is True
    subject.apis_api.get_api_versions.assert_called_once_with()


def test_openshift_api_absent_when_a_group_is_missing():
    # A kind cluster serves neither group; that is an answer, not an error.
    subject = _discovery("apps", "batch")

    assert subject.openshift_api_present() is False


def test_openshift_api_refusal_propagates():
    # A restricted identity that may not read discovery must not be read as "not OpenShift".
    subject, _exec_command = _kubectl()
    subject.apis_api = MagicMock()
    subject.apis_api.get_api_versions.side_effect = ApiException(status=403, reason="Forbidden")

    with pytest.raises(ApiException):
        subject.openshift_api_present()
