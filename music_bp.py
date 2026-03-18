#!/usr/bin/env python3
"""
music_bp.py — DJ Shin Music Blueprint
掛載到 portal/app.py，提供 /music/* 路由
所有長時間操作透過 SSE 即時串流輸出
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Generator

from flask import Blueprint, Response, jsonify, render_template, request, session, stream_with_context

music_bp = Blueprint("music", __name__, url_prefix="/music")

# ── 路徑 ────────────────────────────────────────────────────────────────────
WORKSPACE       = Path.home() / ".openclaw" / "workspace"
MUSIC_TOOLS     = WORKSPACE / "music-tools"
CRON_SCRIPTS    = WORKSPACE / "cron-scripts"
SCRIPTS         = WORKSPACE / "scripts"

DOWNLOAD_SCRIPT = MUSIC_TOOLS / "download_music_to_nas.py"
VERSIONS_SCRIPT = MUSIC_TOOLS / "search_music_versions.py"
ANALYZE_SCRIPT  = MUSIC_TOOLS / "analyze_music_with_mik.py"

# venv python（裝有 mutagen/gamdl/tiddl/yt-dlp 等依賴）
MUSIC_VENV_PYTHON = str(WORKSPACE / "music-tools-venv" / "bin" / "python3")
MANAGER_SCRIPT  = CRON_SCRIPTS / "openclaw_music_library_manager.sh"
FLAC_ENGINE     = SCRIPTS / "flac-upgrade-engine.py"

SHIN_MUSIC_DIR  = "/Volumes/Shin-Music/放歌專用"
STATE_DIR       = Path("/tmp/openclaw-music")
STATE_FILE      = STATE_DIR / "web_state.json"

# ── Lexicon DB 快取（iMac 本地）───────────────────────────────────────────
LEXICON_CACHE_DIR  = WORKSPACE / "portal" / "cache"
LEXICON_CACHE_DB   = LEXICON_CACHE_DIR / "lexicon_main.db"
LEXICON_SYNC_TIME  = LEXICON_CACHE_DIR / "lexicon_sync_time.txt"
MBP_LEXICON_DB     = "/Users/shinchen/Library/Application Support/Lexicon/main.db"

def get_lexicon_sync_time() -> str:
    """讀取快取 DB 的同步時間標記，用於 UI 顯示。"""
    try:
        if LEXICON_SYNC_TIME.exists():
            return LEXICON_SYNC_TIME.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return "未知"

# ── MBP SSH (Tailscale) ──────────────────────────────────────────────────────
MBP_HOST        = "100.81.94.11"
MBP_USER        = "shinchen"
MBP_WORKSPACE   = "/Users/shinchen/.openclaw/workspace"
MBP_MANAGER     = f"{MBP_WORKSPACE}/cron-scripts/openclaw_music_library_manager.sh"
MBP_FLAC_ENGINE = f"{MBP_WORKSPACE}/scripts/flac-upgrade-engine.py"

def ssh_cmd(remote_cmd: str) -> list[str]:
    """包裝成 SSH 指令，透過 Tailscale 連到 MBP 執行。"""
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"{MBP_USER}@{MBP_HOST}",
        remote_cmd,
    ]

MANAGER_TIMEOUTS = {
    "status": 20, "run-scan": 300, "run-health": 1200,
    "run-compare": 1800, "run-queue": 600, "run-dispatch": 1800, "logs": 20,
}

# ── 狀態 ─────────────────────────────────────────────────────────────────────
def load_state() -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"genre": "", "last_versions": [], "last_query": ""}

def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ── Auth guard ────────────────────────────────────────────────────────────────
def music_auth_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("portal_auth"):
            return jsonify({"ok": False, "error": "未登入"}), 401
        return fn(*args, **kwargs)
    return wrapper

# ── SSE 串流執行器 ─────────────────────────────────────────────────────────────
def run_stream(cmd: list[str], timeout: int = 3600,
               env_extra: dict | None = None) -> Generator[str, None, None]:
    """執行 subprocess，逐行 yield SSE 格式字串。"""
    env = {**os.environ, **(env_extra or {})}
    yield _sse("start", {"ts": time.strftime("%H:%M:%S")})
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert proc.stdout
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                yield _sse("line", {"text": stripped})
        proc.wait(timeout=timeout)
        code = proc.returncode
        yield _sse("done", {"code": code, "ok": code == 0})
    except subprocess.TimeoutExpired:
        yield _sse("error", {"text": "執行逾時"})
    except FileNotFoundError as exc:
        yield _sse("error", {"text": f"找不到指令: {exc}"})
    except Exception as exc:
        yield _sse("error", {"text": str(exc)})

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

# ── Routes ────────────────────────────────────────────────────────────────────

@music_bp.route("/")
@music_auth_required
def music_index():
    state = load_state()
    return render_template("music.html", state=state)


@music_bp.route("/api/state")
@music_auth_required
def api_state():
    return jsonify(load_state())


@music_bp.route("/api/genre", methods=["POST"])
@music_auth_required
def api_set_genre():
    data = request.get_json() or {}
    genre = data.get("genre", "").strip()
    state = load_state()
    state["genre"] = genre
    save_state(state)
    return jsonify({"ok": True, "genre": genre})


# ── 下載 ──────────────────────────────────────────────────────────────────────
import re as _re
_ANSI_RE = _re.compile(r'\x1b\[[0-9;]*[mGKHF]|\r')
_PROG_RE = _re.compile(r'(\d+)\s*%.*?([\d.]+\s*(?:MB|KB|GB)/s).*?(\d+:\d+)', _re.IGNORECASE)

def _parse_progress(raw: str) -> str | None:
    """從 beatportdl 進度行萃取簡潔資訊，回傳 None 表示非進度行。"""
    clean = _ANSI_RE.sub('', raw).strip()
    # beatportdl 進度格式：⣷ Title [FLAC]  [===>---] 50% | 4.5 MB/s | 00:03
    m = _PROG_RE.search(clean)
    if m:
        pct, speed, eta = m.group(1), m.group(2), m.group(3)
        # 取曲目名稱（第一個 [ 之前）
        title_part = clean.split('[')[0].strip()
        # 移除 spinner 字元
        title_part = _re.sub(r'^[\u2800-\u28ff\u25a0-\u25ff\s]+', '', title_part).strip()
        return f"{title_part}  {pct}%  {speed}  ETA {eta}"
    # 完成行
    if clean.startswith('\u2713') or '✓' in clean:
        title = clean.replace('✓', '').strip()
        return f"✓ {title}"
    return None

@music_bp.route("/stream/download")
@music_auth_required
def stream_download():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "缺少 query"}), 400

    def generate():
        cmd = [MUSIC_VENV_PYTHON, str(DOWNLOAD_SCRIPT), "--manual-request"] + query.split()
        env = {**os.environ}
        yield _sse("start", {"ts": time.strftime("%H:%M:%S")})
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            assert proc.stdout
            last_progress = ""
            result_json = None
            for line in proc.stdout:
                raw = line.rstrip()
                if not raw:
                    continue
                # 嘗試解析為最終 JSON
                if raw.startswith('{') and '"success"' in raw:
                    try:
                        result_json = json.loads(raw)
                        continue
                    except Exception:
                        pass
                # 過濾進度行
                prog = _parse_progress(raw)
                if prog:
                    if prog != last_progress:
                        last_progress = prog
                        yield _sse("progress", {"text": prog})
                else:
                    # 非進度行：只顯示有意義的訊息
                    clean = _ANSI_RE.sub('', raw).strip()
                    if clean and not clean.startswith('Enter url'):
                        yield _sse("line", {"text": clean})
            proc.wait(timeout=3600)
            # 根據結果 JSON 或 exit code 判斷成敗
            if result_json:
                ok = result_json.get("success", False) or bool(result_json.get("files"))
                files = result_json.get("files", [])
                if ok and files:
                    for f in files:
                        yield _sse("line", {"text": f"\u2705 已存: {f.split('/')[-1]}"})
                    yield _sse("done", {"code": 0, "ok": True})
                else:
                    err = result_json.get("error", "下載失敗")
                    # 過濾 EOF 誤報
                    if "EOF" in err or "Enter url" in err:
                        if last_progress:
                            yield _sse("done", {"code": 0, "ok": True})
                        else:
                            yield _sse("error", {"text": err})
                    else:
                        yield _sse("error", {"text": err})
            else:
                code = proc.returncode
                yield _sse("done", {"code": code, "ok": code in (0, 1) and bool(last_progress)})
        except subprocess.TimeoutExpired:
            yield _sse("error", {"text": "下載逾時"})
        except Exception as exc:
            yield _sse("error", {"text": str(exc)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 版本查詢 ──────────────────────────────────────────────────────────────────
@music_bp.route("/api/versions")
@music_auth_required
def api_versions():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "缺少 query"}), 400

    result = subprocess.run(
        [MUSIC_VENV_PYTHON, str(VERSIONS_SCRIPT)] + query.split(),
        text=True, capture_output=True, timeout=300,
    )
    output = result.stdout.strip()

    try:
        payload = json.loads(output)
    except Exception:
        return jsonify({"ok": False, "error": output or result.stderr.strip()}), 500

    numbered: list[dict] = []
    for provider in payload.get("providers", []):
        provider_name = provider.get("provider", "")
        for match in provider.get("matches", [])[:5]:
            numbered.append({**match, "provider": provider_name})

    state = load_state()
    state["last_versions"] = numbered
    state["last_query"] = query
    save_state(state)

    return jsonify({"ok": True, "query": query, "items": numbered})


# ── Pick ──────────────────────────────────────────────────────────────────────
@music_bp.route("/stream/pick")
@music_auth_required
def stream_pick():
    index = request.args.get("n", "1")
    if not index.isdigit():
        return jsonify({"error": "無效編號"}), 400

    state = load_state()
    items = state.get("last_versions", [])
    idx = int(index) - 1

    if not items or idx < 0 or idx >= len(items):
        return jsonify({"error": "請先查詢版本"}), 400

    target = items[idx]
    url = target.get("url", "")
    if not url:
        return jsonify({"error": f"#{index} 無直接下載連結"}), 400

    def generate():
        cmd = [MUSIC_VENV_PYTHON, str(DOWNLOAD_SCRIPT), "--manual-request", url]
        yield from run_stream(cmd, timeout=3600)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Analyze ───────────────────────────────────────────────────────────────────
@music_bp.route("/stream/analyze")
@music_auth_required
def stream_analyze():
    genre = request.args.get("genre", "").strip()
    if not genre:
        state = load_state()
        genre = state.get("genre", "")

    def generate():
        cmd = [MUSIC_VENV_PYTHON, str(ANALYZE_SCRIPT)]
        if genre:
            cmd += ["--genre", genre]
        yield from run_stream(cmd, timeout=3600)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── One（下載 + Analyze）─────────────────────────────────────────────────────
@music_bp.route("/stream/one")
@music_auth_required
def stream_one():
    raw = request.args.get("q", "").strip()
    if not raw:
        return jsonify({"error": "缺少 query"}), 400

    if "|" in raw:
        genre, query = raw.split("|", 1)
        genre = genre.strip()
        query = query.strip()
    else:
        state = load_state()
        genre = state.get("genre", "")
        query = raw

    if genre:
        state = load_state()
        state["genre"] = genre
        save_state(state)

    def generate():
        # Step 1: download
        yield _sse("phase", {"text": f"下載: {query}"})
        dl_cmd = [MUSIC_VENV_PYTHON, str(DOWNLOAD_SCRIPT), "--manual-request"] + query.split()
        exit_code = 0
        env = {**os.environ}
        proc = subprocess.Popen(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        assert proc.stdout
        for line in proc.stdout:
            s = line.rstrip()
            if s:
                yield _sse("line", {"text": s})
        proc.wait()
        exit_code = proc.returncode
        if exit_code != 0:
            yield _sse("done", {"code": exit_code, "ok": False, "phase": "download"})
            return

        # Step 2: analyze
        yield _sse("phase", {"text": f"Analyze: {genre or '自動判斷'}"})
        az_cmd = [MUSIC_VENV_PYTHON, str(ANALYZE_SCRIPT)]
        if genre:
            az_cmd += ["--genre", genre]
        proc2 = subprocess.Popen(az_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        assert proc2.stdout
        for line in proc2.stdout:
            s = line.rstrip()
            if s:
                yield _sse("line", {"text": s})
        proc2.wait()
        yield _sse("done", {"code": proc2.returncode, "ok": proc2.returncode == 0, "phase": "one"})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Library Manager ───────────────────────────────────────────────────────────
@music_bp.route("/stream/library/<command>")
@music_auth_required
def stream_library(command: str):
    allowed = {"status", "run-scan", "run-health", "run-dedup", "run-check"}
    if command not in allowed:
        return jsonify({"error": "不允許的指令"}), 400

    def generate():
        timeout = MANAGER_TIMEOUTS.get(command, 300)

        if command == "status":
            # 優先讀 iMac 本地快取 DB，SSH 失敗時 fallback
            import sqlite3 as _sqlite3
            LEXICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            use_cache = LEXICON_CACHE_DB.exists()
            if use_cache:
                sync_time = get_lexicon_sync_time()
                yield _sse("line", {"text": f"[快取 DB] 使用本地快取 DB（同步時間: {sync_time}）"})
                try:
                    db = _sqlite3.connect(str(LEXICON_CACHE_DB), timeout=30)
                    total      = db.execute('SELECT COUNT(*) FROM Track WHERE archived=0').fetchone()[0]
                    playlists  = db.execute('SELECT COUNT(*) FROM Playlist').fetchone()[0]
                    total_dur  = db.execute('SELECT SUM(duration) FROM Track WHERE archived=0').fetchone()[0] or 0
                    days       = int(total_dur) // 86400
                    hours      = (int(total_dur) % 86400) // 3600
                    has_bpm    = db.execute('SELECT COUNT(*) FROM Track WHERE archived=0 AND bpm>0').fetchone()[0]
                    has_key    = db.execute("SELECT COUNT(*) FROM Track WHERE archived=0 AND key!='' AND key IS NOT NULL").fetchone()[0]
                    has_genre  = db.execute("SELECT COUNT(*) FROM Track WHERE archived=0 AND genre!='' AND genre IS NOT NULL").fetchone()[0]
                    has_cue    = db.execute('SELECT COUNT(DISTINCT trackId) FROM Cuepoint').fetchone()[0]
                    no_bpm     = total - has_bpm
                    no_key     = total - has_key
                    no_genre   = total - has_genre
                    no_cue     = total - has_cue
                    healthy    = db.execute('''
                      SELECT COUNT(*) FROM Track t WHERE t.archived=0 AND t.bpm>0
                      AND t.key!='' AND t.key IS NOT NULL
                      AND EXISTS (SELECT 1 FROM Cuepoint c WHERE c.trackId=t.id)
                    ''').fetchone()[0]
                    healthy_pct = round(healthy / total * 100, 1) if total else 0
                    genres     = db.execute("SELECT genre, COUNT(*) as c FROM Track WHERE archived=0 AND genre!='' GROUP BY genre ORDER BY c DESC LIMIT 10").fetchall()
                    dist_bpm   = db.execute('''
                      SELECT
                        SUM(CASE WHEN bpm>=60  AND bpm<90  THEN 1 ELSE 0 END),
                        SUM(CASE WHEN bpm>=90  AND bpm<110 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN bpm>=110 AND bpm<120 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN bpm>=120 AND bpm<130 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN bpm>=130 AND bpm<140 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN bpm>=140 THEN 1 ELSE 0 END)
                      FROM Track WHERE archived=0 AND bpm>0
                    ''').fetchone()
                    db.close()
                    sep = '=' * 50
                    yield _sse("line", {"text": sep})
                    yield _sse("line", {"text": f"  TRACKS        {total:>8,}"})
                    yield _sse("line", {"text": f"  PLAYLISTS     {playlists:>8,}"})
                    yield _sse("line", {"text": f"  TOTAL PLAYTIME  {days}d {hours}h"})
                    yield _sse("line", {"text": f"  HEALTHY       {healthy_pct:>7}%  ({healthy:,}/{total:,})"})
                    yield _sse("line", {"text": sep})
                    yield _sse("line", {"text": ""})
                    yield _sse("line", {"text": "分析狀態:"})
                    yield _sse("line", {"text": f"  有 BPM        {has_bpm:>7,}  缺 {no_bpm:,}"})
                    yield _sse("line", {"text": f"  有 Key        {has_key:>7,}  缺 {no_key:,}"})
                    yield _sse("line", {"text": f"  有 Genre      {has_genre:>7,}  缺 {no_genre:,}"})
                    yield _sse("line", {"text": f"  有 CUE 點     {has_cue:>7,}  缺 {no_cue:,}"})
                    yield _sse("line", {"text": ""})
                    yield _sse("line", {"text": "BPM 分佈:"})
                    bpm_labels = ['60-89','90-109','110-119','120-129','130-139','140+']
                    bpm_max = max(dist_bpm) if dist_bpm and max(dist_bpm) else 1
                    for lbl, cnt in zip(bpm_labels, dist_bpm):
                        bar = '\u2588' * min(int((cnt or 0) / bpm_max * 30), 30)
                        yield _sse("line", {"text": f"  {lbl:8} {bar} {cnt or 0:,}"})
                    yield _sse("line", {"text": ""})
                    yield _sse("line", {"text": "Genre Top 10:"})
                    for g, c in genres:
                        yield _sse("line", {"text": f"  {g[:25]:25} {c:,}"})
                    yield _sse("done", {"code": 0, "ok": True})
                    return
                except Exception as e:
                    yield _sse("line", {"text": f"[快取 DB 讀取失敗: {e}] 改用 SSH→MBP..."})
            else:
                yield _sse("line", {"text": "[快取 DB 不存在] SSH→MBP 即時查詢..."})
            # Fallback: SSH→MBP
            status_script = r"""
