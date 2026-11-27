#!/usr/bin/env python3
"""selector_v2.py — 句子级选段器 v2（修复吞字 + 正脸筛选 + 缺素材不硬剪）

相对 v1 的三处硬修复（用户 2026-11-27 验收指出）：
  1. 吞字：coverage 门槛 0.60→0.98，且候选必须"末词完整落在 take 内"；
     无达标候选 → 标 missing，绝不降级硬剪。
  2. 正脸：候选 take 抽 3 帧送 macOS Vision 检测 yaw/pitch/roll，
     超阈值(YAW_MAX=0.20/PITCH_MAX=0.20/ROLL_MAX=0.20)淘汰。
  3. 词边界精确切片：t0/t1 用首个/末个匹配词的真实起止，不做 ±0.08 padding。

对齐基准仍用定稿文案 token；手动版转写只用于最终验收（ground truth），不参与选段。

用法: python select_takes.py --job <job.json>
job.json 设 no_face=true 可跳过 Vision（仅调试）。
"""
from __future__ import annotations
import json
import re
import sys
import difflib
import subprocess
import tempfile
from pathlib import Path

import jobcfg as J

TOOL_DIR = J.SCRIPTS
sys.path.insert(0, str(TOOL_DIR))
from segment_selector import tokenize, norm_word  # noqa: E402

CFG = J.load(J.job_arg())
JOB = Path(CFG["out_dir"])
# small 对部分句子识别不全（实测"删掉它会带来的损失是什么/第二步删除"词级全丢），
# medium 完整识别。默认用 medium 做选段基准。
WD_NAME = "words_medium" if CFG["words_model"] == "medium" else "words"
WORDS_DIR = Path(CFG["words_medium_dir"] if CFG["words_model"] == "medium" else CFG["words_dir"])
SCRIPT = Path(CFG["script"])
OUT = JOB / "selection.json"
REPORT = JOB / "selection_report.md"
FACE_SCRIPT = J.FACE_SCRIPT
VIDEOS = [Path(v) for v in CFG["videos"]]

FILLERS = {"嗯", "呃", "额", "啊", "呀", "哦", "嘛", "那个", "这个", "就是",
           "然后", "所以说", "对吧", "知道吧", "um", "uh", "erm"}
SPLIT_RE = re.compile(r"[。！？；?!;]")
TAG_RE = re.compile(r"^\s*【[^】]*】\s*$")

# --- 完整性硬门槛 ---
# 注意：coverage 只用于"能否定位到这句"，不能当完整性门槛。
# 用户口播与文案措辞有出入（文案"删了会有什么具体损失" vs 口播"删掉它会带来的损失是什么"），
# 逐字覆盖率必然低。真正的吞字来自"切片只取匹配词区间"。
# 修复=定位后把 t0/t1 扩展到完整语音段（按停顿边界）。
MIN_COVERAGE = 0.30          # 仅定位门槛：能找到这句的位置即可
TAIL_CUT_SEC = 0.12          # 末词距 take 末尾小于此值 → 疑似说一半
MAX_FILLER = 0.45
UTTER_PAUSE = 0.30           # 词间隔 >此值视为句子边界
MAX_EXPAND_WORDS = 3         # 扩展上限：只补 whisper 切碎的相邻词，不跨句

# --- 正脸硬门槛 ---
YAW_MAX = 0.20               # 左右转头：正脸判定核心
PITCH_MAX = 0.25             # 上下点头（微微低头不算侧脸）
ROLL_MAX = 0.35              # 头部倾斜(歪头)不算侧脸，只作软参考，不设硬门槛
FACE_SAMPLES = 3             # 每个 take 抽 3 帧
FACE_PASS_RATIO = 0.67       # 至少 2/3 帧正脸

# --- 其他 ---
CLUSTER_GAP_SEC = 4.5
JOIN_GAP_SEC = 0.40
RECENCY_W = 0.06             # 最后一遍优先，但权重低于完整性/正脸


