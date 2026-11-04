import contextlib
import json
import logging
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import yaml

from sregym.generators.workload.wrk2 import Wrk2, Wrk2WorkloadManager
from sregym.paths import FAULT_SCRIPTS, HOTEL_RES_METADATA, TARGET_MICROSERVICES
from sregym.service.apps.base import Application
from sregym.service.apps.helpers import get_frontend_url
from sregym.service.kubectl import KubeCtl
from sregym.storage import uses_openebs

logger = logging.getLogger("all.application")
logger.propagate = True
logger.setLevel(logging.DEBUG)

HOTEL_RESERVATION_APPLICATION_IMAGE = (
    "ghcr.io/sregym/hotel-reservation:sha-d2c036bc3d1138f5a0bdaffd3d87f37522c3461a"
    "@sha256:1c685e1c4c304f327952397b3a99c54dadb086607774d71f222b200ec0d2e65e"
)

# A storage driver deletes the volume behind a PersistentVolume only after the claims
# in the namespace are gone, and cleanup must not race it. Cleanup re-reads the PVs for
# this long before it reports what the driver has not finished. It is small because it
# runs once per problem, and bounded so a slow driver cannot hold up the next one.
PV_RELEASE_WAIT_SECONDS = 10
PV_RELEASE_POLL_SECONDS = 2