import sqlite3, os
DB = '/Users/shinchen/Library/Application Support/Lexicon/main.db'
db = sqlite3.connect(DB)
total = db.execute('SELECT COUNT(*) FROM Track WHERE archived=0').fetchone()[0]
playlists = db.execute('SELECT COUNT(*) FROM Playlist').fetchone()[0]
total_dur = db.execute('SELECT SUM(duration) FROM Track WHERE archived=0').fetchone()[0] or 0
days = total_dur // 86400
hours = (total_dur % 86400) // 3600
has_bpm = db.execute('SELECT COUNT(*) FROM Track WHERE archived=0 AND bpm>0').fetchone()[0]
has_key = db.execute("SELECT COUNT(*) FROM Track WHERE archived=0 AND key!='' AND key IS NOT NULL").fetchone()[0]
has_genre = db.execute("SELECT COUNT(*) FROM Track WHERE archived=0 AND genre!='' AND genre IS NOT NULL").fetchone()[0]
has_cue = db.execute('SELECT COUNT(DISTINCT trackId) FROM Cuepoint').fetchone()[0]
no_bpm = total - has_bpm
no_key = total - has_key
no_genre = total - has_genre
no_cue = total - has_cue
healthy = db.execute('''
  SELECT COUNT(*) FROM Track t WHERE t.archived=0 AND t.bpm>0
  AND t.key!='' AND t.key IS NOT NULL
  AND EXISTS (SELECT 1 FROM Cuepoint c WHERE c.trackId=t.id)
''').fetchone()[0]
healthy_pct = round(healthy/total*100, 1) if total else 0
genres = db.execute("SELECT genre, COUNT(*) as c FROM Track WHERE archived=0 AND genre!='' GROUP BY genre ORDER BY c DESC LIMIT 10").fetchall()
dist_bpm = db.execute('''
  SELECT
    SUM(CASE WHEN bpm>=60 AND bpm<90 THEN 1 ELSE 0 END) as d60,
    SUM(CASE WHEN bpm>=90 AND bpm<110 THEN 1 ELSE 0 END) as d90,
    SUM(CASE WHEN bpm>=110 AND bpm<120 THEN 1 ELSE 0 END) as d110,
    SUM(CASE WHEN bpm>=120 AND bpm<130 THEN 1 ELSE 0 END) as d120,
    SUM(CASE WHEN bpm>=130 AND bpm<140 THEN 1 ELSE 0 END) as d130,
    SUM(CASE WHEN bpm>=140 THEN 1 ELSE 0 END) as d140
  FROM Track WHERE archived=0 AND bpm>0
''').fetchone()
print('='*50)
print(f'  TRACKS        {total:>8,}')
print(f'  PLAYLISTS     {playlists:>8,}')
print(f'  TOTAL PLAYTIME  {days}d {hours}h')
print(f'  HEALTHY       {healthy_pct:>7}%  ({healthy:,}/{total:,})')
print('='*50)
print(f'\n分析狀態:')
print(f'  有 BPM        {has_bpm:>7,}  缺 {no_bpm:,}')
print(f'  有 Key        {has_key:>7,}  缺 {no_key:,}')
print(f'  有 Genre      {has_genre:>7,}  缺 {no_genre:,}')
print(f'  有 CUE 點     {has_cue:>7,}  缺 {no_cue:,}')
print(f'\nBPM 分佈:')
labels = ['60-89','90-109','110-119','120-129','130-139','140+']
for lbl, cnt in zip(labels, dist_bpm):
    bar = '\u2588' * min(int((cnt or 0)/max(dist_bpm)*30),30) if max(dist_bpm) else ''
    print(f'  {lbl:8} {bar} {cnt or 0:,}')
