"""
segment_selector.py — 分段口播的「选段 / 去重 / 评分」核心逻辑（纯 Python，无外部依赖）

解决的真问题：
  用户分段录短视频，第一段末尾觉得不好 → 重录成第二段开头，
  于是「第一段尾」与「第二段头」在文案上重叠。需要：
    1. 把每段转写对齐到最终文案，定位每段覆盖文案的哪一段；
    2. 检测出相邻段的重叠区间（= 重录区）；
    3. 对重叠区两段候选打分（连贯/错误/语气词/长停顿），保留更优的，删掉更差的；
    4. 段内超长停顿（默认 >1.2s）删掉，短停顿保留；
    5. 口语词（呃/额/嗯…）只轻微扣分，不删除，保留真实感。

本模块只做「决策」：输入是已转写好的结构化数据，输出是
「保留哪些 (文件, 入点, 出点) 片段 + 人类可读报告」。
真正的音视频切割由 pipeline 用 ffmpeg 完成。

可直接单测：构造 mock 的 segments（带 word 时间戳）即可，无需 whisper/ffmpeg。
"""

from __future__ import annotations
import re
import difflib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ----------------------------------------------------------------------------
# 配置（可被 pipeline 覆盖）
# ----------------------------------------------------------------------------
DEFAULTS = {
    "long_pause_sec": 1.2,        # 超过此长度的停顿(词间空隙)才删；以下保留
    "pause_pad_sec": 0.15,        # 切停顿时的前后保留余量，避免切到字头字尾
    "w_coverage": 3.0,            # 文案覆盖率权重（最重要：这段该说的说全了没）
    "w_filler": 1.0,              # 多余废话(不在文案里的词)惩罚
    "w_disfluency": 0.6,          # 语气词密度惩罚（轻，保留口语感）
    "w_pause": 0.8,               # 长停顿总时长惩罚（每秒）
    "dup_similarity": 0.6,        # 重叠区「真重复」判定阈值：低于此值视为顺序录制(非重录)，两段都保留
    "disfluency_words": {"呃", "额", "嗯", "啊", "呀", "哦", "那个", "就是",
                         "这个", "然后然后", " um", " uh", "erm", "like", "you know"},
}


def overlap_similarity(winner_seg: "Segment", loser_seg: "Segment", ov_s: int, ov_e: int) -> float:
    """比较两段在文案重叠区内【实际说出的文字】相似度。
    这是防止误删的关键：只有重叠区真的重复(高相似)才做去重/裁剪；
    若相似度低，说明只是 difflib 因识别误差把两段『对齐』到了同一文案区，
    实际是顺序录制的不同内容 → 必须两段都保留。"""
    wt = "".join(w.text for w, _ in words_in_span(winner_seg, ov_s, ov_e))
    lt = "".join(w.text for w, _ in words_in_span(loser_seg, ov_s, ov_e))
    if not wt or not lt:
        return 0.0
    return difflib.SequenceMatcher(None, wt, lt).ratio()


# ----------------------------------------------------------------------------
# 分词 / 归一化
# ----------------------------------------------------------------------------
def tokenize(text: str) -> List[str]:
    """把文本切成 token：CJK 单字 + 拉丁词。标点/空格去掉，小写化。
    中文按字切便于模糊对齐；英文按词切。混合文本各自成立。"""
    text = text.lower()
    tokens: List[str] = []
    buf = []
    for ch in text:
        if re.match(r"[\u4e00-\u9fff]", ch):
            if buf:
                tokens.append("".join(buf)); buf = []
            tokens.append(ch)
        elif re.match(r"[a-z0-9]", ch):
            buf.append(ch)
        else:
            if buf:
                tokens.append("".join(buf)); buf = []
    if buf:
        tokens.append("".join(buf))
    return [t for t in tokens if t]


def norm_word(w: str) -> str:
    return w.lower().strip(" .,!?，。！？、；：…—“”‘’()（）\"'")


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------
@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Segment:
    """一段已转写的视频素材。"""
    file: str
    idx: int                     # 录制顺序（1,2,3…）
    words: List[Word]           # whisper 词级时间戳（按时间排序）
    duration: float = 0.0        # 视频总时长（秒），用于尾部裁剪判定

    # —— 下面由 align_to_script 填充 ——
    script_start: int = -1       # 本段覆盖文案的起始 token 下标（含）
    script_end: int = -1         # 本段覆盖文案的结束 token 下标（不含）
    seg2script: dict = field(default_factory=dict)   # seg token idx -> script token idx

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def tokens(self) -> List[str]:
        return [norm_word(w.text) for w in self.words]


@dataclass
class KeepClip:
    """最终要保留的一个连续片段。"""
    file: str
    seg_idx: int
    in_sec: float
    out_sec: float
    reason: str = ""


@dataclass
class OverlapDecision:
    seg_a: int
    seg_b: int
    script_span: Tuple[int, int]
    winner: int                  # 胜出段的 seg_idx
    loser: int
    score_a: float
    score_b: float
    note: str


