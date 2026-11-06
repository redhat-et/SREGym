"""Add a registry host to the unqualified image references in the manifests SREGym applies.

On OpenShift the image preflight refuses a run whose manifests name `mongo:4.4.6` or
`hashicorp/consul:1.22.3`: the cluster's short-name table would pick the registry, and for these
two it picks one that cannot serve them. The manifests live in the SREGym-applications submodule,
which this repository does not edit, so this script is the explicit, reversible way to qualify them.

    uv run python scripts/qualify_submodule_images.py            # dry run: list what would change
    uv run python scripts/qualify_submodule_images.py --apply    # rewrite the files in the submodule tree
    uv run python scripts/qualify_submodule_images.py --revert   # git checkout exactly those files

It scans the same manifest sets the preflight does (every application's `K8S Deploy Path`) and
reuses the preflight's own rule for what counts as unqualified, so the two cannot disagree. It
only touches `image:` lines. A bare name becomes `docker.io/library/<name>`; `ns/name` becomes
`docker.io/ns/name`. Commit the submodule change or revert it; the script never commits.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sregym.service.image_preflight import find_unqualified  # noqa: E402

APPS = ROOT / "SREGym-applications"
METADATA = ROOT / "sregym" / "service" / "metadata"
IMAGE_LINE = re.compile(r"""^(?P<head>\s*-?\s*"?image"?\s*:\s*["']?)(?P<ref>[^\s"']+)(?P<tail>.*)$""")


def qualify(reference: str) -> str:
    """Return `reference` with the Docker Hub host added (bare names go under `library/`)."""
    return f"docker.io/{reference}" if "/" in reference else f"docker.io/library/{reference}"


def rewrite_text(text: str, references: set[str]) -> tuple[str, int]:
    """Rewrite `image:` lines whose value is in `references`; return the new text and the count."""
    count = 0
    out = []
    for line in text.splitlines(keepends=True):
        match = IMAGE_LINE.match(line.rstrip("\n"))
        if match and match["ref"] in references:
            newline = "\n" if line.endswith("\n") else ""
            line = f"{match['head']}{qualify(match['ref'])}{match['tail']}{newline}"
            count += 1
        out.append(line)
    return "".join(out), count


def deploy_dirs() -> list[Path]:
    """Every application manifest directory the preflight scans (`K8S Deploy Path`)."""
    dirs = []
    for meta in sorted(METADATA.glob("*.json")):
        path = json.loads(meta.read_text()).get("K8S Deploy Path")
        if path and (APPS / path).is_dir():
            dirs.append(APPS / path)
    return dirs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="rewrite the files")
    mode.add_argument("--revert", action="store_true", help="git checkout the modified manifests")
    args = parser.parse_args()
    dirs = deploy_dirs()

    if args.revert:
        changed = subprocess.run(
            ["git", "-C", str(APPS), "diff", "--name-only", "--", *[str(d.relative_to(APPS)) for d in dirs]],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        if changed:
            subprocess.run(["git", "-C", str(APPS), "checkout", "--", *changed], check=True)
        print(f"reverted {len(changed)} file(s)")
        return 0

    by_file: dict[Path, set[str]] = {}
    for finding in find_unqualified(dirs):
        by_file.setdefault(finding.manifest, set()).add(finding.reference)
    if not by_file:
        print("nothing to qualify: every scanned image reference names a registry host")
        return 0
    total = 0
    for path, refs in sorted(by_file.items()):
        new, count = rewrite_text(path.read_text(), refs)
        total += count
        verb = "rewrote" if args.apply else "would rewrite"
        for ref in sorted(refs):
            print(f"{verb} {path.relative_to(APPS)}: {ref} -> {qualify(ref)}")
        if args.apply:
            path.write_text(new)
    print(f"{total} line(s) in {len(by_file)} file(s)" + ("" if args.apply else " (dry run; use --apply)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
