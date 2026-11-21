# -*- coding: utf-8 -*-
"""日文视频 → 日文 SRT 字幕流水线(云端 runner 用, 纯 ASR, 零 LLM)

流程:
  1. 列出源远端目标目录(默认 out3)的 mp4 分片
  2. 按「番号」归组(去掉 已处理_ 前缀 / .partNNN_D 后缀)
  3. 每组的 .partNNN 分片按序号用 ffmpeg concat 拼成整片(单分片直接拷)
  4. faster-whisper / kotoba-whisper 做日文语音转写 -> 日文 srt 段
  5. 按标点把长段重切成字幕行(<=8s / <=42字), 组内每个分片各出一份同名 srt
  6. 上传回源远端同目录, 文件名 = <视频原名>.srt

不翻译、不调用任何 LLM: 本脚本没有任何网关/模型 API 调用
(中译链路与转写后 LLM 校对于 2026-11-21 整体删除)。

脱敏: 公开仓库, 日志里不出现真实盘名/目录名/文件名/番号 —— 全部 hash。
      真值来自 pathcfg(CI: Secret PATHS_JSON / 本机: paths.local.json)。

用法:
  python subtitle_translate.py --apply                 # 真跑
  python subtitle_translate.py                          # 演练(只列分组不处理)
  python subtitle_translate.py --only GROUP --apply     # 只处理某一组(按番号)
  python subtitle_translate.py --limit-groups 1 --apply # 最多处理前 N 组

环境变量(CI 里由 workflow 注入):
  SRC_REMOTE        源 rclone 远端名, 默认可由 pathcfg "SUB_SRC" 给
  SRC_DIR           源目录, 默认 pathcfg "SUB_DIR" 或 out3
  WHISPER_MODEL     默认 large-v3; 也可用 kotoba-whisper-v2.0-faster(日语专用)
  BUDGET_MIN        本轮时间预算(分钟)
"""
import argparse
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import pathcfg
import logmask

logmask.install()

# ---------------- 配置读取(脱敏: 真值走 pathcfg) ----------------
def cfg(key, default=""):
    return os.environ.get(key) or pathcfg.get(key, default)


SRC_REMOTE = cfg("SUB_SRC", "")
SRC_DIR = cfg("SUB_DIR", "out3")
# (零 LLM: 不再读取 SUBTITLE_API_BASE / SUBTITLE_API_KEY / SUBTITLE_MODEL*)
# whisper 模型: 默认 large-v3(全量, 日语最准; large-v3-turbo 快 4-5x 但精度略低)
#   兼容口径: 接受 "large-v3-turbo" / "turbo" 等别名, 内部映射到 HF 上可直连下载的 CT2 仓库
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
# 别名 -> HF 仓库(仅当传入的是别名而非真实路径/仓库时使用)
_WHISPER_ALIAS = {
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3_turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    # 日语专用微调(推荐): 同规模日语更准, 直接是 CT2 仓库, faster-whisper 可加载
    "kotoba": "kotoba-tech/kotoba-whisper-v2.0-faster",
    "kotoba-v2": "kotoba-tech/kotoba-whisper-v2.0-faster",
    "kotoba-v2.1": "kotoba-tech/kotoba-whisper-v2.1-faster",
    "kotoba-whisper-v2.0-faster": "kotoba-tech/kotoba-whisper-v2.0-faster",
    "kotoba-whisper-v2.1-faster": "kotoba-tech/kotoba-whisper-v2.1-faster",
}


def resolve_whisper_model(name):
    """把 whisper 模型别名解析成可直接加载的标识。
    'large-v3-turbo'/'turbo' 等别名 -> HF CT2 仓库; 其它(medium/large-v3/small/本地路径)原样返回。"""
    key = (name or "").strip().lower()
    return _WHISPER_ALIAS.get(key, name)