print(f'\nGenre Top 10:')
for g, c in genres:
    print(f'  {g[:25]:25} {c:,}')
db.close()
"""
            import base64
            b64 = base64.b64encode(status_script.encode('utf-8')).decode()
            remote = f"python3 -c \"import base64; exec(base64.b64decode('{b64}').decode())\""
            cmd = ssh_cmd(remote)
            yield _sse("line", {"text": "[SSH→MBP] Lexicon DB 狀態查詢"})
            yield from run_stream(cmd, timeout=60)

        elif command in ("run-scan",):
            # 透過 SSH 在 MBP 執行掃描腳本
            scan_script = r"""
import os, collections
root = '/Volumes/Shin-Music/放歌專用'
exts = collections.Counter()
non_flac = []
total_size = 0
total_files = 0
for dirpath, dirs, files in os.walk(root):
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in ('.flac','.mp3','.m4a','.aiff','.wav','.aif','.ogg','.alac'):
            fp = os.path.join(dirpath, f)
            sz = os.path.getsize(fp)
            total_size += sz
            total_files += 1
            exts[ext] += 1
            if ext != '.flac':
                non_flac.append(f)
print(f'掃描路徑: {root}')
print(f'總檔案數: {total_files}')
print(f'總容量: {total_size/1024/1024/1024:.2f} GB')
print('\n格式分佈:')
for ext, cnt in sorted(exts.items(), key=lambda x: -x[1]):
    print(f'  {ext}: {cnt} 個')
