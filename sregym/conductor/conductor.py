import asyncio
import concurrent.futures
import contextlib
import json
import logging
import math
import shlex
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from kubernetes.client.rest import ApiException

from sregym.cluster_platform import (
    KIND,
    OPENSHIFT,
    PlatformVerificationError,
    get_platform,
    is_openshift,
    serves_metrics_api,
    verify_platform_matches_cluster,
)
from sregym.conductor.constants import StartProblemResult
from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.detection import DetectionOracle
from sregym.conductor.oracles.diagnosis_oracle import DiagnosisOracle
from sregym.conductor.problems.registry import ProblemRegistry
from sregym.conductor.submission import (
    SUBMISSION_STAGES,
    EvaluationInProgress,
    SubmissionAttemptClosed,
    SubmissionAttemptMismatch,
    SubmissionStage,
    SubmissionStageMismatch,
)
from sregym.conductor.utils import is_ordered_subset
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.noise.manager import get_noise_manager
from sregym.observer.jaeger import Jaeger
from sregym.observer.otel_collector import OtelCollector
from sregym.paths import BASE_DIR, CLUSTER_BASELINE_STATE_FILE
from sregym.phases import PhaseLedger
from sregym.profile import is_svelte
from sregym.service.admission_preflight import (
    MISSING,
    check_promtail_privileged,
    check_scc_grant,
    grant_commands,
    promtail_grant_command,
)
from sregym.service.apps.app_registry import AppRegistry
from sregym.service.capacity_preflight import check_capacity
from sregym.service.cluster_egress import ClusterEgressBoundary
from sregym.service.cluster_state import (
    RECONCILE_MODES,
    RECONCILE_OFF,
    RECONCILE_ON,
    RECONCILE_REPORT,
    ClusterStateManager,
)
from sregym.service.damage_report import DAMAGED, DamageReport, compare
from sregym.service.image_preflight import find_unqualified
from sregym.service.image_preflight import report as format_image_report
from sregym.service.internet_policy import InternetPolicy
from sregym.service.k8s_proxy import KubernetesAPIProxy
from sregym.service.kubectl import WAIT_FOR_POD_READY_TIMEOUT, KubeCtl
from sregym.service.mcp_server import MCPServer
from sregym.service.owned_objects import (
    OWNED_CLUSTER_SCOPED_KINDS,
    delete_ledger_namespaces,
    delete_ledger_objects,
    delete_owned_cluster_scoped,
    ledger_namespaces_present,
    owned_names,
)
from sregym.service.ownership_ledger import OwnershipLedger
from sregym.service.rollout import deployment_rollout_complete
from sregym.service.telemetry.loki import Loki
from sregym.service.telemetry.prometheus import Prometheus
from sregym.storage import OPENebs, get_storage_class, storage_class_is_explicit, uses_openebs

# The agent-facing stages, in the only order they can run. Shared with
# `--stages`' choices so the CLI and the conductor cannot disagree about what
# exists.
ALL_STAGES = ("diagnosis", "mitigation")

# Why a filtered internet policy cannot be served here. Stated once, because two
# entry points answer for it: main.py refuses the run at argument resolution, and
# start_k8s_proxy refuses it again for a Conductor built elsewhere.
FILTERED_INTERNET_UNAVAILABLE = (
    "filtered internet access needs Calico 3.29 or later with the adminnetworkpolicy tier, and this "
    "platform runs OVN-Kubernetes"
)


def resolve_reconcile_mode(requested: str | None) -> str:
    """Return the reconcile mode to use, with the platform default when none was requested.

    A local kind cluster is disposable, so reconciling it back to the baseline is
    the default there. A managed platform is not: its baseline is a snapshot of a
    bare cluster, so the objects absent from it belong to the platform, and
    deleting them is not cleanup. The default there is to report only.
    """
    if requested is not None:
        if requested not in RECONCILE_MODES:
            raise ValueError(f"Unknown reconcile mode {requested!r}; expected one of {RECONCILE_MODES}")
        return requested
    return RECONCILE_OFF if is_openshift() else RECONCILE_ON


def reconcile_refusal(requested: str | None, confirmed: bool) -> str | None:
    """Return the one-line refusal for an unconfirmed destructive reconcile, or None.

    Reconcile deletes every object absent from the baseline. On a managed platform
    that set includes platform objects, so asking for it takes two answers: the
    mode, and the confirmation. Both entry points answer before any cluster work.
    """
    if confirmed or resolve_reconcile_mode(requested) != RECONCILE_ON or not is_openshift():
        return None
    return (
        "Refusing --reconcile on: reconcile deletes every object absent from the baseline, and on this "
        "platform that includes platform objects. Re-run with --reconcile report to list those writes "
        "without making them, or add --confirm-destructive-reconcile to accept the deletion."
    )


@dataclass
class ConductorConfig:
    """Configuration for Conductor deployment options."""

    deploy_loki: bool = True
    enable_noise: bool = False
    internet_policy: InternetPolicy = field(default_factory=InternetPolicy)
    k8s_proxy_listen_host: str = "127.0.0.1"
    k8s_proxy_listen_port: int = 16443
    block_workload_creation: bool = False
    # Which stages this run should attempt. None means every stage the problem
    # supports, which is what an unset --stages leaves in place.
    stages: tuple[str, ...] | None = None
    baseline_override_s: int | None = None  # overrides per-problem baseline_duration_s when set
    propagation_override_s: int | None = None  # overrides per-problem propagation_duration_s when set
    # What cleanup does with the baseline: "on", "report" or "off". None means the
    # platform decides, which cannot be a dataclass default because that would call
    # get_platform() at import time.
    reconcile: str | None = None
    # An operator assertion that the cluster resolves the unqualified image names in the
    # manifests it is about to apply. The preflight cannot know a cluster's short-name table,
    # so it refuses; this says "I know this one, proceed" and every finding is still logged.
    allow_unqualified_images: bool = False
    # Keep a run-local baseline so the damage verdict can compare. Set when a run asks for a
    # verdict; on OpenShift it is implied, because that platform is where a run must prove it
    # left the cluster as it found it.
    damage_verdict: bool = False

    @property
    def restrict_network_access(self) -> bool:
        # The old workload flag remains an input for programmatic callers.
        # Derive the effective policy here, for both CLI and direct use.
        return self.internet_policy.is_filtered or self.block_workload_creation