# --- 速度旋钮(都可用仓库 Variables 覆盖, 不改代码) ---
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "4"))
WHISPER_BATCH = int(os.environ.get("WHISPER_BATCH", "8"))  # 批量推理批大小(BatchedInferencePipeline)
WHISPER_BEAM = int(os.environ.get("WHISPER_BEAM", "5"))    # 5=束搜索(精度优先); 1=贪心(最快)
# --- ASR 质量旋钮(2026-11-20 质量升级) ---
# 日文专用模型优先: kotoba-whisper 是 whisper-large-v3 的日语微调, 同规模下日语错字率明显低
ASR_MODEL = (os.environ.get("ASR_MODEL") or "kotoba-whisper-v2.0-faster").strip()
ASR_FALLBACK = (os.environ.get("ASR_FALLBACK") or "large-v3").strip()
# 日语 initial_prompt: 促使模型输出带句读的完整句, 明显改善切句与断句质量
ASR_PROMPT = (os.environ.get("ASR_PROMPT") or
              "以下は日本語の会話です。句読点を入れて、聞き取れた内容をそのまま正確に書き起こしてください。").strip()
# 2026-11-21: LLM 链路(中译 + 转写后 LLM 校对)已整体删除 ——
# 本脚本零 LLM 依赖: 不读任何 API key, 不向任何网关发请求, 只跑本地 whisper 推理。
HALLU_SIL = float(os.environ.get("HALLUCINATION_SILENCE_THRESHOLD", "2.0"))

# 分片命名: [已处理_]<base>.partNNN[_D].mp4  ；<base> 可能含中文/番号/空格
_RE_PART = re.compile(r"(?:^|_)(?P<base>.+?)\.part(?P<idx>\d+)(?:_[A-Za-z])?\.(?P<ext>mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)
_RE_PLAIN = re.compile(r"(?:^|_)(?P<base>.+?)\.(?P<ext>mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)


def _norm_base(base):
    """归一化组名: 剥掉 已处理_ 前缀(可能多个), 让同番号的已处理/未处理分片归为一组。"""
    b = base
    while True:
        nb = re.sub(r"^已处理_", "", b)
        if nb == b:
            break
        b = nb
    return b


def now_ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(msg):
    print("[%s] %s" % (now_ts(), msg), flush=True)


def run(cmd, **kw):
    """跑外部命令, 返回 (rc, stdout, stderr); 日志经 logmask 脱敏"""
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, p.stdout, p.stderr


# ---------------- rclone / 文件分组 ----------------
def rclone_lsf(remote, path):
    """返回 [(文件名, 字节大小), ...]  —— 用 lsjson, 避免 lsf 分隔符歧义"""
    import json as _json
    rc, out, err = run(["rclone", "lsjson", "%s:%s" % (remote, path),
                        "--files-only", "--max-depth", "1"])
    if rc != 0:
        raise SystemExit("rclone lsjson 失败: %s" % err[:300])
    try:
        arr = _json.loads(out) if out.strip() else []
    except Exception:
        arr = []
    res = []
    for it in arr:
        if it.get("IsDir"):
            continue
        res.append((it.get("Name", ""), int(it.get("Size", 0) or 0)))
    return res


def group_files(files):
    """files: [(fn, size) 或 fn] -> {base: [(idx, fname), ...]}; 兼容纯名单"""
    groups = {}
    for item in files:
        fn = item[0] if isinstance(item, (tuple, list)) else item
        m = _RE_PART.search(fn)
        if m:
            base = _norm_base(m.group("base"))
            idx = int(m.group("idx"))
            groups.setdefault(base, []).append((idx, fn))
        else:
            mp = _RE_PLAIN.search(fn)
            if mp:
                groups.setdefault(_norm_base(mp.group("base")), []).append((0, fn))
    # 排序每个组内分片
    for b in groups:
        groups[b].sort(key=lambda x: x[0])
    return groups


def group_sizes(files, groups):
    """{base: 总字节大小}; files=[(fn,size)]"""
    sz = {}
    sizemap = {fn: s for fn, s in files if isinstance((fn, s), tuple)} if files and isinstance(files[0], (tuple, list)) else {}
    for base, parts in groups.items():
        tot = 0
        for _idx, fn in parts:
            tot += sizemap.get(fn, 0)
        sz[base] = tot
    return sz


def download_group(remote, path, base, parts, workdir):
    """下载一组分片到 workdir, 返回按序的本地路径列表。

    速度: 一次 rclone copy --files-from(并行 transfers), 不再逐片起进程。
    """
    lst = os.path.join(workdir, "dl.txt")
    with io.open(lst, "w", encoding="utf-8") as f:
        for _idx, fn in parts:
            f.write(fn + "\n")
    rc, out, err = run(["rclone", "copy", "%s:%s" % (remote, path), workdir,
                        "--files-from", lst, "--transfers", "8", "--checkers", "8",
                        "--drive-chunk-size", "128M", "--multi-thread-streams", "4",
                        "--retries", "3", "--stats-one-line", "--stats", "30s"])
    if rc != 0:
        raise RuntimeError("下载失败: %s" % err[-200:])
    locals_ = []
    for idx, fn in parts:
        local = os.path.join(workdir, os.path.basename(fn))
        if not os.path.exists(local):
            raise RuntimeError("下载后缺文件 idx=%d" % idx)
        locals_.append(local)
    return locals_


def concat_parts(local_parts, outpath):
    """用 ffmpeg concat demuxer 把分片按序拼成整片; 校验/重编码音频确保音轨可用"""
    if len(local_parts) == 1:
        shutil.copyfile(local_parts[0], outpath)
    else:
        lst = outpath + ".txt"
        with io.open(lst, "w", encoding="utf-8") as f:
            for p in local_parts:
                f.write("file '%s'\n" % p.replace("'", "'\\''"))
        rc, out, err = run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                            "-c", "copy", outpath])
        if rc != 0 or not os.path.exists(outpath):
            rc2, out2, err2 = run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                                   "-c:v", "libx264", "-c:a", "aac", outpath])
            if rc2 != 0:
                raise RuntimeError("ffmpeg 拼接失败: %s / %s" % (err[-200:], err2[-200:]))
    # 校验音轨: 无音轨 / 音频异常时, 用统一 16k 单声道 wav 作为转写输入
    rc, out, err = run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=codec_name,channels,sample_rate",
                        "-of", "csv=p=0", outpath])
    has_audio = rc == 0 and out.strip() != ""
    log("音轨检测: %s" % (out.strip() if has_audio else "无音轨!"))
    if has_audio:
        wav = outpath + ".wav"
        rc2, o2, e2 = run(["ffmpeg", "-y", "-i", outpath, "-vn", "-ac", "1", "-ar", "16000",
                           "-c:a", "pcm_s16le", wav])
        if rc2 == 0 and os.path.exists(wav) and os.path.getsize(wav) > 1024:
            log("已抽出声轨 wav -> %.1f MB" % (os.path.getsize(wav) / 1048576.0))
            return wav
        log("!! 抽音轨失败, 直接用原视频: %s" % (e2[-150:] if e2 else ""))
    return outpath


