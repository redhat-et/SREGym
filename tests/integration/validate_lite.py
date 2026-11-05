"""Validate all Lite fault lifecycles serially on a disposable cluster.

No LLM is called. Each problem uses the normal Conductor deployment, workloads,
fault injector, recovery, and mitigation oracle. Stop on failure so the cluster
can be inspected before any subsequent fault runs.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.conductor.problem_sets import SREGYM_LITE_PROBLEMS
from sregym.profile import PROFILES

KIND_CONTEXT_PREFIX = "kind-"
KUBE_SYSTEM_UID_COMMAND = ["kubectl", "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"]


def platform_for_context(context: str) -> str:
    """Return the platform a context belongs to.

    A ``kind-`` context is the local cluster the Lite suite is developed on. Any other
    context is a managed cluster, which SREGym supports as ``openshift``, and which the
    operator has confirmed to be disposable.
    """
    return KIND if context.startswith(KIND_CONTEXT_PREFIX) else OPENSHIFT


def context_refusal(named: str | None, current: str) -> str | None:
    """Return why a named context cannot be used, or None when it can.

    Every kubectl call in this run — here and inside the child validator — uses the
    current context, so a name that is not the current one would validate one cluster
    and mutate another.
    """
    if named is None or named == current:
        return None
    return (
        f"Refusing --context {named!r}: kubectl uses the current context {current!r}, so the run would "
        "validate one cluster and mutate another; select that context first"
    )


def disposable_context_refusal(context: str, *, confirmed: bool) -> str | None:
    """Return why a context may not carry a destructive run, or None when it may.

    A local ``kind`` cluster is disposable by construction. Any other context may name a
    cluster somebody else owns, so the operator states that the run may delete from it.
    """
    if context.startswith(KIND_CONTEXT_PREFIX) or confirmed:
        return None
    return (
        f"Refusing context {context!r}: this run deletes namespaces, so a managed cluster needs "
        "--confirm-disposable-cluster"
    )


def managed_cluster_identity() -> str:
    """Return the ``kube-system`` namespace UID, the identity the cluster baseline uses."""
    uid = subprocess.check_output(KUBE_SYSTEM_UID_COMMAND, text=True).strip()
    if not uid:
        raise RuntimeError("kube-system has no namespace UID, so this cluster cannot be identified")
    return uid


def resume_refusal(report: dict, identity: dict, profile: str) -> str | None:
    """Return why stored results cannot be resumed on this cluster, or None when they can.

    The record names the context, the platform and the cluster identity, so a resume
    across a recreated or different cluster is refused by name instead of skipping the
    problems that cluster's results record as passed.
    """
    if report.get("profile") != profile or not report.get("with_loki"):
        return "Existing results belong to a different profile; use --resume with that profile or a new directory"
    stored = report.get("cluster")
    if not isinstance(stored, dict):
        return "Existing results carry no cluster record; use a new directory"
    if stored.get("context") != identity["context"]:
        return (
            f"Existing results belong to context {stored.get('context')!r}, this run is against "
            f"{identity['context']!r}; use a new directory"
        )
    if stored.get("platform") != identity["platform"]:
        return (
            f"Existing results belong to platform {stored.get('platform')!r}, this run is "
            f"{identity['platform']!r}; use a new directory"
        )
    if stored.get("identity") != identity["identity"]:
        what = "node set" if identity["platform"] == KIND else "kube-system namespace UID"
        return (
            f"The {what} of context {identity['context']!r} is not the one these results came from: the cluster "
            "was recreated, or a different cluster answers to that context; use a new directory"
        )
    return None


def _kind_identity(context: str) -> list[dict]:
    """Return the local kind cluster's nodes, after proving the context is that cluster."""
    nodes = json.loads(subprocess.check_output(["kubectl", "get", "nodes", "-o", "json"], text=True))
    cluster_name = context.removeprefix(KIND_CONTEXT_PREFIX)
    local_nodes = set(subprocess.check_output(["kind", "get", "nodes", "--name", cluster_name], text=True).split())
    if not local_nodes or local_nodes != {node["metadata"]["name"] for node in nodes["items"]}:
        raise RuntimeError("The selected Kubernetes context does not match the local KIND cluster")
    for node in nodes["items"]:
        expected_provider = f"kind://docker/{cluster_name}/{node['metadata']['name']}"
        if node.get("spec", {}).get("providerID") != expected_provider:
            raise RuntimeError("The selected nodes are not from the expected Docker KIND cluster")
    return sorted(
        (
            {
                "name": node["metadata"]["name"],
                "uid": node["metadata"]["uid"],
                "architecture": node["status"]["nodeInfo"]["architecture"],
            }
            for node in nodes["items"]
        ),
        key=lambda node: node["name"],
    )


