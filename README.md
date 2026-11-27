# 墨迹 · AI 短视频剪辑工作台（Video AI Edit）

一组给 AI 用的短视频剪辑技能。你不需要会剪映，把文案和素材交给 WorkBuddy，说一句话就能出片。

## 它能解决什么问题

- 同一段口播录了好几遍，要按文案自动挑最好的那一遍、拼成一条——这正是当前技能 `short-video-clip-merge` 做的事。
- 后续会逐步加入：字幕添加、封面处理、光线/音量调节。每个能力都是**独立技能**，可单独安装，互不干扰。

## 当前技能

| 技能 | 能力 | 触发词（对着 WorkBuddy 说） | 版本 |
|---|---|---|---|
| [`short-video-clip-merge`](skills/short-video-clip-merge/) | 按文案把多段口播拼成一条：逐句选最优 take、去口误/虚词/重复、正脸筛选、标注缺失素材 | 「帮我合并短视频片段」「按文案拼视频」「把几段口播合成一条」 | v0.1.0 |

规划中：`short-video-subtitle`、`short-video-cover`、`short-video-light`、`short-video-volume`。

## 安装

1. 克隆或下载本仓库（不会 git 的话，直接点右上角 Code → Download ZIP）。
2. 把 `skills/` 下的技能目录复制到你的 WorkBuddy 技能目录：

```bash
# macOS / Linux
cp -R skills/short-video-clip-merge ~/.workbuddy/skills/
```

3. 重启 WorkBuddy 会话，让技能被加载。

## 依赖

| 依赖 | 用途 | 安装 |
|---|---|---|
| Python 3.x | 跑所有脚本 | 系统自带或 python.org |
| `faster-whisper` | 词级转写定位口误 | `pip install faster-whisper` |
| `imageio-ffmpeg` | 定位 ffmpeg | `pip install imageio-ffmpeg` |
| `ffmpeg` / `ffprobe` | 切片、拼接、自检 | macOS: `brew install ffmpeg` |
| macOS Vision | 正脸检测（`facebatch.swift`） | macOS 自带，无需安装 |
| HF 镜像（可选） | 国内下载whisper模型防 502 | `export HF_ENDPOINT=https://hf-mirror.com` `export HF_HUB_DISABLE_XET=1` |

> 非 macOS 用户没有 Vision，可在 `job.json` 设 `no_face: true` 跳过正脸检测。

## 快速开始

给 WorkBuddy 三样东西：

1. **定稿文案**（`script.txt`，保留段落标签）；
2. **视频片段**（按录制顺序排列的路径列表）；
3. 一句话需求，例如：

> 我有一段口播文案和四段重复录制的片段，帮我按文案拼成一条，去掉重复和口误，缺的地方标出来，先不要加字幕。

完整配置示例见 [`examples/job.json`](examples/job.json)。

## 产物

| 文件 | 内容 |
|---|---|
| `final.mp4` | 成片，1080×1920 / 30fps / H.264+AAC |
| `selection.json` / `selection_report.md` | 逐句选段决策、候选、正脸分数、缺失清单 |
| `report.txt` | 自检结果（黑帧/静音/规格） |
| `source_check.json` / `.md` | 完整源验证结论（若提供了手动剪版） |

## 常见问题

- **HF 502**：设置 `HF_ENDPOINT=https://hf-mirror.com` 与 `HF_HUB_DISABLE_XET=1` 后重试。
- **成片没声音**：检查切片命令 `-ss` 是否放在 `-i` 前（放后面会导致音频淡出成静音）。
- **吞字**：切片区间要扩展到完整语音段，不能只取匹配到的词。
- **侧脸入镜**：候选需过正脸检测（yaw<0.20 / pitch<0.25），不合格的换 take。
- **装上不触发**：确认技能目录名、`SKILL.md` 里的 `name`、触发词三处一致。

## 许可

MIT，见 [LICENSE](LICENSE)。随便用、随便改，署名即可。