# ---------------- ASR ----------------
def transcribe(video_path, whisper_model, max_seconds=0):
    """faster-whisper 日文转写, 返回 [(start, end, text), ...]

    速度: CPU int8 + 批量推理(BatchedInferencePipeline) + 贪心解码(beam=1)。
    VAD 空结果时先"放宽 VAD"重跑, 最后才全量无 VAD —— 不再一上来就跑两遍全片。
    """
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    resolved = resolve_whisper_model(whisper_model)
    log("加载 whisper 模型 %s (解析=%s, CPU int8, threads=%d, beam=%d)..." % (
        whisper_model, resolved, WHISPER_THREADS, WHISPER_BEAM))
    # 模型加载失败自动回退(日语微调仓库若不可用 -> 退回 large-v3), 不让整组任务直接死掉
    model, err_last, cands = None, None, []
    for c in [resolved, resolve_whisper_model(ASR_FALLBACK)]:
        if c and c not in cands:
            cands.append(c)
    for cand in cands:
        try:
            model = WhisperModel(cand, device="cpu", compute_type="int8",
                                 cpu_threads=WHISPER_THREADS, num_workers=1)
            resolved = cand
            break
        except Exception as e:
            err_last = e
            log("!! 模型加载失败 %s: %s" % (cand, str(e)[:160]))
    if model is None:
        raise RuntimeError("whisper 模型全部加载失败: %s" % err_last)
    log("实际使用模型: %s" % resolved)
    pipe = BatchedInferencePipeline(model=model)

    # 紧 VAD: 这类片对白稀疏, 但阈值过高会把对白整段滤掉(旧版实测 long-audio 出 0 条)
    VAD_TIGHT = {"min_silence_duration_ms": 2000, "threshold": 0.15,
                 "min_speech_duration_ms": 50, "speech_pad_ms": 400}
    # 放宽 VAD: 出 0 条时的第一手段(比无 VAD 全量重跑快好几倍)
    VAD_LOOSE = {"min_silence_duration_ms": 400, "threshold": 0.04,
                 "min_speech_duration_ms": 30, "speech_pad_ms": 500}

    def _collect(segments, info):
        out, lastlog = [], time.time()
        for seg in segments:
            txt = (seg.text or "").strip()
            if txt:
                out.append((seg.start, seg.end, txt))
            # 每 30s 打一次进度(能看到是否在推进)
            if time.time() - lastlog > 30:
                log("  转写进度: 已到 %.0fs / 共 %.0fs, 已收 %d 条" % (seg.end, info.duration, len(out)))
                lastlog = time.time()
        return out

    def _run(use_vad, vad_params=None):
        log("开始转写(日文, vad=%s, beam=%d, batch=%d, prompt=%s, max_s=%s)..." % (
            use_vad, WHISPER_BEAM, WHISPER_BATCH, "on" if ASR_PROMPT else "off",
            max_seconds or "full"))
        # 质量参数: 束搜索 + 压缩比/对数概率双阈值挡掉幻觉与静音误出 + 重复惩罚防循环
        kw = dict(language="ja", beam_size=WHISPER_BEAM, condition_on_previous_text=False,
                  vad_filter=use_vad, temperature=0.0,
                  compression_ratio_threshold=2.2, log_prob_threshold=-1.0,
                  no_speech_threshold=0.5, repetition_penalty=1.02)
        if ASR_PROMPT:
            kw["initial_prompt"] = ASR_PROMPT
        if use_vad and HALLU_SIL > 0:
            # 长静音段幻觉抑制(只在有 VAD 时生效)
            kw["hallucination_silence_threshold"] = HALLU_SIL
        if use_vad and vad_params:
            kw["vad_parameters"] = vad_params
        if max_seconds or not use_vad:
            # 探针(需 clip_timestamps 字符串)或全量无 VAD:
            # 批量管线在 vad_filter=False 且无 clip_timestamps 时直接 RuntimeError, 所以走单条路径
            if max_seconds:
                kw["clip_timestamps"] = "0,%d" % max_seconds
            segments, info = model.transcribe(video_path, **kw)
        else:
            segments, info = pipe.transcribe(video_path, batch_size=WHISPER_BATCH, **kw)
        log("检测语种=%s p=%.2f, 时长=%.1fs" % (info.language, info.language_probability, info.duration))
        return _collect(segments, info)

    out = _run(True, VAD_TIGHT)
    if not out and not max_seconds:
        log("!! 紧 VAD 后无字幕, 先用放宽 VAD 重跑(比全量重跑快得多)")
        out = _run(True, VAD_LOOSE)
    if not out and not max_seconds:
        log("!! 放宽 VAD 仍无字幕, 回退到无 VAD 全量转写")
        out = _run(False)
    log("转写完成: %d 条(重切前)" % len(out))
    out = split_long_segments(out)
    log("转写完成: %d 条(按句重切后, 完整句优先/软上限%.0fs)" % (len(out), MAX_SEG_DUR))
    return out


