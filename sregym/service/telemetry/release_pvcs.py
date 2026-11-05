"""Release storage that Helm leaves behind on uninstall."""

import subprocess

from sregym.service.kubectl import WAIT_FOR_POD_READY_TIMEOUT


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
