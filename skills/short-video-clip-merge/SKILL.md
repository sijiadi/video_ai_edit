---
name: short-video-clip-merge
description: 文案驱动的多段口播视频自动拼接——输入一段定稿文案 + 多个重复录制的口播片段，按文案叙事顺序匹配、逐句选最优 take、裁掉口误/虚词/重复段、正脸筛选、处理衔接，必要时标注缺失或不适配素材，输出合并成片。当用户说"帮我合并短视频片段""拼接短视频""把几段口播合成一条""按文案拼视频""合并重复录制的片段"或提供文案 + 多个 .MOV/.mp4 片段要求合并时使用。若用户同时提供了自己认可的手动剪版/完整成片，先走 Step 0 验证，合格则直接转码，绝不重造。
agent_created: true
version: 0.1.0
---

# 短视频片段拼接（short-video-clip-merge）

把一段定稿文案 + 多个重复录制的口播片段，拼成一条完整、正脸、不重复的成片。

## 何时使用 / 何时不用

| 场景 | 用不用 |
|---|---|
| 一段文案录了多段/多遍，同一句反复录，要拼成一条 | ✅ 本技能 |
| 用户给了自己认可的手动剪版/完整成片 | ✅ 先走 Step 0 验证 → 合格直接转码 |
| 单段视频去掉开头点手机动作、加封面字幕（无重复 take） | ❌ 用 `video-autocut` |
| 纯文案生成动画（无真人素材） | ❌ 用 `hyperframes` |

## 铁律（血泪教训，违反必错）

0. **先问自己：有没有用户认可的完整成片？** 有就走 Step 0 验证。若它完整、正脸、无重复、开头动态 → **直接转码交付，禁止用自动选段重造**。2026-11-27 因忽视此条，对着手动版仍走自动选段，连错两轮，引入 6 类问题（吞字、重复、多字、截断、侧脸、静止封面）。
1. **按「句」选段，不按「段」选。** 同一句跨多段、非相邻重复时，段级选优（`segment_selector.select()`）无能为力。
2. **吞字的真正根因是切片只取「匹配到的词」区间**，不是覆盖率低。定位到句子后必须把 t0/t1 **按停顿边界扩展到完整语音段**，否则 coverage 0.5 的句子只剪进半句画面+音频。
3. **coverage 不能当完整性门槛。** 用户口播与文案措辞有出入（"删了会有什么具体损失" vs "删掉它会带来的损失是什么"），逐字覆盖率必然低。coverage 只作定位门槛（≥0.30）。
4. **候选必须正脸看镜头。** 音频质量再好，侧脸 take 也要淘汰。macOS Vision 测 yaw<0.20 / pitch<0.25，3 帧至少 2 帧通过；roll（歪头）不算侧脸，不设硬门槛。OpenCV 在本机损坏（缺 libtesseract），本技能自带 Swift Vision 脚本。
5. **缺素材标出，不硬剪。** 无达标候选 → 标 `missing`，字幕也剔除该句（避免字幕悬空），报告里列出，让用户决定补录或接受删句。
6. **跨句去重。** 相邻句常取同一语音段（重叠 5s+），合并时 `c.t0 <= last.out + 0.40` 就算连续/重叠，out 取 max，只取一次，否则成片重复、时长虚高。
7. **`-ss` 必须在 `-i` 前。** 切片命令若把 `-ss` 放 `-i` 后，`afade` 作用于完整输入，淡出后整段音频永久静音——曾导致成片只有前 24s 有声。
8. **转写模型分工：定位用 small，字幕/选段基准用 medium。** medium 会把口误平滑掉；但 medium 也有词级时间戳缺失（text 字段有、words 数组没有），遇到关键句丢词时以 text 粗定位 + 抽帧验证兜底。
9. **拼接用 `concat_filtergraph`（单次解码+编码），每 10 片分层。** 禁止 `concat demuxer + -c copy`（边界黑帧/爆音）。
10. **短句兜底。** "第二，删除"这类短 anchor 被 whisper 合并进邻句，全局 difflib 对不上 → 用 `fallback_short()` 在本段单独模糊匹配。
11. **字幕文字以定稿文案为准，whisper 只借时间轴。** 用户"先不需要加字幕"时设 `no_subs: true`，本技能不烧字幕。
12. **封面 2.5s 前置时，字幕必须 +cover_sec 偏移**，否则首条字幕叠在封面、全程提前。`no_cover: true` 时不加封面。