class Conductor:
    def __init__(self, config: ConductorConfig | None = None, *, judge_preflight: Callable[[], None] | None = None):
        self.config = config or ConductorConfig()
        self._judge_preflight = judge_preflight
        self._judge_preflight_done = False

        # core services
        self.problems = ProblemRegistry()
        self.kubectl = KubeCtl()
        self.prometheus = Prometheus()
        self.jaeger = Jaeger()
        self.otel_collector = OtelCollector()
        self.loki = Loki()
        self.mcp_server = MCPServer(restrict_network_access=self.config.restrict_network_access)
        self.apps = AppRegistry()
        self.agent_name = None
        self.agent_launches_container = False

        self.cluster_state = ClusterStateManager(self.kubectl)
        self.cluster_egress = ClusterEgressBoundary(self.kubectl)
        # The run's record of the scenario-created objects it made, matched by UID at teardown.
        self.ownership_ledger = OwnershipLedger()
        self._baseline_captured = False
        # False once the persisted baseline is refused for describing a cluster that
        # no longer exists: reconcile then has nothing trustworthy to aim at.
        self._baseline_usable = True
        self._platform_verified = False
        # False until the first problem of a campaign has cleared the capacity
        # preflight; every later problem of that campaign reuses the verdict.
        self._capacity_verified = False

        # Kubernetes API proxy to hide chaos engineering namespaces and load generators from agents
        self.k8s_proxy = KubernetesAPIProxy(
            listen_port=self.config.k8s_proxy_listen_port,
            listen_host=self.config.k8s_proxy_listen_host,
            restrict_network_access=self.config.restrict_network_access,
        )
        self._agent_kubeconfig_path: str | None = None

        self.problem_id: str | None = None
        self.problem = None
        self.app = None
        self.detection_oracle = None
        self.execution_start_time: float = 0.0
        self.agent_timeout_seconds: float | None = None
        self.agent_deadline_monotonic: float | None = None

        # grading flow state
        # submission_stage reflects the current stage (e.g., "diagnosis", "mitigation") or "done"
        self.submission_stage = None
        self.results = {}
        self._submit_future = None  # Future for the executor running _submit_evaluate_and_advance
        self._submit_evaluation_timeout: float | None = None
        self._submission_lock = threading.RLock()
        self._pending_submission_stages: dict[tuple[int, str], int] = {}
        self._submission_generation = 0
        self._aborted_submission_generations: set[int] = set()
        self._accepting_submissions = False
        self._attempt_closed = True

        self.tasklist = None
        self.logger = logging.getLogger("all.sregym.conductor")

        # Resolved once, here, so every cleanup in this run uses the same mode. No
        # I/O: the only input is the requested value or the platform singleton.
        self.reconcile = resolve_reconcile_mode(self.config.reconcile)
        self.logger.info(
            "[CLEANUP] Reconcile mode: %s (%s)",
            self.reconcile,
            "requested" if self.config.reconcile is not None else f"platform default for {get_platform()}",
        )

        # Phase-boundary ledger, bound per attempt by whoever owns the run
        # directory (main.py). Left unset by cli.py, the external harness and
        # the tests, where `_phase` degrades to a no-op.
        self.phases: PhaseLedger | None = None

        self.stage_sequence: list[dict] = []
        self.current_stage_index: int = 0
        self.waiting_for_agent: bool = False
        self._evaluating: bool = False  # True while a submission is being evaluated
        self.fault_injected: bool = False

    @property
    def current_problem(self):
        """Return the current problem, raising if none is loaded."""
        if self.problem is None:
            raise RuntimeError("No problem is loaded")
        return self.problem

    def register_agent(self, name="agent", *, container_isolation: bool = True):
        """Record the agent this run drives, and whether it runs in a container.

        ``container_isolation`` is the registration's flag, not an assumption: an
        agent registered with ``container_isolation: false`` runs on the host, so no
        container is launched for it and no container runtime is needed.
        """
        self.agent_name = name
        self.agent_launches_container = bool(container_isolation)

    def start_k8s_proxy(self):
        """
        Start the Kubernetes API proxy that hides chaos engineering namespaces.
        Should be called before launching agents.
        """
        self.logger.info("Starting Kubernetes API filtering proxy...")
        if self.config.internet_policy.is_filtered:
            if is_openshift():
                # Backstop only: main.py refuses this before any container work, so the
                # run reaching this line built its own Conductor. The boundary is a
                # Calico policy in the adminnetworkpolicy tier, which this platform has
                # no tier for, so start() could only fail here.
                raise RuntimeError(
                    f"{FILTERED_INTERNET_UNAVAILABLE}, so the cluster egress boundary cannot be built. "
                    "Re-run with --internet-access open."
                )
            self.cluster_egress.start()
        else:
            self._stop_cluster_egress()
        try:
            if is_openshift():
                self._hide_platform_namespaces()
            self.k8s_proxy.start()
            self._agent_kubeconfig_path = self.k8s_proxy.generate_agent_kubeconfig()
        except BaseException:
            self._stop_cluster_egress()
            raise
        self.logger.info(f"Agent kubeconfig generated at: {self._agent_kubeconfig_path}")

    def _stop_cluster_egress(self) -> None:
        """Remove the cluster egress boundary, on the platforms that can have one.

        A filtered run creates the boundary, and an interrupted one can leave it
        behind, so an open run clears it before the proxy starts and again if the
        proxy fails to start.

        OpenShift never has one to clear: the boundary is a Calico policy this
        platform cannot create, and under a restricted identity the delete comes back
        403 rather than the tolerated 404, which ``stop()`` raises. Skipping the call
        is this platform's answer, not a swallowed error.
        """
        if is_openshift():
            self.logger.debug("Platform openshift has no cluster egress boundary to remove; skipping its removal")
            return
        self.cluster_egress.stop()

    def _hide_platform_namespaces(self) -> None:
        """Add the platform's own namespaces to what the proxy hides from the agent.

        The names are read from the cluster instead of hard-coded, so a platform
        namespace this harness has never heard of is hidden too. The assignment
        makes a new set because the proxy copies the attribute into closure
        locals in `start()`; replacing the attribute before `start()` is the only
        thing that is visible to the running proxy.

        This is not an authorization boundary. A request for a hidden namespace
        gets 404 from the proxy, but the agent's MCP path does not pass through
        this proxy at all, so what the agent can reach is decided elsewhere. That
        is a recorded decision, not something this method changes.
        """
        try:
            items = self.kubectl.list_namespaces().items
        except Exception as exc:
            raise RuntimeError(
                f"Could not read the cluster's namespaces ({exc!r}), so the openshift* namespaces cannot be "
                "hidden from the agent. The proxy will not start with an unknown hidden set."
            ) from exc
        if not items:
            raise RuntimeError(
                "Could not read a single namespace from the cluster, so the openshift* namespaces cannot be "
                "hidden from the agent. The proxy will not start with an unknown hidden set."
            )
        platform_namespaces = {
            item.metadata.name
            for item in items
            if item.metadata and item.metadata.name and item.metadata.name.startswith("openshift")
        }
        self.k8s_proxy.hidden_namespaces = set(self.k8s_proxy.hidden_namespaces) | platform_namespaces
        self.logger.info(f"[PROXY] Hiding {len(platform_namespaces)} openshift* namespaces from the agent")

    def stop_k8s_proxy(self):
        """Stop the Kubernetes API proxy."""
        self.logger.info("Stopping Kubernetes API filtering proxy...")
        try:
            self.k8s_proxy.stop()
        finally:
            try:
                self.cluster_egress.stop()
            finally:
                self._agent_kubeconfig_path = None

    def clear_cluster_egress_boundary(self):
        """Remove a policy left by a previously interrupted filtered run."""
        self.cluster_egress.stop()

    def get_agent_kubeconfig_path(self) -> str | None:
        """
        Get the path to the kubeconfig file that agents should use.
        This kubeconfig points to the filtering proxy that hides chaos namespaces.
        """
        return self._agent_kubeconfig_path

    def verify_platform(self) -> None:
        """Confirm the declared platform against cluster discovery, once per conductor.

        Public because there are two entry points, not one: main.py through
        start_problem, and tests/integration/validate_problem.py, which drives the
        stages itself and never calls start_problem. main.py's retry loop calls
        start_problem up to three times, so discovery runs once per conductor
        rather than once per attempt. It cannot run in __init__: a unit test that
        never touches a cluster still constructs a Conductor.
        """
        if self._platform_verified:
            return
        # Informational only, for logs and reports: behaviour is decided by the
        # cluster_platform module predicates, never by reading this attribute.
        self.platform = verify_platform_matches_cluster(self.kubectl.openshift_api_present, log=self.logger)
        self._refuse_default_openebs_on_openshift()
        self._platform_verified = True

    def _refuse_default_openebs_on_openshift(self) -> None:
        """Stop a run that would install OpenEBS cluster-wide on a managed platform.

        OpenEBS is the default storage class, and selecting it applies an operator
        manifest fetched from the internet: two CRDs, cluster-scoped RBAC and two
        StorageClasses, plus a privileged node-disk-manager DaemonSet. On a managed
        cluster the privileged pods are refused, so the run dies *after* those
        cluster-scoped objects exist, and nothing here removes them -- the teardown
        removes only harness infrastructure and objects its ledger recorded, and these are neither.
        That is a leftover on somebody else's cluster, so a forgotten --storage-class
        is a refusal rather than a deploy.

        A class named outright is the operator's call, and it is only warned about:
        the install fails loudly on restricted admission anyway.
        """
        if self.platform != OPENSHIFT or not uses_openebs():
            return
        if storage_class_is_explicit():
            self.logger.warning(
                "Storage class %r selected by name on openshift: it applies the OpenEBS operator manifest "
                "(two CRDs, cluster-scoped RBAC, two StorageClasses, a privileged node-disk-manager DaemonSet). "
                "Nothing in this harness removes those objects; a restricted cluster refuses the privileged "
                "pods after they exist.",
                OPENebs,
            )
            return
        raise PlatformVerificationError(
            f"Refusing to run on openshift with the default storage class {OPENebs!r}: selecting it installs the "
            "OpenEBS operator cluster-wide (CRDs, ClusterRoles, StorageClasses, a privileged node-disk-manager "
            "DaemonSet) and nothing here removes those objects. Pass the cluster's own class: "
            "--storage-class <name> (or set SREGYM_STORAGE_CLASS). See docs/openshift.md."
        )

    def _capture_run_baseline(self) -> None:
        """Remember the cluster as it is before this run's first write, if nothing did.

        The damage verdict compares the run against a baseline, and the only thing that
        captured one until now was reconcile -- which is `off` by default on a managed
        platform, so the verdict had nothing to compare and could only report that it could
        not answer. This is a read-only snapshot held in memory: nothing is written, so a run
        that does not ask for a verdict behaves exactly as before.
        """
        if not (is_openshift() or getattr(getattr(self, "config", None), "damage_verdict", False)):
            return
        # A partly built conductor, which several tests and the validator use, has no cluster
        # state to snapshot; that is not a failure of this run.
        state = getattr(self, "cluster_state", None)
        if state is None or getattr(state, "baseline", None) is not None:
            return
        try:
            state.capture_baseline()
            self.logger.info("[DAMAGE] Captured a run baseline so the damage verdict can compare")
        except Exception as exc:
            self.logger.warning(f"[DAMAGE] Could not capture a run baseline: {exc}")

    def _verify_image_references(self, problem) -> None:
        """Refuse a run whose manifests name images the cluster may rewrite.

        A managed cluster resolves an unqualified image name through its own short-name
        alias table before pulling, so the name in the manifest is not the image that is
        pulled. The failure then arrives as `ImagePullBackOff` on eight pods and a 600 s
        readiness timeout, which reads like an application fault. This happens before the
        application deploy, so it costs one line instead.

        The scan covers the manifests SREGym applies itself and the application's own
        manifest directory. Chart templates are skipped: they are YAML only after
        rendering, and the summary the operator needs is the manifest path.
        """
        if not is_openshift():
            return
        paths = [BASE_DIR / "observer"]
        app_path = getattr(problem.app, "k8s_deploy_path", None)
        if app_path:
            paths.append(Path(app_path))
        findings = find_unqualified(paths)
        if not findings:
            self.logger.info("[DEPLOY] Every scanned image reference names its registry")
            return
        message = format_image_report(findings)
        if getattr(getattr(self, "config", None), "allow_unqualified_images", False):
            self.logger.warning(
                f"[DEPLOY] {message} Continuing because --allow-unqualified-images was given: "
                "the operator asserts this cluster resolves these names."
            )
            return
        self.logger.error(f"[DEPLOY] {message}")
        raise RuntimeError(message)

    def _ownership_ledger(self) -> OwnershipLedger:
        """Return this run's ownership ledger, or a file-backed one for a conductor built without it.

        Tests build a conductor with `__new__` and never run `__init__`, so the attribute may be absent.
        """
        return getattr(self, "ownership_ledger", None) or OwnershipLedger()

    def teardown_observers(self) -> list[str]:
        """Remove the observer stack and everything SREGym created outside its namespaces.

        Called at the end of a campaign, never between problems: the oracles exec into
        `observe/prometheus-server`, and a result may be re-evaluated after its problem ended.

        Three things survive a namespaced cleanup and are removed here: the Helm releases, the
        release PVCs Helm retains on purpose, and the objects SREGym created outside its namespaces.
        The cluster-scoped ones are matched by ownership (`sregym.service.owned_objects`): the
        infrastructure label, the Helm release annotations, or the run's ownership ledger by UID.
        The scenario namespaces (`platform-ops`) are removed through the ledger alone. A same-named
        object the run did not create is never touched.

        Returns the objects it removed, which is what the damage verdict is given as its only
        tolerance: the verdict can excuse what this method reports and nothing else.
        """
        ledger = self._ownership_ledger()
        removed: list[str] = []
        # `helm uninstall` removes the chart's ClusterRoles and Bindings without telling anyone,
        # so they are recorded before it runs: every owned name that is gone afterwards was
        # removed by this teardown, and the damage verdict must tolerate exactly that.
        owned_before = {
            kind: set(owned_names(self.kubectl, kind, log=self.logger, ledger=ledger))
            for kind in OWNED_CLUSTER_SCOPED_KINDS
        }
        for component in (getattr(self, "prometheus", None), getattr(self, "loki", None)):
            teardown = getattr(component, "teardown", None)
            if teardown is None:
                continue
            try:
                teardown()
            except Exception as exc:
                self.logger.warning(f"[TEARDOWN] {type(component).__name__} teardown failed: {exc}")
        removed.extend(delete_owned_cluster_scoped(self.kubectl, log=self.logger, ledger=ledger))
        for kind, names in owned_before.items():
            still_there = set(owned_names(self.kubectl, kind, log=self.logger, ledger=ledger))
            removed.extend(f"{kind}/{name}" for name in sorted(names - still_there))

        for namespace in self._observer_namespaces():
            self.logger.info(f"[TEARDOWN] Removing namespace {namespace}")
            try:
                self.kubectl.exec_command(f"kubectl delete namespace {self._q(namespace)} --ignore-not-found")
            except Exception as exc:
                self.logger.warning(f"[TEARDOWN] Could not delete namespace {namespace}: {exc}")
                continue
            removed.append(f"namespace/{namespace}")

        removed.extend(delete_ledger_namespaces(self.kubectl, ledger, log=self.logger))

        for volume in self._report_surviving_volumes():
            self.logger.warning(
                f"[TEARDOWN] PersistentVolume {volume} is still present after teardown; "
                "remove it by hand once its claim is gone"
            )
        # A deleted object is reported by the delete and again by the before/after comparison.
        return list(dict.fromkeys(removed))

    def _reset_observers(self) -> None:
        """Remove the observer stack after a scenario, so the next one deploys it fresh.

        OpenShift, with Loki deployed by this run, only (see `_cleanup_sync`). `deploy_app()`
        redeploys the stack for every scenario, so this costs one redeploy per scenario and keeps
        one scenario's telemetry, retained volumes and Helm state out of the next. Each component's
        own teardown runs first: Prometheus deletes its retained claim, and Loki uninstalls Promtail
        with its release. The namespace delete then takes what is left in `observe`, which holds
        the observer pods. It does not wait: a slow volume release must not spend the cleanup
        drain budget. The next `deploy_app()` waits for `observe` to finish terminating before it
        installs Prometheus (see `Prometheus.deploy`).

        The `sregym` namespace and the MCP server are not touched. Only `prometheus.namespace` is
        deleted, and it is `observe`. `teardown_observers()` is the end-of-campaign path and is not
        this method. A failure here propagates to the caller, which logs it as a warning.
        """
        for component in (self.prometheus, self.loki):
            try:
                component.teardown()
            except Exception as exc:
                self.logger.warning(f"[RESET] {type(component).__name__} teardown failed: {exc}")
        namespace = self.prometheus.namespace
        self.logger.info(f"[RESET] Removing observer namespace {namespace}")
        self.kubectl.exec_command_checked(
            f"kubectl delete namespace {self._q(namespace)} --ignore-not-found --wait=false",
            timeout=WAIT_FOR_POD_READY_TIMEOUT,
        )

    def _observer_namespaces(self) -> list[str]:
        """Return the namespaces the observer stack and the MCP server own, deduplicated."""
        namespaces = []
        for component in (getattr(self, "prometheus", None), getattr(self, "mcp_server", None)):
            namespace = getattr(component, "namespace", None)
            if namespace and namespace not in namespaces:
                namespaces.append(namespace)
        return namespaces

    def _report_surviving_volumes(self) -> list[str]:
        """Return the SREGym-owned volumes still present, without ever patching a finalizer."""
        try:
            listing = self.kubectl.exec_command_checked("kubectl get pv -o json", timeout=60)
            items = json.loads(listing).get("items", [])
        except Exception:
            return []
        survivors = []
        for item in items:
            claim_ref = item.get("spec", {}).get("claimRef") or {}
            if claim_ref.get("namespace") in set(self._observer_namespaces()):
                survivors.append(item["metadata"]["name"])
        return sorted(survivors)

    def cluster_damage_verdict(self, deleted_by_us=(), after_teardown: bool = False) -> "DamageReport":
        """Compare the cluster with the baseline and return the verdict.

        The baseline is the one this run captured before its first write. A run that cannot
        compare -- no baseline, an unreadable API -- gets a warning and no verdict, never a
        passing one.

        `after_teardown` turns on the second direction of the check. Comparing snapshots can only
        see what was removed or changed; an object this harness created and then failed to remove
        is an addition, which is normally the run's own work and merely reported. Once a teardown
        was asked for and has run, anything still carrying this harness's ownership is a leftover,
        and it is named as damage rather than logged.
        """
        # A verdict is given once per run. A second one would compare the cluster with a
        # baseline taken before a teardown that already ran, and read that teardown as damage.
        self._damage_verdict_given = True
        baseline = getattr(self.cluster_state, "baseline", None)
        if baseline is None:
            self.logger.warning("[DAMAGE] No baseline snapshot, so no verdict can be given")
            return DamageReport(verdict=DAMAGED, changed=["no baseline snapshot was captured"])
        try:
            current = self.cluster_state.capture_baseline()
        except Exception as exc:
            self.logger.warning(f"[DAMAGE] Could not re-read the cluster, so no verdict can be given: {exc}")
            return DamageReport(verdict=DAMAGED, changed=[f"the cluster could not be re-read: {exc}"])
        survivors = self._objects_left_behind() if after_teardown else ()
        report = compare(
            baseline,
            current,
            deleted_by_us=deleted_by_us,
            owned_namespaces=self._run_namespaces(),
            survivors=survivors,
        )
        self.logger.log(logging.ERROR if not report.harmless else logging.INFO, report.text())
        return report

    def _objects_left_behind(self) -> list[str]:
        """Return what this harness still owns on the cluster after a teardown.

        Ownership is read from the objects themselves -- the infrastructure label and the Helm
        release annotations -- or, for scenario-created objects, from the run's ownership ledger
        matched by UID. Nothing is read from a list of names the harness expects to have made, so
        a same-named object belonging to someone else is not reported here. A read that fails is
        a warning and an empty answer: the caller is a verdict, and inventing survivors from an
        unreadable API would be as wrong as hiding them.
        """
        ledger = self._ownership_ledger()
        left: list[str] = []
        for kind in OWNED_CLUSTER_SCOPED_KINDS:
            left.extend(f"{kind}/{name}" for name in owned_names(self.kubectl, kind, log=self.logger, ledger=ledger))
        for namespace in self._observer_namespaces():
            try:
                self.kubectl.exec_command_checked(f"kubectl get namespace {self._q(namespace)} -o name", timeout=30)
            except Exception:
                continue
            left.append(f"namespace/{namespace}")
        left.extend(f"namespace/{name}" for name in ledger_namespaces_present(self.kubectl, ledger, log=self.logger))
        left.extend(f"persistentvolume/{name}" for name in self._report_surviving_volumes())
        return sorted(set(left))

    def _verify_scc_grant(self) -> None:
        """Make the `anyuid` SCC prerequisite visible before the first destructive step.

        The application images need `anyuid` on OpenShift: several apps pin a non-root
        UID that `restricted-v2` refuses, and mongo and memcached run as root. The
        harness never grants it, and app namespaces are created per problem, so this
        asks the API server about each namespace's service-account group up front and
        refuses on a confirmed gap. A review the identity may not read is a warning
        and the run continues; see `sregym.service.admission_preflight`.

        Skipped on `kind`, which has no SCCs: no read and no log line there.
        """
        if not is_openshift():
            return
        result = check_scc_grant(self.kubectl, self._run_namespaces())
        if result.state == MISSING:
            raise RuntimeError(
                f"The anyuid SCC is not granted to the service accounts in {', '.join(result.missing)}, so the "
                "application pods that pin a non-root UID, and mongo and memcached, which run as root, are refused "
                "by restricted-v2 admission. The harness never grants an SCC; an operator grants it once per "
                "namespace:\n" + "\n".join(grant_commands(result.missing))
            )
        # `cannot_verify` is already reported by the preflight, with the same commands,
        # and the run continues: SCC objects and their bindings are cluster-scoped, so a
        # project-scoped identity cannot read them.
        self._warn_promtail_privileged()

    def _warn_promtail_privileged(self) -> None:
        """Say once that Promtail has no `privileged` grant, which costs every log line.

        Warn-only, and only when this run deploys Loki. Measured on ROSA 4.22.18: without the grant
        every promtail pod is refused, and with `hostmount-anyuid` the pods are admitted but cannot read
        `/var/log/pods`, because the container carries the namespace's MCS label. Either way Loki holds
        nothing and the Loki-backed tools return no log line for the whole run, so the warning says that
        instead of hedging. It is still not a refusal: the fault lifecycle and the scores do not need
        Promtail, and the harness never runs the command it prints.
        """
        if not getattr(getattr(self, "config", None), "deploy_loki", False):
            return
        if check_promtail_privileged(self.kubectl) is False:
            self.logger.warning(
                "No privileged SCC binding for the promtail service account in observe. Measured on ROSA: "
                "Promtail then reads no log file at all, so Loki holds nothing and every Loki-backed tool "
                "returns empty for this run. Grant it once per cluster, before or during the run; the "
                "binding is cluster-scoped, so a teardown does not remove it:\n%s",
                promtail_grant_command(),
            )

    def _run_namespaces(self) -> list[str]:
        """Every namespace this run deploys into, deduplicated, in deployment order.

        Read from the objects that will deploy there rather than from literals: the
        telemetry stack owns `observe` and the MCP server owns `sregym`, and a composite
        application spans several namespaces, which `deploy_app` reads the same way.
        Each attribute is read with `getattr` so a partly built conductor, or a problem
        whose app is not attached, answers for what it does know instead of failing
        inside a preflight.
        """
        problem = getattr(self, "problem", None)
        app = getattr(problem, "app", None)
        components = (getattr(self, "prometheus", None), getattr(self, "mcp_server", None))
        namespaces = [
            *(getattr(component, "namespace", None) for component in components),
            getattr(problem, "namespace", None),
            *(getattr(app, "namespaces", None) or ()),
            getattr(app, "namespace", None),
        ]
        return list(dict.fromkeys(namespace for namespace in namespaces if namespace))

    def required_binaries(self) -> list[str]:
        """Return the host binaries this run needs on PATH.

        kubectl and helm drive the cluster on every platform. docker is needed
        only when the harness drives a local container runtime: the kind udev
        preflight and the node-level injectors shell out to docker, and an agent
        that is launched in a container is the other consumer. A validator run, or
        an agent registered without container isolation, launches no container, so
        a managed platform needs no Docker daemon on the runner host.
        """
        binaries = ["kubectl", "helm"]
        if get_platform() == KIND or self.agent_launches_container:
            binaries.append("docker")
        return binaries

    def dependency_check(self, binaries: list[str]):
        for b in binaries:
            if shutil.which(b) is None:
                self.logger.error(f"Required dependency '{b}' not found.")
                raise RuntimeError(f"[❌] Required dependency '{b}' not found.")

    def get_problem_stages(self):
        """Record which stages this run intends to attempt.

        Precedence: the run's own configuration (`--stages`), then the legacy
        per-problem `tasklist.yml`, then every stage. Whether a stage *can* run
        is a separate question, answered in `_build_stage_sequence` from the
        oracles the problem actually attaches; this method only records intent.
        """
        if self.config.stages is not None:
            requested = list(self.config.stages)
            if not is_ordered_subset(requested, list(ALL_STAGES)):
                msg = f"Requested stages {requested} must be a subset of {list(ALL_STAGES)}, in that order"
                self.logger.error(msg)
                raise ValueError(msg)
            self.logger.info(f"Stages requested for this run: {requested}")
            self.tasklist = requested
            return

        file_dir = Path(__file__).resolve().parent
        tasklist_path = file_dir / "tasklist.yml"

        # If tasklist file doesn't exist, default to running diagnosis + mitigation
        if not tasklist_path.exists():
            self.logger.info("No tasklist.yml found. Defaulting to running diagnosis and mitigation for this problem.")
            self.tasklist = list(ALL_STAGES)
            return

        with open(tasklist_path) as f:
            tasklist = yaml.safe_load(f)
            if not tasklist:
                msg = "Badly formatted tasklist.yml"
                self.logger.error(msg)
                raise RuntimeError(msg)
            problems = tasklist["all"]["problems"]

        if self.problem_id not in (problems if problems else []):
            self.logger.warning("problem_id not found in tasklist. Defaulting to running diagnosis and mitigation.")
            self.tasklist = list(ALL_STAGES)
        else:
            problem_tasklist = problems[self.problem_id]
            if not problem_tasklist:
                msg = f"No tasks specified for {self.problem_id}"
                self.logger.error(msg)
                raise RuntimeError(msg)

            if not is_ordered_subset(problem_tasklist, list(ALL_STAGES)):
                msg = f"Task list for {self.problem_id} is either out of order or has an unknown step (allowed: diagnosis, mitigation)"
                self.logger.error(msg)
                raise RuntimeError(msg)

            self.logger.info(f"Tasklist specified for {self.problem_id}. Configured stages to run: {problem_tasklist}")

            # Use the tasklist as-is (stage names: diagnosis, mitigation)
            self.tasklist = problem_tasklist

    def _build_stage_sequence(self):
        """Build the sequence of stages (diagnosis, mitigation) based on tasklist and available oracles."""
        self.stage_sequence = []
        self.current_stage_index = 0
        self.waiting_for_agent = False
        self._evaluating = False
        self._accepting_submissions = False
        self.fault_injected = False

        if not self.tasklist:
            self.logger.warning("Empty tasklist; no stages configured for this problem.")
            return

        # Map stage names to their evaluation functions
        stage_definitions = {
            "diagnosis": self._evaluate_diagnosis,
            "mitigation": self._evaluate_mitigation,
        }

        # A stage the caller named explicitly is a different thing from one that
        # came from the default or tasklist.yml: an absent oracle is a conflict
        # in the first case and merely a fact in the second.
        explicitly_requested = self.config.stages is not None

        # Determine which stages are actually available (oracle attached)
        for name in self.tasklist:
            evaluation = stage_definitions.get(name)
            if evaluation is None:
                self.logger.warning(f"Unknown stage '{name}' in tasklist; skipping.")
                continue

            if getattr(self.problem, f"{name}_oracle", None):
                self.stage_sequence.append(
                    {
                        "name": name,
                        "evaluation": evaluation,
                    }
                )
                continue

            if explicitly_requested:
                # Skipping quietly here would report a successful run that
                # measured nothing at all.
                msg = f"Stage {name!r} was requested for {self.problem_id!r}, but it has no {name}_oracle"
                self.logger.error(msg)
                raise ValueError(msg)

            self.logger.info(f"⏩ {name.capitalize()} oracle is not attached. Skipping {name}.")

        if not self.stage_sequence:
            self.logger.warning(
                "No stages left after checking oracles. This problem will complete without agent interaction."
            )

    def _preflight_active_oracles(self) -> None:
        """Check judge credentials once, only if an effective stage uses them."""
        if self._judge_preflight_done or self._judge_preflight is None:
            return
        if any(
            getattr(getattr(self.problem, f"{stage['name']}_oracle"), "requires_llm_judge", False)
            for stage in self.stage_sequence
        ):
            self._judge_preflight()
            self._judge_preflight_done = True

    def _inject_fault(self):
        """Inject fault and prepare diagnosis checkpoint if available."""
        problem = self.current_problem

        # Snapshot the healthy cluster before breaking it. The oracle is built
        # in Problem.__init__, which runs before deploy_app(), so this is the
        # first point at which the app actually exists. Also lets a mitigation
        # oracle ignore chronic pre-existing noise (e.g. ContainerCPUThrottling
        # from the astronomy-shop Grafana sidecar, SREGym#745) and grade only
        # the injected fault.
        mitigation_oracle = getattr(problem, "mitigation_oracle", None)
        if mitigation_oracle is not None:
            mitigation_oracle.capture_baseline()

        problem.inject_fault()
        self.logger.info("[ENV] Injected fault")
        self.fault_injected = True

        # Prepare diagnosis checkpoint if available, after fault injection but before agent stages
        if (
            hasattr(problem, "diagnosis_oracle")
            and problem.diagnosis_oracle
            and isinstance(problem.diagnosis_oracle, DiagnosisOracle)
        ):
            problem.diagnosis_oracle.load_diagnosis_checkpoint()
            self.logger.info("Diagnosis checkpoint loaded after fault injection.")

    def _evaluate_diagnosis(self, solution):
        """Evaluation logic for diagnosis stage."""
        problem = self.current_problem

        self.logger.info("Start Eval for Diagnosis", extra={"sol": solution})
        try:
            r = problem.diagnosis_oracle.evaluate(solution)
        except Exception as e:
            self.logger.exception("Diagnosis oracle raised; recording as failure to avoid a stuck stage.")
            r = {**Oracle.fail_from_exception(e), "error": f"{type(e).__name__}: {e}"}
        r["submission"] = solution
        self.logger.info(
            f"[EVAL] Diagnosis {'Succeed' if r.get('success') else 'Failed'}\n "
            f"TTL: {time.time() - self.execution_start_time}"
        )
        return r

    def _evaluate_mitigation(self, solution):
        """Evaluation logic for mitigation stage."""
        problem = self.current_problem
        # Currently mitigation_oracle.evaluate() does not take the agent solution directly.
        self.logger.info("Start Eval for Mitigation", extra={"sol": solution})
        try:
            r = problem.mitigation_oracle.evaluate()
        except Exception as e:
            self.logger.exception("Mitigation oracle raised; recording as failure to avoid a stuck stage.")
            # Keep the existing top-level error field for result consumers.
            r = {**Oracle.fail_from_exception(e), "error": f"{type(e).__name__}: {e}"}
        self.logger.info(
            f"[EVAL] Mitigation {'Succeed' if r.get('success') else 'Failed'}\n "
            f"TTM: {time.time() - self.execution_start_time}"
        )
        return r

    def bind_phase_ledger(self, path, **context) -> None:
        """Point the phase ledger at this attempt's run directory.

        Called per attempt by the caller that owns the directory, so each
        attempt gets its own `phases.jsonl` rather than appending to a shared
        one.
        """
        self.phases = PhaseLedger(path, context={"problem_id": self.problem_id, **context})

    def _phase(self, name: str, **fields):
        """Time a block as a phase, or do nothing if no ledger is bound.

        A missing ledger must never change control flow, so the fallback is a
        real no-op context manager rather than a branch at every call site.
        `getattr` rather than attribute access: instrumentation must also
        survive a Conductor built without __init__, which several tests and
        external callers do.
        """
        ledger = getattr(self, "phases", None)
        if ledger is None:
            return contextlib.nullcontext()
        return ledger.phase(name, **fields)

    def _mark(self, name: str, event: str, **fields) -> None:
        """Record one boundary for a phase that is not a block.

        The agent stages start when the conductor opens them for submission and
        end when their evaluation returns -- two different call sites, so they
        cannot use the context manager.
        """
        ledger = getattr(self, "phases", None)
        if ledger is not None:
            ledger.record(name, event, **fields)

    def _phase_is_open(self, name: str) -> bool:
        """True if `name` was started and not yet ended. False with no ledger."""
        ledger = getattr(self, "phases", None)
        return ledger is not None and ledger.is_open(name)

    async def _advance_to_next_stage(self, start_index: int = 0):
        """
        Advance to the next stage starting from start_index.
        If there are more stages, set up for agent submission.
        Otherwise, finish the problem.
        """
        self.waiting_for_agent = False
        self.current_stage_index = start_index

        if not self.stage_sequence:
            self.logger.info("No stages configured; starting problem cleanup.")
            self.finish_problem_in_background()
            return

        # Inject the fault once before the first stage.
        if start_index == 0 and not self.fault_injected:
            with self._phase("inject_fault"):
                self._inject_fault()

            # Let the fault surface in telemetry before the agent sees it.
            propagation = (
                self.config.propagation_override_s
                if self.config.propagation_override_s is not None
                else self.problem.propagation_duration_s
            )
            if propagation > 0:
                self.logger.info(f"[PROPAGATION] Waiting {propagation}s for the fault to reach telemetry...")
                with self._phase("propagation", seconds=propagation):
                    await asyncio.sleep(propagation)
                self.logger.info("[PROPAGATION] Propagation window complete.")

        if start_index < len(self.stage_sequence):
            stage = self.stage_sequence[start_index]
            stage_name: str = stage["name"]

            self.logger.debug(f"Advancing to stage '{stage_name}' and waiting for agent.")
            self.waiting_for_agent = True
            self.submission_stage = stage_name
            self._accepting_submissions = not self._attempt_closed
            self.logger.info(f"[STAGE] Go to stage {self.submission_stage}")
            # The agent's clock for this stage starts here: submissions are now
            # accepted. Ends where its evaluation returns.
            self._mark(f"stage:{stage_name}", "start")

            # Update NoiseManager stage
            if self.config.enable_noise:
                try:
                    nm = get_noise_manager()
                    nm.set_stage(stage_name)
                except Exception as e:
                    self.logger.warning(f"Failed to set NoiseManager stage: {e}")
        else:
            # No more stages; finish the problem
            self.finish_problem_in_background()

    def _cleanup_sync(self, generation: int | None = None):
        """
        Blocking cleanup operations (fault recovery, app teardown, reconciliation).
        Captures self.problem at entry so that start_problem() can safely replace
        self.problem/self.app for the next problem without affecting this cleanup.
        """
        # Snapshot the problem reference immediately so that any concurrent
        # replacement of self.problem by start_problem() does not affect this cleanup.
        problem = self.problem
        cleanup_generation = self._submission_generation if generation is None else generation
        cleanup_errors: list[str] = []

        def cleanup_was_abandoned() -> bool:
            with self._submission_lock:
                return (
                    cleanup_generation != self._submission_generation
                    or cleanup_generation in self._aborted_submission_generations
                )

        def stop_late_cleanup() -> bool:
            if not cleanup_was_abandoned():
                return False
            self.logger.warning(
                "Stopping late cleanup work for aborted or replaced attempt generation %s",
                cleanup_generation,
            )
            return True

        self.logger.info("[CLEANUP] Starting cleanup (fault recovery, undeploy, reconcile)")

        # Stop noises
        if self.config.enable_noise:
            get_noise_manager().stop()
            self.logger.info("[CLEANUP] NoiseManager stopped")

        if stop_late_cleanup():
            return

        # Recover fault using the captured problem reference
        if problem:
            self.logger.info("[CLEANUP] Recovering fault...")
            try:
                problem.recover_fault()
                self.logger.info("[CLEANUP] Fault recovered")
            except Exception as exc:
                self.logger.exception("[CLEANUP] Fault recovery failed")
                cleanup_errors.append(f"recover_fault: {type(exc).__name__}: {exc}")

        if stop_late_cleanup():
            return

        # Undeploy app using the captured problem reference
        self.logger.info("[CLEANUP] Undeploying app...")
        if problem:
            try:
                problem.app.cleanup()
                self.logger.info("[CLEANUP] App undeployed")
            except Exception as exc:
                self.logger.exception("[CLEANUP] App cleanup failed")
                cleanup_errors.append(f"app_cleanup: {type(exc).__name__}: {exc}")

        if stop_late_cleanup():
            return

        # OpenShift: collect what this run's ledger recorded and the recovery above left on the
        # cluster, by UID. Kind keeps its main behaviour, which has no ledger.
        if is_openshift():
            try:
                removed = delete_ledger_objects(self.kubectl, self._ownership_ledger(), log=self.logger)
                if removed:
                    self.logger.info(f"[CLEANUP] Removed ledger-recorded objects: {removed}")
            except Exception as e:
                self.logger.exception("[CLEANUP] Failed to remove ledger-recorded objects")
                cleanup_errors.append(f"ledger_cleanup: {type(e).__name__}: {e}")

        # Reconcile cluster state to baseline
        if self._baseline_captured:
            try:
                if not self._baseline_usable:
                    self.logger.info(
                        "[CLEANUP] Reconcile skipped: the persisted baseline names namespaces that no longer "
                        "exist, so it is neither usable nor replaced"
                    )
                elif self.reconcile == RECONCILE_ON:
                    self.logger.info("[CLEANUP] Reconciling cluster state to baseline...")
                    changes = self.cluster_state.reconcile_to_baseline()
                    if any(v for v in changes.values() if v):
                        self.logger.info(f"Cluster state reconciliation changes: {changes}")
                    self.logger.info("[CLEANUP] Cluster state reconciled")
                elif self.reconcile == RECONCILE_REPORT:
                    mutations = self.cluster_state.plan_reconcile()
                    self.logger.info(f"[CLEANUP] Reconcile report: {len(mutations)} mutations, none applied")
                    for mutation in mutations:
                        self.logger.info(f"[CLEANUP] would {mutation.action} {mutation.target} — {mutation.note}")
                else:
                    self.logger.info(
                        "[CLEANUP] Reconcile skipped (off): the baseline is kept for reporting, "
                        "it is not an ownership map"
                    )
            except Exception as e:
                self.logger.exception("[CLEANUP] Failed to reconcile cluster state")
                cleanup_errors.append(f"cluster_reconcile: {type(e).__name__}: {e}")

        # OpenShift keeps the observer stack between scenarios unless it is reset here. Only with
        # reconcile off, the mode in which the baseline is kept for reporting and not reconciled
        # against, and only when this run deploys Loki. The reset runs after the reconcile, so
        # reconcile never sees a stack that was just removed. A failed reset is not a cleanup
        # error: the next deploy waits for `observe` and redeploys it, so the campaign goes on.
        if is_openshift() and self.reconcile == RECONCILE_OFF and self.config.deploy_loki:
            try:
                self._reset_observers()
                self.logger.info("[CLEANUP] Observer stack reset")
            except Exception as e:
                self.logger.warning(f"[CLEANUP] Observer stack reset failed, the next deploy will redeploy it: {e}")

        if stop_late_cleanup():
            return

        # Set to "done" after all cleanup is complete.
        with self._submission_lock:
            if cleanup_was_abandoned():
                self.logger.warning(
                    "Ignoring late cleanup state for aborted or replaced attempt generation %s",
                    cleanup_generation,
                )
                return
            if cleanup_errors:
                self.results["cleanup_failed"] = True
                self.results["cleanup_error"] = "; ".join(cleanup_errors)
            self.submission_stage = "done"
            self._accepting_submissions = False
            self._attempt_closed = True
        self.logger.info("[CLEANUP] Cleanup complete, stage set to 'done'")

    def _finish_problem(self, generation: int | None = None):
        """
        Runs problem teardown synchronously (fault recovery, app undeploy, cluster
        reconciliation) before returning.

        Idempotent: safe to call from multiple paths (submit flow via
        ``_advance_to_next_stage``, and ``main.py`` as a post-exit safety net).
        The first call runs cleanup; subsequent calls no-op. ``start_problem()``
        resets the stage to ``"setup"``, so the deploy-retry path still cleans up
        each failed attempt.
        """
        cleanup_generation = self._submission_generation if generation is None else generation
        with self._submission_lock:
            if (
                cleanup_generation != self._submission_generation
                or cleanup_generation in self._aborted_submission_generations
            ):
                self.logger.warning(
                    "Skipping cleanup transition for aborted or replaced attempt generation %s",
                    cleanup_generation,
                )
                return
            if self.submission_stage in ("done", "tearing_down"):
                self.logger.info(
                    f"[STAGE] _finish_problem already ran/running (submission_stage={self.submission_stage!r}); skipping"
                )
                return
            self.logger.info("[STAGE] Done, starting teardown")
            # A stage still open at teardown never received a submission -- the
            # agent exited, timed out, or was killed. Close it here with a
            # written-down end rather than leaving the reader to infer one from
            # the next phase's start.
            open_stage = self.submission_stage
            self._accepting_submissions = False
            self._attempt_closed = True
            self.submission_stage = "tearing_down"
        # Only close a stage that is genuinely still open. `submission_stage`
        # still names the last stage after it has been evaluated, so closing on
        # that alone writes a second end for a stage that finished cleanly.
        if open_stage in {stage["name"] for stage in self.stage_sequence} and self._phase_is_open(
            f"stage:{open_stage}"
        ):
            self._mark(f"stage:{open_stage}", "end", outcome="no_submission")
        with self._phase("cleanup"):
            self._cleanup_sync(cleanup_generation)
        self.logger.info("[STAGE] Teardown complete")

    def finish_problem_in_background(self) -> concurrent.futures.Future:
        """Return running teardown or start it so the driver can enforce a deadline."""
        with self._submission_lock:
            if self._submit_future is not None:
                if self.submission_stage in ("tearing_down", "done"):
                    return self._submit_future
                raise RuntimeError("Cannot start cleanup while submission work is still active.")
            if self.submission_stage == "tearing_down":
                raise RuntimeError(
                    "Cleanup stopped unexpectedly while tearing down; it cannot be treated as complete or retried safely."
                )
            generation = self._submission_generation
            future: concurrent.futures.Future = concurrent.futures.Future()

            def run_cleanup() -> None:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    self._finish_problem(generation)
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        future.set_exception(exc)
                    else:
                        future.set_exception(RuntimeError(f"Cleanup terminated with {type(exc).__name__}: {exc}"))
                else:
                    future.set_result(None)

            self._submit_future = future
            threading.Thread(
                target=run_cleanup,
                name=f"sregym-cleanup-{generation}",
                daemon=True,
            ).start()
            return future

    async def start_problem(self) -> StartProblemResult:
        """
        1) Provision infra & workload
        2) Initialize Act registry and execute initial GymActs and first AgentAct precondition

        Returns:
            StartProblemResult.SUCCESS once the problem is running; failures raise.
        """
        if self.problem_id is None:
            raise RuntimeError("Cannot start problem: problem_id is not set")

        with self._submission_lock:
            if self._submission_generation in self._aborted_submission_generations:
                raise RuntimeError(
                    "This Conductor abandoned a stuck evaluator and cannot be reused. "
                    "Start a new benchmark process so late external side effects cannot overlap another attempt."
                )

        # Wait for every accepted evaluation and any API request that is waiting
        # for the next stage. This prevents a late request from one attempt from
        # being submitted against the next problem.
        # Close the old attempt before taking the drain snapshot. A request
        # that was already registered can finish, but no later request can
        # cross this boundary into the new attempt.
        self.close_submissions()
        if self._submit_future is not None or self._pending_submission_stages:
            self.logger.info("[WAIT] Waiting for previous problem's submission work to finish...")
            await self.wait_for_submission_work(timeout=None)
            self.logger.info("[WAIT] Previous problem's submission work finished")
        with self._submission_lock:
            self._submit_future = None
            self._submission_generation += 1
            self._accepting_submissions = False
            self._attempt_closed = False
            self.submission_stage = "setup"
            self.waiting_for_agent = False
            self._evaluating = False
            self.problem = None
            self.app = None
            self.results = {}
            self.agent_timeout_seconds = None
            self.agent_deadline_monotonic = None

        self.execution_start_time = time.time()
        self.problem = self.problems.get_problem_instance(self.problem_id)
        self.app = self.problem.app
        self.detection_oracle = DetectionOracle(self.problem)

        self.verify_platform()
        # Read-only, and before the first destructive step: the anyuid SCC grant is what
        # the application images need on OpenShift, and the harness never grants it.
        self._verify_scc_grant()

        required_binaries = self.required_binaries()
        self.dependency_check(required_binaries)
        self.logger.debug(f"Dependency check passed: {', '.join(required_binaries)}")

        self.logger.info(f"[Session Start] Problem ID: {self.problem_id}")
        self.logger.info(f"[STAGE] Start testing on problem: {self.problem_id}")

        self.get_problem_stages()
        self._build_stage_sequence()
        self._preflight_active_oracles()

        with self._phase("fix_kubernetes"):
            self.fix_kubernetes()

        self.logger.info("Undeploying app leftovers...")
        with self._phase("undeploy_leftovers"):
            self.undeploy_app()  # Cleanup any leftovers
        self.logger.info("App leftovers undeployed.")
        self.logger.info("Deploying app...")
        with self._phase("deploy"):
            self.deploy_app()
        self.logger.info("App deployed.")

        baseline = (
            self.config.baseline_override_s
            if self.config.baseline_override_s is not None
            else self.problem.baseline_duration_s
        )
        if baseline > 0:
            self.logger.info(f"[BASELINE] Running steady-state for {baseline}s before fault injection...")
            with self._phase("baseline", seconds=baseline):
                await asyncio.sleep(baseline)
            self.logger.info("[BASELINE] Baseline period complete.")

        # Update NoiseManager with problem context
        if self.config.enable_noise:
            try:
                nm = get_noise_manager()
                context = {
                    "namespace": self.app.namespace,
                    "app_name": self.app.name,
                    # We can add more info here if needed, e.g. service list
                }
                nm.set_problem_context(context)
                nm.start()
            except Exception as e:
                self.logger.warning(f"Failed to update NoiseManager context: {e}")

        # After deployment, advance to the first stage
        await self._advance_to_next_stage(start_index=0)

        self.execution_start_time = time.time()  # Reset: measure agent time only

        if self.submission_stage and self.submission_stage != "done":
            self.logger.info(f"✅ Deployment complete. Ready for submission. Current stage is: {self.submission_stage}")
        else:
            self.logger.info(
                "✅ Deployment complete. No stages configured; problem will complete without agent submission."
            )
        return StartProblemResult.SUCCESS

    def _submit_evaluate_and_advance(self, sol, current_stage, generation: int):
        """
        Blocking work for a submission: evaluate the oracle, advance stage, manage noise.
        Runs in a background thread so the HTTP response is not blocked.
        """
        stage_name: str = current_stage["name"]
        self.logger.info(f"Evaluating stage '{stage_name}'", extra={"sol": sol})

        # Stop noise before evaluation to ensure clean environment
        if self.config.enable_noise:
            self.logger.info("Stopping noise manager before evaluation...")
            get_noise_manager().stop()

        # The agent's time on this stage ends when a submission arrives to be
        # evaluated; grading time is its own phase, not the agent's.
        self._mark(f"stage:{stage_name}", "end", outcome="submitted")

        outcome = None
        # Run the evaluation function for the current stage. The per-stage
        # _evaluate_* methods catch their own oracle exceptions; this outer
        # guard is defense in depth so an ordinary evaluation error still
        # produces a failed stage result.
        try:
            with self._phase(f"evaluate:{stage_name}"):
                outcome = current_stage["evaluation"](sol)
        except Exception:
            self.logger.exception(
                f"Stage '{stage_name}' evaluation raised unexpectedly; advancing anyway "
                "so the conductor doesn't get stuck waiting on a dead stage."
            )
            outcome = {"success": False, "error": "stage evaluation raised", "submission": sol}

        next_index = self.current_stage_index + 1
        next_stage_name: str | None = None
        with self._submission_lock:
            if generation != self._submission_generation or generation in self._aborted_submission_generations:
                self.logger.warning(
                    "Discarding %s result because attempt generation %s was aborted or replaced",
                    stage_name,
                    generation,
                )
                return

            if outcome is not None:
                self.results[stage_name.capitalize()] = outcome
                timing_key = "TTL" if stage_name == "diagnosis" else "TTM"
                self.results[timing_key] = time.time() - self.execution_start_time

            if next_index < len(self.stage_sequence):
                next_stage_name = self.stage_sequence[next_index]["name"]
            stage_ledger = getattr(self, "phases", None)

        if next_stage_name is not None:
            # Keep the old stage marked busy while noise is restarted. The
            # API can wait for the next stage, but cannot submit into a
            # partially completed transition. Do not hold the submission lock
            # across NoiseManager I/O because a stuck noise backend must not
            # prevent the driver from closing or abandoning the attempt.
            if self.config.enable_noise:
                nm = None
                try:
                    nm = get_noise_manager()
                    self.logger.info("Restarting noise manager for next stage...")
                    nm.start()
                    with self._submission_lock:
                        transition_aborted = (
                            generation != self._submission_generation
                            or generation in self._aborted_submission_generations
                        )
                    if not transition_aborted:
                        nm.set_stage(next_stage_name)
                except Exception as e:
                    self.logger.warning(f"Failed to restart NoiseManager: {e}")

                # A slow start/set_stage can finish after the driver abandons
                # the evaluator and performs its global noise stop. Stop again
                # so this late worker cannot leave noise running.
                with self._submission_lock:
                    transition_aborted = (
                        generation != self._submission_generation or generation in self._aborted_submission_generations
                    )
                if transition_aborted and nm is not None:
                    try:
                        nm.stop()
                    except Exception as e:
                        self.logger.warning(f"Failed to stop late NoiseManager restart: {e}")
                    return

            # Keep submissions waiting until the start record is written.
            # Use this attempt's ledger, and keep disk I/O outside the lock.
            if stage_ledger is not None:
                stage_ledger.record(f"stage:{next_stage_name}", "start")
            with self._submission_lock:
                transition_aborted = (
                    generation != self._submission_generation or generation in self._aborted_submission_generations
                )
                if transition_aborted:
                    self.logger.warning(
                        "Skipping %s stage transition because attempt generation %s was aborted or replaced",
                        next_stage_name,
                        generation,
                    )
                else:
                    self.current_stage_index = next_index
                    self.submission_stage = next_stage_name
                    self.waiting_for_agent = True
                    self._evaluating = False
                    self._accepting_submissions = not self._attempt_closed
                    self.logger.info(f"[STAGE] Go to stage {self.submission_stage}")
            if transition_aborted and stage_ledger is not None:
                stage_ledger.record(f"stage:{next_stage_name}", "end", outcome="aborted")
            return

        if next_index >= len(self.stage_sequence):
            # Keep the accepted evaluation marked as active until teardown
            # finishes. A concurrent submit observes ``tearing_down`` instead
            # of a brief, invalid "idle mitigation" state.
            try:
                self._finish_problem(generation)
            finally:
                with self._submission_lock:
                    if generation == self._submission_generation:
                        self._evaluating = False

    async def submit(
        self,
        solution: str | None,
        *,
        expected_stage: str | None = None,
        expected_generation: int | None = None,
    ) -> dict:
        """
        Called by CLI or HTTP /submit.  Kicks off evaluation in the
        background and returns immediately.
        """
        sol = solution

        with self._submission_lock:
            if expected_generation is not None and expected_generation != self._submission_generation:
                raise SubmissionAttemptMismatch(expected_generation, self._submission_generation)

            registered_request = (
                expected_generation is not None
                and self._pending_submission_stages.get((expected_generation, expected_stage or ""), 0) > 0
            )
            if not self._accepting_submissions and not registered_request:
                raise SubmissionAttemptClosed("This attempt no longer accepts new submissions.")

            # If all tasks are already completed, simply return the final snapshot.
            if self.submission_stage == "done":
                self.logger.info("All tasks already completed; ignoring new submission.")
                return dict(self.results)

            # If teardown is in progress, return current results without evaluation
            if self.submission_stage == "tearing_down":
                self.logger.info("Teardown in progress; returning current results without evaluation.")
                return dict(self.results)

            if not self.stage_sequence:
                self.logger.warning("submit() called but no stages are configured; returning current results.")
                return dict(self.results)

            if expected_stage is not None and self.submission_stage != expected_stage:
                raise SubmissionStageMismatch(expected_stage, self.submission_stage)

            if not self.waiting_for_agent:
                if self._evaluating:
                    self.logger.info(
                        f"submit() called while evaluation is already in progress for stage '{self.submission_stage}'."
                    )
                    raise EvaluationInProgress(self.submission_stage)
                self.logger.error(
                    "submit() called when conductor is not waiting for a submission. "
                    f"Current submission_stage={self.submission_stage}"
                )
                raise RuntimeError("Conductor is not currently waiting for an agent submission.")

            current_stage = self.stage_sequence[self.current_stage_index]
            accepted_stage: str = current_stage["name"]

            # Mark that we're no longer waiting so duplicate submits are rejected.
            self.waiting_for_agent = False
            self._evaluating = True

            # Run evaluation and stage advancement in an executor thread so the HTTP
            # response returns immediately. Store the future so start_problem() can
            # await it and guarantee cleanup is fully done before the next problem starts.
            generation = self._submission_generation
            future: concurrent.futures.Future = concurrent.futures.Future()

            def run_evaluation() -> None:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    result = self._submit_evaluate_and_advance(sol, current_stage, generation)
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        future.set_exception(exc)
                    else:
                        future.set_exception(RuntimeError(f"Evaluator terminated with {type(exc).__name__}: {exc}"))
                else:
                    future.set_result(result)

            oracle = getattr(self.problem, f"{accepted_stage}_oracle", None)
            self._submit_evaluation_timeout = getattr(oracle, "evaluation_timeout_seconds", None)
            self._submit_future = future
            threading.Thread(
                target=run_evaluation,
                name=f"sregym-evaluate-{generation}-{accepted_stage}",
                daemon=True,
            ).start()

        return {"status": "accepted", "message": "Submission received", "stage": accepted_stage}

    def _register_pending_submission_locked(self, stage: str) -> int:
        if self._attempt_closed or not self._accepting_submissions:
            raise SubmissionAttemptClosed("This attempt no longer accepts new submissions.")
        generation = self._submission_generation
        key = (generation, stage)
        self._pending_submission_stages[key] = self._pending_submission_stages.get(key, 0) + 1
        return generation

    def register_submission_request(
        self,
        solution: str,
        stage: SubmissionStage | None = None,
    ) -> tuple[SubmissionStage, int]:
        """Resolve and register one request atomically.

        Legacy clients omit the stage. Preserve the current-stage behavior and
        recognize an empty request during diagnosis grading as early mitigation.
        """
        with self._submission_lock:
            requested_stage = stage
            if requested_stage is None:
                if self.submission_stage == "mitigation":
                    requested_stage = "mitigation"
                elif self.submission_stage == "diagnosis":
                    requested_stage = "mitigation" if self._evaluating and solution == "" else "diagnosis"
                else:
                    requested_stage = "mitigation" if solution == "" else "diagnosis"
            if requested_stage not in SUBMISSION_STAGES:
                raise ValueError(f"Unknown submission stage: {requested_stage!r}")
            generation = self._register_pending_submission_locked(requested_stage)
            return requested_stage, generation

    def register_pending_submission(self, stage: str) -> int:
        """Track a known-stage request and return its attempt generation."""
        with self._submission_lock:
            return self._register_pending_submission_locked(stage)

    def submission_state(self) -> tuple[str | None, bool, int]:
        """Return the agent-facing stage, evaluation flag, and attempt generation atomically."""
        with self._submission_lock:
            return self.submission_stage, self._evaluating, self._submission_generation

    def unregister_pending_submission(self, stage: str, generation: int) -> None:
        """Stop tracking an API request after it is accepted or rejected."""
        with self._submission_lock:
            key = (generation, stage)
            remaining = self._pending_submission_stages.get(key, 0) - 1
            if remaining > 0:
                self._pending_submission_stages[key] = remaining
            else:
                self._pending_submission_stages.pop(key, None)

    def close_submissions(self) -> bool:
        """Atomically reject new requests and report whether registered work remains."""
        with self._submission_lock:
            self._accepting_submissions = False
            self._attempt_closed = True
            return self._submit_future is not None or bool(self._pending_submission_stages)

    def abandon_submission_work(self) -> None:
        """Detach a stuck evaluator and prevent it from mutating this or a later attempt."""
        with self._submission_lock:
            generation = self._submission_generation
            self._aborted_submission_generations.add(generation)
            self._attempt_closed = True
            self._accepting_submissions = False
            self.waiting_for_agent = False
            self._evaluating = False
            self.submission_stage = "aborted"
            self._submit_future = None

    async def wait_for_submission_evaluations(self, timeout: float | None) -> None:
        """Wait for accepted stage requests and grading, but not teardown.

        Give each accepted stage evaluation a fresh deadline. An early
        mitigation request can wait behind diagnosis grading. Each will get
        their own deadlines.

        The final stage worker performs teardown in the same future. Return as
        soon as that worker enters ``tearing_down`` so the driver can apply a
        separate cleanup deadline without allowing attempts to overlap.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        observed_future: concurrent.futures.Future | None = None

        while True:
            with self._submission_lock:
                stage = self.submission_stage
                future = self._submit_future
                pending = bool(self._pending_submission_stages)
                oracle_timeout = getattr(self, "_submit_evaluation_timeout", None)

            if future is not None and future is not observed_future:
                observed_future = future
                stage_timeout = timeout
                if timeout is not None and type(oracle_timeout) in (int, float) and math.isfinite(oracle_timeout):
                    stage_timeout = max(timeout, oracle_timeout)
                deadline = loop.time() + stage_timeout if stage_timeout is not None else None

            if future is not None and future.done():
                try:
                    future.result()
                finally:
                    with self._submission_lock:
                        if self._submit_future is future:
                            self._submit_future = None
                continue

            if stage in {"tearing_down", "done"}:
                return

            if future is None and not pending:
                return

            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.sleep(min(0.05, remaining))
            else:
                await asyncio.sleep(0.05)

    async def wait_for_submission_work(self, timeout: float | None) -> None:
        """Wait for all accepted evaluations and registered stage requests.

        An early mitigation request can be waiting while diagnosis is graded.
        When diagnosis finishes, that request creates a new evaluation future.
        Looping until both sources are idle prevents cleanup between those two
        evaluations.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None

        while True:
            with self._submission_lock:
                future = self._submit_future
                pending = bool(self._pending_submission_stages)

            if future is not None:
                try:
                    if future.done():
                        future.result()
                    elif deadline is None:
                        await asyncio.shield(asyncio.wrap_future(future))
                    else:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise TimeoutError
                        await asyncio.wait_for(
                            asyncio.shield(asyncio.wrap_future(future)),
                            timeout=remaining,
                        )
                finally:
                    if future.done():
                        with self._submission_lock:
                            if self._submit_future is future:
                                self._submit_future = None
                continue

            if pending:
                if deadline is not None and loop.time() >= deadline:
                    raise TimeoutError
                await asyncio.sleep(0.05)
                continue

            return

    def missing_submission_stages(self) -> list[str]:
        """Return configured stages that did not produce an evaluation result."""
        return [stage["name"] for stage in self.stage_sequence if stage["name"].capitalize() not in self.results]

    def record_incomplete_attempt(
        self,
        reason: str,
        *,
        agent_return_code: int | None = None,
    ) -> None:
        """Record why an attempt ended without all configured stage results."""
        missing = self.missing_submission_stages()
        current_stage = self.submission_stage
        if current_stage not in {"diagnosis", "mitigation", "tearing_down"}:
            current_stage = missing[0] if missing else "run"

        self.results["run_status"] = "incomplete"
        self.results["incomplete_reason"] = reason
        self.results["incomplete_stage"] = current_stage
        self.results["missing_stages"] = ",".join(missing)
        if agent_return_code is not None:
            self.results["agent_return_code"] = agent_return_code

    def finalize_attempt_status(self) -> str:
        """Set an explicit complete or incomplete status before results are written."""
        if self.results.get("run_status") == "incomplete":
            return "incomplete"
        if self.results.get("cleanup_failed"):
            self.record_incomplete_attempt("cleanup_failed")
            return "incomplete"
        if self.missing_submission_stages():
            self.record_incomplete_attempt("missing_stage_results")
        else:
            self.results["run_status"] = "complete"
        return self.results["run_status"]

    @staticmethod
    def _q(value):
        return shlex.quote(str(value))

    def _fix_calico_route_reflector_label_drift(self):
        self.logger.info("[FIX] Calico route-reflector label drift leftovers if any")
        try:
            from sregym.conductor.problems.calico_route_reflector_label_drift import (
                CalicoRouteReflectorLabelDriftHotelReservation,
            )

            problem = CalicoRouteReflectorLabelDriftHotelReservation
            kubectl = KubeCtl()

            def kubectl_json(command):
                output = kubectl.exec_command(command)
                try:
                    return json.loads(output)
                except json.JSONDecodeError:
                    return None

            def has_problem_label(resource):
                if not resource:
                    return False
                labels = resource.get("metadata", {}).get("labels", {}) or {}
                return labels.get(problem.PROBLEM_LABEL_KEY) == problem.PROBLEM_LABEL_VALUE

            def bool_from_state(data, key):
                if not data or key not in data:
                    return None
                return json.loads(data[key])

            def list_from_state(data, key):
                if not data or key not in data:
                    return None
                return json.loads(data[key])

            nodes = (kubectl_json("kubectl get nodes -o json") or {}).get("items", [])
            marked_nodes = [
                node.get("metadata", {}).get("name")
                for node in nodes
                if (node.get("metadata", {}).get("annotations", {}) or {}).get(problem.NODE_MARKER_ANNOTATION) == "true"
            ]
            marked_nodes = [node for node in marked_nodes if node]

            bgppeer = kubectl_json(f"kubectl get bgppeer {self._q(problem.BGP_PEER_NAME)} --ignore-not-found -o json")
            bgppeers = (kubectl_json("kubectl get bgppeers -o json") or {}).get("items", [])
            bgp_config = kubectl_json("kubectl get bgpconfiguration default --ignore-not-found -o json")
            support_namespace = kubectl_json(
                f"kubectl get namespace {self._q(problem.PROBE_NAMESPACE)} --ignore-not-found -o json"
            )
            state_configmap = kubectl_json(
                f"kubectl -n {self._q(problem.STATE_NAMESPACE)} get configmap "
                f"{self._q(problem.STATE_CONFIGMAP_NAME)} --ignore-not-found -o json"
            )
            state_data = (state_configmap or {}).get("data", {}) or {}

            bgppeer_labeled = has_problem_label(bgppeer)
            bgp_config_labeled = has_problem_label(bgp_config)
            support_namespace_labeled = has_problem_label(support_namespace)
            original_bgppeer_names = list_from_state(state_data, problem.STATE_BGP_PEERS_KEY)
            created_bgppeer_names = set()
            if original_bgppeer_names is not None:
                original_bgppeer_names = set(original_bgppeer_names)
                current_bgppeer_names = {
                    peer.get("metadata", {}).get("name") for peer in bgppeers if peer.get("metadata", {}).get("name")
                }
                created_bgppeer_names = current_bgppeer_names - original_bgppeer_names
            elif bgppeer_labeled:
                created_bgppeer_names.add(problem.BGP_PEER_NAME)
            residue_found = bool(
                marked_nodes
                or created_bgppeer_names
                or bgppeer_labeled
                or bgp_config_labeled
                or support_namespace_labeled
                or state_data
            )
            if not residue_found:
                return

            if support_namespace_labeled:
                kubectl.exec_command(f"kubectl delete namespace {self._q(problem.PROBE_NAMESPACE)} --ignore-not-found")
            for bgppeer_name in sorted(created_bgppeer_names):
                kubectl.exec_command(f"kubectl delete bgppeer {self._q(bgppeer_name)} --ignore-not-found")

            bgp_config_preexisted = bool_from_state(state_data, problem.STATE_CONFIG_PREEXISTED_KEY)
            original_bgp_configuration = state_data.get(problem.STATE_CONFIGURATION_KEY)
            if bgp_config_preexisted is True and original_bgp_configuration:
                kubectl.exec_command("kubectl apply -f -", input_data=original_bgp_configuration)
            elif bgp_config_preexisted is False or bgp_config_labeled:
                kubectl.exec_command("kubectl delete bgpconfiguration default --ignore-not-found")

            state_route_reflector_node = state_data.get(problem.STATE_PRIMARY_NODE_KEY)
            if state_route_reflector_node and state_route_reflector_node not in marked_nodes:
                marked_nodes.append(state_route_reflector_node)
            legacy_label_preexisted = bool_from_state(state_data, problem.STATE_NODE_LABEL_PREEXISTED_KEY)
            rr_annotation_preexisted = bool_from_state(state_data, problem.STATE_NODE_ANNOTATION_PREEXISTED_KEY)
            rr_annotation_value = state_data.get(problem.STATE_NODE_ANNOTATION_VALUE_KEY)

            for node_name in marked_nodes:
                quoted_node = self._q(node_name)
                if legacy_label_preexisted is True:
                    kubectl.exec_command(
                        f"kubectl label node {quoted_node} {self._q(f'{problem.LEGACY_MASTER_LABEL}=')} --overwrite"
                    )
                else:
                    kubectl.exec_command(
                        f"kubectl label node {quoted_node} {self._q(f'{problem.LEGACY_MASTER_LABEL}-')}"
                    )

                if rr_annotation_preexisted is True and rr_annotation_value:
                    kubectl.exec_command(
                        f"kubectl annotate node {quoted_node} "
                        f"{self._q(f'{problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION}={rr_annotation_value}')} --overwrite"
                    )
                else:
                    kubectl.exec_command(
                        f"kubectl annotate node {quoted_node} "
                        f"{self._q(f'{problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION}-')}"
                    )
                kubectl.exec_command(
                    f"kubectl annotate node {quoted_node} {self._q(f'{problem.NODE_MARKER_ANNOTATION}-')}"
                )

            kubectl.exec_command(
                f"kubectl -n {self._q(problem.STATE_NAMESPACE)} delete configmap "
                f"{self._q(problem.STATE_CONFIGMAP_NAME)} --ignore-not-found"
            )
            kubectl.exec_command("kubectl -n kube-system rollout restart ds/calico-node")
            kubectl.exec_command("kubectl -n kube-system rollout status ds/calico-node --timeout=180s")
        except Exception as e:
            self.logger.warning(f"Could not fix Calico route-reflector label drift state: {e}")

    def fix_kubernetes(self):
        """Prepare the cluster for a run, starting with a read-only snapshot of it.

        The snapshot is what the damage verdict compares against, so it has to be taken before
        anything this run creates -- observers included -- and before reconcile, which deletes.
        """
        self.logger.info("Fixing Kubernetes... to normal state.")
        self._capture_run_baseline()

        if is_openshift():
            self.logger.info(
                "[FIX] Skipping the distribution-specific repairs on this platform: the kube-proxy DaemonSet image "
                "recovery, the kubelet crash recovery, the kubelet disk-pressure eviction leftovers, the Calico "
                "IPPool/IPAMConfig reset, the Calico route-reflector label drift cleanup, and the CoreDNS NXDOMAIN "
                "template recovery. They repair components a managed cluster does not run."
            )
            # SREGym can only delete Failed pods in a namespace it owns: a cluster-wide
            # delete would reach other tenants' and platform namespaces.
            self.kubectl.exec_command(
                f"kubectl delete pods -n {self.current_problem.namespace} "
                "--field-selector=status.phase=Failed --ignore-not-found=true"
            )
        else:
            self.logger.info("[FIX] Imbalance leftover if any")

            injector = VirtualizationFaultInjector(namespace="kube-system")
            injector.recover_daemon_set_image_replacement(daemon_set_name="kube-proxy")

            self.logger.info("[FIX] KubeletCrash leftover if any")
            injector = RemoteOSFaultInjector()
            injector.recover_kubelet_crash()

            self.logger.info("[FIX] KubeletEvictionThresholdMisconfig leftover if any")
            injector.recover_disk_pressure_all()
            # Delete Failed pods left by the eviction loop. Skip the app namespace
            # because undeploy_app() tears down the application namespace anyway.
            from sregym.conductor.problems.kubelet_eviction_threshold_misconfig import (
                KubeletEvictionThresholdMisconfig,
            )

            self.kubectl.exec_command(
                f"kubectl delete pods --all-namespaces "
                f"--field-selector=status.phase=Failed,metadata.namespace!={KubeletEvictionThresholdMisconfig.NAMESPACE} "
                "--ignore-not-found=true"
            )

            self.logger.info("[FIX] Calico IPPool/strictAffinity leftover if any")
            try:
                from sregym.conductor.problems.pod_cidr_exhaustion_hotel_reservation import (
                    PodCIDRExhaustionHotelReservation,
                )

                kubectl = KubeCtl()
                kubectl.exec_command(
                    f"kubectl patch ippool {PodCIDRExhaustionHotelReservation.DEFAULT_POOL_NAME} --type=merge "
                    '-p \'{"spec":{"disabled":false}}\''
                )
                kubectl.exec_command(
                    'kubectl patch ipamconfig default --type=merge -p \'{"spec":{"strictAffinity":false}}\''
                )
                kubectl.exec_command(
                    f"kubectl delete ippool {PodCIDRExhaustionHotelReservation.TINY_POOL_NAME} --ignore-not-found"
                )
                kubectl.exec_command(
                    f"kubectl delete namespace {PodCIDRExhaustionHotelReservation.EXHAUST_NAMESPACE} --ignore-not-found"
                )
            except Exception as e:
                self.logger.warning(f"Could not fix Calico IPPool state: {e}")

            self._fix_calico_route_reflector_label_drift()

            self.logger.info("[FIX] Stale CoreDNS NXDOMAIN templates if any")
            injector = VirtualizationFaultInjector(namespace="kube-system")
            try:
                injector.recover_all_nxdomain_templates()
            except Exception as e:
                self.logger.error(f"Failed to recover CoreDNS NXDOMAIN templates: {e}")

        self.logger.info("[FIX] NightlyRebalanceOOM kube-system actor leftover if any")
        try:
            from sregym.conductor.problems.nightly_rebalance_oom import NightlyRebalanceOOM

            NightlyRebalanceOOM.cleanup_leftover_actor()
        except Exception as e:
            self.logger.warning(f"Could not clean up NightlyRebalanceOOM actor: {e}")

        self.logger.info("[FIX] Clock drift leftover if any")
        try:
            injector = RemoteOSFaultInjector()
            injector.recover_clock_drift()
        except Exception as e:
            self.logger.warning(f"Could not fix leftover clock drift state: {e}")

        self.logger.info("Fix Kubernetes completed.")

    def deploy_app(self):
        """Kubectl + Prometheus + problem.app deployment."""
        problem = self.current_problem
        self.submission_stage = "setup"

        # Load or capture baseline state BEFORE any infrastructure deployment.
        # This captures the bare cluster state so reconciliation can clean up
        # everything added during a problem run (including infrastructure drift).
        if not self._baseline_captured:
            # The problem's own namespaces are created after the snapshot, so the
            # staleness check has to accept them. Sorted, so the check is comparable
            # between runs, and deduplicated, since a composite app repeats its first
            # namespace in `namespaces`.
            allow_missing = tuple(sorted(n for n in {problem.namespace, *getattr(problem.app, "namespaces", ())} if n))
            if self.cluster_state.load_baseline_state(
                CLUSTER_BASELINE_STATE_FILE,
                allow_missing=allow_missing,
                check_staleness=is_openshift(),
            ):
                self.logger.info("[DEPLOY] Loaded persisted cluster baseline state")
            elif self.cluster_state.baseline_load_status == "stale":
                # Capture-and-save here would overwrite the only record of the cluster
                # this run expected with a capture of the cluster it actually got.
                self.logger.error(
                    "[DEPLOY] The persisted cluster baseline names namespaces that no longer exist, so it is "
                    "neither usable nor replaced; reconcile stays disabled for this run"
                )
                self._baseline_usable = False
            else:
                self.logger.info("[DEPLOY] No persisted baseline state found, capturing and saving...")
                self.cluster_state.save_baseline_state(CLUSTER_BASELINE_STATE_FILE)
            self._baseline_captured = True
            if is_openshift() and self._baseline_usable:
                self.logger.info(
                    "[DEPLOY] The cluster baseline is kept for reporting only; it is not an ownership map: "
                    "reconcile is off by default on this platform"
                )

        self.logger.info("[DEPLOY] Checking Kubernetes pod networking…")
        if is_openshift():
            # Writing into kube-system is not ours on a managed platform, so the
            # probe runs in the namespace this run owns.
            self.kubectl.create_namespace_if_not_exist(problem.namespace)
            self.logger.info(
                f"[DEPLOY] Running the pod networking probe in SREGym's own namespace ({problem.namespace}); "
                "writing into kube-system is not ours on this platform."
            )
            self.kubectl.assert_pod_networking(namespace=problem.namespace)
        else:
            self.kubectl.assert_pod_networking()

        self._ensure_metrics_api()

        svelte = is_svelte()
        if is_openshift() and svelte:
            self.logger.info(
                "[DEPLOY] The OpenEBS block is not platform-gated yet (plan item 4): it runs on this platform "
                "too. Once it is gated, `svelte` and `full` will differ here only by the Prometheus values "
                "overlay."
            )
        if uses_openebs():
            self.logger.info("[DEPLOY] Setting up OpenEBS…")
            # `openebs` is protected from reconciliation, so it persists across
            # problems and the operator manifest only needs fetching once.
            if self._openebs_ready(svelte):
                self.logger.info("[DEPLOY] OpenEBS already deployed; skipping operator re-apply")
            else:
                if not svelte:
                    # The udev preflight exists solely for the node-disk-manager,
                    # which svelte does not deploy.
                    self._preflight_openebs_udev_mount()
                self.kubectl.exec_command("kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml")
                self._make_openebs_hostpath_default()
                if not svelte:
                    # The operator manifest has no NDM node selector. An extra
                    # selector can survive apply and prevent every NDM pod scheduling.
                    self.kubectl.apps_v1_api.patch_namespaced_daemon_set(
                        "openebs-ndm",
                        "openebs",
                        [{"op": "add", "path": "/spec/template/spec/nodeSelector", "value": {}}],
                        _request_timeout=10,
                    )
            # Idempotent and cheap; also covers an `openebs` left over from an
            # earlier full-profile run on the same cluster.
            if svelte:
                self._trim_openebs_ndm()
            # An interrupted preload can leave completed non-Job pods behind.
            # Remove only our disposable helpers before checking OpenEBS readiness.
            self.kubectl.exec_command_checked(
                "kubectl delete pods -n openebs -l app=sregym-image-preload "
                "--ignore-not-found=true --grace-period=0 --wait=true "
                f"--timeout={WAIT_FOR_POD_READY_TIMEOUT}s",
                timeout=WAIT_FOR_POD_READY_TIMEOUT + 10,
            )
            self.kubectl.wait_for_ready("openebs")
            self._wait_for_infrastructure_ready("OpenEBS", lambda: self._openebs_ready(svelte))
            self._preload_openebs_helper_image()
            if not svelte:
                self._ensure_openebs_device_storageclass()
        else:
            # Every step of the block above exists to install or repair OpenEBS. A
            # class the cluster already provides needs none of them, and the run has
            # to check that class instead.
            self.logger.info(
                "[DEPLOY] Skipping OpenEBS: this run uses storage class %r, which the cluster already "
                "provides, so the operator, the openebs-hostpath default patch, the node-disk-manager, the "
                "helper-image preload and openebs-device are not needed",
                get_storage_class(),
            )
            self.verify_storage_class()

        # Before the ~15 observer pods exist, not after them: a cluster or project that
        # cannot hold one run fails the deploy pod by pod, one readiness timeout each.
        self._verify_capacity()

        self.logger.info("[DEPLOY] Deploying Prometheus…")
        self.prometheus.deploy()

        self.logger.info("[DEPLOY] Deploying Jaeger…")
        self.jaeger.deploy()

        self.logger.info("[DEPLOY] Deploying OTel Collector…")
        self.otel_collector.deploy()

        if self.config.deploy_loki:
            self.logger.info("[DEPLOY] Deploying Loki…")
            self.loki.deploy()
        else:
            self.logger.info("[DEPLOY] Skipping Loki deployment (external harness mode)")

        self.logger.info("[DEPLOY] Deploying MCP server…")
        self.mcp_server.deploy()

        self.logger.info("[ENV] Set up necessary components: metrics-server, OpenEBS, Prometheus, Jaeger, Loki")

        # train-ticket pods need jaeger at startup; create ExternalName before deploy.
        # Other apps get it after deploy to avoid Helm ownership conflicts.
        is_train_ticket = problem.app.__class__.__name__ == "TrainTicket"

        # Composite apps span multiple namespaces; fall back to the single
        # `namespace` attribute for regular apps.
        app_namespaces = getattr(problem.app, "namespaces", None) or [problem.app.namespace]

        if is_train_ticket:
            for ns in app_namespaces:
                self.kubectl.exec_command(
                    f"kubectl create namespace {ns} --dry-run=client -o yaml | kubectl apply -f -"
                )
                self.jaeger.create_external_name_service(ns)

        self.logger.info("[DEPLOY] Deploying and starting workload")
        self._verify_image_references(problem)
        problem.app.deploy()
        self.logger.info(f"[ENV] Deploy application: {problem.app.name}")

        if not is_train_ticket:
            for ns in app_namespaces:
                self.jaeger.create_external_name_service(ns)

        if problem.run_default_workload:
            problem.app.start_workload()
            self.logger.info("[ENV] Start workload")
        else:
            self.logger.info("[ENV] Default application workload disabled for this problem")

    def undeploy_app(self):
        """Teardown problem.app and, if no other apps running, OpenEBS/Prometheus."""
        if self.problem:
            self.problem.app.cleanup()

    def _verify_capacity(self) -> None:
        """Refuse before the pods are created when this run does not fit the cluster.

        Skipped on `kind`, whose cluster is a workstation sized for this run: no read
        and no log line there. On OpenShift the run's pod, CPU, memory and storage
        footprint is compared with what the untainted workers can allocate and with a
        `ResourceQuota` in any namespace the run uses; see
        `sregym.service.capacity_preflight`.

        Once per campaign, not once per problem: every problem of a campaign deploys
        onto the same cluster, so the scan runs on the first one and the verdict is
        kept on this instance. A new campaign needs a new conductor, which is what
        main.py and the validator already build. The footprint is the same for every
        problem of a campaign -- the deployment profile and `deploy_loki` are fixed for
        the process -- which is what makes the remembered verdict valid.

        The verdict is only remembered once the scan passed: a refusal raises, so the
        campaign ends rather than carrying a failed check into the next problem.
        """
        if not is_openshift():
            return
        # A conductor built without __init__ -- several tests and the validator do --
        # has no verdict yet and no config; a missing config deploys Loki, which is
        # `ConductorConfig`'s own default, so the estimate errs high, never low.
        if getattr(self, "_capacity_verified", False):
            return
        config = getattr(self, "config", None)
        deploy_loki = getattr(config, "deploy_loki", True)
        check_capacity(self.kubectl, self._run_namespaces(), deploy_loki=deploy_loki, log=self.logger)
        self._capacity_verified = True

    def _ensure_metrics_api(self) -> None:
        """Require a working metrics.k8s.io without replacing one the platform serves."""
        if serves_metrics_api():
            self.logger.info(
                "[DEPLOY] The platform serves metrics.k8s.io itself; skipping components.yaml, whose APIService and "
                "RBAC would replace the platform's own."
            )
            self.kubectl.wait_for_metrics_api()
            return

        self.logger.info("[DEPLOY] Setting up metrics-server…")
        # metrics-server lives in kube-system, which is protected from
        # reconciliation, so it survives between problems. Re-applying it fetches
        # the manifest from GitHub every problem and rolls the deployment for no
        # gain. The apply and the patch must be skipped together: the patch is an
        # `add` to the args list, and running it without the apply that resets
        # those args would append duplicates on every problem.
        if self._metrics_server_configured():
            self.logger.info("[DEPLOY] metrics-server already present and patched; skipping re-apply")
        else:
            self.kubectl.exec_command(
                "kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/"
                "releases/latest/download/components.yaml"
            )
            self.kubectl.exec_command(
                "kubectl -n kube-system patch deployment metrics-server "
                "--type=json -p='["
                '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"},'
                '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-preferred-address-types=InternalIP"}'
                "]'"
            )
            # Apply retains selector keys added outside the original manifest.
            # Replace the complete selector so the Service reaches metrics-server.
            self.kubectl.core_v1_api.patch_namespaced_service(
                "metrics-server",
                "kube-system",
                [{"op": "replace", "path": "/spec/selector", "value": {"k8s-app": "metrics-server"}}],
                _request_timeout=10,
            )
        self.kubectl.wait_for_ready("kube-system")
        self._wait_for_infrastructure_ready("metrics-server", self._metrics_server_configured)

    def _wait_for_infrastructure_ready(self, name: str, is_ready: Callable[[], bool], timeout: float = 180) -> None:
        """Require functional infrastructure, not just Ready surviving pods."""
        deadline = time.monotonic() + timeout
        while not is_ready():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"{name} did not become healthy within {timeout:g}s; stopping application setup")
            time.sleep(min(2, remaining))

    def _preflight_openebs_udev_mount(self) -> None:
        if shutil.which("docker") is None:
            self.logger.info("[DEPLOY] Docker is unavailable; skipping kind /run/udev preflight")
            return

        nodes_output = self.kubectl.exec_command(
            "kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}'"
        )
        missing_nodes: list[str] = []

        for node in (line.strip() for line in nodes_output.splitlines()):
            if not node or " " in node:
                continue
            quoted_node = shlex.quote(node)
            result = self.kubectl.exec_command(
                f"docker inspect {quoted_node} >/dev/null 2>&1 "
                f"&& docker exec {quoted_node} sh -c 'test -d /run/udev && echo ok || echo missing' "
                "|| echo not-a-docker-node"
            ).strip()
            if result == "missing":
                missing_nodes.append(node)

        if missing_nodes:
            missing_list = ", ".join(missing_nodes)
            raise RuntimeError(
                "OpenEBS node-disk-manager requires /run/udev inside kind node containers. "
                f"Missing /run/udev on: {missing_list}. Create /run/udev on the host before kind cluster "
                "creation and mount hostPath /run/udev to containerPath /run/udev in the kind config."
            )

    _METRICS_SERVER_ARGS = ("--kubelet-insecure-tls", "--kubelet-preferred-address-types=InternalIP")

    # components.yaml creates this alongside the Deployment. It grants
    # metrics-server the ability to create subjectaccessreviews; without it the
    # aggregated API returns 500 and every `kubectl top` fails.
    _METRICS_SERVER_BINDING = "metrics-server:system:auth-delegator"

    def _metrics_server_configured(self) -> bool:
        """True if metrics-server has its configuration, RBAC, and usable metrics.

        Checking more than existence matters because the Deployment and its
        cluster-scoped RBAC have different lifetimes: the Deployment sits in the
        protected kube-system namespace, while the ClusterRoleBinding is
        cluster-scoped. Accepting a Deployment whose binding has been reconciled
        away would leave metrics-server permanently broken instead of repairing
        it, so verify both and re-apply if either is missing.
        """
        args = self.kubectl.exec_command(
            "kubectl -n kube-system get deployment metrics-server "
            "-o jsonpath='{.spec.template.spec.containers[0].args}' --ignore-not-found --request-timeout=10s"
        )
        if not args or not args.strip():
            return False
        if not all(arg in args for arg in self._METRICS_SERVER_ARGS):
            return False

        binding = self.kubectl.exec_command(
            f"kubectl get clusterrolebinding {self._METRICS_SERVER_BINDING} -o name "
            "--ignore-not-found --request-timeout=10s"
        )
        if not binding or not binding.strip():
            self.logger.info("[DEPLOY] metrics-server RBAC missing; re-applying components.yaml")
            return False
        # A Ready Deployment does not prove that its Service and aggregated API
        # still work. Exercise the same API path used by kubectl top.
        return self.kubectl.metrics_api_answers()

    def _openebs_ready(self, svelte: bool) -> bool:
        """True if the OpenEBS tier already matches what this profile wants.

        Under `full` that includes the node-disk-manager, so that a
        partially-present `openebs` namespace is repaired by re-applying the
        operator manifest rather than silently accepted. Checking it also matters
        because `openebs` persists between problems: a cluster whose last run was
        `svelte` has had NDM removed, and accepting the provisioner alone would
        leave `openebs-device` permanently unbacked after switching back.
        """
        out = self.kubectl.exec_command(
            "kubectl -n openebs get deployment openebs-localpv-provisioner "
            "-o json --ignore-not-found --request-timeout=10s"
        )
        try:
            if not deployment_rollout_complete(json.loads(out)):
                return False
        except (ValueError, TypeError):
            return False

        if svelte:
            return True

        try:
            ndm = self.kubectl.apps_v1_api.read_namespaced_daemon_set("openebs-ndm", "openebs", _request_timeout=10)
        except ApiException:
            return False
        status = ndm.status
        if status is None:
            return False
        desired = status.desired_number_scheduled or 0
        return (
            desired > 0
            and (status.observed_generation or 0) >= (ndm.metadata.generation or 0)
            and status.current_number_scheduled == desired
            and status.updated_number_scheduled == desired
            and status.number_ready == desired
            and status.number_available == desired
            and (status.number_misscheduled or 0) == 0
        )

    def _preload_openebs_helper_image(self) -> None:
        """Make LocalPV's helper image available before workload PVC creation."""
        deployment = self.kubectl.apps_v1_api.read_namespaced_deployment(
            "openebs-localpv-provisioner", "openebs", _request_timeout=10
        )
        containers = deployment.spec.template.spec.containers or []
        helper_image = next(
            (
                env.value
                for container in containers
                for env in (container.env or [])
                if env.name == "OPENEBS_IO_HELPER_IMAGE" and env.value
            ),
            None,
        )
        if not helper_image:
            raise RuntimeError("OpenEBS provisioner does not declare OPENEBS_IO_HELPER_IMAGE")
        self.logger.info("[DEPLOY] Preloading OpenEBS helper image %s on every node…", helper_image)
        self.kubectl.preload_image_on_nodes(
            helper_image,
            namespace="openebs",
            timeout=WAIT_FOR_POD_READY_TIMEOUT,
        )

    # openebs-operator.yaml bundles the node-disk-manager alongside the LocalPV
    # provisioner. NDM exists to discover block devices and back the
    # `openebs-device` StorageClass; SREGym only ever provisions through
    # `openebs-hostpath`, which needs the provisioner alone. Measured on one
    # problem, these seven pods cost 603m CPU (13% of the cluster) scanning
    # disks nothing claims.
    _OPENEBS_NDM_WORKLOADS = (
        ("daemonset", "openebs-ndm"),
        ("daemonset", "openebs-ndm-node-exporter"),
        ("deployment", "openebs-ndm-operator"),
        ("deployment", "openebs-ndm-cluster-exporter"),
    )

    def _trim_openebs_ndm(self) -> None:
        """Remove unused device storage and node-disk-manager workloads.

        Deleting rather than scaling: the operator manifest is a plain apply with
        no controller to recreate them, and re-applying it recreates them for this
        method to remove again.
        """
        baseline = self.cluster_state.baseline
        if baseline is None:
            raise RuntimeError("Cannot trim OpenEBS before the cluster baseline is available")
        # Keep user-provided StorageClasses. The shared cluster baseline is
        # captured before SREGym installs OpenEBS and survives interrupted runs.
        if "openebs-device" not in baseline.storage_classes:
            try:
                self.cluster_state.storage_v1.delete_storage_class("openebs-device")
            except ApiException as exc:
                if exc.status != 404:
                    raise

        self.logger.info("[svelte] Removing OpenEBS node-disk-manager (openebs-hostpath does not use it)")
        for kind, name in self._OPENEBS_NDM_WORKLOADS:
            self.kubectl.exec_command(f"kubectl delete {kind} {name} -n openebs --ignore-not-found")

    def _ensure_openebs_device_storageclass(self) -> None:
        self.logger.info("[DEPLOY] Ensuring OpenEBS LocalPV-Device StorageClass…")
        existing = self.kubectl.exec_command(
            "kubectl get storageclass openebs-device -o jsonpath='{.provisioner}{\"\\n\"}{.volumeBindingMode}'"
        )
        if "not found" not in existing.lower() and "error from server" not in existing.lower():
            provisioner, _, volume_binding_mode = existing.partition("\n")
            if provisioner.strip() != "openebs.io/local":
                raise RuntimeError(
                    "StorageClass/openebs-device already exists with unexpected provisioner "
                    f"{provisioner.strip()!r}; expected 'openebs.io/local'."
                )
            if volume_binding_mode.strip() != "WaitForFirstConsumer":
                raise RuntimeError(
                    "StorageClass/openebs-device already exists with unexpected volumeBindingMode "
                    f"{volume_binding_mode.strip()!r}; expected 'WaitForFirstConsumer'."
                )
            self.logger.info("[DEPLOY] StorageClass/openebs-device already exists; leaving it unchanged")
            return

        device_sc_yaml = """apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: openebs-device
  annotations:
    openebs.io/cas-type: local
    cas.openebs.io/config: |
      - name: StorageType
        value: "device"
provisioner: openebs.io/local
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Delete
"""
        result = self.kubectl.exec_command("kubectl apply -f -", input_data=device_sc_yaml)
        if "error from server" in result.lower() or "invalid" in result.lower():
            raise RuntimeError(f"Failed to create StorageClass/openebs-device: {result.strip()}")

    # A StorageClass names itself as cluster default through this annotation. Two
    # classes carrying it make the default ambiguous, so the whole set is read.
    _DEFAULT_CLASS_ANNOTATION = "storageclass.kubernetes.io/is-default-class"

    def _default_storage_classes(self) -> set[str]:
        """Return the names of the storage classes the cluster marks as default."""
        listed = self.kubectl.exec_command("kubectl get storageclass -o json --request-timeout=10s")
        try:
            items = json.loads(listed).get("items", [])
        except (ValueError, AttributeError) as exc:
            raise RuntimeError(f"Could not list the cluster's storage classes: {listed.strip()}") from exc
        return {
            item["metadata"]["name"]
            for item in items
            if (item.get("metadata", {}).get("annotations") or {}).get(self._DEFAULT_CLASS_ANNOTATION) == "true"
        }

    def _make_openebs_hostpath_default(self) -> None:
        """Make `openebs-hostpath` the cluster default on kind, as `main` does.

        On OpenShift the cluster's own default class is left alone: the run refuses to start
        unless the class it selected is the default, and this method never changes that.
        """
        if is_openshift():
            self.logger.info("[DEPLOY] Not touching the cluster's default storage class on this platform")
            return
        self.kubectl.exec_command(
            "kubectl patch storageclass openebs-hostpath "
            '-p \'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
        )

    def verify_storage_class(self) -> None:
        """Require the selected storage class to exist, provision dynamically and be the default.

        Fail closed, because the harness does not own every claim it causes: third-party
        charts and the submodule's PVCs name no class, so they bind through the cluster
        default. A class the harness cannot bind through therefore has to stop the run
        before it deploys anything.
        """
        name = get_storage_class()
        read = self.kubectl.exec_command(
            f"kubectl get storageclass {shlex.quote(name)} -o json --ignore-not-found --request-timeout=10s"
        )
        try:
            storage_class = json.loads(read) if read.strip() else None
        except ValueError as exc:
            raise RuntimeError(f"Could not read storage class {name!r}: {read.strip()}") from exc
        if not isinstance(storage_class, dict) or not storage_class:
            raise RuntimeError(
                f"Storage class {name!r} does not exist on this cluster; the class named by --storage-class "
                "(or $SREGYM_STORAGE_CLASS) must exist on this cluster."
            )

        provisioner = storage_class.get("provisioner") or ""
        if not provisioner or provisioner == "kubernetes.io/no-provisioner":
            raise RuntimeError(
                f"Storage class {name!r} has provisioner {provisioner or 'none'}, but a dynamically provisioned "
                "class is required: a static class needs a hand-made PersistentVolume for every claim, which "
                "this harness does not create."
            )

        default_classes = self._default_storage_classes()
        if default_classes != {name}:
            current = ", ".join(sorted(default_classes))
            if not default_classes:
                state = "no storage class is the cluster default"
                advice = f"make {name!r} the default before the run"
            elif name in default_classes:
                state = (
                    f"{len(default_classes)} storage classes are the cluster default ({current}), and {name!r} is "
                    "one of them; exactly one must be, because a claim that names no class would otherwise bind "
                    "through whichever default the cluster picks"
                )
                advice = f"remove the default annotation from the other {len(default_classes) - 1} before the run"
            else:
                state = f"the cluster default is {current}, not {name!r}"
                advice = (
                    f"pass --storage-class {sorted(default_classes)[0]} on this cluster, or make {name!r} the default"
                )
            raise RuntimeError(
                f"Storage class {name!r} cannot be used: {state}. Third-party charts and the submodule's PVCs carry "
                f"no storage class, so they bind through the default: {advice}."
            )

        if (storage_class.get("reclaimPolicy") or "Delete") == "Retain":
            self.logger.warning(
                "[DEPLOY] Storage class %r reclaims with Retain, so the volumes it backs outlive their claims; "
                "that is legitimate and not refused, and the harness's own cleanup covers the leftovers it creates",
                name,
            )

    def get_deployed_apps(self):
        deployed_apps = []
        for app_name in self.apps.get_app_names():
            namespace = self.apps.get_app_metadata(app_name)["Namespace"]
            if self.kubectl.get_namespace_deployment_status(namespace):
                deployed_apps.append(app_name)

        return deployed_apps
