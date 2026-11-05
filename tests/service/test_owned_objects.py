"""Ownership is a label, so a same-named foreign object is never deleted.

The rule under test: an object is SREGym's when it carries `sregym.io/owned: "true"` or a Helm
release name this harness deploys. Everything else is somebody's platform component, and the
teardown keeps it.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from sregym.service.owned_objects import (
    OWNED_LABEL,
    delete_owned_cluster_scoped,
    foreign_names,
    is_owned,
    label_owned,
    owned_names,
)


def _listing(*items):
    return json.dumps({"items": [{"metadata": metadata} for metadata in items]})


def _kubectl(listings: dict[str, str]):
    """Return a fake kubectl whose `get <kind> -o json` answers from `listings`."""
    kubectl = Mock()

    def exec_command_checked(command, timeout=None):
        for kind, payload in listings.items():
            if command.startswith(f"kubectl get {kind} "):
                return payload
        return _listing()

    kubectl.exec_command_checked.side_effect = exec_command_checked
    return kubectl


def test_the_explicit_label_marks_ownership():
    assert is_owned({"name": "mcp-server", "labels": {OWNED_LABEL: "true"}})
    assert not is_owned({"name": "mcp-server", "labels": {OWNED_LABEL: "false"}})
    assert not is_owned({"name": "mcp-server"})


def test_a_helm_release_name_marks_ownership():
    for release in ("prometheus", "loki", "promtail"):
        assert is_owned({"labels": {"app.kubernetes.io/instance": release}})
    assert not is_owned({"labels": {"app.kubernetes.io/instance": "metrics-server"}})
    assert not is_owned({"labels": {"app.kubernetes.io/instance": "kube-state-metrics"}})


def test_labelling_leaves_an_unlabelled_resource_with_no_name_alone():
    resources = [{"metadata": {"name": "mcp-server"}}, {"kind": "List"}, {"metadata": {}}]

    labelled = label_owned(resources)

    assert labelled[0]["metadata"]["labels"] == {OWNED_LABEL: "true"}
    assert labelled[1] == {"kind": "List"}
    assert labelled[2] == {"metadata": {}}


def test_a_same_named_foreign_object_is_not_deleted():
    kubectl = _kubectl(
        {
            "clusterrole": _listing(
                {"name": "mcp-server", "labels": {OWNED_LABEL: "true"}},
                {"name": "prometheus-server", "labels": {"app.kubernetes.io/instance": "prometheus"}},
                {"name": "system:metrics-server", "labels": {"app.kubernetes.io/instance": "metrics-server"}},
            ),
            "clusterrolebinding": _listing(
                {"name": "mcp-server", "labels": {OWNED_LABEL: "true"}},
            ),
        }
    )

    removed = delete_owned_cluster_scoped(kubectl, log=SimpleNamespace(info=Mock(), warning=Mock(), debug=Mock()))

    assert removed == ["clusterrole/mcp-server", "clusterrole/prometheus-server", "clusterrolebinding/mcp-server"]
    deleted = " ".join(call.args[0] for call in kubectl.exec_command_checked.call_args_list)
    assert "system:metrics-server" not in deleted


def test_the_earlier_attempt_case_is_covered():
    """An object created by an interrupted run has no baseline entry, and is still removed."""
    kubectl = _kubectl({"clusterrole": _listing({"name": "mcp-server", "labels": {OWNED_LABEL: "true"}})})

    assert owned_names(kubectl, "clusterrole") == ["mcp-server"]


def test_a_read_failure_removes_nothing_and_says_so():
    kubectl = Mock()
    kubectl.exec_command_checked.side_effect = RuntimeError("connection refused")
    log = SimpleNamespace(info=Mock(), warning=Mock(), debug=Mock())

    assert delete_owned_cluster_scoped(kubectl, log=log) == []
    assert any("Could not list" in str(call.args[0]) for call in log.warning.call_args_list)


def test_foreign_names_lists_what_the_teardown_keeps():
    kubectl = _kubectl(
        {
            "clusterrole": _listing(
                {"name": "sregym-thing", "labels": {OWNED_LABEL: "true"}},
                {"name": "openshift-something", "labels": {}},
            )
        }
    )

    assert foreign_names(kubectl, "clusterrole") == ["openshift-something"]
