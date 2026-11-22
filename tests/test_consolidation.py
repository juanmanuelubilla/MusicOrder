"""Tests for the CONSOLIDATION stage (two sources -> one clean collection)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from musicfusion.merge.consolidator import (
    ConsolidationPlanner,
    _display_choice,
)


def _av_available() -> bool:
    try:
        return (
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=False).returncode == 0
        )
    except OSError:
        return False


def _write_audio(path: Path, freq: int = 440, duration: float = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
            "-c:a", "flac", str(path),
        ],
        check=True,
        capture_output=True,
    )


def _tag(path: Path, **tags: str) -> None:
    import mutagen

    audio = mutagen.File(str(path))
    assert audio is not None
    audio.update(tags)
    audio.save()


def _settings(tmp_path: Path):
    from musicfusion.config.settings import Settings

    settings = Settings(config_dir=tmp_path / "config")
    settings.set("db.path", str(tmp_path / "db.sqlite"))
    settings.set("watch.enabled", False)
    settings.set("scan.include_extensions", [".flac"])
    return settings


def _planner(settings):
    from musicfusion.db.session import Database

    return ConsolidationPlanner(settings, Database(settings.db_path))


pytestmark = pytest.mark.skipif(not _av_available(), reason="ffmpeg required")


# ---------------------------------------------------------------------------
# pure unit checks of naming + album gate
# ---------------------------------------------------------------------------


def test_display_choice_prefers_accented_variant() -> None:
    assert _display_choice("Babasonicos", "Babasónicos") == "Babasónicos"
    assert _display_choice("Babasónicos", "Babasonicos") == "Babasónicos"
    assert _display_choice("Soda Stereo", "soda stereo") in {"Soda Stereo", "soda stereo"}


def test_album_gate_separates_trinchera_from_avanzada() -> None:
    from pathlib import Path as P

    from musicfusion.compare.candidates import AlbumFolder, AlbumPairing
    from musicfusion.matching.engine import MatchTrack

    def folder(name: str, key: str, titles: list[str]) -> AlbumFolder:
        book = AlbumFolder(
            side="a", folder=P("/x") / name, raw_artist="Artista", raw_album=name,
            artist_key="artista", album_key=key, year=2022,
        )
        for title in titles:
            entry_path = book.folder / f"01 - {title}.flac"
            book.entries.append(
                __import__(
                    "musicfusion.compare.candidates", fromlist=["make_track_entry"]
                ).make_track_entry(
                    entry_path, "a",
                    MatchTrack(path=str(entry_path), artist="Artista", album=name, title=title),
                )
            )
        return book

    a = folder("2022 - Trinchera", "trinchera", ["Fuego", "Luz", "Mar"])
    b = folder("2022 - Trinchera Avanzada", "trinchera avanzada", ["Fuego", "Luz", "Mar", "Extra"])
    pairing = AlbumPairing(a=a, b=b, artist_score=1.0, album_score=0.94, year_score=1.0)
    planner = _planner(_dummy_settings())
    status, _score, reason = planner._album_verdict(pairing)
    assert status == "SEPARATE"
    assert "distintos" in reason


def _dummy_settings():
    from musicfusion.config.settings import Settings

    settings = Settings(config_dir=Path("/tmp/opencode/mf-gate-config"))
    settings.set("db.path", "/tmp/opencode/mf-gate/db.sqlite")
    settings.set("watch.enabled", False)
    return settings


# ---------------------------------------------------------------------------
# integration: real trees on disk
# ---------------------------------------------------------------------------


def test_consolidation_merges_accent_variants_into_one_album(tmp_path: Path) -> None:
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    dest = tmp_path / "final"

    intro_a = root_a / "Babasonicos" / "1992 - Pasto" / "01 - Intro.flac"
    dgen_a = root_a / "Babasonicos" / "1992 - Pasto" / "02 - D-Generacion.flac"
    intro_b = root_b / "Babasónicos" / "1992 - Pasto" / "01 - Intro.flac"
    dgen_b = root_b / "Babasónicos" / "1992 - Pasto" / "02 - D-Generación.flac"
    trip_b = root_b / "Babasónicos" / "1992 - Pasto" / "03 - Tripeando.flac"

    _write_audio(intro_a, 440)
    _write_audio(dgen_a, 500)
    _tag(intro_a, TITLE="Intro", ARTIST="Babasónicos", ALBUM="Pasto")
    _tag(dgen_a, TITLE="D-Generacion", ARTIST="Babasónicos", ALBUM="Pasto")
    # same songs in B, accent-different names, distinct bytes (freq) so the
    # Decision Engine must actually pick a winner
    _write_audio(intro_b, 441)
    _write_audio(dgen_b, 501)
    _write_audio(trip_b, 620)
    _tag(intro_b, TITLE="Intro", ARTIST="Babasónicos", ALBUM="Pasto")
    _tag(dgen_b, TITLE="D-Generación", ARTIST="Babasónicos", ALBUM="Pasto")
    _tag(trip_b, TITLE="Tripeando", ARTIST="Babasónicos", ALBUM="Pasto")

    settings = _settings(tmp_path)
    planner = _planner(settings)
    plan = planner.plan(root_a, root_b, dest)

    merged = [a for art in plan.artists for a in art.albums if a.status == "MERGED"]
    assert len(merged) == 1

    babayo = next(a for a in plan.artists if a.name_a or a.name_b)
    assert babayo.display == "Babasónicos"

    copies = [action for action in plan.actions if action.kind == "copy"]
    targets = {str(action.target) for action in copies}
    assert len(targets) == len(copies), "no debe planear dos archivos sobre el mismo destino"

    album_dirs = {Path(t).parent.name for t in targets}
    assert album_dirs == {"1992 - Pasto"}
    artist_dirs = {Path(t).parent.parent.name for t in targets}
    assert artist_dirs == {"Babasónicos"}, "la salida no debe anidar artista dos veces"

    # every source song survives exactly once (winner may be A's or B's copy,
    # so compare canonical keys): Intro x1, D-Generacion x1, Tripeando x1
    from musicfusion.matching.normalize import music_tokens

    folded_targets = {" ".join(music_tokens(Path(t).stem)) for t in targets}
    assert folded_targets == {
        "01 intro",
        "02 d generacion",
        "03 tripeando",
    }

    preview = plan.preview_text()
    for token in (
        "MUSICFUSION CONSOLIDATION PREVIEW",
        "SOURCE A:",
        "SOURCE B:",
        "DESTINATION:",
        "ÁLBUM:",
        "MATCH:",
        "TRACK 01:",
        "A →",
        "B →",
        "RESULT:",
        "DECISION:",
        "UNCHANGED",
        "RESUMEN",
        "Albums:",
        "A only:",
        "B only:",
        "merged:",
        "Tracks:",
        "duplicates:",
        "conflicts:",
        "selected A:",
        "selected B:",
        "Files to destination:",
        "Source files modified:",
        "Source files deleted:",
    ):
        assert token in preview, token

    stats = plan.stats
    assert stats.albums_merged == 1
    assert stats.tracks_only_b >= 1
    removed_or_exact = stats.duplicates_removed + stats.duplicates_exact
    assert removed_or_exact >= 2, "Intro y D-Generacion deben resolverse como duplicadas"
    assert stats.files_to_destination == len(
        [a for a in plan.actions if a.kind == "copy"]
    )
    # every duplicate slot selects exactly one source
    select_actions = [a for a in plan.actions if a.decision.startswith("SELECT_")]
    assert all(a.selected in {"A", "B"} for a in select_actions)
    assert all(0.0 <= a.confidence <= 1.0 for a in plan.actions)


def test_exclusive_albums_and_tracks_are_preserved(tmp_path: Path) -> None:
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    dest = tmp_path / "final"

    only_a_album = root_a / "Banda" / "2000 - Solo A" / "01 - Unica A.flac"
    shared_a = root_a / "Banda" / "2001 - Compartido" / "01 - Tema.flac"
    only_b_album = root_b / "Banda" / "2010 - Solo B" / "01 - Unica B.flac"
    shared_b = root_b / "Bandá" / "2001 - Compartido" / "01 - Tema.flac"

    for path, title, album in (
        (only_a_album, "Unica A", "Solo A"),
        (shared_a, "Tema", "Compartido"),
        (only_b_album, "Unica B", "Solo B"),
        (shared_b, "Tema", "Compartido"),
    ):
        _write_audio(path, 300 + len(title))
        _tag(path, TITLE=title, ARTIST="Banda", ALBUM=album)

    planner = _planner(_settings(tmp_path))
    plan = planner.plan(root_a, root_b, dest)

    statuses = sorted(a.status for art in plan.artists for a in art.albums)
    assert statuses.count("EXCLUSIVE_A") == 1
    assert statuses.count("EXCLUSIVE_B") == 1
    assert statuses.count("MERGED") == 1

    copied_names = {a.source.name for a in plan.actions if a.kind == "copy"}
    assert "01 - Unica A.flac" in copied_names
    assert "01 - Unica B.flac" in copied_names
    # the shared track must exist exactly once in the destination plan
    shared_targets = [
        a.target for a in plan.actions
        if a.kind == "copy" and a.target.name == "01 - Tema.flac"
    ]
    assert len(shared_targets) == 1


def test_different_versions_are_kept_both(tmp_path: Path) -> None:
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    dest = tmp_path / "final"

    studio = root_a / "Banda" / "2005 - Disco" / "05 - Cancion.flac"
    vivo = root_b / "Banda" / "2005 - Disco" / "05 - Cancion - En Vivo.flac"
    _write_audio(studio, 330)
    _write_audio(vivo, 700)
    _tag(studio, TITLE="Cancion", ARTIST="Banda", ALBUM="Disco")
    _tag(vivo, TITLE="Cancion - En Vivo", ARTIST="Banda", ALBUM="Disco")

    planner = _planner(_settings(tmp_path))
    plan = planner.plan(root_a, root_b, dest)

    verdicts = [tv for art in plan.artists for al in art.albums for tv in al.tracks]
    different = [tv for tv in verdicts if tv.result == "DIFFERENT"]
    assert different, "una versión en vivo nunca es duplicado del original"
    assert all(tv.decision == "KEEP_BOTH" for tv in different)

    sources = {a.source.name for a in plan.actions if a.kind == "copy"}
    assert "05 - Cancion.flac" in sources
    assert "05 - Cancion - En Vivo.flac" in sources


def test_similar_named_albums_with_weak_tracklists_stay_separate(tmp_path: Path) -> None:
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    dest = tmp_path / "final"

    a1 = root_a / "Banda" / "2022 - Trinchera" / "01 - Fuego.flac"
    b1 = root_b / "Banda" / "2022 - Trinchera Avanzada" / "01 - Agua.flac"
    b2 = root_b / "Banda" / "2022 - Trinchera Avanzada" / "02 - Tierra.flac"
    for path, title in ((a1, "Fuego"), (b1, "Agua"), (b2, "Tierra")):
        _write_audio(path, 350 + len(title))
        _tag(path, TITLE=title, ARTIST="Banda", ALBUM=path.parent.name.split("- ", 1)[1])

    planner = _planner(_settings(tmp_path))
    plan = planner.plan(root_a, root_b, dest)

    statuses = [a.status for art in plan.artists for a in art.albums]
    assert "MERGED" not in statuses
    assert statuses.count("EXCLUSIVE_A") == 1
    assert statuses.count("EXCLUSIVE_B") == 1
    sources = {a.source.name for a in plan.actions if a.kind == "copy"}
    assert {"01 - Fuego.flac", "01 - Agua.flac", "02 - Tierra.flac"} <= sources


def test_unresolvable_copies_land_in_review_zone(tmp_path: Path) -> None:
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    dest = tmp_path / "final"

    fa = root_a / "Banda" / "2001 - Disco" / "01 - Igual.flac"
    fb = root_b / "Banda" / "2001 - Disco" / "01 - Igual.flac"
    # generic title ("Intro" -> requires_review) plus duration, track, year
    # and disc mismatches keep identity below MATCH_CONFIRMED (but above the
    # version gate), while equal technical quality leaves the Decision Engine
    # without a preference -> CONFLICT / REVIEW_REQUIRED
    _write_audio(fa, 440, duration=20)
    _write_audio(fb, 880, duration=22)
    _tag(fa, TITLE="Intro", ARTIST="Banda", ALBUM="Disco",
         DATE="1990", DISCNUMBER="1", TRACKNUMBER="1")
    _tag(fb, TITLE="Intro", ARTIST="Banda", ALBUM="Disco",
         DATE="2001", DISCNUMBER="2", TRACKNUMBER="4")

    planner = _planner(_settings(tmp_path))
    plan = planner.plan(root_a, root_b, dest)

    reviews = [a for a in plan.actions if a.kind == "review"]
    assert reviews, "copias demasiado igualadas deben ir a revisión"
    assert all("_REVISION" in str(a.target) for a in reviews)
    kept_sides = {Path(str(a.target)).name[:2] for a in reviews}
    assert kept_sides == {"A-", "B-"}, "ambas copias deben preservarse para revisión"
    assert plan.stats.pending_decisions >= 1
    assert plan.stats.conflicts >= 1


def test_covers_consolidate_without_duplicates(tmp_path: Path) -> None:
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    dest = tmp_path / "final"

    track_a = root_a / "Banda" / "2001 - Disco" / "01 - Uno.flac"
    track_b = root_b / "Bandá" / "2001 - Disco" / "01 - Uno.flac"
    cover_a = root_a / "Banda" / "2001 - Disco" / "cover.jpg"
    cover_b = root_b / "Bandá" / "2001 - Disco" / "cover.jpg"
    big_cover_b = root_b / "Bandá" / "2001 - Disco" / "front.png"

    for path in (track_a, track_b):
        _write_audio(path, 460)
        _tag(path, TITLE="Uno", ARTIST="Banda", ALBUM="Disco")
    cover_bytes = b"\xff\xd8\xff\xe0fakejpegdata" * 10
    cover_a.write_bytes(cover_bytes)
    cover_b.write_bytes(cover_bytes)
    big_cover_b.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 4000)

    planner = _planner(_settings(tmp_path))
    plan = planner.plan(root_a, root_b, dest)

    art_actions = [a for a in plan.actions if a.target.suffix in {".jpg", ".png"}]
    jpg_targets = [a for a in art_actions if a.target.suffix == ".jpg"]
    png_targets = [a for a in art_actions if a.target.suffix == ".png"]
    assert len(jpg_targets) == 1, "covers idénticos deben quedar una sola vez"
    assert jpg_targets[0].target.name == "cover.jpg"
    assert len(png_targets) == 1
    winners = {a.decision for a in art_actions}
    assert any(d.startswith(("SELECT_A", "SELECT_B", "COPY_FROM")) for d in winners)


def test_execute_copies_plan_and_verifies_integrity(tmp_path: Path) -> None:
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    dest = tmp_path / "final"

    unique_a = root_a / "Banda" / "1999 - Alpha" / "01 - SoloA.flac"
    dup_a = root_a / "Banda" / "1999 - Alpha" / "02 - Doble.flac"
    dup_b = root_b / "Banda" / "1999 - Alpha" / "02 - Doble.flac"
    _write_audio(unique_a, 480)
    _write_audio(dup_a, 490)
    _write_audio(dup_b, 491)
    _tag(unique_a, TITLE="SoloA", ARTIST="Banda", ALBUM="Alpha")
    _tag(dup_a, TITLE="Doble", ARTIST="Banda", ALBUM="Alpha")
    _tag(dup_b, TITLE="Doble", ARTIST="Banda", ALBUM="Alpha")

    settings = _settings(tmp_path)
    planner = _planner(settings)
    plan = planner.plan(root_a, root_b, dest)

    result, verification_dry = planner.execute(plan, dry_run=True, assume_yes=True)
    assert result is None
    assert not dest.exists()

    result, verification = planner.execute(plan, dry_run=False, assume_yes=True)
    assert verification.ok, verification.summary()
    expected_files = {
        a.target for a in plan.actions if a.kind in ("copy", "review")
    }
    existing = {p for p in expected_files if p.is_file()}
    assert existing == expected_files
    # sources untouched
    assert unique_a.is_file() and dup_a.is_file() and dup_b.is_file()


def test_sources_remain_bit_identical_after_execution(tmp_path: Path) -> None:
    """ABSOLUTE rule: A and B must be byte-for-byte identical afterwards."""
    import hashlib

    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    dest = tmp_path / "final"

    fa = root_a / "Banda" / "2001 - Disco" / "01 - Tema.flac"
    fb = root_b / "Banda" / "2001 - Disco" / "01 - Tema.flac"
    extra_b = root_b / "Bandá" / "2001 - Disco" / "cover.jpg"
    _write_audio(fa, 500)
    _write_audio(fb, 501)
    _tag(fa, TITLE="Tema", ARTIST="Banda", ALBUM="Disco")
    _tag(fb, TITLE="Tema", ARTIST="Banda", ALBUM="Disco")
    extra_b.parent.mkdir(parents=True, exist_ok=True)
    extra_b.write_bytes(b"\xff\xd8\xff\xe0jpeg")

    def digest_tree(roots):
        state = {}
        for root in roots:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    state[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return state

    roots = (root_a, root_b)
    before = digest_tree(roots)

    planner = _planner(_settings(tmp_path))
    plan = planner.plan(root_a, root_b, dest)
    _result, verification = planner.execute(plan, dry_run=False, assume_yes=True)

    after = digest_tree(roots)
    assert before == after, "las fuentes NO deben cambiar ni un byte"
    assert verification.ok
    assert verification.sources_checked >= 3
    assert not verification.sources_modified
    # every planned destination file lives strictly inside dest
    for action in plan.actions:
        if action.kind in ("copy", "review"):
            assert action.target.is_relative_to(dest)


def test_existing_destination_requires_explicit_overwrite(tmp_path: Path) -> None:
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    dest = tmp_path / "final"

    fa = root_a / "Banda" / "2001 - Disco" / "01 - Uno.flac"
    fb = root_b / "Bandá" / "2001 - Disco" / "01 - Uno.flac"
    _write_audio(fa, 520)
    _write_audio(fb, 521)
    _tag(fa, TITLE="Uno", ARTIST="Banda", ALBUM="Disco")
    _tag(fb, TITLE="Uno", ARTIST="Banda", ALBUM="Disco")

    settings = _settings(tmp_path)
    planner = _planner(settings)

    # first run into a FRESH destination works without any flag
    planner.plan(root_a, root_b, tmp_path / "fresh_dest")

    # existing destination: planning must refuse without --overwrite
    dest.mkdir()
    (dest / "sentinel.txt").write_text("no borrar")
    with pytest.raises(ValueError, match="ya existe"):
        planner.plan(root_a, root_b, dest)
    with pytest.raises(ValueError, match="ya existe"):
        planner.plan(root_a, root_b, dest)  # even twice

    plan = planner.plan(root_a, root_b, dest, overwrite=True)
    result, verification = planner.execute(
        plan, dry_run=False, assume_yes=True, overwrite=True
    )
    assert verification.ok
    assert (dest / "sentinel.txt").read_text() == "no borrar", \
        "overwrite fusiona sin destruir lo anterior"


def test_plan_records_spec_fields_for_every_action(tmp_path: Path) -> None:
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    dest = tmp_path / "final"

    fa = root_a / "Banda" / "2001 - Disco" / "01 - Uno.flac"
    fb = root_b / "Bandá" / "2001 - Disco" / "01 - Dos.flac"
    _write_audio(fa, 530)
    _write_audio(fb, 531)
    _tag(fa, TITLE="Uno", ARTIST="Banda", ALBUM="Disco")
    _tag(fb, TITLE="Dos", ARTIST="Banda", ALBUM="Disco")

    planner = _planner(_settings(tmp_path))
    plan = planner.plan(root_a, root_b, dest)

    required = {
        "source", "destination", "album", "track",
        "source_a", "source_b", "selected_source",
        "matching_score", "quality_score", "decision",
        "reason", "operation", "confidence",
    }
    payload = plan.as_dict()
    for entry in payload["actions"]:
        missing = required - set(entry)
        assert not missing, f"faltan campos del plan: {missing}"
    decisions = {entry["decision"] for entry in payload["actions"]}
    allowed = {"SELECT_A", "SELECT_B", "COPY_FROM_A", "COPY_FROM_B", "KEEP_BOTH", "REVIEW_REQUIRED"}
    assert decisions <= allowed
