"""Tests for the MusicBrainz album verification service (no network)."""

from __future__ import annotations

from pathlib import Path

from musicfusion.services.albumverify import AlbumCache, verify_album_folder
from musicfusion.services.musicbrainz import ReleaseInfo


class FakeClient:
    """Offline stand-in for MusicBrainzClient."""

    def __init__(self, groups: list[ReleaseInfo], track_counts: dict[str, int]) -> None:
        self.groups = groups
        self.track_counts = track_counts
        self.search_calls = 0

    def search_releasegroup(self, artist: str, album: str, limit: int = 5) -> list[ReleaseInfo]:
        self.search_calls += 1
        return list(self.groups)

    def lookup_release_group(self, group_id: str) -> dict:
        return {"releases": [{"id": group_id + "-r1", "date": "1999-06-08"}]}

    def lookup_release(self, release_id: str) -> dict:
        count = self.track_counts.get(release_id.replace("-r1", ""), 0)
        return {"media": [{"track-count": count}]}

    def release_group_tracklist(self, group_id: str) -> dict | None:
        count = self.track_counts.get(group_id)
        if count is None:
            return None
        return {
            "count": count,
            "titles": [f"Tema {n}" for n in range(1, count + 1)],
        }


def _make_tree(base: Path) -> Path:
    folder = base / "Babasonicos" / "2008 - Mucho"
    folder.mkdir(parents=True)
    for n in range(1, 11):
        (folder / f"{n:02d} - Tema {n}.flac").write_bytes(b"x")
    return folder


def test_verify_album_ok(tmp_path: Path) -> None:
    folder = _make_tree(tmp_path)
    client = FakeClient(
        [ReleaseInfo(title="Mucho", artist="Babasónicos", release_id="rg-mucho",
                     year=2008, score=95)],
        {"rg-mucho": 10},
    )
    result = verify_album_folder(folder, client=client)
    assert result.matched is True
    assert result.status == "ok"
    assert result.mb_title == "Mucho"
    assert result.mb_year == 2008
    assert result.mb_tracks == 10
    assert result.local_tracks == 10


def test_verify_album_count_mismatch_detects_other_edition(tmp_path: Path) -> None:
    """Mucho + (13 files) against the 10-track 'Mucho' release is flagged."""
    folder = tmp_path / "Babasonicos" / "2009 - Mucho +"
    folder.mkdir(parents=True)
    for n in range(1, 14):
        (folder / f"{n:02d} - Tema {n}.flac").write_bytes(b"x")
    client = FakeClient(
        [ReleaseInfo(title="Mucho", artist="Babasónicos", release_id="rg-mucho",
                     year=2008, score=90)],
        {"rg-mucho": 10},
    )
    result = verify_album_folder(folder, client=client)
    assert result.status == "count_mismatch"
    assert result.local_tracks == 13 and result.mb_tracks == 10


def test_verify_album_not_found(tmp_path: Path) -> None:
    folder = tmp_path / "Desconocido" / "2020 - Rarísimo"
    folder.mkdir(parents=True)
    (folder / "01 - X.flac").write_bytes(b"x")
    client = FakeClient([], {})
    result = verify_album_folder(folder, client=client)
    assert result.matched is False
    assert result.status == "not_found"


def test_verify_album_uses_cache(tmp_path: Path) -> None:
    folder = _make_tree(tmp_path)
    cache = AlbumCache(tmp_path / "cache.json")
    client = FakeClient(
        [ReleaseInfo(title="Mucho", artist="Babasónicos", release_id="rg-mucho",
                     year=2008, score=95)],
        {"rg-mucho": 10},
    )
    first = verify_album_folder(folder, client=client, cache=cache)
    calls_after_first = getattr(client, "search_calls", 0)
    second = verify_album_folder(folder, client=client, cache=cache)
    assert first.status == second.status == "ok"
    assert getattr(client, "search_calls", 0) == calls_after_first


# ---------------------------------------------------------------------------
# shared pair/verdict helpers
# ---------------------------------------------------------------------------

def _result(name: str, artist: str, status: str = "ok"):
    from pathlib import Path as _Path

    from musicfusion.services.albumverify import AlbumVerification

    return AlbumVerification(
        folder=_Path("/x") / name, artist=artist, album_name=name, year=None,
        local_tracks=10, status=status,
    )


