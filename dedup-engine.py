#!/usr/bin/env python3
"""
dedup-engine.py — 指紋重複掃描引擎（MBP 執行）
支援 --resume-from=N 從第 N 個檔案繼續。
"""
import argparse, collections, json, os, subprocess, sys
from pathlib import Path

FPCALC = '/opt/homebrew/bin/fpcalc'
ROOT   = os.environ.get('OPENCLAW_MUSIC_SCAN_DIR', '/Volumes/Shin-Music/放歌專用')
EXTS   = {'.flac', '.mp3', '.m4a', '.aiff', '.wav', '.aif', '.alac'}
STATE_DIR = Path('/tmp/openclaw-music')
STATE_FILE = STATE_DIR / 'dedup-state.json'

parser = argparse.ArgumentParser()
parser.add_argument('--resume-from', type=int, default=0)
args = parser.parse_args()

print(f'掃描 {ROOT} ...')
files = []
for dp, dirs, fs in os.walk(ROOT):
    for f in sorted(fs):
        if os.path.splitext(f)[1].lower() in EXTS:
            files.append(os.path.join(dp, f))
files.sort()

total = len(files)
start = args.resume_from
print(f'共 {total} 個檔案，從第 {start+1} 開始計算指紋...' if start else f'共 {total} 個檔案，開始計算指紋...')

# 從已存的 state 載入前段指紋（resume 用）
fingerprints = {}
if start and STATE_FILE.exists():
    try:
        saved = json.loads(STATE_FILE.read_text())
        fingerprints = saved.get('fingerprints', {})
        print(f'  載入已有指紋 {len(fingerprints)} 首')
    except Exception:
        pass

STATE_DIR.mkdir(parents=True, exist_ok=True)

for i, fp in enumerate(files):
    if i < start:
        continue
    try:
        r = subprocess.run([FPCALC, '-json', fp], capture_output=True, text=True, timeout=30)
        d = json.loads(r.stdout)
        fingerprints[fp] = d.get('fingerprint', '')
    except Exception as e:
        print(f'  跳過: {os.path.basename(fp)} ({e})')
    cur = i + 1
    if cur % 50 == 0 or cur == total:
        print(f'  [{cur}/{total}] 進度 {int(cur/total*100)}%')
        # 寫 state
        STATE_FILE.write_text(json.dumps({
            'current': cur, 'total': total,
            'fingerprints': fingerprints
        }, ensure_ascii=False))

print(f'建立指紋庫完成: {len(fingerprints)} 首')

groups = collections.defaultdict(list)
for path, fp in fingerprints.items():
    key = fp[:8] if fp else 'NOPRINT'
    groups[key].append(path)
dupes = {k: v for k, v in groups.items() if len(v) > 1}
print(f'發現可能重複組數: {len(dupes)}')
if dupes:
    for key, paths in list(dupes.items())[:20]:
        print(f'\n[重複組 {key[:6]}]')
        for p in paths:
            print(f'  {os.path.basename(p)}')
else:
    print('未發現重複曲目')

# 完成後清除 state
STATE_FILE.unlink(missing_ok=True)
