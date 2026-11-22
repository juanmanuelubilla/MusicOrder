"""Metadata extraction through Mutagen (ID3, Vorbis, MP4, APE, AIFF).

Readers normalize every container into a plain :class:`TrackInfo` object.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.apev2 import APEv2File
from mutagen.flac import FLAC
from mutagen.id3 import ID3FileType
from mutagen.mp4 import MP4
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".mp4",
    ".wav", ".wv", ".ape", ".aiff", ".aifc", ".mpc", ".wma", ".ac3",
}


@dataclass
class TrackInfo:
    """A normalized metadata snapshot of a single audio file."""

    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    track_no: int | None = None
    disc_no: int | None = None
    total_tracks: int | None = None
    year: int | None = None
    genre: str = ""
    composer: str = ""
    comment: str = ""
    duration_ms: int = 0
    bitrate_kbps: int | None = None
    sample_rate_hz: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    isrc: str = ""
    recording_id: str = ""
    release_id: str = ""
    has_embedded_art: bool = False
    replay_gain: float | None = None
    tags: dict[str, list[str]] = field(default_factory=dict)

    def metadata_score(self) -> float:
        """Return a 0..100 richness score for the present metadata."""
        score = 0.0
        for value in (self.title, self.artist, self.album, self.album_artist):
            if value:
                score += 1.0
        for present in (self.track_no is not None, self.year is not None,
                        bool(self.genre), bool(self.composer), self.duration_ms > 0):
            score += float(present)
        return round(100.0 * score / 9.0, 1)


def _text(values: object) -> str:
    """Return the first string value of a tag value collection."""
    if isinstance(values, list) and values:
        return str(values[0]).strip()
    if values:
        return str(values).strip()
    return ""


def _number(values: object) -> int | None:
    value = _text(values)
    if not value:
        return None
    try:
        return int(value.split("/")[0].strip())
    except ValueError:
        return None


def _year(values: object) -> int | None:
    value = _text(values)
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits[:4])
    except ValueError:
        return None


def _gain(values: object) -> float | None:
    value = _text(values)
    try:
        return float(value.replace("dB", "").replace("db", "").strip())
    except ValueError:
        return None


def _fill_vorbis(info: TrackInfo, tags: object) -> None:
    """Extract metadata from a Vorbis-comment tag object."""
    if tags is None:
        return
    items: dict[str, list[str]] = {}
    tag_items = getattr(tags, "items", None)  # type: ignore[attr-defined]
    if not callable(tag_items):
        return
    try:
        for key, value in tag_items():  # type: ignore[misc]
            lowered = str(key).lower()
            items[lowered] = [str(v) for v in (value if isinstance(value, list) else [value])]
    except (AttributeError, TypeError):
        return
    mapping = {
        "title": "title", "artist": "artist", "album": "album",
        "albumartist": "album_artist", "album artist": "album_artist",
        "genre": "genre", "composer": "composer", "comment": "comment",
        "tracknumber": "track_no", "discnumber": "disc_no",
        "date": "year", "year": "year",
        "isrc": "isrc",
        "musicbrainz_trackid": "recording_id",
        "musicbrainz_albumid": "release_id",
        "replaygain_track_gain": "replay_gain",
    }
    for key, target in mapping.items():
        values = items.get(key) or items.get(key.upper(), [])
        if not values:
            continue
        parsed: object
        if target in ("track_no", "disc_no"):
            parsed = _number(values)
        elif target == "year":
            parsed = _year(values)
        elif target == "replay_gain":
            parsed = _gain(values)
        else:
            parsed = _text(values)
        if parsed:
            setattr(info, target, parsed)
    info.has_embedded_art = info.has_embedded_art or bool(
        getattr(tags, "metadata_block_picture", None)
    )


def _read_id3(info: TrackInfo, audio: object) -> None:
    """Extract metadata from ID3 tags."""
    tag = getattr(audio, "tags", None)
    if not tag:
        return
    try:
        frames = list(tag.values())
    except AttributeError:
        return
    joined: dict[str, list[str]] = {}
    for frame in frames:
        name = getattr(frame, "FrameID", "?")
        joined.setdefault(name, []).append(str(frame))
    info.title = _text(joined.get("TIT2"))
    info.artist = _text(joined.get("TPE1"))
    info.album_artist = _text(joined.get("TPE2")) or info.artist
    info.album = _text(joined.get("TALB"))
    info.genre = _text(joined.get("TCON"))
    info.composer = _text(joined.get("TCOM"))
    info.isrc = _text(joined.get("TSRC"))
    info.track_no = _number(joined.get("TRCK")) or info.track_no
    info.disc_no = _number(joined.get("TPOS")) or info.disc_no
    for frame_id in ("TDRC", "TYER", "TORY"):
        candidate = _year(joined.get(frame_id))
        if candidate is not None:
            info.year = candidate
            break
    for txxx in joined.get("TXXX", []):
        value = _text([txxx])
        if "MusicBrainz Recording Id" in value:
            info.recording_id = value.split("=")[-1].strip()
        elif "MusicBrainz Release Id" in value:
            info.release_id = value.split("=")[-1].strip()
    info.has_embedded_art = any(frame_id.startswith("APIC") for frame_id in joined)


def _read_mp4(info: TrackInfo, audio: object) -> None:
    """Extract metadata from MP4/M4A atom tags."""
    tag = getattr(audio, "tags", None)
    if not tag:
        return

    def get(*keys: str) -> str:
        for key in keys:
            value = tag.get(key)
            if value:
                return str(value[0])
        return ""

    info.title = get("\xa9nam")
    info.artist = get("\xa9ART")
    info.album_artist = get("aART") or info.artist
    info.album = get("\xa9alb")
    info.genre = get("\xa9gen")
    info.composer = get("\xa9wrt")
    info.year = _year([get("\xa9day")])
    info.track_no = _number([get("\xa9trk")])
    info.disc_no = _number([get("disk")])
    info.has_embedded_art = "covr" in tag


def _fill_ape(info: TrackInfo, audio: object) -> None:
    """Extract metadata from an APEv2 tag (used by .ape, .wv and .mpc)."""
    tag = audio
    if not tag:
        return
    tag_items = getattr(tag, "items", None)
    if not callable(tag_items):
        return
    items: dict[str, str] = {}
    tag_pairs = tag_items()  # type: ignore[misc]
    for key, value in tag_pairs:  # type: ignore[assignment]
        key_text = key.decode() if isinstance(key, bytes) else str(key)
        value_text = str(value)
        if isinstance(value, bytes):
            try:
                value_text = value.decode("utf-8")
            except UnicodeDecodeError:
                value_text = value.hex()
        items[key_text] = value_text
    info.title = _text([items.get("Title", "")])
    info.artist = _text([items.get("Artist", "")])
    info.album = _text([items.get("Album", "")])
    info.genre = _text([items.get("Genre", "")])
    info.composer = _text([items.get("Composer", "")])
    info.track_no = _number([items.get("Track", "")])
    info.year = _year([items.get("Year", "")])
    info.disc_no = _number([items.get("Disc", "")])


def _fill_audio_info(info: TrackInfo, audio: object) -> None:
    """Fill technical audio information from ``audio.info``."""
    meta = getattr(audio, "info", None)
    if meta is None:
        return
    duration = getattr(meta, "length", None)
    if duration:
        info.duration_ms = int(duration * 1000)
    bitrate = getattr(meta, "bitrate", None)
    if bitrate:
        info.bitrate_kbps = int(bitrate // 1000)
    sample_rate = getattr(meta, "sample_rate", None)
    if sample_rate:
        info.sample_rate_hz = int(sample_rate)
    channels = getattr(meta, "channels", None)
    if channels:
        info.channels = int(channels)
    depth = getattr(meta, "bits_per_sample", None)
    if depth:
        info.bit_depth = int(depth)


def read_metadata(path: str | Path) -> TrackInfo | None:
    """Read normalized metadata from a single audio file.

    Returns ``None`` for non-audio files or unparsable containers.
    """
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        return None
    try:
        audio = MutagenFile(str(file_path))
    except Exception as exc:
        logger.debug("Cannot open %s: %s", file_path, exc)
        return None
    if audio is None:
        return None

    info = TrackInfo()
    try:
        audio_tags = getattr(audio, "tags", None)
        if audio_tags is not None:
            if isinstance(audio, ID3FileType):
                _read_id3(info, audio)
            elif isinstance(audio, FLAC):
                _fill_vorbis(info, audio_tags)
            elif isinstance(audio, (OggVorbis,
                                    OggOpus,
                                    OggFLAC)):
                _fill_vorbis(info, audio_tags)
            elif isinstance(audio, MP4):
                _read_mp4(info, audio)
            elif isinstance(audio, APEv2File):
                _fill_ape(info, audio_tags)
            elif isinstance(audio, AIFF):
                _read_id3(info, audio)
        _fill_audio_info(info, audio)
    except Exception as error:
        logger.warning("Tag parsing failed for %s: %s", file_path, error)
    return info


def write_metadata_tags(path: str | Path, info: TrackInfo) -> list[str]:
    """Write the fields of ``info`` into the file's native tags.

    Non-empty ``info`` values overwrite the corresponding tags; empty fields are
    left untouched. Returns a list of error messages (empty on success). The
    embedded artwork is never touched.
    """
    errors: list[str] = []
    try:
        audio = MutagenFile(str(path))
    except Exception as exc:  # noqa: BLE001
        return [f"cannot open {path}: {exc}"]
    if audio is None:
        return [f"unsupported container: {path}"]
    try:
        if isinstance(audio, ID3FileType):
            _write_id3(audio, info)
        elif isinstance(audio, (FLAC, OggVorbis, OggOpus, OggFLAC)):
            _write_vorbis(audio, info)
        elif isinstance(audio, MP4):
            _write_mp4(audio, info)
        elif isinstance(audio, APEv2File):
            _write_ape(audio, info)
        else:
            errors.append(f"tag container not writable: {path}")
        if not errors:
            audio.save()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{path}: {exc}")
    return errors


def _write_id3(audio: object, info: TrackInfo) -> None:
    from mutagen.id3 import ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK

    tag = getattr(audio, "tags", None)
    if not isinstance(tag, ID3):
        return
    if info.title:
        tag.add(TIT2(encoding=3, text=[info.title]))
    if info.artist:
        tag.add(TPE1(encoding=3, text=[info.artist]))
    if info.album_artist:
        tag.add(TPE2(encoding=3, text=[info.album_artist]))
    if info.album:
        tag.add(TALB(encoding=3, text=[info.album]))
    if info.genre:
        tag.add(TCON(encoding=3, text=[info.genre]))
    if info.composer:
        tag.add(TCOM(encoding=3, text=[info.composer]))
    if info.track_no is not None:
        tag.add(TRCK(encoding=3, text=[str(info.track_no)]))
    if info.disc_no is not None:
        tag.add(TPOS(encoding=3, text=[str(info.disc_no)]))
    if info.year is not None:
        tag.add(TDRC(encoding=3, text=[str(info.year)]))


def _write_vorbis(audio: object, info: TrackInfo) -> None:
    tag = getattr(audio, "tags", None)
    if tag is None:
        return
    for key, value in (
        ("TITLE", info.title), ("ARTIST", info.artist),
        ("ALBUMARTIST", info.album_artist), ("ALBUM", info.album),
        ("GENRE", info.genre), ("COMPOSER", info.composer),
    ):
        if value:
            tag[key] = [str(value)]
    if info.track_no is not None:
        tag["TRACKNUMBER"] = [str(info.track_no)]
    if info.disc_no is not None:
        tag["DISCNUMBER"] = [str(info.disc_no)]
    if info.year is not None:
        tag["DATE"] = [str(info.year)]


def _write_mp4(audio: object, info: TrackInfo) -> None:
    tag = getattr(audio, "tags", None)
    if tag is None:
        return
    if info.title:
        tag["\xa9nam"] = [info.title]
    if info.artist:
        tag["\xa9ART"] = [info.artist]
    if info.album_artist:
        tag["aART"] = [info.album_artist]
    if info.album:
        tag["\xa9alb"] = [info.album]
    if info.genre:
        tag["\xa9gen"] = [info.genre]
    if info.composer:
        tag["\xa9wrt"] = [info.composer]
    if info.track_no is not None:
        tag["trkn"] = [(info.track_no, info.track_no)]
    if info.year is not None:
        tag["\xa9day"] = [str(info.year)]


def _write_ape(audio: object, info: TrackInfo) -> None:
    tag = getattr(audio, "tags", None)
    if tag is None:
        return
    for key, value in (
        ("Title", info.title), ("Artist", info.artist),
        ("Album", info.album), ("Genre", info.genre),
        ("Composer", info.composer),
    ):
        if value:
            tag[key] = str(value)
    if info.track_no is not None:
        tag["Track"] = str(info.track_no)
    if info.year is not None:
        tag["Year"] = str(info.year)


__all__ = ["TrackInfo", "read_metadata", "write_metadata_tags", "AUDIO_EXTENSIONS"]
