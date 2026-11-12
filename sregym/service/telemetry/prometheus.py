import json
import logging
import subprocess
from pathlib import Path

import yaml

from sregym.cluster_platform import is_openshift
from sregym.paths import BASE_DIR, PROMETHEUS_METADATA
from sregym.profile import is_svelte
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl
from sregym.service.rollout import deployment_rollout_complete
from sregym.service.telemetry.release_pvcs import delete_retained_pvcs, use_main_release_handling
from sregym.storage import get_storage_class, uses_openebs


class Prometheus:
    def __init__(self):
        self.config_file = PROMETHEUS_METADATA
        self.name = None
        self.namespace = None
        self.helm_configs = {}

        self.logger = logging.getLogger("all.infra.prometheus")
        self.logger.propagate = True
        self.logger.setLevel(logging.DEBUG)

        self.load_service_json()

    def load_service_json(self):
        """Load metric service metadata into attributes."""
        with open(self.config_file) as file:
            metadata = json.load(file)

        self.name = metadata.get("Name")
        self.namespace = metadata.get("Namespace")

        self.helm_configs = metadata.get("Helm Config", {})

        self.name = metadata["Name"]
        self.namespace = metadata["Namespace"]
        if "Helm Config" in metadata:
            self.helm_configs = metadata["Helm Config"]
            if "chart_path" in self.helm_configs:
                chart_path = self.helm_configs["chart_path"]
                self.helm_configs["chart_path"] = str(BASE_DIR / chart_path)

    def get_service_json(self) -> dict:
        """Get metric service metadata in JSON format."""
        with open(self.config_file) as file:
            return json.load(file)

    def get_service_summary(self) -> str:
        """Get a summary of the metric service metadata."""
        service_json = self.get_service_json()
        service_name = service_json.get("Name", "")
        namespace = service_json.get("Namespace", "")
        desc = service_json.get("Desc", "")
        supported_operations = service_json.get("Supported Operations", [])
        operations_str = "\n".join([f"  - {op}" for op in supported_operations])

        return (
            f"Telemetry Service Name: {service_name}\n"
            f"Namespace: {namespace}\n"
            f"Description: {desc}\n"
            f"Supported Operations:\n{operations_str}"
        )

    def deploy(self):
        """Deploy the metric collector using Helm."""
        if use_main_release_handling():
            self._deploy_as_on_main()
            return

        helm_configs = self._requested_helm_configs()
        if self._is_prometheus_running() and Helm.release_matches_requested_values(helm_configs):
            self.logger.info("Prometheus runs the requested values. Skipping redeployment.")
            return

        # Wait for namespace to be fully terminated before attempting fresh install
        self._wait_for_namespace_termination()

        Helm.uninstall(**self.helm_configs)
        self._delete_retained_pvcs()

        Helm.install(**helm_configs)
        Helm.record_values_fingerprint(helm_configs)
        Helm.assert_if_deployed(self.namespace)

    def _deploy_as_on_main(self):
        """The kind deploy, call for call as `main` makes it: no fingerprint."""
        if self._is_prometheus_running() and self._matches_requested_profile():
            self.logger.info("Prometheus already uses the requested profile. Skipping redeployment.")
            return

        # Wait for namespace to be fully terminated before attempting fresh install
        self._wait_for_namespace_termination()

        Helm.uninstall(**self.helm_configs)
        self._delete_retained_pvcs()

        helm_configs = dict(self.helm_configs)
        if is_svelte():
            svelte_values = BASE_DIR / "observer" / "prometheus" / "values-svelte.yaml"
            helm_configs["extra_args"] = [*helm_configs.get("extra_args", []), "-f", str(svelte_values)]
            self.logger.info("[svelte] Dropping Alertmanager and Pushgateway; retention 15d -> 2h")

        Helm.install(**helm_configs)
        Helm.assert_if_deployed(self.namespace)

    def _matches_requested_profile(self) -> bool:
        """Compare installed Helm values with the settings controlled by the profile."""
        result = subprocess.run(
            [
                "helm",
                "get",
                "values",
                self.helm_configs["release_name"],
                "-n",
                self.namespace,
                "--all",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        installed = json.loads(result.stdout)
        with open(Path(self.helm_configs["chart_path"]) / "values.yaml") as file:
            expected = yaml.safe_load(file)
        # Compare actual values rather than a new marker, so releases installed
        # before profile tracking also work. Other Helm values remain unrelated.
        with open(BASE_DIR / "observer" / "prometheus" / "values-svelte.yaml") as file:
            svelte_values = yaml.safe_load(file)
        for component, settings in svelte_values.items():
            for setting, svelte_value in settings.items():
                wanted = svelte_value if is_svelte() else expected[component][setting]
                if installed.get(component, {}).get(setting) != wanted:
                    self.logger.info("Prometheus profile differs at %s.%s; redeploying", component, setting)
                    return False
        return True

    def _requested_helm_configs(self) -> dict:
        """The Helm config of this deployment: the metadata config plus profile and class."""
        helm_configs = dict(self.helm_configs)
        extra_args = list(helm_configs.get("extra_args", []))
        if is_svelte():
            svelte_values = BASE_DIR / "observer" / "prometheus" / "values-svelte.yaml"
            extra_args += ["-f", str(svelte_values)]
            self.logger.info("[svelte] Dropping Alertmanager and Pushgateway; retention 15d -> 2h")
        if is_openshift():
            openshift_values = BASE_DIR / "observer" / "prometheus" / "values-openshift.yaml"
            extra_args += ["-f", str(openshift_values)]
            self.logger.info("[openshift] Dropping the control-plane node pins and node-exporter")
        if not uses_openebs():
            # The vendored values file names openebs-hostpath, which is the `kind`
            # render input. Any other class is passed over it, unchanged.
            extra_args += ["--set-string", f"server.persistentVolume.storageClass={get_storage_class()}"]
        if extra_args:
            helm_configs["extra_args"] = extra_args
        return helm_configs

    def _delete_retained_pvcs(self) -> None:
        """Remove release PVCs that Helm intentionally retains on uninstall."""
        delete_retained_pvcs(self.namespace, self.helm_configs["release_name"])

    def teardown(self):
        """Teardown the metric collector deployment and the volume Helm deliberately keeps.

        `helm uninstall` removes the StatefulSet and leaves its claim, because the chart asks for
        `persistentVolumeReclaimPolicy: Retain`. Without the second step an 8 GiB volume survives
        every teardown, which is what the first campaign left behind.
        """
        Helm.uninstall(**self.helm_configs)
        # On kind the claim is left as `main` leaves it: the next deploy deletes it before installing.
        if not use_main_release_handling():
            self._delete_retained_pvcs()

    def _wait_for_namespace_termination(self):
        """Wait for namespace to be fully deleted if it's currently in Terminating state."""
        result = subprocess.run(
            f"kubectl get namespace {self.namespace} -o jsonpath='{{.status.phase}}' 2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() == "Terminating":
            self.logger.info(f"Namespace '{self.namespace}' is terminating, waiting for full deletion...")
            KubeCtl().wait_for_namespace_deletion(self.namespace)

    def _is_prometheus_running(self) -> bool:
        """Require a current server rollout as well as Ready release pods."""
        release_name = self.helm_configs["release_name"]
        command = (
            f"kubectl get pods,deployments,statefulsets -n {self.namespace} "
            f"-l app.kubernetes.io/instance={release_name} -o json"
        )
        try:
            resources = json.loads(KubeCtl().exec_command(command)).get("items", [])
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return False

        pods = [resource for resource in resources if resource.get("kind") == "Pod"]
        servers = [
            resource
            for resource in resources
            if resource.get("kind") in {"Deployment", "StatefulSet"}
            and resource.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == "prometheus"
        ]
        server_pods = [
            pod
            for pod in pods
            if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == "prometheus"
        ]
        return (
            bool(server_pods)
            and bool(servers)
            and all(self._server_rollout_complete(server) for server in servers)
            and all(
                not pod.get("metadata", {}).get("deletionTimestamp")
                and any(
                    condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in pod.get("status", {}).get("conditions", [])
                )
                for pod in pods
            )
        )

    @staticmethod
    def _server_rollout_complete(server: dict) -> bool:
        if server["kind"] == "Deployment":
            return deployment_rollout_complete(server)

        metadata = server.get("metadata", {})
        status = server.get("status", {})
        desired = server.get("spec", {}).get("replicas", 1)
        generation = metadata.get("generation")
        observed = status.get("observedGeneration")
        return (
            not metadata.get("deletionTimestamp")
            and desired > 0
            and generation is not None
            and observed is not None
            and observed >= generation
            and all(status.get(field, 0) == desired for field in ("replicas", "updatedReplicas", "readyReplicas"))
            and bool(status.get("updateRevision"))
            and status.get("currentRevision") == status["updateRevision"]
        )