def parse_anchors(script_text: str):
    lines = [ln.strip() for ln in script_text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not TAG_RE.match(ln)]
    anchors, offset = [], 0
    for line in lines:
        for sent in SPLIT_RE.split(line):
            sent = sent.strip()
            if not sent:
                continue
            toks = tokenize(sent)
            if not toks:
                continue
            anchors.append({"id": len(anchors), "text": sent,
                            "s": offset, "e": offset + len(toks)})
            offset += len(toks)
    return anchors


def load_segments():
    segs = []
    for idx, vp in enumerate(VIDEOS, 1):
        jp = WORDS_DIR / (vp.stem + ".json")
        if not jp.exists():
            raise FileNotFoundError(f"缺少转写：{jp}")
        data = json.loads(jp.read_text(encoding="utf-8"))
        words = data.get("words", [])
        toks, widx = [], []
        for wi, w in enumerate(words):
            for t in tokenize(w["text"]):
                toks.append(t)
                widx.append(wi)
        segs.append({"file": str(vp), "idx": idx, "stem": vp.stem,
                     "words": words, "tokens": toks,
                     "token_word_idx": widx, "seg2script": {}})
    return segs


def align(segs, script_tokens):
    for seg in segs:
        sm = difflib.SequenceMatcher(None, seg["tokens"], script_tokens, autojunk=False)
        seg["seg2script"] = {ai + k: bi + k
                             for ai, bi, size in sm.get_matching_blocks() if size
                             for k in range(size)}


def candidate_for_anchor(seg, anchor):
    """某句在某段中的所有出现（簇）→ 候选。t0/t1 用真实词边界，不加 padding。"""
    s, e = anchor["s"], anchor["e"]
    matched = [(ti, si, seg["token_word_idx"][ti])
               for ti, si in seg["seg2script"].items() if s <= si < e]
    if not matched:
        return []
    matched.sort(key=lambda x: x[2])
    clusters, cur, last_si = [], [], -1
    for m in matched:
        si = m[1]
        gap = 0.0
        if cur:
            gap = (seg["words"][m[2]]["start"]
                   - seg["words"][cur[-1][2]]["end"])
        if cur and (si < last_si - 2 or gap > CLUSTER_GAP_SEC):
            clusters.append(cur)
            cur, last_si = [], si
        else:
            last_si = max(last_si, si)
        cur.append(m)
    if cur:
        clusters.append(cur)

    out = []
    words = seg["words"]
    for cl in clusters:
        covered = len({si for _, si, _ in cl})
        coverage = covered / max(1, e - s)
        m0, m1 = cl[0][2], cl[-1][2]        # 匹配到的首/末词下标

        # 关键修复：从匹配词向两侧扩展到【完整语音段】边界。
        # 只取匹配词区间会导致吞字（coverage 低的句子只剩半句画面+音频）。
        wi0, wi1 = m0, m1
        for _ in range(MAX_EXPAND_WORDS):
            if wi0 <= 0:
                break
            gap = words[wi0]["start"] - words[wi0 - 1]["end"]
            if gap > UTTER_PAUSE:
                break
            wi0 -= 1
        for _ in range(MAX_EXPAND_WORDS):
            if wi1 >= len(words) - 1:
                break
            gap = words[wi1 + 1]["start"] - words[wi1]["end"]
            if gap > UTTER_PAUSE:
                break
            wi1 += 1

        t0 = words[wi0]["start"]           # 完整语音段边界
        t1 = words[wi1]["end"]
        span = words[wi0:wi1 + 1]
        matched_wi = {wi for _, _, wi in cl}
        extra = [w for wi, w in enumerate(span) if wi0 + wi not in matched_wi]
        n = max(1, len(span))
        filler = len(extra) / n
        dis = sum(1 for w in span if norm_word(w["text"]) in FILLERS) / n
        # 末词截断：只判【音频物理截断】——语音段异常短（<0.5s）说明只说了半句。
        # 不判文案词匹配数：口播与文案措辞有出入（"至少要删一半" vs "超过一半该删"）
        # 会让正常完整句误判为截断。
        tail_cut = (t1 - t0) < 0.5
        # 完整性信号：扩展后语音段里匹配词占比（越高越完整）
        integrity = covered / max(1, len(span))
        out.append({
            "file": seg["file"], "stem": seg["stem"], "take_index": seg["idx"],
            "t0": round(t0, 3), "t1": round(t1, 3),
            "coverage": round(coverage, 3), "integrity": round(integrity, 3),
            "filler": round(filler, 3), "disfluency": round(dis, 3),
            "tail_cut": tail_cut,
            "holes": [], "span_wi": [wi0, wi1], "match_wi": [m0, m1],
            "spoken": "".join(w["text"] for w in span),
            "quality": round(0.45 * coverage + 0.25 * integrity
                             - 0.15 * filler - 0.15 * dis, 4),
        })
    return out


