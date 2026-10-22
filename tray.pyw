"""
工作列（右下角）圖示：在背景跑 bot.py，當掉 10 秒後自動重開。
圖示顏色：綠＝執行中、黃＝重新啟動中、紅＝停住了（滑鼠移上去看原因）。
右鍵選單可以看紀錄、開資料夾、重新啟動、結束。也可以私訊機器人 /restart 從手機重新啟動。
機器人停止、當掉、或上次電腦關機時，會寫進 bot.log，並用 Telegram 私訊通知你。
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
LOG = HERE / "bot.log"
HEARTBEAT = HERE / ".heartbeat"  # 執行中每分鐘更新；正常結束會刪掉，留著就代表上次是突然停的
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW：不跳黑色視窗
COLORS = {"running": (46, 204, 113), "waiting": (241, 196, 15), "error": (231, 76, 60)}
RESTART_CODE = 4  # bot.py 收到 /restart 時用這個代碼結束
LOG_KEEP_DAYS = 2  # bot.log 每幾天清一次
LOG_CLEARED = HERE / ".log-cleared"  # 上次清 bot.log 的時間
_log_lock = threading.Lock()


def read_env():
    vals = {}
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                vals.setdefault(k, v)
    return vals


def clear_old_log():
    """bot.log 每 LOG_KEEP_DAYS 天清一次（太大也清）。"""
    now = time.time()
    try:
        last = float(LOG_CLEARED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOG_CLEARED.write_text(str(now), encoding="utf-8")  # 第一次：從現在開始算
        return
    too_big = LOG.exists() and LOG.stat().st_size > 5_000_000
    if now - last >= LOG_KEEP_DAYS * 86400 or too_big:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        LOG.write_text(f"{stamp} [工作列] 清空舊紀錄（每 {LOG_KEEP_DAYS} 天清一次）\n", encoding="utf-8")
        LOG_CLEARED.write_text(str(now), encoding="utf-8")


def append_log(text):
    with _log_lock:
        try:
            clear_old_log()
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass


def write_log(text):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_log(f"{stamp} [工作列] {text}\n")


def pump_log(stream):
    """把 bot.py 的輸出一行行寫進 bot.log。不一直開著 bot.log，才能隨時清掉它。"""
    for line in iter(stream.readline, b""):
        append_log(line.decode("utf-8", "replace").rstrip("\r\n") + "\n")


def last_log_lines(n=8):
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return ""
    return re.sub(r"\d{6,}:[\w-]{30,}", "***", "\n".join(lines))


def tell_user(text, tries=3):
    """私訊 ALLOWED_USER_IDS 的第一個人；機器人本身掛了也照樣能送。"""
    env = read_env()
    token, uid = env.get("BOT_TOKEN"), env.get("ALLOWED_USER_IDS", "").split(",")[0].strip()
    if not token or not uid:
        return
    for i in range(tries):
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": uid, "text": text[:4000]}, timeout=10)
            return
        except requests.RequestException:
            if i < tries - 1:
                time.sleep(10)


def python_exe():
    """用 python.exe 跑 bot（pythonw 也行，但 python.exe 錯誤訊息比較完整）。"""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console if console.exists() else exe)


class Runner:
    def __init__(self, on_change, on_relaunch):
        self.on_change, self.on_relaunch = on_change, on_relaunch
        self.proc = None
        self.quitting = self.relaunch = False
        self.wake = threading.Event()
        self.state, self.reason = "waiting", "啟動中…"

    def set(self, state, reason):
        self.state, self.reason = state, reason
        self.on_change()

    def heartbeat(self):
        while not self.quitting:
            try:
                HEARTBEAT.write_text(datetime.now().strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
            except OSError:
                pass
            time.sleep(60)

    def loop(self):
        if HEARTBEAT.exists():
            when = HEARTBEAT.read_text(encoding="utf-8", errors="replace").strip()
            msg = f"上次機器人在 {when} 左右停止（電腦關機、睡眠或程式被強制結束）"
            write_log(msg)
            threading.Thread(target=tell_user, args=(f"⚠️ {msg}，現在已重新啟動。",), daemon=True).start()
        threading.Thread(target=self.heartbeat, daemon=True).start()
        write_log("啟動")
        while not self.quitting:
            self.proc = subprocess.Popen(
                [python_exe(), "-X", "utf8", "-u", str(HERE / "bot.py")],
                cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            pump = threading.Thread(target=pump_log, args=(self.proc.stdout,), daemon=True)
            pump.start()
            self.set("running", "執行中")
            rc = self.proc.wait()
            pump.join(5)  # 最後幾行寫完再往下
            if self.quitting:
                break
            if rc == RESTART_CODE:  # 從 Telegram 要求重新啟動：連圖示一起重開，順便更新下載工具
                write_log("從 Telegram 重新啟動")
                self.relaunch = self.quitting = True
                self.on_relaunch()
                break
            restarting = self.wake.is_set()
            write_log(f"機器人結束，代碼 {rc}" + ("（手動重新啟動）" if restarting else ""))
            if not restarting:
                text = f"⚠️ TG 下載機器人停止了（代碼 {rc}）\n\n最後的紀錄：\n{last_log_lines()}"
                threading.Thread(target=tell_user, args=(text,), daemon=True).start()
            if rc == 2:
                self.set("error", "token 不對，請執行 configure.bat 後按「重新啟動」")
                self.wake.wait()
            elif rc == 3:
                self.set("error", "已經有另一個機器人在跑（可能開了 start.bat）")
                self.wake.wait()
            elif not self.wake.is_set():
                self.set("waiting", f"程式結束了（代碼 {rc}），10 秒後自動重開")
                self.wake.wait(10)
            self.wake.clear()

    def restart(self):
        self.set("waiting", "重新啟動中…")
        self.wake.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def quit(self):
        write_log("手動結束")
        tell_user("⏹ TG 下載機器人已從工作列手動關閉。", tries=1)
        try:
            HEARTBEAT.unlink()
        except OSError:
            pass
        self.quitting = True
        self.wake.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def make_icon(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(42, 171, 238))  # Telegram 藍
    d.rectangle((27, 14, 37, 34), fill="white")      # 下載箭頭
    d.polygon([(18, 32), (46, 32), (32, 48)], fill="white")
    d.ellipse((40, 40, 62, 62), fill=color, outline="white", width=3)  # 狀態燈
    return img


def main():
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 47832))  # 圖示只開一個
    except OSError:
        return
    import pystray

    icon = pystray.Icon("tg-downloader", make_icon(COLORS["waiting"]), "TG 下載機器人：啟動中…")

    def refresh():
        try:
            icon.icon = make_icon(COLORS[runner.state])
            icon.title = f"TG 下載機器人：{runner.reason}"[:127]
            icon.update_menu()
        except Exception:
            pass
        if runner.state == "error":
            try:
                icon.notify(runner.reason, "TG 下載機器人")
            except Exception:
                pass

    runner = Runner(refresh, lambda: icon.stop())

    def on_quit(icon_, item):
        runner.quit()
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem(lambda item: runner.reason, lambda *_: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("看紀錄", lambda *_: os.startfile(LOG), default=True),
        pystray.MenuItem("開啟資料夾", lambda *_: os.startfile(HERE)),
        pystray.MenuItem("重新啟動", lambda *_: runner.restart()),
        pystray.MenuItem("結束", on_quit),
    )

    def setup(ic):
        ic.visible = True
        threading.Thread(target=runner.loop, daemon=True).start()

    icon.run(setup)
    if runner.relaunch:  # 交給 start-hidden.vbs 重開（它會先更新 yt-dlp / gallery-dl）
        try:
            HEARTBEAT.unlink()
        except OSError:
            pass
        lock.close()  # 讓新的圖示開得起來
        subprocess.Popen(["wscript", str(HERE / "start-hidden.vbs")], cwd=HERE)


if __name__ == "__main__":
    main()
