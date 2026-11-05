"""Interface for helm operations"""

import hashlib
import json
import logging
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import yaml

from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.infra.helm")
logger.propagate = True
logger.setLevel(logging.DEBUG)

# Marker on the release's own Helm secrets: the values the release was installed with.
# Helm owns those secrets, so the marker disappears with the release it describes.
VALUES_FINGERPRINT_ANNOTATION = "sregym.io/values-fingerprint"
# Returned by `requested_values_fingerprint` when an input cannot be read. Nothing is ever
# recorded under this value, so comparing against it always refuses reuse.
UNKNOWN_VALUES_FINGERPRINT = "unknown"
FINGERPRINT_COMMAND_TIMEOUT = 60


class Helm:
    @staticmethod
    def install(**args):
        """Install a helm chart

        Args:
            release_name (str): Name of the release
            chart_path (str): Path to the helm chart
            namespace (str): Namespace to install the chart
            version (str): Version of the chart
            values_file (str): Optional values override file
            extra_args (List[str | Sequence[str]]): Extra arguments for the helm install
                command. A string entry is one argument, exactly as given, and is never
                split; a sequence entry contributes its elements in order. Nothing is
                split and nothing reaches a shell, so a value that holds a space stays
                one argument.
            remote_chart (bool): Whether the chart is remote (from a Helm repo)

        An `extra_args` string entry used to be split with `shlex.split`, so a path with a
        space silently became two arguments. That was a defect, not a convention: pass the
        flag and its value as separate entries, for example `["--set", "a=b"]`.
        """

        release_name = args.get("release_name")
        chart_path = args.get("chart_path")
        namespace = args.get("namespace")
        version = args.get("version")
        extra_args = args.get("extra_args")
        remote_chart = args.get("remote_chart", False)

        logger.info(f"Helm Install: {release_name} in namespace {namespace}")

        if not remote_chart:
            Helm.ensure_dependencies(chart_path)

        # An argv list, never a shell string: a storage class name or a chart value
        # is caller input, and a shell would interpret it.
        command = ["helm", "install", release_name, chart_path, "-n", namespace, "--create-namespace"]

        if version:
            command += ["--version", version]

        if values_file := args.get("values_file"):
            command += ["-f", str(values_file)]

        command += Helm._extra_args_argv(extra_args)

        process = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate()

        stderr = error.decode("utf-8").strip() if error else ""
        stdout = output.decode("utf-8").strip() if output else ""

        if stderr:
            logger.warning(f"Helm install stderr output:\n{stderr}")

        if process.returncode != 0:
            raise RuntimeError(
                f"Helm install failed for release '{release_name}' in namespace '{namespace}'. "
                f"Chart: {chart_path}. Error output:\n{stderr}\n"
                f"Stdout (for context):\n{stdout}"
            )
        else:
            logger.debug(stdout)

    @staticmethod
    def _extra_args_argv(extra_args: Sequence[str | Sequence[str]] | None) -> list[str]:
        """Flatten `extra_args` into argv entries: a string is one argument, a sequence expands."""
        argv: list[str] = []
        for entry in extra_args or []:
            argv += [entry] if isinstance(entry, str) else [*entry]
        return argv

    @staticmethod
    def ensure_dependencies(chart_path: str):
        """Vendor a local chart's subcharts, skipping the update when already satisfied.

        `helm dependency update` re-fetches every repository index over the network
        on each call. It runs once per problem per local chart, so across a suite it
        is a meaningful share of wall clock (and the only reason a run needs network
        access once images are cached).

        `helm dependency list` answers the same question locally in ~0.1s: it reports
        "ok" per dependency when the pinned version is present in charts/. Only fall
        through to the update when something is missing or the wrong version.
        """
        chart_dir = Path(chart_path).expanduser()
        with (chart_dir / "Chart.yaml").open() as file:
            chart = yaml.safe_load(file) or {}
        # Helm v1 charts can declare dependencies in requirements.yaml.
        requirements = chart_dir / "requirements.yaml"
        if chart.get("apiVersion") == "v1" and requirements.exists():
            with requirements.open() as file:
                chart = yaml.safe_load(file) or {}
        dependencies = chart.get("dependencies", [])
        if not dependencies:
            return

        listing = subprocess.run(["helm", "dependency", "list", str(chart_dir)], capture_output=True, text=True)
        if listing.returncode == 0:
            # Keep empty repository columns for local subcharts. Warnings are
            # outside the tab-separated table, and must not count as dependencies.
            rows = [[field.strip() for field in line.split("\t")] for line in listing.stdout.splitlines()[1:]]
            statuses = [row[-1] for row in rows if len(row) in (3, 4)]
            if len(statuses) == len(dependencies) and all(status in {"ok", "unpacked"} for status in statuses):
                logger.debug(f"Chart dependencies already satisfied for {chart_path}; skipping update")
                return
            logger.info(f"Chart dependencies not satisfied for {chart_path} ({statuses or 'none listed'}); updating")

        result = subprocess.run(["helm", "dependency", "update", str(chart_dir)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Helm dependency update failed for chart '{chart_path}'. "
                f"Error output:\n{result.stderr.strip()}\nStdout:\n{result.stdout.strip()}"
            )

    @staticmethod
    def uninstall(**args):
        """Uninstall a helm chart

        Args:
            release_name (str): Name of the release
            namespace (str): Namespace to uninstall the chart
        """
        release_name = args.get("release_name")
        namespace = args.get("namespace")

        logger.info(f"Helm Uninstall: {release_name} in namespace {namespace}")

        release_exists = Helm._release_status(release_name, namespace)
        if release_exists is None:
            return
        if not release_exists:
            logger.debug(f"Release {release_name} does not exist. Skipping uninstall.")
            return

        command = ["helm", "uninstall", release_name, "-n", namespace]
        process = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate()

        stderr = error.decode("utf-8").strip() if error else ""
        stdout = output.decode("utf-8").strip() if output else ""

        if stderr:
            logger.warning(f"Helm uninstall stderr output:\n{stderr}")

        if process.returncode != 0:
            raise RuntimeError(
                f"Helm uninstall failed for release '{release_name}' in namespace '{namespace}'. "
                f"Release: {release_name}. Error output:\n{stderr}\n"
                f"Stdout (for context):\n{stdout}"
            )
        else:
            logger.debug(stdout)

    @staticmethod
    def exists_release(release_name: str, namespace: str) -> bool:
        """Check if a Helm release exists

        Args:
            release_name (str): Name of the release
            namespace (str): Namespace to check

        Returns:
            bool: True if release exists
        """
        return Helm._release_status(release_name, namespace) is True

    @staticmethod
    def _release_status(release_name: str, namespace: str) -> bool | None:
        """Return whether a release exists, or None when Helm cannot check."""
        command = ["helm", "list", "-n", namespace]
        process = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate()

        if process.returncode != 0:
            stderr = error.decode("utf-8").strip() if error else "unknown error"
            logger.error(f"Failed to list Helm releases in namespace '{namespace}': {stderr}")
            return None

        return release_name in output.decode("utf-8")

    @staticmethod
    def requested_values_fingerprint(configs: dict) -> str:
        """Fingerprint the values a caller asks Helm to install with.

        The inputs are the ones the caller supplies: the chart reference and the requested
        `version` first, then the values in the order `install` applies them: the base
        `values.yaml` of a local chart, the `values_file` argument, then every `-f`/`--values`
        file and every `--set`/`--set-string` pair in `extra_args`. A values file is loaded
        and re-dumped with sorted keys, so key order and YAML formatting do not matter;
        `--set` pairs are kept verbatim and in order. A remote chart has no local base file,
        so only the overlays and pairs actually passed are covered; the chart's own defaults
        stay outside the fingerprint.

        The chart reference and the version are part of the fingerprint because Helm picks
        the release content from them: a chart-version bump with unchanged values still has
        to redeploy. A remote chart without a `version` is unpinned and records an empty
        one, because the version Helm then resolves is unknown here.

        An unreadable input is skipped with one warning instead of raising, and makes the
        whole result `UNKNOWN_VALUES_FINGERPRINT`. That value is never recorded, so an
        unreadable input leaves the fingerprint untrustworthy and forces the caller to
        treat the request as a redeploy, which is the safe direction.
        """
        parts: list[str] = [f"chart:{configs.get('chart_path') or ''}@{configs.get('version') or ''}"]
        if configs.get("chart_path") and not configs.get("remote_chart", False):
            chart_values = Path(configs["chart_path"]).expanduser() / "values.yaml"
            base = Helm._canonical_values_file(chart_values)
            if base is None:
                return UNKNOWN_VALUES_FINGERPRINT
            parts.append(f"base:{base}")

        if values_file := configs.get("values_file"):
            canonical = Helm._canonical_values_file(Path(values_file).expanduser())
            if canonical is None:
                return UNKNOWN_VALUES_FINGERPRINT
            parts.append(f"file:{canonical}")

        extra = Helm._extra_arg_values_parts(configs.get("extra_args") or [])
        if extra is None:
            return UNKNOWN_VALUES_FINGERPRINT
        parts += extra

        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    @staticmethod
    def _extra_arg_values_parts(extra_args: Sequence[str | Sequence[str]]) -> list[str] | None:
        """Canonical parts from `extra_args`: one per values file and per `--set` pair, in order."""
        argv = Helm._extra_args_argv(extra_args)

        parts: list[str] = []
        index = 0
        while index < len(argv):
            argument = argv[index]
            index += 1
            if argument in {"-f", "--values"}:
                if index == len(argv):
                    break
                value = argv[index]
                index += 1
            elif argument.startswith("--values="):
                value = argument.split("=", 1)[1]
            else:
                value = None

            if value is not None:
                canonical = Helm._canonical_values_file(Path(value).expanduser())
                if canonical is None:
                    return None
                parts.append(f"file:{canonical}")
            elif argument in {"--set", "--set-string"}:
                if index == len(argv):
                    break
                parts.append(f"{argument}:{argv[index]}")
                index += 1
            elif argument.startswith(("--set=", "--set-string=")):
                parts.append(argument)
        return parts

    @staticmethod
    def _canonical_values_file(path: Path) -> str | None:
        """Load a values file and re-dump it with sorted keys, or None when it is unreadable."""
        try:
            with path.open() as file:
                return json.dumps(yaml.safe_load(file) or {}, sort_keys=True)
        except (OSError, yaml.YAMLError, TypeError, ValueError) as error:
            logger.warning(f"Helm values file {path} is unreadable ({error}); its values are not fingerprinted")
            return None

    @staticmethod
    def record_values_fingerprint(configs: dict) -> None:
        """Record the fingerprint of the values an install used, on the release's own secrets.

        A failure only costs a future redeploy, because a release without a trustworthy
        fingerprint is never reused, so it is logged as a warning and never raised.
        """
        release_name = configs.get("release_name")
        namespace = configs.get("namespace")
        if not release_name or not namespace:
            logger.warning("Cannot record a values fingerprint without a release name and a namespace")
            return

        fingerprint = Helm.requested_values_fingerprint(configs)
        if fingerprint == UNKNOWN_VALUES_FINGERPRINT:
            logger.warning(f"Not recording a values fingerprint for release {release_name}: its values are unreadable")
            return

        command = [
            "kubectl",
            "annotate",
            "secret",
            "-n",
            namespace,
            "-l",
            f"owner=helm,name={release_name}",
            f"{VALUES_FINGERPRINT_ANNOTATION}={fingerprint}",
            "--overwrite",
        ]
        try:
            result = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=FINGERPRINT_COMMAND_TIMEOUT
            )
        except (OSError, subprocess.SubprocessError) as error:
            logger.warning(f"Could not record the values fingerprint of release {release_name}: {error}")
            return
        if result.returncode != 0:
            logger.warning(
                f"Could not record the values fingerprint of release {release_name}: {result.stderr.strip()}"
            )

    @staticmethod
    def release_values_fingerprint(release_name: str, namespace: str) -> str | None:
        """Read the recorded values fingerprint of an installed release, or None when absent."""
        command = [
            "kubectl",
            "get",
            "secret",
            "-n",
            namespace,
            "-l",
            f"owner=helm,name={release_name}",
            "-o",
            "json",
        ]
        try:
            result = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=FINGERPRINT_COMMAND_TIMEOUT
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            items = json.loads(result.stdout).get("items") or []
        except (AttributeError, ValueError):
            return None

        recorded: tuple[int, str] | None = None
        for item in items:
            metadata = item.get("metadata") or {}
            fingerprint = (metadata.get("annotations") or {}).get(VALUES_FINGERPRINT_ANNOTATION)
            if not fingerprint:
                continue
            try:
                version = int((metadata.get("labels") or {}).get("version", 0))
            except (TypeError, ValueError):
                version = 0
            if recorded is None or version > recorded[0]:
                recorded = (version, fingerprint)
        return recorded[1] if recorded else None

    @staticmethod
    def release_matches_requested_values(configs: dict) -> bool:
        """Whether the installed release was installed with exactly the values this config asks for.

        False means "redeploy", which is always safe; True only means the recorded and the
        requested fingerprint are equal. The comparison never raises, so a cluster that
        cannot answer costs a redeploy instead of a failure.
        """
        release_name = configs.get("release_name")
        namespace = configs.get("namespace")
        try:
            if not release_name or not namespace:
                logger.warning("Cannot compare release values without a release name and a namespace; redeploying")
                return False
            if not Helm.exists_release(release_name, namespace):
                logger.warning(f"Release {release_name} in {namespace} does not exist; redeploying")
                return False
            recorded = Helm.release_values_fingerprint(release_name, namespace)
            if recorded is None:
                logger.warning(f"Release {release_name} in {namespace} has no readable values fingerprint; redeploying")
                return False
            requested = Helm.requested_values_fingerprint(configs)
            if requested == UNKNOWN_VALUES_FINGERPRINT:
                logger.warning(f"The values requested for release {release_name} are unreadable; redeploying")
                return False
            if recorded != requested:
                logger.info(f"Release {release_name} in {namespace} was installed with other values; redeploying")
                return False
            return True
        # A redeploy is always safe, a silent reuse of other values is not.
        except Exception as error:
            logger.warning(f"Cannot compare the values of release {release_name} in {namespace} ({error}); redeploying")
            return False

    @staticmethod
    def assert_if_deployed(namespace: str):
        """Assert if all services in the application are deployed

        Args:
            namespace (str): Namespace to check

        Returns:
            bool: True if deployed

        Raises:
            Exception: If not deployed
        """
        kubectl = KubeCtl()
        try:
            kubectl.wait_for_ready(namespace)
        except Exception as e:
            raise e

        return True

    @staticmethod
    def upgrade(**args):
        """Upgrade a helm chart

        Args:
            release_name (str): Name of the release
            chart_path (str): Path to the helm chart
            namespace (str): Namespace to upgrade the chart
            values_file (str): Path to the values.yaml file
            set_values (dict): Key-value pairs for --set options
        """

        release_name = args.get("release_name")
        chart_path = args.get("chart_path")
        namespace = args.get("namespace")
        values_file = args.get("values_file")
        set_values = args.get("set_values", {})

        logger.info(f"Helm Upgrade: {release_name} in namespace {namespace}")

        command = [
            "helm",
            "upgrade",
            release_name,
            chart_path,
            "-n",
            namespace,
            "-f",
            values_file,
            "--server-side=true",
            "--force-conflicts",
        ]

        # Add --set options if provided
        for key, value in set_values.items():
            command.append("--set")
            command.append(f"{key}={value}")

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate()

        stderr = error.decode("utf-8").strip() if error else ""
        stdout = output.decode("utf-8").strip() if output else ""

        if stderr:
            logger.warning(f"Helm upgrade stderr output:\n{stderr}")

        if process.returncode != 0:
            logger.error("Error during helm upgrade:")
            raise RuntimeError(
                f"Helm upgrade failed for release '{release_name}' in namespace '{namespace}'. "
                f"Chart: {chart_path}. Error output:\n{stderr}\n"
                f"Stdout (for context):\n{stdout}"
            )
        else:
            logger.info("Helm upgrade successful!")
            logger.debug(stdout)

    @staticmethod
    def add_repo(name: str, url: str, max_retries: int = 3, backoff_factor: float = 2.0):
        """Add a Helm repository with retry logic

        Args:
            name (str): Name of the repository
            url (str): URL of the repository
            max_retries (int): Maximum number of retry attempts
            backoff_factor (float): Multiplier for exponential backoff (seconds)

        Raises:
            RuntimeError: If all retry attempts fail
        """
        logger.info(f"Helm Repo Add: {name} with url {url}")
        command = ["helm", "repo", "add", name, url]

        for attempt in range(max_retries):
            process = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = process.communicate()

            # Check if the repo add was successful (return code 0) or repo already exists
            stdout = output.decode("utf-8").strip() if output else ""
            stderr = error.decode("utf-8").strip() if error else ""

            # Success if returncode is 0 or if "already exists" in output
            if process.returncode == 0 or "already exists" in stderr or "already exists" in stdout:
                logger.info(f"Helm repo {name} added successfully: {stdout}")
                return

            if attempt < max_retries - 1:
                wait_time = backoff_factor**attempt
                logger.warning(
                    f"Helm repo add failed on attempt {attempt + 1}/{max_retries}. "
                    f"Retrying in {wait_time}s... Error: {stderr}"
                )
                time.sleep(wait_time)
            else:
                # Final attempt failed
                logger.error(
                    f"Helm repo add failed after {max_retries} attempts. "
                    f"Error output:\n{stderr}\n"
                    f"Stdout (for context):\n{stdout}"
                )
                raise RuntimeError(
                    f"Helm repo add failed for '{name}' at {url} after {max_retries} attempts. "
                    f"Error output:\n{stderr}\n"
                    f"Stdout (for context):\n{stdout}"
                )

    @staticmethod
    def repo_update(max_retries: int = 3, backoff_factor: float = 2.0):
        """Update Helm repositories with retry logic

        Args:
            max_retries (int): Maximum number of retry attempts
            backoff_factor (float): Multiplier for exponential backoff (seconds)

        Raises:
            RuntimeError: If all retry attempts fail
        """
        logger.info("Helm Repo Update with retry logic")
        command = ["helm", "repo", "update"]

        for attempt in range(max_retries):
            process = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = process.communicate()

            if process.returncode == 0:
                logger.info(f"Helm repo update successful on attempt {attempt + 1}")
                logger.debug(output.decode("utf-8"))
                return

            # Log the error but continue retrying
            stderr = error.decode("utf-8").strip() if error else ""
            stdout = output.decode("utf-8").strip() if output else ""

            if attempt < max_retries - 1:
                wait_time = backoff_factor**attempt
                logger.warning(
                    f"Helm repo update failed on attempt {attempt + 1}/{max_retries}. "
                    f"Retrying in {wait_time}s... Error: {stderr}"
                )
                time.sleep(wait_time)
            else:
                # Final attempt failed
                logger.error(
                    f"Helm repo update failed after {max_retries} attempts. "
                    f"Error output:\n{stderr}\n"
                    f"Stdout (for context):\n{stdout}"
                )
                raise RuntimeError(
                    f"Helm repo update failed after {max_retries} attempts. "
                    f"Error output:\n{stderr}\n"
                    f"Stdout (for context):\n{stdout}"
                )


# Example usage
if __name__ == "__main__":
    sn_configs = {
        "release_name": "social-network",
        "chart_path": "~/DeathStarBench/socialNetwork/helm-chart/socialnetwork",
        "namespace": "social-network",
    }
    Helm.install(**sn_configs)
    Helm.uninstall(**sn_configs)