# ---------------- 长段重切(字幕后处理) ----------------
# 批量推理管线会把整段对白并成 30~200 秒的长块, 直接当字幕不可用。
# 2026-11-20 调整: 断句以「语义完整的句子」为原子单位 ——
#   句末标点(。！？!?…．)才是真正的断句点; 逗号/顿号(，、,)只是句中停顿, 不再强制切行。
#   这样一句本来一口气能讲完的话不会被拦腰截断、跳到下一行。
#   只有单句本身就明显超长(>软上限)时, 才降级: 先在句内停顿处切, 最后才按字数硬切兜底。
MAX_SEG_DUR = float(os.environ.get("MAX_SEG_DUR", "8"))
MAX_SEG_CHARS = int(os.environ.get("MAX_SEG_CHARS", "42"))
_END_PUNCT = "。！？!?…．"          # 句末: 强断句边界
_MID_PUNCT = "，、,"              # 句内停顿: 仅单句超长时的次级切点
_END_SPLIT = re.compile(r"(?<=[%s])" % _END_PUNCT)
_MID_SPLIT = re.compile(r"(?<=[%s])" % _MID_PUNCT)
_LETTER = re.compile(r"[ぁ-んァ-ヶー一-龥A-Za-z0-9]")


def _atomic_sentences(tx):
    """按句末标点切成完整句子(标点留在句尾); 纯标点碎片并回上一句。"""
    parts = [x.strip() for x in _END_SPLIT.split(tx) if x and x.strip()]
    out = []
    for p in parts:
        if _LETTER.search(p):
            out.append(p)
        elif out:
            out[-1] += p          # 只剩「…」之类并回上一句
    return out or ([tx.strip()] if tx.strip() else [])


