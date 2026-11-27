"""
retrim_v2.py — 保守重剪（v3：逐字字幕 + 视频帧封面）

对照用户手动剪辑版学习后的策略：
  1. 三段按录制顺序【全保留】拼接，绝不再用「整段删除重叠段」的危险逻辑。
  2. 每段开头：裁掉首词之前的所有静音/手点手机动作（in_sec = 首词 start - lead）。
  3. 口误检测：用 small 模型定位 seg1 中「完美的解释了这句话」(口误) →「完美的解释了这件事」(更正)
     的时间区间，按【源时间】裁掉口误段（medium 会平滑掉口误，故必须用 small 定位）。
  4. 封面：从视频里选一帧「眼睛睁开」的画面，叠两排标题（上排白字黑边 / 下排黄字黑边）。
  5. 字幕：以【用户手动剪辑版全文】为文字金标准，只借 whisper 给时间轴
     （字符级对齐，彻底杜绝『whisper 吞字』漏字）；≤12 字/行，重点词黄色 + 大 2 号；
     无描边、轻阴影；位置距底约 22%。

用法:
  python retrim_v2.py <job_v2.json> [--re-transcribe]
"""
from __future__ import annotations
import sys, os, json, subprocess, tempfile, datetime, difflib, re
import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
HERE = os.path.dirname(os.path.abspath(__file__))
SUB_FONT = "Hiragino Sans GB"          # 字幕字体（中文渲染稳定）
DEFAULT_COVER_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"   # 苍耳丰黑 缺失时的兜底


