# cloudtransfer · 设置清单(只出日文 srt + Telegram 报告)

> 仓库: `tamd258/cloudtransfer-subtitle`(public, default branch `main`)
> 更新时间: 2026-11-19(处理目录切到 `ydy2:五星`, 报告接回 Telegram)

## 一、当前处理目录

| 项 | 值 |
|---|---|
| 源/目标 | `ydy2:五星`(139 个人云, 经 alist WebDAV: `https://abcd.tammader.de5.net/dav/ydy2`) |
| 规模 | 391 组(顶层), 视频与 srt 同目录 |

`PATHS_JSON` 现在就是:

```json
{ "SUB_SRC": "ydy2", "SUB_DIR": "五星" }
```

## 二、Secrets(7 个)

| Secret | 用途 |
|---|---|
| `RCLONE_CONF` | rclone 配置原文(内含 `gd6x` / `gd2` 两段) |
| `YDY_DAV_TOKEN` | ydy2 的 alist WebDAV 令牌; 4 个 workflow 在「写 rclone 配置」后把它追加成 `[ydy2]` 段 |
| `PATHS_JSON` | 路径字典(见上) |
| `GH_TOKEN` | 调 GitHub API(dispatcher 补发批次、自接力、报告查运行状态) |
| `TG_TOKEN` / `TG_CHAT_ID` | Telegram 报告/告警 |
| `TRANSLATE_RULES` / `TRANSLATE_TERMS` | 中译规则与术语表(当前 `TRANSLATE=off` 用不到, 留着备用) |

## 三、Telegram 报告(四处, 全走 `tgnotify.py`)

| 触发 | 内容 | 频率 |
|---|---|---|
| `subtitle-translate` 每批跑完 | 批次小结: `ok/fail` + 总组/完成/待办 | 每批 1 条 |
| `subtitle-report` | 进度: 总组/完成/待办/完成率/视频数/孤儿 srt/近期运行 | 每 2 小时 |
| `subtitle-verify` | 质量校验: 坏文件、坏条目、阈值与结论 | 每 6 小时 |
| `subtitle-dispatch` | 只在异常时告警(0/N 补发失败、查 run 失败) | 事件驱动 |

- 待办清零时进度报告标题自动变成 `✅ 全部完成`。
- 缺 `TG_TOKEN`/`TG_CHAT_ID` 时全部静默降级成"只打日志", 不影响流程退出码。
- run 日志照旧脱敏(`logmask`); TG 是私聊, 发的是真实目录名/统计, 不放令牌。

## 四、Variables

| Variable | 现值 | 作用 |
|---|---|---|
| `TARGET_PARALLEL` | 8 | dispatch 补满的并发批次数 |
| `GROUPS_PER_RUN` | 6 | 每批处理几组 |
| `TRANSLATE_ZH` | false | 是否中译(当前关) |

## 五、Workflows

| 文件 | 触发 | 作用 |
|---|---|---|
| `subtitle-translate.yml` | schedule `23 */2` + 派发 | 下载→拼接→whisper 日文转写→按标点重切(≤8s/≤42字)→上传同名 srt→发批次小结 |
| `subtitle-dispatch.yml` | schedule `8,18,28,38,48,58 * * * *` | 补满并发批次; 失败才发 TG |
| `subtitle-report.yml` | schedule `43 */2` | 进度报告(日志 + TG) |
| `subtitle-verify.yml` | schedule `17 */6` | srt 质量校验(日志 + TG) |
| `subtitle-burn.yml` | 手动(已 disabled) | 烧录阶段已废弃, 保留只为兼容 |

## 六、srt 命名与完成判定

- 单文件: `ABC-123.mp4` → `ABC-123.srt`
- 多分片: `XYZ-456.part001.mp4 … part017.mp4` → 每片一份 `…part001.srt …`
- 完成判定: 组内**每个**视频都有同名 srt 才算该组完成

## 七、关键参数(写在代码里)

- whisper 默认 `large-v3`; 依赖锁 `av==18.1.0` / `ctranslate2==4.8.5` / `faster-whisper==1.2.1`
- 按组总大小升序处理(小文件先跑, 巨型文件排最后)
- 幂等: 已有同名 srt 的组自动跳过
- 输出只写 `<视频同名>.srt`, 不产生任何视频文件