def _soft_split(s, cap, splitter):
    """把超长句在次级标点处切成 <=cap 的块, 标点留在块尾。"""
    subs = [x for x in splitter.split(s) if x]
    blocks, cur = [], ""
    for sub in subs:
        if not cur:
            cur = sub
        elif len(cur) + len(sub) <= cap:
            cur += sub
        else:
            blocks.append(cur)
            cur = sub
    if cur:
        blocks.append(cur)
    return blocks


def split_long_segments(segs, max_dur=None, max_chars=None):
    """把过长的转写段按「语义完整的句子」重切成字幕行, 时间按字长比例分配。

    原则(2026-11-20): 完整一句话优先整句独占一行, 不因字数/时长上限被拦腰截断。
      1) 按句末标点成句 -> 每句为原子单元;
      2) 相邻句能一起塞进上限就合并(少碎行), 塞不下才各自成行;
      3) 单个句子自身超上限 -> 先按句内停顿切, 仍超才按字数硬切兜底。
    segs: [(start, end, text)] -> [(start, end, text)]
    """
    max_dur = max_dur or MAX_SEG_DUR
    max_chars = max_chars or MAX_SEG_CHARS
    out = []
    for st, en, tx in segs:
        tx = (tx or "").strip()
        if not tx or not _LETTER.search(tx):
            continue
        dur = max(0.3, en - st)
        if dur <= max_dur and len(tx) <= max_chars:
            out.append((st, en, tx))
            continue

        per_char = dur / max(1, len(tx))                       # 每字多少秒
        cap = max(6, min(max_chars, int(max_dur / per_char)))  # 合并多句时正常每行字数上限
        # 软容忍: 一个完整句子宁可略超(+50%字 / +2s)也别切断语义;
        #         只有"把多个句子合并到一行"时才用严格 cap, 避免整行过长。
        hard_cap = min(max_chars + 16, int(cap * 1.5))
        hard_dur = max_dur + 2.0

        sents = _atomic_sentences(tx)
        if not sents:
            continue

        # 单句自身超长 -> 降级切分(先句内停顿, 再按字硬切)
        units = []   # [(文本, 是否可与其他句合并)]
        for s in sents:
            s_dur = len(s) * per_char
            if len(s) <= hard_cap and s_dur <= hard_dur:
                units.append((s, True))
                continue
            for b in _soft_split(s, hard_cap, _MID_SPLIT):
                while len(b) > hard_cap:                      # 子句仍超长 -> 按字硬切兜底
                    units.append((b[:hard_cap], False))
                    b = b[hard_cap:]
                if b:
                    units.append((b, False))

        # 合并相邻「可合并」短句, 减少碎行; 硬切出来的块不再参与合并。
        lines, cur, cur_len = [], "", 0
        for u, mergeable in units:
            if cur and (not mergeable or cur_len + len(u) > cap
                        or (cur_len + len(u)) * per_char > max_dur):
                lines.append(cur)
                cur, cur_len = "", 0
            cur += u
            cur_len += len(u)
            if not mergeable:
                lines.append(cur)
                cur, cur_len = "", 0
        if cur:
            lines.append(cur)

        # 按字长比例分配时间
        total = sum(len(x) for x in lines) or 1
        t = st
        for ln in lines:
            d = max(0.6, dur * len(ln) / total)
            e = min(en, t + d)
            if e - t < 0.4:
                e = min(en, t + 0.4)
            out.append((round(t, 2), round(e, 2), ln))
            t = e
            if t >= en - 0.01:
                break
    return out


