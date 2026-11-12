"""Which cluster objects this run created, recorded by UID in a file the harness owns.

Scenarios create a few cluster-scoped objects by name: the mutating webhooks, the
`platform-ops` namespace, the finalizer scenario's ClusterRole and ClusterRoleBinding. The
harness used to mark them with a label. A label is written by the same process that creates the
object, so it proves nothing about who made it: a platform component with the same name is
indistinguishable, and the harness would delete or overwrite it.

The ledger instead records, at creation time, the kind, name and UID the API server assigned.
Before the harness replaces or deletes a same-named object it checks the live UID against the
ledger, and it deletes with a UID precondition, so an object that was replaced underneath it
is never removed. The file lives in the run's cache directory, so a fresh cache directory
starts with an empty ledger, and the harness then refuses rather than adopting a leftover.
"""

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from kubernetes.client import V1DeleteOptions, V1Preconditions
from kubernetes.client.rest import ApiException

import sregym.paths

logger = logging.getLogger("all.sregym.service.ownership_ledger")

# The kinds a scenario may record. Anything else is a programming error, not a runtime condition.
KINDS = ("namespace", "clusterrole", "clusterrolebinding", "mutatingwebhookconfiguration")

_CORRUPT_SUFFIX = ".corrupt"


@dataclass(frozen=True)
class LedgerEntry:
    kind: str
    name: str
    uid: str
    namespace: str | None
    recorded_at: str
    owner: str


class ForeignObjectError(RuntimeError):
    """A same-named object exists that this run did not create, so the harness will not touch it."""


def uid_of_object(obj) -> str | None:
    """Return the UID of a Kubernetes API object, or None when it has none."""
    uid = getattr(getattr(obj, "metadata", None), "uid", None)
    return uid if isinstance(uid, str) and uid else None


def delete_owned(ledger: "OwnershipLedger", kind: str, name: str, delete) -> str:
    """Delete `name` only if the ledger recorded it, and only the object with the recorded UID.

    `delete` is a callable taking `body=` (for example a bound `delete_namespace` with `name`
    already applied). Returns one of:

    - "not_ours": the ledger has no entry for this name, so nothing was called;
    - "deleted": the object with the recorded UID was removed;
    - "absent": the object was already gone;
    - "foreign": the object at that name has a different UID, so it is someone else's.

    The entry is forgotten in every case except one that raises.
    """
    uid = ledger.uid_of(kind, name)
    if not uid:
        return "not_ours"
    body = V1DeleteOptions(preconditions=V1Preconditions(uid=uid))
    try:
        delete(body=body)
    except ApiException as exc:
        if exc.status == 404:
            ledger.forget(kind, name)
            return "absent"
        if exc.status == 409:
            ledger.forget(kind, name)
            return "foreign"
        raise
    ledger.forget(kind, name)
    return "deleted"


