"""Release reuse: a recorded values fingerprint decides whether Helm installs again."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
import yaml

from sregym import cluster_platform, profile, storage
from sregym.cluster_platform import OPENSHIFT
from sregym.service import helm as helm_module
from sregym.service.helm import UNKNOWN_VALUES_FINGERPRINT, VALUES_FINGERPRINT_ANNOTATION, Helm
from sregym.service.telemetry.loki import Loki
from sregym.service.telemetry.prometheus import Prometheus
from sregym.service.telemetry.release_pvcs import use_main_release_handling
from sregym.storage import set_storage_class


@pytest.fixture(autouse=True)
def reset_selection(monkeypatch):
    monkeypatch.setattr(profile, "_profile", None)
    monkeypatch.delenv("SREGYM_PROFILE", raising=False)
    monkeypatch.setattr(storage, "_storage_class", None)
    monkeypatch.delenv("SREGYM_STORAGE_CLASS", raising=False)
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


@pytest.fixture
def openshift(monkeypatch):
    """Select the OpenShift path, where the release fingerprint decides reuse and redeploys."""
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.setenv("SREGYM_PLATFORM", OPENSHIFT)


class FakeCluster:
    """Answer the cluster calls of the fingerprint path and record every command.

    The Helm and PVC modules import the same `subprocess`, so one patch of
    `subprocess.run` intercepts the secret reads, the annotations and the PVC deletes.
    """

    def __init__(self, releases=("loki", "prometheus", "promtail")):
        self.releases = set(releases)
        self.annotations = {}
        self.secret_error = False
        self.annotate_error = False
        self.commands = []

    def run(self, command, **_kwargs):
        self.commands.append(list(command))
        if command[:3] == ["kubectl", "get", "secret"]:
            if self.secret_error:
                return SimpleNamespace(returncode=1, stdout="", stderr="forbidden")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"items": [self._secret(command)]}), stderr="")
        if command[:3] == ["kubectl", "annotate", "secret"] and self.annotate_error:
            return SimpleNamespace(returncode=1, stdout="", stderr="secrets is forbidden")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _secret(self, command):
        release = next(part for part in command if part.startswith("owner=helm,name=")).split("=", 2)[2]
        metadata = {"labels": {"owner": "helm", "name": release, "version": "1"}, "annotations": {}}
        fingerprint = self.annotations.get(release)
        if fingerprint:
            metadata["annotations"][VALUES_FINGERPRINT_ANNOTATION] = fingerprint
        return {"metadata": metadata}

    def popen(self, command, **_kwargs):
        self.commands.append(command)
        listing = "NAME\tNAMESPACE\tSTATUS\n" + "".join(
            f"{name}\tobserve\tdeployed\n" for name in sorted(self.releases)
        )
        return SimpleNamespace(communicate=lambda: (listing.encode(), b""), returncode=0)

    def annotated(self, release):
        """The fingerprint the last annotate command wrote for a release, or None."""
        for command in reversed(self.commands):
            if command[:3] == ["kubectl", "annotate", "secret"] and f"owner=helm,name={release}" in command:
                return command[-2].split("=", 1)[1]
        return None

    def deleted_pvcs(self):
        return [command for command in self.commands if command[:3] == ["kubectl", "delete", "pvc"]]


@pytest.fixture
def cluster(monkeypatch):
    fake = FakeCluster()
    monkeypatch.setattr(helm_module.subprocess, "run", fake.run)
    monkeypatch.setattr(helm_module.subprocess, "Popen", fake.popen)
    return fake


@pytest.fixture
def helm(monkeypatch):
    """Stub the cluster-touching Helm calls; the fingerprint helpers stay real."""
    patched = SimpleNamespace(install=MagicMock(), uninstall=MagicMock(), assert_if_deployed=MagicMock())
    for name, mock in vars(patched).items():
        monkeypatch.setattr(Helm, name, mock)
    return patched


def local_chart(tmp_path, values):
    chart = tmp_path / "chart"
    chart.mkdir(exist_ok=True)
    (chart / "values.yaml").write_text(yaml.safe_dump(values))
    return chart


def write_overlay(path, values):
    path.write_text(yaml.safe_dump(values))
    return path


def requested_configs(chart, overlay=None, storage_class="gp3"):
    extra_args = []
    if overlay is not None:
        extra_args += ["-f", str(overlay)]
    extra_args += ["--set-string", f"server.persistentVolume.storageClass={storage_class}"]
    return {
        "release_name": "prometheus",
        "namespace": "observe",
        "chart_path": str(chart),
        "extra_args": extra_args,
    }


# ------------------------------------------------------------------------ fingerprint


def test_fingerprint_is_stable_when_the_values_do_not_change(tmp_path):
    chart = local_chart(tmp_path, {"server": {"retention": "15d"}})
    overlay = write_overlay(tmp_path / "overlay.yaml", {"alertmanager": {"enabled": False}})
    configs = requested_configs(chart, overlay)

    first = Helm.requested_values_fingerprint(configs)

    # Key order and YAML formatting are not part of the fingerprint.
    (chart / "values.yaml").write_text("# reformatted\nserver:\n  retention: 15d\n")
    assert Helm.requested_values_fingerprint(configs) == first


def test_fingerprint_changes_when_the_base_values_change(tmp_path):
    chart = local_chart(tmp_path, {"server": {"retention": "15d"}})
    overlay = write_overlay(tmp_path / "overlay.yaml", {"alertmanager": {"enabled": False}})
    configs = requested_configs(chart, overlay)

    first = Helm.requested_values_fingerprint(configs)

    (chart / "values.yaml").write_text(yaml.safe_dump({"server": {"retention": "2h"}}))
    assert Helm.requested_values_fingerprint(configs) != first


def test_fingerprint_changes_when_an_overlay_is_added(tmp_path):
    chart = local_chart(tmp_path, {"server": {"retention": "15d"}})
    overlay = write_overlay(tmp_path / "svelte.yaml", {"alertmanager": {"enabled": False}})
    base_configs = {"release_name": "prometheus", "namespace": "observe", "chart_path": str(chart)}

    without_overlay = Helm.requested_values_fingerprint(base_configs)

    assert Helm.requested_values_fingerprint({**base_configs, "extra_args": ["-f", str(overlay)]}) != without_overlay


def test_fingerprint_changes_when_a_set_string_value_changes(tmp_path):
    chart = local_chart(tmp_path, {"server": {"persistentVolume": {"storageClass": "openebs-hostpath"}}})

    assert Helm.requested_values_fingerprint(
        requested_configs(chart, storage_class="gp3")
    ) != Helm.requested_values_fingerprint(requested_configs(chart, storage_class="standard"))


def test_fingerprint_changes_when_the_chart_version_changes(tmp_path):
    chart = local_chart(tmp_path, {"server": {"retention": "15d"}})
    configs = {**requested_configs(chart), "version": "1.0.0"}

    bumped = Helm.requested_values_fingerprint({**configs, "version": "1.1.0"})

    assert bumped != Helm.requested_values_fingerprint(configs)


def test_fingerprint_is_stable_when_the_chart_version_does_not_change(tmp_path):
    chart = local_chart(tmp_path, {"server": {"retention": "15d"}})
    configs = {**requested_configs(chart), "version": "1.0.0"}

    first = Helm.requested_values_fingerprint(configs)

    assert Helm.requested_values_fingerprint(dict(configs)) == first


def test_fingerprint_changes_when_the_chart_reference_changes(tmp_path):
    chart = local_chart(tmp_path, {"server": {"retention": "15d"}})
    configs = requested_configs(chart)

    other = {**configs, "chart_path": str(tmp_path / "other-chart")}

    assert Helm.requested_values_fingerprint(other) != Helm.requested_values_fingerprint(configs)


def test_an_unreadable_values_file_makes_the_fingerprint_unknown(tmp_path, caplog):
    chart = local_chart(tmp_path, {"server": {"retention": "15d"}})
    configs = requested_configs(chart, tmp_path / "absent.yaml")

    assert Helm.requested_values_fingerprint(configs) == UNKNOWN_VALUES_FINGERPRINT
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "absent.yaml" in warnings[0].getMessage()


# ------------------------------------------------------------------- release comparison


def test_a_matching_fingerprint_reuses_the_release(cluster):
    configs = Prometheus().helm_configs
    cluster.annotations["prometheus"] = Helm.requested_values_fingerprint(configs)

    assert Helm.release_matches_requested_values(configs) is True


def test_a_missing_annotation_refuses_reuse(cluster, caplog):
    configs = Prometheus().helm_configs

    assert Helm.release_matches_requested_values(configs) is False
    assert "no readable values fingerprint" in caplog.text


def test_an_unreadable_secret_refuses_reuse(cluster, caplog):
    configs = Prometheus().helm_configs
    cluster.annotations["prometheus"] = Helm.requested_values_fingerprint(configs)
    cluster.secret_error = True

    assert Helm.release_matches_requested_values(configs) is False
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "no readable values fingerprint" in warnings[0].getMessage()


def test_a_nonexistent_release_refuses_reuse(cluster, caplog):
    configs = Prometheus().helm_configs
    cluster.annotations["prometheus"] = Helm.requested_values_fingerprint(configs)
    cluster.releases = set()

    assert Helm.release_matches_requested_values(configs) is False
    assert "does not exist" in caplog.text


def test_a_different_fingerprint_refuses_reuse(cluster):
    configs = Prometheus().helm_configs
    cluster.annotations["prometheus"] = "0" * 64

    assert Helm.release_matches_requested_values(configs) is False


def test_a_chart_version_bump_redeploys_the_release(cluster):
    configs = Prometheus()._requested_helm_configs()
    installed = {**configs, "version": "1.0.0"}
    cluster.annotations["prometheus"] = Helm.requested_values_fingerprint(installed)

    assert Helm.release_matches_requested_values({**configs, "version": "1.1.0"}) is False
    assert Helm.release_matches_requested_values(installed) is True


def test_a_failed_annotation_is_a_warning_not_a_failure(cluster, caplog):
    cluster.annotate_error = True

    Helm.record_values_fingerprint(Prometheus().helm_configs)

    assert [command for command in cluster.commands if command[:3] == ["kubectl", "annotate", "secret"]]
    assert "Could not record the values fingerprint" in caplog.text


# ---------------------------------------------------------------------------- Prometheus


@pytest.mark.usefixtures("openshift")
def test_prometheus_reuses_the_release_it_recorded(monkeypatch, cluster, helm):
    prometheus = Prometheus()
    monkeypatch.setattr(prometheus, "_is_prometheus_running", lambda: False)
    monkeypatch.setattr(prometheus, "_wait_for_namespace_termination", lambda: None)

    prometheus.deploy()
    recorded = cluster.annotated("prometheus")
    assert recorded
    cluster.annotations["prometheus"] = recorded
    cluster.commands.clear()
    helm.install.reset_mock()
    helm.uninstall.reset_mock()

    monkeypatch.setattr(prometheus, "_is_prometheus_running", lambda: True)
    prometheus.deploy()

    helm.install.assert_not_called()
    helm.uninstall.assert_not_called()
    assert cluster.deleted_pvcs() == []
    assert cluster.annotated("prometheus") is None


@pytest.mark.usefixtures("openshift")
def test_prometheus_redeploys_when_the_fingerprint_differs(monkeypatch, cluster, helm):
    prometheus = Prometheus()
    monkeypatch.setattr(prometheus, "_is_prometheus_running", lambda: True)
    monkeypatch.setattr(prometheus, "_wait_for_namespace_termination", lambda: None)
    cluster.annotations["prometheus"] = "0" * 64

    prometheus.deploy()

    helm.uninstall.assert_called_once_with(**prometheus.helm_configs)
    deleted = cluster.deleted_pvcs()
    assert len(deleted) == 1
    assert "app.kubernetes.io/instance=prometheus" in deleted[0]
    helm.install.assert_called_once()
    assert cluster.annotated("prometheus") == Helm.requested_values_fingerprint(helm.install.call_args.kwargs)


# ---------------------------------------------------------------------------------- Loki


@pytest.mark.usefixtures("openshift")
def test_loki_reuses_the_release_it_recorded(monkeypatch, cluster, helm):
    loki = Loki()
    monkeypatch.setattr(loki, "_add_grafana_helm_repo", lambda: None)
    monkeypatch.setattr(loki, "_deploy_promtail", MagicMock())
    monkeypatch.setattr(loki, "_is_loki_running", lambda: False)

    loki.deploy()
    recorded = cluster.annotated("loki")
    assert recorded
    cluster.annotations["loki"] = recorded
    cluster.commands.clear()
    helm.install.reset_mock()
    helm.uninstall.reset_mock()

    monkeypatch.setattr(loki, "_is_loki_running", lambda: True)
    loki.deploy()

    helm.uninstall.assert_not_called()
    helm.install.assert_not_called()
    assert cluster.deleted_pvcs() == []
    assert cluster.annotated("loki") is None


@pytest.mark.usefixtures("openshift")
def test_loki_redeploys_and_deletes_its_claims_when_the_fingerprint_differs(monkeypatch, cluster, helm):
    loki = Loki()
    monkeypatch.setattr(loki, "_add_grafana_helm_repo", lambda: None)
    monkeypatch.setattr(loki, "_deploy_promtail", MagicMock())
    monkeypatch.setattr(loki, "_is_loki_running", lambda: True)
    cluster.annotations["loki"] = "0" * 64
    helm.uninstall.side_effect = lambda **_: cluster.commands.append(["uninstall"])
    helm.install.side_effect = lambda **_: cluster.commands.append(["install"])

    loki.deploy()

    helm.uninstall.assert_called_once_with(**loki.helm_configs)
    deleted = cluster.deleted_pvcs()
    assert len(deleted) == 1
    assert "app.kubernetes.io/instance=loki" in deleted[0]
    assert cluster.commands.index(["uninstall"]) < cluster.commands.index(deleted[0])
    assert cluster.commands.index(deleted[0]) < cluster.commands.index(["install"])
    assert cluster.annotated("loki") == Helm.requested_values_fingerprint(helm.install.call_args.kwargs)


@pytest.mark.usefixtures("openshift")
def test_a_changed_storage_class_redeploys_loki(monkeypatch, cluster, helm):
    loki = Loki()
    monkeypatch.setattr(loki, "_add_grafana_helm_repo", lambda: None)
    monkeypatch.setattr(loki, "_deploy_promtail", MagicMock())
    monkeypatch.setattr(loki, "_is_loki_running", lambda: False)

    loki.deploy()
    cluster.annotations["loki"] = cluster.annotated("loki")
    helm.install.reset_mock()
    helm.uninstall.reset_mock()

    set_storage_class("gp3")
    monkeypatch.setattr(loki, "_is_loki_running", lambda: True)
    loki.deploy()

    helm.uninstall.assert_called_once_with(**loki.helm_configs)
    extra_args = helm.install.call_args.kwargs["extra_args"]
    assert "singleBinary.persistence.storageClass=gp3" in extra_args


# ------------------------------------------------------------------------------ Promtail


@pytest.mark.usefixtures("openshift")
def test_promtail_reuses_the_release_it_recorded(monkeypatch, cluster, helm):
    loki = Loki()
    monkeypatch.setattr(loki, "_is_promtail_running", lambda: False)

    loki._deploy_promtail()
    recorded = cluster.annotated("promtail")
    assert recorded
    cluster.annotations["promtail"] = recorded
    cluster.commands.clear()
    helm.install.reset_mock()

    monkeypatch.setattr(loki, "_is_promtail_running", lambda: True)
    loki._deploy_promtail()

    helm.install.assert_not_called()
    assert cluster.annotated("promtail") is None


@pytest.mark.usefixtures("openshift")
def test_promtail_redeploys_when_its_values_file_changes(monkeypatch, cluster, helm, tmp_path):
    loki = Loki()
    values_file = tmp_path / "promtail-values.yaml"
    values_file.write_text(yaml.safe_dump({"config": {"clients": ["http://loki:3100"]}}))
    loki.promtail_values_file = str(values_file)
    monkeypatch.setattr(loki, "_is_promtail_running", lambda: False)
    loki._deploy_promtail()
    cluster.annotations["promtail"] = cluster.annotated("promtail")
    helm.install.reset_mock()
    monkeypatch.setattr(loki, "_is_promtail_running", lambda: True)

    loki._deploy_promtail()
    helm.install.assert_not_called()

    values_file.write_text(yaml.safe_dump({"config": {"clients": ["http://other:3100"]}}))
    loki._deploy_promtail()

    helm.install.assert_called_once()
    assert helm.install.call_args.kwargs["extra_args"][:2] == ["-f", str(values_file)]


@pytest.mark.usefixtures("openshift")
def test_promtail_uninstalls_a_stale_release_before_installing(monkeypatch, cluster, helm):
    """A release that exists but does not run makes `helm install` fail with "cannot
    reuse a name that is still in use"; it is removed first. A cluster that refused
    Promtail's pods once could not deploy a second scenario without this."""
    loki = Loki()
    monkeypatch.setattr(loki, "_is_promtail_running", lambda: False)
    helm.uninstall.side_effect = lambda **_: cluster.commands.append(["uninstall"])
    helm.install.side_effect = lambda **_: cluster.commands.append(["install"])

    loki._deploy_promtail()

    helm.uninstall.assert_called_once_with(release_name="promtail", namespace="observe")
    helm.install.assert_called_once()
    assert cluster.commands.index(["uninstall"]) < cluster.commands.index(["install"])


