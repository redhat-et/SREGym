"""The default storage class must not install OpenEBS on a managed platform.

Selecting OpenEBS applies an operator manifest from the internet: two CRDs,
cluster-scoped RBAC and two StorageClasses, plus a privileged node-disk-manager
DaemonSet. On a managed cluster the privileged pods are refused, so the run dies
after those cluster-scoped objects exist, and nothing in this harness removes them
-- ``--teardown-observers`` removes what carries the ownership label, and they carry
none. A forgotten ``--storage-class`` therefore refuses instead of deploying.
"""

import logging

import pytest

from sregym import cluster_platform, storage
from sregym.cluster_platform import PlatformVerificationError
from sregym.conductor.conductor import Conductor


@pytest.fixture(autouse=True)
def no_storage_class_selected(monkeypatch):
    """Start every test from the real default: nothing named, so OpenEBS."""
    monkeypatch.setattr(storage, "_storage_class", None)
    monkeypatch.delenv("SREGYM_STORAGE_CLASS", raising=False)


def conductor_for(platform: str) -> Conductor:
    """A conductor just far enough built to reach the storage/platform check."""
    conductor = Conductor.__new__(Conductor)
    conductor.platform = platform
    conductor.logger = logging.getLogger("test.openebs")
    return conductor


def test_the_default_class_is_refused_on_openshift(monkeypatch):
    conductor = conductor_for(cluster_platform.OPENSHIFT)

    with pytest.raises(PlatformVerificationError, match="--storage-class"):
        conductor._refuse_default_openebs_on_openshift()


def test_the_refusal_names_what_would_be_installed(monkeypatch):
    conductor = conductor_for(cluster_platform.OPENSHIFT)

    with pytest.raises(PlatformVerificationError) as raised:
        conductor._refuse_default_openebs_on_openshift()

    message = str(raised.value)
    assert "CRDs" in message
    assert "node-disk-manager" in message
    assert "nothing here removes" in message


def test_the_default_class_still_runs_on_kind(monkeypatch):
    """kind behaviour must not change: OpenEBS is how kind gets a storage class."""
    conductor = conductor_for(cluster_platform.KIND)

    conductor._refuse_default_openebs_on_openshift()


def test_a_named_class_is_accepted_on_openshift(monkeypatch):
    monkeypatch.setattr(storage, "_storage_class", "gp3-csi")
    conductor = conductor_for(cluster_platform.OPENSHIFT)

    conductor._refuse_default_openebs_on_openshift()


def test_naming_openebs_yourself_warns_rather_than_refuses(monkeypatch, caplog):
    """An explicit request is the operator's call; it is named, not overridden."""
    monkeypatch.setattr(storage, "_storage_class", storage.OPENebs)
    conductor = conductor_for(cluster_platform.OPENSHIFT)

    with caplog.at_level(logging.WARNING):
        conductor._refuse_default_openebs_on_openshift()

    assert "selected by name on openshift" in caplog.text
    assert "node-disk-manager" in caplog.text


def test_the_env_var_counts_as_explicit(monkeypatch):
    """SREGYM_STORAGE_CLASS is a named class too, so it is honoured."""
    monkeypatch.setenv("SREGYM_STORAGE_CLASS", "gp3-csi")
    conductor = conductor_for(cluster_platform.OPENSHIFT)

    conductor._refuse_default_openebs_on_openshift()


def test_the_check_is_reached_through_verify_platform(monkeypatch):
    """It must run on the one funnel both entry points use, before any cluster write."""
    conductor = conductor_for(cluster_platform.OPENSHIFT)
    monkeypatch.setattr(conductor, "_platform_verified", False, raising=False)
    monkeypatch.setattr(
        "sregym.conductor.conductor.verify_platform_matches_cluster",
        lambda discover, log=None: cluster_platform.OPENSHIFT,
    )
    conductor.kubectl = type("Kube", (), {"openshift_api_present": lambda self=None: True})()

    with pytest.raises(PlatformVerificationError, match="--storage-class"):
        conductor.verify_platform()

    assert conductor._platform_verified is False
