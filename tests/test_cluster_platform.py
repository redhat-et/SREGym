import importlib

import pytest

from sregym import cluster_platform
from sregym.cluster_platform import KIND, OPENSHIFT, PlatformVerificationError


class RecordingLogger:
    """A logger the test reads directly.

    caplog cannot be trusted here: `logger.init_logger()` installs handlers with
    propagate=False on the `all.*` loggers, so whether caplog sees a message
    depends on which test in the suite initialised logging first.
    """

    def __init__(self):
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, message):
        self.infos.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def debug(self, message):
        pass


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


def test_platform_defaults_to_kind():
    assert cluster_platform.get_platform() == KIND
    assert cluster_platform.is_openshift() is False


def test_set_platform_selects_openshift():
    cluster_platform.set_platform(OPENSHIFT)
    assert cluster_platform.get_platform() == OPENSHIFT
    assert cluster_platform.is_openshift() is True


def test_env_var_is_the_fallback_when_set_platform_was_never_called(monkeypatch):
    monkeypatch.setenv("SREGYM_PLATFORM", OPENSHIFT)
    assert cluster_platform.get_platform() == OPENSHIFT


def test_unknown_env_var_raises(monkeypatch):
    monkeypatch.setenv("SREGYM_PLATFORM", "nonsense")
    with pytest.raises(ValueError, match="nonsense"):
        cluster_platform.get_platform()


def test_set_platform_rejects_unknown_platform_and_keeps_previous_value():
    cluster_platform.set_platform(OPENSHIFT)
    with pytest.raises(ValueError, match="Unknown platform"):
        cluster_platform.set_platform("nonsense")
    assert cluster_platform.get_platform() == OPENSHIFT


@pytest.mark.parametrize(
    ("platform", "serves_metrics_api", "restricted_admission", "pins_control_plane"),
    [(KIND, False, False, True), (OPENSHIFT, True, True, False)],
)
def test_derived_predicates(platform, serves_metrics_api, restricted_admission, pins_control_plane):
    cluster_platform.set_platform(platform)
    assert cluster_platform.serves_metrics_api() is serves_metrics_api
    assert cluster_platform.restricted_admission() is restricted_admission
    assert cluster_platform.pins_control_plane() is pins_control_plane


def test_verify_refuses_kind_on_an_openshift_cluster():
    cluster_platform.set_platform(KIND)
    with pytest.raises(PlatformVerificationError, match="--platform openshift"):
        cluster_platform.verify_platform_matches_cluster(lambda: True)


def test_verify_confirms_openshift_on_an_openshift_cluster():
    cluster_platform.set_platform(OPENSHIFT)
    log = RecordingLogger()

    assert cluster_platform.verify_platform_matches_cluster(lambda: True, log=log) == OPENSHIFT
    assert any("confirmed by API discovery" in message for message in log.infos)


def test_verify_refuses_openshift_declared_on_a_cluster_that_is_not_openshift():
    # A result labelled openshift has to come from an OpenShift cluster.
    cluster_platform.set_platform(OPENSHIFT)

    with pytest.raises(PlatformVerificationError, match="--platform kind"):
        cluster_platform.verify_platform_matches_cluster(lambda: False)


@pytest.mark.parametrize("value", ["1", "true", "yes", "0", ""])
def test_no_environment_variable_overrides_the_openshift_refusal(monkeypatch, value):
    # The override was removed: no value of the old variable lifts the refusal.
    monkeypatch.setenv("SREGYM_ALLOW_UNVERIFIED_PLATFORM", value)
    cluster_platform.set_platform(OPENSHIFT)

    with pytest.raises(PlatformVerificationError, match="--platform kind"):
        cluster_platform.verify_platform_matches_cluster(lambda: False)


def test_verify_accepts_kind_on_a_kind_cluster():
    cluster_platform.set_platform(KIND)
    assert cluster_platform.verify_platform_matches_cluster(lambda: False) == KIND


def test_verify_refuses_when_discovery_fails():
    # Continuing on an unknown cluster would restore the hazard the check removes.
    cluster_platform.set_platform(KIND)

    def discover():
        raise ConnectionError("forbidden")

    with pytest.raises(PlatformVerificationError, match="explicit --platform"):
        cluster_platform.verify_platform_matches_cluster(discover)


def test_import_does_no_io(monkeypatch):
    monkeypatch.setattr(cluster_platform.os, "environ", {})
    module = importlib.reload(cluster_platform)
    assert module.get_platform() == KIND
