"""Cluster platform selection.

SREGym is developed against a local ``kind`` cluster, but the same harness has to
run on a managed OpenShift cluster. The two platforms differ in what the harness
may install and in what the cluster accepts: OpenShift already serves the
``metrics.k8s.io`` API and enforces ``restricted-v2`` security context
constraints, so the kind code path collides with the platform instead of
complementing it.

The platform changes what the harness is allowed to do, so it is declared
explicitly (``--platform`` / ``$SREGYM_PLATFORM``) rather than detected at every
call site. Read it with :func:`get_platform`, or with one of the derived
predicates below. It is a module-level singleton rather than a field on
``ConductorConfig`` because ``Application`` instances and injectors are
constructed deep inside ``ProblemRegistry`` with no access to the conductor's
config.
"""

import logging
import os

KIND = "kind"
OPENSHIFT = "openshift"
PLATFORMS = (KIND, OPENSHIFT)

_ENV_VAR = "SREGYM_PLATFORM"

# TEMPORARY_ESCAPE_HATCH — delete this constant, its use in
# verify_platform_matches_cluster and its test before this branch is proposed
# upstream. It exists for one reason: nobody has a managed OpenShift cluster on
# their desk, so the OpenShift code paths can only be exercised end to end on a
# local kind cluster, declared as OpenShift. A run started this way is NOT an
# OpenShift run, and its result must never be reported as one. Grep for the
# marker when the sandbox work is over.
#
# Only an explicit enable lifts the refusal: the value must be one of
# _ALLOW_UNVERIFIED_VALUES after stripping blanks and ignoring case, so
# SREGYM_ALLOW_UNVERIFIED_PLATFORM=0 (or =false, or an empty value) still refuses.
_ALLOW_UNVERIFIED_ENV = "SREGYM_ALLOW_UNVERIFIED_PLATFORM"
_ALLOW_UNVERIFIED_VALUES = frozenset({"1", "true", "yes"})

logger = logging.getLogger("all.sregym.cluster_platform")

_platform: str | None = None


class PlatformVerificationError(RuntimeError):
    """The declared platform is not confirmed by the cluster, so the run must not start."""


def _unverified_platform_allowed() -> bool:
    """Return True only for an explicit enable of the temporary escape hatch."""
    value = os.environ.get(_ALLOW_UNVERIFIED_ENV, "")
    return value.strip().lower() in _ALLOW_UNVERIFIED_VALUES


def set_platform(platform: str) -> None:
    """Set the active platform. Called once from main.py before any deployment."""
    global _platform
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform {platform!r}; expected one of {PLATFORMS}")
    _platform = platform
    logger.info(f"Cluster platform: {platform}")


def get_platform() -> str:
    """Return the active platform, falling back to $SREGYM_PLATFORM then 'kind'.

    The env fallback exists so that code paths which bypass main.py (tests, the
    measurement harness, external harnesses) can still select a platform.
    """
    if _platform is not None:
        return _platform
    env = os.environ.get(_ENV_VAR)
    if env:
        if env not in PLATFORMS:
            raise ValueError(f"{_ENV_VAR}={env!r} is not one of {PLATFORMS}")
        return env
    return KIND


def is_openshift() -> bool:
    """Return True when the active platform is OpenShift.

    Ask one of the derived predicates below instead at a call site that only
    cares about one OpenShift property; this function is for code that has to
    name the platform.
    """
    return get_platform() == OPENSHIFT


def serves_metrics_api() -> bool:
    """Return True when the platform serves metrics.k8s.io/v1beta1 itself.

    True on ``openshift``: the platform serves the resource metrics API, so the
    harness must never install one. Ask the platform this, not
    ``is_openshift()`` at every call site.
    """
    return get_platform() == OPENSHIFT


def restricted_admission() -> bool:
    """Return True when workloads are admitted under restricted-v2 SCCs.

    True on ``openshift``. The SCC preflight in
    ``sregym.service.admission_preflight`` acts on it. Ask the platform this,
    not ``is_openshift()`` at every call site.
    """
    return get_platform() == OPENSHIFT


def pins_control_plane() -> bool:
    """Return True when manifests may pin node-role.kubernetes.io/control-plane.

    True on ``kind``, where the single node is the control plane. On
    ``openshift`` those pins must be dropped, which overlays do in a later
    change. Ask the platform this, not ``is_openshift()`` at every call site.
    """
    return get_platform() == KIND


