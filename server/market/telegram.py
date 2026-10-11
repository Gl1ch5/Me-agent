"""
name: telegram
description: send messages/files to your Telegram via a bot
icon: ✈
color: cyan
env: TG_BOT_TOKEN, TG_CHAT_ID
---
Set secrets TG_BOT_TOKEN and TG_CHAT_ID. Use to notify the user when long work finishes.
"""


@action
def send(ctx, text: str):
    "Send a text message"
    t, c = ctx.env.get("TG_BOT_TOKEN"), ctx.env.get("TG_CHAT_ID")
    r = ctx.http.post(f"https://api.telegram.org/bot{t}/sendMessage", {"chat_id": c, "text": text[:4000]})
    return "sent" if r.get("ok") else r