@pytest.mark.usefixtures("openshift")
def test_promtail_reinstall_does_not_raise_when_its_pods_are_never_admitted(monkeypatch, cluster, helm):
    """A cluster whose SCCs refuse Promtail's pods leaves the release installed and no
    pod running. A repeated deploy must reinstall and return, not raise: the fault
    lifecycle does not depend on Promtail, and failing here once failed every scenario."""
    loki = Loki()
    monkeypatch.setattr(loki, "_is_promtail_running", lambda: False)

    loki._deploy_promtail()
    helm.uninstall.reset_mock()
    helm.install.reset_mock()

    loki._deploy_promtail()

    helm.uninstall.assert_called_once_with(release_name="promtail", namespace="observe")
    helm.install.assert_called_once()


# ----------------------------------------------------------- Promtail's running-and-ready question


class _PodsListed:
    """Answer the one `kubectl get pods` call `_is_promtail_running` makes."""

    def __init__(self, listing):
        self.listing = listing

    def exec_command(self, command):  # noqa: ARG002 - the shape is what matters
        return self.listing


def test_a_running_and_ready_promtail_counts_as_running(monkeypatch):
    monkeypatch.setattr(
        "sregym.service.telemetry.loki.KubeCtl",
        lambda: _PodsListed("NAME READY STATUS\npromtail-abc 1/1 Running"),
    )

    assert Loki()._is_promtail_running() is True