class HotelReservation(Application):
    def __init__(
        self,
        mount_failure_scripts: bool = True,
        deployment_env_overrides: dict[str, dict[str, dict[str, str]]] | None = None,
    ):
        super().__init__(HOTEL_RES_METADATA)
        self.kubectl = KubeCtl()
        self.script_dir = FAULT_SCRIPTS
        self.helm_deploy = False
        self.mount_failure_scripts = mount_failure_scripts
        self.deployment_env_overrides = deployment_env_overrides or {}

        self.load_app_json()

        self.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )

    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]
        self.frontend_service = metadata.get("frontend_service", "frontend")
        self.frontend_port = metadata.get("frontend_port", 5000)

    # Script file lists for failure configmaps (used by populate/clear lifecycle)
    FAILURE_ADMIN_RATE_SCRIPTS = [
        "revoke-admin-rate-mongo.sh",
        "revoke-mitigate-admin-rate-mongo.sh",
        "remove-admin-mongo.sh",
        "remove-mitigate-admin-rate-mongo.sh",
    ]
    FAILURE_ADMIN_GEO_SCRIPTS = [
        "revoke-admin-geo-mongo.sh",
        "revoke-mitigate-admin-geo-mongo.sh",
        "remove-admin-mongo.sh",
        "remove-mitigate-admin-geo-mongo.sh",
    ]

    def create_configmaps(self):
        """Create configmaps for the hotel reservation application.

        Note: failure-admin-{geo,rate} are created as standalone K8s objects (not mounted
        into pods). They serve as noise/red herrings for unrelated faults.
        """
        self.kubectl.create_or_update_configmap(
            name="mongo-rate-script",
            namespace=self.namespace,
            data=self._prepare_configmap_data(["k8s-rate-mongo.sh"]),
        )

        self.kubectl.create_or_update_configmap(
            name="mongo-geo-script",
            namespace=self.namespace,
            data=self._prepare_configmap_data(["k8s-geo-mongo.sh"]),
        )

    def populate_failure_configmaps(self):
        """Create/fill failure-admin-{geo,rate} configmaps with fault scripts (noise)."""
        self.kubectl.create_or_update_configmap(
            name="failure-admin-rate",
            namespace=self.namespace,
            data=self._prepare_configmap_data(self.FAILURE_ADMIN_RATE_SCRIPTS),
        )
        self.kubectl.create_or_update_configmap(
            name="failure-admin-geo",
            namespace=self.namespace,
            data=self._prepare_configmap_data(self.FAILURE_ADMIN_GEO_SCRIPTS),
        )

    def clear_failure_configmaps(self):
        """Empty failure-admin-{geo,rate} configmaps (best-effort).

        Removes all script data so agents cannot read fault details.
        """
        try:
            self.kubectl.create_or_update_configmap(
                name="failure-admin-rate",
                namespace=self.namespace,
                data={},
            )
            self.kubectl.create_or_update_configmap(
                name="failure-admin-geo",
                namespace=self.namespace,
                data={},
            )
        except Exception as e:
            logger.warning(f"Best-effort clearing of failure configmaps failed: {e}")

    def _patch_mongo_failure_script_mounts(self):
        """Patch mongodb-geo and mongodb-rate deployments to mount failure configmaps at /scripts.

        Uses JSON patch (append-only) to add the volume and volumeMount without
        needing to re-specify existing volumes — safer if the YAML changes.
        """
        patches = [
            ("mongodb-geo", "failure-admin-geo"),
            ("mongodb-rate", "failure-admin-rate"),
        ]
        for deployment_name, configmap_name in patches:
            patch_cmd = (
                f"kubectl patch deployment {deployment_name} -n {self.namespace} --type=json -p="
                "'["
                f'{{"op":"add","path":"/spec/template/spec/volumes/-",'
                f'"value":{{"name":"failure-script","configMap":{{"name":"{configmap_name}"}}}}}}'
                f',{{"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-",'
                f'"value":{{"name":"failure-script","mountPath":"/scripts"}}}}'
                "]'"
            )
            self.kubectl.exec_command(patch_cmd)

    @contextlib.contextmanager
    def _rendered_deployment_configs(self) -> Iterator[Path]:
        """Render problem-specific env values before Kubernetes sees a Deployment.

        Most problems use the application manifests unchanged. A problem that
        needs a different, initially healthy runtime policy can provide exact
        Deployment/container overrides. Rendering a temporary manifest tree
        avoids a setup rollout and its misleading ReplicaSet history.
        """
        if not self.deployment_env_overrides:
            yield Path(self.k8s_deploy_path)
            return

        with tempfile.TemporaryDirectory(prefix="hotel-reservation-config-") as temporary_dir:
            rendered_path = Path(temporary_dir) / "kubernetes"
            shutil.copytree(self.k8s_deploy_path, rendered_path)
            unmatched = {
                (deployment_name, container_name)
                for deployment_name, containers in self.deployment_env_overrides.items()
                for container_name in containers
            }

            for config_path in [*rendered_path.rglob("*.yaml"), *rendered_path.rglob("*.yml")]:
                with config_path.open() as config_file:
                    documents = list(yaml.safe_load_all(config_file))

                changed = False
                for document in documents:
                    if not isinstance(document, dict) or document.get("kind") != "Deployment":
                        continue
                    deployment_name = document.get("metadata", {}).get("name")
                    container_overrides = self.deployment_env_overrides.get(deployment_name)
                    if not container_overrides:
                        continue

                    containers = document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                    for container in containers:
                        container_name = container.get("name")
                        values = container_overrides.get(container_name)
                        if not values:
                            continue
                        existing = [item for item in container.get("env", []) if item.get("name") not in values]
                        container["env"] = [
                            *existing,
                            *({"name": name, "value": str(value)} for name, value in values.items()),
                        ]
                        unmatched.discard((deployment_name, container_name))
                        changed = True

                if changed:
                    with config_path.open("w") as config_file:
                        yaml.safe_dump_all(documents, config_file, sort_keys=False)

            if unmatched:
                targets = ", ".join(
                    f"deployment/{deployment}:{container}" for deployment, container in sorted(unmatched)
                )
                raise RuntimeError(f"deployment environment override targets were not found: {targets}")

            yield rendered_path

    def deploy(self):
        """Deploy the Kubernetes configurations."""
        self.logger.info(f"Deploying Kubernetes configurations in namespace: {self.namespace}")
        self.create_namespace()
        self.create_configmaps()
        with self._rendered_deployment_configs() as config_path:
            self.kubectl.apply_configs(self.namespace, config_path)
        if self.mount_failure_scripts:
            self.populate_failure_configmaps()
            self._patch_mongo_failure_script_mounts()
        self.kubectl.wait_for_ready(self.namespace)

    def delete(self):
        """Delete the configmap."""
        self.kubectl.delete_configs(self.namespace, self.k8s_deploy_path)

    def cleanup(self):
        """Delete the entire namespace for the hotel reservation application."""
        self.kubectl.delete_namespace(self.namespace)

        self.kubectl.wait_for_namespace_deletion(self.namespace)
        if uses_openebs():
            # An OpenEBS LocalPV volume is reclaimed by a helper pod that can leave the
            # PersistentVolume behind in 'Terminating' with its hostpath directory still
            # in place, so remove the volumes this application's claims named: clear the
            # finalizer the stuck volume waits on, then delete the volume.
            for pv in self._namespace_persistent_volumes():
                pv_name = pv.get("metadata", {}).get("name")
                if not pv_name:
                    continue
                self._remove_pv_finalizers(pv_name)
                delete_command = f"kubectl delete pv {pv_name}"
                delete_result = self.kubectl.exec_command(delete_command)
                logger.info(f"Deleted PersistentVolume {pv_name}: {delete_result.strip()}")
        else:
            # A CSI driver owns the whole lifecycle of its volume, so cleanup deletes
            # nothing here; see _report_unreleased_persistent_volumes.
            self._report_unreleased_persistent_volumes()
        time.sleep(5)

        if hasattr(self, "wrk"):
            # self.wrk.stop()
            self.kubectl.delete_job(label="job=workload", namespace=self.namespace)

    def _namespace_persistent_volumes(self) -> list[dict]:
        """Return the PersistentVolumes whose claim lives in this application's namespace.

        The claim reference decides the match, not the volume name: a dynamically
        provisioned volume is named ``pvc-<uid>``, so a name match finds nothing on a
        cluster whose default class provisions them, and it can match a volume another
        namespace or release owns.
        """
        raw = self.kubectl.exec_command("kubectl get pv -o json")
        try:
            pv_list = json.loads(raw)
        except (TypeError, ValueError):
            pv_list = None
        if not isinstance(pv_list, dict):
            logger.warning(f"Could not read the cluster's PersistentVolumes: {raw}")
            return []
        return [
            pv
            for pv in pv_list.get("items") or []
            if (pv.get("spec", {}).get("claimRef") or {}).get("namespace") == self.namespace
        ]

    def _report_unreleased_persistent_volumes(self):
        """Name the PersistentVolumes the storage driver has not finished deleting.

        Removing a PersistentVolume, or clearing its finalizers, before the CSI
        controller confirms the backend volume is gone deletes the record of the volume
        while the volume itself stays behind, so nothing is deleted or patched here.
        """
        deadline = time.monotonic() + PV_RELEASE_WAIT_SECONDS
        remaining = self._namespace_persistent_volumes()
        while remaining and time.monotonic() < deadline:
            time.sleep(PV_RELEASE_POLL_SECONDS)
            remaining = self._namespace_persistent_volumes()

        for pv in remaining:
            logger.warning(
                f"PersistentVolume {pv.get('metadata', {}).get('name', '<unnamed>')} "
                f"(phase {pv.get('status', {}).get('phase', 'Unknown')}, "
                f"volumeHandle {self._csi_volume_handle(pv)}) still exists after namespace "
                f"{self.namespace} was deleted; the CSI driver owns its deletion"
            )
        if remaining:
            logger.warning(
                f"{len(remaining)} PersistentVolume(s) claimed by namespace {self.namespace} were not released "
                f"within {PV_RELEASE_WAIT_SECONDS}s and are left to the CSI driver"
            )

    @staticmethod
    def _csi_volume_handle(pv: dict) -> str:
        """Return the CSI driver's handle for the volume, or 'n/a' when the PV has no CSI block."""
        csi = pv.get("spec", {}).get("csi") or {}
        return csi.get("volumeHandle") or "n/a"

    def _remove_pv_finalizers(self, pv_name: str):
        """Remove finalizers from the PersistentVolume to prevent it from being stuck in a 'Terminating' state."""
        # Patch the PersistentVolume to remove finalizers if it is stuck
        patch_command = f'kubectl patch pv {pv_name} -p \'{{"metadata":{{"finalizers":null}}}}\''
        _ = self.kubectl.exec_command(patch_command)

    # helper methods
    def _prepare_configmap_data(self, script_files: list) -> dict:
        data = {}
        for file in script_files:
            data[file] = self._read_script(f"{self.script_dir}/{file}")
        return data

    def _read_script(self, file_path: str) -> str:
        with open(file_path) as file:
            return file.read()

    def create_workload(
        self, rate: int = 100, dist: str = "exp", connections: int = 100, duration: int = 30, threads: int = 3
    ):
        self.wrk = Wrk2WorkloadManager(
            wrk=Wrk2(
                rate=rate,
                dist=dist,
                connections=connections,
                duration=duration,
                threads=threads,
                namespace=self.namespace,
            ),
            payload_script=self.payload_script,
            url="{placeholder}",
            namespace=self.namespace,
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.url = get_frontend_url(self)
        self.wrk.start()

    def stop_workload(self):
        if hasattr(self, "wrk"):
            self.wrk.stop()
