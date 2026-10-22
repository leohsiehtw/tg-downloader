"""
TG Downloader：把網址裡的影片 / 圖片下載下來，貼到指定的 Telegram 頻道；
沒設 CHANNEL_ID 就直接傳回你跟機器人的私訊聊天。

兩種收網址的方式（可同時開）：
  1. HTTP：iPhone 捷徑 POST /download  {"url": "..."}，header 帶 X-Secret
  2. Telegram：直接把連結分享 / 傳給這個機器人（只接受 ALLOWED_USER_IDS 裡的人）

  3. 上傳資料夾：把檔案丟進「上傳」資料夾，機器人私訊問你要貼到哪個子頻道
  4. 壓縮檔：把 zip / 7z / rar（含分卷）轉傳給機器人，解壓後把圖片、影片貼到頻道；要密碼會私訊問你

私訊機器人 /status 看進度和電腦狀態（CPU、RAM、GPU）、/restart 重新啟動（在工作列圖示底下跑時才有用）。

目標若是開了「話題 (Topics)」的群組，可用 TOPICS 設定子頻道，傳連結時選要貼到哪一個。

設定全部來自環境變數，見 .env.example。
"""
import collections
import contextlib
import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

# 讀同資料夾的 .env（不覆蓋已存在的環境變數）
_env = Path(__file__).with_name(".env")
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()  # 例如 -1001234567890 或 @mychannel；留空＝傳回私訊聊天
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
PORT = int(os.environ.get("PORT", "8080"))
ENABLE_POLLING = os.environ.get("ENABLE_POLLING", "1") == "1"
# 官方 Bot API 上傳上限 50MB；自架 telegram-bot-api 可到 2000MB
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
# 設 1 就改用你自己的帳號上傳（要先跑 login.bat），上限 2GB
# UPLOAD_AS_USER：0＝全部機器人傳；big＝50MB 以下機器人傳、超過才用你的帳號；1＝全部用你的帳號
UPLOAD_AS_USER = os.environ.get("UPLOAD_AS_USER", "0") in ("1", "big")
ONLY_BIG_AS_USER = os.environ.get("UPLOAD_AS_USER", "0") == "big"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2000" if UPLOAD_AS_USER else "50"))
# 選用：IG / X 等需要登入的網站。匯出的 cookies 檔丟進 cookies 資料夾就好（檔名隨意），
# 下載工具會依網址自己挑那個網站的 cookies。設了 COOKIES_FILE 就只用那個檔。
COOKIES_FILE = os.environ.get("COOKIES_FILE") or None
COOKIE_DIR = Path(__file__).with_name("cookies")
# 自架 telegram-bot-api 用 --local 跑在同一台電腦時設 1：直接給檔案路徑，不必整個檔案讀進記憶體上傳
LOCAL_BOT_API = os.environ.get("LOCAL_BOT_API", "0") == "1"
BOT_MAX_BYTES = (2000 if LOCAL_BOT_API else 50) * 1024 * 1024  # 機器人自己能傳的上限
TEMP_DIR = os.environ.get("TEMP_DIR") or None  # 下載暫存資料夾，預設系統暫存
DOCKER_PATH_PREFIX = os.environ.get("DOCKER_PATH_PREFIX", "").rstrip("/")  # TEMP_DIR 在 Docker 裡的路徑


def _parse_topics(raw):
    """TOPICS=Q:12,Ns:34,General:0 → {"Q": 12, ...}；0 代表 General（不帶 thread id）。"""
    out = {}
    for part in raw.split(","):
        if ":" in part:
            name, tid = part.rsplit(":", 1)
            if name.strip() and tid.strip().isdigit():
                out[name.strip()] = int(tid)
    return out


TOPICS = _parse_topics(os.environ.get("TOPICS", "")) if CHANNEL_ID else {}  # 私訊聊天沒有子頻道
# 丟進這個資料夾的檔案，機器人會私訊問你要貼到哪個子頻道
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or Path(__file__).with_name("上傳"))
FAILED_DIR = "失敗"
# 沒指定子頻道時：有填 DEFAULT_TOPIC 就直接用；沒填就跳按鈕讓你選（捷徑模式沒填則貼到 General）
DEFAULT_TOPIC = os.environ.get("DEFAULT_TOPIC", "").strip()

API = f"{API_BASE}/bot{BOT_TOKEN}"
MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # sendPhoto 上限，超過改用文件傳
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".wmv", ".flv", ".ts", ".mpg", ".mpeg", ".3gp"}
GIF_EXT = {".gif"}
URL_RE = re.compile(r"https?://\S+")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg-downloader")

# 下載可以好幾個同時跑；上傳一次一個（同時傳只會互搶頻寬，Telegram 也容易限流）
DOWNLOAD_WORKERS = int(os.environ.get("DOWNLOAD_WORKERS", "3"))
# 等下載的：(網址、檔案清單或壓縮檔, 通知對象, 子頻道 id, 拿來顯示進度的訊息 id)
jobs: "queue.Queue[tuple[str | list | ArchiveJob, int | None, int, int | None]]" = queue.Queue()
uploads: queue.Queue = queue.Queue()  # 下載好、等上傳的：(Progress, 上傳要做的事)
pending: dict = {}  # 等你按按鈕選子頻道的網址、檔案清單或壓縮檔
active = []  # 正在處理（下載中或等上傳、上傳中）的工作的 Progress
_count = {"outstanding": 0, "downloading": 0}  # 還沒做完的工作數、正在下載的數量
_count_lock = threading.Lock()
_local = threading.local()  # 每個執行緒各自在處理哪個工作（status() 用）
restart_when_idle = None  # 按了「做完再重啟」的人
poll_offset = None  # 已經處理到哪一則 Telegram 更新
FOREGROUND = sys.stdout.isatty()  # start.bat 開的視窗；在工作列圖示底下跑時是 False
RESTART_CODE = 4  # 結束代碼 4＝請工作列圖示整個重開
RESTART_FLAG = Path(__file__).with_name(".restart")  # 重開後要通知誰
STARTED = time.time()
LIVE_SECONDS = 3  # 進度和 /status 每幾秒更新一次


def find_topic(text):
    """訊息裡有子頻道名稱（不分大小寫，可加 #）就回傳它的 id。"""
    words = {w.lstrip("#").lower() for w in URL_RE.sub(" ", text).split()}
    for name, tid in TOPICS.items():
        if name.lower() in words:
            return tid
    return None


def default_topic():
    return TOPICS.get(DEFAULT_TOPIC, 0)


def topic_name(tid):
    return next((n for n, i in TOPICS.items() if i == tid), "")


def place(tid):
    """完成訊息裡「已傳到哪」。"""
    return topic_name(tid) or ("頻道" if CHANNEL_ID else "這個聊天")


def enqueue(what, chat, thread_id, msg_id=None):
    with _count_lock:
        _count["outstanding"] += 1
    jobs.put((what, chat, thread_id, msg_id))


def idle():
    return _count["outstanding"] == 0


def wait_note():
    """下載的位置都滿了才要排隊；回傳要附在訊息後面的說明，不用等就是空字串。"""
    if _count["downloading"] < DOWNLOAD_WORKERS:
        return ""
    n = jobs.qsize()
    return f"排隊中，前面還有 {n} 個" if n else "排隊中，等前面的下載完"


# ---------------- Telegram ----------------

# 所有執行緒共用一組和 Telegram 的連線。每則訊息、每個按鈕都在新的執行緒處理，
# 以前每個執行緒各開一條新連線（查 DNS + TLS 握手），按鈕按下去要等好幾秒才有反應。
_http = requests.Session()
_http.mount("https://", requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=32))


def tg(method, data=None, files=None, timeout=300):
    for attempt in range(3):
        try:
            r = _http.post(f"{API}/{method}", data=data, files=files, timeout=timeout)
        except requests.RequestException as e:
            if attempt == 0 and not files and isinstance(e, requests.ConnectionError):
                continue  # 放太久的連線被斷掉了，重新連一次
            # 錯誤訊息裡的網址含 token，記錄前先遮掉
            raise ConnectionError(str(e).replace(BOT_TOKEN, "***")) from None
        body = r.json()
        if body.get("ok"):
            return body["result"]
        wait = (body.get("parameters") or {}).get("retry_after")
        if (body.get("error_code") == 429 and wait and wait <= 30 and attempt < 2 and not files
                and method != "editMessageText"):  # 進度更新不用等，下一輪再改就好
            time.sleep(wait)  # 訊息送太快被 Telegram 限流，等它說的秒數再送
            continue
        raise RuntimeError(f"{method} 失敗：{body.get('description')}")


def notify(chat_id, text):
    if chat_id is None:
        return
    try:
        tg("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
    except Exception as e:
        log.warning("通知失敗：%s", e)


def edit(chat_id, msg_id, text):
    try:
        tg("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": text,
                               "disable_web_page_preview": True})
    except Exception:
        pass


# ---------------- 進度 ----------------

def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit in ("B", "KB") else f"{n:.1f}{unit}"
        n /= 1024


def bar(frac, width=10):
    n = max(0, min(width, round(frac * width)))
    return "▓" * n + "░" * (width - n)


def duration_text(sec):
    if sec < 60:
        return "不到 1 分鐘"
    if sec < 3600:
        return f"{sec // 60:.0f} 分鐘"
    if sec >= 86400:
        return f"{sec // 86400:.0f} 天 {sec % 86400 // 3600:.0f} 小時"
    return f"{sec // 3600:.0f} 小時 {sec % 3600 // 60:.0f} 分"


class Progress:
    """私訊裡一則會自己更新的進度訊息；每 LIVE_SECONDS 秒改一次（太快會被 Telegram 限流）。"""

    def __init__(self, chat, head, msg_id=None):
        self.chat, self.head, self.msg_id = chat, head, msg_id
        self.text, self.shown = "準備中…", None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        if chat is not None:
            threading.Thread(target=self._loop, daemon=True).start()

    def render(self):
        return f"{self.head}\n{self.text}"

    def _flush(self):
        with self.lock:
            text = self.render()
            if self.stopped.is_set() or text == self.shown:
                return
            try:
                if self.msg_id is None:
                    self.msg_id = tg("sendMessage", {"chat_id": self.chat, "text": text,
                                                     "disable_web_page_preview": True})["message_id"]
                else:
                    tg("editMessageText", {"chat_id": self.chat, "message_id": self.msg_id, "text": text,
                                           "disable_web_page_preview": True})
                self.shown = text
            except Exception as e:
                log.debug("更新進度失敗：%s", e)

    def _loop(self):
        while True:
            self._flush()
            # 同時好幾個工作時每則改慢一點，加起來才不會超過 Telegram 的限制、拖慢其他回覆
            if self.stopped.wait(LIVE_SECONDS * max(1, len(active))):
                return

    def close(self):
        """工作結束：收掉進度訊息（結果另外發一則新訊息，手機才會跳通知）。"""
        with self.lock:
            self.stopped.set()
            if self.msg_id is not None:
                try:
                    tg("deleteMessage", {"chat_id": self.chat, "message_id": self.msg_id})
                except Exception:
                    pass


def status(text, progress=None):
    progress = progress or getattr(_local, "progress", None)
    if progress is not None:
        progress.text = text


