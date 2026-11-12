"""The mutating-webhook scenario records what it creates and only deletes or replaces what it recorded."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from kubernetes.client.exceptions import ApiException

import sregym.conductor.problems.mutating_webhook_resource_limits as scenario_module
from sregym.conductor.problems.mutating_webhook_resource_limits import MutatingWebhookResourceLimits
from sregym.service.ownership_ledger import ForeignObjectError, OwnershipLedger

WEBHOOK = MutatingWebhookResourceLimits.WEBHOOK_NAME
DECOYS = [spec["name"] for spec in MutatingWebhookResourceLimits.DECOY_WEBHOOKS]
NAMESPACE = MutatingWebhookResourceLimits.BACKEND_SVC_NAMESPACE


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(scenario_module.time, "sleep", lambda _seconds: None)


def _uid(name):
    return f"uid-{name}"


def _created(body):
    """Return the object the API server would hand back for a create, with a UID assigned."""
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=body["metadata"]["name"], uid=_uid(body["metadata"]["name"]), resource_version="7"
        )
    )


def _existing(name, uid):
    return SimpleNamespace(metadata=SimpleNamespace(name=name, uid=uid, resource_version="3"))


def _conflict():
    return ApiException(status=409, reason="Conflict")


def _not_found():
    return ApiException(status=404, reason="Not Found")


@pytest.fixture
def problem():
    p = object.__new__(MutatingWebhookResourceLimits)
    p.faulty_service = "nginx-thrift"
    p.namespace = "social-network"
    p.ca_bundle = "Q0E="
    p.fault_injected = False
    p.admission_api = Mock()
    p.core_api = Mock()
    p.admission_api.create_mutating_webhook_configuration.side_effect = lambda body: _created(body)
    p.core_api.create_namespace.side_effect = lambda body: _created(body.to_dict())
    p._run = Mock()
    p._generate_tls_material = Mock(return_value={"tls_crt_b64": "Y3J0", "tls_key_b64": "a2V5", "ca_bundle": "Q0E="})
    p._patch_target_deployment_resources = Mock()
    deployment = SimpleNamespace(
        spec=SimpleNamespace(
            selector=SimpleNamespace(match_labels={"service": "nginx-thrift"}),
        )
    )
    p.kubectl = Mock()
    p.kubectl.get_deployment.return_value = deployment
    p.core_api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[SimpleNamespace(metadata=SimpleNamespace(name="nginx-thrift-abc"))]
    )
    p.core_api.read_namespace.side_effect = _not_found()
    return p


def _ledger():
    return OwnershipLedger()


def _all_created_metadata(problem):
    """Metadata of every webhook and namespace body the scenario sent to the API."""
    bodies = [c.kwargs["body"] for c in problem.admission_api.create_mutating_webhook_configuration.call_args_list]
    bodies += [c.kwargs["body"] for c in problem.admission_api.replace_mutating_webhook_configuration.call_args_list]
    bodies += [c.kwargs["body"].to_dict() for c in problem.core_api.create_namespace.call_args_list]
    return [body["metadata"] for body in bodies]


def test_clean_inject_records_five_webhooks_and_the_namespace(problem):
    problem.inject_fault()

    webhook_entries = {entry.name: entry.uid for entry in _ledger().entries("mutatingwebhookconfiguration")}
    assert webhook_entries == {name: _uid(name) for name in [WEBHOOK, *DECOYS]}
    namespace_entries = _ledger().entries("namespace")
    assert [(entry.name, entry.uid) for entry in namespace_entries] == [(NAMESPACE, _uid(NAMESPACE))]


def test_backend_namespace_is_created_by_api_not_by_manifest(problem):
    problem.inject_fault()

    apply_calls = [c for c in problem._run.call_args_list if c.args and c.args[0][:2] == ["kubectl", "apply"]]
    assert len(apply_calls) == 1
    manifest = apply_calls[0].kwargs["input"]
    assert "kind: Namespace" not in manifest
    problem.core_api.create_namespace.assert_called_once()


def test_no_label_or_annotation_on_any_created_object_mentions_sregym_or_owned(problem):
    problem.inject_fault()

    for metadata in _all_created_metadata(problem):
        for field in ("labels", "annotations"):
            for key, value in (metadata.get(field) or {}).items():
                assert "sregym" not in f"{key}={value}".lower()
                assert "owned" not in f"{key}={value}".lower()

    manifest = next(
        c.kwargs["input"] for c in problem._run.call_args_list if c.args and c.args[0][:2] == ["kubectl", "apply"]
    )
    for doc in yaml.safe_load_all(manifest):
        if not doc:
            continue
        metadata = doc.get("metadata", {})
        for field in ("labels", "annotations"):
            for key, value in (metadata.get(field) or {}).items():
                assert "sregym" not in f"{key}={value}".lower(), doc["kind"]
                assert "owned" not in f"{key}={value}".lower(), doc["kind"]


def test_foreign_real_webhook_with_same_name_is_refused_and_not_replaced(problem):
    def create(body):
        if body["metadata"]["name"] == WEBHOOK:
            raise _conflict()
        return _created(body)

    problem.admission_api.create_mutating_webhook_configuration.side_effect = create
    problem.admission_api.read_mutating_webhook_configuration.side_effect = lambda name: _existing(name, "foreign-uid")

    with pytest.raises(ForeignObjectError):
        problem.inject_fault()

    problem.admission_api.replace_mutating_webhook_configuration.assert_not_called()
    assert _ledger().uid_of("mutatingwebhookconfiguration", WEBHOOK) is None


def test_foreign_decoy_with_same_name_is_refused_and_not_replaced(problem):
    decoy = DECOYS[0]

    def create(body):
        if body["metadata"]["name"] == decoy:
            raise _conflict()
        return _created(body)

    problem.admission_api.create_mutating_webhook_configuration.side_effect = create
    problem.admission_api.read_mutating_webhook_configuration.side_effect = lambda name: _existing(name, "foreign-uid")

    with pytest.raises(ForeignObjectError):
        problem._install_decoy_webhooks()

    problem.admission_api.replace_mutating_webhook_configuration.assert_not_called()


def test_ledger_owned_leftover_webhook_is_replaced(problem):
    _ledger().record("mutatingwebhookconfiguration", WEBHOOK, "uid-old", owner="test")

    def create(body):
        if body["metadata"]["name"] == WEBHOOK:
            raise _conflict()
        return _created(body)

    problem.admission_api.create_mutating_webhook_configuration.side_effect = create
    problem.admission_api.read_mutating_webhook_configuration.side_effect = lambda name: _existing(name, "uid-old")

    problem.inject_fault()

    replaced = problem.admission_api.replace_mutating_webhook_configuration.call_args
    assert replaced.kwargs["name"] == WEBHOOK
    problem.admission_api.delete_mutating_webhook_configuration.assert_not_called()


def test_recover_with_no_ledger_entries_deletes_nothing(problem):
    problem.recover_fault()

    problem.admission_api.delete_mutating_webhook_configuration.assert_not_called()
    problem.core_api.delete_namespace.assert_not_called()


def test_recover_deletes_each_recorded_object_by_name_with_uid_precondition(problem):
    problem.inject_fault()

    problem.admission_api.delete_mutating_webhook_configuration.reset_mock()
    problem.core_api.read_namespace.side_effect = _not_found()
    problem.recover_fault()

    deleted = problem.admission_api.delete_mutating_webhook_configuration.call_args_list
    assert [c.kwargs["name"] for c in deleted] == [WEBHOOK, *DECOYS]
    for c in deleted:
        assert c.kwargs["body"].preconditions.uid == _uid(c.kwargs["name"])

    namespace_delete = problem.core_api.delete_namespace.call_args
    assert namespace_delete.kwargs["name"] == NAMESPACE
    assert namespace_delete.kwargs["body"].preconditions.uid == _uid(NAMESPACE)
    assert _ledger().entries() == []


def test_recover_waits_for_namespace_to_disappear(problem, monkeypatch):
    problem.inject_fault()
    reads = [SimpleNamespace(metadata=SimpleNamespace(name=NAMESPACE)), _not_found()]
    problem.core_api.read_namespace.side_effect = reads

    problem.recover_fault()

    assert problem.core_api.read_namespace.call_count == 2


def test_recover_conflict_on_delete_leaves_object_and_forgets_entry(problem):
    problem.inject_fault()

    def delete(name, body):
        if name == WEBHOOK:
            raise _conflict()

    problem.admission_api.delete_mutating_webhook_configuration.side_effect = delete

    problem.recover_fault()

    names = [c.kwargs["name"] for c in problem.admission_api.delete_mutating_webhook_configuration.call_args_list]
    assert WEBHOOK in names
    assert _ledger().uid_of("mutatingwebhookconfiguration", WEBHOOK) is None
    assert _ledger().uid_of("mutatingwebhookconfiguration", DECOYS[0]) is None


def test_recover_namespace_conflict_on_delete_forgets_entry_and_keeps_going(problem):
    _ledger().record("namespace", NAMESPACE, "uid-other", owner="test")
    problem.core_api.delete_namespace.side_effect = _conflict()

    problem.recover_fault()

    problem.core_api.delete_namespace.assert_called_once()
    assert _ledger().uid_of("namespace", NAMESPACE) is None
    restarts = [c for c in problem._run.call_args_list if "restart" in c.args[0]]
    assert len(restarts) == 1


def test_backend_namespace_is_reused_only_when_ledger_records_it(problem):
    problem.core_api.create_namespace.side_effect = _conflict()
    problem.core_api.read_namespace.side_effect = None
    problem.core_api.read_namespace.return_value = _existing(NAMESPACE, "foreign-uid")

    with pytest.raises(ForeignObjectError):
        problem._create_backend_namespace()


def test_backend_namespace_ledger_owned_leftover_is_reused(problem):
    _ledger().record("namespace", NAMESPACE, "uid-old", owner="test")
    problem.core_api.create_namespace.side_effect = _conflict()
    problem.core_api.read_namespace.side_effect = None
    problem.core_api.read_namespace.return_value = _existing(NAMESPACE, "uid-old")

    problem._create_backend_namespace()

    assert _ledger().uid_of("namespace", NAMESPACE) == "uid-old"
