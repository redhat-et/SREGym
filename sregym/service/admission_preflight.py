"""Read-only admission preflight for the `anyuid` SCC grant.

The application images need `anyuid` on OpenShift: several applications pin a
non-root UID in their manifests, which `restricted-v2` refuses because it assigns a
UID from the namespace's own range instead, and `mongo` and `memcached` run as root.
The harness never grants the SCC. A grant widens the harness's own admission rights
on a cluster it does not own, it is one operator command per namespace, and a
benchmark that gives itself the privilege it is measuring for cannot answer the
question it was run to answer. This module makes the prerequisite visible instead,
by asking the API server what it would answer for the namespace's service accounts.

The check is a `SubjectAccessReview` for verb `use` on
`securitycontextconstraints/anyuid` for the group `system:serviceaccounts:<ns>`,
and two properties make it the right check:

* A group review works before the namespace exists, which matters here: `observe`
  and `sregym` are created by `helm --create-namespace`, and application namespaces
  are created and deleted per problem.
* It covers both ways `oc adm policy add-scc-to-group` records a grant: RBAC on
  `system:openshift:scc:anyuid`, or the SCC's legacy `.groups` field.

Pod Security labels are deliberately not read: OpenShift's label syncer writes them
only after the namespace exists, so before that they answer a different question.

Nothing here changes the cluster. The only request is a `SubjectAccessReview`, which
the API server evaluates and discards, and no `kubectl` or `oc` command is run.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from kubernetes import client
from kubernetes.client.rest import ApiException

logger = logging.getLogger("all.sregym.admission_preflight")

SCC_NAME = "anyuid"
SCC_API_GROUP = "security.openshift.io"
SCC_RESOURCE = "securitycontextconstraints"

# Promtail reads the node's pod logs through hostPath mounts and runs as uid 0. The upstream
# worktree's ROSA notes record it admitted by the `privileged` SCC with no binding of ours, on a
# cluster-admin login, so this grant is an unmeasured prerequisite and never a refusal.
PRIVILEGED_SCC = "privileged"
PROMTAIL_NAMESPACE = "observe"
PROMTAIL_SERVICE_ACCOUNT = "promtail"

GRANTED = "granted"
MISSING = "missing"
CANNOT_VERIFY = "cannot_verify"


def grant_command(namespace: str) -> str:
    """Return the exact command that grants the `anyuid` SCC to a namespace's service accounts."""
    return f"oc adm policy add-scc-to-group {SCC_NAME} system:serviceaccounts:{namespace}"


def promtail_grant_command() -> str:
    """Return the exact command that grants the `privileged` SCC to Promtail's service account."""
    return f"oc adm policy add-scc-to-user {PRIVILEGED_SCC} -z {PROMTAIL_SERVICE_ACCOUNT} -n {PROMTAIL_NAMESPACE}"


def grant_commands(namespaces: Iterable[str]) -> list[str]:
    """Return the grant command for each of `namespaces`, in the order given."""
    return [grant_command(namespace) for namespace in namespaces]


@dataclass(frozen=True)
class PreflightResult:
    """What the API server answered, per namespace, and what it did not answer for.

    `missing` holds the namespaces whose grant the API server confirmed absent;
    `unverified` holds the ones whose review was refused, which is not the same
    answer and must not be reported as one.
    """

    namespaces: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        """Return `missing`, `cannot_verify` or `granted`.

        A confirmed gap outranks a refusal, because it is the one answer that has to
        stop the run: refusing on a namespace nobody could read would block an
        operator who already granted the SCC.
        """
        if self.missing:
            return MISSING
        if self.unverified:
            return CANNOT_VERIFY
        return GRANTED


def serves_scc_api(kubectl) -> bool:
    """Return whether the cluster serves the SCC API at all.

    Asked through discovery, which every authenticated identity may read, rather than by
    getting an SCC object, which needs a cluster-scoped grant. A cluster that does not serve
    `security.openshift.io` cannot be missing an SCC grant: it has no SCCs.
    """
    try:
        groups = kubectl.apis_api.get_api_versions().groups or []
    except Exception:
        return True  # unknown: keep the strict path rather than quieten a real gap
    return any(group.name == SCC_API_GROUP for group in groups)