def upload_meter():
    """給 user_upload 回報上傳進度：第幾個、百分比、速度、還要多久。
    回報是從 user_upload 自己的執行緒呼叫的，所以先記住現在這個工作的進度訊息。"""
    start, progress = time.time(), getattr(_local, "progress", None)

    def report(name, idx, count, done, total):
        speed = done / max(time.time() - start, 0.1)
        frac = done / total if total else 1
        left = (total - done) / speed if speed else 0
        status(f"⬆️ 上傳中 {idx}/{count}：{name}\n{bar(frac)} {frac:.0%}（{human(done)} / {human(total)}）\n"
               f"{human(speed)}/s，約剩 {duration_text(left)}", progress)
    return report


def download_meter(name, idx, count, before, total):
    """下載轉傳來的檔案的進度；before＝前面幾個分卷已經下載的量，total＝全部分卷加起來。"""
    start, progress = time.time(), getattr(_local, "progress", None)

    def report(done, _size=None):
        speed = done / max(time.time() - start, 0.1)
        frac = (before + done) / total if total else 1
        left = (total - before - done) / speed if speed else 0
        status(f"⬇️ 下載中 {idx}/{count}：{name}\n{bar(frac)} {frac:.0%}（{human(before + done)} / {human(total)}）\n"
               f"{human(speed)}/s，約剩 {duration_text(left)}", progress)
    return report


def status_text():
    lines = [p.render() for p in list(active)] or ["沒有在處理的工作"]
    if not jobs.empty():
        lines.append(f"排隊中：{jobs.qsize()} 個")
    if pending:
        lines.append(f"等你選子頻道：{len(pending)} 個")
    if waiting:
        lines.append(f"等你給密碼：{len(waiting)} 個壓縮檔")
    return "\n\n".join(lines)


# ---------------- 電腦狀態（/status） ----------------

_net = []  # 上次量網路的 (時間, 已送出, 已收到)
_disk = []  # 上次量硬碟的 (時間, 已讀, 已寫)
_gpu = [0.0, []]  # (上次查的時間, 結果)
STATS = {"files": 0, "bytes": 0, "failed": 0, "downloaded": 0}  # 這次啟動後傳了多少、失敗幾次
TOTALS_FILE = Path(__file__).with_name(".totals.json")  # 從開始用到現在總共處理了多少（重啟也不會歸零）
_totals_lock = threading.Lock()


def count(**add):
    """記到這次啟動的統計，也加進累計（存在 .totals.json）。"""
    with _totals_lock:
        for k, v in add.items():
            STATS[k] = STATS.get(k, 0) + v
        try:
            totals = json.loads(TOTALS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            totals = {"since": time.strftime("%Y/%m/%d")}
        for k, v in add.items():
            totals[k] = totals.get(k, 0) + v
        try:
            tmp = TOTALS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(totals), encoding="utf-8")
            tmp.replace(TOTALS_FILE)
        except OSError as e:
            log.warning("存累計統計失敗：%s", e)


def totals():
    try:
        return json.loads(TOTALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
PS_GPU = (r"$n = (Get-CimInstance Win32_VideoController).Name -join ' / '; "
          r"$s = (Get-Counter '\GPU Engine(*engtype_3D)\Utilization Percentage' -ErrorAction SilentlyContinue)"
          r".CounterSamples | Measure-Object CookedValue -Sum; "
          r"$u = if ($s) { [math]::Round($s.Sum) } else { '' }; Write-Output ($n + '|' + $u)")


def gauge(label, pct, detail="", warn=(60, 85)):
    """一行：燈號（綠 / 黃 / 紅）＋名稱＋百分比＋補充。手機很窄，不放長條免得折行。"""
    dot = "🟢" if pct < warn[0] else "🟡" if pct < warn[1] else "🔴"
    return f"{dot} {label}　{pct:.0f}%" + (f"　{detail}" if detail else "")


def gpu_lines():
    """NVIDIA 顯卡用 nvidia-smi（有溫度、顯存）；其他顯卡用 Windows 效能計數器看使用率。"""
    now = time.time()
    if now - _gpu[0] < 10:
        return _gpu[1]
    lines = []
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
                              "temperature.gpu,fan.speed,power.draw,power.limit,utilization.encoder",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        for row in out.strip().splitlines():
            name, util, used, total, temp, fan, watt, limit, enc = (x.strip() for x in row.split(","))
            used, total = int(used) / 1024, int(total) / 1024
            extra = f"　風扇 {fan}%" if fan.isdigit() else ""  # 沒有風扇資訊的顯卡會給 [N/A]
            lines.append(gauge("顯示卡", int(util), f"{temp}°C" + (" 🔥" if int(temp) >= 80 else "") + extra))
            lines.append(gauge("顯　存", used / total * 100 if total else 0, f"{used:.1f} / {total:.1f} GB"))
            try:
                lines.append(gauge("功　耗", float(watt) / float(limit) * 100, f"{float(watt):.0f} / {float(limit):.0f} W"))
            except (ValueError, ZeroDivisionError):
                pass
            if enc.isdigit():
                lines.append(gauge("編碼器", int(enc), "壓縮影片用"))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if not lines and os.name == "nt":
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", PS_GPU],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
            name, util = out.rsplit("|", 1)
            if util.isdigit():
                lines.append(gauge("顯示卡", min(int(util), 100)))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    _gpu[:] = [now, lines]
    return lines


def pc_text():
    try:
        import psutil
    except ImportError:
        return "（要先 pip install psutil 才看得到 CPU / RAM）"
    now = time.time()
    mem = psutil.virtual_memory()
    # 名稱都用三個全形字，數字才會對齊
    lines = [gauge("處理器", psutil.cpu_percent(), f"{psutil.cpu_count()} 執行緒"),
             gauge("記憶體", mem.percent, f"{mem.used / 2**30:.1f} / {mem.total / 2**30:.0f} GB"),
             *gpu_lines(), "", "💾 硬碟用量"]
    for part in psutil.disk_partitions():  # 每一顆硬碟（光碟機、沒插的讀卡機會略過）
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            du = shutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        if du.total:
            name = part.mountpoint.rstrip("\\/:") or part.mountpoint
            if len(name) == 1:  # C → Ｃ槽（全形字母，幾顆硬碟的數字才會對齊）
                name = chr(ord(name.upper()) + 0xFEE0) + "槽"
            lines.append(gauge(name, du.used / du.total * 100,
                               f"剩 {du.free / 2**30:.0f} / {du.total / 2**30:.0f} GB", warn=(80, 90)))
    io = psutil.disk_io_counters()
    if io and _disk:
        t, read, wrote = _disk
        dt = max(now - t, 0.1)
        lines.append(f"💽 讀 {human((io.read_bytes - read) / dt)}/s　寫 {human((io.write_bytes - wrote) / dt)}/s")
    if io:
        _disk[:] = [now, io.read_bytes, io.write_bytes]
    lines.append("")
    net = psutil.net_io_counters()
    if _net:
        t, sent, recv = _net
        dt = max(now - t, 0.1)
        lines.append(f"🌐 網路　↑ {human((net.bytes_sent - sent) / dt)}/s　↓ {human((net.bytes_recv - recv) / dt)}/s")
    _net[:] = [now, net.bytes_sent, net.bytes_recv]
    top = busiest(psutil)
    if top:
        lines.append(f"🔝 最忙的程式：{top}")
    lines.append(f"⏱ 機器人已執行 {duration_text(now - STARTED)}")
    lines.append(f"🔌 電腦已開機 {duration_text(now - psutil.boot_time())}")
    lines += ["", "📦 處理了多少"]
    lines.append(f"　這次啟動：下載 {human(STATS['downloaded'])}・上傳 {STATS['files']} 個（{human(STATS['bytes'])}）"
                 + (f"・失敗 {STATS['failed']} 次" if STATS["failed"] else ""))
    t = totals()
    if t.get("since"):
        lines.append(f"　{t['since']} 起：下載 {human(t.get('downloaded', 0))}・上傳 {t.get('files', 0)} 個"
                     f"（{human(t.get('bytes', 0))}）")
    return "\n".join(lines)


def busiest(psutil):
    """CPU 用最多的程式（跟上次查的時候比）。第一次查還沒有數字，會是空的。"""
    cores = psutil.cpu_count() or 1
    best = None
    for proc in psutil.process_iter(["name", "cpu_percent"]):
        info = proc.info
        if proc.pid == 0 or not info.get("cpu_percent"):  # pid 0 是 Windows 的「系統閒置處理程序」
            continue
        if best is None or info["cpu_percent"] > best[1]:
            best = (info["name"], info["cpu_percent"])
    return f"{best[0]} {best[1] / cores:.0f}%" if best and best[1] / cores >= 1 else ""


_dashboard = {}  # 正在更新的 /status 訊息


def live_status(chat, minutes=5):
    """/status：送一則訊息，接下來幾分鐘每 LIVE_SECONDS 秒更新；再打一次 /status 會換成新的一則。"""
    old = _dashboard.get("stop")
    if old:
        old.set()
    stop = _dashboard["stop"] = threading.Event()

    def run():
        msg_id, shown, body, end = None, None, "", time.time() + minutes * 60
        try:  # 先馬上回一則，讀電腦狀態要一兩秒
            shown = f"🖥 電腦狀態・讀取中…\n\n📋 工作\n{status_text()}"
            msg_id = tg("sendMessage", {"chat_id": chat, "text": shown,
                                        "disable_web_page_preview": True})["message_id"]
        except Exception as e:
            log.debug("回覆 /status 失敗：%s", e)
        while not stop.is_set() and time.time() < end:
            body = f"{pc_text()}\n\n📋 工作\n{status_text()}"
            text = f"🖥 電腦狀態・{time.strftime('%H:%M:%S')} 更新\n\n{body}"
            if text != shown:
                try:
                    if msg_id is None:
                        msg_id = tg("sendMessage", {"chat_id": chat, "text": text,
                                                    "disable_web_page_preview": True})["message_id"]
                    else:
                        tg("editMessageText", {"chat_id": chat, "message_id": msg_id, "text": text,
                                               "disable_web_page_preview": True})
                    shown = text
                except Exception as e:
                    log.debug("更新 /status 失敗：%s", e)
            stop.wait(LIVE_SECONDS)
        if msg_id is not None and shown:
            edit(chat, msg_id, f"🖥 電腦狀態・已停止更新\n\n{body}\n\n再打 /status 看最新的")
    threading.Thread(target=run, daemon=True).start()


def kind_of(path: Path):
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "photo" if path.stat().st_size <= PHOTO_MAX_BYTES else "document"
    if ext in VIDEO_EXT:
        return "video"
    if ext in GIF_EXT:
        return "animation"
    return "document"


def target(thread_id, chat=None):
    """要貼到哪：有設 CHANNEL_ID 就貼到群組 / 頻道，沒設就傳回發送者的聊天。"""
    dest = CHANNEL_ID or chat
    if not dest:
        raise RuntimeError("沒設 CHANNEL_ID，也不知道要傳回哪個聊天（ALLOWED_USER_IDS 是空的？）")
    d = {"chat_id": dest}
    if thread_id and CHANNEL_ID:
        d["message_thread_id"] = thread_id
    return d


def send_files(paths, caption, thread_id=0, chat=None):
    """照片/影片每 10 個一組用相簿送，其餘逐一送。回傳 Telegram 確認收到的則數。"""
    group = [p for p in paths if kind_of(p) in ("photo", "video")]
    others = [p for p in paths if p not in group]
    first = True
    sent = 0

    for i in range(0, len(group), 10):
        batch = group[i:i + 10]
        if len(batch) == 1:
            others.insert(0, batch[0])
            continue
        media, files = [], {}
        for n, p in enumerate(batch):
            key = f"f{n}"
            item = {"type": kind_of(p), "media": local_uri(p) if LOCAL_BOT_API else f"attach://{key}"}
            if item["type"] == "video":
                item["supports_streaming"] = True
            if first and n == 0:
                item["caption"] = caption
            media.append(item)
            if not LOCAL_BOT_API:
                files[key] = open(p, "rb")
        status(f"⬆️ 上傳中（相簿 {i // 10 + 1}，{len(batch)} 個檔案）")
        try:
            sent += len(tg("sendMediaGroup", {**target(thread_id, chat), "media": json.dumps(media)}, files or None))
        finally:
            for f in files.values():
                f.close()
        first = False

    for p in others:
        k = kind_of(p)
        method = {"photo": "sendPhoto", "video": "sendVideo",
                  "animation": "sendAnimation", "document": "sendDocument"}[k]
        data = target(thread_id, chat)
        if first:
            data["caption"] = caption
        if k == "video":
            data["supports_streaming"] = "true"
        status(f"⬆️ 上傳中：{p.name}")
        if LOCAL_BOT_API:
            data[k] = local_uri(p)
            tg(method, data)
        else:
            with open(p, "rb") as f:
                tg(method, data, {k: f})
        sent += 1
        first = False
    return sent


def local_uri(p: Path):
    """給本機 telegram-bot-api 的檔案路徑；Docker 版要換成容器內看到的路徑。"""
    if DOCKER_PATH_PREFIX and TEMP_DIR:
        rel = p.resolve().relative_to(Path(TEMP_DIR).resolve())
        return f"file://{DOCKER_PATH_PREFIX}/{rel.as_posix()}"
    return p.resolve().as_uri()


# ---------------- 下載 ----------------

def run_tool(cmd, on_line=None, timeout=3600):
    """跑外部程式，邊跑邊把每一行輸出交給 on_line 顯示進度；on_line 回傳 True 的行不算進錯誤訊息。"""
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    tail = collections.deque(maxlen=20)
    try:
        for line in p.stdout:
            line = line.rstrip()
            try:
                consumed = on_line(line) if on_line else False
            except Exception:
                consumed = False
            if line and not consumed:
                tail.append(line)
    finally:
        timer.cancel()
        p.wait()
    return subprocess.CompletedProcess(cmd, p.returncode, "\n".join(tail), "")


YTDLP_PCT = re.compile(r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*(\S+)(?:\s+at\s+(\S+/s))?")


_cookies = [None, None]  # 上次看到的 (檔名, 修改時間, 大小) 清單、合併好的檔案
_cookie_lock = threading.Lock()


def cookie_file():
    """每次下載前看一下 cookies 資料夾（只比對修改時間，幾乎不花時間）；有換檔才重新合併，不用重啟。"""
    if COOKIES_FILE:
        return COOKIES_FILE
    sources = cookie_sources()
    try:
        stamp = [(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in sources]
        if stamp != _cookies[0]:
            merged = Path(__file__).with_name(".cookies-all.txt")
            if sources:  # 每個網站各一個檔，合併成一個給下載工具用
                merged.write_text("# Netscape HTTP Cookie File\n" + "\n".join(
                    p.read_text(encoding="utf-8-sig", errors="replace") for p in sources), encoding="utf-8")
                log.info("使用登入 cookies：%s", "、".join(p.name for p in sources))
            _cookies[:] = [stamp, str(merged) if sources else None]
    except OSError as e:  # 剛好在換檔，先用上一次的
        log.warning("讀 cookies 失敗：%s", e)
    return _cookies[1]


def job_cookies(out: Path):
    """每個下載各用一份 cookies：yt-dlp 結束時會把 cookies 寫回檔案，同時下載才不會互相蓋掉。"""
    with _cookie_lock:
        cookies = cookie_file()
        if not cookies:
            return None
        mine = out / ".cookies.txt"
        shutil.copyfile(cookies, mine)
        return str(mine)


def run_ytdlp(url, out: Path):
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "--restrict-filenames",
        # 最高解析度優先；同解析度時優先選 iPhone 能直接播的 H.264 / AAC
        "-f", "bv*+ba/b", "-S", "res,fps,vcodec:h264,acodec:aac",
        "--merge-output-format", "mp4",
        "-o", str(out / "%(id)s.%(ext)s"), url,
    ]
    cookies = job_cookies(out)
    if cookies:
        cmd[3:3] = ["--cookies", cookies]

    def on_line(line):
        m = YTDLP_PCT.search(line)
        if m:
            pct = float(m[1])
            size = m[2].replace("iB", "B")  # yt-dlp 寫 MiB，跟其他地方一樣寫 MB
            speed = f"，{m[3].replace('iB', 'B')}" if m[3] else ""
            status(f"⬇️ 下載中 {bar(pct / 100)} {pct:.0f}%\n檔案 {size}{speed}")
            return True
        if line.startswith("[Merger]"):
            status("🎞 合併影像和聲音…")
    return run_tool(cmd, on_line)


def run_gallerydl(url, out: Path):
    cmd = [sys.executable, "-m", "gallery_dl", "-D", str(out), url]
    cookies = job_cookies(out)
    if cookies:
        cmd[3:3] = ["--cookies", cookies]
    count = [0, 0]  # 檔案數、總大小

    def on_line(line):
        if str(out) in line:  # gallery-dl 每下載完一個檔案印一行路徑
            count[0] += 1
            try:
                count[1] += Path(line.strip().lstrip("# ")).stat().st_size
            except OSError:
                pass
            status(f"⬇️ 下載中（已下載 {count[0]} 個檔案，{human(count[1])}）")
            return True
    return run_tool(cmd, on_line)


def video_codec(p: Path):
    try:
        return subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception:
        return ""


def video_seconds(p: Path):
    try:
        return float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(p)],
            capture_output=True, text=True, timeout=60).stdout.strip())
    except Exception:
        return 0.0


