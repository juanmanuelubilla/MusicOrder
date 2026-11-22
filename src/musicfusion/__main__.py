"""Application entry point.

Usage:
    python -m musicfusion          # start the GUI
    musicfusion --headless scan    # run in headless mode
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from musicfusion.config.settings import Settings, load_settings

if TYPE_CHECKING:  # pragma: no cover
    from musicfusion.matching import AlbumSignature

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the command line argument parser."""
    parser = argparse.ArgumentParser(
        prog="musicfusion",
        description="MusicFusion Pro - gestor de bibliotecas musicales",
    )
    parser.add_argument("--version", action="store_true", help="show version and exit")
    parser.add_argument(
        "--config",
        default=None,
        help="path to an explicit config file (YAML)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="path to a profile file (YAML) to import on startup",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="launch the graphical interface"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without a graphical interface",
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="incremental scan of configured libraries")
    scan.add_argument("--full", action="store_true", help="force full re-scan")
    scan.add_argument("--rehash", action="store_true", help="recompute all hashes")

    merge = sub.add_parser("merge", help="fuse multiple libraries into a destination avoiding duplicates")
    merge.add_argument("paths", nargs="*", help="source libraries followed by destination (or use --sources and --dest)")
    merge.add_argument("--sources", nargs="+", default=None, help="one or more source library paths")
    merge.add_argument("--dest", default=None, help="destination library path")
    merge.add_argument("--dry-run", "--simulation", action="store_true", help="simulation mode: analyze without copying")
    merge.add_argument("--no-subdirs", action="store_true", help="do not recurse into subdirectories")
    merge.add_argument("--no-verify", action="store_true", help="skip post-copy SHA-256 verification")
    merge.add_argument("--preserve-structure", action="store_true", help="preserve original folder hierarchy in destination")
    merge.add_argument("--normalize", "--normalize-structure", action="store_true", help="normalize destination layout as Artista/Año - Álbum/00 - Título.ext")
    merge.add_argument("--update-tags", action="store_true", help="write normalized metadata tags into copied destination files")
    merge.add_argument("--no-online", action="store_true", help="skip online release year queries (MusicBrainz/Wikipedia)")
    merge.add_argument("--report-txt", default=None, help="export report to TXT file")
    merge.add_argument("--report-csv", default=None, help="export report to CSV file")

    dup = sub.add_parser("duplicates", help="detect duplicates across libraries")
    dup.add_argument("--report", default=None, help="write a report (html/csv/xlsx/pdf)")

    match = sub.add_parser("match", help="compute how likely two files are the same song")
    match.add_argument("file_a", help="source audio file")
    match.add_argument("file_b", help="candidate audio file")
    match.add_argument(
        "--context",
        nargs=2,
        metavar=("DIR_A", "DIR_B"),
        help="album folders whose composition reinforces the match",
    )
    match.add_argument("--json", action="store_true", help="print the result as JSON")
    match.add_argument("--persist", action="store_true", help="store the result in the database")

    report = sub.add_parser("report", help="generate the latest reports")
    report.add_argument("--kind", default="html", choices=["html", "csv", "xlsx", "pdf"])

    fp = sub.add_parser(
        "fingerprint", help="compute and store acoustic fingerprints (chromaprint)"
    )
    fp.add_argument("--path", default=None, help="root folder (default: configured libraries)")
    fp.add_argument("--force", action="store_true", help="recompute even when already stored")

    sug = sub.add_parser("suggest", help="metadata centralization suggestions")
    sug.add_argument("--dirs", nargs="*", default=[], help="restrict to these folders")
    sug.add_argument("--min-gain", type=float, default=8.0, help="minimum score gain to propose")
    sug.add_argument("--apply", action="store_true", help="apply the suggestions (writes tags)")

    albums = sub.add_parser(
        "albums", help="verify album folders against MusicBrainz (title/year/track count)"
    )
    albums.add_argument("--path", required=True, help="library root laid out as Artista/Año - Álbum")
    albums.add_argument("--refresh", action="store_true", help="ignore the local cache")
    albums.add_argument(
        "--ai", action="store_true",
        help="ask the local Ollama model for a second opinion on similar-name album pairs",
    )
    albums.add_argument("--model", default=None, help="Ollama model name (default: settings)")
    albums.add_argument("--host", default=None, help="Ollama host URL (default: settings)")
    albums.add_argument(
        "--offline", action="store_true",
        help="fast LOCAL comparison only: no MusicBrainz, no network, no cache",
    )

    sync = sub.add_parser(
        "sync-plan",
        help="analyze incoming music against a MASTER collection (read-only plan)",
    )
    sync.add_argument("--master", required=True, help="PATH 1: organized master collection")
    sync.add_argument("--incoming", required=True, help="PATH 2: new/mixed collection")
    sync.add_argument("--json", action="store_true", help="emit the full structured plan as JSON")
    sync.add_argument(
        "--min-confidence", type=float, default=0.0,
        help="only show items with confidence >= this percent",
    )

    mcol = sub.add_parser(
        "merge-collection",
        help="incorporate missing artists/albums from PATH2 into MASTER PATH1",
    )
    mcol.add_argument("--master", required=True, help="PATH 1: master collection (destination)")
    mcol.add_argument("--source", required=True, help="PATH 2: new collection (origin)")
    mcol.add_argument(
        "--execute", action="store_true",
        help="perform the real copy (default is dry-run report only)",
    )
    mcol.add_argument(
        "--yes", action="store_true",
        help="skip the interactive confirmation (still requires --execute)",
    )
    mcol.add_argument("--json", action="store_true", help="emit actions as JSON")

    resolve = sub.add_parser(
        "sync-resolve",
        help="apply an agent's decisions (JSON) to a serialized sync plan",
    )
    resolve.add_argument("--plan", required=True, help="plan file (sync-plan --json output)")
    resolve.add_argument(
        "--decisions", required=True,
        help='decisions file: {"artist:X": "MATCH", "album:X/Y": "NEW_ALBUM", ...}',
    )
    resolve.add_argument("--out", default=None, help="write the resolved plan here (JSON)")

    consolidate = sub.add_parser(
        "consolidate",
        help="consolidate two sources into ONE clean collection (A + B -> destino)",
    )
    consolidate.add_argument("lib_a", help="source collection A")
    consolidate.add_argument("lib_b", help="source collection B")
    consolidate.add_argument("dest", help="final consolidated collection path")
    consolidate.add_argument(
        "--execute", action="store_true",
        help="perform the real copy (default is dry-run preview only)",
    )
    consolidate.add_argument(
        "--yes", action="store_true",
        help="skip the interactive confirmation (still requires --execute)",
    )
    consolidate.add_argument(
        "--overwrite", action="store_true",
        help="allow an existing destination (merge into it; never deletes)",
    )
    consolidate.add_argument("--json", action="store_true", help="emit the full plan as JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI."""
    args = build_parser().parse_args(argv)

    if args.version:
        from musicfusion import __version__

        print(f"MusicFusion Pro {__version__}")
        return 0

    settings = load_settings(args.config)
    if args.profile:
        settings.import_profile(args.profile)

    from musicfusion import __version__
    from musicfusion.core.log import setup_logging

    setup_logging(settings.config_dir / "logs", level=settings.log_level)
    logger.info("MusicFusion Pro starting (version=%s)", __version__)

    if args.command == "scan":
        return _run_scan(settings, full=args.full, rehash=args.rehash)
    if args.command == "merge":
        from pathlib import Path
        from musicfusion.merge.engine import FusionEngine, FusionOptions

        sources_raw = args.sources or []
        dest_raw = args.dest

        if not sources_raw and args.paths:
            if len(args.paths) >= 2:
                sources_raw = args.paths[:-1]
                dest_raw = args.paths[-1]

        if not sources_raw or not dest_raw:
            print("Error: Se deben especificar al menos una fuente y un destino.", file=sys.stderr)
            print("Uso: musicfusion merge ORIGEN1 [ORIGEN2 ...] DESTINO [opciones]", file=sys.stderr)
            return 1

        opts = FusionOptions(
            sources=[Path(p) for p in sources_raw],
            destination=Path(dest_raw),
            simulation=args.dry_run,
            include_subdirectories=not args.no_subdirs,
            verify_sha256=not args.no_verify,
            preserve_structure=args.preserve_structure,
            normalize_structure=args.normalize,
            update_tags=args.update_tags or args.normalize,
            online_lookup=not args.no_online,
        )

        def _cli_progress(p):
            if p.current_file:
                print(f"[{p.percent:5.1f}%] {p.status_message}", end="\r", flush=True)

        engine = FusionEngine(opts)
        report = engine.run(on_progress=_cli_progress)
        print()  # newline after carriage return
        print(report.summary())

        if args.report_txt:
            report.export_txt(Path(args.report_txt))
            print(f"Informe TXT exportado a: {args.report_txt}")
        if args.report_csv:
            report.export_csv(Path(args.report_csv))
            print(f"Informe CSV exportado a: {args.report_csv}")
        return 0
    if args.command == "duplicates":
        from musicfusion.duplicates.detect import DuplicateDetector

        detector = DuplicateDetector(settings)
        groups = detector.run()
        print(f"{len(groups)} grupos de duplicados.")
        if args.report:
            from musicfusion.reports.exporter import export_duplicates

            export_duplicates(detector.results_as_dicts(), args.report)
        return 0
    if args.command == "match":
        return _run_match(settings, args)
    if args.command == "report":
        from musicfusion.reports.exporter import export_all

        exported = export_all(settings)
        for path in exported:
            print(path)
        return 0
    if args.command == "fingerprint":
        return _run_fingerprint(settings, args)
    if args.command == "suggest":
        return _run_suggest(settings, args)
    if args.command == "albums":
        return _run_albums(settings, args)
    if args.command == "sync-plan":
        return _run_sync_plan(settings, args)
    if args.command == "merge-collection":
        return _run_merge_collection(settings, args)
    if args.command == "sync-resolve":
        return _run_sync_resolve(settings, args)
    if args.command == "consolidate":
        return _run_consolidate(settings, args)

    if args.headless:
        logger.error("No command provided for headless mode")
        print(
            "Modo --headless requiere un subcomando. Uso:\n"
            "  python -m musicfusion scan                 # escanear bibliotecas\n"
            "  python -m musicfusion merge A B DEST      # fusionar\n"
            "  python -m musicfusion duplicates --report R  # duplicados\n"
            "  python -m musicfusion match A B           # comparar dos ficheros\n"
            "  python -m musicfusion fingerprint         # huellas acústicas\n"
            "  python -m musicfusion suggest             # sugerencias de metadatos\n"
            "  python -m musicfusion report              # informes\n"
            "Ejecute 'python -m musicfusion scan' para empezar.",
            file=sys.stderr,
        )
        return 2

    return _run_gui(settings)


def _run_match(settings: Settings, args: argparse.Namespace) -> int:
    """Compute how likely two files are the same song."""
    import json as _json

    from musicfusion.audio.metadata import read_metadata
    from musicfusion.matching import (
        MatchConfig,
        MatchEngine,
        MatchTrack,
        album_context,
        cache_key,
        config_fingerprint,
        explain,
    )
    from musicfusion.matching.storage import MatchStore

    config = MatchConfig.from_settings(settings)
    engine = MatchEngine(config)

    files = [args.file_a, args.file_b]
    tracks: list[MatchTrack] = []
    for path in files:
        info = read_metadata(path)
        fingerprint = ""
        if info and info.recording_id:
            fingerprint = info.recording_id
        track = MatchTrack(
            path=path,
            artist=info.artist if info else "",
            album_artist=info.album_artist if info else "",
            album=info.album if info else "",
            title=info.title if info else "",
            track_no=info.track_no if info else None,
            disc_no=info.disc_no if info else None,
            year=info.year if info else None,
            duration_ms=info.duration_ms if info else 0,
            fingerprint=fingerprint,
            isrc=info.isrc if info else "",
            recording_id=info.recording_id if info else "",
        )
        tracks.append(track)

    left, right = tracks
    context = None
    if args.context:
        left_sig = album_signature_of_folder(Path(args.context[0]))
        right_sig = album_signature_of_folder(Path(args.context[1]))
        context = album_context(left_sig, right_sig)

    outcome = engine.match(left, right, context=context)
    outcome.cache_key = cache_key(left, right, config_fingerprint(config))

    if args.persist:
        from musicfusion.db.session import Database

        store = MatchStore(Database(settings.db_path))
        store.save(outcome)

    if args.json:
        print(_json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(explain(outcome))
    return 0


def _run_fingerprint(settings: Settings, args: argparse.Namespace) -> int:
    """Compute and persist acoustic fingerprints for scanned or given files."""
    from pathlib import Path

    from musicfusion.audio.fingerprint import backend_name, fingerprint_available
    from musicfusion.db.session import Database
    from musicfusion.scan.indexer import LibraryIndexer
    from musicfusion.services.fingerprints import fingerprint_map, fingerprint_track

    if not fingerprint_available():
        print(
            "No hay backend de huellas acústicas disponibles. "
            "Instala pyacoustid + libchromaprint (o el binario fpcalc)."
        )
        return 1

    db = Database(settings.db_path)
    db.create_schema()
    indexer = LibraryIndexer(settings, db)

    roots = [Path(args.path)] if args.path else [Path(lib.path) for lib in settings.libraries()]
    files = [path for root in roots for path in indexer.walk_audio_files(root)]

    if not files:
        print(f"Procesar 0 archivos (backend={backend_name()}).")
        return 0

    known = fingerprint_map(db, roots=roots) if roots else fingerprint_map(db)
    print(f"Procesar {len(files)} archivos (backend={backend_name()})…")
    computed = skipped = 0
    for index, path in enumerate(files, start=1):
        if str(path) in known and not args.force:
            skipped += 1
            continue
        fingerprint_track(db, str(path), force=args.force)
        computed += 1
        if computed % 25 == 0 or index == len(files):
            print(f"  {index}/{len(files)} huellas totales, {computed} calculadas")
    print(f"Completado: {computed} calculadas, {skipped} ya presentes.")
    return 0


def _run_suggest(settings: Settings, args: argparse.Namespace) -> int:
    """List (and optionally apply) metadata centralization suggestions."""
    from pathlib import Path

    from musicfusion.db.session import Database
    from musicfusion.services.metasuggest import apply_suggestions, build_suggestions

    db = Database(settings.db_path)
    db.create_schema()
    roots = [Path(d) for d in args.dirs] or None
    suggestions = build_suggestions(settings, db, roots=roots, min_gain=args.min_gain)

    if not suggestions:
        print("No se encontraron sugerencias de metadatos.")
        return 0
    print(f"{len(suggestions)} sugerencias de metadatos:")
    for suggestion in suggestions:
        print(
            f"  {suggestion.target_path}"
            f"  <- {suggestion.donor_path}"
            f"  (+{suggestion.gain:.1f}, por {suggestion.matched_by})"
            f"  [{', '.join(suggestion.missing)}]"
        )
    if args.apply:
        result = apply_suggestions(db, suggestions)
        print(f"Aplicadas: {result.applied} · omitidas: {result.skipped}")
        for message in result.errors:
            print(f"  ERROR: {message}")
    return 0


def _run_albums_offline(root: Path) -> int:
    """Fast local-only album comparison (no network, no MusicBrainz)."""
    from musicfusion.services.albumverify import compare_local_trees

    print(f"Comparación LOCAL de álbumes en {root} (sin red)…")
    groups = compare_local_trees(root)
    if not groups:
        print("Sin álbumes equivalentes o sospechosos: todo se ve único.")
        return 0
    marks = {
        "DUPLICADO_PROBABLE": "=",
        "POSIBLE_EDICION": "~",
        "REVISAR_MISMO_NOMBRE": "?",
        "POSIBLE_EQUIVALENTE": "?",
    }
    for group in groups:
        mark = marks.get(group["kind"], " ")
        print(
            f"  [{mark}] {group['artist']}: {group['folder_a']}  <->  {group['folder_b']}"
            f"  [{group['kind']} · {group['confidence']:.0f}%]"
        )
        print(f"        {group['tracks']} · {group['detail']}")
    print(f"\nTotal: {len(groups)} pares a revisar.")
    return 0


def _run_albums(settings: Settings, args: argparse.Namespace) -> int:
    """Verify every album folder of a tree against MusicBrainz."""
    from pathlib import Path

    from musicfusion.services.albumverify import AlbumCache, verify_album_tree
    from musicfusion.services.musicbrainz import MusicBrainzClient

    if args.host:
        settings.set("ollama.host", args.host)
    if args.model:
        settings.set("ollama.model", args.model)

    root = Path(args.path)
    if not root.is_dir():
        print(f"No existe la carpeta: {root}", file=sys.stderr)
        return 1

    if args.offline:
        return _run_albums_offline(root)

    cache_path = settings.config_dir / "mb_albums_cache.json"
    if args.refresh and cache_path.exists():
        cache_path.unlink()
    cache = AlbumCache(cache_path)
    client = MusicBrainzClient()

    print(f"Verificando álbumes de {root} contra MusicBrainz…")
    results = verify_album_tree(root, client=client, cache=cache)
    marks = {"ok": "=", "count_mismatch": "!", "not_found": "?", "no_artist": "?", "error": "x"}
    for result in results:
        mark = marks.get(result.status, " ")
        official = "-" if result.mb_tracks is None else str(result.mb_tracks)
        title = f" -> '{result.mb_title}' ({result.mb_year or '?'})" if result.matched else ""
        print(f"  [{mark}] {result.artist} / {result.album_name}"
              f"  [{result.local_tracks} locales | {official} MB]{title}")
        if result.status != "ok":
            print(f"        {result.detail}")
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print(
        f"Total: {len(results)} álbumes -> "
        + ", ".join(f"{status}: {n}" for status, n in sorted(counts.items()))
    )

    if args.ai:
        _run_album_ai_opinions(settings, results)
    return 0


def _run_album_ai_opinions(settings: Settings, results) -> None:
    """Second opinion of the local model for similar-name album pairs.

    The model never answers from memory: each pair is grounded with retrieved
    evidence (official MusicBrainz tracklists; Wikipedia summaries when
    MusicBrainz has no match), so brand-new albums are decidable too.
    """
    from musicfusion.services.albumverify import (
        ai_pair_verdicts,
        find_similar_album_pairs,
    )
    from musicfusion.services.musicbrainz import MusicBrainzClient
    from musicfusion.services.ollamalocal import configured_client
    from musicfusion.services.wikipedia import WikipediaClient

    pairs = find_similar_album_pairs(results, results)
    if not pairs:
        print("IA: no hay pares de álbumes con nombre similar para opinar.")
        return

    ai_client = configured_client(settings)
    if not ai_client.available():
        print(
            f"IA: Ollama no disponible en {ai_client.host} "
            "(modelo '" + ai_client.model + "'). Revisa Ajustes o usa --model."
        )
        return
    verdict_cache = settings.config_dir / "mb_ai_verdicts.json"
    mb_client = MusicBrainzClient()
    wiki_client = WikipediaClient()
    print(f"IA ({ai_client.model}): {len(pairs)} pares similares…")
    for left, right, verdict, cached in ai_pair_verdicts(
        pairs,
        ai_client=ai_client,
        mb_client=mb_client,
        wiki_client=wiki_client,
        verdict_cache=verdict_cache,
    ):
        label = f"{left.artist} / '{left.album_name}' vs '{right.album_name}'"
        if verdict is None:
            print(f"  [ai?] {label}: sin veredicto del modelo")
            continue
        print(
            f"  [ai] {label} => {verdict.relationship}"
            f" ({verdict.confidence:.0f}%){' (cache)' if cached else ''}: {verdict.reason}"
        )


def _run_sync_plan(settings: Settings, args: argparse.Namespace) -> int:
    """Read-only sync analysis of an incoming collection vs the MASTER.

    This is the deterministic entry point Hermes Agent can call before
    reasoning about ambiguous cases (see docs/COMANDOS.md, sección sync-plan).
    """
    import json as _json
    from pathlib import Path

    from musicfusion.sync.analyzer import analyze_sync

    master = Path(args.master)
    incoming = Path(args.incoming)
    if not master.is_dir():
        print(f"No existe el maestro: {master}", file=sys.stderr)
        return 1
    if not incoming.is_dir():
        print(f"No existe la entrada: {incoming}", file=sys.stderr)
        return 1

    plan = analyze_sync(master, incoming, settings=settings)
    if args.json:
        print(_json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
        return 0

    data = plan.as_dict()
    print(f"MAESTRO : {plan.master}")
    print(f"ENTRADA : {plan.incoming}")
    print(f"Artistas: {data['artists_total']}")
    for artist in plan.artists:
        mark = {"ARTIST_MATCHED": "=", "AMBIGUOUS_ARTIST": "?", "NEW_ARTIST": "+"}[artist.decision]
        print(
            f"\n[{mark}] {artist.incoming_name}"
            + (f" -> {artist.master_name}" if artist.master_name else "")
            + f"  [{artist.decision} · {artist.confidence:.0f}%]"
        )
        print(f"      {artist.reason}")
        if artist.decision == "NEW_ARTIST":
            print("      acción: IGNORAR (nada de este artista se copiará)")
            continue
        for album in artist.albums:
            amark = {
                "EXISTS": "=",
                "NEW_ALBUM": "+",
                "AMBIGUOUS_ALBUM": "?",
                "IGNORE": "-",
            }.get(album.decision, " ")
            line = f"   [{amark}] {album.incoming_folder}"
            if album.master_folder:
                line += f" == {album.master_folder}"
            line += f"  [{album.decision} · {album.confidence:.0f}%]"
            if album.confidence < args.min_confidence and not album.review_required:
                continue
            print(line)
            if album.destination:
                print(f"         destino propuesto: {album.destination}")
            if album.evidence.get("tracklist_similarity") is not None:
                print(
                    f"         evidencia: tracklist={album.evidence['tracklist_similarity']:.2f}, "
                    f"pistas {album.evidence.get('tracks_incoming', '?')} vs "
                    f"{album.evidence.get('tracks_master', '?')}"
                )
            print(f"         {album.reason}")
    summary = data["albums_by_decision"]
    print("\nResumen álbumes:", ", ".join(f"{k}: {v}" for k, v in sorted(summary.items())) or "ninguno")
    review = data["needs_review"]
    if review:
        print("Casos para revisión del agente:", ", ".join(review))
    return 0


def _run_merge_collection(settings: Settings, args: argparse.Namespace) -> int:
    """MERGE PATH2 -> PATH1: add only what is really missing.

    Dry-run by default (detailed report, zero writes). The real copy requires
    --execute plus explicit confirmation (--yes or interactive 'si').
    """
    import json as _json
    from pathlib import Path

    from musicfusion.sync.analyzer import analyze_sync
    from musicfusion.sync.executor import build_actions, execute_merge

    master = Path(args.master)
    source = Path(args.source)
    if not master.is_dir():
        print(f"No existe el maestro (PATH 1): {master}", file=sys.stderr)
        return 1
    if not source.is_dir():
        print(f"No existe la colección nueva (PATH 2): {source}", file=sys.stderr)
        return 1

    plan = analyze_sync(master, source, settings=settings)
    actions = build_actions(plan)

    if args.json:
        print(_json.dumps({
            "plan": plan.as_dict(),
            "actions": [a.as_dict() for a in actions],
            "dry_run": not args.execute,
        }, ensure_ascii=False, indent=2))
        return 0

    mode = "DRY-RUN (no se escribe nada)" if not args.execute else "EJECUCIÓN REAL"
    print(f"MAESTRO  (PATH 1): {master}")
    print(f"ORIGEN   (PATH 2): {source}")
    print(f"MODO     : {mode}")
    print("-" * 78)
    for action in actions:
        head = f"{action.status:<20} {action.action_label:<15}"
        is_album = bool(action.item) and action.item != action.artist_incoming
        item = (
            f"{action.artist_incoming}/{action.item}" if is_album else f"{action.artist_incoming}"
        )
        extra = ""
        if action.destination:
            extra = f"\n      destino: {action.destination}"
        elif action.artist_master:
            extra = f"\n      equivalente maestro: {action.artist_master}"
        print(f"[{head}] {item}  ({action.confidence:.0f}%){extra}")
        print(f"      {action.reason}")

    pending = [a for a in actions if a.action_label == "COPIAR"]
    reviews = [a for a in actions if a.action_label == "REVIEW"]
    print("-" * 78)
    print(f"A copiar: {len(pending)} | revisión: {len(reviews)} | "
          f"total ítems: {len(actions)}")

    if not args.execute:
        print("\nDry-run completado. No se modificó ningún archivo.")
        print("Para aplicar: repita con --execute (y confirme con --yes o interactivamente).")
        return 0

    if not pending:
        print("Nada que copiar.")
        return 0

    confirmed = args.yes
    if not confirmed:
        answer = input(f"¿Copiar {len(pending)} elementos en el MAESTRO? (si/no): ")
        confirmed = answer.strip().lower() in {"si", "sí", "s", "y", "yes"}
    if not confirmed:
        print("Cancelado. No se modificó nada.")
        return 1

    result = execute_merge(actions, assume_yes=True)
    for copied in result.copied:
        print(f"COPIADO : {copied}")
    for skipped in result.skipped_existing:
        print(f"OMITIDO (ya existía): {skipped}")
    for error in result.errors:
        print(f"ERROR   : {error}", file=sys.stderr)
    print(
        f"Listo: {result.files_copied} archivos, "
        f"{result.bytes_copied / (1024 * 1024):.1f} MiB copiados."
    )
    return 0 if not result.errors else 2


def _run_sync_resolve(settings: Settings, args: argparse.Namespace) -> int:
    """Hermes bridge: apply decisions to a serialized plan (file-based flow).

    Typical agent workflow (no Python needed):
      1. sync-plan --master P1 --incoming P2 --json > plan.json
      2. read plan.json, reason about items in needs_review
      3. write decisions.json {"artist:X": "MATCH", "album:X/Y": "IGNORE"}
      4. sync-resolve --plan plan.json --decisions decisions.json --out final.json
    """
    import json as _json
    from pathlib import Path

    from musicfusion.sync.analyzer import apply_decisions

    plan_path = Path(args.plan)
    decisions_path = Path(args.decisions)
    if not plan_path.is_file():
        print(f"No existe el plan: {plan_path}", file=sys.stderr)
        return 1
    if not decisions_path.is_file():
        print(f"No existe el archivo de decisiones: {decisions_path}", file=sys.stderr)
        return 1
    try:
        plan_dict = _json.loads(plan_path.read_text(encoding="utf-8"))
        decisions = _json.loads(decisions_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"JSON inválido: {exc}", file=sys.stderr)
        return 1

    result = apply_decisions(plan_dict, decisions)

    out_path = Path(args.out) if args.out else None
    payload = _json.dumps(
        {"result": result, "plan": plan_dict}, ensure_ascii=False, indent=2
    )
    if out_path is not None:
        out_path.write_text(payload + "\n", encoding="utf-8")
        print(f"Plan resuelto escrito en {out_path}")
    else:
        print(payload)

    print(
        f"Aplicadas: {len(result['applied'])} · rechazadas: {len(result['rejected'])} · "
        f"a copiar: {len(plan_dict.get('new_albums_to_copy', []))}"
    )
    return 0


def _run_consolidate(settings: Settings, args: argparse.Namespace) -> int:
    """CONSOLIDATION stage: A + B -> ONE clean collection (dry-run by default).

    Pipeline: analyze -> plan -> validate -> [confirm] -> copy -> verify -> log.
    Sources are never modified; unresolved conflicts land in DEST/_REVISION.
    """
    from pathlib import Path

    from musicfusion.db.session import Database
    from musicfusion.merge.consolidator import ConsolidationPlanner

    lib_a, lib_b, dest = Path(args.lib_a), Path(args.lib_b), Path(args.dest)
    for path in (lib_a, lib_b):
        if not path.is_dir():
            print(f"No existe la fuente: {path}", file=sys.stderr)
            return 1

    database = Database(settings.db_path)
    planner = ConsolidationPlanner(settings, database)
    try:
        plan = planner.plan(lib_a, lib_b, dest, overwrite=args.overwrite)
    except ValueError as exc:
        print(f"Plan rechazado: {exc}", file=sys.stderr)
        return 1

    if args.json:
        import json as _json

        print(_json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(plan.preview_text())

    if not args.execute:
        copies = sum(1 for a in plan.actions if a.kind == "copy")
        reviews = sum(1 for a in plan.actions if a.kind == "review")
        skips = sum(1 for a in plan.actions if a.kind == "skip")
        print(
            "\nDry-run completado (0 archivos modificados). "
            f"copias={copies} omitidos={skips} revision={reviews}"
        )
        print("Para aplicar la consolidación repita con --execute.")
        return 0

    try:
        result, verification = planner.execute(
            plan, dry_run=False, assume_yes=args.yes, overwrite=args.overwrite
        )
    except KeyboardInterrupt as exc:
        print(str(exc))
        return 1
    except ValueError as exc:
        print(f"Ejecución cancelada: {exc}", file=sys.stderr)
        return 2
    if result is not None:
        print(f"Copiados: {result.copied} · omitidos: {result.skipped} · errores: {result.errors}")
    print(verification.summary())
    for item in verification.missing[:20]:
        print(f"  FALTA: {item}")
    for item in verification.sources_modified[:20]:
        print(f"  FUENTE MODIFICADA (¡no debería pasar!): {item}")
    if verification.ok:
        print(
            f"Fuentes intactas: A y B sin modificaciones "
            f"({verification.sources_checked} archivos verificados)."
        )
    return 0 if verification.ok else 2


def album_signature_of_folder(folder: Path) -> AlbumSignature:
    """Build an album signature from every audio file under ``folder``."""
    from musicfusion.audio.metadata import read_metadata
    from musicfusion.matching import album_signature, member_from_track

    members = []
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() not in {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav"}:
            continue
        info = read_metadata(path)
        if info is None:
            continue
        members.append(
            member_from_track(
                title=info.title or path.stem,
                artist=info.artist or "",
                disc_no=info.disc_no,
                track_no=info.track_no,
                duration_ms=info.duration_ms,
                path=str(path),
            )
        )
    return album_signature(
        title=folder.name,
        artist=members[0].artist if members else "",
        members=members,
    )


def _run_scan(settings: Settings, full: bool, rehash: bool) -> int:
    """Execute a scanning job and log the summary."""
    from musicfusion.scan.indexer import LibraryIndexer

    indexer = LibraryIndexer(settings)
    stats = indexer.run(full=full, force_hashes=rehash)
    logger.info(
        "Scan finished: found=%d added=%d modified=%d removed=%d",
        stats["files_found"],
        stats["files_added"],
        stats["files_modified"],
        stats["files_removed"],
    )
    return 0


def _run_gui(settings: Settings) -> int:
    """Launch the graphical interface."""
    import importlib.util

    if importlib.util.find_spec("PySide6") is None:
        print(
            "PySide6 not available. Install dependencies with:\n"
            "  python scripts/install_dependencies.py",
            file=sys.stderr,
        )
        return 1

    import os

    if sys.platform != "linux" or "DISPLAY" in os.environ:
        from musicfusion.ui.application import run

        return int(run(settings))
    print("No display available; run with --headless or launch inside a desktop.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
