"""Storage class selection: the selector, its validator, both entry points and its consumers.

The consumers are the OpenEBS block in ``Conductor.deploy_app`` (skipped for a named
class), the verification that replaces it, the cluster-default patch, and the Helm arguments
of the Prometheus and Loki releases.
"""

import importlib.util
import json
import logging
import re
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sregym import cluster_platform, profile, storage
from sregym.conductor import conductor as conductor_module
from sregym.conductor.conductor import Conductor
from sregym.paths import BASE_DIR
from sregym.service.helm import Helm
from sregym.service.telemetry import loki as loki_module
from sregym.service.telemetry import prometheus as prometheus_module
from sregym.service.telemetry.loki import Loki
from sregym.service.telemetry.prometheus import Prometheus
from sregym.storage import get_storage_class, set_storage_class, uses_openebs, validate_storage_class_name

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN = REPO_ROOT / "main.py"
VALIDATE_PROBLEM = REPO_ROOT / "tests" / "integration" / "validate_problem.py"


@pytest.fixture(autouse=True)
def reset_storage_class(monkeypatch):
    monkeypatch.setattr(storage, "_storage_class", None)
    monkeypatch.delenv("SREGYM_STORAGE_CLASS", raising=False)
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)
    monkeypatch.setattr(profile, "_profile", None)
    monkeypatch.delenv("SREGYM_PROFILE", raising=False)


class ReachedDeployment(Exception):
    """Raised in place of the first cluster-touching step, so a run stops there."""


def load_module(path, monkeypatch, name):
    """Load a script by path, the way tests/test_deployment_profiles.py loads main.py."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def main_benchmark_args(**overrides):
    """Arguments `_run_benchmark` reads before it reaches the cluster."""
    values = {
        "agent": None,
        "internet_access": "open",
        "container_hardening": "on",
        "profile": "full",
        "platform": "kind",
        "noise": False,
        "use_external_harness": True,
        "stages": None,
        "baseline": None,
        "propagation": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def stop_before_deployment(benchmark_main, monkeypatch):
    """Patch `_run_benchmark`'s surroundings so it stops at `ConductorConfig`."""
    monkeypatch.setattr(benchmark_main.os, "environ", benchmark_main.os.environ.copy())
    monkeypatch.setattr(benchmark_main, "_configure_model_environment", lambda args: ("unused", "unused"))
    monkeypatch.setattr(benchmark_main, "set_profile", lambda profile: None)

    def conductor_config(**_kwargs):
        raise ReachedDeployment

    monkeypatch.setattr(benchmark_main, "ConductorConfig", conductor_config)


# ----------------------------------------------------------------------- selector


def test_storage_class_defaults_to_the_openebs_class():
    assert get_storage_class() == "openebs-hostpath"
    assert get_storage_class() == storage.OPENebs
    assert uses_openebs() is True


def test_the_setter_wins_over_the_default():
    set_storage_class("gp3")

    assert get_storage_class() == "gp3"
    assert uses_openebs() is False


def test_the_environment_variable_is_the_fallback(monkeypatch):
    monkeypatch.setenv("SREGYM_STORAGE_CLASS", "gp3")

    assert get_storage_class() == "gp3"
    assert uses_openebs() is False


def test_the_setter_wins_over_the_environment_variable(monkeypatch):
    monkeypatch.setenv("SREGYM_STORAGE_CLASS", "standard")
    set_storage_class("gp3")

    assert get_storage_class() == "gp3"


@pytest.mark.parametrize(("name", "openebs"), [("openebs-hostpath", True), ("gp3", False), ("standard", False)])
def test_uses_openebs_is_true_only_for_the_openebs_class(name, openebs):
    set_storage_class(name)

    assert uses_openebs() is openebs


# ---------------------------------------------------------------------- validator


@pytest.mark.parametrize("name", ["gp3", "gp3-csi", "standard", "a", "a" + "b" * 251])
def test_valid_names_are_returned_unchanged(name):
    assert validate_storage_class_name(name) == name


def test_a_253_character_name_is_accepted():
    name = "a" * 253

    assert validate_storage_class_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "GP3",
        "-gp3",
        "gp3-",
        "gp3_csi",
        "gp3;curl x|sh",
        "gp3$(id)",
        "a" * 254,
        "gp3\n",
    ],
)
def test_invalid_names_are_refused_and_named_in_the_message(name):
    with pytest.raises(ValueError) as error:
        validate_storage_class_name(name)

    message = str(error.value)
    assert f"{name!r}" in message
    assert "DNS-1123" in message