def ffmpeg_progress(head, total):
    """給 run_tool 的 on_line：ffmpeg -progress 每秒印 out_time_us=已處理的微秒數。"""
    def on_line(line):
        if "=" in line and " " not in line:
            k, v = line.split("=", 1)
            if k == "out_time_us" and v.isdigit() and total:
                frac = min(int(v) / 1e6 / total, 1)
                status(f"{head}\n{bar(frac)} {frac:.0%}")
            return True
    return on_line


AUDIO_KBPS = 128
_nvenc = [True]  # 顯卡壓縮失敗過一次，之後就直接用 CPU
_cuda_decode = [True]  # 讀原片也交給顯卡（不然 CPU 光解碼就很忙）；失敗過一次就改回 CPU 讀
# 顯卡壓縮用的 ffmpeg：新版要 610 以上的驅動，GTX 10 系列裝不到，所以另外放一份 8.0 在 ffmpeg-gpu 資料夾
_gpu_exe = Path(__file__).with_name("ffmpeg-gpu") / "ffmpeg.exe"
GPU_FFMPEG = str(_gpu_exe) if _gpu_exe.exists() else "ffmpeg"


_encode_lock = threading.RLock()


@contextlib.contextmanager
def encode_turn(name):
    """轉檔、壓縮很吃 CPU 和顯卡，同時下載好幾個時也一次只做一支，其他的等。"""
    if not _encode_lock.acquire(blocking=False):
        status(f"🎞 等前一支轉檔完：{name}")
        _encode_lock.acquire()
    try:
        yield
    finally:
        _encode_lock.release()


def shrink(p: Path, out_dir: Path | None = None):
    with encode_turn(p.name):
        return _shrink(p, out_dir)


def _shrink(p: Path, out_dir: Path | None = None):
    """影片超過上傳上限：降低位元率重新壓成 H.264，大小盡量接近上限；位元率太低時順便降解析度。
    有 NVIDIA 顯卡就用顯卡壓，快很多。壓不下來就回傳原檔，照舊當成太大的檔案處理。"""
    total = video_seconds(p)
    if not total:
        return p
    out = (out_dir or p.parent) / (p.stem + ".small.mp4")
    target = MAX_BYTES * 0.97  # 留一點給檔案格式本身和誤差
    for _ in range(3):
        kbps = int(target * 8 / total / 1000) - AUDIO_KBPS
        if kbps < 150:
            log.warning("%s 太長，壓到 %dMB 以下畫質會太差，不壓", p.name, MAX_UPLOAD_MB)
            return p
        # 以短邊算（直的影片也適用），位元率不夠時降解析度，畫面才不會糊成一塊
        short = (2160 if kbps >= 20000 else 1440 if kbps >= 10000 else 1080 if kbps >= 5000
                 else 720 if kbps >= 2500 else 480)
        scale = (f"scale='if(gte(iw,ih),-2,trunc(min({short},iw)/2)*2)'"
                 f":'if(gte(iw,ih),trunc(min({short},ih)/2)*2,-2)'")
        head = f"🗜 壓縮中：{p.name}\n超過 {MAX_UPLOAD_MB}MB，壓到 {target / 2**20:.0f}MB 左右"
        status(head)
        nvenc = ["h264_nvenc", "-preset", "p5"]
        encoders = []  # (程式, 放在 -i 前面的參數, 編碼器)，依序試
        if _nvenc[0] and _cuda_decode[0]:
            encoders.append((GPU_FFMPEG, ["-hwaccel", "cuda"], nvenc))
        if _nvenc[0]:
            encoders.append((GPU_FFMPEG, [], nvenc))
        encoders.append(("ffmpeg", [], ["libx264", "-preset", "medium"]))
        for exe, before_input, enc in encoders:
            r = run_tool([exe, "-v", "error", "-nostats", "-progress", "pipe:1", "-y", *before_input, "-i", str(p),
                          "-vf", scale, "-c:v", *enc, "-b:v", f"{kbps}k", "-maxrate", f"{kbps * 2}k",
                          "-bufsize", f"{kbps * 2}k", "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-sn", "-dn",
                          "-movflags", "+faststart", str(out)],
                         ffmpeg_progress(head, total), timeout=max(7200, int(total * 3)))
            if r.returncode == 0 and out.exists():
                break
            if before_input:
                log.info("顯卡讀原片不能用，改用 CPU 讀：%s", r.stdout[-200:])
                _cuda_decode[0] = False
            elif enc is nvenc:
                log.info("顯卡壓縮不能用，改用 CPU：%s", r.stdout[-200:])
                _nvenc[0] = False
        else:
            log.warning("壓縮失敗，維持原檔：%s", r.stdout[-300:])
            return p
        size = out.stat().st_size
        if size <= MAX_BYTES:
            log.info("壓縮 %s：%s → %s", p.name, human(p.stat().st_size), human(size))
            if out_dir is None:
                p.unlink()
            return out
        target *= MAX_BYTES * 0.97 / size  # 還是超過：照比例再壓一次
    out.unlink(missing_ok=True)
    return p


