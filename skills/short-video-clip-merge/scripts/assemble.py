#!/usr/bin/env python3
"""assemble.py — 按 selection.json 切片、挖洞、拼接、字幕、封面、自检。

复用 retrim_v2 已验证的 concat_filtergraph / build_script_cues / make_cover /
detect_black_intervals / detect_silence_intervals，不重新实现。
"""
from __future__ import annotations
import json
import os
import re
import sys
import difflib
import subprocess
import tempfile
from pathlib import Path

import jobcfg as J

TOOL_DIR = J.SCRIPTS
sys.path.insert(0, str(TOOL_DIR))
import retrim_v2 as R  # noqa: E402

CFG = J.load(J.job_arg())
JOB = Path(CFG["out_dir"])
SEL = JOB / "selection.json"
SCRIPT = Path(CFG["script"])
WORDS_DIR = Path(CFG["words_dir"])
FF = R.FF
NO_COVER = bool(CFG["no_cover"])
NO_SUBS = bool(CFG["no_subs"])

V = {
    "width": 1080, "height": 1920, "fps": 30, "crf": 18,
    "sub_font": "Hiragino Sans GB", "sub_font_size": 72,
    "sub_shadow": 2, "sub_margin_x": 80, "sub_margin_v": 430,
    "sub_highlights": [],
    "cover_font_size": 150, "cover_margin_ratio": 0.08,
    "cover_pos_from_bottom": 0.28,
    "cover_font_path": "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "cover_sec": 2.5,
}
COVER_ROWS = [{"text": r["text"] if isinstance(r, dict) else r,
               "fill": (r.get("fill", "white") if isinstance(r, dict) else "white"),
               "outline": (r.get("outline", "black") if isinstance(r, dict) else "black")}
              for r in CFG["title_rows"]]
TEXT_FIXES = dict(CFG["text_fixes"])  # 字幕笔误修正（报告里记录）
JOIN_FADE = 0.008
MIN_SUB = 0.18


def clean_script(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not re.match(r"^\s*【[^】]*】\s*$", ln)]
    s = "\n".join(lines)
    for a, b in TEXT_FIXES.items():
        s = s.replace(a, b)
    s = re.sub(r"\s+", "", s)
    return s


def subtract_holes(a: float, b: float, holes: list) -> list:
    bounds = [(a, b)]
    for hs, he in sorted(holes):
        nb = []
        for x0, x1 in bounds:
            if he <= x0 or hs >= x1:
                nb.append((x0, x1))
            else:
                if hs > x0:
                    nb.append((x0, hs))
                if he < x1:
                    nb.append((he, x1))
        bounds = nb
    return [(x0, x1) for x0, x1 in bounds if x1 - x0 >= MIN_SUB]


def ff(args, log=None):
    r = subprocess.run([FF, "-y", *args], capture_output=True, text=True)
    if r.returncode != 0:
        if log:
            Path(log).write_text(r.stderr, encoding="utf-8")
        raise RuntimeError(f"ffmpeg 失败: {r.stderr[-2000:]}")
    return r