print(f'\n非 FLAC 檔案: {len(non_flac)} 個')
if non_flac:
    print('需升級清單 (前50筆):')
    for fn in non_flac[:50]:
        print(f'  {fn}')
"""
            import base64
            b64 = base64.b64encode(scan_script.encode('utf-8')).decode()
            remote = f"python3 -c \"import base64,sys; exec(base64.b64decode('{b64}').decode())\""
            cmd = ssh_cmd(remote)
            yield _sse("line", {"text": f"[SSH→MBP] 掃描 {SHIN_MUSIC_DIR}"})
            yield from run_stream(cmd, timeout=timeout)
        elif command == "run-dedup":
            # 建立指紋庫並找重複曲目（使用外部 dedup-engine.py）
            resume_from_dedup = request.args.get("resume_from", "").strip()
            flags = []
            if resume_from_dedup and resume_from_dedup.isdigit():
                flags.append(f"--resume-from={resume_from_dedup}")
            remote = f"python3 {MBP_DEDUP_ENGINE} {' '.join(flags)}".strip()
            cmd = ssh_cmd(remote)
            label = "指紋比對重複掃描"
            if resume_from_dedup: label += f"（繼續第{resume_from_dedup}首）"
            yield _sse("phase", {"text": f"{label} [SSH→MBP]"})
            yield from _run_long("dedup", cmd, timeout=7200,
                                 progress_re=r"\[(\d+)/(\d+)\]")

        elif command == "run-health":
            # Lexicon DB：列出缺 BPM / Key / Genre 的曲目，優先讀快取 DB
            import sqlite3 as _sqlite3
            use_cache = LEXICON_CACHE_DB.exists()
            db_path   = str(LEXICON_CACHE_DB) if use_cache else None
            if use_cache:
                sync_time = get_lexicon_sync_time()
                yield _sse("line", {"text": f"[快取 DB] 健康檢查（同步時間: {sync_time}）"})
                try:
                    db      = _sqlite3.connect(db_path, timeout=30)
                    total   = db.execute('SELECT COUNT(*) FROM Track WHERE archived=0').fetchone()[0]
                    no_bpm  = db.execute('SELECT id,title,artist FROM Track WHERE archived=0 AND (bpm IS NULL OR bpm=0)').fetchall()
                    no_key  = db.execute("SELECT id,title,artist FROM Track WHERE archived=0 AND (key IS NULL OR key='')").fetchall()
                    no_genre= db.execute("SELECT id,title,artist FROM Track WHERE archived=0 AND (genre IS NULL OR genre='')").fetchall()
                    no_cue  = db.execute('''
                      SELECT t.id, t.title, t.artist FROM Track t
                      WHERE t.archived=0
                      AND NOT EXISTS (SELECT 1 FROM Cuepoint c WHERE c.trackId=t.id)
                    ''').fetchall()
                    db.close()
                    sep = '=' * 50
                    yield _sse("line", {"text": sep})
                    yield _sse("line", {"text": f"  TOTAL TRACKS    {total:,}"})
                    yield _sse("line", {"text": f"  缺 BPM          {len(no_bpm):,}"})
                    yield _sse("line", {"text": f"  缺 Key          {len(no_key):,}"})
                    yield _sse("line", {"text": f"  缺 Genre        {len(no_genre):,}"})
                    yield _sse("line", {"text": f"  缺 CUE 點       {len(no_cue):,}"})
                    yield _sse("line", {"text": sep})
                    if no_bpm:
                        yield _sse("line", {"text": "\n[缺 BPM] 前30筆:"})
                        for _,t,a in no_bpm[:30]:
                            yield _sse("line", {"text": f"  {(a or '?')[:20]:20}  {(t or '?')[:35]}"})
                    if no_key:
                        yield _sse("line", {"text": "\n[缺 Key] 前30筆:"})
                        for _,t,a in no_key[:30]:
                            yield _sse("line", {"text": f"  {(a or '?')[:20]:20}  {(t or '?')[:35]}"})
                    if no_genre:
                        yield _sse("line", {"text": "\n[缺 Genre] 前30筆:"})
                        for _,t,a in no_genre[:30]:
                            yield _sse("line", {"text": f"  {(a or '?')[:20]:20}  {(t or '?')[:35]}"})
                    yield _sse("done", {"code": 0, "ok": True})
                    return
                except Exception as e:
                    yield _sse("line", {"text": f"[快取 DB 讀取失敗: {e}] 改用 SSH→MBP..."})
            else:
                yield _sse("line", {"text": "[快取 DB 不存在] SSH→MBP 即時查詢..."})
            # Fallback: SSH→MBP
            health_script = r"""