# --------------------------------------------------------------------- main.py


def test_main_sets_the_storage_class_it_is_given(monkeypatch):
    benchmark_main = load_module(MAIN, monkeypatch, "sregym_main_for_storage_class_test")
    stop_before_deployment(benchmark_main, monkeypatch)

    with pytest.raises(ReachedDeployment):
        benchmark_main._run_benchmark(main_benchmark_args(storage_class="gp3"))

    assert storage.get_storage_class() == "gp3"


def test_main_refuses_an_invalid_storage_class_with_one_line(monkeypatch):
    benchmark_main = load_module(MAIN, monkeypatch, "sregym_main_for_storage_class_refusal_test")
    stop_before_deployment(benchmark_main, monkeypatch)

    with pytest.raises(SystemExit) as exit_info:
        benchmark_main._run_benchmark(main_benchmark_args(storage_class="gp3;curl x|sh"))

    message = exit_info.value.code
    assert isinstance(message, str)
    assert "\n" not in message
    assert "gp3;curl x|sh" in message
    assert storage.get_storage_class() == storage.OPENebs


def test_main_keeps_working_when_the_flag_is_absent(monkeypatch):
    # tests/test_main_campaign_abort.py builds args objects without the flag.
    benchmark_main = load_module(MAIN, monkeypatch, "sregym_main_for_missing_storage_class_test")
    stop_before_deployment(benchmark_main, monkeypatch)

    with pytest.raises(ReachedDeployment):
        benchmark_main._run_benchmark(main_benchmark_args())

    assert storage.get_storage_class() == storage.OPENebs


def test_the_main_parser_offers_the_storage_class_flag(monkeypatch, capsys):
    # The parser lives under `if __name__ == "__main__":`, so it is reached only by
    # running the script. --help exits before any run starts.
    monkeypatch.setattr(sys, "argv", ["main.py", "--help"])

    with pytest.raises(SystemExit):
        runpy.run_path(str(MAIN), run_name="__main__")

    assert "--storage-class" in capsys.readouterr().out


# --------------------------------------------------- tests/integration/validate_problem.py


def validate_problem_stages(module):
    return {"deploy": module.Stage("Deploy application")}


def test_validate_problem_records_the_storage_class_it_is_given(monkeypatch, tmp_path):
    module = load_module(VALIDATE_PROBLEM, monkeypatch, "sregym_validate_problem_for_storage_class_test")
    json_summary = tmp_path / "summary.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_problem.py",
            "--problem",
            "demo",
            "--storage-class",
            "gp3",
            "--summary",
            str(tmp_path / "summary.md"),
            "--json-summary",
            str(json_summary),
        ],
    )
    monkeypatch.setattr(module, "init_logger", lambda: None)
    monkeypatch.setattr(module, "set_profile", lambda profile: None)
    monkeypatch.setattr(module, "validate", lambda *args, **kwargs: (True, validate_problem_stages(module)))

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    assert exit_info.value.code == 0
    recorded = json.loads(json_summary.read_text())
    assert recorded["storage_class"] == "gp3"
    assert recorded["platform"] == "kind"


def test_validate_problem_refuses_an_invalid_storage_class_with_one_line(monkeypatch):
    module = load_module(VALIDATE_PROBLEM, monkeypatch, "sregym_validate_problem_for_storage_class_refusal_test")
    monkeypatch.setattr(sys, "argv", ["validate_problem.py", "--problem", "demo", "--storage-class", "gp3;curl x|sh"])
    monkeypatch.setattr(module, "init_logger", lambda: None)
    monkeypatch.setattr(module, "set_profile", lambda profile: None)

    def validate(*_args, **_kwargs):
        pytest.fail("validation must not start for an invalid storage class")

    monkeypatch.setattr(module, "validate", validate)

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    message = exit_info.value.code
    assert isinstance(message, str)
    assert "\n" not in message
    assert "gp3;curl x|sh" in message
    assert storage.get_storage_class() == storage.OPENebs


# --------------------------------------------------- the cluster's storage classes


def storage_class(name, *, provisioner="ebs.csi.aws.com", reclaim_policy="Delete"):
    """A StorageClass object as `kubectl get storageclass <name> -o json` returns it."""
    return {"metadata": {"name": name}, "provisioner": provisioner, "reclaimPolicy": reclaim_policy}


