"""Ownership is the infrastructure label, a Helm release, or a ledger UID match.

The rule under test: an object is SREGym's when it carries `app.kubernetes.io/instance:
observability-stack`, when it is a Helm release this harness deploys (label and release
annotations both match), or when the run's ownership ledger recorded its exact name and UID.
Everything else is somebody's platform component, and the teardown keeps it. The label
`sregym.io/owned` is no longer a marker of anything.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client import AdmissionregistrationV1Api, RbacAuthorizationV1Api
from kubernetes.client.rest import ApiException

from sregym.service import owned_objects
from sregym.service.owned_objects import (
    INFRA_LABEL,
    INFRA_VALUE,
    delete_ledger_namespaces,
    delete_owned_cluster_scoped,
    foreign_names,
    is_owned,
    label_infrastructure,
    ledger_namespaces_present,
    owned_names,
)
from sregym.service.ownership_ledger import OwnershipLedger


def _listing(*items):
    return json.dumps({"items": [{"metadata": metadata} for metadata in items]})


class FakeTypedApi:
    """Stands in for every typed client. Each delete is recorded as (method, name, precondition uid).

    A name in `refuse` answers with that HTTP status, as the API server does for a precondition
    that no longer matches (409) or an object already gone (404).
    """

    def __init__(self, refuse: dict[str, int] | None = None):
        self.calls: list[tuple[str, str, str | None]] = []
        self.refuse = refuse or {}

    def _delete(self, method, name, body):
        uid = body.preconditions.uid if body.preconditions is not None else None
        self.calls.append((method, name, uid))
        if name in self.refuse:
            raise ApiException(status=self.refuse[name])

    def delete_namespace(self, name, body):
        self._delete("delete_namespace", name, body)

    def delete_cluster_role(self, name, body):
        self._delete("delete_cluster_role", name, body)

    def delete_cluster_role_binding(self, name, body):
        self._delete("delete_cluster_role_binding", name, body)

    def delete_mutating_webhook_configuration(self, name, body):
        self._delete("delete_mutating_webhook_configuration", name, body)


@pytest.fixture
def typed(monkeypatch):
    """Route every typed delete to one FakeTypedApi, so no test touches a cluster."""
    api = FakeTypedApi()
    monkeypatch.setattr(owned_objects, "_typed_api", lambda kubectl, api_name: api)
    return api


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


def _log():
    return SimpleNamespace(info=Mock(), warning=Mock(), debug=Mock())


def test_the_infrastructure_label_marks_ownership():
    assert is_owned({"name": "mcp-server", "labels": {INFRA_LABEL: INFRA_VALUE}})
    assert not is_owned({"name": "mcp-server", "labels": {INFRA_LABEL: "something-else"}})
    assert not is_owned({"name": "mcp-server"})


def test_the_old_owned_label_alone_is_not_ownership():
    assert not is_owned({"name": "mcp-server", "labels": {"sregym.io/owned": "true"}})


def test_a_helm_release_is_ownership_only_with_its_release_annotations():
    for release in ("prometheus", "loki", "promtail"):
        metadata = {
            "labels": {INFRA_LABEL: release},
            "annotations": {"meta.helm.sh/release-name": release, "meta.helm.sh/release-namespace": "observe"},
        }
        assert is_owned(metadata)
    # The label alone, with no release annotations, is not enough.
    assert not is_owned({"labels": {INFRA_LABEL: "prometheus"}})
    # The same release name in another namespace is someone else's.
    assert not is_owned(
        {
            "labels": {INFRA_LABEL: "prometheus"},
            "annotations": {"meta.helm.sh/release-name": "prometheus", "meta.helm.sh/release-namespace": "kube-system"},
        }
    )
    assert not is_owned({"labels": {INFRA_LABEL: "metrics-server"}})


def test_a_ledger_uid_match_is_ownership_and_another_uid_is_not(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")

    assert is_owned({"name": "configmap-cleanup-controller", "uid": "uid-1"}, "clusterrole", ledger)
    assert not is_owned({"name": "configmap-cleanup-controller", "uid": "uid-2"}, "clusterrole", ledger)
    # Without the ledger, the name alone proves nothing.
    assert not is_owned({"name": "configmap-cleanup-controller", "uid": "uid-1"}, "clusterrole")


def test_labelling_leaves_an_unlabelled_resource_with_no_name_alone():
    resources = [{"metadata": {"name": "mcp-server"}}, {"kind": "List"}, {"metadata": {}}]

    labelled = label_infrastructure(resources)

    assert labelled[0]["metadata"]["labels"] == {INFRA_LABEL: INFRA_VALUE}
    assert labelled[1] == {"kind": "List"}
    assert labelled[2] == {"metadata": {}}


def test_a_same_named_foreign_object_is_not_deleted(typed):
    kubectl = _kubectl(
        {
            "clusterrole": _listing(
                {"name": "mcp-server", "uid": "uid-a", "labels": {INFRA_LABEL: INFRA_VALUE}},
                {"name": "system:metrics-server", "uid": "uid-b", "labels": {INFRA_LABEL: "metrics-server"}},
            ),
            "clusterrolebinding": _listing(
                {"name": "mcp-server", "uid": "uid-c", "labels": {INFRA_LABEL: INFRA_VALUE}},
            ),
        }
    )

    removed = delete_owned_cluster_scoped(kubectl, log=_log())

    assert removed == ["clusterrole/mcp-server", "clusterrolebinding/mcp-server"]
    assert typed.calls == [
        ("delete_cluster_role", "mcp-server", "uid-a"),
        ("delete_cluster_role_binding", "mcp-server", "uid-c"),
    ]


def test_a_ledger_owned_cluster_role_is_deleted_with_its_uid_and_forgotten(typed, tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")
    kubectl = _kubectl({"clusterrole": _listing({"name": "configmap-cleanup-controller", "uid": "uid-1"})})

    removed = delete_owned_cluster_scoped(kubectl, log=_log(), ledger=ledger)

    assert removed == ["clusterrole/configmap-cleanup-controller"]
    assert typed.calls == [("delete_cluster_role", "configmap-cleanup-controller", "uid-1")]
    assert ledger.uid_of("clusterrole", "configmap-cleanup-controller") is None


def test_a_ledger_entry_with_another_uid_is_not_deleted(typed, tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")
    kubectl = _kubectl({"clusterrole": _listing({"name": "configmap-cleanup-controller", "uid": "uid-2"})})

    assert delete_owned_cluster_scoped(kubectl, log=_log(), ledger=ledger) == []
    assert typed.calls == []
    assert ledger.uid_of("clusterrole", "configmap-cleanup-controller") == "uid-1"


def test_a_failed_listing_does_not_prune_the_ledger(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")
    kubectl = Mock()
    kubectl.exec_command_checked.side_effect = RuntimeError("connection refused")

    assert delete_owned_cluster_scoped(kubectl, log=_log(), ledger=ledger) == []
    assert ledger.uid_of("clusterrole", "configmap-cleanup-controller") == "uid-1"


def test_a_successful_listing_prunes_entries_that_are_gone(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("clusterrole", "gone-already", "uid-1")
    kubectl = _kubectl({"clusterrole": _listing()})

    delete_owned_cluster_scoped(kubectl, log=_log(), ledger=ledger)

    assert ledger.uid_of("clusterrole", "gone-already") is None


def test_the_earlier_attempt_case_is_covered_by_the_label():
    """An object created by an interrupted run that carries the infrastructure label is still removed."""
    kubectl = _kubectl({"clusterrole": _listing({"name": "mcp-server", "labels": {INFRA_LABEL: INFRA_VALUE}})})

    assert owned_names(kubectl, "clusterrole") == ["mcp-server"]


def test_a_read_failure_removes_nothing_and_says_so():
    kubectl = Mock()
    kubectl.exec_command_checked.side_effect = RuntimeError("connection refused")
    log = _log()

    assert delete_owned_cluster_scoped(kubectl, log=log) == []
    assert any("Could not list" in str(call.args[0]) for call in log.warning.call_args_list)


def test_foreign_names_lists_what_the_teardown_keeps():
    kubectl = _kubectl(
        {
            "clusterrole": _listing(
                {"name": "sregym-thing", "labels": {INFRA_LABEL: INFRA_VALUE}},
                {"name": "openshift-something", "labels": {}},
            )
        }
    )

    assert foreign_names(kubectl, "clusterrole") == ["openshift-something"]


def test_ledger_namespaces_are_matched_by_uid_only(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("namespace", "platform-ops", "uid-1")
    listing = _listing(
        {"name": "platform-ops", "uid": "uid-1"},
        {"name": "sregym", "uid": "uid-9"},
    )
    kubectl = _kubectl({"namespace": listing})

    assert ledger_namespaces_present(kubectl, ledger) == ["platform-ops"]


def test_delete_ledger_namespaces_deletes_only_a_uid_match(typed, tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("namespace", "platform-ops", "uid-1")
    ledger.record("namespace", "other", "uid-2")
    kubectl = _kubectl(
        {
            "namespace": _listing(
                {"name": "platform-ops", "uid": "uid-1"},
                {"name": "other", "uid": "uid-replaced"},
            )
        }
    )

    removed = delete_ledger_namespaces(kubectl, ledger, log=_log())

    assert removed == ["namespace/platform-ops"]
    assert typed.calls == [("delete_namespace", "platform-ops", "uid-1")]
    assert ledger.uid_of("namespace", "platform-ops") is None
    assert ledger.uid_of("namespace", "other") == "uid-2"


def test_a_namespace_replaced_after_listing_is_refused_left_alone_and_forgotten(typed, tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("namespace", "platform-ops", "uid-1")
    kubectl = _kubectl({"namespace": _listing({"name": "platform-ops", "uid": "uid-1"})})
    # The API server answers 409: the precondition UID no longer matches the object at that name.
    typed.refuse["platform-ops"] = 409

    assert delete_ledger_namespaces(kubectl, ledger, log=_log()) == []
    assert typed.calls == [("delete_namespace", "platform-ops", "uid-1")]
    assert ledger.uid_of("namespace", "platform-ops") is None


def test_a_nameless_item_is_skipped_not_a_key_error(typed, tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")
    listing = json.dumps(
        {
            "items": [
                {"metadata": {}},
                {"kind": "Status"},
                {"metadata": {"labels": {INFRA_LABEL: INFRA_VALUE}, "uid": "uid-x"}},
                {"metadata": {"name": "configmap-cleanup-controller", "uid": "uid-1"}},
            ]
        }
    )
    kubectl = _kubectl({"clusterrole": listing})

    assert owned_names(kubectl, "clusterrole", ledger=ledger) == ["configmap-cleanup-controller"]
    assert foreign_names(kubectl, "clusterrole", ledger=ledger) == []
    removed = delete_owned_cluster_scoped(kubectl, log=_log(), ledger=ledger)

    assert removed == ["clusterrole/configmap-cleanup-controller"]
    assert typed.calls == [("delete_cluster_role", "configmap-cleanup-controller", "uid-1")]


def test_a_nameless_namespace_item_is_skipped(typed, tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("namespace", "platform-ops", "uid-1")
    listing = json.dumps(
        {"items": [{"metadata": {"uid": "uid-9"}}, {"metadata": {"name": "platform-ops", "uid": "uid-1"}}]}
    )
    kubectl = _kubectl({"namespace": listing})

    assert delete_ledger_namespaces(kubectl, ledger, log=_log()) == ["namespace/platform-ops"]
    assert typed.calls == [("delete_namespace", "platform-ops", "uid-1")]


def test_each_kind_is_deleted_through_its_typed_client_method(typed, tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("clusterrolebinding", "binding", "uid-b")
    ledger.record("mutatingwebhookconfiguration", "hook", "uid-w")
    kubectl = _kubectl(
        {
            "clusterrolebinding": _listing({"name": "binding", "uid": "uid-b"}),
            "mutatingwebhookconfiguration": _listing({"name": "hook", "uid": "uid-w"}),
        }
    )

    removed = delete_owned_cluster_scoped(kubectl, log=_log(), ledger=ledger)

    assert sorted(removed) == ["clusterrolebinding/binding", "mutatingwebhookconfiguration/hook"]
    assert sorted(typed.calls) == [
        ("delete_cluster_role_binding", "binding", "uid-b"),
        ("delete_mutating_webhook_configuration", "hook", "uid-w"),
    ]


def test_typed_clients_share_the_kubectl_api_client():
    """The rbac and admission clients are built on KubeCtl's ApiClient, not a fresh configuration."""
    shared = object()
    kubectl = SimpleNamespace(core_v1_api=SimpleNamespace(api_client=shared))

    assert owned_objects._typed_api(kubectl, "CoreV1Api") is kubectl.core_v1_api
    rbac = owned_objects._typed_api(kubectl, "RbacAuthorizationV1Api")
    admission = owned_objects._typed_api(kubectl, "AdmissionregistrationV1Api")
    assert isinstance(rbac, RbacAuthorizationV1Api) and rbac.api_client is shared
    assert isinstance(admission, AdmissionregistrationV1Api) and admission.api_client is shared
