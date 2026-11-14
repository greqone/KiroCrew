# SPDX-License-Identifier: Apache-2.0
"""Member record files are written without following links.

A member's folder is ``members/<slug>`` under the data home. The writers of a file
there (the greeting marker and the welcome and create-request records) must refuse a
link at ``members``, at ``members/<slug>`` or at the file itself, leave what the link
points at byte for byte as it was, and create nothing behind it. A plain folder keeps
working as before.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from conftest import requires_symlinks
from kiro_crew import atomic_write, members, pinned_fs
from kiro_crew.dashboard import mate_welcome
from kiro_crew.testing.links import make_dir_link

SLUG = "ada"


@dataclass(frozen=True)
class Site:
    """One writer: how to call it and the file it touches."""

    name: str
    call: Callable[[], Any]
    leaf: str


def _sites() -> list[Site]:
    slug = members.member_slug(SLUG)
    return [
        Site(
            "greeting_marker",
            lambda: mate_welcome.claim_greeting_marker(slug, "k"),
            mate_welcome.GREETING_MARKER_FILENAME,
        ),
        Site(
            "member_record",
            lambda: members._write_member_record(
                SLUG, None, members.WELCOME_OWED_FILENAME, {"member": SLUG}
            ),
            members.WELCOME_OWED_FILENAME,
        ),
    ]


SITES = _sites()
IDS = [s.name for s in SITES]

needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="a swap after the open only matters where the folder is held by descriptor",
)
needs_posix_modes = pytest.mark.skipif(
    sys.platform == "win32", reason="owner-only modes are a POSIX notion"
)


def _run(site: Site) -> Any:
    """Call the writer; a refusal of any kind is the result, not a failure of the test."""
    try:
        return site.call()
    except Exception as exc:  # noqa: BLE001 - the outcome is read off the disk
        return exc


def _snapshot(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = path.read_bytes()
    return out


def _members() -> Path:
    return members.members_root()


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    (outside / SLUG).mkdir(parents=True)
    return outside


def _peer() -> Path:
    """Another member's real folder, inside the members root."""
    peer = _members() / "bob"
    peer.mkdir(parents=True)
    return peer


@pytest.fixture
def no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform that cannot open relative to a directory descriptor."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_plain_member_folder_is_written_as_before(site: Site) -> None:
    result = _run(site)
    assert not isinstance(result, Exception), result
    assert (_members() / members.member_slug(SLUG) / site.leaf).is_file()


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_members_is_refused(site: Site, tmp_path: Path) -> None:
    outside = _outside(tmp_path)
    before = _snapshot(outside)
    make_dir_link(_members(), outside)
    assert isinstance(_run(site), members.MemberSlugError)
    assert _snapshot(outside) == before, f"{site.name} changed what the link points at"


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_the_member_folder_is_refused(site: Site) -> None:
    peer = _peer()
    before = _snapshot(peer)
    make_dir_link(_members() / SLUG, peer)
    assert isinstance(_run(site), members.MemberSlugError)
    assert _snapshot(peer) == before, f"{site.name} changed what the link points at"


