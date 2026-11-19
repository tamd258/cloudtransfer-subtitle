# -*- coding: utf-8 -*-
"""Telegram 报告发送(字幕流程共用)

用法:
    import tgnotify
    tgnotify.send("文本")      # 缺 TG_TOKEN / TG_CHAT_ID 时静默跳过, 返回 None
    tgnotify.enabled()        # 两个 Secret 都在?

为什么独立成模块: 进度 / 批次 / 校验 / 告警 四处都要发, 逻辑只留一份。
发出去的是**未脱敏**原文(stdout 才有 logmask 层), 所以文本里别放令牌、手机号、邮箱。
"""
import json
import os
import urllib.parse
import urllib.request


def _env(k):
    return (os.environ.get(k) or "").strip()


def enabled():
    return bool(_env("TG_TOKEN") and _env("TG_CHAT_ID"))


def send(text, quiet=False):
    """发一条消息; 返回 message_id, 缺配置/失败返回 None。quiet=True 时连日志都不打。"""
    tok, chat = _env("TG_TOKEN"), _env("TG_CHAT_ID")
    if not tok or not chat:
        if not quiet:
            print("[tg] 缺 TG_TOKEN / TG_CHAT_ID, 本次不发报告")
        return None
    try:
        body = urllib.parse.urlencode({
            "chat_id": chat,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tok,
                                     data=body, method="POST")
        with urllib.request.urlopen(req, timeout=30) as x:
            d = json.loads(x.read().decode("utf-8", "replace"))
        if d.get("ok"):
            mid = (d.get("result") or {}).get("message_id")
            print("[tg] 已发送 message_id=%s" % mid)
            return mid
        if not quiet:
            print("[tg] 发送失败: %s" % str(d)[:160])
    except Exception as e:
        if not quiet:
            print("[tg] 发送异常: %s" % str(e)[:160])
    return None
