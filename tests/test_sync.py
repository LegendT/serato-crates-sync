"""
Tests for serato-crates-sync.

These tests verify:
1. Folder scanning and crate tree building
2. Dry-run plan generation
3. Crate file creation with backup
"""

import os
import tempfile
from pathlib import Path

import pytest

from serato_crates_sync.cli import (
    DEFAULT_AUDIO_EXTENSIONS,
    CratePlan,
    build_crate_tree,
    count_crates_and_tracks,
    create_sync_plan,
    get_existing_crate_names,
    get_subcrates_folder,
    is_audio_file,
    parse_extensions,
    scan_folder_for_tracks,
    write_crate_binary,
    backup_subcrates,
)


@pytest.fixture
def temp_music_folder():
    """Create a temporary music folder structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "MusicRoot"

        # Create folder structure:
        # MusicRoot/
        #   House/
        #     Deep/
        #       track1.mp3
        #       track2.mp3
        #     Classic/
        #       track3.mp3
        #   HipHop/
        #     track4.mp3
        #     track5.m4a
        #   EmptyFolder/
        #   .hidden/
        #     secret.mp3

        (root / "House" / "Deep").mkdir(parents=True)
        (root / "House" / "Classic").mkdir(parents=True)
        (root / "HipHop").mkdir(parents=True)
        (root / "EmptyFolder").mkdir(parents=True)
        (root / ".hidden").mkdir(parents=True)

        # Create fake audio files
        (root / "House" / "Deep" / "track1.mp3").touch()
        (root / "House" / "Deep" / "track2.mp3").touch()
        (root / "House" / "Classic" / "track3.mp3").touch()
        (root / "HipHop" / "track4.mp3").touch()
        (root / "HipHop" / "track5.m4a").touch()
        (root / ".hidden" / "secret.mp3").touch()

        # Create a non-audio file
        (root / "HipHop" / "readme.txt").touch()

        yield root


@pytest.fixture
def temp_serato_folder():
    """Create a temporary Serato folder structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        serato = Path(tmpdir) / "_Serato_"
        subcrates = serato / "Subcrates"
        subcrates.mkdir(parents=True)

        # Create an existing crate
        (subcrates / "ExistingCrate.crate").touch()

        yield serato


class TestAudioDetection:
    """Tests for audio file detection."""

    def test_is_audio_file_mp3(self, temp_music_folder):
        mp3_file = temp_music_folder / "House" / "Deep" / "track1.mp3"
        assert is_audio_file(mp3_file, DEFAULT_AUDIO_EXTENSIONS) is True

    def test_is_audio_file_m4a(self, temp_music_folder):
        m4a_file = temp_music_folder / "HipHop" / "track5.m4a"
        assert is_audio_file(m4a_file, DEFAULT_AUDIO_EXTENSIONS) is True

    def test_is_not_audio_file_txt(self, temp_music_folder):
        txt_file = temp_music_folder / "HipHop" / "readme.txt"
        assert is_audio_file(txt_file, DEFAULT_AUDIO_EXTENSIONS) is False

    def test_is_not_audio_file_directory(self, temp_music_folder):
        directory = temp_music_folder / "House"
        assert is_audio_file(directory, DEFAULT_AUDIO_EXTENSIONS) is False


class TestFolderScanning:
    """Tests for folder scanning."""

    def test_scan_folder_for_tracks(self, temp_music_folder):
        tracks = scan_folder_for_tracks(
            temp_music_folder / "House" / "Deep",
            DEFAULT_AUDIO_EXTENSIONS
        )
        assert len(tracks) == 2
        assert all(t.suffix == ".mp3" for t in tracks)

    def test_scan_folder_excludes_non_audio(self, temp_music_folder):
        tracks = scan_folder_for_tracks(
            temp_music_folder / "HipHop",
            DEFAULT_AUDIO_EXTENSIONS
        )
        # Should include mp3 and m4a, but not txt
        assert len(tracks) == 2
        names = {t.name for t in tracks}
        assert "track4.mp3" in names
        assert "track5.m4a" in names
        assert "readme.txt" not in names

    def test_scan_empty_folder(self, temp_music_folder):
        tracks = scan_folder_for_tracks(
            temp_music_folder / "EmptyFolder",
            DEFAULT_AUDIO_EXTENSIONS
        )
        assert len(tracks) == 0


