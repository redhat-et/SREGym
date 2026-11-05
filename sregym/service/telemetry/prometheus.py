import json
import logging
import subprocess

from sregym.paths import BASE_DIR, PROMETHEUS_METADATA
from sregym.profile import is_svelte
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl
from sregym.service.rollout import deployment_rollout_complete
from sregym.service.telemetry.release_pvcs import delete_retained_pvcs
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

    def _requested_helm_configs(self) -> dict:
        """The Helm config of this deployment: the metadata config plus profile and class."""
        helm_configs = dict(self.helm_configs)
        extra_args = list(helm_configs.get("extra_args", []))
        if is_svelte():
            svelte_values = BASE_DIR / "observer" / "prometheus" / "values-svelte.yaml"
            extra_args += ["-f", str(svelte_values)]
            self.logger.info("[svelte] Dropping Alertmanager and Pushgateway; retention 15d -> 2h")
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
        """Teardown the metric collector deployment."""
        Helm.uninstall(**self.helm_configs)

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