def fit_photo(p: Path, out_dir: Path | None = None):
    """超過 10MB 或長寬加起來超過 10000 的圖片，Telegram 只能當檔案傳，手機上要點開下載才看得到。
    這種就轉成剛好放得進的 JPEG 當照片貼（Telegram 顯示照片時本來就會再壓縮）。"""
    from PIL import Image, ImageOps
    try:
        with Image.open(p) as im:
            w, h = im.size
            if (p.stat().st_size <= PHOTO_MAX_BYTES and w + h <= 10000) or max(w, h) > 20 * min(w, h):
                return p  # 本來就能當照片，或長條圖當照片也傳不了
            status(f"🖼 縮小圖片：{p.name}")
            im = ImageOps.exif_transpose(im)
            if "A" in im.getbands():  # 透明的地方鋪白底
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            im = im.convert("RGB")
            scale = min(1, 9999 / (w + h))
            out = (out_dir or p.parent) / (p.stem + ".tg.jpg")
            for _ in range(8):
                size = (max(1, int(w * scale)), max(1, int(h * scale)))
                img = im if size == im.size else im.resize(size, Image.LANCZOS)
                for q in (92, 85):
                    img.save(out, "JPEG", quality=q, optimize=True)
                    if out.stat().st_size <= PHOTO_MAX_BYTES:
                        break
                else:
                    scale *= 0.8
                    continue
                break
            else:
                out.unlink()
                return p
        # 原圖要關掉才能刪（Windows）
        log.info("縮小圖片 %s：%dx%d %s → %dx%d %s", p.name, w, h, human(p.stat().st_size),
                 *size, human(out.stat().st_size))
        if out_dir is None:
            p.unlink()
        return out
    except Exception as e:
        log.warning("縮小圖片失敗，用檔案傳原圖：%s（%s）", p.name, e)
    return p


def make_playable(p: Path, out_dir: Path | None = None):
    """最高畫質常是 VP9 / AV1，iPhone 的 Telegram 不一定能播；這種就轉成同解析度的 H.264 mp4。
    H.264 / HEVC 但不是 mp4（例如 iPhone 的 .mov）只換外殼，不重新壓縮。超過上傳上限的交給 shrink。
    太大的圖片交給 fit_photo 縮成能當照片貼的大小。
    out_dir 有給時原檔保留（上傳資料夾用），沒給時用新檔取代原檔（下載的暫存檔）。"""
    if p.suffix.lower() in IMAGE_EXT:
        return fit_photo(p, out_dir)
    if p.suffix.lower() not in VIDEO_EXT:
        return p
    if p.stat().st_size > MAX_BYTES:
        return shrink(p, out_dir)
    codec = video_codec(p)
    if codec in ("h264", "hevc") and p.suffix.lower() == ".mp4":
        return p
    out = (out_dir or p.parent) / (p.stem + ".tg.mp4")
    if codec in ("h264", "hevc"):
        args = ["-c", "copy"]
    else:
        log.info("轉檔 %s（%s → H.264）", p.name, codec or "?")
        args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k"]
    with encode_turn(p.name):
        status(f"🎞 轉檔中：{p.name}")
        r = run_tool(["ffmpeg", "-v", "error", "-nostats", "-progress", "pipe:1", "-y", "-i", str(p), *args,
                      "-movflags", "+faststart", str(out)],
                     ffmpeg_progress(f"🎞 轉檔中：{p.name}", video_seconds(p)), timeout=7200)
    if r.returncode != 0 or not out.exists():
        log.warning("轉檔失敗，直接上傳原檔：%s", r.stdout[-300:])
        return p
    if out.stat().st_size > MAX_BYTES:  # 轉成 H.264 後變太大，從原檔直接壓到上限內
        out.unlink()
        return shrink(p, out_dir)
    if out_dir is None:
        p.unlink()
    return out


def media_in(folder: Path):
    exts = IMAGE_EXT | VIDEO_EXT | GIF_EXT
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts)


def download(url, folder: Path):
    """先用 gallery-dl（圖片 / 多圖貼文較強），沒抓到再用 yt-dlp（影片）。"""
    errors = []
    status("⬇️ 下載中…")
    for name, fn in (("gallery-dl", run_gallerydl), ("yt-dlp", run_ytdlp)):
        try:
            r = fn(url, folder)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        found = media_in(folder)
        if found:
            log.info("%s 下載到 %d 個檔案", name, len(found))
            return [make_playable(p) for p in found]
        errors.append(f"{name}: {(r.stderr or r.stdout).strip()[-300:]}")
    raise RuntimeError("下載不到任何媒體\n" + "\n".join(errors) + login_hint(url, errors))


# 各網站代表「有登入」的那個 cookie；匯出時沒登入就不會有它
LOGIN_COOKIE = {"instagram.com": "sessionid", "threads.net": "sessionid", "threads.com": "sessionid",
                "x.com": "auth_token", "twitter.com": "auth_token", "facebook.com": "c_user",
                "tiktok.com": "sessionid", "youtube.com": "SID", "pixiv.net": "PHPSESSID"}


def cookie_sources():
    """cookies 資料夾裡的 .txt；舊版放在主資料夾、檔名有 cookies 的 .txt 也照樣讀。"""
    if COOKIES_FILE:
        return [Path(COOKIES_FILE)]
    found = list(COOKIE_DIR.glob("*.txt")) + list(Path(__file__).parent.glob("*cookies*.txt"))
    return sorted((p for p in found if not p.name.startswith(".")), key=lambda p: p.name)


def login_hint(url, errors):
    """被導去登入頁 = 網站不認 cookies；看 cookies 檔判斷是沒放、匯出時沒登入、過期，還是被網站作廢。"""
    text = "\n".join(errors).lower()
    if not any(k in text for k in ("login", "log in", "logged in", "requested content is not available", "401")):
        return ""
    host = (urlparse(url).hostname or "").lower()
    site = ".".join(host.split(".")[-2:])
    want = LOGIN_COOKIE.get(site)
    hits = []  # (檔案, 這個網站的 cookies {名稱: 到期時間})
    for p in cookie_sources():
        try:
            lines = p.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        found = {}
        for line in lines:
            f = line.removeprefix("#HttpOnly_").split("\t")
            if len(f) >= 7 and not line.startswith("# ") and f[0].lstrip(".").endswith(site):
                found[f[5]] = int(f[4]) if f[4].isdigit() else 0
        if found:
            hits.append((p, found))
    where = f"cookies\\www.{site}_cookies.txt"
    fix = f"請在瀏覽器登入 {site}，確認能正常瀏覽後重新匯出 cookies，覆蓋 {{}}，不用重啟。"
    if not hits:
        return f"\n\n🔑 Cookie 問題：沒有 {site} 的 cookies。" + fix.format(where)
    # 有登入 cookie 的那份才是實際在用的；有好幾份時，合併後排後面的會蓋掉前面的
    p, found = ([h for h in hits if want in h[1]] or hits)[-1]
    name = p.name if p.parent == COOKIE_DIR else str(p)
    day = time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))
    head = f"\n\n🔑 Cookie 問題（{name}，{day} 匯出）："
    if want and want not in found:
        why = f"裡面沒有登入用的 {want}，匯出時這個瀏覽器沒有登入 {site}。"
    elif want and 0 < found[want] < time.time():
        why = f"登入用的 {want} 在 {time.strftime('%Y-%m-%d', time.localtime(found[want]))} 就過期了。"
    elif want:
        why = (f"登入用的 {want} 還沒到期，但 {site} 不認它，通常是換過帳號、在瀏覽器登出過，"
               "或帳號被要求驗證（手機 / email）。")
    else:
        why = f"{site} 把下載導去登入頁，這份 cookies 的登入大概失效了。"
    return head + why + fix.format(name)


def upload(files, caption, thread_id, chat=None):
    """上傳到頻道（沒設 CHANNEL_ID 就傳回 chat）；超過上限的不傳，回傳 (已上傳, 太大) 兩個清單。"""
    # 私訊模式下，你的帳號只能貼到你自己跟機器人的聊天；其他人的聊天一律機器人傳
    as_user = UPLOAD_AS_USER and (CHANNEL_ID or chat == user_uploader().me_id)
    limit = MAX_BYTES if as_user else BOT_MAX_BYTES
    too_big = [p for p in files if p.stat().st_size > limit]
    ok = [p for p in files if p not in too_big]
    if ok:
        status(f"⬆️ 上傳中（{len(ok)} 個檔案，{human(sum(p.stat().st_size for p in ok))}）")
        by_user = [p for p in ok if as_user and (not ONLY_BIG_AS_USER or p.stat().st_size > BOT_MAX_BYTES)]
        by_bot = [p for p in ok if p not in by_user]
        sent = 0
        if by_bot:
            sent += send_files(by_bot, caption=caption, thread_id=thread_id, chat=chat)
        if by_user:
            sent += user_uploader().send(by_user, caption="" if by_bot else caption, thread_id=thread_id,
                                         progress=upload_meter(), to=CHANNEL_ID or BOT_USERNAME)
        if sent != len(ok):
            raise RuntimeError(f"只確認到 {sent}/{len(ok)} 個檔案上傳成功")
        count(files=len(ok), bytes=total_size(ok))
    if too_big:
        names = ", ".join(f"{p.name} ({p.stat().st_size // 1048576}MB)" for p in too_big)
        tg("sendMessage", {**target(thread_id, chat),
                           "text": f"{caption}\n⚠️ 超過 {limit // 1048576}MB 上傳上限，未上傳：{names}".strip()})
    return ok, too_big


def handle(url, reply_chat, thread_id=0):
    """下載（在下載執行緒，可以好幾個同時）；回傳之後要在上傳執行緒做的事，失敗就回傳 None。"""
    folder = Path(tempfile.mkdtemp(prefix="tgdl-", dir=TEMP_DIR))

    def failed(e):
        log.exception("處理失敗 %s", url)
        count(failed=1)
        notify(reply_chat, f"❌ 失敗：{url}\n{str(e)[:500]}")

    try:
        files = download(url, folder)
    except Exception as e:
        failed(e)
        shutil.rmtree(folder, ignore_errors=True)
        return None
    count(downloaded=total_size(files))

    def send():
        try:
            ok, _ = upload(files, url, thread_id, reply_chat)
            squeezed = "，影片超過上限有壓縮畫質" if any(p.name.endswith(".small.mp4") for p in ok) else ""
            notify(reply_chat, f"✅ 已傳到{place(thread_id)}"
                               f"（{len(ok)} 個檔案，{human(total_size(ok))}{squeezed}）\n{url}")
        except Exception as e:
            failed(e)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    return send


_uploader = None
_uploader_lock = threading.Lock()


def user_uploader():
    global _uploader
    with _uploader_lock:  # 上傳和下載壓縮檔的執行緒可能同時要用
        if _uploader is None:
            from user_upload import Uploader
            _uploader = Uploader()
    return _uploader


def close_uploader():
    global _uploader
    with _uploader_lock:
        if _uploader is not None:
            _uploader.close()
            _uploader = None


def trash(p: Path):
    """丟進資源回收筒（不是永久刪除），需要時還能救回來。"""
    from send2trash import send2trash
    send2trash(str(p))


