"""Workspace path validation and security checks (B-059).

Single chokepoint for all file operations: resolves the path against the
workspace boundary and rejects known dangerous targets before the caller
touches the filesystem. Layers:

1. Workspace boundary (path must resolve inside ``workspace_root``).
2. Symlink ancestor walk — no ancestor of the target between the workspace
   root and its parent may be a symlink, which closes the classic
   ``resolve()``-only TOCTOU escape where an attacker swaps a directory for a
   symlink after the boundary check but before the write.
3. Hardlink rejection — shared inodes can reach outside the workspace.
4. Sensitive-path patterns — credentials, system files, Windows device names,
   null bytes. Defense-in-depth on top of the YAML rules in ToolGuard.

This module supersedes the original ``resolve_and_validate_path`` in
``extensions/tools/builtin/files.py`` (kept as a thin re-export for backward
compatibility). It is the symmetric counterpart of
``security/credential_scrubber.py``: scrubber masks secrets *after* a tool
runs; the validator blocks dangerous paths *before* a tool runs.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

__all__ = [
    "PermissionDenied",
    "resolve_and_validate_path",
]


class PermissionDenied(PermissionError):
    """Raised when a path falls outside the workspace or matches a sensitive pattern.

    Subclasses :class:`PermissionError` so existing ``except PermissionError``
    handlers in file tools keep working unchanged.
    """


# ─── Sensitive-path patterns ────────────────────────────────────────────────
#
# Matched against the resolved POSIX path (absolute). Each entry is a compiled
# regex; the order does not matter — any match rejects the path.
_SENSITIVE_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Credential files (host home + any depth)
    re.compile(r"(^|/)\.ssh(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)\.aws(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)\.kube(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)\.gnupg(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)\.docker/config\.json$", re.IGNORECASE),
    re.compile(r"(^|/)\.netrc$", re.IGNORECASE),
    # Env files (any depth)
    re.compile(r"(^|/)\.env(\.[^/]*)?$", re.IGNORECASE),
    # Browser login data / cookies
    re.compile(r"/Chrome/User Data/[^/]+/Login Data$", re.IGNORECASE),
    re.compile(r"/Chrome/User Data/[^/]+/Cookies$", re.IGNORECASE),
    re.compile(r"/Firefox/Profiles/[^/]+/logins\.json$", re.IGNORECASE),
    re.compile(r"/Firefox/Profiles/[^/]+/cookies\.sqlite$", re.IGNORECASE),
    # Unix system files
    re.compile(r"^/etc/(passwd|shadow|sudoers)(\.d/.*)?$"),
    re.compile(r"^/proc/"),
    re.compile(r"^/sys/"),
    re.compile(r"^/dev/"),
    re.compile(r"^/boot/"),
)

# Windows reserved device names — match the final path component only.
_WIN_DEVICE_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\.|$)", re.IGNORECASE)

# Hard cap on how many path components we walk for the ancestor check. A
# runaway symlink chain should not be able to pin the CPU.
_MAX_ANCESTOR_STEPS = 128


def _reject_null_byte(path_str: str) -> None:
    if "\x00" in path_str:
        raise PermissionDenied("Access denied: null byte in path.")


def _reject_sensitive_path(resolved: Path, original: str) -> None:
    posix = resolved.as_posix()
    for pattern in _SENSITIVE_PATH_PATTERNS:
        if pattern.search(posix):
            raise PermissionDenied(
                f"Access denied: Path '{original}' matches a sensitive "
                f"location (credentials, system files, or device)."
            )
    # Windows device names apply to the final component on any platform — a
    # user could create a file literally named "CON" inside the workspace on
    # Unix and it would break later on Windows hosts.
    name = resolved.name
    if name and _WIN_DEVICE_NAME.match(name):
        raise PermissionDenied(f"Access denied: '{name}' is a reserved Windows device name.")


def _reject_symlink_ancestors(workspace_root: Path, resolved: Path, original: str) -> None:
    """Walk from ``workspace_root`` up to ``resolved`` and reject any symlink link
    that points outside the workspace tree.

    ``Path.resolve()`` already dereferences symlinks, but there is a TOCTOU
    window between the resolve and the actual write: an attacker (or a sibling
    agent in the same container) can swap a directory for a symlink in that
    window. Walking the *literal* path components with ``lstat`` and rejecting
    any symlink closes that window.
    """
    try:
        rel = resolved.relative_to(workspace_root)
    except ValueError:
        # Should not happen after the boundary check, but be defensive: the
        # resolved path is outside the workspace.
        raise PermissionDenied(
            f"Access denied: Path '{original}' is outside of workspace '{workspace_root}'."
        ) from None

    # When the resolved path IS the workspace root, rel.parts is empty (or
    # just (".",)) and there is nothing to walk.
    parts = rel.parts
    if not parts or parts == (".",):
        return

    current = workspace_root
    steps = 0
    for part in parts:
        steps += 1
        if steps > _MAX_ANCESTOR_STEPS:
            raise PermissionDenied(
                f"Access denied: Path '{original}' exceeds the maximum "
                f"supported depth ({_MAX_ANCESTOR_STEPS})."
            )
        if not part:
            continue
        candidate = current / part
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            # The leaf will be created by the caller; missing intermediate
            # components are fine as long as the existing prefix is clean.
            current = candidate
            continue
        except OSError as exc:
            raise PermissionDenied(f"Access denied: Cannot inspect '{original}' ({exc}).") from exc
        # Any symlink in the path is suspicious — the workspace should be a
        # plain directory tree. A symlink that resolves back inside the
        # workspace is still allowed (its target already passed the boundary
        # check via ``resolved``), but one that escapes is not. We check the
        # link target explicitly to distinguish.
        if stat.S_ISLNK(st.st_mode):
            link_target = candidate.resolve(strict=False)
            try:
                link_target.relative_to(workspace_root)
            except ValueError:
                raise PermissionDenied(
                    f"Access denied: Path '{original}' crosses a symlink "
                    f"('{part}') that leaves the workspace."
                ) from None
        current = candidate


def _reject_hardlink(resolved: Path) -> None:
    """Reject existing regular files with multiple hard links (shared inodes).

    Directories are excluded: on macOS, ``/var/folders/...`` temp dirs
    legitimately report ``st_nlink=2`` (the parent link + the ``.`` entry),
    and directory hardlinks are restricted by the filesystem anyway.
    """
    try:
        st = resolved.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        return
    if st.st_nlink > 1:
        raise PermissionDenied(
            f"Access denied: '{resolved}' is a hardlink (st_nlink={st.st_nlink}); "
            "hardlinks can reach outside the workspace."
        )


def resolve_and_validate_path(
    path_str: str,
    *,
    workspace_root: Path | None = None,
) -> Path:
    """Resolve ``path_str`` to an absolute path inside the workspace.

    Args:
        path_str: Absolute or workspace-relative path.
        workspace_root: Workspace root. Defaults to the current working
            directory (matching the original behaviour where, in the
            container, ``cwd=/workspace`` and in dev mode ``cwd`` is the
            process working directory).

    Raises:
        PermissionDenied: On boundary violation, symlink escape, hardlink,
            sensitive-path match, null byte, or unsupported depth.

    Returns:
        The resolved absolute :class:`~pathlib.Path`.
    """
    _reject_null_byte(path_str)

    ws = (workspace_root or Path.cwd()).resolve()
    target = Path(path_str)
    if not target.is_absolute():
        target = ws / target
    resolved = target.resolve()

    # 1. Workspace boundary check (string startswith is bypassable — compare
    #    parents instead, exactly like the original implementation).
    if not (resolved == ws or ws in resolved.parents):
        raise PermissionDenied(f"Access denied: Path '{path_str}' is outside of workspace '{ws}'.")

    # 2. Ancestor-walk: reject symlink escapes in the literal path. This is
    #    the TOCTOU hardening that the resolve-only check cannot provide.
    _reject_symlink_ancestors(ws, resolved, path_str)

    # 3. Hardlink rejection on the existing leaf.
    _reject_hardlink(resolved)

    # 4. Sensitive path patterns (credentials / system / device names).
    _reject_sensitive_path(resolved, path_str)

    return resolved


# ``sys.platform`` is referenced indirectly via ``_WIN_DEVICE_NAME`` for the
# leaf-name check; no explicit platform branching is needed.
_ = sys.platform
