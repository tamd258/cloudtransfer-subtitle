# -*- coding: utf-8 -*-
"""字幕批量进度统计(跑一遍打日志 + 发 Telegram 报告)

统计源目录(SRC_REMOTE:SRC_DIR, 由 PATHS_JSON 的 SUB_SRC/SUB_DIR 给)里:
  - 归组后的总组数(一组 = 一个视频或一套 partNNN 分片)
  - 已完成组数(组内每个视频都有同名 srt)
  - 待办组数
  - 视频文件数 / 已出 srt 数 / 没有同名视频的孤儿 srt
并结合最近几轮 subtitle-translate 的运行状态, 拼报告。

环境变量: RCLONE_CONF 已写入 ~/.config/rclone; SUB_SRC/SUB_DIR 由 PATHS_JSON 给;
          TG_TOKEN / TG_CHAT_ID 有就发 Telegram, 缺则只打日志(不影响退出码)。
脱敏: 日志走 logmask; 番号/盘名在 run 日志里不出现明文。TG 消息是私聊, 按真实名字发。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pathcfg
import logmask
import tgnotify

logmask.install()

SRC_REMOTE = os.environ.get("SUB_SRC") or pathcfg.get("SUB_SRC", "")
SRC_DIR = os.environ.get("SUB_DIR") or pathcfg.get("SUB_DIR", "")

_RE_PART = re.compile(r"(?:^|_)(?P<base>.+?)\.part(?P<idx>\d+)(?:_[A-Za-z])?\.(mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)
_RE_PLAIN = re.compile(r"(?:^|_)(?P<base>.+?)\.(mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)


def log(m):
    print("%s %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def rclone_files(remote, path):
    import subprocess
    p = subprocess.run(["rclone", "lsf", "%s:%s" % (remote, path), "--files-only", "--max-depth", "1"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[:200])
    return [l.strip() for l in p.stdout.splitlines() if l.strip()]


def collect():
    """统计: 总组数 / 完成组数 / 待办组数 / 视频数 / 已配 srt 的视频数 / 孤儿 srt 数

    完成判定: 组内每个视频文件旁都有同名 srt(<视频文件名去扩展名>.srt)。
    """
    files = rclone_files(SRC_REMOTE, SRC_DIR)
    groups = {}          # 番号 -> {视频文件名, ...}
    srt_all = set()      # 目录里所有 .srt
    vid_stem = set()     # 所有视频文件去扩展名
    for fn in files:
        if fn.lower().endswith(".srt"):
            srt_all.add(fn)
            continue
        vid_stem.add(os.path.splitext(fn)[0])
        m = _RE_PART.search(fn) or _RE_PLAIN.search(fn)
        if m:
            groups.setdefault(m.group("base"), set()).add(fn)
    srt_matched = set(s for s in srt_all if s[:-4] in vid_stem)
    done = 0
    for _base, parts in groups.items():
        need = set(os.path.splitext(p)[0] + ".srt" for p in parts)
        if need <= srt_all:
            done += 1
    return {
        "total_groups": len(groups),
        "done": done,                     # 已完成(组内视频全带同名 srt)
        "srt": len(srt_matched),          # 已配字幕的视频文件数
        "videos": len(vid_stem),          # 视频文件数
        "burn": 0,                        # 流程已不烧录
        "pending": len(groups) - done,    # 还没配齐同名 srt 的组数
        "srt_no_burn": 0,
        "orphan_srt": len(srt_all) - len(srt_matched),   # 没有同名视频的 srt
    }


def gh_runs():
    """最近几轮 subtitle-translate / dispatch 运行概况"""
    tok = os.environ.get("GH_TOKEN", "")
    if not tok:
        return None
    url = "https://api.github.com/repos/tamd258/cloudtransfer-subtitle/actions/runs?per_page=20"
    req = urllib.request.Request(url, headers={"Authorization": "token " + tok,
                                               "User-Agent": "wb-substat",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            d = json.loads(x.read().decode("utf-8", "replace"))
    except Exception as e:
        log("!! 查 GitHub 失败: %s" % str(e)[:120])
        return None
    runs = d.get("workflow_runs") or []
    act = [r for r in runs if r["status"] in ("queued", "in_progress")]
    recent = [r for r in runs if r["status"] == "completed"][:10]
    succ = sum(1 for r in recent if r.get("conclusion") == "success")
    fail = sum(1 for r in recent if r.get("conclusion") == "failure")
    return {"active": len(act), "recent_n": len(recent), "succ": succ, "fail": fail}


def build_text(stats, runs):
    """报告正文(只出 srt 的口径: 没有硬字幕/烧录这些旧字段)"""
    t = time.strftime("%m-%d %H:%M")
    total = stats["total_groups"]
    done = stats["done"]
    pct = (done * 100.0 / total) if total else 0.0
    head = "✅ 全部完成" if (total and stats["pending"] == 0) else "🎬 字幕进度"
    s = "%s · %s\n" % (head, t)
    s += "目录 %s:%s\n" % (SRC_REMOTE, SRC_DIR)
    s += "总组 %d | ✅完成 %d | ⏳待办 %d | 完成率 %.1f%%" % (total, done, stats["pending"], pct)
    s += "\n视频 %d 个 | 已配 srt %d 个" % (stats["videos"], stats["srt"])
    if stats["orphan_srt"]:
        s += "\n⚠ 没有同名视频的孤儿 srt: %d 个" % stats["orphan_srt"]
    if runs:
        s += "\n近期运行: 成功 %d / 失败 %d / 进行中 %d" % (runs["succ"], runs["fail"], runs["active"])
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-tg", action="store_true", help="只打日志, 不发 Telegram")
    a = ap.parse_args()
    if not SRC_REMOTE:
        log("!! 缺 SUB_SRC(PATHS_JSON)")
        return 1
    stats = collect()
    runs = gh_runs()
    if a.json:
        print(json.dumps({"stats": stats, "runs": runs}, ensure_ascii=False))
        return 0
    text = build_text(stats, runs)
    print(text)
    if not a.no_tg:
        if tgnotify.enabled():
            tgnotify.send(text)
        else:
            log("[tg] 未配置 TG_TOKEN / TG_CHAT_ID, 只打日志")
    return 0


if __name__ == "__main__":
    sys.exit(main())
