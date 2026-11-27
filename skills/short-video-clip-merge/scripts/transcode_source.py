#!/usr/bin/env python3
"""transcode_source.py — Step 0 通过后的交付脚本：完整源直接转码为竖版发布规格。

不加字幕、不加封面卡（用户明确"先不需要加字幕"时）。
如需字幕/封面，改用 video-autocut 技能或在 job.json 开 no_subs=false / no_cover=false。

用法: python transcode_source.py --job <job.json>
输出: <out_dir>/final.mp4
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

import jobcfg as J

CFG = J.load(J.job_arg())
JOB = Path(CFG["out_dir"])
src = CFG.get("complete_source")
if not src or not Path(src).exists():
    raise SystemExit("job.json 未提供 complete_source 或文件不存在")
final = JOB / "final.mp4"
W, H, FPS, CRF = 1080, 1920, 30, 20
print(f"[转码] {Path(src).name} → {W}x{H}/{FPS}fps", flush=True)
r = subprocess.run([
    "ffmpeg", "-y", "-i", src,
    "-vf", f"scale={W}:{H}:flags=lanczos,fps={FPS},format=yuv420p",
    "-c:v", "libx264", "-crf", str(CRF), "-preset", "medium",
    "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
    "-movflags", "+faststart", str(final),
], capture_output=True, text=True)
if r.returncode != 0:
    raise RuntimeError(f"转码失败: {r.stderr[-1500:]}")
pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                     "format=duration,size:stream=codec_type,width,height,r_frame_rate",
                     "-of", "default=noprint_wrappers=1", str(final)],
                    capture_output=True, text=True)
print(pr.stdout, flush=True)
print(f"OK -> {final}", flush=True)
