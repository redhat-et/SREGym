"""Static OpenShift compatibility lint for the Lite applications. Needs `helm`, never a cluster.

Renders each application the way the harness installs it (with the OpenShift overlay when one
exists) and scans the manifests for constructs that do not run unchanged on OpenShift.

    uv run python scripts/openshift_chart_lint.py [--no-overlay]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT / "SREGym-applications"
VALUES = ROOT / "sregym" / "service" / "apps" / "values"

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("kube-dns name", re.compile(r"kube-dns(?:\.kube-system\.svc)?|kube-system\.svc")),
    ("hardcoded DNS IP", re.compile(r"\b(?:172\.30\.0\.10|10\.96\.0\.10|127\.0\.0\.11)\b")),
    ("privileged", re.compile(r"privileged:\s*true")),
    ("hostPath", re.compile(r"^\s*hostPath:", re.M)),
    ("hostNetwork/hostPort", re.compile(r"hostNetwork:\s*true|^\s*hostPort:", re.M)),
    ("runAsUser pin", re.compile(r"runAsUser:\s*\d+")),
    (
        "cluster-scoped kind",
        re.compile(
            r"^kind:\s*(ClusterRole|ClusterRoleBinding|CustomResourceDefinition|"
            r"MutatingWebhookConfiguration|ValidatingWebhookConfiguration|"
            r"StorageClass|PersistentVolume)\s*$",
            re.M,
        ),
    ),
)
IMAGE = re.compile(r"^\s*image:\s*[\"']?([^\s\"']+)", re.M)


def scan(text: str) -> dict[str, int]:
    """Count findings per rule in rendered manifest text, including unqualified image names."""
    found = {name: len(rx.findall(text)) for name, rx in RULES}
    unqualified = 0
    for ref in IMAGE.findall(text):
        first = ref.split("/", 1)[0]
        if "/" not in ref or not ("." in first or ":" in first or first == "localhost"):
            unqualified += 1
    found["unqualified image"] = unqualified
    return {k: v for k, v in found.items() if v}


@dataclass
class Target:
    name: str
    chart: str | None = None
    values: list[str] = field(default_factory=list)
    overlay_key: str | None = None
    manifests: str | None = None


TARGETS = (
    Target(
        "social-network",
        "socialNetwork/helm-chart/socialnetwork",
        ["social-network-images.yaml"],
        "social-network",
    ),
    Target(
        "astronomy-shop",
        "astronomy-shop/charts/opentelemetry-demo",
        ["astronomy-shop-fixes.yaml"],
        "astronomy-shop",
    ),
    Target("hotel-reservation", manifests="hotelReservation/kubernetes"),
)


def render(target: Target, with_overlay: bool) -> str:
    if target.manifests:
        files = sorted((APPS_DIR / target.manifests).rglob("*.yaml"))
        return "\n---\n".join(f.read_text() for f in files)
    cmd = ["helm", "template", target.name, str(APPS_DIR / target.chart)]
    for v in target.values:
        cmd += ["-f", str(VALUES / v)]
    overlay = VALUES / f"{target.overlay_key}-openshift.yaml"
    if with_overlay and target.overlay_key and overlay.is_file():
        cmd += ["-f", str(overlay)]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-overlay", action="store_true", help="render without the OpenShift overlay")
    args = parser.parse_args()
    rc = 0
    for target in TARGETS:
        try:
            findings = scan(render(target, not args.no_overlay))
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"| {target.name} | render failed: {str(exc)[:120]} |")
            rc = 2
            continue
        row = ", ".join(f"{k} x{v}" for k, v in findings.items()) or "clean"
        print(f"| {target.name} | {row} |")
    return rc


if __name__ == "__main__":
    sys.exit(main())