class Cluster:
    """Answer the storage-class reads and record every command the conductor issues."""

    def __init__(self, classes=(), defaults=()):
        self.commands = []
        self.items = []
        for item in classes:
            is_default = item["metadata"]["name"] in defaults
            annotations = {"storageclass.kubernetes.io/is-default-class": "true"} if is_default else {}
            self.items.append({**item, "metadata": {**item["metadata"], "annotations": annotations}})

    def exec_command(self, command, input_data=None):
        self.commands.append(command)
        if "get storageclass" not in command:
            return ""
        named = re.fullmatch(r"kubectl get storageclass (\S+) -o json.*", command)
        if named is None:
            return json.dumps({"items": self.items})
        selected = next((item for item in self.items if item["metadata"]["name"] == named.group(1)), None)
        return json.dumps(selected) if selected else ""

    def writes(self):
        return [command for command in self.commands if not command.startswith("kubectl get")]


def conductor_reading(cluster):
    """A bare Conductor whose only wired dependency is the cluster it reads."""
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.storage_class_selection")
    conductor.kubectl = MagicMock()
    conductor.kubectl.exec_command.side_effect = cluster.exec_command
    conductor.kubectl.exec_command_checked.return_value = ""
    return conductor


# ------------------------------------------------- deploy_app: the OpenEBS block gate


@pytest.fixture
def deployment():
    """A conductor whose deployment reaches every step around the OpenEBS block."""
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.storage_class_selection")
    conductor.kubectl = MagicMock()
    conductor.kubectl.assert_pod_networking = MagicMock()
    conductor.kubectl.exec_command_checked.return_value = ""
    conductor._baseline_captured = True
    conductor._ensure_metrics_api = MagicMock()
    conductor._openebs_ready = MagicMock(return_value=False)
    conductor._wait_for_infrastructure_ready = MagicMock()
    conductor._preflight_openebs_udev_mount = MagicMock()
    conductor._trim_openebs_ndm = MagicMock()
    conductor._preload_openebs_helper_image = MagicMock()
    conductor._ensure_openebs_device_storageclass = MagicMock()
    conductor.problem = SimpleNamespace(
        namespace="sregym-demo",
        app=SimpleNamespace(name="demo", namespace="sregym-demo", namespaces=["sregym-demo"], deploy=MagicMock()),
        run_default_workload=False,
    )
    conductor.prometheus = MagicMock()
    conductor.jaeger = MagicMock()
    conductor.otel_collector = MagicMock()
    conductor.loki = MagicMock()
    conductor.mcp_server = MagicMock()
    conductor.config = MagicMock(deploy_loki=True)
    return conductor


@pytest.fixture(autouse=True)
def full_profile(monkeypatch):
    monkeypatch.setattr(conductor_module, "is_svelte", lambda: False)


def openebs_cluster(**kwargs):
    return Cluster([storage_class(storage.OPENebs, provisioner="openebs.io/local")], **kwargs)


def attach(deployment, cluster):
    """Answer the deployment's kubectl calls from ``cluster``."""
    deployment.kubectl.exec_command.side_effect = cluster.exec_command
    return cluster


def test_the_openebs_class_keeps_the_whole_openebs_block(deployment, caplog):
    cluster = attach(deployment, openebs_cluster(defaults=[storage.OPENebs]))
    caplog.set_level(logging.INFO)

    deployment.deploy_app()

    assert "kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml" in cluster.commands
    assert any(command.startswith(f"kubectl patch storageclass {storage.OPENebs}") for command in cluster.commands)
    deployment.kubectl.apps_v1_api.patch_namespaced_daemon_set.assert_called_once_with(
        "openebs-ndm",
        "openebs",
        [{"op": "add", "path": "/spec/template/spec/nodeSelector", "value": {}}],
        _request_timeout=10,
    )
    deployment._preflight_openebs_udev_mount.assert_called_once_with()
    deployment._preload_openebs_helper_image.assert_called_once_with()
    deployment.kubectl.wait_for_ready.assert_called_once_with("openebs")
    deployment._wait_for_infrastructure_ready.assert_called_once()
    deployment._ensure_openebs_device_storageclass.assert_called_once_with()
    assert deployment.prometheus.deploy.call_count == 1
    assert not [
        message for message in (record.getMessage() for record in caplog.records) if "Skipping OpenEBS" in message
    ]
    # Nothing verified a named class: the OpenEBS class is installed and patched instead.
    assert not [command for command in cluster.commands if "--ignore-not-found" in command]