## 工作流

### Step 0 — 完整源门控（若用户提供手动剪版/完整成片）
`python scripts/check_source.py --job job.json`
- 验证：锚点覆盖 ≥90%、无重复锚点、无 >1.5s 长停顿、首词 <1s（开头非静止）、正脸。
- 全部通过 → `python scripts/transcode_source.py --job job.json`，交付 `final.mp4`，**结束**。
- 不通过 → 看 `source_check.md` 的失败项，再决定是否走自动选段。

### Step 1 — 建 job 目录与配置
建 `<job_dir>/`，写 `script.txt`（定稿文案，保留【钩子】等段落标签）和 `job.json`：

```json
{
  "script": "/abs/path/script.txt",
  "videos": ["/abs/path/frag1.MOV", "/abs/path/frag2.MOV"],
  "out_dir": "/abs/path/job_dir",
  "complete_source": "/abs/path/手动版.MOV",
  "words_model": "medium",
  "no_face": false,
  "no_cover": true,
  "no_subs": true,
  "title_rows": [],
  "text_fixes": {"卡主": "卡住"}
}
```

- `videos` 按录制顺序排列（文件名时间戳递增即顺序）。
- `complete_source` 可选；无则删掉该字段。
- 用户说"先不需要加字幕" → `no_subs: true, no_cover: true`。

### Step 2 — 词级转写（前台慢，必须后台跑）
```bash
HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 \
  <python> scripts/transcribe_words.py <job_dir>/words_medium medium <videos...>
```
Python 用任一装了 faster-whisper / imageio-ffmpeg 的解释器（`python3`）。4 个文件约 4.5 分钟，后台运行。

### Step 3 — 逐句选段
`python scripts/select_takes.py --job job.json`
产出 `selection.json`（逐句决策+候选+原因）、`selection_report.md`。**必须人工抽查报告**：确认用户点名的句子选了正脸 take，确认 missing 句确实缺素材而非误判。

### Step 4 — 拼接成片
`python scripts/assemble.py --job job.json`
切片 → 分层 concat → （可选字幕）→ （可选封面）→ `final.mp4` + `report.txt`。

### Step 5 — 自检与交付
- 读 `report.txt`：无黑帧、无 >0.15s 静音吞字、30fps/1080×1920、音轨存在。
- 抽 3-5 帧送 `scripts/facebatch.swift` 确认正脸。
- 有 `complete_source` 时，对比成片与其转写文本相似度。
- `present_files` 交付 `final.mp4` + 报告。

## 输入要求（必给 / 按需）

**必给**
1. 定稿文案（可带段落标签）。
2. 视频片段路径（≥1，按录制顺序）或完整成片路径。
3. 是否要字幕/封面（无明示则默认 `no_subs: false, no_cover: false`，用户说"先不要字幕"则都设 true）。

**按需**
- 手动剪版（ground truth，用于 Step 0 和最终验收）。
- 封面标题行 `title_rows`、字幕笔误修正 `text_fixes`、正脸阈值（默认 yaw<0.20/pitch<0.25）。

## 输出格式

| 文件 | 内容 |
|---|---|
| `final.mp4` | 成片，1080×1920/30fps/H.264+AAC |
| `selection.json` / `selection_report.md` | 逐句选段决策、候选、正脸分数、missing 清单 |
| `report.txt` | 自检结果（黑帧/静音/规格/相似度） |
| `source_check.json` / `.md`（Step 0） | 完整源验证结论 |
| `subtitles.srt` / `.ass`（若开字幕） | 字幕文件 |

## 环境

- Python：`python3`（需装 `faster-whisper` 与 `imageio-ffmpeg`）
- 转写前必设 `HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1`（否则 HF 502）。
- 正脸检测：`swift scripts/facebatch.swift <list.json>`，macOS Vision，输入图片路径数组，输出 TSV。
- 本技能脚本已 vendoring 到 `scripts/`，不依赖工作区绝对路径。
