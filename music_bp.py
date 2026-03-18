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
MANAGER_SCRIPT  = CRON_SCRIPTS / "openclaw_music_library_manager.sh"
FLAC_ENGINE     = SCRIPTS / "flac-upgrade-engine.py"

SHIN_MUSIC_DIR  = "/Volumes/Shin-Music/放歌專用"
STATE_DIR       = Path("/tmp/openclaw-music")
STATE_FILE      = STATE_DIR / "web_state.json"

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
@music_bp.route("/stream/download")
@music_auth_required
def stream_download():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "缺少 query"}), 400

    def generate():
        cmd = ["python3", str(DOWNLOAD_SCRIPT), "--manual-request"] + query.split()
        yield from run_stream(cmd, timeout=3600)

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
        ["python3", str(VERSIONS_SCRIPT)] + query.split(),
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
        cmd = ["python3", str(DOWNLOAD_SCRIPT), "--manual-request", url]
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
        cmd = ["python3", str(ANALYZE_SCRIPT)]
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
        dl_cmd = ["python3", str(DOWNLOAD_SCRIPT), "--manual-request"] + query.split()
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
        az_cmd = ["python3", str(ANALYZE_SCRIPT)]
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
    allowed = {"status", "run-scan", "run-health", "run-compare", "run-queue", "run-dispatch", "logs"}
    if command not in allowed:
        return jsonify({"error": "不允許的指令"}), 400

    def generate():
        timeout = MANAGER_TIMEOUTS.get(command, 300)
        # 透過 Tailscale SSH 在 MBP 執行 manager 腳本
        remote = f"/bin/bash {MBP_MANAGER} {command}"
        cmd = ssh_cmd(remote)
        yield _sse("line", {"text": f"[SSH→MBP] {remote}"})
        yield from run_stream(cmd, timeout=timeout)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── FLAC 升級 ─────────────────────────────────────────────────────────────────
@music_bp.route("/stream/flac")
@music_auth_required
def stream_flac():
    dry = request.args.get("dry", "0") == "1"

    def generate():
        dry_flag = "--dry-run" if dry else ""
        # 透過 Tailscale SSH 在 MBP 執行 FLAC 升級（硬碟接在 MBP）
        remote = (
            f"OPENCLAW_MUSIC_SCAN_DIR='{SHIN_MUSIC_DIR}' "
            f"python3 {MBP_FLAC_ENGINE} {dry_flag}".strip()
        )
        cmd = ssh_cmd(remote)
        yield _sse("phase", {"text": f"FLAC 升級{'（dry run）' if dry else ''}: {SHIN_MUSIC_DIR} [SSH→MBP]"})
        yield from run_stream(cmd, timeout=7200)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