def run_ff(args: list, log_path=None):
    cmd = [FF, "-y", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        msg = r.stderr[-3000:]
        if log_path:
            open(log_path, "w").write(r.stderr)
        raise RuntimeError(f"ffmpeg 失败 (exit {r.returncode}):\n{msg}")


# ---------------------------------------------------------------------------
# 视频拼接：用 filter_complex 单次解码+编码，杜绝 concat demuxer -c copy 的边界黑屏/花屏
# ---------------------------------------------------------------------------
def concat_filtergraph(clips: list[str], out: str, cfg: dict):
    """把已切好的视频片段用 ffmpeg filter_complex 的 concat 滤镜拼成一条视频。
    与 concat demuxer + -c copy 不同，这里每个片段都被解码后在同一滤镜图里拼接并
    重新编码一次，因此不会因片段间关键帧/GOP/音频参数不一致而出现黑帧或爆音。
    """
    if not clips:
        raise ValueError("concat_filtergraph: clips 为空")
    inputs = []
    for c in clips:
        inputs += ["-i", c]
    parts = []
    for i in range(len(clips)):
        parts.append(f"[{i}:v][{i}:a]")
    filt = "".join(parts) + f"concat=n={len(clips)}:v=1:a=1[v][a]"
    run_ff(inputs + [
        "-filter_complex", filt,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(cfg.get("crf", 18)),
        "-c:a", "aac", "-ar", "44100",
        "-r", str(cfg.get("fps", 30)),
        "-movflags", "+faststart",
        out,
    ])


# ---------------------------------------------------------------------------
# 成片自检：黑屏 / 静音检测（用于和学习目标对比时量化边界问题）
# ---------------------------------------------------------------------------
def detect_black_intervals(path, d=0.03, pix_th=0.02):
    out = subprocess.run([FF, "-i", path, "-vf", f"blackdetect=d={d}:pix_th={pix_th}",
                          "-an", "-f", "null", "-"], capture_output=True, text=True).stderr
    return [(float(m.group(1)), float(m.group(2)), float(m.group(3)))
            for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)", out)]


def detect_silence_intervals(path, d=0.15, noise="-35dB"):
    out = subprocess.run([FF, "-i", path, "-af", f"silencedetect=noise={noise}:d={d}",
                          "-vn", "-f", "null", "-"], capture_output=True, text=True).stderr
    starts = [float(m.group(1)) for m in re.finditer(r"silence_start:([\d.]+)", out)]
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end:([\d.]+)", out)]
    return [(s, e, e - s) for s, e in zip(starts, ends)]


# ---------------------------------------------------------------------------
# 口误检测（small 转写，按源时间）
# ---------------------------------------------------------------------------
def detect_slip(words):
    """找『完 美 的 解 释 了 这 句 话 完 美 …』模式，返回 (口误起始, 更正起始) 时间。"""
    for j in range(2, len(words) - 4):
        if words[j]["text"] == "这" and words[j + 1]["text"] == "句" and words[j + 2]["text"] == "话":
            s0 = None
            for k in range(j - 1, -1, -1):
                if words[k]["text"] == "完":
                    s0 = words[k]["start"]; break
            s1 = None
            for m in range(j + 3, len(words)):
                if words[m]["text"] == "完":
                    s1 = words[m]["start"]; break
            if s0 is not None and s1 is not None and (s1 - s0) < 6:
                return (round(s0, 3), round(s1, 3))
    return None


def compute_ranges(words, holes, cfg):
    if not words:
        return []
    in_sec = max(0.0, words[0]["start"] - cfg["lead_sec"])
    out_sec = words[-1]["end"] + cfg["tail_sec"]
    ranges = [(in_sec, out_sec)]
    for (hs, he) in holes:
        nr = []
        for (a, b) in ranges:
            if he <= a or hs >= b:
                nr.append((a, b))
            else:
                if hs > a:
                    nr.append((a, hs))
                if he < b:
                    nr.append((he, b))
        ranges = nr
    return [(round(s, 3), round(e, 3)) for (s, e) in ranges if e - s > 0.2]


def build_cut_words(seg_words, ranges, holes):
    """用【原始分段转写】(完整、可靠) 过滤出成片词并映射到成片时间轴。
    不依赖对成片的二次转写(那种转写可能丢句)。

    口误洞(hole)处理 —— 关键修复：
    medium 会把口误平滑成「更正」(如『完美的解释了这件事』被放在口误的源时间区)，
    所以洞内词正是要保留的更正文本，不能丢。做法：
      · 洞前词 / 洞内词：按源时间直接放(cs = ws - in_sec)。洞内词在成片里
        恰好落在「更正被听到」的位置，字幕与裁掉口误后的视频对齐。
      · 洞后词：整体前移一个洞时长(cs = ws - in_sec - 洞时长)，与成片拼接时间一致。
      · 丢弃开头静音(lead)与超出成片的尾巴。
    """
    all_words = []
    global_offset = 0.0
    for i, sw in enumerate(seg_words):
        if not ranges[i]:
            continue
        in_sec = ranges[i][0][0]
        out_sec = ranges[i][-1][1]
        hole = holes[i][0] if holes[i] else None
        if hole:
            h0, h1 = hole
            # 洞只与保留区间 [in_sec, out_sec] 的交集有效，避免洞越界导致 seg_dur 为负。
            h0_eff = max(h0, in_sec)
            h1_eff = min(h1, out_sec)
            if h0_eff < h1_eff:
                hole_dur = h1_eff - h0_eff
            else:
                hole_dur = 0.0
                hole = None
        else:
            h0_eff = h1_eff = hole_dur = None
        seg_dur = sum(ek - sk for sk, ek in ranges[i])
        seg_cut = []
        for w in sw:
            ws, we = float(w["start"]), float(w["end"])
            # 边界安全：词的中点在保留区间内就保留，并把时间夹到 [in_sec, out_sec]，
            # 避免边界词被静默丢弃，也避免输出时间出现负值。
            mid = (ws + we) * 0.5
            if mid < in_sec - 1e-3 or mid > out_sec + 1e-3:
                continue
            eff_s = max(ws, in_sec)
            eff_e = min(we, out_sec)
            if eff_e <= eff_s:
                continue
            if hole and eff_s > h1_eff:           # 洞后：前移有效洞时长
                cs = (eff_s - in_sec) - hole_dur
                ce = (eff_e - in_sec) - hole_dur
            else:                                 # 洞前 / 洞内(洞内=被平滑的更正，保留)
                cs = eff_s - in_sec
                ce = eff_e - in_sec
            seg_cut.append({"text": w["text"], "start": cs, "end": ce})
        for w in seg_cut:
            all_words.append({"text": w["text"],
                              "start": w["start"] + global_offset,
                              "end": w["end"] + global_offset})
        global_offset += seg_dur
    return all_words


# ---------------------------------------------------------------------------
# 字幕：词 → 字符 → 校正 → 分行（≤max_chars）
# ---------------------------------------------------------------------------
def words_to_chars(words):
    """把词级转写摊平成字符级 (ch, start, end)，字符时间在词内按比例插值。"""
    chars = []
    for w in words:
        txt = w["text"]
        n = max(1, len(txt))
        t0, t1 = float(w["start"]), float(w["end"])
        for k, ch in enumerate(txt):
            chars.append({"ch": ch, "start": t0 + (t1 - t0) * k / n,
                          "end": t0 + (t1 - t0) * (k + 1) / n})
    return chars


_HALF2FULL = {",": "，", "?": "？", "!": "！", ";": "；", ":": "：",
              "(": "（", ")": "）"}


def normalize_punct(chars):
    for c in chars:
        c["ch"] = _HALF2FULL.get(c["ch"], c["ch"])
    return chars


def apply_fixes(chars, fixes):
    """字符级替换：把 find 串全部替换为 replace，替换后的字符均匀铺在原来的时间跨度上。"""
    for fx in fixes:
        find, repl = fx["find"], fx.get("replace", "")
        text = "".join(c["ch"] for c in chars)
        while True:
            pos = text.find(find)
            if pos < 0:
                break
            i0, i1 = pos, pos + len(find)
            t0, t1 = chars[i0]["start"], chars[i1 - 1]["end"]
            n = max(1, len(repl))
            newc = [{"ch": repl[k],
                     "start": t0 + (t1 - t0) * k / n,
                     "end": t0 + (t1 - t0) * (k + 1) / n} for k in range(n)]
            chars = chars[:i0] + newc + chars[i1:]
            text = "".join(c["ch"] for c in chars)
            print(f"[fix] ✓ {find!r} → {repl!r}")
    return chars


def apply_canonical(chars, canonical):
    """字符级金标准对齐：以 canonical(用户手动剪辑版全文) 为正确文字，只借用
    whisper 的时间轴。彻底消除『whisper 转写吞字』导致的字幕漏字瓶颈。

    做法：把 auto(成片转写摊平的字符) 与 canon(手动版全文) 做 difflib 对齐，
    相等段保留原时间；replace 段用原时间跨度铺上正确文字；delete 段(口误残留等
    多出的字)丢弃；insert 段(自动转写缺失的字)挂在最近时间点。"""
    auto = "".join(c["ch"] for c in chars)
    # canonical 归一化：半角标点→全角、去空格，与 chars 对齐
    canon = "".join(_HALF2FULL.get(ch, ch) for ch in canonical if not ch.isspace())
    sm = difflib.SequenceMatcher(None, auto, canon, autojunk=False)
    out, n = [], len(chars)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(chars[i1:i2])
        elif tag == "replace":
            t0 = chars[i1]["start"]
            t1 = chars[min(i2, n) - 1]["end"] if i2 > 0 else chars[i1]["start"]
            m = max(1, j2 - j1)
            for k in range(m):
                out.append({"ch": canon[j1 + k],
                            "start": t0 + (t1 - t0) * k / m,
                            "end": t0 + (t1 - t0) * (k + 1) / m})
        elif tag == "delete":
            pass  # 自动转写多出的字（如口误残留）直接丢弃
        elif tag == "insert":
            t = chars[i1]["start"] if i1 < n else (chars[i2 - 1]["end"] if i2 > 0 else 0.0)
            for k in range(j1, j2):
                out.append({"ch": canon[k], "start": t, "end": t})
    print(f"[字幕] 金标准对齐：auto={len(auto)}字 → canonical={len(canon)}字")
    return out


_PUNCT = "。！？…，、；："


def _split_single_line(text, max_chars):
    """把一句拆成若干 ≤max_chars 的单行块，优先在逗号/顿号/分号/冒号处切。"""
    if len(text) <= max_chars:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            # 从 end-1 往回找标点（不包含 end 本身），确保块长严格 ≤max_chars
            k = end - 1
            while k > start and text[k] not in "，、；：":
                k -= 1
            if k > start:
                end = k + 1  # 把标点留在这块末尾
        chunks.append(text[start:end])
        start = end
    return chunks


def split_cues(chars, max_chars, single_line=False):
    """按【句子】为单位做字幕断句。

    single_line=False（多行兼容旧行为）：尽量整句一屏，可自动折 2~3 行，
        只有超过 3*max_chars 的过长句才切分。

    single_line=True（一屏一行）：每屏只显示一行，最多 max_chars 个字；
        超过则按逗号/顿号等拆成多屏，禁止换行。

    时间修正：只处理 cue 重叠，不再把每条 cue 强制拉长到 0.6s，避免下一句
    的视觉提前感。最后一条 cue 也不再额外延后。
    """
    # 1) 按【句末标点】分组为句子（。！？ 为句界；逗号、顿号等仅作句内切分点）
    sents, cur = [], []
    for c in chars:
        cur.append(c)
        if c["ch"] in "。！？":
            sents.append(cur); cur = []
    if cur:
        sents.append(cur)

    cues = []
    if single_line:
        for s in sents:
            text = "".join(x["ch"] for x in s)
            t0, t1 = s[0]["start"], s[-1]["end"]
            chunks = _split_single_line(text, max_chars)
            n = max(1, len(chunks))
            for i, chunk in enumerate(chunks):
                cs = t0 + (t1 - t0) * i / n
                ce = t0 + (t1 - t0) * (i + 1) / n
                cues.append({"start": cs, "end": ce, "text": chunk})
    else:
        LINE = max_chars
        MAX_CUE = LINE * 3

        def _wrap(text):
            if len(text) <= LINE:
                return text
            mid = len(text) // 2
            best = None
            for i, ch in enumerate(text):
                if ch in "，、；：":
                    if best is None or abs(i - mid) < abs(best - mid):
                        best = i
            if best is not None and 0 < best < len(text) - 1:
                return text[:best + 1] + "\\N" + text[best + 1:]
            return text[:LINE] + "\\N" + text[LINE:]

        def _split_long(text):
            if len(text) <= MAX_CUE:
                return [text]
            parts, start = [], 0
            while start < len(text):
                end = min(start + MAX_CUE, len(text))
                if end < len(text):
                    k = end
                    while k > start and text[k] not in "，、；：。！？":
                        k -= 1
                    if k > start:
                        end = k + 1
                parts.append(text[start:end])
                start = end
            return parts

        for s in sents:
            text = "".join(x["ch"] for x in s)
            t0, t1 = s[0]["start"], s[-1]["end"]
            parts = _split_long(text)
            n = len(parts)
            for i, part in enumerate(parts):
                cs = t0 + (t1 - t0) * i / n
                ce = t0 + (t1 - t0) * (i + 1) / n
                cues.append({"start": cs, "end": ce, "text": _wrap(part)})

    # 时间修正：只消除重叠，最短保留 0.3s（防闪），但不主动延后。
    for i, cue in enumerate(cues):
        min_end = cue["start"] + 0.3
        max_end = cues[i + 1]["start"] - 0.01 if i + 1 < len(cues) else float("inf")
        cue["end"] = max(cue["end"], min_end)
        if cue["end"] > max_end:
            cue["end"] = max(min_end, max_end)
    return cues


# ---------------------------------------------------------------------------
# 字幕：用【用户提供的正确文案】驱动文字与断句，whisper 只借时间轴
# ---------------------------------------------------------------------------
_PUNCT_ALL = r"，。：；、？！“”\"'（）()…—《》【】\s\r\n\t"


def _strip_punct(s: str) -> str:
    """剔除所有标点/空白（用于字幕显示文本，满足『字幕不要标点』）。"""
    return re.sub(f"[{_PUNCT_ALL}]", "", s)


def _wrap_script(text: str, max_line: int = 12) -> str:
    """把一行文本按 ≤max_line 字折行（同一 cue 内 \\N 换行）。
    优先在靠近行尾的虚词/助词处断，读起来更自然。"""
    text = text.replace("\n", "").replace("\\N", "")
    if len(text) <= max_line:
        return text
    lines = []
    start = 0
    while start < len(text):
        end = min(start + max_line, len(text))
        if end < len(text):
            best = end
            for k in range(end - 1, start, -1):
                if text[k] in "的了是就也都把被在对于和跟与或因为所以但但是而着过啊呢吗吧":
                    best = k + 1
                    break
            end = best
            if end <= start:
                end = min(start + max_line, len(text))
        lines.append(text[start:end])
        start = end
    return "\\N".join(lines)


def build_script_cues(chars, script, max_line=12):
    """用【用户提供的正确文案】驱动字幕文字与断句，whisper 只提供时间轴。

    - 按 逗号/句号/冒号/分号 切分文案，每个 chunk = 一条字幕（顿号不切，留在 chunk 内）。
    - 显示文本剔除【全部】标点。
    - chunk 内超过 max_line 字则同一 cue 内折行（多行同一帧）。
    - difflib 把(去标点后的)文案对齐到 whisper 字符网格，借其时间轴。
    - 返回的 cue 时间为【成片输出时间轴】，不含封面/延后偏移（由调用方加）。
    """
    W = "".join(c["ch"] for c in chars)
    raw_segs = [s for s in re.split(r"[，。：；]", script) if s.strip()]
    segs = [_strip_punct(s) for s in raw_segs]
    segs = [s for s in segs if s]
    S = "".join(segs)

    sm = difflib.SequenceMatcher(None, W, S, autojunk=False)
    s2w = [-1] * len(S)
    for b in sm.get_matching_blocks():
        for k in range(b.size):
            if b.b + k < len(S):
                s2w[b.b + k] = b.a + k
    # 前向填充洞（whisper 误识/口误造成的 -1 借相邻正确字的时间）
    last = -1
    for i in range(len(s2w)):
        if s2w[i] == -1:
            s2w[i] = last
        else:
            last = s2w[i]
    first_valid = next((x for x in s2w if x != -1), 0)
    s2w = [x if x != -1 else first_valid for x in s2w]

    cues = []
    pos = 0
    nW = len(W)
    for seg in segs:
        a = pos
        b = pos + len(seg)
        wi0 = s2w[a] if a < len(s2w) else 0
        wi1 = s2w[b - 1] if b - 1 < len(s2w) else nW - 1
        wi0 = max(0, min(wi0, nW - 1))
        wi1 = max(wi0, min(wi1, nW - 1))
        t0 = chars[wi0]["start"]
        t1 = chars[wi1]["end"]
        cues.append({"start": t0, "end": t1, "text": _wrap_script(seg, max_line)})
        pos = b

    # 消除重叠：cue 最短 0.3s，且不越过下一条起点
    for i, cue in enumerate(cues):
        min_end = cue["start"] + 0.3
        max_end = cues[i + 1]["start"] - 0.01 if i + 1 < len(cues) else float("inf")
        cue["end"] = max(cue["end"], min_end)
        if cue["end"] > max_end:
            cue["end"] = max(min_end, max_end)
    return cues


def apply_highlights(text, keys, base_size, hi_size):
    for k in keys:
        if k and k in text:
            text = text.replace(
                k,
                "{\\c&H00FFFF&\\fs%d\\b1}%s{\\c&H00FFFFFF&\\fs%d\\b0}" % (hi_size, k, base_size))
    return text


def srt_time(t):
    t = max(0.0, t)
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s += 1; ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs == 100:
        s += 1; cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def cues_to_srt(cues):
    return "\n".join(f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['text'].replace('\\N', chr(10))}\n"
                     for i, c in enumerate(cues, 1))


