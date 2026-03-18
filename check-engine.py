#!/usr/bin/env python3
"""
check-engine.py — Dropbox 新歌 vs 歌庫指紋比對引擎（MBP 執行）
支援 --resume-from=N 從第 N 個歌庫索引檔案繼續。
"""
import argparse, collections, json, os, subprocess, sys
from pathlib import Path

FPCALC    = '/opt/homebrew/bin/fpcalc'
ROOT      = os.environ.get('OPENCLAW_MUSIC_SCAN_DIR', '/Volumes/Shin-Music/放歌專用')
DROPBOX   = os.path.expanduser(
    '~/Library/CloudStorage/Dropbox-ProfessionalDJteam/Chen Shin/Music-Flac'
)
EXCLUDE   = os.path.join(DROPBOX, 'Upgrade')
EXTS      = {'.flac', '.mp3', '.m4a', '.aiff', '.wav', '.aif', '.alac'}
STATE_DIR  = Path('/tmp/openclaw-music')
STATE_FILE = STATE_DIR / 'check-state.json'

parser = argparse.ArgumentParser()
parser.add_argument('--resume-from', type=int, default=0,
                    dest='resume_from',
                    help='從歌庫第 N 個索引檔案繼續（0 = 重頭）')
args = parser.parse_args()

# ── 掃 Dropbox 新歌（排除 Upgrade）─────────────────────────────────────────
new_files: list[str] = []
for dp, dirs, files in os.walk(DROPBOX):
    dirs[:] = [d for d in dirs if os.path.join(dp, d) != EXCLUDE]
    for f in sorted(files):
        if os.path.splitext(f)[1].lower() in EXTS:
            new_files.append(os.path.join(dp, f))

if not new_files:
    print('Dropbox 資料夾中無音樂檔案（已排除 Upgrade）')
    sys.exit(0)

print(f'Dropbox 新歌：{len(new_files)} 個（排除 Upgrade）')
print(f'建立歌庫指紋索引中（{ROOT}）...')

# ── 收集歌庫檔案清單 ──────────────────────────────────────────────────────
lib_files: list[str] = []
for dp, dirs, files in os.walk(ROOT):
    for f in sorted(files):
        if os.path.splitext(f)[1].lower() in EXTS:
            lib_files.append(os.path.join(dp, f))
lib_files.sort()

lib_total_count = len(lib_files)
start = args.resume_from
print(f'共 {lib_total_count} 個歌庫檔案，從第 {start+1} 開始建立索引...'
      if start else f'共 {lib_total_count} 個歌庫檔案，開始建立索引...')

# ── 從 state 載入前段指紋（resume 用）─────────────────────────────────────
lib_fps: dict[str, list[str]] = collections.defaultdict(list)
if start and STATE_FILE.exists():
    try:
        saved = json.loads(STATE_FILE.read_text())
        for key, paths in saved.get('lib_fps', {}).items():
            lib_fps[key] = paths
        print(f'  載入已有索引 {sum(len(v) for v in lib_fps.values())} 首')
    except Exception:
        pass

STATE_DIR.mkdir(parents=True, exist_ok=True)

# ── 建立歌庫指紋索引 ─────────────────────────────────────────────────────
for i, fp_path in enumerate(lib_files):
    if i < start:
        continue
    try:
        r = subprocess.run([FPCALC, '-json', fp_path],
                           capture_output=True, text=True, timeout=30)
        fp_val = json.loads(r.stdout).get('fingerprint', '')[:8]
        if fp_val:
            lib_fps[fp_val].append(fp_path)
    except Exception as e:
        print(f'  跳過: {os.path.basename(fp_path)} ({e})')
    cur = i + 1
    if cur % 50 == 0 or cur == lib_total_count:
        print(f'  [{cur}/{lib_total_count}] 索引進度 {int(cur/lib_total_count*100)}%')
        # 寫 state（儲存 lib_fps 以便 resume）
        STATE_FILE.write_text(json.dumps({
            'current':    cur,
            'total':      lib_total_count,
            'lib_fps':    dict(lib_fps),
        }, ensure_ascii=False))

print(f'歌庫指紋索引完成：{sum(len(v) for v in lib_fps.values())} 首')
print('=' * 50)

# ── 比對 Dropbox 新歌 ────────────────────────────────────────────────────
dupes:  list[tuple[str, str]] = []
new_ok: list[str]             = []

for nf in new_files:
    fname = os.path.basename(nf)
    try:
        r = subprocess.run([FPCALC, '-json', nf],
                           capture_output=True, text=True, timeout=30)
        fp_val = json.loads(r.stdout).get('fingerprint', '')[:8]
        if fp_val and fp_val in lib_fps:
            match = os.path.basename(lib_fps[fp_val][0])
            dupes.append((fname, match))
        else:
            new_ok.append(fname)
    except Exception as e:
        print(f'  跳過: {fname} ({e})')

print(f'\n[結果]')
print(f'  重複（已在歌庫）: {len(dupes)} 首 → 不需匯入')
print(f'  新歌（歌庫未有）: {len(new_ok)} 首 → 可保留')

if dupes:
    print(f'\n[重複清單]')
    for d, m in dupes:
        print(f'  {d[:50]:50}  ← 重複: {m[:40]}')

if new_ok:
    print(f'\n[新歌清單]')
    for n in new_ok:
        print(f'  {n}')

# 完成後清除 state
STATE_FILE.unlink(missing_ok=True)