def cluster_identity(context: str | None = None, *, confirm_disposable_cluster: bool = False) -> dict:
    """Return the record that identifies the cluster this run is allowed to mutate.

    ``context`` defaults to the current kubectl context. A local ``kind-`` context is
    proven disposable by matching the cluster's nodes against the local kind cluster. Any
    other context needs ``--confirm-disposable-cluster``, because this run deletes
    namespaces, and is identified by the ``kube-system`` namespace UID.
    """
    current = subprocess.check_output(["kubectl", "config", "current-context"], text=True).strip()
    context = context or current
    for refusal in (
        context_refusal(context, current),
        disposable_context_refusal(context, confirmed=confirm_disposable_cluster),
    ):
        if refusal:
            raise RuntimeError(refusal)
    platform = platform_for_context(context)
    identity = _kind_identity(context) if platform == KIND else managed_cluster_identity()
    return {"context": context, "platform": platform, "identity": identity}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="full")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--context",
        default=None,
        help=(
            "kubectl context to validate on (default: the current context). It has to be the current "
            "context, because every kubectl call in this run uses that one."
        ),
    )
    parser.add_argument(
        "--confirm-disposable-cluster",
        action="store_true",
        help=(
            "Confirm that a context which is not a local kind cluster may be deleted from. This run "
            "deletes namespaces, so a managed cluster needs this explicit confirmation."
        ),
    )
    parser.add_argument("--resume", action="store_true", help="Skip passed problems on this same cluster/profile")
    parser.add_argument("--problems", nargs="+", choices=SREGYM_LITE_PROBLEMS, default=SREGYM_LITE_PROBLEMS)
    parser.add_argument("--problem-timeout", type=int, default=1800)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "suite.json"
    try:
        identity = cluster_identity(args.context, confirm_disposable_cluster=args.confirm_disposable_cluster)
    except RuntimeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr, flush=True)
        return 2
    report = {"cluster": identity, "profile": args.profile, "with_loki": True, "attempts": []}
    if report_path.exists():
        if not args.resume:
            parser.error("Output directory already contains results; use --resume or a new directory")
        report = json.loads(report_path.read_text())
        refusal = resume_refusal(report, identity, args.profile)
        if refusal:
            parser.error(refusal)
    passed = {attempt["problem"] for attempt in report["attempts"] if attempt["passed"]}
    validator = Path(__file__).with_name("validate_problem.py")
    for problem in args.problems:
        if problem in passed:
            print(f"SKIP (already passed): {problem}", flush=True)
            continue
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        prefix = args.output_dir / f"{problem}-{stamp}"
        json_path = prefix.with_suffix(".json")
        log_path = prefix.with_suffix(".log")
        command = [
            sys.executable,
            "-u",
            str(validator),
            "--problem",
            problem,
            "--profile",
            args.profile,
            "--platform",
            identity["platform"],
            "--with-loki",
            "--summary",
            str(prefix.with_suffix(".md")),
            "--json-summary",
            str(json_path),
        ]
        print(f"START: {problem} (log: {log_path})", flush=True)
        started = time.monotonic()
        timed_out = False
        with log_path.open("w") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.wait(timeout=args.problem_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            except KeyboardInterrupt:
                os.killpg(process.pid, signal.SIGTERM)
                raise
        result = json.loads(json_path.read_text()) if json_path.exists() else {}
        success = process.returncode == 0 and result.get("passed") is True and not timed_out
        report["attempts"].append(
            {
                "problem": problem,
                "passed": success,
                "timed_out": timed_out,
                "exit_code": process.returncode,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "log": str(log_path),
                "result": result,
            }
        )
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"{'PASS' if success else 'FAIL'}: {problem} ({time.monotonic() - started:.0f}s)", flush=True)
        if not success:
            print("Stopped. Inspect the log and recover cluster state before resuming.", flush=True)
            return 1
    print(f"All {len(args.problems)} selected Lite lifecycles passed. Results: {report_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