NOISE_UNAVAILABLE = (
    "--noise installs Chaos Mesh, whose privileged chaos-daemon needs an SCC the harness never grants, patches "
    "the daemon's tolerations and strips the finalizers of every chaos resource in every namespace, and was never "
    "run on OpenShift. Its container-runtime detection also does not recognise CRI-O (cri-o://). Without a guard "
    "the install fails quietly and the run reports a result with no noise in it"
)


def supports_noise() -> bool:
    """Return True when the Chaos Mesh noise backend may run.

    True on ``kind`` only. On ``openshift`` it is refused, loudly, instead of degrading to a
    run that claims noise it did not inject; see ``NOISE_UNAVAILABLE`` and ``docs/openshift.md``.
    Ask the platform this, not ``is_openshift()`` at every call site.
    """
    return get_platform() == KIND


def require_noise_support() -> None:
    """Raise when the platform cannot run the Chaos Mesh backend.

    The one check every install path calls first, so a scenario that installs Chaos Mesh without
    ``--noise`` is refused for the same reason as the flag.
    """
    if not supports_noise():
        raise RuntimeError(f"Chaos Mesh is not supported on {get_platform()}: {NOISE_UNAVAILABLE}.")


def verify_platform_matches_cluster(discover, *, log=None) -> str:
    """Confirm the declared platform against what API discovery reports.

    ``discover`` is a zero-argument callable returning True when the cluster is
    OpenShift; it owns all I/O, this module owns none. Returns the platform the
    run continues with.

    Either platform must be confirmed by the cluster. Declaring ``kind`` for an
    OpenShift cluster is refused because the kind code path is destructive on a
    managed cluster; declaring ``openshift`` for a cluster that does not look like
    OpenShift is refused because a result labelled ``openshift`` must come from an
    OpenShift cluster. ``SREGYM_ALLOW_UNVERIFIED_PLATFORM`` lifts the second
    refusal for local debugging only; that is temporary, and a run started that way
    is not an OpenShift run. Only an explicit enable value (``1``, ``true`` or
    ``yes``) lifts it; ``0`` and ``false`` do not.

    A ``discover`` that raises is treated as unknown and **refuses**: continuing
    with the declared platform after a failed discovery would restore exactly the
    hazard this check exists to remove. A cluster whose API cannot be read fails
    the run anyway.
    """
    log = log if log is not None else logger
    platform = get_platform()
    try:
        cluster_is_openshift = bool(discover())
    except Exception as exc:
        raise PlatformVerificationError(
            f"Could not discover the cluster platform ({exc!r}), so the declared platform {platform!r} is "
            "unconfirmed. Re-run with an explicit --platform (or set SREGYM_PLATFORM) once the cluster API "
            "answers, or check that kubectl reaches the right cluster."
        ) from exc

    if cluster_is_openshift:
        if platform == OPENSHIFT:
            log.info("Platform openshift confirmed by API discovery.")
            return OPENSHIFT
        raise PlatformVerificationError(
            f"This cluster is OpenShift, but the platform resolved to {platform!r}. "
            "Re-run with --platform openshift (or set SREGYM_PLATFORM=openshift). "
            "The kind code path is destructive on a managed cluster."
        )

    if platform == OPENSHIFT:
        if _unverified_platform_allowed():
            # TEMPORARY_ESCAPE_HATCH — removed with the constant above.
            log.warning(
                "API discovery does not report OpenShift, but the platform resolved to 'openshift' and "
                f"{_ALLOW_UNVERIFIED_ENV} is enabled. Continuing for local debugging only: this run is NOT an "
                "OpenShift run, it exercises OpenShift code paths on a cluster that is not OpenShift, and its result "
                "must never be reported as an OpenShift result."
            )
            return OPENSHIFT
        raise PlatformVerificationError(
            f"This cluster does not report the OpenShift APIs, but the platform resolved to {platform!r}. "
            "Re-run with --platform kind, or check that kubectl reaches the cluster you meant: a result labelled "
            "openshift has to come from an OpenShift cluster. "
            f"(Temporary, for local debugging only: {_ALLOW_UNVERIFIED_ENV}=1 lifts this refusal.)"
        )

    log.debug("Platform kind confirmed by API discovery.")
    return KIND
