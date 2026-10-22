"""
一鍵設定：問你 token / api_id / api_hash，整理好 .env，登入你的 Telegram 帳號，
自動填上你的 user id。雙擊 configure.bat 執行，之後要改設定也可以再跑一次。
"""
import asyncio
import re
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
ORDER = ["BOT_TOKEN", "CHANNEL_ID", "ALLOWED_USER_IDS", "TOPICS", "DEFAULT_TOPIC",
         "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "UPLOAD_AS_USER",
         "SHARED_SECRET", "PORT", "ENABLE_POLLING", "COOKIES_FILE"]
VALID = {
    "BOT_TOKEN": r"\d+:[\w-]{30,}",
    "CHANNEL_ID": r"-?\d+|@\w+",
    "ALLOWED_USER_IDS": r"\d+(,\d+)*",
    "TELEGRAM_API_ID": r"\d+",
    "TELEGRAM_API_HASH": r"[0-9a-f]{32}",
}


def looks_ok(key, value):
    if not value or "（" in value or "(" in value:
        return False
    pat = VALID.get(key)
    return bool(re.fullmatch(pat, value)) if pat else True


def read_env():
    """讀舊的 .env；同名多行時取第一個看起來正確的值，範本說明文字一律丟掉。"""
    vals = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                if k not in vals and looks_ok(k, v):
                    vals[k] = v
    return vals


def write_env(vals):
    lines = ["# 由 configure.bat 產生。不要把這個檔案傳給任何人。"]
    for k in ORDER + sorted(set(vals) - set(ORDER)):
        if k in vals:
            lines.append(f"{k}={vals[k]}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mask(v):
    return v[:4] + "…" + v[-3:] if len(v) > 10 else v


def ask(vals, key, prompt, check=None):
    cur = vals.get(key, "")
    while True:
        hint = f"（直接按 Enter 沿用 {mask(cur)}）" if cur else ""
        v = input(f"{prompt}{hint}：").strip() or cur
        if not looks_ok(key, v):
            print("  格式不對，請再貼一次。")
            continue
        if check:
            err = check(v)
            if err:
                print(f"  {err}")
                cur = "" if v == cur else cur
                continue
        vals[key] = v
        return v


def check_token(token):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
    except Exception as e:
        return f"連不到 Telegram：{e}"
    if not r.get("ok"):
        return "Telegram 說這組 token 無效（Unauthorized）。請到 @BotFather 確認是最新的那組。"
    print(f"  ✓ 機器人 @{r['result']['username']}")
    return None


def check_chat(token, chat_id):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getChat",
                         params={"chat_id": chat_id}, timeout=20).json()
    except Exception as e:
        return f"連不到 Telegram：{e}"
    if not r.get("ok"):
        return "機器人看不到這個群組，請先把機器人加進群組並設成管理員。"
    print(f"  ✓ 群組「{r['result'].get('title')}」")
    return None


async def login_user(vals):
    from telethon import TelegramClient
    client = TelegramClient(str(HERE / "user"), int(vals["TELEGRAM_API_ID"]), vals["TELEGRAM_API_HASH"])
    print("\n接下來登入你的 Telegram 帳號（傳大檔案用）。手機號碼請加國碼，例如 +886912345678。")
    await client.start()
    me = await client.get_me()
    print(f"  ✓ 已登入：{me.first_name}（id {me.id}）")
    try:
        await client.get_dialogs()
        chat = vals["CHANNEL_ID"]
        await client.get_entity(int(chat) if chat.lstrip("-").isdigit() else chat)
        print("  ✓ 你的帳號找得到這個群組")
    except Exception:
        print("  ⚠ 你的帳號找不到這個群組，請確認你在群組裡。")
    await client.disconnect()
    return me.id


def main():
    print("=== TG 下載機器人設定 ===\n")
    vals = read_env()
    vals.setdefault("PORT", "8080")
    vals.setdefault("ENABLE_POLLING", "1")
    vals.setdefault("TOPICS", "Q:11643,A:12635,Ns:14878,頂:14323,General:0")

    token = ask(vals, "BOT_TOKEN", "1/3 貼上機器人 token（@BotFather 給的）", check_token)
    ask(vals, "CHANNEL_ID", "    群組 id（-100 開頭）", lambda c: check_chat(token, c))
    print("\n到 https://my.telegram.org → API development tools 拿下面兩個值。")
    ask(vals, "TELEGRAM_API_ID", "2/3 api_id（一串數字）")
    ask(vals, "TELEGRAM_API_HASH", "3/3 api_hash（32 個英數字）")
    write_env(vals)  # 先存一次，登入失敗也不會白填

    # 機器人自己的 id（token 冒號前那段）不能當成你的 id
    ids = [x for x in vals.get("ALLOWED_USER_IDS", "").split(",") if x and x != token.split(":")[0]]
    vals["ALLOWED_USER_IDS"] = ",".join(ids)
    try:
        uid = asyncio.run(login_user(vals))
        if str(uid) not in ids:
            ids.insert(0, str(uid))
        vals["ALLOWED_USER_IDS"] = ",".join(ids)
        vals["UPLOAD_AS_USER"] = "1"
    except Exception as e:
        print(f"\n⚠ 帳號登入沒有完成：{e}\n  先用機器人上傳（50MB 上限），之後可以再跑一次 configure.bat。")
        vals["UPLOAD_AS_USER"] = "0"
    if not vals["ALLOWED_USER_IDS"]:
        del vals["ALLOWED_USER_IDS"]
    write_env(vals)

    print("\n✓ 設定已存到 .env。")
    if not vals.get("ALLOWED_USER_IDS"):
        print("  還差你的 user id：開 start.bat 後私訊機器人 /start，把它回的數字填進 ALLOWED_USER_IDS。")
    print("  雙擊 restart.bat 讓背景的機器人套用新設定。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
