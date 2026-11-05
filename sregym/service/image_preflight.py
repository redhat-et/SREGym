"""Image-reference preflight: name the references the cluster cannot resolve.

A managed cluster rewrites an image name that carries no registry host through a short-name
alias table before it pulls. `mongo:4.4.6` then resolves to a mirror that answers 403 and
`hashicorp/consul:1.22.3` to a registry that does not know the repository. On the first real
OpenShift run that cost eight `ImagePullBackOff` pods and the full 600 s readiness budget
(measured on one managed cluster). A reference that names its registry is not a short name, so
the alias table does not apply to it.

A reference names a registry when its first path component contains a dot or a colon, or is
`localhost`: `registry.k8s.io/pause:3.10`, `ghcr.io/sregym/hotel-reservation@sha256:...` and
`localhost:5000/foo` do. `docker.io/busybox:1.36`, `docker.io/jaegertracing/all-in-one:1.57` and
`docker.io/otel/opentelemetry-collector-contrib:0.104.0` do not. A slash on its own is not a registry:
`otel/...` and `hashicorp/consul` are Docker Hub namespaces, and Docker Hub is exactly what an
alias table is free to replace.

This module reads the manifests it is given and says what to change. Which manifest sets a run
scans, and where the scan sits in the deploy, is the caller's decision.
"""

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger("all.sregym.image_preflight")

# References that provably pull without a registry host. An entry is a measurement, not a
# preference: it records a pull that succeeded through the cluster's own short-name
# resolution, so the preflight must not refuse a run over it.
ALLOWLIST: frozenset[str] = frozenset(
    {
        # Measured on a managed cluster: these resolved through its own short-name table and
        # the pods pulled and ran. Bare `memcached` is what the manifests actually say, which
        # is why the entry is the bare name and not a tag the manifests do not carry.
        "memcached",
    }
)

_MANIFEST_SUFFIXES = frozenset({".yaml", ".yml", ".json"})
_REASON = "no registry host, so the cluster's short-name policy chooses the registry"


@dataclass(frozen=True)
class Finding:
    """One image reference that does not name its registry."""

    reference: str
    manifest: Path
    reason: str


def classify(reference: str) -> tuple[bool, str]:
    """Return whether `reference` names a registry host explicitly, and the host when it does.

    The host is the first path component when it contains a dot or a colon or is `localhost`,
    which is the rule the container runtimes apply. The tag and the digest are not part of the
    name, so `docker.io/busybox:1.36` has one component and no host, while `localhost:5000/foo` has the
    host `localhost:5000`.
    """
    name = reference.strip().partition("@")[0]
    first, separator, _ = name.partition("/")
    if separator and ("." in first or ":" in first or first == "localhost"):
        return True, first
    return False, ""


def repository(reference: str) -> str:
    """Return `reference` without its registry host, tag or digest.

    This is the key the allowlist matches on. A measurement records a repository that pulled,
    and whether the manifest pinned a tag is a separate question, so `docker.io/library/memcached`
    and bare `memcached` compare equal.
    """
    name = reference.strip().partition("@")[0]
    qualified, host = classify(name)
    path = name[len(host) + 1 :] if qualified else name
    return path.rpartition(":")[0] if ":" in path.rpartition("/")[2] else path


def _allowed(reference: str, allowlist: frozenset[str]) -> bool:
    """Return whether `reference` or its repository is on `allowlist`."""
    return reference in allowlist or repository(reference) in {repository(entry) for entry in allowlist}


def find_unqualified(manifest_paths: Iterable[Path], allowlist: frozenset[str] = ALLOWLIST) -> list[Finding]:
    """Return one `Finding` per image reference that names no registry host.

    A directory is walked recursively for `*.yaml`, `*.yml` and `*.json`; a file is read as it
    is. Every document that parses is scanned, and a reference on `allowlist` is left out
    because it has been measured pulling.
    """
    findings: list[Finding] = []
    for path in _manifest_files(manifest_paths):
        for reference in _image_references(_documents(path)):
            qualified, _ = classify(reference)
            if qualified or _allowed(reference, allowlist):
                continue
            findings.append(Finding(reference, path, _REASON))
    return findings


def report(findings: Iterable[Finding]) -> str:
    """Return one line per finding, then a summary line that names the fix.

    This text is what an operator reads, so the last line says what to change: add a registry
    host to each reference, or pin the submodule to a commit whose manifests are already
    qualified.
    """
    findings = list(findings)
    if not findings:
        return "Every scanned image reference names a registry host."
    lines = [f"{finding.manifest}: {finding.reference} - {finding.reason}" for finding in findings]
    manifests = len({finding.manifest for finding in findings})
    lines.append(
        f"{len(findings)} image reference(s) in {manifests} manifest file(s) name no registry host. "
        "Add one to each reference (`docker.io/mongo:4.4.6` for Docker Hub, `quay.io/...` for Quay), or pin "
        "the SREGym-applications submodule to a commit whose manifests are already qualified."
    )
    return "\n".join(lines)


def _manifest_files(manifest_paths: Iterable[Path]) -> Iterator[Path]:
    """Yield each manifest file named by `manifest_paths`, walking a directory and keeping one copy of a repeat."""
    seen: dict[Path, None] = {}
    for path in map(Path, manifest_paths):
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.suffix in _MANIFEST_SUFFIXES and candidate.is_file():
                seen.setdefault(candidate, None)
    return iter(seen)


def _documents(path: Path) -> list:
    """Return the documents in `path` that parse, or none when the file is not a manifest.

    A chart template is YAML only after rendering, and one unreadable file must not stop the
    scan, so it is named on a single debug line and skipped.
    """
    try:
        text = path.read_text()
        if path.suffix == ".json":
            return [json.loads(text)]
        return list(yaml.safe_load_all(text))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.debug("Skipping %s: it does not parse as a manifest (%s)", path, exc)
        return []


def _image_references(node) -> Iterator[str]:
    """Yield the value of every `image` key in `node`, at any depth, that holds a string."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "image" and isinstance(value, str):
                yield value
            else:
                yield from _image_references(value)
    elif isinstance(node, list):
        for item in node:
            yield from _image_references(item)


def unused_allowlist_entries(manifest_paths: Iterable[Path], allowlist: frozenset[str] = ALLOWLIST) -> list[str]:
    """Return the allowlist entries that match no reference in `manifest_paths`.

    An entry that matches nothing is a measurement nobody can still stand behind, and it hides
    the next manifest that needs the same treatment. The test suite fails on a non-empty result.
    """
    references = [
        reference for path in _manifest_files(manifest_paths) for reference in _image_references(_documents(path))
    ]
    used = {entry for entry in allowlist if any(_allowed(reference, frozenset({entry})) for reference in references)}
    return sorted(set(allowlist) - used)