import sqlite3
DB = '/Users/shinchen/Library/Application Support/Lexicon/main.db'
db = sqlite3.connect(DB)
total = db.execute('SELECT COUNT(*) FROM Track WHERE archived=0').fetchone()[0]
no_bpm  = db.execute('SELECT id,title,artist FROM Track WHERE archived=0 AND (bpm IS NULL OR bpm=0)').fetchall()
no_key  = db.execute("SELECT id,title,artist FROM Track WHERE archived=0 AND (key IS NULL OR key='')").fetchall()
no_genre= db.execute("SELECT id,title,artist FROM Track WHERE archived=0 AND (genre IS NULL OR genre='')").fetchall()
no_cue  = db.execute('''
  SELECT t.id, t.title, t.artist FROM Track t
  WHERE t.archived=0
  AND NOT EXISTS (SELECT 1 FROM Cuepoint c WHERE c.trackId=t.id)
''').fetchall()
print('='*50)
print(f'  TOTAL TRACKS    {total:,}')
print(f'  缺 BPM          {len(no_bpm):,}')
print(f'  缺 Key          {len(no_key):,}')
print(f'  缺 Genre        {len(no_genre):,}')
print(f'  缺 CUE 點       {len(no_cue):,}')
print('='*50)
if no_bpm:
    print(f'\n[缺 BPM] 前30筆:')
    for _,t,a in no_bpm[:30]:
        print(f'  {(a or "?")[:20]:20}  {(t or "?")[:35]}')
