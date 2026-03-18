#!/Users/shinba/.openclaw/workspace/music-tools-venv/bin/python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import urlopen

from mutagen import File as MutagenFile
from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC

TZ_TPE = timezone(timedelta(hours=8))

WORKSPACE   = Path.home() / ".openclaw" / "workspace"
MUSIC_TOOLS = WORKSPACE / "music-tools"
VENV_BIN    = WORKSPACE / "music-tools-venv" / "bin"

# 確保 ffprobe/ffmpeg 可被找到
_extra = '/Users/shinba/bin'
if _extra not in os.environ.get('PATH', ''):
    os.environ['PATH'] = _extra + ':' + os.environ.get('PATH', '')

COOKIES           = WORKSPACE / "cookies.txt"
BEATPORTDL        = MUSIC_TOOLS / "beatportdl"
BEATPORT_INBOX    = Path("/tmp/openclaw-music/downloads")
LOCAL_DOWNLOAD_DIR = Path("/tmp/openclaw-music/downloads")

DROPBOX_FLAC_DIR  = "/Music-Flac"
LOCAL_DROPBOX_DIR = (
    Path.home() / "Library" / "CloudStorage"
    / "Dropbox-ProfessionalDJteam" / "Chen Shin" / "Music-Flac"
)
DROPBOX_CREDS_FILE = Path.home() / ".openclaw" / "workspace" / ".dropbox-credentials"

STATE_DIR = Path("/tmp/openclaw-music")

ALLOWED_WEEKDAYS   = {0, 1, 2, 3, 4, 5, 6}
ALLOWED_START_HOUR = 0
ALLOWED_END_HOUR   = 24

ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


# ── Dropbox token ─────────────────────────────────────────────────────────────

def get_dropbox_token() -> str:
    """refresh token → access token"""
    if not DROPBOX_CREDS_FILE.exists():
        return ""
    creds = json.loads(DROPBOX_CREDS_FILE.read_text(encoding="utf-8"))
    refresh_token = creds.get("refresh_token", "")
    app_key       = creds.get("app_key", "")
    app_secret    = creds.get("app_secret", "")
    if not all([refresh_token, app_key, app_secret]):
        return ""
    try:
        data = urlencode({
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     app_key,
            "client_secret": app_secret,
        }).encode()
        req = urllib.request.Request(
            "https://api.dropbox.com/oauth2/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        ctx = ssl._create_unverified_context()
        with urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read()).get("access_token", "")
    except Exception:
        return ""


def dropbox_upload(local_path: Path, dropbox_path: str, token: str) -> bool:
    """Dropbox API 上傳，成功回傳 True。"""
    try:
        data = local_path.read_bytes()
        req = urllib.request.Request(
            "https://content.dropboxapi.com/2/files/upload",
            data=data,
            headers={
                "Authorization":  f"Bearer {token}",
                "Dropbox-API-Arg": json.dumps({
                    "path":       dropbox_path,
                    "mode":       "overwrite",
                    "autorename": True,
                    "mute":       False,
                }),
                "Content-Type": "application/octet-stream",
            },
            method="POST",
        )
        ctx = ssl._create_unverified_context()
        with urlopen(req, context=ctx, timeout=600) as resp:
            result = json.loads(resp.read())
            return bool(result.get("id"))
    except Exception as e:
        print(f"[Dropbox API 上傳失敗: {e}]")
        return False


# ── Subprocess helper ─────────────────────────────────────────────────────────

def run(command: list[str], timeout: int = 900,
        cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ,
           "PATH": f"{VENV_BIN}:/Users/shinba/bin:{os.environ.get('PATH', '')}"}
    return subprocess.run(
        command, text=True, capture_output=True, timeout=timeout,
        cwd=str(cwd) if cwd else None, env=env,
    )


def within_allowed_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ_TPE)
    return now.weekday() in ALLOWED_WEEKDAYS and ALLOWED_START_HOUR <= now.hour < ALLOWED_END_HOUR


# ── Audio helpers ─────────────────────────────────────────────────────────────