# ---------------- SRT ----------------
def _fmt_ts(sec):
    if sec < 0:
        sec = 0
    ms = int(round(sec * 1000))
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    mm = ms % 1000
    return "%02d:%02d:%02d,%03d" % (h, m, s, mm)


def write_srt(segs, path):
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        for n, (st, en, txt) in enumerate(segs, 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (n, _fmt_ts(st), _fmt_ts(en), txt))


# ---------------- 同名 srt 命名 + 按分片切时间轴 ----------------
def part_basenames(parts):
    """[(idx, fname), ...] -> [去扩展名的文件名, ...]  —— 即每个视频文件该配的 srt 名(不带 .srt)"""
    return [os.path.splitext(fn)[0] for _idx, fn in parts]


def required_srt_names(parts):
    """这个组「算做完」需要哪些 srt 文件名(带 .srt)"""
    return set("%s.srt" % b for b in part_basenames(parts))


def probe_duration(path):
    """ffprobe 取媒体时长(秒); 失败返回 0.0"""
    rc, out, err = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path])
    if rc != 0:
        return 0.0
    try:
        return float((out or "").strip().splitlines()[0])
    except Exception:
        return 0.0


def split_segments_by_parts(segs, durations):
    """把「合并整片」时间轴的 segs 按各分片时长切成每片一份(时间轴改回该片内的相对时间)。

    segs: [(start, end, text)] 相对合并整片起点
    durations: [该组每个分片的时长(秒)], 顺序与分片一致
    返回: [[(start_i, end_i, text), ...], ...]  每个分片一份
    """
    out = []
    off = 0.0
    for d in durations:
        end = off + d
        part = []
        for st, en, tx in segs:
            # 与该片区间有交集才留下
            if en <= off + 0.05 or st >= end - 0.05:
                continue
            a = max(0.0, st - off)
            b = min(d, en - off)
            if b <= a:
                b = min(d, a + 0.2)
            part.append((a, b, tx))
        out.append(part)
        off = end
    return out


# ---------------- 主流程 ----------------
def process_group(remote, path, base, parts, workdir, apply, probe_seconds=0):
    log("处理组 (parts=%d, probe=%s)" % (len(parts), probe_seconds or "off"))
    if not apply:
        return
    t0 = time.time()
    gdir = os.path.join(workdir, "grp")
    os.makedirs(gdir, exist_ok=True)
    local_parts = download_group(remote, path, base, parts, gdir)
    video = os.path.join(gdir, "merged.mp4")
    concat_parts(local_parts, video)
    log("拼接完成 -> %.1f MB" % (os.path.getsize(video) / 1048576.0))

    segs = transcribe(video, WHISPER_MODEL, max_seconds=probe_seconds)
    if probe_seconds:
        log("=== PROBE 结果: 前 %ds 收到 %d 条字幕 ===" % (probe_seconds, len(segs)))
        for st, en, tx in segs[:10]:
            log("  [%.1f-%.1f] %s" % (st, en, tx))
        for p in local_parts + [video]:
            try: os.remove(p)
            except Exception: pass
        return
    if not segs:
        # 整组都没语音(静音/纯音乐): 给每个分片写空 srt 占位。
        # 不写占位的话「组内每片都有同名 srt」永远不成立 -> 这组每轮都被重新下载重传(死循环)。
        log("!! 整组无字幕段 -> 写空 srt 占位(避免无限重跑)")
        n_ph = 0
        for nm in part_basenames(parts):
            ph = os.path.join(gdir, os.path.basename(nm) + ".srt")
            write_srt([(0.0, 1.0, "")], ph)
            rc, out, err = run(["rclone", "copyto", ph,
                                "%s:%s/%s.srt" % (remote, path, nm), "--retries", "3"])
            if rc == 0:
                n_ph += 1
            else:
                log("!! 上传占位失败 %s.srt: %s" % (nm, err[:200]))
        log("上传空 srt 占位 -> %d/%d 份" % (n_ph, len(part_basenames(parts))))
        for p in local_parts + [video]:
            try:
                os.remove(p)
            except Exception:
                pass
        return
    zh = segs          # 只出日文识别 srt: 转写结果就是最终字幕
    log("日文识别 %d 条 -> 直接写 srt" % len(zh))

    # ---- 每片一份同名 srt: 合并时间轴按分片时长切开, 时间轴改回片内相对时间 ----
    names = part_basenames(parts)
    if len(local_parts) == 1:
        per_part = [zh]
    else:
        durs = [probe_duration(p) for p in local_parts]
        if all(d > 0 for d in durs):
            log("分片时长: %s 秒" % ", ".join("%.1f" % d for d in durs))
            per_part = split_segments_by_parts(zh, durs)
        else:
            log("!! 分片时长探测失败(%s) —— 退化为只给首片写整份 srt" % durs)
            per_part = [zh if i == 0 else [] for i in range(len(local_parts))]

    n_up = 0
    for name, ss in zip(names, per_part):
        if not ss:
            # 该片没语音: 写空 srt 占位(一条空白字幕), 让这组能算"已完成",
            # 否则它永远挂在待处理里被反复下载+转写(实测 40 组卡 3 小时)。
            log("  该片无字幕段 -> 写空 srt 占位: %s.srt" % name)
            ss = [(0.0, 1.0, "")]
        local_srt = os.path.join(gdir, os.path.basename(name) + ".srt")
        write_srt(ss, local_srt)
        rc, out, err = run(["rclone", "copyto", local_srt,
                            "%s:%s/%s.srt" % (remote, path, name), "--retries", "3"])
        if rc != 0:
            log("!! 上传失败 %s.srt: %s" % (name, err[:200]))
        else:
            n_up += 1
    log("上传同名 srt -> %d/%d 份 (%.1fs)" % (n_up, len(names), time.time() - t0))

    # 清理
    for p in local_parts + [video]:
        try:
            os.remove(p)
        except Exception:
            pass


