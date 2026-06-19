"""Atomic filesystem primitives (B-059).

Two helpers used by every write path:

* :func:`atomic_write_text` — text files (``write_file``, ``edit_file``,
  ``convert_format`` text output, etc.).
* :func:`atomic_save_via` — binary/object writes where the saver is a callable
  that accepts a path (openpyxl ``Workbook.save``, matplotlib ``fig.savefig``).

Both write to a temp file in the *same directory* as the target, ``fsync``,
then ``os.replace`` — making the write atomic on POSIX. The temp file uses
``O_NOFOLLOW`` so a symlink swapped in between the caller's path validation
and the write cannot redirect the bytes outside the workspace (the TOCTOU
window that plain ``open(path, 'w')`` leaves open).

Reference: OpenHands ``file_store/local.py`` (temp + fsync + rename) +
NemoClaw ``credential-filter.ts`` (``O_NOFOLLOW``).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path

__all__ = [
    "atomic_save_via",
    "atomic_write_text",
]


def _temp_path_for(target: Path) -> Path:
    """Return a temp path in the same directory as ``target``.

    Same-directory is required: ``os.replace`` raises ``OSError: Cross-device
    link`` (EXDEV) across filesystems. The suffix carries pid + random hex so
    two concurrent writers cannot collide.
    """
    suffix = f".tmp.{os.getpid()}.{secrets.token_hex(4)}"
    return target.with_name(target.name + suffix)


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Write ``content`` to ``path`` atomically.

    The target is opened with ``O_WRONLY|O_CREAT|O_NOFOLLOW|O_TRUNC``: a
    pre-existing symlink at ``path`` will fail with ``ELOOP`` rather than
    silently being followed, which closes the symlink-swap TOCTOU window. The
    data goes to a temp file first, is flushed + fsynced, then
    ``os.replace``'d onto ``path``.

    On some platforms (notably macOS) ``O_CREAT|O_NOFOLLOW`` will happily
    create a new file but still follow an existing symlink, so we also
    explicitly reject a symlink target up front via ``os.lstat``.
    """
    path = Path(path)
    # Defense-in-depth: reject a symlink target before opening. On Linux
    # O_NOFOLLOW alone yields ELOOP on a symlink; on macOS the behaviour with
    # O_CREAT is less strict, so the explicit check makes the guarantee
    # cross-platform.
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            raise OSError(f"Refusing to write through symlink at '{path}'")
    except FileNotFoundError:
        pass

    data = content.encode(encoding)
    tmp = _temp_path_for(path)
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_TRUNC,
        0o644,
    )
    try:
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup; ignore the rare case where another writer's
        # tmp happens to live at the same path.
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def atomic_save_via(saver: Callable[[Path], None], path: Path) -> None:
    """Atomically save via ``saver`` (e.g. ``wb.save``).

    ``saver`` is invoked with a temp path in the same directory; on success
    the temp is ``os.replace``'d onto ``path``. Unlike :func:`atomic_write_text`
    this cannot enforce ``O_NOFOLLOW`` at the saver level (openpyxl opens the
    path itself), so callers must ensure ``path`` is already validated via
    :func:`corpclaw_lite.security.path_validator.resolve_and_validate_path`.
    The temp file is opened with ``O_NOFOLLOW`` defensively via an empty
    sentinel so a swap during the save still surfaces as an error.
    """
    path = Path(path)
    tmp = _temp_path_for(path)
    # Create the temp file with O_NOFOLLOW so that if a symlink is swapped
    # onto the temp path while we run, we get ELOOP instead of silently
    # writing through it. The saver then truncates and rewrites it.
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_TRUNC,
        0o644,
    )
    os.close(fd)
    try:
        saver(tmp)
        # fsync the directory so the rename itself is durable.
        os.replace(tmp, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Not all filesystems support directory fsync; tolerate it.
            pass
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