def ffprobe_info(path: Path) -> dict[str, Any]:
    result = run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    payload = json.loads(result.stdout or "{}")
    fmt  = payload.get("format", {})
    tags = {str(k).lower(): str(v) for k, v in fmt.get("tags", {}).items()}
    stream = next(
        (s for s in payload.get("streams", []) if s.get("codec_type") == "audio"), {}
    )
    return {
        "title":    tags.get("title") or path.stem,
        "artist":   tags.get("artist") or tags.get("album_artist") or "Unknown Artist",
        "album":    tags.get("album") or "Unknown Album",
        "genre":    tags.get("genre") or tags.get("style") or tags.get("grouping") or "",
        "track":    tags.get("track") or "",
        "date":     tags.get("date") or tags.get("year") or "",
        "duration": float(fmt.get("duration") or 0),
        "codec":    stream.get("codec_name", ""),
        "ext":      path.suffix.lower(),
    }


def sanitize(part: str) -> str:
    normalized = unicodedata.normalize("NFC", str(part or ""))
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", normalized)
    cleaned    = re.sub(r'[<>:"/\\|?*]+', "_", normalized).strip()
    cleaned    = re.sub(r"\s+", " ", cleaned)
    return cleaned[:180] if cleaned else "Unknown"


def clean_output(text: str) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", text or "")
    cleaned = cleaned.replace("\r", "\n")
    lines   = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return "\n".join(lines)


def year_from_date(value: str) -> str:
    match = re.search(r"(19|20)\d{2}", value or "")
    return match.group(0) if match else "Unknown Year"


def normalize_compare_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = re.sub(r"\s+", "", normalized)
    return re.sub(r"[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", "", normalized)


def apple_country_from_url(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}", parts[0], re.IGNORECASE):
        return parts[0].lower()
    return "tw"


def apple_track_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    query  = parse_qs(parsed.query)
    if query.get("i"):
        return query["i"][0]
    digits = [p for p in parsed.path.split("/") if p.isdigit()]
    return digits[-1] if digits else ""