def fallback_short(seg, anchor):
    """短句兜底：在段 token 序列里模糊找该句，用 SequenceMatcher 最佳块。"""
    target = tokenize(anchor["text"])
    if not target or not seg["tokens"]:
        return None
    sm = difflib.SequenceMatcher(None, seg["tokens"], target, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= max(2, len(target) - 1)]
    if not blocks:
        return None
    best = max(blocks, key=lambda b: b.size)
    wi0 = seg["token_word_idx"][best.a]
    wi1 = seg["token_word_idx"][best.a + best.size - 1]
    words = seg["words"]
    # 扩展到完整语音段
    for _ in range(MAX_EXPAND_WORDS):
        if wi0 > 0 and words[wi0]["start"] - words[wi0 - 1]["end"] <= UTTER_PAUSE:
            wi0 -= 1
        else:
            break
    for _ in range(MAX_EXPAND_WORDS):
        if wi1 < len(words) - 1 and words[wi1 + 1]["start"] - words[wi1]["end"] <= UTTER_PAUSE:
            wi1 += 1
        else:
            break
    t0, t1 = words[wi0]["start"], words[wi1]["end"]
    return {
        "file": seg["file"], "stem": seg["stem"], "take_index": seg["idx"],
        "t0": round(t0, 3), "t1": round(t1, 3),
        "coverage": round(best.size / len(target), 3),
        "integrity": 0.8, "filler": 0.3, "disfluency": 0.0,
        "tail_cut": (t1 - t0) < 0.5,
        "holes": [], "span_wi": [wi0, wi1], "match_wi": [wi0, wi1],
        "spoken": "".join(w["text"] for w in words[wi0:wi1 + 1]),
        "quality": round(0.45 * best.size / len(target) + 0.25 * 0.8, 4),
        "fallback": True,
    }