def main():
    sel = json.loads(SEL.read_text(encoding="utf-8"))
    clips = sel["keep_clips"]
    print(f"[切片] {len(clips)} 个源片段 → 挖洞切片", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="stitch_"))
    parts = []
    for ci, c in enumerate(clips, 1):
        for h in c.get("holes", []):
            pass
        for si, (t0, t1) in enumerate(subtract_holes(c["in"], c["out"], c.get("holes", []))):
            dur = t1 - t0
            out = tmp / f"p{ci:03d}_{si:02d}.mp4"
            fade_out = max(0.0, dur - JOIN_FADE)
            # -ss 必须放在 -i 前：否则 afade 作用于完整输入，淡出后整段音频永久静音
            ff([
                "-ss", f"{t0:.3f}", "-i", c["file"], "-t", f"{dur:.3f}",
                "-vf", f"scale={V['width']}:{V['height']},fps={V['fps']},format=yuv420p",
                "-af", f"afade=t=in:st=0:d={JOIN_FADE},afade=t=out:st={fade_out:.3f}:d={JOIN_FADE}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(V["crf"]),
                "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out),
            ])
            parts.append(str(out))
            print(f"  p{ci:03d}_{si:02d} {t0:.2f}-{t1:.2f} ({dur:.2f}s)", flush=True)
    if not parts:
        raise RuntimeError("没有可拼接的切片")

    print(f"[拼接] {len(parts)} 个切片（分层，每 10 片一组）", flush=True)
    merged = JOB / "merged_v2.mp4"
    mids = []
    for i in range(0, len(parts), 10):
        mid = tmp / f"g{i // 10:02d}.mp4"
        R.concat_filtergraph(parts[i:i + 10], str(mid), V)
        mids.append(str(mid))
    R.concat_filtergraph(mids, str(merged), V)

    # ===== 字幕（可选）：对成片重新 medium 转写取词级时间轴，文字以文案为准 =====
    cues, sub_text = [], ""
    srt = ass = None
    if not NO_SUBS:
        print("[转写] 成片 medium 词级转写（字幕时间轴）", flush=True)
        env = dict(os.environ, HF_ENDPOINT="https://hf-mirror.com", HF_HUB_DISABLE_XET="1")
        r = subprocess.run([
            sys.executable, str(TOOL_DIR / "transcribe_words.py"),
            str(WORDS_DIR), "medium", str(merged),
        ], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"成片转写失败: {r.stderr[-2000:]}")
        print(r.stdout, flush=True)
        mwords = json.loads((WORDS_DIR / (merged.stem + ".json")).read_text(encoding="utf-8"))["words"]
        chars = R.normalize_punct(R.words_to_chars(mwords))
        # 字幕只用入选句文案（缺素材句已剔除），避免字幕显示但无对应语音
        script = clean_script(sel.get("included_text") or SCRIPT.read_text(encoding="utf-8"))
        cues = R.build_script_cues(chars, script, max_line=12)
        # 封面前置 cover_sec → 字幕整体延后，避免叠在封面/全程提前（踩坑5）
        if not NO_COVER:
            for cue in cues:
                cue["start"] += V["cover_sec"]
                cue["end"] += V["cover_sec"]
        sub_text = "".join(c["text"].replace("\\N", "") for c in cues)
        srt = JOB / "subtitles.srt"
        ass = JOB / "subtitles.ass"
        srt.write_text(R.cues_to_srt(cues), encoding="utf-8")
        ass.write_text(R.cues_to_ass(cues, V), encoding="utf-8")

    # ===== 封面（可选）：取第一个切片靠前的一帧 =====
    cover_mp4 = None
    cover = None
    if not NO_COVER:
        if not COVER_ROWS:
            raise RuntimeError("要加封面必须在 job.json 提供 title_rows（至少一行）")
        print("[封面] 抽帧", flush=True)
        frame = JOB / "cover_frame.png"
        ff(["-ss", "0.6", "-i", parts[0], "-frames:v", "1", str(frame)])
        cover = JOB / "cover.png"
        R.make_cover(str(frame), str(cover), COVER_ROWS, V)
        cover_mp4 = JOB / "cover.mp4"
        ff(["-loop", "1", "-r", str(V["fps"]), "-i", str(cover),
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", str(V["cover_sec"]), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-r", str(V["fps"]), "-c:a", "aac", "-shortest", str(cover_mp4)])

    # ===== 成片：封面 + 正片拼接，再烧字幕（可选） =====
    print("[成片] 组装 + 编码", flush=True)
    body = JOB / "body.mp4"
    inputs = [str(cover_mp4), str(merged)] if cover_mp4 else [str(merged)]
    R.concat_filtergraph(inputs, str(body), V)
    final = JOB / "final.mp4"
    vf = []
    if ass:
        vf.append(f"subtitles='{ass}':original_size={V['width']}x{V['height']}:"
                  f"force_style='FontName={V['sub_font']},FontSize={V['sub_font_size']},"
                  f"MarginL={V['sub_margin_x']},MarginR={V['sub_margin_x']},"
                  f"MarginV={V['sub_margin_v']},Outline=3'")
    args = ["-i", str(body)]
    if vf:
        args += ["-vf", ",".join(vf)]
    args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-r", str(V["fps"]), "-c:v", "libx264", "-crf", "20", "-c:a", "aac", str(final)]
    ff(args)

    # ===== 自检 =====
    def norm(t):
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", t)
    script_for_check = clean_script(sel.get("included_text") or SCRIPT.read_text(encoding="utf-8"))
    ratio = difflib.SequenceMatcher(None, norm(sub_text), norm(script_for_check)).ratio() if sub_text else 0.0
    missing = sel["summary"]["missing"]
    black = R.detect_black_intervals(str(final))
    silence = R.detect_silence_intervals(str(final))
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                         "format=duration:stream=codec_type,width,height,r_frame_rate",
                         "-of", "json", str(final)], capture_output=True, text=True)
    probe = json.loads(pr.stdout)
    checks = {
        "无黑帧": len(black) == 0,
        "无 >0.15s 静音吞字": len(silence) == 0,
        "规格 30fps / 1080x1920": any(
            st.get("width") == 1080 and st.get("height") == 1920 and st.get("r_frame_rate") == "30/1"
            for st in probe["streams"]),
        "音轨存在": any(st.get("codec_type") == "audio" for st in probe["streams"]),
        "入选句无缺素材": not missing,
    }
    if not NO_SUBS:
        checks["与文案文本相似度 >0.96"] = ratio > 0.96
        checks["字幕条数 >0"] = len(cues) > 0
    if not NO_COVER:
        checks["封面生成"] = cover.exists()
    rep = [
        "=== 多遍口播拼接自检 ===",
        f"黑帧：{black if black else '无'}",
        f"静音：{silence if silence else '无'}",
        f"成片时长：{probe['format'].get('duration')}s",
        f"字幕条数：{len(cues)}",
        f"字幕笔误修正：{TEXT_FIXES}",
        f"缺素材句（不硬剪）：{missing}",
        f"入选句文案：{sel.get('included_text', '')[:80]}…",
        "",
        "=== 检查项 ===",
    ]
    rep += [f"  [{'✓' if v else '✗'}] {k}" for k, v in checks.items()]
    rep += ["", f"成片：{final}"]
    if srt:
        rep.append(f"字幕：{srt} / {ass}")
    if cover:
        rep.append(f"封面：{cover}")
    (JOB / "report.txt").write_text("\n".join(rep), encoding="utf-8")
    print("\n".join(rep), flush=True)


if __name__ == "__main__":
    main()
