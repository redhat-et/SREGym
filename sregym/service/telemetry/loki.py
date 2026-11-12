import json
import logging
import subprocess

from sregym.cluster_platform import is_openshift
from sregym.paths import BASE_DIR, LOKI_METADATA
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl
from sregym.service.telemetry.release_pvcs import delete_retained_pvcs
from sregym.storage import get_storage_class, uses_openebs


class Loki:
    def __init__(self):
        self.config_file = LOKI_METADATA
        self.name = None
        self.namespace = None
        self.helm_configs = {}
        self.promtail_release_name = "promtail"
        self.promtail_values_file = str(BASE_DIR / "observer/loki/promtail-values.yaml")

        self.logger = logging.getLogger("all.infra.loki")
        self.logger.propagate = True
        self.logger.setLevel(logging.DEBUG)

        self.load_service_json()

    def load_service_json(self):
        """Load Loki service metadata into attributes."""
        with open(self.config_file) as file:
            metadata = json.load(file)

        self.name = metadata.get("Name")
        self.namespace = metadata.get("Namespace")

        self.helm_configs = metadata.get("Helm Config", {})

        self.name = metadata["Name"]
        self.namespace = metadata["Namespace"]
        if "Helm Config" in metadata:
            self.helm_configs = metadata["Helm Config"]
            # Handle remote charts differently - don't prepend BASE_DIR
            if not self.helm_configs.get("remote_chart", False):
                if "chart_path" in self.helm_configs:
                    chart_path = self.helm_configs["chart_path"]
                    self.helm_configs["chart_path"] = str(BASE_DIR / chart_path)

            # Resolve extra_args paths relative to BASE_DIR
            if "extra_args" in self.helm_configs:
                extra_args = self.helm_configs["extra_args"]
                resolved_args = []
                for i, arg in enumerate(extra_args):
                    if i > 0 and extra_args[i - 1] == "-f":
                        # This is a values file path, resolve it
                        resolved_args.append(str(BASE_DIR / arg))
                    else:
                        resolved_args.append(arg)
                self.helm_configs["extra_args"] = resolved_args

    def get_service_json(self) -> dict:
        """Get Loki service metadata in JSON format."""
        with open(self.config_file) as file:
            return json.load(file)

    def get_service_summary(self) -> str:
        """Get a summary of the Loki service metadata."""
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
        """Deploy Loki using Helm."""
        helm_configs = self._requested_helm_configs()
        if self._is_loki_running() and Helm.release_matches_requested_values(helm_configs):
            self.logger.info("Loki runs the requested values. Reusing the release.")
            self._deploy_promtail()
            return

        self.logger.info("Loki does not run the requested values. Redeploying the release.")
        Helm.uninstall(**self.helm_configs)
        self._delete_retained_pvcs()

        # Add Grafana Helm repo for Loki chart
        self._add_grafana_helm_repo()

        Helm.install(**helm_configs)
        Helm.record_values_fingerprint(helm_configs)
        Helm.assert_if_deployed(self.namespace)
        self._deploy_promtail()

    def _requested_helm_configs(self) -> dict:
        """The Helm config of this deployment: the metadata config plus the requested class."""
        helm_configs = dict(self.helm_configs)
        extra_args = list(helm_configs.get("extra_args", []))
        if is_openshift():
            # The overlay carries the node pins and the gateway, so a platform
            # switch changes the fingerprint and forces the redeploy.
            extra_args += ["-f", str(BASE_DIR / "observer/loki/values-openshift.yaml")]
            self.logger.info("[openshift] Dropping the control-plane node pins and the gateway")
        if not uses_openebs():
            # The vendored values file names openebs-hostpath, which is the `kind`
            # render input. Any other class is passed over it, unchanged.
            extra_args += ["--set-string", f"singleBinary.persistence.storageClass={get_storage_class()}"]
        if extra_args:
            helm_configs["extra_args"] = extra_args
        return helm_configs

    def _delete_retained_pvcs(self) -> None:
        """Remove Loki PVCs that Helm intentionally retains on uninstall."""
        delete_retained_pvcs(self.namespace, self.helm_configs["release_name"])

    def _add_grafana_helm_repo(self):
        """Add Grafana Helm repository for Loki chart."""
        self.logger.info("Adding Grafana Helm repository...")
        try:
            KubeCtl().exec_command("helm repo add grafana https://grafana.github.io/helm-charts")
            KubeCtl().exec_command("helm repo update")
        except Exception as e:
            self.logger.warning(f"Failed to add Grafana Helm repo (may already exist): {e}")

    def teardown(self):
        """Teardown the Loki deployment."""
        Helm.uninstall(**self.helm_configs)
        Helm.uninstall(release_name=self.promtail_release_name, namespace=self.namespace)

    def _is_loki_running(self) -> bool:
        """Check if Loki is already running in the cluster."""
        command = f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=loki"
        try:
            result = KubeCtl().exec_command(command)
            if "Running" in result:
                return True
        except subprocess.CalledProcessError:
            return False
        return False

    def _deploy_promtail(self):
        helm_configs = self._promtail_helm_configs()
        if self._is_promtail_running() and Helm.release_matches_requested_values(helm_configs):
            self.logger.info("Promtail runs the requested values. Skipping redeployment.")
            return

        # A release record that is not running makes `helm install` fail with "cannot
        # reuse a name that is still in use", which fails the deploy of every scenario:
        # a cluster that refused Promtail's pods once could not run a second scenario at
        # all. Promtail's DaemonSet is not running for two reasons on a managed cluster,
        # the values changed or no security context constraint admits its pods, and only
        # an uninstalled release is installable again. `uninstall` is a no-op when no
        # release exists, so a first run is unchanged.
        self.logger.info("Promtail does not run the requested values. Reinstalling the release.")
        Helm.uninstall(release_name=self.promtail_release_name, namespace=self.namespace)

        self.logger.info("Deploying Promtail for Loki log collection...")
        Helm.install(**helm_configs)
        Helm.record_values_fingerprint(helm_configs)
        Helm.assert_if_deployed(self.namespace)

    def _promtail_helm_configs(self) -> dict:
        """The Helm config of this deployment: the Promtail chart and its values file."""
        extra_args = ["-f", self.promtail_values_file]
        if is_openshift():
            extra_args += ["-f", str(BASE_DIR / "observer/loki/promtail-values-openshift.yaml")]
        return {
            "release_name": self.promtail_release_name,
            "chart_path": "grafana/promtail",
            "namespace": self.namespace,
            "remote_chart": True,
            "extra_args": extra_args,
        }

    def _is_promtail_running(self) -> bool:
        """Whether at least one Promtail pod is Running *and* ready.

        Readiness is part of the question, not just the pod phase: a pod in `CrashLoopBackOff` or one
        that runs and never becomes ready shows `Running` in the STATUS column while ingesting nothing,
        and a release in that state must be reinstalled rather than reused. Measured on ROSA 4.22.18: a
        Promtail admitted by a too-narrow SCC is exactly that pod.
        """
        command = f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=promtail"
        try:
            result = KubeCtl().exec_command(command)
        except subprocess.CalledProcessError:
            return False
        for line in result.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 3:
                continue
            ready, status = fields[1], fields[2]
            containers = ready.split("/")
            if status == "Running" and len(containers) == 2 and containers[0] == containers[1] != "0":
                return True
        return False