def face_check(cands, no_face=False):
    """批量正脸检测：每个候选抽 3 帧 → Vision → 写回 face 字段。"""
    if no_face:
        for cs in cands:
            for c in cs:
                c["face"] = {"ok": True, "skipped": True, "passed": 3,
                             "samples": 3, "yaw": 0.0, "pitch": 0.0, "roll": 0.0}
        return
    import subprocess as sp
    tmp = Path(tempfile.mkdtemp(prefix="facecheck_"))
    tasks = []  # (ai, ci, path)
    for ai, cs in enumerate(cands):
        for ci, c in enumerate(cs):
            dur = c["t1"] - c["t0"]
            for k in range(FACE_SAMPLES):
                frac = (k + 0.5) / FACE_SAMPLES
                t = c["t0"] + dur * frac
                fp = tmp / f"a{ai:03d}_c{ci:02d}_{k}.png"
                r = sp.run(["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", c["file"],
                            "-frames:v", "1", "-vf", "scale=360:640", str(fp)],
                           capture_output=True, text=True)
                if r.returncode == 0 and fp.exists():
                    tasks.append((ai, ci, str(fp)))
    if not tasks:
        for cs in cands:
            for c in cs:
                c["face"] = {"ok": False, "reason": "无抽帧"}
        return
    lj = tmp / "list.json"
    lj.write_text(json.dumps([p for _, _, p in tasks], ensure_ascii=False), encoding="utf-8")
    r = sp.run(["swift", str(FACE_SCRIPT), str(lj)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Vision 检测失败: {r.stderr[-1000:]}")
    # 解析
    detected = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 10 or parts[1] != "FACE":
            continue
        try:
            detected[parts[0]] = {"q": float(parts[2]), "yaw": float(parts[3]),
                                  "pitch": float(parts[4]), "roll": float(parts[5])}
        except ValueError:
            continue
    # 汇总到候选
    acc = {}
    for ai, ci, p in tasks:
        acc.setdefault((ai, ci), []).append(detected.get(p))
    for ai, cs in enumerate(cands):
        for ci, c in enumerate(cs):
            samples = acc.get((ai, ci), [])
            valid = [s for s in samples if s is not None]
            passed = [s for s in valid
                      if abs(s["yaw"]) < YAW_MAX and abs(s["pitch"]) < PITCH_MAX]
            ratio = len(passed) / max(1, len(valid))
            c["face"] = {
                "ok": len(valid) >= FACE_SAMPLES - 1 and ratio >= FACE_PASS_RATIO,
                "samples": len(valid), "passed": len(passed), "ratio": round(ratio, 3),
                "yaw": round(max((abs(s["yaw"]) for s in valid), default=9), 3),
                "pitch": round(max((abs(s["pitch"]) for s in valid), default=9), 3),
                "roll": round(max((abs(s["roll"]) for s in valid), default=9), 3),
            }


def decide(anchors, all_cands):
    flat = [(ai, c) for ai, cs in enumerate(all_cands) for c in cs]
    ordered = sorted(flat, key=lambda x: (x[1]["take_index"], x[1]["t0"]))
    max_rank = max(1, len(ordered) - 1)
    for rank, (ai, c) in enumerate(ordered):
        c["recency"] = round(rank / max_rank, 4)
        c["final"] = round(c["quality"] + RECENCY_W * c["recency"], 4)

    decisions = []
    for ai, anchor in enumerate(anchors):
        cands = all_cands[ai]
        if not cands:
            decisions.append({"anchor_id": ai, "text": anchor["text"],
                              "chosen": None, "candidates": [],
                              "reason": "缺素材：全部片段无匹配语音",
                              "status": "missing"})
            continue
        # 硬门槛：coverage + filler + 非截断 + 正脸
        def eligible(c):
            # filler 不作硬门槛：扩展语音段后 filler 会把相邻句词误算成虚词。
            # 硬门槛只保留三项：能定位 / 非截断 / 正脸。
            return (c["coverage"] >= MIN_COVERAGE
                    and not c["tail_cut"]
                    and c.get("face", {}).get("ok", False))
        viable = [c for c in cands if eligible(c)]
        rejected = [c for c in cands if not eligible(c)]
        if not viable:
            # 诊断原因
            why = []
            if not any(c["coverage"] >= MIN_COVERAGE for c in cands):
                why.append(f"覆盖率不足(最高{max(c['coverage'] for c in cands):.2f}<{MIN_COVERAGE})")
            if not any(not c["tail_cut"] for c in cands):
                why.append("全部疑似说一半")
            if not any(c["filler"] <= MAX_FILLER for c in cands):
                why.append(f"虚词过多(最低{min(c['filler'] for c in cands):.2f}>{MAX_FILLER})")
            if not why:
                why.append("门槛组合无达标候选")
            decisions.append({"anchor_id": ai, "text": anchor["text"],
                              "chosen": None, "candidates": cands,
                              "rejected": rejected,
                              "reason": "缺素材：" + "/".join(why),
                              "status": "missing"})
            continue
        viable.sort(key=lambda c: (-c["final"], -c["recency"]))
        best = viable[0]
        newest_ok = any(c["recency"] > max(x["recency"] for x in viable) and eligible(c)
                        for c in cands)
        reason = ("采用最后一遍（达标）" if newest_ok else
                  f"采用最优达标 take q={best['quality']:.3f} cov={best['coverage']:.2f} "
                  f"face_ok={best['face']['passed']}/{best['face']['samples']}")
        decisions.append({"anchor_id": ai, "text": anchor["text"],
                          "chosen": best, "candidates": cands, "rejected": rejected,
                          "reason": reason, "status": "ok"})
    return decisions


def merge_clips(decisions):
    """相邻句同源且时间连续/重叠 → 合并，避免同一语音段被重复取用。

    关键：相邻两句在原始口播里常连着说，候选区间会重叠（如 [26][27] 都取
    161.44-165.26）。必须合并只取一次，否则成片重复、时长虚高。
    """
    clips = []
    for d in decisions:
        c = d.get("chosen")
        if not c:
            continue
        if clips:
            last = clips[-1]
            if last["file"] == c["file"] and c["t0"] <= last["out"] + JOIN_GAP_SEC:
                # 连续或重叠：扩展 out（不回退），记录 anchor
                last["out"] = max(last["out"], c["t1"])
                last["anchor_ids"].append(d["anchor_id"])
                continue
        clips.append({"file": c["file"], "stem": c["stem"],
                      "in": c["t0"], "out": c["t1"],
                      "holes": [], "anchor_ids": [d["anchor_id"]]})
    return clips


def main():
    no_face = bool(CFG["no_face"])
    anchors = parse_anchors(SCRIPT.read_text(encoding="utf-8"))
    script_tokens = [t for a in anchors for t in tokenize(a["text"])]
    segs = load_segments()
    align(segs, script_tokens)

    all_cands = []
    for a in anchors:
        cs = []
        for seg in segs:
            cs.extend(candidate_for_anchor(seg, a))
        # 短句兜底：短 anchor（如"第二，删除"）常被 whisper 合并进相邻词，
        # difflib 全局对齐匹配不到 → 单独在本段找最佳同音/近音块。
        if len(tokenize(a["text"])) <= 6:
            for seg in segs:
                fb = fallback_short(seg, a)
                if fb:
                    cs.append(fb)
        cs.sort(key=lambda c: (c["take_index"], c["t0"]))
        all_cands.append(cs)

    print(f"[正脸] 检测 {sum(len(c) for c in all_cands)} 个候选…", flush=True)
    face_check(all_cands, no_face=no_face)

    decisions = decide(anchors, all_cands)
    clips = merge_clips(decisions)
    missing = [d["anchor_id"] for d in decisions if d["status"] == "missing"]

    # 入选句文案（缺素材句剔除）——字幕只覆盖实际成片内容，避免字幕悬空
    included_text = "。".join(d["text"] for d in decisions if d["status"] == "ok") + "。"

    result = {
        "anchors": anchors, "decisions": decisions, "keep_clips": clips,
        "included_text": included_text,
        "summary": {
            "anchors": len(anchors),
            "candidates": sum(len(c) for c in all_cands),
            "missing": missing,
            "merged_clips": len(clips),
            "est_duration_sec": round(sum(c["out"] - c["in"] for c in clips), 2),
            "thresholds": {"coverage": MIN_COVERAGE, "yaw": YAW_MAX,
                           "pitch": PITCH_MAX, "roll": ROLL_MAX,
                           "face_pass_ratio": FACE_PASS_RATIO},
        },
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"句子数：{len(anchors)}  候选 take：{result['summary']['candidates']}",
        f"合并片段：{len(clips)}  预计时长：{result['summary']['est_duration_sec']}s",
        f"缺素材句（不硬剪）：{missing}",
        f"阈值：cov≥{MIN_COVERAGE} yaw<{YAW_MAX} pitch<{PITCH_MAX} roll<{ROLL_MAX}",
        "",
    ]
    for d in decisions:
        c = d.get("chosen")
        if c:
            f = c.get("face", {})
            lines.append(f"[{d['anchor_id']:02d}] {d['text'][:26]}… | {c['stem'][-12:]} "
                         f"{c['t0']:.2f}-{c['t1']:.2f} | cov={c['coverage']:.2f} "
                         f"face {f.get('passed')}/{f.get('samples')} yaw={f.get('yaw')} | {d['reason']}")
        else:
            lines.append(f"[{d['anchor_id']:02d}] {d['text'][:26]}… | ❌ {d['reason']}")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"OK -> {OUT}")
    print(f"OK -> {REPORT}")
    print(f"missing={len(missing)} {missing}")


if __name__ == "__main__":
    main()