class OwnershipLedger:
    """A JSON file of the objects this run created, keyed by (kind, name).

    With no `path`, the file is `sregym.paths.OWNERSHIP_LEDGER_FILE`, read at each call so a test
    or an operator can point it elsewhere after import.
    """

    def __init__(self, path: str | os.PathLike | None = None):
        self._explicit_path = Path(path) if path is not None else None

    @property
    def path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        return Path(sregym.paths.OWNERSHIP_LEDGER_FILE)

    def entries(self, kind: str | None = None) -> list[LedgerEntry]:
        """Return the recorded entries, optionally only those of one kind."""
        found, _corrupt = _read(self.path)
        return [entry for entry in found.values() if kind is None or entry.kind == kind]

    def record(self, kind: str, name: str, uid: str | None, *, namespace: str | None = None, owner: str = "") -> None:
        """Record that this run created `kind/name` with `uid`, replacing any earlier entry for it.

        An empty or non-string UID is not recorded: an entry without a UID could match any object
        of that name, which is the failure this ledger exists to prevent.
        """
        if kind not in KINDS:
            raise ValueError(f"unsupported ledger kind {kind!r}; expected one of {KINDS}")
        if not isinstance(uid, str) or not uid:
            logger.warning("Not recording %s %s in the ownership ledger: the API returned no UID", kind, name)
            return
        entry = LedgerEntry(
            kind=kind,
            name=name,
            uid=uid,
            namespace=namespace,
            recorded_at=datetime.now(UTC).isoformat(),
            owner=owner,
        )
        self._update(lambda found: found.__setitem__((kind, name), entry))

    def uid_of(self, kind: str, name: str) -> str | None:
        """Return the recorded UID for `kind/name`, or None when the run did not record it."""
        found, _corrupt = _read(self.path)
        entry = found.get((kind, name))
        return entry.uid if entry is not None else None

    def owns(self, kind: str, name: str, uid: str | None) -> bool:
        """Return whether the object `kind/name` with exactly `uid` is one this run created."""
        if not (kind and name and uid):
            return False
        recorded = self.uid_of(kind, name)
        return recorded is not None and recorded == uid

    def forget(self, kind: str, name: str) -> None:
        """Drop the entry for `kind/name`, if there is one."""
        found, _corrupt = _read(self.path)
        if (kind, name) not in found:
            return
        self._update(lambda current: current.pop((kind, name), None))

    def require_owned(self, kind: str, name: str, existing_uid: str | None, *, action: str) -> None:
        """Raise ForeignObjectError unless the existing object `kind/name` is one this run created.

        `action` completes the sentence "will not <action>" in the message, for example "replace"
        or "adopt".
        """
        if self.owns(kind, name, existing_uid):
            return
        raise ForeignObjectError(
            f"{kind} {name} already exists and this run did not create it; will not {action}. "
            "Remove it by hand, or if an earlier attempt created it, run with the same SREGYM_OWNERSHIP_LEDGER"
        )

    def _update(self, change) -> None:
        """Read the ledger, apply `change` to the mapping in place, and write it back atomically."""
        path = self.path
        found, corrupt = _read(path)
        if corrupt:
            _quarantine(path)
        change(found)
        _write(path, found)


def _read(path: Path) -> tuple[dict[tuple[str, str], LedgerEntry], bool]:
    """Return the entries in `path` and whether the file exists but cannot be read as a ledger.

    A missing file is an empty ledger. A file that is unreadable or has the wrong shape reads as
    empty, with a warning, and is flagged so the next write moves it aside instead of overwriting it.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}, False
    except OSError as exc:
        logger.warning("Could not read the ownership ledger %s: %s", path, exc)
        return {}, True
    try:
        return _parse(json.loads(text)), False
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning("The ownership ledger %s is not valid, so it is treated as empty: %s", path, exc)
        return {}, True


def _parse(data) -> dict[tuple[str, str], LedgerEntry]:
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("expected an object with an 'entries' list")
    found: dict[tuple[str, str], LedgerEntry] = {}
    for item in data["entries"]:
        if not isinstance(item, dict):
            raise ValueError("an entry is not an object")
        entry = LedgerEntry(
            kind=_str_field(item, "kind"),
            name=_str_field(item, "name"),
            uid=_str_field(item, "uid"),
            namespace=item.get("namespace") if isinstance(item.get("namespace"), str) else None,
            recorded_at=item.get("recorded_at") if isinstance(item.get("recorded_at"), str) else "",
            owner=item.get("owner") if isinstance(item.get("owner"), str) else "",
        )
        found[(entry.kind, entry.name)] = entry
    return found


def _str_field(item: dict, key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"an entry has no usable '{key}'")
    return value


def _write(path: Path, found: dict[tuple[str, str], LedgerEntry]) -> None:
    """Write the ledger to a temporary file in the same directory, then rename it over `path`.

    The rename is atomic on one filesystem, so a crash leaves either the old ledger or the new
    one, never a partial file. `mkstemp` creates the file with mode 0600.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"entries": [asdict(entry) for entry in sorted(found.values(), key=lambda e: (e.kind, e.name))]}
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _quarantine(path: Path) -> None:
    """Move an unreadable ledger aside so a fresh one can be written without losing the evidence."""
    target = path.with_name(path.name + _CORRUPT_SUFFIX)
    try:
        os.replace(path, target)
        logger.warning("Moved the unreadable ownership ledger to %s", target)
    except OSError as exc:
        logger.warning("Could not move the unreadable ownership ledger %s aside: %s", path, exc)