def test_a_crash_looping_promtail_does_not_count_as_running(monkeypatch):
    """A pod in CrashLoopBackOff must be reinstalled, not reused: this is the ROSA state under a
    too-narrow SCC, and reusing the release there is what used to fail every scenario."""
    monkeypatch.setattr(
        "sregym.service.telemetry.loki.KubeCtl",
        lambda: _PodsListed("NAME READY STATUS\npromtail-abc 0/1 CrashLoopBackOff"),
    )

    assert Loki()._is_promtail_running() is False


def test_a_running_but_unready_promtail_does_not_count_as_running(monkeypatch):
    monkeypatch.setattr(
        "sregym.service.telemetry.loki.KubeCtl",
        lambda: _PodsListed("NAME READY STATUS\npromtail-abc 0/1 Running"),
    )

    assert Loki()._is_promtail_running() is False


def test_no_promtail_pod_does_not_count_as_running(monkeypatch):
    monkeypatch.setattr(
        "sregym.service.telemetry.loki.KubeCtl",
        lambda: _PodsListed("No resources found in observe namespace."),
    )

    assert Loki()._is_promtail_running() is False


# ------------------------------------------------------------- kind: the deploy `main` makes


def test_use_main_release_handling_only_on_kind_with_openebs(monkeypatch):
    assert use_main_release_handling() is True
    set_storage_class("gp3")
    assert use_main_release_handling() is False
    set_storage_class("openebs-hostpath")
    monkeypatch.setenv("SREGYM_PLATFORM", OPENSHIFT)
    monkeypatch.setattr(cluster_platform, "_platform", None)
    assert use_main_release_handling() is False


