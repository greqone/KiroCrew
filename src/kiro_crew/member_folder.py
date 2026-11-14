"""A member's folder under the data home, opened without following links.

``members/<slug>`` holds the files the gateway keeps for one member. The folder is
opened one level at a time, each level relative to the one before and never through a
link, and a file is then written or claimed by descriptor, so a link at ``members``,
at ``members/<slug>`` or at a file is refused rather than followed.
Where the platform cannot open relative to a descriptor the same levels are checked with
``lstat`` and the files are addressed by name.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import members
from kiro_crew.atomic_write import atomic_write, atomic_write_at
from kiro_crew.pinned_fs import dir_flags, open_pinned_descendant_dir


@dataclass(frozen=True)
class MemberFolder:
    """An open member folder: *fd* is ``None`` where the platform cannot pin one.

    *parent_fd* is the open ``members`` folder above it, or ``None`` where *fd* is.
    """

    fd: int | None
    path: Path
    parent_fd: int | None = None

    def leaf(self, name: str) -> str | Path:
        """What to hand a call that also takes ``dir_fd=self.fd`` for file *name*."""
        return name if self.fd is not None else self.path / name

    def refuse_link(self, name: str) -> None:
        """Raise :class:`members.MemberSlugError` when file *name* is a link."""
        try:
            linked = stat.S_ISLNK(
                os.stat(self.leaf(name), dir_fd=self.fd, follow_symlinks=False).st_mode
            )
        except FileNotFoundError:
            linked = False
        if linked:
            raise members.MemberSlugError(f"refusing {name!r}: it is a link")

    def write(self, name: str, text: str) -> None:
        """Replace file *name* with *text*; a link at *name* is refused, not replaced."""
        self.refuse_link(name)
        if self.fd is None:
            atomic_write(self.path / name, text, restrict_to_owner=True)
        else:
            atomic_write_at(self.fd, name, text, mode=0o600)


@contextmanager
def open_member_folder(slug: str) -> Iterator[MemberFolder]:
    """Open ``members/<slug>``, creating it owner-only; a link anywhere on the way is refused.

    The refusal is :class:`members.MemberSlugError`. A level is created relative to the
    open level above it, so nothing is made behind a link.
    """
    members.validate_slug(slug)
    root = members.members_root()
    base = root.parent.resolve()
    base.mkdir(parents=True, exist_ok=True)
    path = base / root.name / slug
    with open_pinned_descendant_dir(
        base, (root.name,), what="member folder", create=True, refusal=members.MemberSlugError
    ) as parent_fd:
        if parent_fd is None:
            # No descriptors here: the whole chain is checked by ``lstat`` instead.
            with open_pinned_descendant_dir(
                base,
                (root.name, slug),
                what="member folder",
                create=True,
                refusal=members.MemberSlugError,
            ):
                yield MemberFolder(None, path)
            return
        # The slug level is opened relative to the held ``members`` descriptor, so both
        # levels stay held and neither is looked up by name again.
        with suppress(FileExistsError):
            os.mkdir(slug, 0o700, dir_fd=parent_fd)
        try:
            fd = os.open(slug, dir_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise members.MemberSlugError(
                f"refusing to use the member folder: {slug!r} is a link or not a directory"
            ) from exc
        try:
            yield MemberFolder(fd, path, parent_fd)
        finally:
            os.close(fd)