def test_a_named_class_skips_the_openebs_block_and_verifies_the_class(deployment, caplog):
    set_storage_class("gp3")
    cluster = attach(deployment, Cluster([storage_class("gp3")], defaults=["gp3"]))
    caplog.set_level(logging.INFO)

    deployment.deploy_app()

    assert not [command for command in cluster.commands if "openebs-operator.yaml" in command]
    assert not [command for command in cluster.commands if command.startswith("kubectl patch storageclass")]
    deployment._openebs_ready.assert_not_called()
    deployment._preflight_openebs_udev_mount.assert_not_called()
    deployment._trim_openebs_ndm.assert_not_called()
    deployment._preload_openebs_helper_image.assert_not_called()
    deployment._ensure_openebs_device_storageclass.assert_not_called()
    deployment.kubectl.apps_v1_api.patch_namespaced_daemon_set.assert_not_called()
    deployment.kubectl.wait_for_ready.assert_not_called()
    deployment.kubectl.exec_command_checked.assert_not_called()
    deployment._wait_for_infrastructure_ready.assert_not_called()
    deployment.prometheus.deploy.assert_called_once_with()
    deployment.loki.deploy.assert_called_once_with()
    skip_lines = [record.getMessage() for record in caplog.records if "Skipping OpenEBS" in record.getMessage()]
    assert len(skip_lines) == 1
    assert "gp3" in skip_lines[0]


def test_the_gate_verifies_the_class_before_deploying_the_observers(deployment):
    set_storage_class("gp3")
    cluster = attach(
        deployment,
        Cluster([storage_class(storage.OPENebs, provisioner="openebs.io/local")], defaults=[storage.OPENebs]),
    )

    with pytest.raises(RuntimeError, match="'gp3' does not exist"):
        deployment.deploy_app()

    deployment.prometheus.deploy.assert_not_called()
    assert not [command for command in cluster.writes() if "openebs" in command]


# ---------------------------------------------------- verify_storage_class, fail closed


def test_a_dynamic_default_class_passes_verification(caplog):
    set_storage_class("gp3")
    conductor = conductor_reading(Cluster([storage_class("gp3")], defaults=["gp3"]))
    caplog.set_level(logging.INFO)

    conductor.verify_storage_class()

    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_a_missing_class_is_refused():
    set_storage_class("gp3")
    conductor = conductor_reading(Cluster([storage_class(storage.OPENebs, provisioner="openebs.io/local")]))

    with pytest.raises(RuntimeError, match="'gp3' does not exist on this cluster") as error:
        conductor.verify_storage_class()

    assert "must exist on this cluster" in str(error.value)


@pytest.mark.parametrize("provisioner", ["kubernetes.io/no-provisioner", None, ""])
def test_a_class_without_a_dynamic_provisioner_is_refused(provisioner):
    set_storage_class("gp3")
    class_entry = storage_class("gp3")
    if provisioner is None:
        class_entry.pop("provisioner")
    else:
        class_entry["provisioner"] = provisioner
    conductor = conductor_reading(Cluster([class_entry], defaults=["gp3"]))

    with pytest.raises(RuntimeError, match="dynamically provisioned class is required"):
        conductor.verify_storage_class()


def test_a_non_default_class_is_refused_naming_the_default_and_the_flag():
    set_storage_class("gp3")
    conductor = conductor_reading(Cluster([storage_class("gp3"), storage_class("gp3-csi")], defaults=["gp3-csi"]))

    with pytest.raises(RuntimeError) as error:
        conductor.verify_storage_class()

    message = str(error.value)
    assert "cannot be used" in message
    assert "gp3-csi" in message
    assert "--storage-class gp3-csi" in message


def test_no_default_class_is_refused_naming_the_cluster_state():
    set_storage_class("gp3")
    conductor = conductor_reading(Cluster([storage_class("gp3")]))

    with pytest.raises(RuntimeError) as error:
        conductor.verify_storage_class()

    message = str(error.value)
    assert "cannot be used" in message
    assert "no storage class is the cluster default" in message


def test_two_default_classes_are_refused_even_when_one_is_the_requested_class():
    # A claim that names no class binds through whichever default the cluster picks,
    # so two defaults are refused with advice that does not send the operator in a circle.
    set_storage_class("gp3")
    conductor = conductor_reading(Cluster([storage_class("gp3"), storage_class("other")], defaults=["gp3", "other"]))

    with pytest.raises(RuntimeError) as raised:
        conductor.verify_storage_class()

    message = str(raised.value)
    assert "2 storage classes are the cluster default" in message
    assert "remove the default annotation from the other 1" in message
    assert "pass --storage-class" not in message


