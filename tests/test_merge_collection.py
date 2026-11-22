"""Tests for the conservative merge executor and tag-based identity."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from musicfusion.sync.analyzer import analyze_sync
from musicfusion.sync.executor import build_actions, execute_merge


@pytest.fixture()
def trees(tmp_path: Path) -> tuple[Path, Path]:
    master = tmp_path / "master"
    incoming = tmp_path / "incoming"

    def album(root: Path, artist: str, folder: str, titles: list[str]) -> None:
        d = root / artist / folder
        d.mkdir(parents=True)
        for i, t in enumerate(titles, 1):
            (d / f"{i:02d} - {t}.flac").write_bytes(b"")

    album(master, "Babasónicos", "1992 - Pasto", ["Intro", "D-Generación"])
    album(master, "Babasónicos", "1996 - Dopádromo", ["Zumba", "El Medium"])
    album(incoming, "Babasonicos", "Pasto", ["Intro", "D-Generación"])          # existe
    album(incoming, "Babasonicos", "1994 - Trance Zomba", ["Desarmate"])        # nuevo
    album(incoming, "Divididos", "1993 - La Era de la Boludez", ["Jijiji"])     # artista nuevo
    return master, incoming


def test_build_actions_states_and_labels(trees: tuple[Path, Path]) -> None:
    master, incoming = trees
    plan = analyze_sync(master, incoming)
    actions = build_actions(plan)

    by_item = {a.item: a for a in actions}
    pasto = by_item["Pasto"]
    assert (pasto.status, pasto.action_label, pasto.kind) == ("EXISTE", "NO_HACER_NADA", "NONE")

    trance = by_item["1994 - Trance Zomba"]
    assert (trance.status, trance.action_label) == ("FALTA", "COPIAR")
    # destination uses MASTER artist name
    assert trance.destination == master / "Babasónicos" / "1994 - Trance Zomba"
    assert trance.source == incoming / "Babasonicos" / "1994 - Trance Zomba"

    divididos = by_item["Divididos"]
    assert divididos.kind == "COPY_ARTIST"
    assert (divididos.status, divididos.action_label) == ("FALTA", "COPIAR")
    assert divididos.destination == master / "Divididos"


def test_dry_run_writes_nothing_and_execute_requires_confirmation(
    trees: tuple[Path, Path],
) -> None:
    master, _incoming = trees
    plan = analyze_sync(*trees)
    actions = build_actions(plan)

    # without confirmation it is a safe no-op
    result = execute_merge(actions)
    after = sorted(str(p) for p in master.rglob("*"))
    assert not [p for p in after if "Trance" in p]
    assert result.copied == []


def test_real_copy_adds_only_missing_under_master_naming(
    trees: tuple[Path, Path],
) -> None:
    master, _incoming = trees
    plan = analyze_sync(master, trees[1])
    actions = build_actions(plan)
    result = execute_merge(actions, assume_yes=True)

    copied_names = "\n".join(result.copied)
    assert "Babasónicos/1994 - Trance Zomba" in copied_names
    assert "Divididos" in copied_names
    assert not any("Babasonicos/" in c.split("master/")[-1] for c in result.copied), \
        "el nombre de carpeta del maestro debe preservarse"
    # master original albums untouched
    assert (master / "Babasónicos" / "1992 - Pasto").is_dir()
    assert result.files_copied == 2  # 1 archivo del álbum nuevo + Jijiji del artista nuevo


def test_never_overwrites_existing_destination(
    tmp_path: Path,
) -> None:
    master = tmp_path / "master"
    incoming = tmp_path / "incoming"
    (master / "Babasónicos").mkdir(parents=True)
    inc_album = incoming / "Babasonicos" / "1994 - Trance Zomba"
    inc_album.mkdir(parents=True)
    (inc_album / "01 - Desarmate.flac").write_text("nuevo")
    # pre-create the destination with a sentinel file
    dst = master / "Babasónicos" / "1994 - Trance Zomba"
    dst.mkdir(parents=True)
    (dst / "sentinel.txt").write_text("NO BORRAR")

    plan = analyze_sync(master, incoming)
    actions = build_actions(plan)
    copiar = [a for a in actions if a.action_label == "COPIAR"]

    # the analyzer already sees the folder as existing -> no COPIAR at all
    assert copiar == []

    # even if forced, executor must skip an existing destination
    from musicfusion.sync.executor import MergeAction

    forced = MergeAction(
        kind="COPY_ALBUM", status="FALTA", action_label="COPIAR",
        source=inc_album, destination=dst,
        reason="test", confidence=99.0, item="1994 - Trance Zomba",
        artist_incoming="Babasonicos", artist_master="Babasónicos",
    )
    result = execute_merge([forced], assume_yes=True)
    assert result.skipped_existing and not result.copied
    assert (dst / "sentinel.txt").read_text() == "NO BORRAR"


def _ffmpeg_ok() -> bool:
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0
    except OSError:
        return False


@pytest.mark.skipif(not _ffmpeg_ok(), reason="ffmpeg required")
def test_tag_identity_beats_folder_name(tmp_path: Path) -> None:
    """albumartist/artist tags identify the artist when folders are messy."""
    master = tmp_path / "master"
    incoming = tmp_path / "incoming"
    import mutagen

    def tagged_album(root: Path, folder_artist: str, album: str, tag_artist: str,
                     titles: list[str]) -> None:
        d = root / folder_artist / album
        d.mkdir(parents=True)
        for i, t in enumerate(titles, 1):
            p = d / f"{i:02d} - {t}.flac"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(p)],
                check=True, capture_output=True,
            )
            audio = mutagen.File(str(p))
            audio.update({"ARTIST": tag_artist, "ALBUMARTIST": tag_artist,
                          "ALBUM": album, "TITLE": t})
            audio.save()

    tagged_album(master, "Soda Stereo", "1988 - Doble Vida", "Soda Stereo",
                 ["Picnic en el 4 B", "En la Ciudad de la Furia"])
    # PATH 2: messy folder name, correct tags
    tagged_album(incoming, "soda_stereo (coleccion)", "Doble Vida (1988)",
                 "Soda Stereo", ["Picnic en el 4 B", "En la Ciudad de la Furia"])

    plan = analyze_sync(master, incoming)
    artist = plan.artists[0]
    assert artist.decision == "ARTIST_MATCHED"
    assert artist.master_name == "Soda Stereo"
    assert artist.tag_artist == "soda stereo"
    album = artist.albums[0]
    assert album.decision == "EXISTS"
    assert album.master_folder == "1988 - Doble Vida"
    assert album.evidence["identity_source"].startswith("tags")


@pytest.mark.skipif(not _ffmpeg_ok(), reason="ffmpeg required")
def test_tag_folder_conflict_is_reported_not_decided(tmp_path: Path) -> None:
    """Tags contradicting the folder name are surfaced as conflicts."""
    incoming = tmp_path / "incoming"
    import mutagen

    d = incoming / "Carpeta Rara" / "2020 - Algo"
    d.mkdir(parents=True)
    p = d / "01 - Tema.flac"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(p)],
        check=True, capture_output=True,
    )
    audio = mutagen.File(str(p))
    audio.update({"ARTIST": "Otro Artista Total", "ALBUMARTIST": "Otro Artista Total"})
    audio.save()

    empty_master = tmp_path / "master"
    empty_master.mkdir()
    plan = analyze_sync(empty_master, incoming)
    artist = plan.artists[0]
    assert artist.tag_artist == "otro artista total"
    assert any("conflict" in c.lower() or "revisar" in c.lower() for c in artist.conflicts)


def test_executor_never_touches_source_tree(trees: tuple[Path, Path]) -> None:
    """PATH 2 must remain byte-identical after a real merge."""
    master, incoming = trees
    snapshot = {
        str(p): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(incoming.rglob("*"))
    }
    plan = analyze_sync(master, incoming)
    execute_merge(build_actions(plan), assume_yes=True)
    snapshot_after = {
        str(p): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(incoming.rglob("*"))
    }
    assert snapshot == snapshot_after