def fetch_apple_lookup(url: str) -> tuple[str, dict[str, Any] | None]:
    track_id = apple_track_id_from_url(url)
    country  = apple_country_from_url(url)
    parsed   = urlparse(url)
    normalized_url = urlunparse(
        parsed._replace(query=urlencode({"i": track_id}) if track_id else "", fragment="")
    )
    if not track_id:
        return normalized_url, None
    lookup_url  = f"https://itunes.apple.com/lookup?id={track_id}&entity=song&country={country}"
    ssl_context = ssl._create_unverified_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode    = ssl.CERT_NONE
    with urlopen(lookup_url, timeout=15, context=ssl_context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    record = next(
        (item for item in payload.get("results", []) if str(item.get("trackId")) == track_id),
        None,
    )
    if not record:
        return normalized_url, None
    track_view_url = record.get("trackViewUrl") or normalized_url
    parsed_view    = urlparse(track_view_url)
    normalized_url = urlunparse(
        parsed_view._replace(query=urlencode({"i": track_id}), fragment="")
    )
    expected = {
        "track_id": track_id,
        "title":    record.get("trackName") or "",
        "artist":   record.get("artistName") or "",
        "album":    record.get("collectionName") or "",
        "genre":    record.get("primaryGenreName") or "",
        "date":     record.get("releaseDate") or "",
        "duration": float(record.get("trackTimeMillis") or 0) / 1000.0,
    }
    return normalized_url, expected


def normalize_platform(platform: str) -> str:
    return {
        "apple music": "AppleMusic",
        "beatport":    "Beatport",
        "tidal":       "Tidal",
    }.get(platform.strip().lower(), sanitize(platform))


def infer_genre(info: dict[str, Any]) -> str:
    genre = sanitize(info.get("genre") or "")
    return genre if genre != "Unknown" else "Auto"


def storage_format_label(info: dict[str, Any], platform: str) -> str:
    codec = str(info.get("codec") or "").lower()
    ext   = str(info.get("ext") or "").lower()
    if codec == "alac" or ext == ".alac":
        return "ALAC"
    if codec == "flac" or ext == ".flac":
        return "FLAC"
    return "FLAC"


def storage_extension(path: Path, format_label: str) -> str:
    if format_label == "ALAC":
        return ".alac"
    if format_label == "M4A":
        return ".m4a"
    return path.suffix.lower()


def retag_audio_file(path: Path, expected: dict[str, Any]) -> None:
    ne = {k: unicodedata.normalize("NFC", str(v or "")) for k, v in expected.items()}
    suffix = path.suffix.lower()
    if suffix in {".m4a", ".mp4", ".alac"}:
        audio = EasyMP4(str(path))
        audio["title"] = [ne["title"]]; audio["artist"] = [ne["artist"]]
        audio["album"] = [ne["album"]]; audio["albumartist"] = [ne["artist"]]
        if ne.get("genre"): audio["genre"] = [ne["genre"]]
        if ne.get("date"):  audio["date"]  = [ne["date"]]
        audio.save(); return
    if suffix == ".flac":
        audio = FLAC(str(path))
        audio["title"] = [ne["title"]]; audio["artist"] = [ne["artist"]]
        audio["album"] = [ne["album"]]; audio["albumartist"] = [ne["artist"]]
        if ne.get("genre"): audio["genre"] = [ne["genre"]]
        if ne.get("date"):  audio["date"]  = [ne["date"]]
        audio.save(); return
    audio = MutagenFile(str(path), easy=True)
    if not audio: return
    audio["title"] = [ne["title"]]; audio["artist"] = [ne["artist"]]
    audio["album"] = [ne["album"]]
    if ne.get("genre"): audio["genre"] = [ne["genre"]]
    if ne.get("date"):  audio["date"]  = [ne["date"]]
    audio.save()


def reconcile_apple_music_files(
    audio_files: list[Path], expected: dict[str, Any]
) -> dict[str, Any]:
    if not expected:
        return {}
    if len(audio_files) != 1:
        raise RuntimeError(
            f"Apple Music 單曲驗證失敗：預期 1 個檔案，實際 {len(audio_files)} 個"
        )
    info = ffprobe_info(audio_files[0])
    codec = str(info.get("codec") or "").lower()
    # 只接受 ALAC（無損）→ 轉 FLAC 供 CDJ-3000 使用
    # AAC 有損拒絕，代表此曲 Apple Music 無 ALAC 版本，請改用 Beatport/Tidal
    if codec != "alac":
        raise RuntimeError(
            f"此曲 Apple Music 僅提供 {codec.upper()}（有損），無法用於 CDJ-3000。請改用 Beatport 或 Tidal 下載 FLAC。"
        )
    expected_duration = float(expected.get("duration") or 0)
    actual_duration   = float(info.get("duration") or 0)
    duration_delta    = abs(actual_duration - expected_duration)
    if expected_duration and duration_delta > 3:
        raise RuntimeError(
            f"Apple Music 驗證失敗：時長不符"
            f"（expected={expected_duration:.3f}s actual={actual_duration:.3f}s）"
        )
    before = {k: info.get(k, "") for k in ("title", "artist", "album")}
    changed = any(
        normalize_compare_text(before[k]) != normalize_compare_text(expected[k])
        for k in ("title", "artist", "album") if expected.get(k)
    )
    if changed:
        retag_audio_file(audio_files[0], expected)
    return {
        "track_id":           expected.get("track_id"),
        "duration_delta":     round(duration_delta, 3),
        "metadata_corrected": changed,
        "before":             before,
        "after": {k: expected.get(k, "") for k in ("title", "artist", "album")},
    }


def ensure_storage_file(path: Path, info: dict[str, Any],
                        platform: str) -> tuple[Path, str]:
    label = storage_format_label(info, platform)
    # ALAC → 轉 FLAC
    if label == "ALAC":
        output = path.with_suffix(".flac")
        result = run(["ffmpeg", "-y", "-i", str(path), "-c:a", "flac", str(output)], timeout=1800)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "ffmpeg ALAC→FLAC convert failed")
        path.unlink(missing_ok=True)
        return output, "FLAC"
    if label == "M4A":
        return path, label
    if path.suffix.lower() == ".flac":
        return path, "FLAC"
    output = path.with_suffix(".flac")
    result = run(["ffmpeg", "-y", "-i", str(path), "-c:a", "flac", str(output)], timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg convert failed")
    path.unlink(missing_ok=True)
    return output, "FLAC"


# ── organize_file（唯一上傳入口）─────────────────────────────────────────────

def organize_file(path: Path, platform: str, flac_upgrade: bool = False) -> Path:
    info = ffprobe_info(path)
    stored_path, format_label = ensure_storage_file(path, info, platform)
    info = ffprobe_info(stored_path)

    year          = sanitize(year_from_date(info["date"]))
    genre         = infer_genre(info)
    artist        = sanitize(info["artist"])
    album         = sanitize(info["album"])
    title         = sanitize(info["title"])
    platform_name = normalize_platform(platform)
    filename      = f"{year}_{artist}_{album}_{title}{storage_extension(stored_path, format_label)}"

    # FLAC 升級模式：有損丟掉，非 FLAC 無損轉換
    if flac_upgrade:
        lossy_exts    = {".mp3", ".aac", ".ogg", ".wma"}
        lossless_exts = {".flac", ".alac", ".wav", ".aiff", ".aif", ".m4a"}
        ext = stored_path.suffix.lower()
        if ext in lossy_exts:
            stored_path.unlink(missing_ok=True)
            return Path("/dev/null")
        if format_label != "FLAC" and ext in lossless_exts:
            flac_path = stored_path.with_suffix(".flac")
            conv = subprocess.run(
                ["ffmpeg", "-y", "-i", str(stored_path), "-c:a", "flac", str(flac_path)],
                capture_output=True, text=True, timeout=300,
            )
            if conv.returncode == 0 and flac_path.exists():
                stored_path.unlink(missing_ok=True)
                stored_path   = flac_path
                format_label  = "FLAC"
                filename      = f"{year}_{artist}_{album}_{title}.flac"

    # ── Dropbox API 上傳（所有下載都走這條，包含一般下載和 FLAC 升級）──
    dropbox_path = f"{DROPBOX_FLAC_DIR}/{platform_name}/{genre}/{artist}/{filename}"
    token = get_dropbox_token()
    if token:
        if dropbox_upload(stored_path, dropbox_path, token):
            stored_path.unlink(missing_ok=True)
            return Path(dropbox_path)

    # fallback：本機 Dropbox 同步資料夾
    local_target_dir = LOCAL_DROPBOX_DIR / platform_name / genre / artist
    local_target_dir.mkdir(parents=True, exist_ok=True)
    local_target = local_target_dir / filename
    shutil.move(str(stored_path), str(local_target))
    return local_target


# ── list_recent_files ─────────────────────────────────────────────────────────

def list_recent_files(root: Path, started_at: float) -> list[Path]:
    if not root.exists():
        return []
    files = [
        p for p in root.rglob("*")
        if p.is_file()
        and p.stat().st_mtime >= started_at - 2
        and not p.name.startswith(".")
        and not p.name.startswith("._")
        and "@eaDir" not in p.parts
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


# ── Provider detection ────────────────────────────────────────────────────────

def detect_provider(query: str) -> str:
    q = query.lower().strip()
    if "beatport.com" in q:  return "beatport"
    if "tidal.com"    in q:  return "tidal"
    if "music.apple.com" in q: return "apple_music"
    return "search"


def normalize_tidal_resource(value: str) -> str:
    raw   = value.strip()
    match = re.search(r"(track|album|playlist|video|artist)/(\d+)", raw)
    return f"{match.group(1)}/{match.group(2)}" if match else raw


# ── Downloaders ───────────────────────────────────────────────────────────────

def download_tidal(query: str, temp_dir: Path) -> subprocess.CompletedProcess[str]:
    if query.startswith("http"):
        resource = normalize_tidal_resource(query)
        return run(
            [str(VENV_BIN / "tiddl"), "url", resource, "download",
             "--path", str(temp_dir), "--quality", "master"],
            timeout=1800,
        )
    return run(
        [str(VENV_BIN / "tiddl"), "search", query, "download",
         "--path", str(temp_dir), "--quality", "master"],
        timeout=1800,
    )


def download_beatport(url: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ,
           "PATH": f"{VENV_BIN}:/Users/shinba/bin:{os.environ.get('PATH', '')}"}
    return subprocess.run(
        [str(BEATPORTDL), url],
        input=url + "\n\n",          # 非互動式 stdin pipe
        capture_output=True,
        text=True,
        timeout=1800,
        cwd=str(BEATPORTDL.parent),
        env=env,
    )


def download_apple_music(url: str, temp_dir: Path) -> subprocess.CompletedProcess[str]:
    temp_dir.mkdir(parents=True, exist_ok=True)
    return run(
        [str(VENV_BIN / "gamdl"), url,
         "-o", str(temp_dir),
         "--temp-path", str(temp_dir),
         "--overwrite"],
        timeout=2400,
    )


# ── Main download logic ───────────────────────────────────────────────────────

def download_query(query: str) -> dict[str, Any]:
    provider   = detect_provider(query)
    started_at = time.time()
    temp_dir   = Path(tempfile.mkdtemp(prefix="music-download-", dir=str(STATE_DIR)))
    result_payload: dict[str, Any] = {
        "query":      query,
        "started_at": datetime.now(TZ_TPE).isoformat(),
        "provider":   provider,
        "success":    False,
        "files":      [],
    }
    try:
        if provider == "beatport":
            result       = download_beatport(query)
            recent_files = list_recent_files(BEATPORT_INBOX, started_at)
            platform     = "Beatport"
        elif provider in {"tidal", "search"}:
            result       = download_tidal(query, temp_dir)
            recent_files = [p for p in temp_dir.rglob("*")
                            if p.is_file() and not p.name.startswith(".")]
            platform     = "Tidal"
        elif provider == "apple_music":
            normalized_query, apple_expected = fetch_apple_lookup(query)
            result_payload["query_normalized"] = normalized_query
            if apple_expected:
                result_payload["apple_expected"] = {
                    k: apple_expected.get(k)
                    for k in ("track_id", "title", "artist", "album")
                }
            result       = download_apple_music(normalized_query, temp_dir)
            recent_files = [p for p in temp_dir.rglob("*")
                            if p.is_file() and not p.name.startswith(".")]
            platform     = "Apple Music"
        else:
            raise RuntimeError("unsupported provider")

        audio_files = [
            p for p in recent_files
            if p.suffix.lower() in {".flac", ".wav", ".aiff", ".aif", ".alac", ".m4a"}
            and not p.name.startswith(".")
            and not p.name.startswith("._")
        ]

        result_payload["exit_code"]   = result.returncode
        result_payload["output_tail"] = clean_output(
            (result.stdout or "") + "\n" + (result.stderr or "")
        )[-1200:]

        if result.returncode not in (0, 1) and not audio_files:
            raise RuntimeError(result_payload["output_tail"] or "download failed")
        if not audio_files:
            raise RuntimeError(result_payload["output_tail"] or "沒有找到可整理的 FLAC/無損檔案")

        if provider == "apple_music":
            result_payload["apple_validation"] = reconcile_apple_music_files(
                audio_files, apple_expected
            )

        flac_mode = os.environ.get("FLAC_UPGRADE_MODE", "0") == "1"
        organized = [organize_file(p, platform, flac_upgrade=flac_mode) for p in audio_files]
        result_payload["files"]   = [str(p) for p in organized]
        result_payload["success"] = True
        return result_payload
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument("--result-path",
                        default=str(STATE_DIR / "last_download_result.json"))
    parser.add_argument("--manual-request", action="store_true")
    parser.add_argument("--flac-upgrade",   action="store_true")
    args  = parser.parse_args()
    query = " ".join(args.query).strip()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if args.flac_upgrade:
        os.environ["FLAC_UPGRADE_MODE"] = "1"

    if not args.manual_request and not within_allowed_window():
        payload = {
            "query":      query,
            "started_at": datetime.now(TZ_TPE).isoformat(),
            "success":    False,
            "error":      "FLAC 更新只允許週一至週五 10:00-19:59 執行",
        }
        Path(args.result_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 1

    try:
        payload = download_query(query)
        if args.manual_request:
            payload["manual_request"] = True
        Path(args.result_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        payload = {
            "query":      query,
            "started_at": datetime.now(TZ_TPE).isoformat(),
            "success":    False,
            "error":      str(exc),
        }
        if args.manual_request:
            payload["manual_request"] = True
        Path(args.result_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
