import importlib
import json
import os
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

import sregym.paths
from sregym.service import ownership_ledger
from sregym.service.ownership_ledger import (
    ForeignObjectError,
    OwnershipLedger,
    delete_owned,
    uid_of_object,
)


def test_round_trip_survives_a_new_instance(tmp_path):
    path = tmp_path / "owned_objects.json"
    ledger = OwnershipLedger(path)

    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1", owner="finalizer")

    reread = OwnershipLedger(path)
    assert reread.uid_of("clusterrole", "configmap-cleanup-controller") == "uid-1"
    assert reread.owns("clusterrole", "configmap-cleanup-controller", "uid-1")
    [entry] = reread.entries("clusterrole")
    assert entry.owner == "finalizer"
    assert entry.namespace is None
    assert entry.recorded_at


def test_same_name_with_another_uid_is_not_owned(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("namespace", "platform-ops", "uid-ours")

    assert not ledger.owns("namespace", "platform-ops", "uid-theirs")
    assert not ledger.owns("namespace", "platform-ops", None)
    assert not ledger.owns("namespace", "platform-ops", "")
    assert not ledger.owns("clusterrole", "platform-ops", "uid-ours")


def test_empty_or_non_string_uid_is_not_recorded(tmp_path, caplog):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")

    ledger.record("namespace", "platform-ops", "")
    ledger.record("namespace", "platform-ops", None)
    ledger.record("namespace", "platform-ops", 42)

    assert ledger.uid_of("namespace", "platform-ops") is None
    assert ledger.entries() == []
    assert "not recording" in caplog.text.lower()


def test_unsupported_kind_is_rejected(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    with pytest.raises(ValueError):
        ledger.record("pod", "x", "uid-1")


def test_record_replaces_an_entry_with_the_same_name(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("mutatingwebhookconfiguration", "cert-manager-webhook", "uid-old")
    ledger.record("mutatingwebhookconfiguration", "cert-manager-webhook", "uid-new")

    assert ledger.uid_of("mutatingwebhookconfiguration", "cert-manager-webhook") == "uid-new"
    assert len(ledger.entries()) == 1


def test_forget_removes_only_that_entry(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("namespace", "platform-ops", "uid-1")
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-2")

    ledger.forget("namespace", "platform-ops")

    assert ledger.uid_of("namespace", "platform-ops") is None
    assert ledger.uid_of("clusterrole", "configmap-cleanup-controller") == "uid-2"


def test_require_owned_passes_for_the_recorded_uid_and_refuses_otherwise(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")

    ledger.require_owned("clusterrole", "configmap-cleanup-controller", "uid-1", action="replace")
    with pytest.raises(ForeignObjectError) as raised:
        ledger.require_owned("clusterrole", "configmap-cleanup-controller", "uid-2", action="replace")

    message = str(raised.value)
    assert message.startswith(
        "clusterrole configmap-cleanup-controller already exists and this run did not create it; will not replace."
    )
    assert "SREGYM_OWNERSHIP_LEDGER" in message


def test_corrupt_file_reads_as_empty_and_is_moved_aside_on_write(tmp_path, caplog):
    path = tmp_path / "owned_objects.json"
    path.write_text("{not json")
    ledger = OwnershipLedger(path)

    assert ledger.entries() == []
    assert ledger.uid_of("namespace", "platform-ops") is None
    assert "not valid" in caplog.text
    assert path.read_text() == "{not json"

    ledger.record("namespace", "platform-ops", "uid-1")

    [moved] = list(tmp_path.glob("owned_objects.json.corrupt.*"))
    assert moved.read_text() == "{not json"
    assert OwnershipLedger(path).uid_of("namespace", "platform-ops") == "uid-1"


def test_a_second_corrupt_ledger_does_not_overwrite_the_first(tmp_path):
    path = tmp_path / "owned_objects.json"
    ledger = OwnershipLedger(path)

    path.write_text("{first corruption")
    ledger.record("namespace", "platform-ops", "uid-1")
    path.write_text("{second corruption")
    ledger.record("namespace", "platform-ops", "uid-2")

    moved = sorted(p.read_text() for p in tmp_path.glob("owned_objects.json.corrupt*"))
    assert moved == ["{first corruption", "{second corruption"]
    assert OwnershipLedger(path).uid_of("namespace", "platform-ops") == "uid-2"


def test_wrong_shape_reads_as_empty(tmp_path):
    path = tmp_path / "owned_objects.json"
    path.write_text(json.dumps(["not", "a", "ledger"]))
    assert OwnershipLedger(path).entries() == []

    path.write_text(json.dumps({"entries": [{"kind": "namespace"}]}))
    assert OwnershipLedger(path).entries() == []


def test_failed_write_leaves_the_previous_ledger_and_no_partial_file(tmp_path, monkeypatch):
    path = tmp_path / "owned_objects.json"
    ledger = OwnershipLedger(path)
    ledger.record("namespace", "platform-ops", "uid-1")

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(ownership_ledger.os, "replace", failing_replace)
    with pytest.raises(OSError):
        ledger.record("clusterrole", "configmap-cleanup-controller", "uid-2")
    monkeypatch.undo()

    assert OwnershipLedger(path).uid_of("namespace", "platform-ops") == "uid-1"
    assert OwnershipLedger(path).uid_of("clusterrole", "configmap-cleanup-controller") is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["owned_objects.json"]


def test_ledger_file_is_private(tmp_path):
    path = tmp_path / "nested" / "owned_objects.json"
    OwnershipLedger(path).record("namespace", "platform-ops", "uid-1")
    assert path.stat().st_mode & 0o777 == 0o600


def test_default_path_follows_the_paths_module_at_call_time(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere.json"
    monkeypatch.setattr(sregym.paths, "OWNERSHIP_LEDGER_FILE", target, raising=False)

    OwnershipLedger().record("namespace", "platform-ops", "uid-1")

    assert target.exists()
    assert OwnershipLedger().uid_of("namespace", "platform-ops") == "uid-1"


def test_env_var_overrides_the_default_ledger_location(tmp_path, monkeypatch):
    override = tmp_path / "pinned" / "ledger.json"
    monkeypatch.setenv("SREGYM_OWNERSHIP_LEDGER", str(override))
    try:
        importlib.reload(sregym.paths)
        assert override == sregym.paths.OWNERSHIP_LEDGER_FILE
    finally:
        monkeypatch.delenv("SREGYM_OWNERSHIP_LEDGER")
        importlib.reload(sregym.paths)

    assert sregym.paths.OWNERSHIP_LEDGER_FILE == sregym.paths.CACHE_DIR / "owned_objects.json"


def test_uid_of_object_reads_the_metadata_uid_only():
    assert uid_of_object(SimpleNamespace(metadata=SimpleNamespace(uid="uid-1"))) == "uid-1"
    assert uid_of_object(SimpleNamespace(metadata=SimpleNamespace(uid=""))) is None
    assert uid_of_object(SimpleNamespace(metadata=SimpleNamespace(uid=None))) is None
    assert uid_of_object(SimpleNamespace(metadata=None)) is None
    assert uid_of_object(None) is None


def test_delete_owned_without_a_record_makes_no_call(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    calls = []

    outcome = delete_owned(ledger, "namespace", "platform-ops", lambda body: calls.append(body))

    assert outcome == "not_ours"
    assert calls == []


def test_delete_owned_deletes_with_the_uid_precondition_and_forgets(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-1")
    calls = []

    outcome = delete_owned(ledger, "clusterrole", "configmap-cleanup-controller", lambda body: calls.append(body))

    assert outcome == "deleted"
    [body] = calls
    assert body.preconditions.uid == "uid-1"
    assert ledger.uid_of("clusterrole", "configmap-cleanup-controller") is None


def test_delete_owned_404_is_absent_and_forgets(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("namespace", "platform-ops", "uid-1")

    def gone(body):
        raise ApiException(status=404)

    assert delete_owned(ledger, "namespace", "platform-ops", gone) == "absent"
    assert ledger.uid_of("namespace", "platform-ops") is None


def test_delete_owned_409_is_foreign_and_forgets(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("mutatingwebhookconfiguration", "cert-manager-webhook", "uid-1")

    def conflict(body):
        raise ApiException(status=409)

    assert delete_owned(ledger, "mutatingwebhookconfiguration", "cert-manager-webhook", conflict) == "foreign"
    assert ledger.uid_of("mutatingwebhookconfiguration", "cert-manager-webhook") is None


def test_delete_owned_other_errors_propagate_and_keep_the_entry(tmp_path):
    ledger = OwnershipLedger(tmp_path / "owned_objects.json")
    ledger.record("namespace", "platform-ops", "uid-1")

    def server_error(body):
        raise ApiException(status=500)

    with pytest.raises(ApiException):
        delete_owned(ledger, "namespace", "platform-ops", server_error)
    assert ledger.uid_of("namespace", "platform-ops") == "uid-1"
    assert os.path.exists(ledger.path)