def check_scc_grant(kubectl, namespaces: Iterable[str]) -> PreflightResult:
    """Ask the API server whether `anyuid` use is granted to each namespace's service accounts.

    `kubectl` is the harness `KubeCtl`, which owns the API client; this module makes
    no other call. A review refused with 403 is reported as unverified, with the
    command that checks it, and never as missing: SCC objects and the
    `system:openshift:scc:*` bindings are cluster-scoped, so a project-scoped
    operator cannot read them and would otherwise be blocked with nothing to fix.
    Any other failure propagates, because it is a real error rather than an unknown.
    """
    ordered = list(dict.fromkeys(namespaces))
    if not serves_scc_api(kubectl):
        # A cluster without the SCC API -- a local kind cluster declared as OpenShift, or any
        # plain Kubernetes -- answers `allowed: false` because there is no such resource, not
        # because a grant is missing. Refusing there would block a run over a permission that
        # does not exist on the platform.
        result = PreflightResult(tuple(ordered), (), tuple(ordered))
        logger.debug(
            "This cluster serves no %s API, so every SCC question about %s is unverifiable rather than missing",
            SCC_API_GROUP,
            ", ".join(ordered),
        )
        return result
    missing: list[str] = []
    unverified: list[str] = []
    for namespace in ordered:
        allowed = _anyuid_use_allowed(kubectl, namespace)
        if allowed is None:
            unverified.append(namespace)
        elif not allowed:
            missing.append(namespace)
    result = PreflightResult(tuple(ordered), tuple(missing), tuple(unverified))
    _log(result)
    return result


def check_promtail_privileged(kubectl) -> bool | None:
    """Return whether Promtail's service account may use `privileged`, or None when unknown.

    Answers None on a cluster without the SCC API and when the review is refused (403), the
    same two cases `check_scc_grant` reports as unverifiable. The subject is the service
    account user, because the documented grant is `add-scc-to-user -z promtail`; a review for
    the namespace group would answer false for a user binding. Read-only: nothing is granted.
    """
    if not serves_scc_api(kubectl):
        return None
    user = f"system:serviceaccount:{PROMTAIL_NAMESPACE}:{PROMTAIL_SERVICE_ACCOUNT}"
    review = client.V1SubjectAccessReview(
        spec=client.V1SubjectAccessReviewSpec(
            user=user,
            groups=[
                "system:serviceaccounts",
                f"system:serviceaccounts:{PROMTAIL_NAMESPACE}",
                "system:authenticated",
            ],
            resource_attributes=client.V1ResourceAttributes(
                group=SCC_API_GROUP,
                resource=SCC_RESOURCE,
                name=PRIVILEGED_SCC,
                verb="use",
            ),
        )
    )
    try:
        answer = kubectl.authorization_v1_api.create_subject_access_review(review)
    except ApiException as exc:
        if exc.status != 403:
            raise
        return None
    return bool(answer.status and answer.status.allowed)


def _anyuid_use_allowed(kubectl, namespace: str) -> bool | None:
    """Return whether `namespace`'s service accounts may use `anyuid`, or None if refused.

    The subject is the group, never a service account object: the group exists as
    soon as the namespace name is known, so this answer is available before the
    namespace is created.
    """
    review = client.V1SubjectAccessReview(
        spec=client.V1SubjectAccessReviewSpec(
            groups=[f"system:serviceaccounts:{namespace}"],
            resource_attributes=client.V1ResourceAttributes(
                group=SCC_API_GROUP,
                resource=SCC_RESOURCE,
                name=SCC_NAME,
                verb="use",
            ),
        )
    )
    try:
        answer = kubectl.authorization_v1_api.create_subject_access_review(review)
    except ApiException as exc:
        if exc.status != 403:
            raise
        return None
    return bool(answer.status and answer.status.allowed)


def _log(result: PreflightResult) -> None:
    """Report a granted run in one debug line, and a refusal with the command to check it."""
    if result.state == GRANTED:
        logger.debug("The anyuid SCC is granted to the service accounts in %s", ", ".join(result.namespaces))
        return
    if result.unverified:
        logger.warning(
            "Cannot verify the anyuid SCC grant for %s: the SubjectAccessReview was refused (403). SCC objects and "
            "the system:openshift:scc:* bindings are cluster-scoped, so a project-scoped identity cannot read "
            "them. Continuing; check the grant once per namespace:\n%s",
            ", ".join(result.unverified),
            "\n".join(grant_commands(result.unverified)),
        )