def test_find_similar_pairs_within_and_across_trees() -> None:
    from musicfusion.services.albumverify import find_similar_album_pairs

    tree_a = [_result("Mucho", "B"), _result("Infame", "B")]
    tree_b = [_result("Mucho +", "B"), _result("Dopadromo", "B")]
    within = find_similar_album_pairs(tree_a, None)
    assert [(a.album_name, b.album_name) for a, b in within] == []
    cross = find_similar_album_pairs(tree_a, tree_b)
    assert len(cross) == 1
    assert cross[0][0].album_name == "Mucho" and cross[0][1].album_name == "Mucho +"


def test_ai_pair_verdicts_with_stubs(tmp_path: Path) -> None:
    from musicfusion.services.albumverify import ai_pair_verdicts
    from musicfusion.services.ollamalocal import OllamaClient

    class StubOllama(OllamaClient):
        def __init__(self):
            super().__init__(host="http://fake", model="m")
            self.calls = 0

        def available(self):
            return True

        def chat_json(self, system, user):
            self.calls += 1
            return {"relationship": "deluxe_or_reissue", "confidence": 90,
                    "reason": "temas extra"}

    class StubMB:
        def release_group_tracklist(self, rg_id):
            return {"count": 10, "titles": ["A", "B"]}

    class StubWiki:
        def album_summary(self, artist, album):
            return None

    left, right = _result("Mucho", "B"), _result("Mucho +", "B")
    cache = tmp_path / "verdicts.json"
    stub = StubOllama()
    out = ai_pair_verdicts([(left, right)], ai_client=stub, mb_client=StubMB(),
                           wiki_client=StubWiki(), verdict_cache=cache)
    assert len(out) == 1
    _, _, verdict, cached = out[0]
    assert verdict is not None and verdict.relationship == "deluxe_or_reissue"
    assert cached is False and stub.calls == 1

    # second run: served from cache, no new AI call
    out2 = ai_pair_verdicts([(left, right)], ai_client=stub, mb_client=StubMB(),
                            wiki_client=StubWiki(), verdict_cache=cache)
    assert out2[0][3] is True and stub.calls == 1


# ---------------------------------------------------------------------------
# modo OFFLINE (sin red): agrupación local de álbumes equivalentes
# ---------------------------------------------------------------------------

def test_compare_local_trees_finds_duplicates_without_network(tmp_path: Path) -> None:
    from musicfusion.services.albumverify import compare_local_trees

    root = tmp_path / "lib"
    for artist, folder, titles in [
        ("Babasonicos", "1992 - Pasto", ["Intro", "D-Generacion", "Natural"]),
        ("Babasónicos", "1992 - Pasto", ["Intro", "D-Generación", "Natural"]),
        ("Babasonicos", "1999 - Miami", ["El Ringo", "Gustavo Show"]),
        ("Soda Stereo", "1988 - Doble Vida", ["Picnic en el 4 B"]),
    ]:
        d = root / artist / folder
        d.mkdir(parents=True)
        for i, t in enumerate(titles, 1):
            (d / f"{i:02d} - {t}.flac").write_bytes(b"")

    groups = compare_local_trees(root)
    dupes = [g for g in groups if g["kind"] == "DUPLICADO_PROBABLE"]
    assert len(dupes) == 1
    assert dupes[0]["artist"].startswith("Baba")
    assert "Pasto" in dupes[0]["folder_a"] and "Pasto" in dupes[0]["folder_b"]


def test_compare_local_trees_cross_tree_and_same_name_diff_content(
    tmp_path: Path,
) -> None:
    from musicfusion.services.albumverify import compare_local_trees

    a = tmp_path / "A"
    b = tmp_path / "B"
    for root in (a, b):
        d = root / "Babasónicos" / "1992 - Pasto"
        d.mkdir(parents=True)
    for i, t in enumerate(["Intro", "D-Generación"], 1):
        (a / "Babasónicos" / "1992 - Pasto" / f"{i:02d} - {t}.flac").write_bytes(b"")
    for i, t in enumerate(["Tema Ajeno", "Otra Cosa"], 1):
        (b / "Babasónicos" / "1992 - Pasto" / f"{i:02d} - {t}.flac").write_bytes(b"")

    groups = compare_local_trees(a, b)
    assert len(groups) == 1
    assert groups[0]["kind"] == "REVISAR_MISMO_NOMBRE"
    assert "[A]" in groups[0]["folder_a"] and "[B]" in groups[0]["folder_b"]