def test_kind_loki_deploy_is_main_s_deploy(monkeypatch, cluster, helm):
    loki = Loki()
    monkeypatch.setattr(loki, "_is_loki_running", lambda: False)
    monkeypatch.setattr(loki, "_add_grafana_helm_repo", MagicMock())
    promtail = MagicMock()
    monkeypatch.setattr(loki, "_deploy_promtail", promtail)
    cleanup = MagicMock()
    monkeypatch.setattr(loki, "_delete_retained_pvcs", cleanup)
    order = []
    helm.uninstall.side_effect = lambda **_: order.append("uninstall")
    helm.install.side_effect = lambda **_: order.append("install")
    loki._add_grafana_helm_repo.side_effect = lambda: order.append("repo")

    loki.deploy()

    assert order == ["uninstall", "repo", "install"]
    helm.uninstall.assert_called_once_with(**loki.helm_configs)
    helm.install.assert_called_once_with(**loki.helm_configs)
    helm.assert_if_deployed.assert_called_once_with("observe")
    promtail.assert_called_once_with()
    cleanup.assert_not_called()
    assert not [command for command in cluster.commands if command[:3] == ["kubectl", "annotate", "secret"]]


def test_kind_loki_running_skips_the_redeploy_and_keeps_promtail_logic(monkeypatch, cluster, helm, caplog):
    loki = Loki()
    monkeypatch.setattr(loki, "_is_loki_running", lambda: True)
    promtail = MagicMock()
    monkeypatch.setattr(loki, "_deploy_promtail", promtail)

    loki.deploy()

    assert "Loki is already running. Skipping redeployment." in caplog.text
    promtail.assert_called_once_with()
    helm.uninstall.assert_not_called()
    helm.install.assert_not_called()
    assert cluster.commands == []


