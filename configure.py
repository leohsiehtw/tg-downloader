"""
一鍵設定：只問機器人 token，存進 .env。其他設定（誰能用、傳到哪、群組子頻道、大檔案登入）
都在 Telegram 跟機器人的聊天裡完成：私訊它 /start 或 /setup。
雙擊 configure.bat 執行，之後要換 token 也可以再跑一次。
"""
import re
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
TOKEN_RE = r"\d+:[\w-]{30,}"


def read_lines():
    return ENV.read_text(encoding="utf-8-sig").splitlines() if ENV.exists() else []


def current_token(lines):
    for line in lines:
        k, _, v = line.partition("=")
        if k.strip() == "BOT_TOKEN" and re.fullmatch(TOKEN_RE, v.strip()):
            return v.strip()
    return ""


def save_token(lines, token):
    """只改 BOT_TOKEN 那行，其他在聊天裡設好的值都留著。"""
    lines = [line for line in lines if line.partition("=")[0].strip() != "BOT_TOKEN"]
    if not lines:
        lines = ["# 由 configure.bat 產生，其他設定在 Telegram 私訊機器人 /setup。不要把這個檔案傳給任何人。"]
    lines.insert(1, f"BOT_TOKEN={token}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_token(token):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
    except Exception as e:
        return None, f"連不到 Telegram：{e}"
    if not r.get("ok"):
        return None, "Telegram 說這組 token 無效（Unauthorized）。請到 @BotFather 確認是最新的那組。"
    return r["result"]["username"], None


def main():
    print("=== TG 下載機器人設定 ===\n")
    lines = read_lines()
    cur = current_token(lines)
    while True:
        hint = f"（直接按 Enter 沿用 {cur[:4]}…{cur[-3:]}）" if cur else ""
        token = input(f"貼上機器人 token（@BotFather 給的）{hint}：").strip() or cur
        if not re.fullmatch(TOKEN_RE, token):
            print("  格式不對，應該長得像 123456789:ABC...，請再貼一次。")
            continue
        name, err = check_token(token)
        if err:
            print(f"  {err}")
            continue
        break
    save_token(lines, token)
    print(f"\n✓ 機器人 @{name}，token 已存到 .env。")
    print("接下來：")
    print("  1. 雙擊 restart.bat，讓機器人在背景跑起來（右下角會出現圖示）。")
    print(f"  2. 在 Telegram 打開 https://t.me/{name} 傳 /start。第一個傳的人就是主人。")
    print("  3. 其他設定（傳到哪、群組、大檔案）在聊天裡打 /setup。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