def run_single_file(remote, path, rel, apply):
    """单文件验证模式: 对指定文件(可含子目录)做日文转写, srt 写回同目录。

    用于换模型/换参数后的抽样验收: 不动其它组, 幂等与分组逻辑都不参与。
    """
    log("=== 单文件模式: %s ===" % rel)
    if not apply:
        log("(演练模式, 只下载不处理) —— 去掉 --apply 之外的参数检查")
        return
    workdir = tempfile.mkdtemp(prefix="subone_")
    gdir = os.path.join(workdir, "one")
    os.makedirs(gdir, exist_ok=True)
    local = os.path.join(gdir, os.path.basename(rel))
    rc, out, err = run(["rclone", "copyto", "%s:%s/%s" % (remote, path, rel), local,
                        "--retries", "3", "--stats-one-line", "--stats", "20s"])
    if rc != 0 or not os.path.exists(local):
        raise SystemExit("!! 下载失败: %s" % ((err or out)[:300]))
    log("下载完成 %.1f MB" % (os.path.getsize(local) / 1048576.0))

    t0 = time.time()
    segs = transcribe(local, WHISPER_MODEL, max_seconds=0)
    log("转写耗时 %.1f 分钟, 共 %d 条" % ((time.time() - t0) / 60.0, len(segs)))
    if not segs:
        raise SystemExit("!! 没转写出任何字幕")

    zh = segs
    log("日文识别 %d 条 -> 输出日文 srt" % len(zh))

    stem = os.path.splitext(os.path.basename(rel))[0]
    local_srt = os.path.join(gdir, stem + ".srt")
    write_srt(zh, local_srt)
    dst = "%s:%s/%s.srt" % (remote, path, os.path.splitext(rel)[0])
    rc2, o2, e2 = run(["rclone", "copyto", local_srt, dst, "--retries", "3"])
    log("上传 %s -> rc=%d %s" % ("<srt>", rc2, (e2 or "")[:150]))
    log("=== 前 12 条预览 ===")
    for st, en, tx in zh[:12]:
        log("  [%.1f-%.1f] %s" % (st, en, tx))
    log("srt 行数=%d, 本地路径=%s" % (len(zh), local_srt))
    log("保留本地文件供下载核对: %s" % gdir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit-groups", type=int, default=0)
    ap.add_argument("--budget-min", type=int, default=300)
    ap.add_argument("--list", action="store_true", help="只列出分组统计")
    ap.add_argument("--probe-only", type=int, default=0, help="只转写前 N 秒做验证(不上传)")
    ap.add_argument("--start-index", type=int, default=0, help="从待处理列表第 K 个开始(并行分批用)")
    ap.add_argument("--force", action="store_true", help="配合 --only: 即使已有 srt 也强制重跑(测试用)")
    ap.add_argument("--file", default="",
                    help="单文件验证模式: 只处理该文件(相对源目录, 可含子目录, 如 字幕版/xxx.mp4)")
    a = ap.parse_args()

    if not SRC_REMOTE:
        raise SystemExit("!! 缺少 SRC_REMOTE(SUB_SRC) —— 检查 Secret PATHS_JSON")

    deadline = time.time() + a.budget_min * 60
    if a.file:
        run_single_file(SRC_REMOTE, SRC_DIR, a.file, a.apply)
        return
    log("源=%s:%s | 模式=纯日文ASR(零LLM) | ASR=%s(beam=%d) | apply=%s" % (
        SRC_REMOTE, SRC_DIR, WHISPER_MODEL, WHISPER_BEAM, a.apply))

    files = rclone_lsf(SRC_REMOTE, SRC_DIR)
    log("目录内文件数=%d" % len(files))
    groups = group_files(files)
    gsizes = group_sizes(files, groups)
    log("归组后=%d 组" % len(groups))

    # 幂等: 组内「每个视频文件都已有同名 srt」才算做完
    present_srt = set(fn for fn, _sz in files if fn.lower().endswith(".srt"))
    done_groups = set()
    for b, parts in groups.items():
        if required_srt_names(parts) <= present_srt:
            done_groups.add(b)

    # 目标顺序: 按组总大小升序(小的先做, 巨型文件排最后, 避免阻塞)
    order_by_size = sorted(groups.keys(), key=lambda b: gsizes.get(b, 0))

    targets = []
    for base in order_by_size:
        if base in done_groups and not a.force:
            continue
        if a.only and base != a.only:
            continue
        targets.append(base)
    log("待处理组=%d (已跳过 %d 个已全片带字幕的组; 按大小升序)" % (len(targets), len(done_groups)))

    if a.list:
        for b in targets:
            print("  group parts=%d" % len(groups[b]))
        return

    # 并行分批: 从第 start_index 个待处理组开始
    if a.start_index > 0:
        targets = targets[a.start_index:]
        log("start-index=%d, 剩余可处理=%d" % (a.start_index, len(targets)))
    if a.limit_groups > 0:
        targets = targets[:a.limit_groups]
    log("本批目标组: %d" % len(targets))

    workdir = tempfile.mkdtemp(prefix="subtr_")
    log("临时目录=%s" % workdir)
    ok = fail = 0
    for base in targets:
        if time.time() > deadline:
            log("!! 到达时间预算, 收工")
            break
        try:
            process_group(SRC_REMOTE, SRC_DIR, base, groups[base], workdir, a.apply,
                          probe_seconds=a.probe_only)
            ok += 1
        except Exception as e:
            fail += 1
            log("!! 组处理失败: %s" % str(e)[:200])
    shutil.rmtree(workdir, ignore_errors=True)
    log("完成: ok=%d fail=%d" % (ok, fail))

    # 报告: 每批跑完发一条 Telegram(缺 TG_TOKEN/TG_CHAT_ID 时 tgnotify 静默跳过)
    try:
        import tgnotify
        rest = ""
        try:
            import subtitle_progress as _sp
            _st = _sp.collect()
            rest = "\n总组 %d | 完成 %d | 待办 %d" % (_st["total_groups"], _st["done"], _st["pending"])
        except Exception:
            pass
        tgnotify.send("%s 字幕批次完成 · %s:%s\n本批 ok=%d fail=%d%s" % (
            "✅" if fail == 0 else "⚠️", SRC_REMOTE, SRC_DIR, ok, fail, rest))
    except Exception as e:
        log("!! TG 报告失败: %s" % str(e)[:120])


if __name__ == "__main__":
    sys.exit(main())