def test_kind_promtail_skips_when_any_row_says_running(monkeypatch, cluster, helm):
    """`main` skips Promtail on the word Running alone, so a 0/1 pod that runs is kept."""
    monkeypatch.setattr(
        "sregym.service.telemetry.loki.KubeCtl",
        lambda: _PodsListed("NAME READY STATUS\npromtail-abc 0/1 Running"),
    )
    monkeypatch.setattr(
        Loki,
        "_is_promtail_running",
        MagicMock(side_effect=AssertionError("kind must not use the readiness check")),
    )

    Loki()._deploy_promtail()

    helm.install.assert_not_called()
    helm.uninstall.assert_not_called()
    assert cluster.commands == []


def test_kind_promtail_installs_with_main_arguments(monkeypatch, cluster, helm):
    loki = Loki()
    monkeypatch.setattr(
        "sregym.service.telemetry.loki.KubeCtl",
        lambda: _PodsListed("No resources found in observe namespace."),
    )

    loki._deploy_promtail()

    helm.install.assert_called_once_with(
        release_name="promtail",
        chart_path="grafana/promtail",
        namespace="observe",
        remote_chart=True,
        extra_args=["-f", loki.promtail_values_file],
    )
    helm.assert_if_deployed.assert_called_once_with("observe")
    helm.uninstall.assert_not_called()
    assert cluster.commands == []