def move_to(p: Path, sub):
    dest_dir = UPLOAD_DIR / sub / p.parent.relative_to(UPLOAD_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / p.name
    if dest.exists():
        dest = dest_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{p.name}"
    shutil.move(str(p), str(dest))


def handle_files(paths, reply_chat, thread_id=0):
    """上傳資料夾裡的一批檔案。Telegram 確認整批都收到了才把原檔丟進資源回收筒；
    只要有一個沒成功（或太大），整批都不刪，移到「失敗」資料夾。"""
    names = "、".join(p.name for p in paths[:5]) + ("…" if len(paths) > 5 else "")
    tmp = Path(tempfile.mkdtemp(prefix="tgup-", dir=TEMP_DIR))

    def failed(e):
        log.exception("資料夾上傳失敗 %s", names)
        count(failed=1)
        for p in paths:
            if p.exists():
                move_to(p, FAILED_DIR)
        notify(reply_chat, f"❌ 資料夾上傳失敗：{names}\n{str(e)[:500]}\n檔案移到「失敗」資料夾了。")

    count(downloaded=total_size(paths))  # 資料夾裡的檔案算「處理」的量
    try:  # 轉檔、壓縮在下載執行緒做
        sendable = {make_playable(p, tmp): p for p in paths}  # 上傳用的檔案 → 原檔
    except Exception as e:
        failed(e)
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    return lambda: send_folder(list(sendable), paths, names, tmp, failed, reply_chat, thread_id)


def send_folder(sendable, paths, names, tmp, failed, reply_chat, thread_id):
    try:
        ok, too_big = upload(sendable, "", thread_id, reply_chat)
        size = human(total_size(ok))  # 先算好，原檔等一下會丟進回收筒
        if any(p.name.endswith(".small.mp4") for p in ok):
            size += "，影片超過上限有壓縮畫質"
        if too_big:
            for p in paths:
                move_to(p, FAILED_DIR)
            notify(reply_chat, f"⚠️ {names}\n有 {len(too_big)} 個超過上傳上限，這批都沒刪，移到「失敗」資料夾了。")
            return
        for p in paths:
            trash(p)
        notify(reply_chat, f"✅ {len(ok)} 個檔案（{size}）全部傳到{place(thread_id)}，"
                           f"原檔已丟進資源回收筒：{names}")
    except Exception as e:
        failed(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 壓縮檔（轉傳給機器人的 zip / 7z / rar） ----------------

ARCHIVE_RE = re.compile(r"\.(zip|7z|rar|z\d\d|r\d\d|\d{3})$", re.I)
PARTS_WAIT = 6  # 收到壓縮檔後等幾秒，同一組的分卷一起轉傳時會陸續進來
PASSWORD_WAIT = 24 * 3600  # 等你給密碼最多等多久，過了就把下載的檔案刪掉
PASSWORDS_FILE = Path(__file__).with_name(".passwords.json")  # 解開過的密碼，下次先試
BOT_API_FILE_MAX = 20 * 1024 * 1024  # 機器人自己（Bot API）只能下載 20MB 以下的檔案
_parts = {}  # (對話, 壓縮檔名) → 陸續收到的分卷
_parts_lock = threading.Lock()
_topic_hint = {}  # 對話 → (子頻道 id, 時間)：轉傳時另外打的子頻道名稱
waiting = {}  # 等你給密碼的壓縮檔
BOT_USERNAME = ""


class NeedPassword(Exception):
    pass


class ArchiveJob:
    """一個壓縮檔（可能有好幾個分卷）。等密碼時下載好的分卷留在 folder，拿到密碼再解。"""

    def __init__(self, parts, name, caption):
        self.parts, self.name, self.caption = parts, name, caption
        self.folder = None
        self.password = None  # 你傳來的密碼；None＝還沒給，先自己猜
        self.wrong = []  # 你給過但不對的密碼

    @property
    def size(self):
        return sum(p["size"] for p in self.parts)

    @property
    def label(self):
        more = f"，{len(self.parts)} 個分卷" if len(self.parts) > 1 else ""
        return f"🗜 {self.name}（{human(self.size)}{more}）"


def is_archive(name):
    return bool(ARCHIVE_RE.search(name or ""))


def safe_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "archive"


def archive_key(name):
    """同一組分卷的共同名字：a.7z.001 / a.part2.rar / a.z01 → a.7z / a.rar / a.zip。"""
    for pat, ext in ((r"(.+)\.part\d+\.rar$", ".rar"), (r"(.+\.(?:7z|zip|rar))\.\d{3}$", ""),
                     (r"(.+)\.(?:z\d\d|zip)$", ".zip"), (r"(.+)\.(?:r\d\d|rar)$", ".rar"), (r"(.+)\.\d{3}$", "")):
        m = re.match(pat, name, re.I)
        if m:
            return m[1] + ext
    return name


def natural(p):
    """1、2、10 照數字排，不是 1、10、2。"""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(p).lower())]


def first_volume(paths):
    """要交給 7-Zip 的那個檔：.001、.part1.rar，或沒有編號的 .zip / .rar / .7z（其他分卷它會自己找）。"""
    names = sorted(paths, key=natural)
    for test in (lambda n: n.endswith(".001"), lambda n: re.search(r"\.part0*1\.rar$", n),
                 lambda n: n.endswith((".zip", ".rar", ".7z"))):
        hit = next((p for p in names if test(p.name.lower())), None)
        if hit:
            return hit
    return names[0]


def seven_zip():
    found = [shutil.which("7z"), shutil.which("7za")]
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if os.environ.get(env):
            found.append(str(Path(os.environ[env]) / "7-Zip" / "7z.exe"))
    exe = next((c for c in found if c and Path(c).exists()), None)
    if not exe:
        raise RuntimeError("找不到 7-Zip，請執行 configure.bat 安裝（或到 https://www.7-zip.org 下載安裝）")
    return exe