def test_a_retain_class_only_warns(caplog):
    set_storage_class("gp3")
    conductor = conductor_reading(Cluster([storage_class("gp3", reclaim_policy="Retain")], defaults=["gp3"]))
    caplog.set_level(logging.WARNING)

    conductor.verify_storage_class()

    warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "Retain" in warnings[0]


# ------------------------------------------------ the openebs-hostpath default patch

# `main`'s command, verbatim. The kind branch must issue exactly this and nothing else.
KIND_DEFAULT_PATCH = (
    "kubectl patch storageclass openebs-hostpath "
    '-p \'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
)


@pytest.mark.parametrize("defaults", [[], ["gp3-csi"], [storage.OPENebs]])
def test_kind_patches_openebs_hostpath_as_default_as_main_does(monkeypatch, defaults):
    """On kind the patch is unconditional and reads nothing first, as on `main`."""
    monkeypatch.setenv("SREGYM_PLATFORM", cluster_platform.KIND)
    cluster = Cluster(
        [storage_class(storage.OPENebs, provisioner="openebs.io/local"), storage_class("gp3-csi")], defaults=defaults
    )
    conductor = conductor_reading(cluster)

    conductor._make_openebs_hostpath_default()

    assert cluster.commands == [KIND_DEFAULT_PATCH]


def test_openshift_leaves_the_cluster_default_storage_class_alone(monkeypatch):
    monkeypatch.setenv("SREGYM_PLATFORM", cluster_platform.OPENSHIFT)
    cluster = Cluster(
        [storage_class(storage.OPENebs, provisioner="openebs.io/local"), storage_class("gp3-csi")], defaults=["gp3-csi"]
    )
    conductor = conductor_reading(cluster)
    conductor.logger = MagicMock()

    conductor._make_openebs_hostpath_default()

    assert cluster.commands == []
    conductor.logger.info.assert_called_once()
    assert "Not touching the cluster's default storage class" in conductor.logger.info.call_args.args[0]


# ------------------------------------------------------- Prometheus and Loki Helm args


def deploy_prometheus(monkeypatch, selected_profile):
    profile.set_profile(selected_profile)
    prometheus = Prometheus()
    monkeypatch.setattr(prometheus, "_is_prometheus_running", lambda: False)
    monkeypatch.setattr(prometheus, "_wait_for_namespace_termination", lambda: None)
    monkeypatch.setattr(prometheus, "_delete_retained_pvcs", MagicMock())
    helm = MagicMock(spec=Helm)
    monkeypatch.setattr(prometheus_module, "Helm", helm)
    prometheus.deploy()
    return helm.install.call_args.kwargs.get("extra_args", [])


@pytest.mark.parametrize("selected_profile", ["full", "svelte"])
def test_prometheus_passes_the_named_class_and_nothing_for_openebs(monkeypatch, selected_profile):
    svelte_overlay = ["-f", str(BASE_DIR / "observer" / "prometheus" / "values-svelte.yaml")]
    profile_args = svelte_overlay if selected_profile == "svelte" else []

    set_storage_class("gp3")
    assert deploy_prometheus(monkeypatch, selected_profile) == [
        *profile_args,
        "--set-string",
        "server.persistentVolume.storageClass=gp3",
    ]

    # The vendored values file names openebs-hostpath, so the `kind` install is unchanged.
    set_storage_class(storage.OPENebs)
    assert deploy_prometheus(monkeypatch, selected_profile) == profile_args


def deploy_loki(monkeypatch):
    loki = Loki()
    monkeypatch.setattr(loki, "_is_loki_running", lambda: False)
    monkeypatch.setattr(loki, "_add_grafana_helm_repo", lambda: None)
    monkeypatch.setattr(loki, "_deploy_promtail", lambda: None)
    monkeypatch.setattr(loki, "_delete_retained_pvcs", MagicMock())
    helm = MagicMock(spec=Helm)
    monkeypatch.setattr(loki_module, "Helm", helm)
    loki.deploy()
    return loki.helm_configs["extra_args"], helm.install.call_args.kwargs["extra_args"]


def test_loki_passes_the_named_class_and_nothing_for_openebs(monkeypatch):
    set_storage_class("gp3")
    configured, installed_args = deploy_loki(monkeypatch)
    assert installed_args == [*configured, "--set-string", "singleBinary.persistence.storageClass=gp3"]

    # The vendored values file names openebs-hostpath, so the `kind` install is unchanged.
    set_storage_class(storage.OPENebs)
    configured, installed_args = deploy_loki(monkeypatch)
    assert installed_args == configured