def test_kind_prometheus_deploy_is_main_s_deploy(monkeypatch, cluster, helm):
    prometheus = Prometheus()
    monkeypatch.setattr(prometheus, "_is_prometheus_running", lambda: False)
    monkeypatch.setattr(prometheus, "_wait_for_namespace_termination", lambda: None)
    cleanup = MagicMock()
    monkeypatch.setattr(prometheus, "_delete_retained_pvcs", cleanup)
    query = MagicMock()
    monkeypatch.setattr(prometheus, "_matches_requested_profile", query)

    prometheus.deploy()

    query.assert_not_called()
    cleanup.assert_called_once_with()
    helm.uninstall.assert_called_once_with(**prometheus.helm_configs)
    helm.install.assert_called_once_with(**prometheus.helm_configs)
    helm.assert_if_deployed.assert_called_once_with("observe")
    assert cluster.commands == []


def test_kind_prometheus_reuses_a_release_that_matches_the_values_listing(monkeypatch, helm):
    prometheus = Prometheus()
    monkeypatch.setattr(prometheus, "_is_prometheus_running", lambda: True)
    monkeypatch.setattr(prometheus, "_matches_requested_profile", lambda: True)
    monkeypatch.setattr(prometheus, "_delete_retained_pvcs", MagicMock())

    prometheus.deploy()

    helm.install.assert_not_called()
    helm.uninstall.assert_not_called()
    prometheus._delete_retained_pvcs.assert_not_called()


def test_kind_teardown_leaves_the_claim_as_main_does(monkeypatch, helm):
    prometheus = Prometheus()
    cleanup = MagicMock()
    monkeypatch.setattr(prometheus, "_delete_retained_pvcs", cleanup)

    prometheus.teardown()

    helm.uninstall.assert_called_once_with(**prometheus.helm_configs)
    cleanup.assert_not_called()


def test_openshift_teardown_deletes_the_retained_claim(monkeypatch, helm, openshift):
    prometheus = Prometheus()
    cleanup = MagicMock()
    monkeypatch.setattr(prometheus, "_delete_retained_pvcs", cleanup)

    prometheus.teardown()

    helm.uninstall.assert_called_once_with(**prometheus.helm_configs)
    cleanup.assert_called_once_with()


def test_kind_loki_teardown_is_unchanged(helm):
    loki = Loki()
    loki.teardown()
    assert helm.uninstall.call_args_list == [
        call(**loki.helm_configs),
        call(release_name="promtail", namespace="observe"),
    ]
