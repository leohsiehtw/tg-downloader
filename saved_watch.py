"""
自動抓 IG 收藏、X 書籤：你傳連結給機器人時，順便用 gallery-dl 看最新的幾則，沒處理過的就排進下載。

第一次開某個來源時，只把現有的全部記成「看過了」，不會把以前的收藏一次全下載；
之後新收藏的才會下載。看過的 id 存在 .saved-seen.json。
"""
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("tg-downloader")

# 代號: (顯示名稱, gallery-dl 網址)。IG 收藏的網址帳號隨便填，抓的都是 cookies 那個帳號自己的收藏
SOURCES = {
    "ig": ("IG 收藏", "https://www.instagram.com/me/saved/"),
    "x": ("X 書籤", "https://x.com/i/bookmarks"),
}
SEEN_FILE = Path(__file__).with_name(".saved-seen.json")
PEEK = 30  # 每次看最新幾則；兩次傳連結之間收藏超過這麼多則，多的會漏掉
KEEP = 1000  # 每個來源最多記幾個看過的 id

_lock = threading.Lock()


def load_seen():
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_seen(seen):
    try:
        SEEN_FILE.write_text(json.dumps(seen), encoding="utf-8")
    except OSError as e:
        log.warning("存 %s 失敗：%s", SEEN_FILE.name, e)


def latest(src, cookies, peek=PEEK):
    """回傳最新 peek 則的 [(id, 貼文網址)]（None＝全部），最新的在前面。抓不到就丟 RuntimeError（訊息是 gallery-dl 的錯誤）。"""
    url = SOURCES[src][1]
    tmp = Path(tempfile.mkdtemp(prefix="tgdl-saved-"))
    try:
        cmd = [sys.executable, "-m", "gallery_dl", "-j", url]
        if peek:
            cmd[3:3] = ["--post-range", f"1-{peek}"]
        if cookies:  # gallery-dl 結束時會改寫 cookies 檔，給它一份複本
            mine = tmp / "cookies.txt"
            shutil.copyfile(cookies, mine)
            cmd[3:3] = ["--cookies", str(mine)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=300 if peek else 3600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        messages = json.loads(r.stdout)
    except ValueError:
        messages = None
    if r.returncode or not isinstance(messages, list):
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or f"gallery-dl 結束代碼 {r.returncode}")
    out = []
    for m in messages:
        meta = m[-1] if m and isinstance(m[-1], dict) else {}
        if src == "ig" and meta.get("post_shortcode"):
            item = (meta["post_shortcode"], f"https://www.instagram.com/p/{meta['post_shortcode']}/")
        elif src == "x" and meta.get("tweet_id"):
            user = (meta.get("author") or {}).get("name") or "i"
            item = (str(meta["tweet_id"]), f"https://x.com/{user}/status/{meta['tweet_id']}")
        else:
            continue
        if item not in out:  # 多圖貼文每張圖都會出現一次
            out.append(item)
    return out


def check(src, cookies, enqueue):
    """看一次；新的排進下載（舊的先）。回傳排了幾個。"""
    items = latest(src, cookies)
    with _lock:
        seen = load_seen()
        first = src not in seen
        known = set(seen.get(src, []))
        new = [] if first else [(i, u) for i, u in items if i not in known]
        seen[src] = ([i for i, _ in items if i not in known] + seen.get(src, []))[:KEEP]
        save_seen(seen)
    if first:
        log.info("%s：第一次檢查，現有 %d 則記成看過，之後新收藏的才下載", SOURCES[src][0], len(items))
    for _, url in reversed(new):
        enqueue(url, src)
    return len(new)


def backfill(src, cookies, enqueue):
    """開啟時選了「以前的也下載」：抓全部收藏，全部排進下載（舊的先），之後只抓新的。回傳排了幾個。"""
    items = latest(src, cookies, None)
    with _lock:
        seen = load_seen()
        seen[src] = [i for i, _ in items][:KEEP]
        save_seen(seen)
    for _, url in reversed(items):
        enqueue(url, src)
    return len(items)


def forget(src):
    """關掉某個來源時忘掉看過的清單，下次開啟會重新從「現在」開始算，不會補下載關掉期間的收藏。"""
    with _lock:
        seen = load_seen()
        if seen.pop(src, None) is not None:
            save_seen(seen)


_busy = threading.Lock()  # 已經在檢查就不再開一輪
_broken = set()  # 已經通知過抓不到的來源，恢復前不再重複通知


def check_all(enabled, cookies, enqueue, warn):
    """你傳連結時在背景跑一輪。enabled：開著的來源代號；cookies：cookies 檔路徑；
    enqueue(網址, 來源代號) 排進下載；warn(文字) 通知主人。"""
    if not enabled or not _busy.acquire(blocking=False):
        return
    try:
        for src in enabled:
            try:
                n = check(src, cookies, enqueue)
                if n:
                    log.info("%s：%d 則新的", SOURCES[src][0], n)
                if src in _broken:
                    _broken.discard(src)
                    warn(f"✅ {SOURCES[src][0]} 又抓得到了。")
            except Exception as e:
                log.warning("檢查 %s 失敗：%s", SOURCES[src][0], e)
                if src not in _broken:
                    _broken.add(src)
                    warn(f"⚠️ 抓不到{SOURCES[src][0]}，可能是 cookies 過期或被網站擋了。"
                         f"恢復前不會再通知。\n{str(e)[:300]}")
    finally:
        _busy.release()
