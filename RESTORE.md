# 如何还原到新位置

## 一、最快路径（换账号/换仓库名）

1. 新建仓库，把本目录全部文件推到 `main`（保留 `.github/workflows/` 目录结构）
2. 按下表补 **3 个 Secrets**（见 `SETTINGS.md`；纯日文 ASR，不允许任何 LLM 网关 key）
3. 改一处代码：`subtitle_dispatch.py` 里的
   ```python
   REPO = "maddertam-oss/cloudtransfer"
   ```
   改成 `REPO = "<新owner>/<新repo>"`
4. 同样改 `subtitle_progress.py` 里的仓库地址（`gh_runs()` 里的 URL）
5. **先别启用 workflow** —— 新加的 workflow 文件默认是 `active`，
   一启用 `subtitle-dispatch` 的 cron 就会开始派发批次。
   确认 `RCLONE_CONF` / `PATHS_JSON` / `GH_TOKEN` 就位再启用。

启用顺序：
```bash
# 1) 先跑一次质量校验（只打日志）
gh workflow run subtitle-verify.yml
# 2) 开始识别（只出日文 srt，不翻译、不调 LLM、不烧录）
gh workflow enable subtitle-dispatch.yml
```
> 烧录阶段已取消，没有第 3 步。

## 二、必须改的硬编码（3 处）

| 文件 | 位置 | 当前值 |
|---|---|---|
| `subtitle_dispatch.py` | `REPO = ...` | `maddertam-oss/cloudtransfer` |
| `subtitle_progress.py` | `gh_runs()` 里的 API URL | 同上 |
| `subtitle_translate.py` / `subtitle_progress.py` / `verify_srt.py` | `cfg("SUB_DIR", ...)` 默认值 | `out5`（仅默认值，实际以 `PATHS_JSON` 为准） |

## 三、当前流程形态（2026-11-21 定版：纯日文 ASR）

- **单阶段（纯识别）**：下载 → 拼接 → whisper/kotoba **日文**转写 → 按标点重切（≤8s/≤42字）→ 逐片上传**日文同名 srt**。
  `TRANSLATE=off` 且 `ASR_FIX=off` —— **整条链路零 LLM 调用**（不发 `/chat/completions`，不读 `SUBTITLE_API_*`）。
- **srt 与视频同名**：`X.part002_D.mp4` → `X.part002_D.srt`；多分片组每片各一份，
  时间轴按各片时长切分（片内相对时间）。组内所有分片都有同名 srt 才算完成。
- **不烧录**：`subtitle-burn.yml` 已删除；`BURN_VIDEO = False`；
  `--burn-only` 会 `SystemExit`；dispatch 收到 `PHASE=burn` 直接退出。
- **无 Telegram**：所有报告只打进 run 日志。
- **翻译质量档**：已废弃 —— `QUALITY` / 采样 / 补译等中译逻辑保留在代码里但不再执行（`TRANSLATE=off`）。
- **源**：`BAT_PATHS_JSON = {"SUB_SRC":"gd2","SUB_DIR":"out5"}`。

## 四、依赖版本（别乱升）

```
av==18.1.0
ctranslate2==4.8.5
faster-whisper==1.2.1
```
历史上 `faster-whisper` 最新版与 `av` API 漂移会报
`open() got an unexpected keyword argument 'metadata_errors'`，所以锁死。

## 五、两个已知坑

1. **`BatchedInferencePipeline` 在 `vad_filter=False` 且无 `clip_timestamps` 时会 RuntimeError**
   —— 所以"无 VAD 全量回退"必须走单条 `model.transcribe`（代码已处理）。
2. **GitHub cron 不守时**（实测可数小时不响），所以 `subtitle-translate.yml` 末尾有
   "自接力"：跑完立刻 `sleep $((RANDOM % 25))` 再跑一次 dispatcher。
   只有 cron 的话会大段空转。