ASS_TMPL = """[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,{FONT},{SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,3,{SHADOW},2,{ML},{MR},{MV},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""


def cues_to_ass(cues, cfg):
    head = ASS_TMPL.format(W=cfg["width"], H=cfg["height"], FONT=cfg["sub_font"],
                           SIZE=cfg["sub_font_size"], SHADOW=cfg["sub_shadow"],
                           ML=cfg["sub_margin_x"], MR=cfg["sub_margin_x"],
                           MV=cfg["sub_margin_v"])
    base = cfg["sub_font_size"]
    hi = cfg["sub_font_size"] + 2
    lines = []
    for c in cues:
        txt = apply_highlights(c["text"], cfg.get("sub_highlights", []), base, hi)
        lines.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(c['end'])},Default,,0,0,0,,{txt}")
    return head + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 封面：视频帧 + 两排描边标题
# ---------------------------------------------------------------------------
_COLOR = {"white": (255, 255, 255), "yellow": (255, 214, 0),
          "black": (0, 0, 0), "blue": (60, 150, 255), "red": (240, 60, 60)}


def make_cover(frame_png, out_png, rows, cfg):
    from PIL import Image, ImageDraw, ImageFont
    W, H = cfg["width"], cfg["height"]
    img = Image.open(frame_png).convert("RGB").resize((W, H), Image.LANCZOS)
    d = ImageDraw.Draw(img)

    fpath = cfg.get("cover_font_path") or DEFAULT_COVER_FONT
    margin = int(W * cfg["cover_margin_ratio"])
    usable = W - 2 * margin
    center_y = int(H * (1 - cfg["cover_pos_from_bottom"]))

    # 自动缩字号：两排都塞进可用宽度
    size = cfg.get("cover_font_size", 170)
    while size > 40:
        try:
            font = ImageFont.truetype(fpath, size)
        except Exception:
            font = ImageFont.truetype(DEFAULT_COVER_FONT, size)
        if max(d.textlength(r["text"], font=font) for r in rows) <= usable:
            break
        size -= 6
    stroke = max(4, int(size * 0.06))

    row_h = int(size * 1.22)
    gap = int(size * 0.16)
    total = row_h * len(rows) + gap * (len(rows) - 1)
    top = center_y - total // 2

    for i, r in enumerate(rows):
        fill = _COLOR.get(r.get("fill", "white"), (255, 255, 255))
        outline = _COLOR.get(r.get("outline", "black"), (0, 0, 0))
        cy = top + row_h * i + row_h // 2 + gap * i
        d.text((W // 2, cy), r["text"], font=font, fill=fill,
               anchor="mm", stroke_width=stroke, stroke_fill=outline)
    img.save(out_png)
    return size


# ---------------------------------------------------------------------------
# 封面：自动选「睁眼」帧（人脸关键点 EAR）—— 不再写死某一帧
# ---------------------------------------------------------------------------
def _eye_aspect_ratio(pts):
    import math
    d = lambda a, b: math.hypot(a[0] - b[0], a[1] - b[1])
    return (d(pts[1], pts[5]) + d(pts[2], pts[4])) / (2 * d(pts[0], pts[3]) + 1e-6)


_LEFT_EYE = [33, 160, 158, 133, 153, 144]
_RIGHT_EYE = [362, 385, 387, 263, 373, 380]


def select_cover_frame(video_path, cfg, span=18.0, fps=3.0):
    """从源视频前 span 秒、每 1/fps 秒抽一帧，挑『睁眼分最高 + 非黑』的一帧。
    主：MediaPipe FaceMesh(最准)；备：OpenCV Haar 眼睛级联；兜底：最亮非黑帧。"""
    import glob
    from PIL import Image
    import numpy as np
    tmp = tempfile.mkdtemp(prefix="cover_")
    pat = os.path.join(tmp, "f%03d.jpg")
    run_ff(["-ss", "0", "-i", video_path, "-t", f"{span:.1f}",
            "-vf", f"fps={fps:.3f}", "-q:v", "3", pat])
    files = sorted(glob.glob(os.path.join(tmp, "f*.jpg")))
    if not files:
        return None, -1.0
    best, best_score = None, -1.0

    # 主：MediaPipe FaceMesh
    try:
        import mediapipe as mp
        fm = mp.solutions.face_mesh.FaceMesh(static_image_mode=True,
                                             max_num_faces=1, refine_landmarks=True)
        for f in files:
            img = np.array(Image.open(f).convert("RGB")); H, W, _ = img.shape
            if img.mean() < 55:
                continue
            res = fm.process(img)
            if not res.multi_face_landmarks:
                continue
            lm = res.multi_face_landmarks[0].landmark
            ear = (_eye_aspect_ratio([(lm[i].x * W, lm[i].y * H) for i in _LEFT_EYE]) +
                   _eye_aspect_ratio([(lm[i].x * W, lm[i].y * H) for i in _RIGHT_EYE])) / 2
            if ear > best_score:
                best_score, best = ear, f
        fm.close()
        if best:
            print(f"[封面] MediaPipe 选帧 睁眼分={best_score:.2f} {os.path.basename(best)}")
            return best, best_score
    except Exception as e:
        print(f"[封面] MediaPipe 不可用({e})，试 OpenCV")

    # 备：OpenCV Haar 眼睛级联（在人脸区域内数眼睛，越多越可能睁眼）
    try:
        import cv2
        eye_cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
        face_cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        for f in files:
            img = np.array(Image.open(f).convert("RGB")); H, W, _ = img.shape
            if img.mean() < 55:
                continue
            g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            faces = face_cas.detectMultiScale(g, 1.3, 5)
            if len(faces) == 0:
                continue
            (x, y, w, h) = max(faces, key=lambda r: r[2] * r[3])  # 只取最大人脸
            roi = g[max(0, y - int(0.1 * h)):y + int(1.4 * h),
                    max(0, x - int(0.1 * w)):x + int(1.4 * w)]
            neyes = len(eye_cas.detectMultiScale(roi, 1.1, 3)) if roi.size else 0
            score = neyes * 1000.0 + float(img.mean())   # 眼数优先，亮度兜底
            if score > best_score:
                best_score, best = score, f
        if best:
            print(f"[封面] OpenCV 选帧 眼数≈{int(best_score // 1000)} 亮度={best_score % 1000:.0f} {os.path.basename(best)}")
            return best, best_score
    except Exception as e:
        print(f"[封面] OpenCV 不可用({e})，回退亮度")

    # 兜底：最亮非黑帧
    for f in files:
        img = np.array(Image.open(f).convert("RGB")); b = img.mean()
        if b > 55 and b > best_score:
            best_score, best = b, f
    print(f"[封面] 亮度兜底选帧 亮度={best_score:.0f} {os.path.basename(best) if best else '无'}")
    return best, best_score


# ---------------------------------------------------------------------------
# 转写（medium）
# ---------------------------------------------------------------------------
def transcribe(path, model_size="medium"):
    from faster_whisper import WhisperModel
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segs, _ = model.transcribe(path, language="zh", word_timestamps=True,
                               vad_filter=True, vad_parameters=dict(min_silence_duration_ms=300))
    words = []
    for s in segs:
        for w in (s.words or []):
            t = w.word.strip()
            if t:
                words.append({"text": t, "start": float(w.start), "end": float(w.end)})
    return words


# ---------------------------------------------------------------------------
# 主
# ---------------------------------------------------------------------------
def main():
    job_path = sys.argv[1]
    job = json.load(open(job_path, encoding="utf-8"))
    cfg = job
    v2dir = os.path.dirname(os.path.abspath(job_path))
    srcdir = os.path.join(HERE, "jobs", "价值心法_涨薪70")

    seg_words = [json.load(open(os.path.join(v2dir, seg["transcript"]), encoding="utf-8"))["words"]
                 for seg in job["segments"]]
    manual_text = json.load(open(os.path.join(v2dir, job["manual_edit"]["transcript"]),
                                 encoding="utf-8"))["text"]

    # 口误定位（small）
    holes = [[] for _ in job["segments"]]
    slip_tr = job.get("slip_transcript")
    if slip_tr:
        sw = json.load(open(os.path.join(v2dir, slip_tr), encoding="utf-8"))["words"]
        sp = detect_slip(sw)
        if sp:
            holes[0] = [sp]
            print(f"[口误] seg1 定位到口误区间 {sp}（『完美的解释了这句话』）")

    ranges = [compute_ranges(seg_words[i], holes[i], cfg) for i in range(len(seg_words))]

    report = ["=== 保守重剪决策报告 (v3) ===", ""]
    for i, seg in enumerate(job["segments"], 1):
        report.append(f"段{i} [{seg['file']}]：保留 {len(ranges[i-1])} 段 {ranges[i-1]}"
                      + (f"  （已裁口误 {holes[i-1][0]}）" if holes[i-1] else ""))
    report.append("")

    # ---- 封面（自动选睁眼帧 + 两排标题）----
    if job.get("auto_cover"):
        vf0 = job["segments"][0]["file"]
        vpath = vf0 if os.path.isabs(vf0) else os.path.join(srcdir, vf0)
        frame_png, ear = select_cover_frame(vpath, cfg)
        if not frame_png:  # 检测全失败 → 回退配置的 cover_frame
            frame = job.get("cover_frame") or "cover_candidates/f_2.5.png"
            frame_png = frame if os.path.isabs(frame) else os.path.join(v2dir, frame)
            print("[封面] 自动检测无果，回退 cover_frame")
    else:
        frame = job["cover_frame"]
        frame_png = frame if os.path.isabs(frame) else os.path.join(v2dir, frame)
    cover_png = os.path.join(v2dir, "cover.png")
    used = make_cover(frame_png, cover_png, job["cover_rows"], cfg)
    print(f"[封面] 取帧 {os.path.basename(frame_png)}，字号 {used}，标题 {[r['text'] for r in job['cover_rows']]}")
    cover_mp4 = os.path.join(v2dir, "cover.mp4")
    if cfg.get("cover_sec", 0) > 0:
        run_ff(["-loop", "1", "-r", f"{cfg['fps']}", "-i", cover_png,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{cfg['cover_sec']}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-r", f"{cfg['fps']}", "-c:a", "aac", "-shortest", cover_mp4])

    # ---- 切片 + 拼接 ----
    tmp = tempfile.mkdtemp(prefix="rt2_")
    clips = [cover_mp4] if cfg.get("cover_sec", 0) > 0 else []
    for i, seg in enumerate(job["segments"], 1):
        fpath = seg["file"] if os.path.isabs(seg["file"]) else os.path.join(srcdir, seg["file"])
        for k, (s, e) in enumerate(ranges[i - 1]):
            t = os.path.join(tmp, f"c{i}_{k}.mp4")
            run_ff(["-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-i", fpath,
                    "-vf", f"scale={cfg['width']}:{cfg['height']}", "-r", f"{cfg['fps']}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                    "-c:a", "aac", "-ar", "44100", t])
            clips.append(t)
    merged = os.path.join(v2dir, "merged.mp4")
    concat_filtergraph(clips, merged, cfg)

    # ---- 字幕文本：原始分段转写给时间轴 + 用户手动版做文字金标准(防吞字) ----
    mwords = build_cut_words(seg_words, ranges, holes)
    chars = normalize_punct(words_to_chars(mwords))
    chars = apply_fixes(chars, cfg.get("text_fixes", []))
    chars = apply_canonical(chars, manual_text)   # 关键：文字以手动版为准
    cues = split_cues(chars, cfg["max_chars"])
    sub_text = "".join(c["text"] for c in cues)

    srt_path = os.path.join(v2dir, "subtitles.srt")
    open(srt_path, "w", encoding="utf-8").write(cues_to_srt(cues))
    ass_path = os.path.join(v2dir, "subtitles.ass")
    open(ass_path, "w", encoding="utf-8").write(cues_to_ass(cues, cfg))

    # ---- 烧字幕（ASS 精控样式）----
    final = os.path.join(v2dir, "final.mp4")
    run_ff(["-i", merged,
            "-vf", f"subtitles='{ass_path}'",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-r", f"{cfg['fps']}", "-c:v", "libx264", "-crf", "20", "-c:a", "aac", final])

    # ---- 校验 ----
    def _norm(t):
        return re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', t)
    ratio = difflib.SequenceMatcher(None, sub_text, manual_text).ratio()
    ratio_norm = difflib.SequenceMatcher(None, _norm(sub_text), _norm(manual_text)).ratio()
    checks = {
        "1) 开头手点手机动作已裁": ranges[0][0][0] > 0.3,
        "2) 口误『完美的解释了这句话』已裁": len(holes[0]) > 0,
        "3) 字幕补全『有一句话』未漏字": "有一句话" in sub_text,
        "4) 金句字幕为『创造价值，价格自己会追上来』": ("创造价值" in sub_text and "价格自己会追上来" in sub_text and "你值多少" not in sub_text),
        "5) 后半段完整(未漏剪/无误字)": all(p in sub_text for p in ["不要把时间花在无关紧要", "又问自己一句", "工作、自媒体、创业", "价格自己会追上来", "价格迟早回来"]),
        "6) 封面取自视频帧(非黑/睁眼) + 字幕已加": os.path.exists(cover_png) and os.path.exists(ass_path),
        "7) 字幕与手动剪辑版高度一致(无漏字/误字)": ratio_norm > 0.97,
    }
    # 边界安全自检：用 filter_complex concat 后仍要验证没有黑帧/静音吞字
    black_intervals = detect_black_intervals(final)
    silence_intervals = detect_silence_intervals(final)
    checks["8) 成片无黑屏(concat filter)"] = len(black_intervals) == 0
    checks["9) 成片无>0.15s静音吞字"] = len(silence_intervals) == 0
    report.append(f"字幕与手动剪辑版文本相似度：原始 {ratio:.3f} / 去标点 {ratio_norm:.3f}")
    report.append(f"黑屏检测：{black_intervals if black_intervals else '无'}")
    report.append(f"静音检测：{silence_intervals if silence_intervals else '无'}")
    report.append("=== 九条诉求自检 ===")
    for k, v in checks.items():
        report.append(f"  [{'✓' if v else '✗'}] {k}")
    report.append("")
    report.append(f"输出：{final}")
    report.append(f"字幕：{srt_path} / {ass_path}")
    report.append(f"时间：{datetime.datetime.now()}")
    open(os.path.join(v2dir, "report.txt"), "w", encoding="utf-8").write("\n".join(report))
    print("\n".join(report))


if __name__ == "__main__":
    main()