# ---------------------------------------------------------------------------
# alineación pista-a-pista contra la tracklist oficial (idea clave)
# ---------------------------------------------------------------------------

def test_track_alignment_full_match(tmp_path: Path) -> None:
    from musicfusion.services.albumverify import track_alignment

    files = []
    for i, t in enumerate(["Intro", "D-Generacion", "Natural"], 1):
        p = tmp_path / f"{i:02d} - {t}.flac"
        p.write_bytes(b"")
        files.append(p)
    official = ["Intro", "D-Generación", "Natural"]  # con tildes, como MB

    result = track_alignment(files, official)
    assert result["complete_pct"] == 100.0
    assert all(p["status"] == "OK" for p in result["pairs"])
    assert result["missing"] == [] and result["extra"] == []


def test_track_alignment_detects_missing_and_extra(tmp_path: Path) -> None:
    from musicfusion.services.albumverify import track_alignment

    files = [tmp_path / "01 - Zumba.flac", tmp_path / "02 - Bonus Raro.flac"]
    for p in files:
        p.write_bytes(b"")
    official = ["Zumba", "El Medium", "Cybernecia"]

    result = track_alignment(files, official)
    statuses = {p["official_no"]: p["status"] for p in result["pairs"]}
    assert statuses[1] == "OK"                      # Zumba presente
    assert {"no": 2, "title": "El Medium"} in result["missing"]
    assert {"no": 3, "title": "Cybernecia"} in result["missing"]
    assert result["extra"] == ["02 - Bonus Raro.flac"]
    assert result["complete_pct"] < 100.0


def test_track_alignment_renumbered(tmp_path: Path) -> None:
    from musicfusion.services.albumverify import track_alignment

    # mismo tema pero en otra posición
    files = [tmp_path / "07 - Su Ciervo.flac"]
    files[0].write_bytes(b"")
    official = ["Su Ciervo", "Gronchotica"]

    result = track_alignment(files, official)
    pair = result["pairs"][0]
    assert pair["status"] == "RENUMERADA"
    assert pair["local_no"] == 7 and pair["official_no"] == 1


def test_tracklist_titles_cached_for_offline_ai_evidence(tmp_path: Path) -> None:
    """The cached payload now carries the official titles (not just count)."""
    folder = _make_tree(tmp_path)
    cache = AlbumCache(tmp_path / "cache.json")
    client = FakeClient(
        [ReleaseInfo(title="Mucho", artist="Babasónicos", release_id="rg-mucho",
                     year=2008, score=95)],
        {"rg-mucho": 10},
    )
    result = verify_album_folder(folder, client=client, cache=cache)
    entry = list(cache._data.values())[0]
    assert len(entry["payload"]["official_titles"]) == 10
    # alignment ran against the cached titles: perfect local match
    assert result.missing_tracks == []
    assert result.complete_pct == 100.0
    assert "completo (100%)" in result.detail


def test_old_cache_payload_without_titles_does_not_crash(tmp_path: Path) -> None:
    """Cached entries from BEFORE the tracklist feature have no official
    titles: verification must still succeed (complete_pct simply absent)."""
    from musicfusion.services.albumverify import AlbumVerification, _finish

    folder = _make_tree(tmp_path)  # 10 archivos "Tema N"
    result = AlbumVerification(
        folder=folder, artist="Babasónicos", album_name="Mucho",
        year=2008, local_tracks=10,
    )
    old_payload = {
        "found": True, "title": "Mucho", "artist": "Babasónicos",
        "year": 2008, "tracks": 10, "rg_id": "rg-x", "similarity": 95.0,
        # SIN official_titles: formato de caché antiguo
    }
    finished = _finish(result, old_payload)
    assert finished.status == "ok"
    assert finished.complete_pct is None
    assert finished.detail == "10/10 temas"   # sin la parte '· completo (%)'
