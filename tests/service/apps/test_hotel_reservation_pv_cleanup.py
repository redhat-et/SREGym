"""Cleanup sweeps the PersistentVolumes this application's claims named, never by name.

On a cluster whose default class provisions volumes dynamically the names are
``pvc-<uid>``, so a name match finds nothing (the volumes survive) or matches an
unrelated volume. A CSI driver also owns the deletion of its volume, so clearing a
finalizer to hurry it along leaks the backend volume.
"""

import json
import logging
from types import SimpleNamespace

import pytest

import sregym.service.apps.hotel_reservation as hotel_reservation_module
from sregym import storage
from sregym.service.apps.hotel_reservation import HotelReservation

NAMESPACE = "hotel-reservation"
PV_LIST_COMMAND = "kubectl get pv -o json"


class _AdvancingClock:
    """A clock that the patched sleep advances, so the bounded wait costs no real time."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class _FakeKubeCtl:
    """Record every command, and answer the PV reads from a scripted queue of answers.

    The last answer in the queue repeats, so a volume that outlives the bounded wait
    keeps being reported while one that is released disappears from later reads.
    """

    def __init__(self, pv_answers=()):
        self.pv_answers = [list(items) for items in pv_answers]
        self.commands = []

    def _answered_items(self):
        if len(self.pv_answers) > 1:
            return self.pv_answers.pop(0)
        return self.pv_answers[0] if self.pv_answers else []

    def exec_command(self, command, input_data=None):
        self.commands.append(command)
        if command == PV_LIST_COMMAND:
            items = self._answered_items()
            return json.dumps({"apiVersion": "v1", "kind": "PersistentVolumeList", "items": items})
        if "grep" in command:
            # Answer the name match this cleanup replaced the way a cluster would: the
            # volume names whose name contains the pattern, one per line. A test can
            # then see a name match pick up a volume it must not touch.
            return "\n".join(
                pv["metadata"]["name"] for pv in self._answered_items() if "hotel-reservation" in pv["metadata"]["name"]
            )
        return "persistentvolume deleted"

    def delete_namespace(self, namespace):
        self.commands.append(f"kubectl delete namespace {namespace}")

    def wait_for_namespace_deletion(self, namespace):
        self.commands.append(f"kubectl wait --for=delete namespace/{namespace}")

    def delete_job(self, label=None, namespace=None):
        self.commands.append(f"kubectl delete job -l {label} -n {namespace}")
        return True


def _pv(name, claim_namespace=NAMESPACE, phase="Released", volume_handle=None):
    pv = {
        "metadata": {"name": name},
        "spec": {"claimRef": {"name": f"{name}-claim", "namespace": claim_namespace}},
        "status": {"phase": phase},
    }
    if volume_handle:
        pv["spec"]["csi"] = {"driver": "ebs.csi.aws.com", "volumeHandle": volume_handle}
    return pv


@pytest.fixture
def clock(monkeypatch):
    clock = _AdvancingClock()
    monkeypatch.setattr(hotel_reservation_module, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    return clock


def _cleanup(monkeypatch, kubectl):
    """Run cleanup without constructing the app, so no cluster is contacted."""
    app = HotelReservation.__new__(HotelReservation)
    app.namespace = NAMESPACE
    app.kubectl = kubectl
    # `wrk` present, so the workload job cleanup after the sweep is exercised too.
    monkeypatch.setattr(app, "wrk", SimpleNamespace(), raising=False)
    app.cleanup()


def _messages(caplog):
    return [record.getMessage() for record in caplog.records]


def test_openebs_cleanup_patches_then_deletes_the_volumes_this_namespace_claimed(monkeypatch, clock):
    kubectl = _FakeKubeCtl(
        [
            [
                _pv("pvc-0a1b2c"),
                _pv("pvc-3d4e5f", claim_namespace="another-namespace"),
                _pv("hotel-reservation-data", claim_namespace="another-release"),
            ]
        ]
    )

    _cleanup(monkeypatch, kubectl)

    assert kubectl.commands == [
        f"kubectl delete namespace {NAMESPACE}",
        f"kubectl wait --for=delete namespace/{NAMESPACE}",
        PV_LIST_COMMAND,
        'kubectl patch pv pvc-0a1b2c -p \'{"metadata":{"finalizers":null}}\'',
        "kubectl delete pv pvc-0a1b2c",
        f"kubectl delete job -l job=workload -n {NAMESPACE}",
    ]


def test_openebs_cleanup_leaves_another_namespaces_volume_that_mentions_the_app_in_its_name(monkeypatch, clock):
    """The unanchored name match removed this volume; its claim belongs to another namespace."""
    kubectl = _FakeKubeCtl([[_pv("hotel-reservation-data", claim_namespace="another-release")]])

    _cleanup(monkeypatch, kubectl)

    assert not any("hotel-reservation-data" in command for command in kubectl.commands)


def test_openebs_cleanup_reads_the_volumes_as_json_instead_of_a_shell_match(monkeypatch, clock):
    kubectl = _FakeKubeCtl([[_pv("pvc-0a1b2c")]])

    _cleanup(monkeypatch, kubectl)

    assert PV_LIST_COMMAND in kubectl.commands
    assert not any("grep" in command for command in kubectl.commands)
    assert not any("--no-headers" in command for command in kubectl.commands)


def test_csi_cleanup_reports_an_unreleased_volume_and_deletes_nothing(monkeypatch, clock, caplog):
    monkeypatch.setattr(storage, "_storage_class", "gp3")
    kubectl = _FakeKubeCtl([[_pv("pvc-9f8e7d", volume_handle="vol-0abc123")]])

    with caplog.at_level(logging.WARNING):
        _cleanup(monkeypatch, kubectl)

    assert not any("kubectl patch pv" in command for command in kubectl.commands)
    assert not any("kubectl delete pv" in command for command in kubectl.commands)
    messages = _messages(caplog)
    reported = [message for message in messages if "pvc-9f8e7d" in message]
    assert len(reported) == 1
    assert "Released" in reported[0]
    assert "vol-0abc123" in reported[0]
    summary = [message for message in messages if "1 PersistentVolume(s)" in message]
    assert len(summary) == 1
    assert NAMESPACE in summary[0]


def test_csi_cleanup_reports_a_missing_csi_block_instead_of_raising(monkeypatch, clock, caplog):
    monkeypatch.setattr(storage, "_storage_class", "gp3")
    kubectl = _FakeKubeCtl([[_pv("local-static-pv", phase="Available")]])

    with caplog.at_level(logging.WARNING):
        _cleanup(monkeypatch, kubectl)

    reported = [message for message in _messages(caplog) if "local-static-pv" in message]
    assert len(reported) == 1
    assert "volumeHandle n/a" in reported[0]


def test_csi_cleanup_rechecks_before_it_reports(monkeypatch, clock, caplog):
    """A volume the driver releases inside the bounded wait is not reported."""
    monkeypatch.setattr(storage, "_storage_class", "gp3")
    kubectl = _FakeKubeCtl([[_pv("pvc-9f8e7d", volume_handle="vol-0abc123")], []])

    with caplog.at_level(logging.WARNING):
        _cleanup(monkeypatch, kubectl)

    assert kubectl.commands.count(PV_LIST_COMMAND) == 2
    assert hotel_reservation_module.PV_RELEASE_POLL_SECONDS in clock.sleeps
    assert _messages(caplog) == []


@pytest.mark.parametrize("storage_class", [storage.OPENebs, "gp3"])
def test_namespace_deletion_precedes_the_sweep_and_the_short_wait_follows(monkeypatch, clock, storage_class):
    monkeypatch.setattr(storage, "_storage_class", storage_class)
    kubectl = _FakeKubeCtl([[_pv("pvc-0a1b2c", volume_handle="vol-0abc123")]])

    _cleanup(monkeypatch, kubectl)

    assert kubectl.commands[:3] == [
        f"kubectl delete namespace {NAMESPACE}",
        f"kubectl wait --for=delete namespace/{NAMESPACE}",
        PV_LIST_COMMAND,
    ]
    assert clock.sleeps[-1] == 5
    assert kubectl.commands[-1] == f"kubectl delete job -l job=workload -n {NAMESPACE}"
