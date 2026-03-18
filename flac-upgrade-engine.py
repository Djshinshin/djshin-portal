#!/usr/bin/env python3
"""
flac-upgrade-engine.py — DJ Shin FLAC 升級引擎（MBP 執行）

用法:
    OPENCLAW_MUSIC_SCAN_DIR='/Volumes/Shin-Music/放歌專用' python3 flac-upgrade-engine.py [--dry-run]

邏輯:
    .flac                        → 跳過
    .alac / .wav / .aiff / .aif  → ffmpeg 轉 FLAC → 複製一份上傳 Dropbox（保留原檔）
    .m4a (ALAC codec)            → 同上
    有損 (.mp3 / .m4a-AAC / .aac / .ogg 等)
        → Portal /music/api/versions 搜尋
        → 依序試: Beatport URL → Tidal URL → Apple Music URL
        → 呼叫 download_music_to_nas.py 下載並上傳 Dropbox
        → 全部失敗 → 跳過（保留原有損檔）
        → 成功 → Dropbox 已由 download_music_to_nas.py 處理（保留原有損檔）
    --dry-run → 只掃描列印，不執行任何動作
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import unicodedata
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import urlopen

TZ_TPE = timezone(timedelta(hours=8))

# ── 路徑 ──────────────────────────────────────────────────────────────────────
WORKSPACE         = Path.home() / ".openclaw" / "workspace"
MUSIC_TOOLS       = WORKSPACE / "music-tools"
VENV_BIN          = WORKSPACE / "music-tools-venv" / "bin"
VENV_PYTHON       = VENV_BIN / "python3"
DOWNLOAD_SCRIPT   = MUSIC_TOOLS / "download_music_to_nas.py"
DROPBOX_CREDS     = WORKSPACE / ".dropbox-credentials"
DROPBOX_FLAC_DIR  = "/Music-Flac"
STATE_DIR         = Path("/tmp/openclaw-music")

PORTAL_BASE  = "http://100.78.202.52:8880"  # Tailscale 直連，繞過 Cloudflare
PORTAL_PASS  = "shin2026"

# ffmpeg/ffprobe
def _find_bin(name: str) -> str:
    for p in [f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}",
              shutil.which(name) or ""]:
        if p and Path(p).exists():
            return p
    raise RuntimeError(f"找不到 {name}")

FFMPEG  = _find_bin("ffmpeg")
FFPROBE = _find_bin("ffprobe")

LOSSLESS_EXTS = {".flac", ".alac", ".wav", ".aiff", ".aif", ".m4a"}
LOSSY_EXTS    = {".mp3", ".aac", ".ogg", ".wma", ".opus"}
AUDIO_EXTS    = LOSSLESS_EXTS | LOSSY_EXTS


# ── Portal API ────────────────────────────────────────────────────────────────

_portal_cookie: str = ""

def portal_login() -> str:
    global _portal_cookie
    ctx = ssl._create_unverified_context()
    data = json.dumps({"password": PORTAL_PASS}).encode()
    req = urllib.request.Request(
        f"{PORTAL_BASE}/auth/login",
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    try:
        resp = urlopen(req, context=ctx, timeout=15)
        # 拿 Set-Cookie（相容 urllib / Cloudflare）
        info = resp.info()
        # get_all 返回所有 Set-Cookie 標頭（list）
        cookies = info.get_all("Set-Cookie") or []
        for cookie_hdr in cookies:
            for part in cookie_hdr.split(";"):
                part = part.strip()
                if part.startswith("session="):
                    _portal_cookie = part
                    return _portal_cookie
        # fallback: 直接用單一 get
        cookie_hdr = info.get("Set-Cookie", "")
        for part in cookie_hdr.split(";"):
            part = part.strip()
            if part.startswith("session="):
                _portal_cookie = part
                return _portal_cookie
        print(f"[Portal login 失敗: Set-Cookie header 找不到 session=]", flush=True)
    except Exception as e:
        print(f"[Portal login 失敗: {e}]", flush=True)
    return ""


def portal_versions(query: str) -> list[dict]:
    """呼叫 Portal /music/api/versions，回傳 items 清單。"""
    global _portal_cookie
    ctx = ssl._create_unverified_context()
    encoded = quote(query, safe="")

    for attempt in range(2):
        if not _portal_cookie:
            portal_login()
        if not _portal_cookie:
            print(f"  [Portal login 失敗，跳過]", flush=True)
            return []
        req = urllib.request.Request(
            f"{PORTAL_BASE}/music/api/versions?q={encoded}",
            headers={
                "Cookie": _portal_cookie,
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            },
        )
        try:
            with urlopen(req, context=ctx, timeout=60) as resp:
                payload = json.loads(resp.read())
                return payload.get("items", [])
        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt == 0:
                # session 過期，重新登入後再試
                _portal_cookie = ""
                portal_login()
                continue
            print(f"  [Portal versions 失敗: {e}]", flush=True)
            return []
        except Exception as e:
            print(f"  [Portal versions 失敗: {e}]", flush=True)
            return []
    return []


TIDAL_SEARCH_TOKEN = "__TIDAL_SEARCH__"  # Tidal URL 空時用 query 搜尋下載


def pick_urls_by_priority(items: list[dict], query: str = "") -> list[tuple[str, str]]:
    """
    依 Beatport → Tidal → Apple Music 順序取 URL。
    回傳 [(url, platform_name), ...]。
    Tidal URL 空時改用 TIDAL_SEARCH_TOKEN 標記，main loop 改用 query 字串下載。
    """
    order = ["beatport", "tidal", "apple_music"]
    by_provider: dict[str, str] = {}
    for item in items:
        provider = item.get("provider", "").lower()
        url = item.get("url", "")
        if provider not in order or provider in by_provider:
            continue
        if url:
            by_provider[provider] = url
        elif provider == "tidal" and item.get("label") and query:
            # Tidal 有結果但 url 空 → 用 query 字串直接搜尋下載
            by_provider[provider] = TIDAL_SEARCH_TOKEN
    result = []
    for p in order:
        if p in by_provider:
            label = {"beatport": "Beatport", "tidal": "Tidal",
                     "apple_music": "Apple Music"}.get(p, p)
            result.append((by_provider[p], label))
    return result


# ── Dropbox ───────────────────────────────────────────────────────────────────

def get_dropbox_token() -> str:
    if not DROPBOX_CREDS.exists():
        return ""
    creds = json.loads(DROPBOX_CREDS.read_text(encoding="utf-8"))
    r = creds.get("refresh_token", "")
    k = creds.get("app_key", "")
    s = creds.get("app_secret", "")
    if not all([r, k, s]):
        return ""
    try:
        data = urlencode({"grant_type": "refresh_token", "refresh_token": r,
                          "client_id": k, "client_secret": s}).encode()
        req = urllib.request.Request(
            "https://api.dropbox.com/oauth2/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        ctx = ssl._create_unverified_context()
        with urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read()).get("access_token", "")
    except Exception as e:
        print(f"[Dropbox token 失敗: {e}]", flush=True)
        return ""


def dropbox_upload(local_path: Path, dropbox_path: str, token: str) -> bool:
    try:
        data = local_path.read_bytes()
        req = urllib.request.Request(
            "https://content.dropboxapi.com/2/files/upload", data=data,
            headers={
                "Authorization":   f"Bearer {token}",
                "Dropbox-API-Arg": json.dumps({"path": dropbox_path, "mode": "overwrite",
                                               "autorename": True, "mute": False}),
                "Content-Type":    "application/octet-stream",
            }, method="POST")
        ctx = ssl._create_unverified_context()
        with urlopen(req, context=ctx, timeout=600) as resp:
            return bool(json.loads(resp.read()).get("id"))
    except Exception as e:
        print(f"  [Dropbox 上傳失敗: {e}]", flush=True)
        return False


# ── Audio helpers ─────────────────────────────────────────────────────────────

def sanitize(part: str) -> str:
    s = unicodedata.normalize("NFC", str(part or ""))
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s)
    s = re.sub(r'[<>:"/\\|?*]+', "_", s).strip()
    return re.sub(r"\s+", " ", s)[:180] or "Unknown"


# 無損 codec
LOSSLESS_CODECS = {
    "flac", "alac",
    "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be",
    "pcm_s32le", "pcm_s32be", "pcm_f32le", "pcm_f32be",
    "pcm_f64le", "pcm_f64be", "pcm_s16le_planar",
}
# 有損 codec
LOSSY_CODECS = {
    "aac", "mp3", "vorbis", "opus", "wma",
    "wmav1", "wmav2", "wmapro",
    "he_aac", "aac_latm", "ac3", "eac3", "mp2", "mp1",
}
MIN_BIT_DEPTH = 16


def ffprobe_audio(path: Path) -> dict:
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return {}
        payload = json.loads(r.stdout or "{}")
        fmt    = payload.get("format", {})
        tags   = {str(k).lower(): str(v) for k, v in fmt.get("tags", {}).items()}
        stream = next((s for s in payload.get("streams", [])
                       if s.get("codec_type") == "audio"), {})
        # bit depth: bits_per_raw_sample 優先，fallback bits_per_sample
        bits_raw    = int(stream.get("bits_per_raw_sample") or 0)
        bits_sample = int(stream.get("bits_per_sample") or 0)
        bit_depth   = bits_raw or bits_sample or 0
        bitrate = int(stream.get("bit_rate") or fmt.get("bit_rate") or 0)
        return {
            "tags":        tags,
            "codec":       stream.get("codec_name", "").lower(),
            "bit_depth":   bit_depth,
            "bitrate":     bitrate,
            "sample_rate": int(stream.get("sample_rate") or 0),
        }
    except Exception:
        return {}


def classify_file(path: Path) -> str:
    """
    回傳: 'flac' / 'lossless' / 'lossy' / 'unknown'
    判定通則:
      codec 在 LOSSY_CODECS                → lossy
      codec 在 LOSSLESS_CODECS 且 bit_depth >= 16  → lossless（flac 特判）
      codec 在 LOSSLESS_CODECS 且 bit_depth < 16   → lossy（失真品質）
      codec 不在已知集合 → 靠 bit_depth 判斷，fallback 副檔名
    """
    if not path.exists() or path.stat().st_size < 4096:
        return "unknown"
    ext  = path.suffix.lower()
    info = ffprobe_audio(path)
    if not info:
        return "unknown"
    codec     = info.get("codec", "")
    bit_depth = info.get("bit_depth", 0)

    if codec == "flac":
        return "flac"
    if codec in LOSSY_CODECS:
        return "lossy"
    if codec in LOSSLESS_CODECS:
        if bit_depth > 0 and bit_depth < MIN_BIT_DEPTH:
            return "lossy"
        return "lossless"
    # codec 不在已知集合，靠 bit_depth
    if bit_depth >= MIN_BIT_DEPTH:
        return "lossless"
    if 0 < bit_depth < MIN_BIT_DEPTH:
        return "lossy"
    # bit_depth 讀不到，fallback 副檔名
    if ext == ".flac":
        return "flac"
    if ext in {".alac", ".wav", ".aiff", ".aif"}:
        return "lossless"
    if ext in LOSSY_EXTS or ext in {".m4a", ".aac"}:
        return "lossy"
    return "unknown"


# DJ remix/edit tag 過濾樣式（不影響主標題）
_STRIP_TAGS_RE = re.compile(
    r"\s*[\(\[]\s*"
    r"(?:XMiX\s*(?:Edit|Short\s*Cut|Long\s*Version|Edit\s*Clean)?|XMIXR)"
    r"\s*[\)\]]"
    r"|\s+XMIXR\b"                              # 空白前置的 XMIXR
    r"|\s*[\(\[]\s*XMiX\s*(?:Edit|Short\s*Cut|Long\s*Version|Edit\s*Clean|Edit\s*Clean)?\s*[\)\]]"
    r"|\s+XMiX\s+(?:Edit|Short\s*Cut|Long\s*Version|Edit\s*Clean)\b"  # 空白前置
    r"|(\s*[\(\[]\s*Radio\s*Edit\s*[\)\]])"
    r"|(\s*[\(\[]\s*(?:Clean|Dirty)\s*[\)\]])",
    re.IGNORECASE,
)


def build_search_query(path: Path, tags: dict) -> str:
    artist = tags.get("artist") or tags.get("album_artist") or ""
    title  = tags.get("title") or ""
    if not (artist and title):
        stem = re.sub(r"^\d+[\s._\-]+", "", path.stem)
        return stem
    # 過濾 XMIXR / XMiX Edit / Radio Edit 等 DJ 標記
    clean_title = _STRIP_TAGS_RE.sub("", title).strip()
    # 過濾後沒有 mix 字樣，加 Extended Mix
    if clean_title and not re.search(
        r"(extended|original|club|instrumental|dub|remix)\s*(mix)?\b",
        clean_title, re.IGNORECASE
    ):
        clean_title = f"{clean_title} Extended Mix"
    return f"{artist} {clean_title}".strip()


def build_dropbox_path(tags: dict, src_name: str, platform: str) -> str:
    def year_from(v: str) -> str:
        m = re.search(r"(19|20)\d{2}", v or "")
        return m.group(0) if m else "Unknown"

    artist   = sanitize(tags.get("artist") or tags.get("album_artist") or "Unknown Artist")
    album    = sanitize(tags.get("album") or "Unknown Album")
    title    = sanitize(tags.get("title") or Path(src_name).stem)
    genre    = sanitize(tags.get("genre") or tags.get("style") or "Auto")
    year     = sanitize(year_from(tags.get("date") or tags.get("year") or ""))
    filename = f"{year}_{artist}_{album}_{title}.flac"
    return f"{DROPBOX_FLAC_DIR}/{sanitize(platform)}/{genre}/{artist}/{filename}"


def convert_to_flac_copy(src: Path) -> Path | None:
    """無損非 FLAC → ffmpeg 轉換，回傳 tmp .flac 路徑（原檔不動）。"""
    fd, tmp = tempfile.mkstemp(suffix=".flac", dir="/tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    r = subprocess.run(
        [FFMPEG, "-y", "-i", str(src), "-c:a", "flac", str(tmp_path)],
        capture_output=True, text=True, timeout=1800)
    if r.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 4096:
        return tmp_path
    tmp_path.unlink(missing_ok=True)
    return None


# ── Downloader via Portal /api/download (iMac 執行) ──────────────────────────

def run_download_script(url: str) -> dict:
    """
    透過 Portal /api/download 在 iMac 上執行下載。
    成功時 payload['success'] == True。
    """
    global _portal_cookie
    ctx = ssl._create_unverified_context()
    body = json.dumps({"url": url}).encode()

    for attempt in range(2):
        if not _portal_cookie:
            portal_login()
        if not _portal_cookie:
            return {"success": False, "error": "Portal login 失敗"}
        req = urllib.request.Request(
            f"{PORTAL_BASE}/music/api/download",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Cookie":       _portal_cookie,
                "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            },
            method="POST",
        )
        try:
            with urlopen(req, context=ctx, timeout=1850) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt == 0:
                _portal_cookie = ""
                portal_login()
                continue
            return {"success": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    return {"success": False, "error": "Portal download 失敗"}


# ── Scanner ───────────────────────────────────────────────────────────────────

def scan_files(root: Path) -> list[Path]:
    files = [
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in AUDIO_EXTS
        and not p.name.startswith(".")
        and not p.name.startswith("._")
        and "@eaDir" not in p.parts
        and "RECYCLE" not in str(p).upper()
    ]
    files.sort(key=lambda p: str(p).lower())
    return files


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    scan_dir = Path(os.environ.get("OPENCLAW_MUSIC_SCAN_DIR", "/Volumes/Shin-Music/放歌專用"))
    ts = datetime.now(TZ_TPE).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] FLAC 升級引擎 {'（Dry Run）' if args.dry_run else ''}", flush=True)
    print(f"掃描目錄: {scan_dir}", flush=True)

    if not scan_dir.exists():
        print(f"[ERROR] 目錄不存在: {scan_dir}", flush=True)
        return 1

    files = scan_files(scan_dir)
    total = len(files)
    print(f"找到 {total} 個音頻檔案", flush=True)
    if not total:
        print("無可處理檔案。", flush=True)
        return 0

    # 無損轉換用的 Dropbox token
    token = "" if args.dry_run else get_dropbox_token()
    if not args.dry_run and not token:
        print("[WARNING] Dropbox token 取得失敗，無損轉換上傳可能失敗", flush=True)

    # Portal 登入（有損搜尋用）
    if not args.dry_run:
        portal_login()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    stats = {
        "flac_skip":          0,
        "lossless_converted": 0,
        "lossy_upgraded":     0,
        "lossy_not_found":    0,
        "failed":             0,
    }

    for idx, src in enumerate(files, 1):
        rel  = src.relative_to(scan_dir)
        pf   = f"[{idx}/{total}]"
        info = ffprobe_audio(src)
        tags = info.get("tags", {})
        kind = classify_file(src)  # 'flac' / 'lossless' / 'lossy' / 'unknown'
        codec     = info.get("codec", "?")
        bit_depth = info.get("bit_depth", 0)
        bitrate   = info.get("bitrate", 0)
        bd_str    = f"{bit_depth}bit" if bit_depth else "?bit"
        br_str    = f"{bitrate//1000}kbps" if bitrate else ""

        # ── 已是 FLAC → 跳過 ─────────────────────────────────────────────
        if kind == "flac":
            print(f"{pf} SKIP(FLAC {bd_str}) {rel}", flush=True)
            stats["flac_skip"] += 1
            continue

        # ── ffprobe 失敗 → 跳過 ──────────────────────────────────────────
        if kind == "unknown":
            print(f"{pf} SKIP(unknown codec) {rel}", flush=True)
            continue

        # ── 無損非 FLAC → 轉換後複製上傳（保留原檔）─────────────────────
        if kind == "lossless":
            dropbox_path = build_dropbox_path(tags, src.name, "AppleMusic")
            if args.dry_run:
                print(f"{pf} DRY 無損轉換 [{codec} {bd_str}] {rel} → {dropbox_path}", flush=True)
                stats["lossless_converted"] += 1
                continue
            print(f"{pf} 無損轉換 [{codec} {bd_str}]: {rel}", flush=True)
            tmp_flac = convert_to_flac_copy(src)
            if not tmp_flac:
                print(f"{pf} [FAIL] 轉換失敗: {rel}", flush=True)
                stats["failed"] += 1
                continue
            try:
                ok = dropbox_upload(tmp_flac, dropbox_path, token)
                if ok:
                    print(f"{pf} OK(轉換+上傳) → {dropbox_path}", flush=True)
                    stats["lossless_converted"] += 1
                    # 原檔保留
                else:
                    print(f"{pf} [FAIL] 上傳失敗: {rel}", flush=True)
                    stats["failed"] += 1
            finally:
                tmp_flac.unlink(missing_ok=True)
            continue

        # ── 有損 → Portal 搜尋 → Beatport/Tidal/Apple Music ──────────────
        query = build_search_query(src, tags)
        if args.dry_run:
            print(f"{pf} DRY 有損→搜尋 [{codec} {bd_str} {br_str}] query={query!r} {rel}", flush=True)
            stats["lossy_upgraded"] += 1
            continue

        print(f"{pf} 有損 [{codec} {bd_str} {br_str}] 搜尋: {query!r}", flush=True)
        items = portal_versions(query)
        if not items:
            print(f"{pf} 搜尋無結果，跳過: {rel}", flush=True)
            stats["lossy_not_found"] += 1
            continue

        url_list = pick_urls_by_priority(items, query)
        if not url_list:
            print(f"{pf} 無可用 URL，跳過: {rel}", flush=True)
            stats["lossy_not_found"] += 1
            continue

        succeeded = False
        for url, platform in url_list:
            # Tidal URL 空時用 query 字串搜尋下載
            actual_url = query if url == TIDAL_SEARCH_TOKEN else url
            display_url = f"[tidal search] {query!r}" if url == TIDAL_SEARCH_TOKEN else url
            print(f"  → 嘗試 {platform}: {display_url}", flush=True)
            result = run_download_script(actual_url)
            if result.get("success"):
                files_out = result.get("files", [])
                print(f"  OK({platform}) → {files_out}", flush=True)
                stats["lossy_upgraded"] += 1
                # 原有損檔保留（用戶要求）
                succeeded = True
                break
            else:
                err = result.get("error", "")
                print(f"  [FAIL] {platform}: {err[:120]}", flush=True)

        if not succeeded:
            print(f"{pf} 全部平台失敗，跳過: {rel}", flush=True)
            stats["lossy_not_found"] += 1

    # ── 結果摘要 ─────────────────────────────────────────────────────────────
    print("", flush=True)
    print("═" * 55, flush=True)
    if args.dry_run:
        print(f"Dry Run 完成："
              f"FLAC跳過 {stats['flac_skip']}，"
              f"無損待轉 {stats['lossless_converted']}，"
              f"有損待搜 {stats['lossy_upgraded']}", flush=True)
    else:
        print(f"升級完成："
              f"無損轉換 {stats['lossless_converted']}，"
              f"有損升級 {stats['lossy_upgraded']}，"
              f"找不到 {stats['lossy_not_found']}，"
              f"失敗 {stats['failed']}，"
              f"FLAC跳過 {stats['flac_skip']}", flush=True)
    print("═" * 55, flush=True)
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
