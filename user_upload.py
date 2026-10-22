"""
用你自己的 Telegram 帳號上傳（最大 2GB，Premium 4GB），不受機器人 50MB 限制。

第一次要登入：雙擊 login.bat，輸入手機號碼和 Telegram 傳來的驗證碼，
之後會在資料夾裡留一個 user.session，bot.py 就能直接用。
user.session 等於你帳號的登入狀態，不要給任何人。
"""
import asyncio
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo, InputMediaUploadedDocument

HERE = Path(__file__).resolve().parent
SESSION = str(HERE / "user")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # 超過就不能當照片傳，改用檔案


def _client():
    return TelegramClient(SESSION, int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"])


def _probe(path: Path):
    """用 ffprobe 取影片長寬秒數，讓 Telegram 顯示成可直接播放的影片。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60).stdout
        info = json.loads(out)
        st = info["streams"][0]
        return int(float(info["format"]["duration"])), int(st["width"]), int(st["height"])
    except Exception:
        return 0, 0, 0


def _thumb(path: Path, folder):
    """縮圖放暫存資料夾，不要留在影片旁邊（上傳資料夾會把它當成新檔案）。"""
    t = Path(folder) / (path.stem + ".jpg")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=320:-2", str(t)], capture_output=True, timeout=60)
    except Exception:
        pass
    return str(t) if t.exists() else None


class Uploader:
    """在自己的執行緒裡一直跑一個 event loop，bot.py 好幾個執行緒可以同時呼叫（上傳和下載同時進行）。"""

    def __init__(self):
        self.entities = {}  # 貼過的對象（群組 id、機器人 username）
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self._run(self._start())

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    async def _start(self):
        self.client = _client()
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("還沒登入 Telegram 帳號，請先執行 login.bat")
        self.me_id = (await self.client.get_me()).id
        await self.client.get_dialogs()  # 讓 telethon 認得群組 / 頻道 id

    async def _entity(self, to):
        if to not in self.entities:
            key = int(to) if str(to).lstrip("-").isdigit() else to
            try:
                self.entities[to] = await self.client.get_entity(key)
            except ValueError:  # 剛加入的群組，先重新整理一次對話清單
                await self.client.get_dialogs()
                self.entities[to] = await self.client.get_entity(key)
        return self.entities[to]

    def close(self):
        try:
            self._run(self.client.disconnect())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def _tick(self, p: Path, sent=0):
        """回報整批的上傳進度（給 bot.py 顯示在 Telegram 私訊裡）。"""
        try:
            self.progress(p.name, self.order.index(p) + 1, len(self.order), self.done + sent, self.total)
        except Exception:
            pass

    async def _media(self, p: Path, tmp):
        """影片先自己上傳，附上長寬秒數和縮圖，才能跟照片一起放進同一則相簿。"""
        if p.suffix.lower() not in VIDEO_EXT:
            return str(p)
        dur, w, h = _probe(p)
        t = _thumb(p, tmp)
        file = await self.client.upload_file(str(p), progress_callback=lambda s, _: self._tick(p, s))
        self.done += p.stat().st_size
        return InputMediaUploadedDocument(
            file=file,
            mime_type=mimetypes.guess_type(p.name)[0] or "video/mp4",
            attributes=[DocumentAttributeVideo(dur, w, h, supports_streaming=True),
                        DocumentAttributeFilename(p.name)],
            thumb=await self.client.upload_file(t) if t else None)

    async def _send(self, paths, caption, thread_id, to):
        entity = await self._entity(to)
        reply_to = thread_id or None
        album = [p for p in paths if p.suffix.lower() in VIDEO_EXT
                 or (p.suffix.lower() in IMAGE_EXT and p.stat().st_size <= PHOTO_MAX_BYTES)]
        others = [p for p in paths if p not in album]
        self.order, self.done, self.total = album + others, 0, sum(p.stat().st_size for p in paths)
        first = True
        sent = 0
        with tempfile.TemporaryDirectory(prefix="tgthumb-") as tmp:
            # 照片和影片每 10 個合成一則相簿（全部傳完才一起貼出）；只有一個就單獨貼
            for i in range(0, len(album), 10):
                group = album[i:i + 10]
                batch = [await self._media(p, tmp) for p in group]
                self._tick(group[-1])
                r = await self.client.send_file(entity, batch if len(batch) > 1 else batch[0],
                                                caption=(caption or None) if first else None,
                                                reply_to=reply_to, supports_streaming=True)
                self.done += sum(p.stat().st_size for p in group if p.suffix.lower() not in VIDEO_EXT)
                sent += len(r) if isinstance(r, list) else 1
                first = False
        for p in others:  # gif、太大的圖片和其他檔案逐一送
            await self.client.send_file(entity, str(p), caption=(caption or None) if first else None,
                                        reply_to=reply_to, force_document=p.suffix.lower() in IMAGE_EXT,
                                        progress_callback=lambda s, _, p=p: self._tick(p, s))
            self.done += p.stat().st_size
            sent += 1
            first = False
        return sent

    def send(self, paths, caption, thread_id=0, progress=None, to=None):
        """回傳 Telegram 確認收到的則數。progress(檔名, 第幾個, 共幾個, 已傳 bytes, 總 bytes) 用來顯示進度。
        to：貼到哪，群組 id 或機器人的 username（就是貼到你跟機器人的聊天）。"""
        self.progress = progress or (lambda *a: None)
        return self._run(self._send(list(paths), caption, thread_id, to))

    async def _find(self, bot, name, size, origin):
        """機器人收到的檔案，在你跟機器人的對話裡用大小和檔名找回同一則；找不到再去原本的頻道找。"""
        async for m in self.client.iter_messages(bot, limit=300):
            if m.file and m.file.size == size and (not name or m.file.name == name):
                return m
        if origin:
            try:
                m = await self.client.get_messages(origin[0], ids=origin[1])
                if m and m.file and m.file.size == size:
                    return m
            except Exception:
                pass
        return None

    async def _download(self, bot, name, size, origin, dest, progress):
        m = await self._find(bot, name, size, origin)
        if m is None:
            raise RuntimeError(f"用你的帳號找不到 {name}（在跟機器人的對話裡沒找到同樣大小的檔案）")
        await self.client.download_media(m, file=str(dest), progress_callback=progress)

    def download(self, bot, name, size, dest, origin=None, progress=None):
        """下載轉傳給機器人的檔案（機器人自己只能下載 20MB 以下）。progress(已下載 bytes, 總 bytes)。"""
        self._run(self._download(bot, name, size, origin, dest, progress))


class Login:
    """在聊天裡一步步登入你的帳號：phone() 寄驗證碼 → code() → 有兩步驟驗證的話再 password()。"""

    def __init__(self, api_id, api_hash):
        self.api = (int(api_id), api_hash)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    async def _phone(self, phone):
        self.client = TelegramClient(SESSION, *self.api)
        await self.client.connect()
        self.number = phone
        self.hash = (await self.client.send_code_request(phone)).phone_code_hash

    async def _code(self, code):
        from telethon.errors import SessionPasswordNeededError
        try:
            await self.client.sign_in(self.number, code, phone_code_hash=self.hash)
        except SessionPasswordNeededError:
            return None
        return await self._done()

    async def _password(self, pw):
        await self.client.sign_in(password=pw)
        return await self._done()

    async def _done(self):
        me = await self.client.get_me()
        await self.client.disconnect()
        self.loop.call_soon_threadsafe(self.loop.stop)
        return me

    def phone(self, phone):
        self._run(self._phone(phone))

    def code(self, code):
        """登入成功回傳帳號；要兩步驟驗證密碼時回傳 None。"""
        return self._run(self._code(code))

    def password(self, pw):
        return self._run(self._password(pw))

    def close(self):
        try:
            self._run(self.client.disconnect())
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


def login():
    """互動登入一次，產生 user.session。"""
    async def run():
        client = _client()
        await client.start()
        me = await client.get_me()
        print(f"登入成功：{me.first_name}（id {me.id}）。可以關掉這個視窗了。")
        await client.disconnect()
    asyncio.run(run())


if __name__ == "__main__":
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    if not os.environ.get("TELEGRAM_API_ID") or not os.environ.get("TELEGRAM_API_HASH"):
        sys.exit("請先在 .env 填 TELEGRAM_API_ID 和 TELEGRAM_API_HASH（到 https://my.telegram.org 申請）")
    login()
