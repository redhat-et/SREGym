import json
import logging
from pathlib import Path
from typing import Protocol

from sregym.cluster_platform import is_openshift
from sregym.paths import TARGET_MICROSERVICES

PLATFORM_VALUES_DIR = Path(__file__).with_name("values")


class WorkloadApplication(Protocol):
    """Optional workload contract for applications that provide background traffic."""

    def create_workload(self, **kwargs) -> None: ...

    def start_workload(self) -> None: ...

    def stop_workload(self) -> None: ...


class Application:
    """Base class for all microservice applications."""

    def _validated_path(self, path, label: str) -> Path:
        """Resolve *path* and raise FileNotFoundError with a clear message if it doesn't exist."""
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"{label} does not exist: {resolved}")
        return resolved

    def __init__(self, config_file: str | Path):
        self.config_file = self._validated_path(config_file, "config_file")
        self.name = None
        self.namespace = None
        self.helm_deploy = True
        self.helm_configs = {}
        self.k8s_deploy_path = None
        self.logger = logging.getLogger("all.application")
        self.logger.propagate = True
        self.logger.setLevel(logging.DEBUG)

    # Stem of the platform overlay file: ``values/<key>-openshift.yaml``. None means no overlay.
    platform_overlay_key: str | None = None

    def platform_helm_configs(self) -> dict:
        """Return a copy of ``helm_configs`` with the platform overlay added, if one exists.

        On OpenShift, ``values/<platform_overlay_key>-openshift.yaml`` is appended to
        ``extra_args`` as a ``-f`` file. With no key, no file, or another platform the
        result equals ``helm_configs``. The overlay file is the whole registry: adding
        one is the fix for an application that does not run unchanged on the platform.
        """
        configs = dict(self.helm_configs)
        if not (is_openshift() and self.platform_overlay_key):
            return configs
        overlay = PLATFORM_VALUES_DIR / f"{self.platform_overlay_key}-openshift.yaml"
        if not overlay.is_file():
            return configs
        extra = configs.get("extra_args") or []
        if isinstance(extra, str):
            extra = [extra]
        configs["extra_args"] = [*extra, "-f", str(overlay)]
        self.logger.info(f"[openshift] Applying platform values {overlay.name} to {self.name}")
        return configs

    def deploy(self) -> None:
        """Deploy the application and require its expected healthy state."""
        raise NotImplementedError(f"{type(self).__name__} does not implement deployment")

    def load_app_json(self):
        """Load (basic) application metadata into attributes.

        # NOTE: override this method to load additional attributes!
        """
        with open(self.config_file) as file:
            metadata = json.load(file)

        self.name = metadata["Name"]
        self.namespace = metadata["Namespace"]
        if "Helm Config" in metadata:
            self.helm_configs = metadata["Helm Config"]
            chart_path = self.helm_configs.get("chart_path")

            if chart_path and not self.helm_configs.get("remote_chart", False):
                self.helm_configs["chart_path"] = str(
                    self._validated_path(TARGET_MICROSERVICES / chart_path, "Helm chart_path")
                )

        if "K8S Deploy Path" in metadata:
            self.k8s_deploy_path = self._validated_path(
                TARGET_MICROSERVICES / metadata["K8S Deploy Path"], "K8S deploy path"
            )

    def get_app_json(self) -> dict:
        """Get application metadata in JSON format.

        Returns:
            dict: application metadata
        """
        with open(self.config_file) as file:
            app_json = json.load(file)
        return app_json

    def get_app_summary(self) -> str:
        """Get a summary of the application metadata in string format.
        NOTE: for human and LLM-readable summaries!

        Returns:
            str: application metadata
        """
        app_json = self.get_app_json()
        app_name = app_json.get("Name", "")
        namespace = app_json.get("Namespace", "")
        desc = app_json.get("Desc", "")
        supported_operations = app_json.get("Supported Operations", [])
        operations_str = "\n".join([f"  - {op}" for op in supported_operations])

        description = f"App Name: {app_name}\nNamespace: {namespace}\nDescription: {desc}\nSupported Operations:\n{operations_str}"

        return description

    def create_namespace(self):
        """Create the namespace for the application if it doesn't exist."""
        result = self.kubectl.exec_command(f"kubectl get namespace {self.namespace} --ignore-not-found -o name")
        if not result.strip():
            self.logger.info(f"Namespace {self.namespace} not found. Creating namespace.")
            create_namespace_command = f"kubectl create namespace {self.namespace}"
            create_result = self.kubectl.exec_command(create_namespace_command)
            self.logger.info(f"Namespace {self.namespace} created successfully: {create_result}")
        else:
            self.logger.info(f"Namespace {self.namespace} already exists.")

    def cleanup(self):
        """Delete the entire namespace for the application."""
        self.kubectl.delete_namespace(self.namespace)