if no_key:
    print(f'\n[缺 Key] 前30筆:')
    for _,t,a in no_key[:30]:
        print(f'  {(a or "?")[:20]:20}  {(t or "?")[:35]}')
if no_genre:
    print(f'\n[缺 Genre] 前30筆:')
    for _,t,a in no_genre[:30]:
        print(f'  {(a or "?")[:20]:20}  {(t or "?")[:35]}')
db.close()
"""
            import base64
            b64 = base64.b64encode(health_script.encode('utf-8')).decode()
            remote = f"python3 -c \"import base64; exec(base64.b64decode('{b64}').decode())\""
            cmd = ssh_cmd(remote)
            yield _sse("line", {"text": "[SSH→MBP] Lexicon 健康檢查：查詢缺 BPM/Key/Genre/CUE 曲目"})
            yield from run_stream(cmd, timeout=60)

        elif command == "run-check":
            # 自動掃 Dropbox Music-Flac（排除 Upgrade），與歌庫比對指紋（使用外部 check-engine.py）
            resume_from_check = request.args.get("resume_from", "").strip()
            flags = []
            if resume_from_check and resume_from_check.isdigit():
                flags.append(f"--resume-from={resume_from_check}")
            remote = f"python3 {MBP_CHECK_ENGINE} {' '.join(flags)}".strip()
            cmd = ssh_cmd(remote)
            label = "新歌比對：Dropbox vs 歌庫"
            if resume_from_check: label += f"（繼續第{resume_from_check}首）"
            yield _sse("phase", {"text": f"{label} [SSH→MBP]"})
            yield from _run_long("check", cmd, timeout=7200,
                                 progress_re=r"\[(\d+)/(\d+)\]")

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── FLAC 升級 ─────────────────────────────────────────────────────────────────
# ── 長時間操作 process 管理 ──────────────────────────────────────────────────
# key: 'flac' / 'dedup' / 'check'
_long_procs: dict[str, subprocess.Popen] = {}

# 暫停狀態檔路徑
FLAC_UPGRADE_STATE = STATE_DIR / "flac-upgrade-state.json"
DEDUP_STATE        = STATE_DIR / "dedup-state.json"
CHECK_STATE        = STATE_DIR / "check-state.json"

MBP_DEDUP_ENGINE  = f"{MBP_WORKSPACE}/scripts/dedup-engine.py"
MBP_CHECK_ENGINE  = f"{MBP_WORKSPACE}/scripts/check-engine.py"

# backward compat alias
_flac_proc = None  # unused, kept for safety


def _kill_proc(key: str) -> None:
    proc = _long_procs.pop(key, None)
    if proc and proc.poll() is None:
        proc.terminate()


def _run_long(key: str, cmd: list[str], timeout: int,
              progress_re: str | None = None) -> Generator:
    """
    執行長時間 SSH 工作，管理 proc。
    progress_re: 如果提供，試圖從每行抽取 [N/total] 發送 flac_progress event。
    """
    env = {**os.environ}
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        _long_procs[key] = proc
        assert proc.stdout
        yield _sse("start", {"ts": time.strftime("%H:%M:%S")})
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                if progress_re:
                    m = _re.search(progress_re, stripped)
                    if m:
                        cur   = int(m.group(1))
                        total = int(m.group(2))
                        pct   = int(cur / total * 100) if total else 0
                        yield _sse("flac_progress", {
                            "current": cur, "total": total, "pct": pct
                        })
                yield _sse("line", {"text": stripped})
        proc.wait(timeout=timeout)
        code = proc.returncode
        _long_procs.pop(key, None)
        yield _sse("done", {"code": code, "ok": code == 0})
    except Exception as exc:
        _long_procs.pop(key, None)
        yield _sse("error", {"text": str(exc)})


@music_bp.route("/stream/flac")
@music_auth_required
def stream_flac():
    global _flac_proc
    dry         = request.args.get("dry", "0") == "1"
    resume_from = request.args.get("resume_from", "").strip()
    clear_pause = request.args.get("clear", "0") == "1"

    if clear_pause:
        FLAC_UPGRADE_STATE.unlink(missing_ok=True)

    def generate():
        global _flac_proc
        flags = []
        if dry:
            flags.append("--dry-run")
        if resume_from and resume_from.isdigit():
            flags.append(f"--resume-from={resume_from}")
        flags_str = " ".join(flags)
        remote = (
            f"OPENCLAW_MUSIC_SCAN_DIR='{SHIN_MUSIC_DIR}' "
            f"python3 {MBP_FLAC_ENGINE} {flags_str}".strip()
        )
        cmd = ssh_cmd(remote)
        label = "FLAC 升級"
        if dry:          label += "（dry run）"
        if resume_from:  label += f"（繼續第{resume_from}首）"
        yield _sse("phase", {"text": f"{label}: {SHIN_MUSIC_DIR} [SSH→MBP]"})
        yield from _run_long("flac", cmd, timeout=7200,
                              progress_re=r"\[(\d+)/(\d+)\]")
        # 正常完成清除暫停狀態
        code = _long_procs.get("flac")  # already popped by _run_long
        if not dry:
            # 如果 done event 是 ok=True 才清除（在 done event 後）
            pass  # 清除邏輯已在 /api/flac/pause 和 done SSE 後的 JS 處理

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_STATE_FILES = {
    "flac":  FLAC_UPGRADE_STATE,
    "dedup": DEDUP_STATE,
    "check": CHECK_STATE,
}


@music_bp.route("/api/op/<op_key>/pause", methods=["POST"])
@music_auth_required
def op_pause(op_key: str):
    """記錄暫停點並終止對應 SSH 進程。op_key: flac / dedup / check"""
    if op_key not in _STATE_FILES:
        return jsonify({"error": "unknown op"}), 400
    data    = request.json or {}
    current = data.get("current", 0)
    total   = data.get("total", 0)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "paused_at":   current,
        "total":       total,
        "paused_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "op":          op_key,
    }
    _STATE_FILES[op_key].write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _kill_proc(op_key)
    return jsonify({"ok": True, "state": state})


@music_bp.route("/api/op/<op_key>/stop", methods=["POST"])
@music_auth_required
def op_stop(op_key: str):
    """終止 SSH 進程，不記錄暫停點。"""
    if op_key not in _STATE_FILES:
        return jsonify({"error": "unknown op"}), 400
    _kill_proc(op_key)
    return jsonify({"ok": True})


@music_bp.route("/api/op/<op_key>/state")
@music_auth_required
def op_state(op_key: str):
    """回傳上次暫停狀態，沒有則回傳 null。"""
    if op_key not in _STATE_FILES:
        return jsonify({"error": "unknown op"}), 400
    f = _STATE_FILES[op_key]
    if f.exists():
        try:
            return jsonify(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return jsonify(None)
