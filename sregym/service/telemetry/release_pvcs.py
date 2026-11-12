"""Release storage that Helm leaves behind on uninstall."""

import subprocess

from sregym.cluster_platform import is_openshift
from sregym.service.kubectl import WAIT_FOR_POD_READY_TIMEOUT
from sregym.storage import uses_openebs


def use_main_release_handling() -> bool:
    """Whether the telemetry releases are deployed exactly as `main` deploys them.

    True on `kind` with the OpenEBS class, the only combination `main` ever ran. The
    OpenShift overlays, the release fingerprint and the PVC cleanup are needed on a
    managed cluster, and a run that names a different storage class needs the class
    recorded on the release, so every other combination takes the fingerprint path.
    """
    return not is_openshift() and uses_openebs()


def delete_retained_pvcs(namespace: str, release_name: str) -> None:
    """Remove the claims a release retains on uninstall, before the next install.

    A retained claim outlives both its release and the storage class that provisioned it,
    so a fresh StatefulSet would rebind the old class's claim instead of the class this
    run asks for. A failed LocalPV helper can also leave a claim Bound while its node
    directory was never created, which makes every later install fail at mount time.
    """
    subprocess.run(
        [
            "kubectl",
            "delete",
            "pvc",
            "-n",
            namespace,
            "-l",
            f"app.kubernetes.io/instance={release_name}",
            "--ignore-not-found=true",
            "--wait=true",
            f"--timeout={WAIT_FOR_POD_READY_TIMEOUT}s",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=WAIT_FOR_POD_READY_TIMEOUT + 10,
    )
