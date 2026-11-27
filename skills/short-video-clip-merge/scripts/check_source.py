#!/usr/bin/env python3
"""check_source.py — Step 0 门控：用户提供「已认可完整成片/手动版」时，先验证它是否完整正确。

血泪教训（2026-11-27）：用户第一轮就给了手动版做 ground truth，
却坚持走自动选段，连错两轮引入 6 类问题（吞字/重复/多字/截断/侧脸/静止封面）。
手动版本身完整正脸无字幕时，正确做法是直接转码，而不是用自动选段重造。

判定一个 complete_source 是否可直接交付：
  1. 转写覆盖：关键文案锚点全部出现，且无长停顿（截断信号）
  2. 无重复：同一锚点只出现 1 次
  3. 开头动态：首帧不是静止封面卡（正脸 + 首词在 1s 内出现）
  4. 正脸：抽查 5 帧 yaw/pitch 在阈值内

用法: python check_source.py --job <job.json>
输出: <out_dir>/source_check.json 与 source_check.md
"""
from __future__ import annotations
import json
import os
import re
import difflib
import subprocess
import sys
from pathlib import Path

import jobcfg as J

CFG = J.load(J.job_arg())
JOB = Path(CFG["out_dir"])
TOOL_DIR = J.SCRIPTS
PY = sys.executable
FF = "ffmpeg"


def transcribe(path: Path, out_dir: Path, model: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HF_ENDPOINT="https://hf-mirror.com", HF_HUB_DISABLE_XET="1")
    r = subprocess.run([PY, str(TOOL_DIR / "transcribe_words.py"),
                        str(out_dir), model, str(path)],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"完整源转写失败: {r.stderr[-1500:]}")
    jp = out_dir / (path.stem + ".json")
    return json.loads(jp.read_text(encoding="utf-8"))


def face_samples(path: Path, times: list[float]) -> list[dict]:
    tmp = JOB / "_src_frames"
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, t in enumerate(times):
        fp = tmp / f"f{i}.png"
        subprocess.run([FF, "-y", "-ss", f"{t:.2f}", "-i", str(path),
                        "-frames:v", "1", "-vf", "scale=360:640", str(fp)],
                       capture_output=True, text=True)
        if fp.exists():
            paths.append(str(fp.resolve()))
    if not paths:
        return []
    lj = tmp / "list.json"
    lj.write_text(json.dumps(paths), encoding="utf-8")
    r = subprocess.run(["swift", str(J.FACE_SCRIPT), str(lj)],
                       capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 6 and parts[1] == "FACE":
            out.append({"path": parts[0], "yaw": float(parts[3]),
                        "pitch": float(parts[4]), "roll": float(parts[5])})
    return out


def main():
    src = CFG.get("complete_source")
    if not src or not Path(src).exists():
        raise SystemExit("job.json 未提供 complete_source 或文件不存在；跳过 Step 0，走自动选段流程。")
    path = Path(src)
    print(f"[Step 0] 验证完整源: {path.name}", flush=True)

    data = transcribe(path, JOB / "source_words", CFG["words_model"])
    text = data.get("text", "")
    words = data.get("words", [])

    # 锚点覆盖与重复：从文案抽 8-16 字关键短语
    raw = Path(CFG["script"]).read_text(encoding="utf-8")
    lines = [ln.strip() for ln in raw.splitlines()
             if ln.strip() and not re.match(r"^【[^】]*】$", ln.strip())]
    anchors = []
    for ln in lines:
        for s in re.split(r"[。！？；?!;]", ln):
            s = s.strip()
            if len(s) >= 6:
                anchors.append(s[:8])
    anchors = anchors[:20]
    flat = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
    def norm(t):
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", t)
    # 口播与文案措辞有出入 → 用滑窗模糊匹配，不用精确子串计数
    hits = []
    for a in anchors:
        na = norm(a)
        best = max((difflib.SequenceMatcher(None, na, flat[i:i + len(na) + 4]).ratio()
                    for i in range(0, max(1, len(flat) - len(na)), 3)), default=0)
        hits.append((a, round(best, 3)))
    covered = sum(1 for _, r in hits if r >= 0.60)
    repeated = [a for a, r in hits if r >= 0.60 and flat.count(norm(a)[:6]) >= 2]

    # 长停顿（>1.5s）→ 截断/缺失信号
    gaps = [(round(words[i - 1]["end"], 2), round(words[i]["start"], 2),
             round(words[i]["start"] - words[i - 1]["end"], 2))
            for i in range(1, len(words))
            if words[i]["start"] - words[i - 1]["end"] > 1.5]

    first_word = words[0]["start"] if words else 999
    faces = face_samples(path, [0.3, 3.0, min(30, data["duration"] / 2),
                                min(60, data["duration"] - 5), data["duration"] - 2])
    face_ok = [f for f in faces if abs(f["yaw"]) < 0.20 and abs(f["pitch"]) < 0.25]
    face_pass = len(face_ok) >= max(2, len(faces) - 1) if faces else False

    # 覆盖率仅作提示：whisper 对专有名词/措辞差异识别差（实测"特斯拉Model 3"
    # 识别成"特斯拉摩托三"、"工程师..."整句丢失），不能当硬门槛。
    # 硬门槛 = 无重复 + 无长停顿 + 开头动态 + 正脸 + 时长合理（防大段缺失）。
    script_chars = len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "",
                              Path(CFG["script"]).read_text(encoding="utf-8")))
    expected_sec = script_chars / 4.5          # 中文口播约 4.5 字/秒
    actual_sec = data.get("duration") or 0
    duration_ok = actual_sec >= expected_sec * 0.75
    checks = {
        "无重复锚点": not repeated,
        "无 >1.5s 长停顿": not gaps,
        "开头非静止（首词 <1s）": first_word < 1.0,
        "正脸（yaw<0.20/pitch<0.25）": face_pass,
        "时长合理（≥文案预估75%）": duration_ok,
    }
    ok = all(checks.values())
    result = {
        "source": str(path), "usable_as_final": ok,
        "duration": data.get("duration"), "words": len(words),
        "anchors_total": len(anchors), "anchors_covered": covered,
        "coverage_info": f"{covered}/{len(anchors)}（仅提示，不作门槛）",
        "expected_duration_sec": round(expected_sec, 1),
        "repeated_anchors": repeated, "long_gaps": gaps,
        "first_word_sec": first_word,
        "face_samples": faces, "face_pass": face_pass,
        "checks": checks,
        "next_action": ("直接转码交付（python transcode_source.py --job ...）" if ok
                        else "完整源不合格，走自动选段流程（transcribe → select_takes → assemble）"),
    }
    (JOB / "source_check.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [f"# 完整源验证：{path.name}", "",
          f"可直接使用：**{'是' if ok else '否'}**", "",
          f"- 时长 {data.get('duration')}s / 词数 {len(words)}",
          f"- 锚点覆盖 {covered}/{len(anchors)}（仅提示）",
          f"- 时长预估 {expected_sec:.0f}s / 实际 {actual_sec:.0f}s",
          f"- 重复锚点：{repeated if repeated else '无'}",
          f"- 长停顿：{gaps if gaps else '无'}",
          f"- 首词出现：{first_word}s",
          f"- 正脸检测：{len(face_ok)}/{len(faces)} 通过", "",
          "## 检查项", ""]
    md += [f"- [{'x' if v else ' '}] {k}" for k, v in checks.items()]
    md += ["", f"下一步：{result['next_action']}"]
    (JOB / "source_check.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md), flush=True)
    print(f"\nOK -> {JOB / 'source_check.json'}", flush=True)


if __name__ == "__main__":
    main()