def saved_passwords():
    try:
        return json.loads(PASSWORDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def remember_password(pw):
    old = [p for p in saved_passwords() if p != pw]
    try:
        PASSWORDS_FILE.write_text(json.dumps([pw] + old[:49], ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("存密碼失敗：%s", e)


def guess_passwords(job):
    """檔名和說明裡常寫著密碼：@帳號、「密碼：xxx」、「解壓碼 xxx」；再加上以前解開過的。"""
    text = " ".join([job.name, job.caption] + [p["name"] for p in job.parts])
    found = []
    for m in re.finditer(r"(?:密[码碼]|解[压壓][码碼]?|pass(?:word)?|pwd)\s*[:：=是为為]?\s*([^\s，,。]+)", text, re.I):
        found.append(m[1])
    for m in re.finditer(r"@([A-Za-z0-9_]{3,})", text):
        found += ["@" + m[1], m[1]]
    return list(dict.fromkeys(found + saved_passwords()))


def fetch_parts(job):
    """下載每個分卷：有開「用你的帳號」就用帳號下載（2GB 內都行），不然用機器人（只能 20MB 以下）。"""
    total, before = job.size, 0
    for i, part in enumerate(job.parts, 1):
        dest = job.folder / part["name"]
        report = download_meter(part["name"], i, len(job.parts), before, total)
        if UPLOAD_AS_USER and (not ONLY_BIG_AS_USER or part["size"] > BOT_API_FILE_MAX):
            user_uploader().download(BOT_USERNAME, part["orig_name"], part["size"], dest,
                                     part["origin"], report)
        else:
            bot_download(part, dest, report)
        before += part["size"]


def bot_download(part, dest, report):
    if part["size"] > BOT_API_FILE_MAX and not LOCAL_BOT_API:
        raise RuntimeError("機器人自己只能下載 20MB 以下的檔案；大檔要先在 /setup 登入你的帳號")
    path = tg("getFile", {"file_id": part["file_id"]})["file_path"]
    if LOCAL_BOT_API and os.path.isabs(path):
        shutil.copyfile(path, dest)
        return
    try:
        with requests.get(f"{API_BASE}/file/bot{BOT_TOKEN}/{path}", stream=True, timeout=300) as r, \
                open(dest, "wb") as f:
            r.raise_for_status()
            done = 0
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                report(done)
    except requests.RequestException as e:  # 錯誤訊息裡的網址含 token
        raise ConnectionError(str(e).replace(BOT_TOKEN, "***")) from None


def extract(job, out: Path):
    """用 7-Zip 只解出圖片和影片。沒密碼先直接解，要密碼就依序試猜的；都不對丟 NeedPassword。"""
    exe = seven_zip()
    first = first_volume([job.folder / p["name"] for p in job.parts])
    only = [f"-ir!*{ext}" for ext in sorted(IMAGE_EXT | VIDEO_EXT | GIF_EXT)]
    tries = [job.password] if job.password is not None else ["", *guess_passwords(job)]
    for n, pw in enumerate(tries, 1):
        shutil.rmtree(out, ignore_errors=True)
        status("📦 解壓中…" if not pw else f"🔑 試密碼 {n - 1}/{len(tries) - 1}…" if job.password is None
               else "📦 用你給的密碼解壓中…")
        r = run_tool([exe, "x", "-y", "-bso0", "-bsp0", "-sccUTF-8", f"-p{pw}", f"-o{out}", str(first), *only],
                     timeout=7200)
        if "wrong password" in r.stdout.lower():
            continue
        if r.returncode not in (0, 1):  # 1＝有警告但有解出來
            raise RuntimeError(f"解壓失敗（分卷不齊或檔案壞掉？）\n{r.stdout[-400:]}")
        if pw:
            remember_password(pw)
            log.info("%s 的密碼是 %s", job.name, pw)
        return pw
    shutil.rmtree(out, ignore_errors=True)
    raise NeedPassword()


def archive_media(out: Path):
    """解出來的圖片和影片，照檔名的數字順序；略過 Mac 壓縮時夾帶的 __MACOSX、._ 檔。"""
    found = [p for p in media_in(out) if "__MACOSX" not in p.parts and not p.name.startswith("._")]
    return sorted(found, key=natural)


def handle_archive(job, reply_chat, thread_id=0):
    """下載壓縮檔、解壓（在下載執行緒）；回傳之後要在上傳執行緒做的事。要密碼時先私訊問你，回傳 None。"""
    def failed(e):
        log.exception("壓縮檔處理失敗 %s", job.name)
        count(failed=1)
        notify(reply_chat, f"❌ 失敗：{job.name}\n{str(e)[:500]}")
        shutil.rmtree(job.folder, ignore_errors=True)

    try:
        if job.folder is None:  # 第一次：先下載（等密碼回來時已經下載好了）
            job.folder = Path(tempfile.mkdtemp(prefix="tgzip-", dir=TEMP_DIR))
            fetch_parts(job)
            count(downloaded=job.size)
        out = job.folder / "解壓"
        pw = extract(job, out)
        files = archive_media(out)
        if not files:
            notify(reply_chat, f"⚠️ {job.name} 裡沒有圖片或影片，什麼都沒貼。")
            shutil.rmtree(job.folder, ignore_errors=True)
            return None
        files = [make_playable(p) for p in files]
    except NeedPassword:
        if job.password is not None:  # 你給的密碼不對
            job.wrong.append(job.password)
        ask_password(job, reply_chat, thread_id)
        return None
    except Exception as e:
        failed(e)
        return None
    caption = Path(job.name).stem if job.name.lower().endswith((".zip", ".7z", ".rar")) else job.name

    def send():
        try:
            ok, _ = upload(files, caption, thread_id, reply_chat)
            photos = sum(1 for p in ok if p.suffix.lower() in IMAGE_EXT | GIF_EXT)
            key = f"，密碼 {pw}" if pw else ""
            notify(reply_chat, f"✅ {job.name} 解壓後已傳到{place(thread_id)}"
                               f"（圖片 {photos} 張、影片 {len(ok) - photos} 支，{human(total_size(ok))}{key}）")
        except Exception as e:
            failed(e)
        finally:
            shutil.rmtree(job.folder, ignore_errors=True)
    return send


def ask_password(job, chat, thread_id):
    """私訊問密碼；你直接傳密碼（好幾個在等時回覆那一則）就會接著解。"""
    if chat is None:
        shutil.rmtree(job.folder, ignore_errors=True)
        return
    key = f"{int(time.time() * 1000) % 10**9}-{len(waiting)}"
    if job.wrong:
        text = f"🔒 「{job.wrong[-1]}」不對，{job.name} 請再傳一次密碼。"
    else:
        guesses = guess_passwords(job)
        tried = f"\n（試過 {len(guesses)} 個可能的密碼都不對）" if guesses else ""
        text = f"🔒 {job.name} 需要密碼。{tried}\n直接把密碼傳給我就好；有好幾個在等時，請回覆這則再傳。"
    try:
        msg_id = tg("sendMessage", {"chat_id": chat, "text": text, "disable_web_page_preview": True,
                                    "reply_markup": json.dumps({"inline_keyboard": [
                                        [{"text": "不解了，刪掉", "callback_data": f"p|{key}"}]]})})["message_id"]
    except Exception as e:
        log.warning("問密碼失敗：%s", e)
        shutil.rmtree(job.folder, ignore_errors=True)
        return
    timer = threading.Timer(PASSWORD_WAIT, give_up_password, (key, "⌛ 等了一天沒收到密碼"))
    timer.daemon = True
    waiting[key] = {"job": job, "chat": chat, "thread": thread_id, "msg": msg_id, "timer": timer}
    timer.start()


def give_up_password(key, why):
    w = waiting.pop(key, None)
    if w is None:
        return
    w["timer"].cancel()
    shutil.rmtree(w["job"].folder, ignore_errors=True)
    edit(w["chat"], w["msg"], f"{why}，{w['job'].name} 下載的檔案已刪掉。要解的話請重新轉傳。")


def take_password(chat, text, msg):
    """你傳來的文字是不是某個壓縮檔的密碼；是的話排回佇列接著解，回傳 True。"""
    mine = [k for k, w in list(waiting.items()) if w["chat"] == chat]
    if not mine:
        return False
    reply_id = (msg.get("reply_to_message") or {}).get("message_id")
    key = next((k for k in mine if waiting[k]["msg"] == reply_id), None)
    if key is None and len(mine) == 1:
        key = mine[0]
    if key is None:
        notify(chat, "有好幾個壓縮檔在等密碼，請長按要解的那則「需要密碼」→ 回覆，再傳密碼。")
        return True
    w = waiting.pop(key, None)
    if w is None:
        return False
    w["timer"].cancel()
    job = w["job"]
    job.password = text
    edit(chat, w["msg"], f"🔑 收到 {job.name} 的密碼，解壓中…")
    enqueue(job, chat, w["thread"], w["msg"])
    return True


def collect_archive(chat, msg, doc):
    """收到壓縮檔先等幾秒，同一組的其他分卷也到了再一起處理。"""
    name = safe_name(doc.get("file_name") or f"{doc['file_unique_id']}.zip")
    origin = msg.get("forward_origin") or {}
    part = {"file_id": doc["file_id"], "name": name, "orig_name": doc.get("file_name") or "",
            "size": doc.get("file_size") or 0,
            "origin": (origin["chat"]["id"], origin["message_id"]) if origin.get("type") == "channel" else None}
    key = (chat, archive_key(name))
    with _parts_lock:
        group = _parts.setdefault(key, {"parts": {}, "text": ""})
        group["parts"][name] = part  # 同一個檔案轉傳兩次只算一次
        group["text"] += " " + (msg.get("caption") or "")
        tid = None if msg.get("forward_origin") else find_topic(msg.get("caption") or "")
        if tid is not None:  # 自己傳檔案時在說明打子頻道名稱（轉傳的說明是別人寫的，不看）
            _topic_hint[chat] = (tid, time.time())
        if group.get("timer"):
            group["timer"].cancel()
        group["timer"] = threading.Timer(PARTS_WAIT, archive_ready, (key,))
        group["timer"].start()


def archive_ready(key):
    with _parts_lock:
        group = _parts.pop(key, None)
    if not group:
        return
    chat, name = key
    parts = sorted(group["parts"].values(), key=lambda p: natural(p["name"]))
    job = ArchiveJob(parts, name, group["text"].strip())
    hint, when = _topic_hint.pop(chat, (None, 0))
    tid = hint if time.time() - when < 120 else None  # 轉傳時另外打的子頻道名稱（兩分鐘內有效）
    if tid is None and TOPICS and not DEFAULT_TOPIC:
        return ask_topic(chat, job, job.label)
    tid = default_topic() if tid is None else tid
    note = wait_note()
    enqueue(job, chat, tid)
    where = topic_name(tid)
    notify(chat, f"⏳ 收到 {job.label}" + (f" → {where}" if where else "") + (f"，{note}" if note else ""))


def job_label(what):
    if isinstance(what, ArchiveJob):
        return what.label
    return file_label(what) if isinstance(what, list) else what


SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
SKIP_SUFFIX = {".tmp", ".part", ".crdownload", ".download", ".ytdl"}


def skip_file(p: Path):
    rel = p.relative_to(UPLOAD_DIR)
    name = p.name.lower()
    return (rel.parts[0] == FAILED_DIR or name in SKIP_NAMES or name.startswith(("~$", "."))
            or p.suffix.lower() in SKIP_SUFFIX
            or name.endswith(".thumb.jpg"))  # 舊版上傳影片時留下的縮圖


def in_use(p: Path):
    """Windows 複製大檔時一開始就把檔案大小設好，所以只看大小不夠，還要確認沒有程式正在寫它。"""
    try:
        with open(p, "ab" if os.access(p, os.W_OK) else "rb"):  # 唯讀檔只能用讀的開
            return False
    except OSError:
        return True


def ask_topic(chat, what, label):
    """私訊跳出子頻道按鈕；沒設子頻道或有 DEFAULT_TOPIC 就直接排進佇列。"""
    if not TOPICS or DEFAULT_TOPIC:
        enqueue(what, chat, default_topic())
        return
    key = f"{int(time.time() * 1000) % 10**9}-{len(pending)}"
    pending[key] = what
    tg("sendMessage", {"chat_id": chat, "text": f"要貼到哪個子頻道？\n{label}",
                       "disable_web_page_preview": True, "reply_markup": topic_keyboard(key)})


def total_size(paths):
    total = 0
    for p in paths:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def file_label(paths):
    label = "📁 " + "、".join(f"{p.name}（{human(total_size([p]))}）" for p in paths[:5])
    if len(paths) > 5:
        label += f" 等 {len(paths)} 個檔案"
    return label + (f"\n共 {human(total_size(paths))}" if len(paths) > 1 else "")


def watch_folder():
    """每 10 秒看一次上傳資料夾；檔案複製完（大小和修改時間都沒變、也沒有程式在寫）就私訊問你要貼到哪。
    已經問過或正在上傳的檔案不會再問。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / FAILED_DIR).mkdir(exist_ok=True)
    seen, queued = {}, set()
    log.info("上傳資料夾：%s", UPLOAD_DIR)
    while True:
        try:
            ready = []
            for p in UPLOAD_DIR.rglob("*"):
                if p in queued or not p.is_file() or skip_file(p):
                    continue
                st = p.stat()
                stamp = (st.st_size, st.st_mtime_ns)
                if st.st_size and seen.get(p) == stamp and not in_use(p):
                    ready.append(p)
                seen[p] = stamp
            ready.sort()
            notify_chat = next(iter(ALLOWED_USER_IDS), None)  # 可能在聊天裡才剛設好
            for i in range(0, len(ready), 10):  # 一次最多 10 個一組
                chunk = ready[i:i + 10]
                if notify_chat is None:
                    enqueue(chunk, None, default_topic())
                else:
                    ask_topic(notify_chat, chunk, file_label(chunk))
                queued.update(chunk)
            queued = {p for p in queued if p.exists()}
            seen = {p: s for p, s in seen.items() if p.exists()}
        except Exception:
            log.exception("掃描上傳資料夾失敗")
        time.sleep(10)


def download_worker():
    """下載（和轉檔）；好幾個一起跑。做完把上傳的部分排給 upload_worker。"""
    while True:
        what, chat, thread_id, msg_id = jobs.get()
        label = job_label(what)
        where = topic_name(thread_id)
        progress = Progress(chat, f"⏳ {label}" + (f" → {where}" if where else ""), msg_id)
        active.append(progress)
        with _count_lock:
            _count["downloading"] += 1
        _local.progress = progress
        send = None
        try:
            if isinstance(what, ArchiveJob):
                send = handle_archive(what, chat, thread_id)
            elif isinstance(what, list):
                send = handle_files(what, chat, thread_id)
            else:
                send = handle(what, chat, thread_id)
        except Exception:
            log.exception("處理失敗 %s", label)
        finally:
            _local.progress = None
            with _count_lock:
                _count["downloading"] -= 1
        if send is None:
            finish(progress)
        else:
            progress.text = "⬆️ 下載好了，等前面的上傳完…"
            uploads.put((progress, send))


def upload_worker():
    """上傳一次一個。"""
    while True:
        progress, send = uploads.get()
        _local.progress = progress
        try:
            send()
        except Exception:
            log.exception("上傳失敗 %s", progress.head)
        finally:
            _local.progress = None
            finish(progress)


def finish(progress):
    progress.close()
    if progress in active:
        active.remove(progress)
    with _count_lock:
        _count["outstanding"] -= 1
    if restart_when_idle is not None and idle():
        restart_now(restart_when_idle)


# ---------------- 遠端重新啟動 ----------------

def restart_now(chat):
    if FOREGROUND:
        return notify(chat, "現在是用 start.bat 在前景執行，不能從手機重啟。")
    try:
        RESTART_FLAG.write_text(str(chat), encoding="utf-8")
    except OSError:
        pass
    if poll_offset:  # 先跟 Telegram 確認這則 /restart 處理過了，不然重開後會再收到一次
        try:
            tg("getUpdates", {"offset": poll_offset, "timeout": 0, "limit": 1}, timeout=15)
        except Exception:
            pass
    notify(chat, "🔄 重新啟動中，好了會再通知你。")
    log.info("從 Telegram 重新啟動")
    os._exit(RESTART_CODE)


VERSIONS_FILE = Path(__file__).with_name(".versions.json")  # 上次啟動時各下載工具的版本


def tool_updates():
    """跟上次啟動比，哪些下載工具更新了（start-hidden.vbs 每次啟動前會先更新）。"""
    from importlib import metadata
    now = {}
    for name in ("yt-dlp", "gallery-dl", "telethon"):
        try:
            now[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    try:
        before = json.loads(VERSIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    try:
        VERSIONS_FILE.write_text(json.dumps(now), encoding="utf-8")
    except OSError:
        pass
    return [f"{n} {before[n]} → {v}" for n, v in now.items() if before.get(n) not in (None, v)]


def ask_restart(chat):
    if idle():
        return restart_now(chat)
    doing = "\n".join(p.head for p in list(active))
    tg("sendMessage", {
        "chat_id": chat, "disable_web_page_preview": True,
        "text": f"還有工作沒做完：\n{doing}\n" + (f"排隊中還有 {jobs.qsize()} 個。\n" if jobs.qsize() else "")
                + "要怎麼重啟？",
        "reply_markup": json.dumps({"inline_keyboard": [
            [{"text": "做完再重啟", "callback_data": "r|later"}, {"text": "現在重啟", "callback_data": "r|now"}],
            [{"text": "取消", "callback_data": "r|cancel"}]]})})


def on_restart_button(choice, chat, msg_id):
    global restart_when_idle
    if choice == "now":
        edit(chat, msg_id, "🔄 現在重啟。做到一半的：上傳資料夾的檔案重啟後會再問你一次，連結要重傳。")
        restart_now(chat)
    elif choice == "later":
        restart_when_idle = chat
        edit(chat, msg_id, "⏳ 好，目前的工作都做完就重啟。")
        if idle():
            restart_now(chat)
    else:
        restart_when_idle = None
        edit(chat, msg_id, "已取消重啟。")


# ---------------- 入口：HTTP（給 iPhone 捷徑） ----------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {"ok": True, "queued": jobs.qsize()})

    def do_POST(self):
        if self.path.rstrip("/") != "/download":
            return self._send(404, {"ok": False})
        if not SHARED_SECRET or self.headers.get("X-Secret") != SHARED_SECRET:
            return self._send(401, {"ok": False, "error": "密碼錯誤"})
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            try:
                body = json.loads(raw)
                text, topic = body.get("url", ""), str(body.get("topic", ""))
            except json.JSONDecodeError:
                text, topic = raw, ""
            urls = URL_RE.findall(text)
        except Exception:
            urls, topic = [], ""
        if not urls:
            return self._send(400, {"ok": False, "error": "找不到網址"})
        notify_chat = next(iter(ALLOWED_USER_IDS), None)
        tid = find_topic(f"{topic} {text}")
        tid = default_topic() if tid is None else tid
        for u in urls:
            enqueue(u, notify_chat, tid)
        self._send(200, {"ok": True, "queued": urls})

    def log_message(self, fmt, *args):
        log.info("http %s", fmt % args)


# ---------------- 在聊天裡設定 ----------------

_env_lock = threading.Lock()
setup_state = {}  # 私訊裡正在設定的人：chat id → {"step": ..., "login": Login}


def apply_env():
    """照 os.environ 重新算一次會在聊天裡改的設定（不用重開）。"""
    global CHANNEL_ID, TOPICS, UPLOAD_AS_USER, ONLY_BIG_AS_USER, MAX_UPLOAD_MB, MAX_BYTES
    CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
    TOPICS = _parse_topics(os.environ.get("TOPICS", "")) if CHANNEL_ID else {}
    ALLOWED_USER_IDS.clear()
    ALLOWED_USER_IDS.update(int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip())
    UPLOAD_AS_USER = os.environ.get("UPLOAD_AS_USER", "0") in ("1", "big")
    ONLY_BIG_AS_USER = os.environ.get("UPLOAD_AS_USER", "0") == "big"
    MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2000" if UPLOAD_AS_USER else "50"))
    MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024


def save_env(**kv):
    """把設定寫進 .env（有就改、沒有就加在最後），馬上生效。"""
    with _env_lock:
        lines = _env.read_text(encoding="utf-8").splitlines() if _env.exists() else []
        for k, v in kv.items():
            os.environ[k] = v
            hit = [i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() == k]
            if hit:
                lines[hit[0]] = f"{k}={v}"
                lines = [line for i, line in enumerate(lines) if i not in hit[1:]]
            else:
                lines.append(f"{k}={v}")
        _env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        apply_env()


def setup_text():
    where = "這個聊天（私訊）" if not CHANNEL_ID else f"群組 {CHANNEL_ID}"
    subs = "、".join(TOPICS) if TOPICS else "（沒有）"
    return (f"⚙️ 目前設定\n下載結果傳到：{where}\n子頻道：{subs}\n上傳身分：{UPLOAD_MODES[upload_mode()]}"
            f"（上限 {MAX_UPLOAD_MB}MB）\n\n要改哪一項？")


UPLOAD_MODES = {"0": "全部機器人傳", "big": "50MB 以下機器人傳，大檔用你的帳號", "1": "全部用你的帳號"}


def upload_mode():
    mode = os.environ.get("UPLOAD_AS_USER", "0")
    return mode if mode in UPLOAD_MODES else "0"


def logged_in():
    return (bool(os.environ.get("TELEGRAM_API_ID")) and Path(__file__).with_name("user.session").exists())


def mode_keyboard():
    return json.dumps({"inline_keyboard": [
        [{"text": ("✅ " if upload_mode() == k else "") + v, "callback_data": f"m|{k}"}]
        for k, v in UPLOAD_MODES.items()]})


def on_mode_button(mode, chat, msg_id):
    if mode not in UPLOAD_MODES:
        return
    if mode != "0" and not logged_in():
        return edit(chat, msg_id, "要先登入你的帳號才能用你的身分傳。打 /setup 按「大檔案（登入帳號）」。")
    save_env(UPLOAD_AS_USER=mode)
    edit(chat, msg_id, f"✅ 上傳身分：{UPLOAD_MODES[mode]}（上限 {MAX_UPLOAD_MB}MB）")


def setup_keyboard():
    return json.dumps({"inline_keyboard": [
        [{"text": "📥 傳回這裡", "callback_data": "s|here"}, {"text": "👥 貼到群組", "callback_data": "s|group"}],
        [{"text": "📦 大檔案（登入帳號）", "callback_data": "s|login"},
         {"text": "📤 上傳身分", "callback_data": "s|mode"}]]})


def show_setup(chat):
    tg("sendMessage", {"chat_id": chat, "text": setup_text(), "reply_markup": setup_keyboard()})


def on_setup_button(choice, chat, msg_id):
    if choice == "here":
        save_env(CHANNEL_ID="", TOPICS="")
        return edit(chat, msg_id, "✅ 之後下載結果都會傳回這個聊天。")
    if choice == "group":
        return edit(chat, msg_id, "👥 貼到群組：\n1. 把我加進群組，設成管理員。\n"
                                  "2. 在群組裡打 /setup，之後就會貼到那裡。\n"
                                  "3. 群組有開「話題」的話，到每個想用的子頻道裡各打一次 /setup，"
                                  "子頻道就會加進按鈕。名稱想自己取就打 /setup 名稱。\n"
                                  "想改回私訊，打 /setup 按「傳回這裡」。")
    if choice == "mode":
        return tg("editMessageText", {"chat_id": chat, "message_id": msg_id, "reply_markup": mode_keyboard(),
                                      "text": "下載結果要用誰的身分傳？用你的帳號傳時，貼文會顯示是你發的。"})
    if choice == "login":
        if os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH"):
            setup_state[chat] = {"step": "phone"}
            return edit(chat, msg_id, LOGIN_PHONE)
        setup_state[chat] = {"step": "api_id"}
        return edit(chat, msg_id, "📦 用你的帳號傳大檔案（最大 2GB）。\n"
                                  "1. 到 https://my.telegram.org 登入 → API development tools，隨便填個名稱建立。\n"
                                  "2. 把 api_id（一串數字）傳給我。\n隨時打 /cancel 取消。")


LOGIN_PHONE = "把你的手機號碼傳給我，要加國碼，例如 +886912345678。"


def setup_input(chat, text, msg):
    """設定進行中：收你傳來的 api_id、api_hash、手機號碼、驗證碼、兩步驟驗證密碼。"""
    st = setup_state[chat]
    step = st["step"]
    if step == "api_id":
        if not re.fullmatch(r"\d+", text):
            return notify(chat, "api_id 應該是一串數字，再傳一次。")
        st["api_id"], st["step"] = text, "api_hash"
        return notify(chat, "收到。再把 api_hash（32 個英數字）傳給我。")
    if step == "api_hash":
        if not re.fullmatch(r"[0-9a-f]{32}", text):
            return notify(chat, "api_hash 應該是 32 個 0-9、a-f 的字，再傳一次。")
        save_env(TELEGRAM_API_ID=st["api_id"], TELEGRAM_API_HASH=text)
        delete_message(chat, msg)
        st["step"] = "phone"
        return notify(chat, "收到。" + LOGIN_PHONE)
    if step == "phone":
        phone = re.sub(r"[^\d+]", "", text)
        if not phone.startswith("+"):
            return notify(chat, "號碼前面要加國碼，例如 +886912345678。")
        close_uploader()  # 重新登入時，舊的連線會鎖住 user.session
        from user_upload import Login
        try:
            st["login"] = Login(os.environ["TELEGRAM_API_ID"], os.environ["TELEGRAM_API_HASH"])
            st["login"].phone(phone)
        except Exception as e:
            setup_state.pop(chat, None)
            return notify(chat, f"❌ 寄驗證碼失敗：{e}\n打 /setup 重來。")
        st["step"] = "code"
        delete_message(chat, msg)
        return notify(chat, "Telegram 寄了驗證碼給你（在「Telegram」官方對話裡）。\n"
                            "⚠️ 驗證碼原樣轉傳會被 Telegram 作廢，請在數字中間加空格再傳，例如 1 2 3 4 5。")
    if step == "code":
        code = re.sub(r"\D", "", text)
        delete_message(chat, msg)
        try:
            me = st["login"].code(code)
        except Exception as e:
            return notify(chat, f"❌ 驗證碼不對或過期了：{e}\n再傳一次，或打 /cancel 取消。")
        if me is None:
            st["step"] = "password"
            return notify(chat, "你的帳號有開兩步驟驗證，請傳密碼給我（收到後會馬上刪掉那則訊息）。")
        return login_done(chat, me)
    if step == "password":
        delete_message(chat, msg)
        try:
            me = st["login"].password(text)
        except Exception as e:
            return notify(chat, f"❌ 密碼不對：{e}\n再傳一次，或打 /cancel 取消。")
        return login_done(chat, me)


def login_done(chat, me):
    setup_state.pop(chat, None)
    if upload_mode() == "0":
        save_env(UPLOAD_AS_USER="big")
    notify(chat, f"✅ 已登入 {me.first_name}。上傳身分：{UPLOAD_MODES[upload_mode()]}（上限 {MAX_UPLOAD_MB}MB）。\n"
                 "想改成全部用你的帳號，打 /setup 按「上傳身分」。")


def cancel_setup(chat):
    st = setup_state.pop(chat, None)
    if st and st.get("login"):
        st["login"].close()
    notify(chat, "已取消設定。" if st else "現在沒有在設定。")


def delete_message(chat, msg):
    """刪掉含有驗證碼、密碼的訊息。"""
    try:
        tg("deleteMessage", {"chat_id": chat, "message_id": msg["message_id"]})
    except Exception:
        pass


def setup_group(msg, arg):
    """在群組（或它的子頻道）打 /setup：之後貼到這個群組，子頻道加進按鈕。"""
    chat = msg["chat"]
    gid = str(chat["id"])
    tid = msg.get("message_thread_id") if msg.get("is_topic_message") else 0
    topics = dict(TOPICS) if CHANNEL_ID == gid else {}
    reply = {"chat_id": chat["id"]}
    if tid:
        reply["message_thread_id"] = tid
    text = "✅ 之後下載結果會貼到這個群組。"
    if chat.get("is_forum"):
        created = (msg.get("reply_to_message") or {}).get("forum_topic_created") or {}
        name = re.sub(r"[,:]", "", arg or created.get("name") or ("General" if not tid else "")).strip()
        if not name:
            reply["text"] = "找不到這個子頻道的名稱，請打 /setup 名稱（例如 /setup 影片）。"
            return tg("sendMessage", reply)
        topics = {n: i for n, i in topics.items() if i != tid}
        topics[name] = tid
        text += f"\n子頻道「{name}」加好了。目前：{'、'.join(topics)}"
    save_env(CHANNEL_ID=gid, TOPICS=",".join(f"{n}:{i}" for n, i in topics.items()))
    reply["text"] = text
    tg("sendMessage", reply)


# ---------------- 入口：Telegram 私訊機器人 ----------------

def topic_keyboard(key):
    names = list(TOPICS)
    rows = [names[i:i + 3] for i in range(0, len(names), 3)]
    return json.dumps({"inline_keyboard": [
        [{"text": n, "callback_data": f"t|{key}|{n}"} for n in row] for row in rows]})


def on_callback(cb):
    """按了子頻道按鈕。"""
    if cb["from"]["id"] not in ALLOWED_USER_IDS:
        return
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat = msg.get("chat", {}).get("id")
    if not data.startswith("t|"):  # 子頻道按鈕在下面回，順便跳出選了哪裡
        answer(cb)
    if data.startswith("r|"):
        return on_restart_button(data[2:], chat, msg.get("message_id"))
    if data.startswith("s|"):
        return on_setup_button(data[2:], chat, msg.get("message_id"))
    if data.startswith("m|"):
        return on_mode_button(data[2:], chat, msg.get("message_id"))
    if data.startswith("p|"):  # 不解這個要密碼的壓縮檔了
        if data[2:] not in waiting:
            return edit(chat, msg.get("message_id"), "這個壓縮檔已經不在等密碼了。")
        return give_up_password(data[2:], "🗑 不解了")
    _, key, name = data.split("|", 2)
    url = pending.pop(key, None)
    if url is None or name not in TOPICS:
        answer(cb)
        return notify(chat, "這個按鈕過期了，請重傳連結或檔案。")
    answer(cb, f"→ {name}")
    # 馬上把按鈕換成「已選好」，輪到它時這則訊息會變成進度顯示
    edit(chat, msg["message_id"], f"⏳ {job_label(url)} → {name}\n{wait_note() or '準備中…'}")
    enqueue(url, chat, TOPICS[name], msg["message_id"])


def answer(cb, text=None):
    """讓按鈕停止轉圈圈；有 text 的話手機上方會短暫跳出這句。"""
    try:
        tg("answerCallbackQuery", {"callback_query_id": cb["id"], **({"text": text} if text else {})})
    except Exception:
        pass


def on_message(msg):
    uid = (msg.get("from") or {}).get("id")
    chat = msg.get("chat", {})
    text = (msg.get("text") or "") + " " + (msg.get("caption") or "")
    private = chat.get("type") == "private"
    if not ALLOWED_USER_IDS and private and text.startswith("/start"):
        # 還沒有主人：第一個私訊 /start 的人就是主人
        save_env(ALLOWED_USER_IDS=str(uid))
        log.info("主人設成 %s", uid)
        notify(chat["id"], "👋 你現在是這個機器人的主人了，只有你能用。\n"
                           "傳連結給我就會下載後傳回這裡。想改成貼到群組，或開大檔案，按下面的按鈕；"
                           "之後打 /setup 也能再改。")
        return show_setup(chat["id"])
    if uid not in ALLOWED_USER_IDS:
        if text.startswith("/start") and private:
            notify(chat["id"], f"你的 user id 是 {uid}。請機器人的主人把它加到 .env 的 ALLOWED_USER_IDS。")
        return
    words = text.split()
    cmd = words[0].split("@")[0].lower() if words else ""
    if cmd == "/setup" and not private:
        return setup_group(msg, " ".join(words[1:]))

    # 在群組的某個子頻道裡打 /topicid，回報它的 id 方便填 TOPICS
    if text.strip().startswith("/topicid"):
        tid = msg.get("message_thread_id") if msg.get("is_topic_message") else 0
        created = (msg.get("reply_to_message") or {}).get("forum_topic_created") or {}
        name = created.get("name") or ("General" if not tid else "這個子頻道")
        reply = {"chat_id": chat["id"], "text": f"{name} 的 id：{tid}\n.env 寫法：{name}:{tid}"}
        if tid:
            reply["message_thread_id"] = tid
        try:
            tg("sendMessage", reply)
        except Exception as e:
            log.warning("回報 topic id 失敗：%s", e)
        return
    if not private:
        return
    if cmd == "/setup":
        setup_state.pop(chat["id"], None)
        return show_setup(chat["id"])
    if cmd == "/cancel":
        return cancel_setup(chat["id"])
    if chat["id"] in setup_state and not cmd.startswith("/"):
        return setup_input(chat["id"], text.strip(), msg)
    if cmd == "/status":
        return live_status(chat["id"])
    if cmd == "/restart":
        return ask_restart(chat["id"])
    doc = msg.get("document")
    if doc and is_archive(doc.get("file_name")):  # 先看檔案：轉傳的說明裡常有別的連結
        return collect_archive(chat["id"], msg, doc)

    urls = URL_RE.findall(text)
    for e in msg.get("entities", []) + msg.get("caption_entities", []):
        if e.get("type") == "text_link":
            urls.append(e["url"])
    urls = list(dict.fromkeys(urls))
    plain = (msg.get("text") or "").strip()
    if not urls and plain and not plain.startswith("/") and take_password(chat["id"], plain, msg):
        return
    if not urls and plain and find_topic(plain) is not None and len(plain.split()) == 1:
        # 轉傳壓縮檔時另外打的子頻道名稱（會比檔案早一點到）
        _topic_hint[chat["id"]] = (find_topic(plain), time.time())
        return
    if not urls:
        hint = ("傳一個連結，或轉傳 zip / 7z / rar 壓縮檔給我，我會下載後"
                + ("貼到頻道。" if CHANNEL_ID else "傳回這裡。"))
        if TOPICS:
            hint += "\n連結前後加上子頻道名稱（" + "、".join(TOPICS) + "）就會直接貼到那裡。"
        hint += "\n/status 看進度和電腦狀態，/setup 改設定，/restart 重新啟動。"
        return notify(chat["id"], hint)

    tid = find_topic(text)
    if tid is None and TOPICS and not DEFAULT_TOPIC:
        for url in urls:
            ask_topic(chat["id"], url, url)
        return
    tid = default_topic() if tid is None else tid
    note = wait_note()
    for url in urls:
        enqueue(url, chat["id"], tid)
    where = topic_name(tid)
    notify(chat["id"], f"⏳ 收到 {len(urls)} 個連結" + (f" → {where}" if where else "")
           + (f"，{note}" if note else ""))


def poll():
    global poll_offset
    while True:
        try:
            ups = tg("getUpdates", {"timeout": 50, "offset": poll_offset,
                                    "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=60)
        except Exception as e:
            log.warning("getUpdates：%s", e)
            time.sleep(5)
            continue
        for u in ups:
            poll_offset = u["update_id"] + 1
            # 每則訊息各自處理，一則回得慢（例如要送好幾個按鈕）不會卡住下一則
            threading.Thread(target=on_update, args=(u,), daemon=True).start()


def on_update(u):
    try:
        if "callback_query" in u:
            on_callback(u["callback_query"])
        elif "message" in u:
            on_message(u["message"])
    except Exception:
        log.exception("處理訊息失敗")


def main():
    # 同一時間只跑一個，不然兩個程式會搶著收訊息
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 47831))
    except OSError:
        log.error("機器人已經在背景執行中（右下角圖示），這個不用再開。要在這裡看即時紀錄，請先執行 stop.bat")
        sys.exit(3)
    if sys.stdout.isatty():  # start.bat 開的視窗
        print("前景執行中，可以直接看紀錄。關掉這個視窗機器人就會停止。")
    if TEMP_DIR:
        Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    while True:  # 剛開機時網路可能還沒好，等到連得上為止
        try:
            me = tg("getMe")
            break
        except ConnectionError as e:
            log.warning("連不到 Telegram，15 秒後重試：%s", e)
            time.sleep(15)
        except RuntimeError as e:
            log.error("%s（token 不對，請重新執行 configure.bat）", e)
            sys.exit(2)
    global BOT_USERNAME
    BOT_USERNAME = me["username"]
    log.info("機器人 @%s 啟動，頻道 %s，子頻道 %s", me["username"], CHANNEL_ID or "（未設定，傳回私訊聊天）",
             TOPICS or "（未設定）")
    try:  # 私訊機器人時，輸入框旁的選單會列出這些指令
        tg("setMyCommands", {"commands": json.dumps([
            {"command": "status", "description": "看進度和電腦狀態（CPU、RAM、GPU）"},
            {"command": "setup", "description": "設定：傳到哪裡、群組子頻道、大檔案"},
            {"command": "restart", "description": "重新啟動機器人"}])})
    except Exception as e:
        log.warning("設定指令選單失敗：%s", e)
    try:  # 先量一次 CPU，之後 /status 才有正確的使用率
        import psutil
        psutil.cpu_percent()
    except ImportError:
        log.warning("沒有 psutil，/status 看不到 CPU / RAM（執行 configure.bat 會自動安裝）")
    updates = tool_updates()
    if updates:
        log.info("下載工具已更新：%s", "、".join(updates))
    news = "\n\n⬆️ 下載工具更新了：\n" + "\n".join(updates) if updates else ""
    restarted = None
    if RESTART_FLAG.exists():  # 剛從 Telegram 要求重新啟動
        try:
            restarted = int(RESTART_FLAG.read_text(encoding="utf-8").strip())
            RESTART_FLAG.unlink()
            notify(restarted, "✅ 機器人已重新啟動。" + news)
        except (OSError, ValueError):
            pass
    if updates and restarted is None and ALLOWED_USER_IDS:  # 開機時有更新也說一聲
        notify(next(iter(ALLOWED_USER_IDS)), "✅ 機器人已啟動。" + news)
    if UPLOAD_AS_USER:
        log.info("用你的帳號上傳，上限 %dMB", MAX_UPLOAD_MB)
    COOKIE_DIR.mkdir(exist_ok=True)
    if not TOTALS_FILE.exists():
        count()  # 從今天開始算累計
    cookie_file()  # 記錄現在用了哪些 cookies 檔
    for _ in range(max(DOWNLOAD_WORKERS, 1)):
        threading.Thread(target=download_worker, daemon=True).start()
    threading.Thread(target=upload_worker, daemon=True).start()
    threading.Thread(target=watch_folder, daemon=True).start()
    if ENABLE_POLLING:
        threading.Thread(target=poll, daemon=True).start()
    if SHARED_SECRET:
        log.info("HTTP 監聽 :%d", PORT)
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    else:
        log.info("未設定 SHARED_SECRET，不開 HTTP，只用 Telegram 私訊")
        threading.Event().wait()


if __name__ == "__main__":
    main()