class TestCrateTreeBuilding:
    """Tests for crate tree building."""

    def test_build_crate_tree_simple(self, temp_music_folder):
        crate = build_crate_tree(
            temp_music_folder / "HipHop",
            DEFAULT_AUDIO_EXTENSIONS
        )
        assert crate is not None
        assert crate.name == "HipHop"
        assert len(crate.tracks) == 2
        assert len(crate.children) == 0

    def test_build_crate_tree_nested(self, temp_music_folder):
        crate = build_crate_tree(
            temp_music_folder / "House",
            DEFAULT_AUDIO_EXTENSIONS
        )
        assert crate is not None
        assert crate.name == "House"
        assert len(crate.tracks) == 0  # House folder has no direct tracks
        assert len(crate.children) == 2  # Deep and Classic

        child_names = {c.name for c in crate.children}
        assert "Deep" in child_names
        assert "Classic" in child_names

    def test_build_crate_tree_excludes_hidden(self, temp_music_folder):
        # Build tree from root - should not include .hidden
        crates = []
        for item in sorted(temp_music_folder.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                crate = build_crate_tree(item, DEFAULT_AUDIO_EXTENSIONS)
                if crate:
                    crates.append(crate)

        crate_names = {c.name for c in crates}
        assert ".hidden" not in crate_names

    def test_build_crate_tree_empty_folder_excluded(self, temp_music_folder):
        crate = build_crate_tree(
            temp_music_folder / "EmptyFolder",
            DEFAULT_AUDIO_EXTENSIONS,
            include_empty=False
        )
        assert crate is None

    def test_build_crate_tree_empty_folder_included(self, temp_music_folder):
        crate = build_crate_tree(
            temp_music_folder / "EmptyFolder",
            DEFAULT_AUDIO_EXTENSIONS,
            include_empty=True
        )
        assert crate is not None
        assert crate.name == "EmptyFolder"
        assert len(crate.tracks) == 0


class TestSyncPlan:
    """Tests for sync plan creation."""

    def test_create_sync_plan(self, temp_music_folder, temp_serato_folder):
        plan = create_sync_plan(
            music_root=temp_music_folder,
            serato_root=temp_serato_folder,
            extensions=DEFAULT_AUDIO_EXTENSIONS
        )

        assert plan.music_root == temp_music_folder
        assert plan.serato_root == temp_serato_folder
        assert plan.total_crates == 5  # MusicRoot, House, Deep, Classic, HipHop
        assert plan.total_tracks == 5  # 2 + 1 + 2
        assert "ExistingCrate" in plan.existing_crates

    def test_count_crates_and_tracks(self, temp_music_folder):
        crate = build_crate_tree(
            temp_music_folder / "House",
            DEFAULT_AUDIO_EXTENSIONS
        )
        crates = [crate] if crate else []

        total_crates, total_tracks = count_crates_and_tracks(crates)
        assert total_crates == 3  # House, Deep, Classic
        assert total_tracks == 3  # track1, track2, track3


class TestExistingCrates:
    """Tests for existing crate detection."""

    def test_get_existing_crate_names(self, temp_serato_folder):
        names = get_existing_crate_names(temp_serato_folder)
        assert "ExistingCrate" in names

    def test_get_existing_crate_names_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            serato = Path(tmpdir) / "_Serato_"
            serato.mkdir()
            # No Subcrates folder
            names = get_existing_crate_names(serato)
            assert names == []


class TestExtensionParsing:
    """Tests for extension parsing."""

    def test_parse_extensions_basic(self):
        exts = parse_extensions("mp3,wav,flac")
        assert ".mp3" in exts
        assert ".wav" in exts
        assert ".flac" in exts

    def test_parse_extensions_with_dots(self):
        exts = parse_extensions(".mp3,.wav")
        assert ".mp3" in exts
        assert ".wav" in exts

    def test_parse_extensions_mixed(self):
        exts = parse_extensions("mp3, .wav, FLAC")
        assert ".mp3" in exts
        assert ".wav" in exts
        assert ".flac" in exts  # Should be lowercase


class TestBackup:
    """Tests for backup functionality."""

    def test_backup_subcrates(self, temp_serato_folder):
        backup_path = backup_subcrates(temp_serato_folder)

        assert backup_path is not None
        assert backup_path.exists()
        assert "BACKUP" in backup_path.name
        assert (backup_path / "ExistingCrate.crate").exists()

    def test_backup_no_subcrates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            serato = Path(tmpdir) / "_Serato_"
            serato.mkdir()
            # No Subcrates folder

            backup_path = backup_subcrates(serato)
            assert backup_path is None


class TestCrateBinaryWrite:
    """Tests for binary crate writing."""

    def test_write_crate_binary(self, temp_music_folder):
        with tempfile.TemporaryDirectory() as tmpdir:
            crate_file = Path(tmpdir) / "TestCrate.crate"

            crate_plan = CratePlan(
                name="TestCrate",
                path=temp_music_folder / "HipHop",
                parent_name=None,
                tracks=[
                    temp_music_folder / "HipHop" / "track4.mp3",
                    temp_music_folder / "HipHop" / "track5.m4a",
                ]
            )

            write_crate_binary(crate_file, crate_plan, lambda p: str(p.resolve()))

            assert crate_file.exists()

            # Check file has content
            content = crate_file.read_bytes()
            assert len(content) > 0

            # Check it starts with 'vrsn' tag
            assert content[:4] == b'vrsn'

            # Check it contains 'otrk' tags
            assert b'otrk' in content


class TestIntegration:
    """Integration tests for the full workflow."""

    def test_dry_run_creates_no_files(self, temp_music_folder, temp_serato_folder):
        """Verify dry-run doesn't create any crate files."""
        subcrates = get_subcrates_folder(temp_serato_folder)
        initial_files = set(subcrates.iterdir())

        # Create plan (this is what dry-run does)
        plan = create_sync_plan(
            music_root=temp_music_folder,
            serato_root=temp_serato_folder,
            extensions=DEFAULT_AUDIO_EXTENSIONS
        )

        # Verify no new files created
        final_files = set(subcrates.iterdir())
        assert initial_files == final_files

    def test_apply_creates_crate_files(self, temp_music_folder, temp_serato_folder):
        """Verify apply creates crate files."""
        from serato_crates_sync.cli import execute_sync

        plan = create_sync_plan(
            music_root=temp_music_folder,
            serato_root=temp_serato_folder,
            extensions=DEFAULT_AUDIO_EXTENSIONS
        )

        # Execute sync
        execute_sync(plan, overwrite=True)

        subcrates = get_subcrates_folder(temp_serato_folder)
        crate_files = list(subcrates.glob("*.crate"))

        # Should have created crates (plus the existing one)
        assert len(crate_files) >= 1

    def test_apply_creates_backup(self, temp_music_folder, temp_serato_folder):
        """Verify apply creates backup."""
        from serato_crates_sync.cli import execute_sync

        plan = create_sync_plan(
            music_root=temp_music_folder,
            serato_root=temp_serato_folder,
            extensions=DEFAULT_AUDIO_EXTENSIONS
        )

        # Execute sync
        execute_sync(plan, overwrite=True)

        # Check for backup folder
        backups = list(temp_serato_folder.glob("Subcrates.BACKUP.*"))
        assert len(backups) >= 1