# ----------------------------------------------------------------------------
# 对齐：每段 -> 文案
# ----------------------------------------------------------------------------
def align_to_script(seg: Segment, script_tokens: List[str], cfg=DEFAULTS) -> None:
    """用 difflib 把本段 token 序列对齐到文案 token 序列，
    写入 seg.script_start / script_end / seg.seg2script。"""
    st = seg.tokens()
    sm = difflib.SequenceMatcher(None, st, script_tokens, autojunk=False)
    seg.seg2script = {}
    starts, ends = [], []
    for ai, bi, size in sm.get_matching_blocks():
        if size == 0:
            continue
        starts.append(bi)
        ends.append(bi + size)
        for k in range(size):
            seg.seg2script[ai + k] = bi + k
    if starts:
        seg.script_start = min(starts)
        seg.script_end = max(ends)
    else:
        seg.script_start = -1
        seg.script_end = -1


def script_span_time(seg: Segment, s: int, e: int) -> Tuple[float, float]:
    """返回本段中「覆盖文案 token [s, e)」的时间区间 [t0, t1]。
    找不到则返回 (0,0) 表示无对应。"""
    ts, te = None, None
    for i, w in enumerate(seg.words):
        si = seg.seg2script.get(i)
        if si is None:
            continue
        if s <= si < e:
            if ts is None:
                ts = w.start
            te = w.end
    if ts is None:
        return 0.0, 0.0
    return ts, te


def words_in_span(seg: Segment, s: int, e: int) -> List[Tuple[Word, Optional[int]]]:
    """返回本段中落在文案 [s,e) 内的 (word, script_idx) 列表。"""
    out = []
    for i, w in enumerate(seg.words):
        si = seg.seg2script.get(i)
        if si is not None and s <= si < e:
            out.append((w, si))
    return out


# ----------------------------------------------------------------------------
# 重叠区评分
# ----------------------------------------------------------------------------
def score_candidate(seg: Segment, s: int, e: int, script_tokens: List[str], cfg=DEFAULTS) -> Tuple[float, dict]:
    """对 seg 在文案 [s,e) 区间内的表现打分。越高越好。"""
    cand = words_in_span(seg, s, e)
    if not cand:
        return -999.0, {"coverage": 0.0, "filler": 1.0, "disfluency": 1.0, "pause_sec": 0.0}
    cand_tokens = {si for _, si in cand}
    span_script = script_tokens[s:e]
    coverage = len(cand_tokens & set(range(s, e))) / max(1, len(span_script))
    # 多余废话：候选里不在文案区间的词占比
    extra = [w for w, si in cand if si is None or not (s <= si < e)]
    filler = len(extra) / max(1, len(cand))
    # 语气词密度
    dis = sum(1 for w, _ in cand if norm_word(w.text) in cfg["disfluency_words"])
    disfluency = dis / max(1, len(cand))
    # 长停顿总时长：候选词之间的空隙
    pause_sec = 0.0
    for k in range(1, len(cand)):
        gap = cand[k][0].start - cand[k - 1][0].end
        if gap > cfg["long_pause_sec"]:
            pause_sec += gap
    score = (cfg["w_coverage"] * coverage
             - cfg["w_filler"] * filler
             - cfg["w_disfluency"] * disfluency
             - cfg["w_pause"] * pause_sec)
    return score, {"coverage": round(coverage, 2), "filler": round(filler, 2),
                   "disfluency": round(disfluency, 2), "pause_sec": round(pause_sec, 2)}


