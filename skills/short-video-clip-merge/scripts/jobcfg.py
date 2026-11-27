#!/usr/bin/env python3
"""jobcfg.py — 加载 job.json，统一路径解析与默认值。

job.json 字段：
  script          定稿文案路径（必填）
  videos          原始片段路径数组（自动选段时必填）
  complete_source 用户提供的「已认可完整成片/手动版」路径（可选，见 Step 0）
  out_dir         输出目录（默认 job.json 所在目录）
  title_rows      封面标题行数组（可选，见 references/cover-and-subs.md）
  text_fixes      字幕笔误修正字典（可选）
  no_face         跳过正脸检测（调试用）
  no_cover        不加封面卡
  no_subs         不烧字幕（用户说"先不需要加字幕"时设 true）
  words_model     选段转写模型 small|medium（默认 medium）
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
FACE_SCRIPT = SCRIPTS / "facebatch.swift"


def load(job_path: str | Path) -> dict:
    jp = Path(job_path).resolve()
    if not jp.exists():
        raise FileNotFoundError(f"job.json 不存在: {jp}")
    cfg = json.loads(jp.read_text(encoding="utf-8"))
    out = Path(cfg.get("out_dir", str(jp.parent))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cfg["job_path"] = str(jp)
    cfg["out_dir"] = str(out)
    cfg["script"] = str(Path(cfg["script"]).resolve()) if cfg.get("script") else ""
    if cfg.get("complete_source"):
        cfg["complete_source"] = str(Path(cfg["complete_source"]).resolve())
    cfg["videos"] = [str(Path(v).resolve()) for v in cfg.get("videos", [])]
    cfg["words_dir"] = str(out / "words")
    cfg["words_medium_dir"] = str(out / "words_medium")
    cfg.setdefault("no_face", False)
    cfg.setdefault("no_cover", False)
    cfg.setdefault("no_subs", False)
    cfg.setdefault("words_model", "medium")
    cfg.setdefault("text_fixes", {})
    cfg.setdefault("title_rows", [])
    return cfg


def job_arg():
    """从 argv 取 --job 参数。"""
    if "--job" not in sys.argv:
        raise SystemExit("用法: python <script>.py --job <job.json 路径>")
    i = sys.argv.index("--job")
    if i + 1 >= len(sys.argv):
        raise SystemExit("--job 后缺少路径")
    return sys.argv[i + 1]