@requires_symlinks
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_the_file_is_refused(site: Site, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    os.symlink(target, folder / site.leaf)
    result = _run(site)
    assert target.read_bytes() == b"untouched"
    assert (folder / site.leaf).is_symlink(), "the link itself was replaced"
    assert isinstance(result, members.MemberSlugError), result
    assert sorted(p.name for p in folder.iterdir()) == [site.leaf], "something was created"


@requires_symlinks
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_the_file_is_refused_without_descriptor_pinning(
    site: Site, tmp_path: Path, no_pin: None
) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    os.symlink(target, folder / site.leaf)
    _run(site)
    assert target.read_bytes() == b"untouched"
    assert (folder / site.leaf).is_symlink(), "the link itself was replaced"


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_members_is_refused_without_descriptor_pinning(
    site: Site, tmp_path: Path, no_pin: None
) -> None:
    outside = _outside(tmp_path)
    before = _snapshot(outside)
    make_dir_link(_members(), outside)
    assert isinstance(_run(site), members.MemberSlugError)
    assert _snapshot(outside) == before, f"{site.name} changed what the link points at"


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_the_member_folder_is_refused_without_descriptor_pinning(
    site: Site, no_pin: None
) -> None:
    peer = _peer()
    before = _snapshot(peer)
    make_dir_link(_members() / SLUG, peer)
    assert isinstance(_run(site), members.MemberSlugError)
    assert _snapshot(peer) == before, f"{site.name} changed what the link points at"


@needs_posix_modes
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_folders_made_by_a_writer_are_owner_only(site: Site) -> None:
    old = os.umask(0o022)
    try:
        assert not isinstance(_run(site), Exception)
    finally:
        os.umask(old)
    folder = _members() / members.member_slug(SLUG)
    for path in (_members(), folder):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
    assert stat.S_IMODE((folder / site.leaf).stat().st_mode) == 0o600


@needs_posix_modes
@pytest.mark.parametrize(("site", "expected"), [(SITES[0], 0o755), (SITES[1], 0o700)], ids=IDS)
def test_an_existing_folder_keeps_or_gains_the_mode_its_writer_gives_it(
    site: Site, expected: int
) -> None:
    """A claim leaves an existing folder as it is; a record writer tightens it, as before."""
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    for path in (_members(), folder):
        path.chmod(0o755)
    assert not isinstance(_run(site), Exception)
    for path in (_members(), folder):
        assert stat.S_IMODE(path.stat().st_mode) == expected, path


@needs_posix_modes
@pytest.mark.parametrize(
    "site",
    [s for s in SITES if s.name != "greeting_marker"],
    ids=[s.name for s in SITES if s.name != "greeting_marker"],
)
def test_a_record_written_without_descriptor_pinning_is_owner_only(
    site: Site, no_pin: None
) -> None:
    assert not isinstance(_run(site), Exception)
    leaf = _members() / members.member_slug(SLUG) / site.leaf
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o600


def test_the_welcome_record_call_reports_a_refusal_as_false(tmp_path: Path) -> None:
    outside = _outside(tmp_path)
    before = _snapshot(outside)
    make_dir_link(_members(), outside)
    assert members.mark_welcome_owed(SLUG) is False
    assert _snapshot(outside) == before


@needs_pinned_walk
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_folder_swapped_for_a_link_after_it_is_open_is_not_written_through(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    peer = _peer()
    before = _snapshot(peer)

    def swap() -> None:
        folder.rename(_members() / "moved-away")
        make_dir_link(folder, peer)

    fired: list[bool] = []
    real = {name: getattr(os, name) for name in ("open", "replace", "rename", "unlink")}

    def wrap(name: str) -> Callable[..., Any]:
        def inner(*args: Any, **kwargs: Any) -> Any:
            names = [str(a) for a in args[:2] if isinstance(a, (str, os.PathLike))]
            if not fired and any(site.leaf in n for n in names):
                fired.append(True)
                swap()
            return real[name](*args, **kwargs)

        return inner

    with monkeypatch.context() as mp:
        wrapped = {name: wrap(name) for name in real}
        # The wrappers must still count as descriptor-relative, or the code under test
        # sees a platform that cannot pin and takes the by-name route.
        mp.setattr(
            os,
            "supports_dir_fd",
            {
                wrapped.get(fn.__name__, fn) if fn in real.values() else fn
                for fn in os.supports_dir_fd
            },
        )
        for name, fn in wrapped.items():
            mp.setattr(os, name, fn)
        _run(site)
    assert _snapshot(peer) == before, f"{site.name} changed what the link points at"


@needs_posix_modes
@needs_pinned_walk
def test_a_folder_swapped_for_a_link_after_it_is_open_is_not_tightened_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    peer = _peer()
    peer.chmod(0o755)
    fired: list[bool] = []

    def swap_first(real: Callable[..., Any]) -> Callable[..., Any]:
        def inner(*args: Any, **kwargs: Any) -> Any:
            if not fired:
                fired.append(True)
                folder.rename(_members() / "moved-away")
                make_dir_link(folder, peer)
            return real(*args, **kwargs)

        return inner

    with monkeypatch.context() as mp:
        for name in ("chmod", "fchmod"):
            mp.setattr(os, name, swap_first(getattr(os, name)))
        _run(SITES[1])
    assert fired, "the writer never tightened a folder"
    assert stat.S_IMODE(peer.stat().st_mode) == 0o755


@requires_symlinks
@needs_pinned_walk
@needs_posix_modes
def test_the_members_folder_stays_held_after_the_walk() -> None:
    from kiro_crew import member_folder

    root = _members()
    victim = root.parent / "victim"
    victim.mkdir()
    victim.chmod(0o755)
    with member_folder.open_member_folder(SLUG) as folder:
        assert folder.parent_fd is not None
        # The members folder is swapped for a link after the walk has passed it.
        root.rename(root.parent / "moved-away")
        make_dir_link(root, victim)
        os.fchmod(folder.parent_fd, stat.S_IRWXU)
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755
    assert stat.S_IMODE((root.parent / "moved-away").stat().st_mode) == 0o700


@requires_symlinks
@needs_pinned_walk
@needs_posix_modes
def test_the_record_writer_tightens_members_through_the_held_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import member_folder

    root = _members()
    victim = root.parent / "victim"
    victim.mkdir()
    victim.chmod(0o755)
    real_open = member_folder.open_member_folder

    @contextmanager
    def open_then_swap(slug: str) -> Iterator[Any]:
        with real_open(slug) as folder:
            # The members folder is swapped for a link after the walk has passed it.
            root.rename(root.parent / "moved-away")
            make_dir_link(root, victim)
            yield folder

    monkeypatch.setattr(member_folder, "open_member_folder", open_then_swap)
    assert members.mark_welcome_owed(SLUG)
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755
    moved = root.parent / "moved-away"
    assert stat.S_IMODE(moved.stat().st_mode) == 0o700
    assert (moved / members.member_slug(SLUG) / members.WELCOME_OWED_FILENAME).is_file()


@requires_symlinks
def test_a_link_at_the_greeting_marker_is_refused_not_read_as_already_claimed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    os.symlink(target, folder / SITES[0].leaf)
    with pytest.raises(members.MemberSlugError):
        SITES[0].call()
    assert target.read_bytes() == b"untouched"


@requires_symlinks
def test_a_link_at_the_record_file_is_refused_and_the_callers_report_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    os.symlink(target, folder / members.WELCOME_OWED_FILENAME)
    with pytest.raises(members.MemberSlugError):
        SITES[1].call()
    assert members.mark_welcome_owed(SLUG) is False
    assert "Could not record the first welcome" in caplog.text
    assert target.read_bytes() == b"untouched"


@needs_posix_modes
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
def test_a_record_that_exists_with_wider_modes_is_rewritten_owner_only(
    pinned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        # Without the data home's owner-only default, the record's own request decides.
        monkeypatch.setattr(atomic_write, "is_owner_only_target", lambda path: False)
    elif not pinned_fs.supports_pinned_walk():
        pytest.skip("descriptor pinning is not available here")
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    leaf = folder / SITES[1].leaf
    leaf.write_text("{}\n")
    leaf.chmod(0o644)
    assert not isinstance(_run(SITES[1]), Exception)
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o600


@needs_posix_modes
def test_an_existing_folder_is_tightened_by_name_without_descriptor_pinning(no_pin: None) -> None:
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    assert not isinstance(_run(SITES[1]), Exception)
    assert stat.S_IMODE(folder.stat().st_mode) == stat.S_IRWXU


@needs_pinned_walk
def test_a_folder_swapped_for_a_link_before_the_write_creates_anything_is_not_written_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.member_folder import MemberFolder

    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    peer = _peer()
    before = _snapshot(peer)
    moved = _members() / "moved-away"
    fired: list[bool] = []
    real_refuse_link = MemberFolder.refuse_link

    def swap_after_the_link_check(self: MemberFolder, name: str) -> None:
        real_refuse_link(self, name)
        if not fired:
            fired.append(True)
            folder.rename(moved)
            make_dir_link(folder, peer)

    monkeypatch.setattr(MemberFolder, "refuse_link", swap_after_the_link_check)
    assert not isinstance(_run(SITES[1]), Exception)
    assert fired, "the writer never checked the file"
    assert (moved / SITES[1].leaf).is_file(), "the record did not land in the folder that was open"
    assert _snapshot(peer) == before, "the record was written through the swapped-in link"