# ----------------------------------------------------------------------------
# 主流程：决策
# ----------------------------------------------------------------------------
def select(segments: List[Segment], script: str, cfg: dict = None) -> Tuple[List[KeepClip], List[OverlapDecision], str]:
    """输入按录制顺序排列的 segments + 最终文案。
    返回 (保留片段列表, 重叠决策列表, 人类可读报告)。"""
    cfg = {**DEFAULTS, **(cfg or {})}
    script_tokens = tokenize(script)
    if not script_tokens:
        raise ValueError("文案为空或无法分词")

    for seg in segments:
        align_to_script(seg, script_tokens, cfg)

    decisions: List[OverlapDecision] = []
    # loser  overlap 时间区间（在 loser 自己的时间坐标里），用于裁剪
    loser_trims: dict[int, List[Tuple[float, float]]] = {seg.idx: [] for seg in segments}

    # 逐对相邻段检测重叠
    for i in range(len(segments) - 1):
        a, b = segments[i], segments[i + 1]
        if a.script_end == -1 or b.script_start == -1:
            continue
        ov_s, ov_e = b.script_start, a.script_end
        if ov_e <= ov_s:
            continue  # 无重叠，正常衔接
        # —— 安全闸门：先确认重叠区是否【真重复】，避免误删顺序录制的新内容 ——
        sim = overlap_similarity(a, b, ov_s, ov_e)
        if sim < cfg["dup_similarity"]:
            note = (f"重叠区「{script_tokens[ov_s:ov_e][:12]}…」(文案 {ov_s}-{ov_e}) "
                    f"实际文字相似度={sim:.2f} < {cfg['dup_similarity']} → 判定为顺序录制(非重录重复)，"
                    f"段{a.idx} 与 段{b.idx} 均【保留】，不做任何删除/裁剪")
            decisions.append(OverlapDecision(a.idx, b.idx, (ov_s, ov_e), -1, -1, 0.0, 0.0, note))
            continue

        sa, da = score_candidate(a, ov_s, ov_e, script_tokens, cfg)
        sb, db = score_candidate(b, ov_s, ov_e, script_tokens, cfg)
        winner = a.idx if sa >= sb else b.idx
        loser = b.idx if winner == a.idx else a.idx
        winner_seg = a if winner == a.idx else b
        loser_seg = a if loser == a.idx else b

        # 关键修正：若 loser 整段文案都被 winner 覆盖（重录冗余），整段删除，
        # 避免只裁重叠窗而残留开头/结尾的小尾巴导致内容重复。
        if (loser_seg.script_start >= winner_seg.script_start
                and loser_seg.script_end <= winner_seg.script_end):
            ld = loser_seg.duration or (loser_seg.words[-1].end if loser_seg.words else 0.0)
            loser_trims[loser].append((0.0, ld))
            note = (f"段{loser} 整段被段{winner}覆盖（文案 {loser_seg.script_start}-{loser_seg.script_end} "
                    f"⊆ {winner_seg.script_start}-{winner_seg.script_end}）→ 整段删除（重录冗余）")
        else:
            note = (f"重叠区「{script_tokens[ov_s:ov_e][:12]}…」(文案 {ov_s}-{ov_e})："
                    f"段{a.idx}分={sa:.2f}{da} vs 段{b.idx}分={sb:.2f}{db} → "
                    f"保留段{winner}，删除段{loser}的重叠部分")
            # 计算 loser 在该重叠区的时间区间，记录待裁剪
            t0, t1 = script_span_time(loser_seg, ov_s, ov_e)
            if t1 > t0:
                loser_trims[loser].append((t0, t1))
        decisions.append(OverlapDecision(a.idx, b.idx, (ov_s, ov_e), winner, loser, sa, sb, note))

    # 构建每段保留区间：先整段，再挖掉 loser 重叠区 + 长停顿
    clips: List[KeepClip] = []
    report_lines = ["=== 选段决策报告 ===", f"文案 token 数：{len(script_tokens)}", ""]
    for seg in segments:
        trims = sorted(loser_trims[seg.idx])
        # 在 [0, duration] 内挖掉 trims，得到 keep 区间
        bounds = [(0.0, seg.duration or (seg.words[-1].end if seg.words else 0.0))]
        for (t0, t1) in trims:
            nb = []
            for (a0, a1) in bounds:
                if t1 <= a0 or t0 >= a1:
                    nb.append((a0, a1))          # 不相交，保留
                else:
                    if t0 > a0:
                        nb.append((a0, t0))       # 重叠前段保留
                    if t1 < a1:
                        nb.append((t1, a1))       # 重叠后段保留
            bounds = nb
        # 段内长停顿切割
        final_bounds = []
        for (a0, a1) in bounds:
            final_bounds.extend(_cut_long_pauses(seg, a0, a1, cfg))
        final_bounds = [(s, e) for (s, e) in final_bounds if e - s > 0.25]
        for (s, e) in final_bounds:
            reason = "保留" if not trims else "保留(已去除重叠重录区)"
            clips.append(KeepClip(seg.file, seg.idx, round(s, 3), round(e, 3), reason))
        report_lines.append(f"段{seg.idx} [{seg.file}]：覆盖文案 {seg.script_start}-{seg.script_end}"
                             f" → 保留 {len(final_bounds)} 段"
                             + (f" {[(round(s,2),round(e,2)) for s,e in final_bounds]}" if final_bounds else "（整段删除！）"))

    for d in decisions:
        report_lines.append(d.note)
    report_lines.append("")
    report_lines.append(f"合计保留 {len(clips)} 个片段，按录制顺序拼接。")
    return clips, decisions, "\n".join(report_lines)


def _cut_long_pauses(seg: Segment, a0: float, a1: float, cfg: dict) -> List[Tuple[float, float]]:
    """在 [a0,a1] 内，把词间 > long_pause_sec 的空隙切掉（保留 pause_pad 余量）。"""
    words = [w for w in seg.words if a0 - 0.01 <= w.start <= a1 + 0.01]
    if len(words) < 2:
        return [(a0, a1)]
    out = []
    cur_s = a0
    for k in range(1, len(words)):
        gap = words[k].start - words[k - 1].end
        if gap > cfg["long_pause_sec"]:
            end = words[k - 1].end + cfg["pause_pad_sec"]
            if end > cur_s + 0.25:
                out.append((cur_s, end))
            cur_s = words[k].start - cfg["pause_pad_sec"]
    out.append((cur_s, a1))
    return [(s, e) for (s, e) in out if e - s > 0.25]
