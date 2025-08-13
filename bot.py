# ──────────────────────────────────────────────────────────────────────────────
# Krishnavi Music Bot – plays songs in Telegram Voice Chats with queue support
# Stack: Python 3.10+, Pyrogram v2, PyTgCalls, yt-dlp, FFmpeg
# Commands: /start, /help, /join, /play <query|YouTube URL>, /queue, /skip,
#           /pause, /resume, /leave
# Notes:
#  • Add the bot to a supergroup and make it admin with "Manage Voice Chats".
#  • Voice chat in the group must be active (any member can start it).
#  • Set API_ID, API_HASH, BOT_TOKEN in environment or .env (see bottom).
# ──────────────────────────────────────────────────────────────────────────────

import asyncio
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.types import Message
from pytgcalls import PyTgCalls
from pytgcalls.types.input_stream import AudioPiped
from pytgcalls.exceptions import GroupCallNotFoundError
import yt_dlp

# ── Config from environment ───────────────────────────────────────────────────
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SESSION_NAME = os.getenv("SESSION_NAME", "krishnavi_session")

if not (API_ID and API_HASH and BOT_TOKEN):
    raise SystemExit("Please set API_ID, API_HASH, BOT_TOKEN env vars")

# ── Pyrogram & PyTgCalls clients ──────────────────────────────────────────────
app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
pytgcalls = PyTgCalls(app)

YTDL_OPTS = {
    "format": "bestaudio[ext=webm][acodec=opus]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "extract_flat": False,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

URL_RE = re.compile(r"^(https?://)\S+", re.I)

@dataclass
class Track:
    title: str
    url: str  # streamable URL (not YouTube watch URL)
    page_url: str  # original page/video URL
    requester: str
    duration: Optional[str] = None

# Per-chat queues and state
queues: Dict[int, Deque[Track]] = defaultdict(deque)
now_playing: Dict[int, Optional[Track]] = defaultdict(lambda: None)
players_lock: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ydl_extract(query: str) -> Tuple[str, str, Optional[str], str]:
    """Return (stream_url, title, duration, page_url) for a query or URL."""
    q = query if URL_RE.match(query) else f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
        info = ydl.extract_info(q, download=False)
        if "entries" in info:
            info = info["entries"][0]
        title = info.get("title") or "Unknown"
        duration = None
        if info.get("duration"):
            # seconds -> mm:ss or hh:mm:ss
            secs = int(info["duration"])
            h, m = divmod(secs, 3600)
            m, s = divmod(m, 60)
            duration = f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
        page_url = info.get("webpage_url") or info.get("original_url") or query

        # Pick best audio URL
        if info.get("url") and info.get("acodec"):
            stream_url = info["url"]
        else:
            # sometimes formats array is needed
            fmts = info.get("formats") or []
            audio = next((f for f in fmts if f.get("acodec") and not f.get("video_codec")), None)
            stream_url = (audio or (fmts[-1] if fmts else {})).get("url")
            if not stream_url:
                raise RuntimeError("Couldn't extract stream URL")
        return stream_url, title, duration, page_url

async def ensure_joined(chat_id: int) -> None:
    """Join voice chat if not already joined."""
    try:
        await pytgcalls.join_group_call(chat_id, AudioPiped("silence.mp3"))
        # Immediately pause; real stream will replace it. We'll stop right away.
        await pytgcalls.leave_group_call(chat_id)
    except GroupCallNotFoundError:
        # No active voice chat
        raise GroupCallNotFoundError("No active voice chat. Start one in the group.")
    except Exception:
        # If already joined, it's fine.
        pass

async def play_next(chat_id: int) -> None:
    async with players_lock[chat_id]:
        while queues[chat_id]:
            track = queues[chat_id].popleft()
            now_playing[chat_id] = track
            try:
                await pytgcalls.join_group_call(chat_id, AudioPiped(track.url))
            except Exception:
                # If already in call, just change stream
                try:
                    await pytgcalls.change_stream(chat_id, AudioPiped(track.url))
                except Exception as e:
                    now_playing[chat_id] = None
                    raise e
            # Wait until the current stream finishes (best-effort):
            # yt-dlp URLs usually close when audio ends; we also monitor queue.
            # Sleep in small chunks; break if a skip/leave happens.
            for _ in range(60 * 60 * 4):  # up to ~4h safety cap
                await asyncio.sleep(5)
                if now_playing[chat_id] is None:
                    break
            # proceed to next item
        # Queue empty: stop playback
        try:
            await pytgcalls.leave_group_call(chat_id)
        except Exception:
            pass
        now_playing[chat_id] = None

# ── Commands ──────────────────────────────────────────────────────────────────

@app.on_message(filters.command(["start", "help"]))
async def start_help(_, m: Message):
    txt = (
        "**Krishnavi Music Bot**\n\n"
        "Add me to a group, start a voice chat, then use:\n"
        "• `/join` — join the active voice chat\n"
        "• `/play <song name or YouTube URL>` — queue a song\n"
        "• `/queue` — show current queue\n"
        "• `/skip` — skip current track\n"
        "• `/pause` / `/resume` — control playback\n"
        "• `/leave` — leave voice chat\n\n"
        "Tip: `/play https://youtu.be/...` also works."
    )
    await m.reply_text(txt)

@app.on_message(filters.command("join") & filters.group)
async def join_vc(_, m: Message):
    cid = m.chat.id
    try:
        await pytgcalls.join_group_call(cid, AudioPiped("silence.mp3"))
        await pytgcalls.leave_group_call(cid)
        await m.reply_text("Joined voice chat. Use /play to start music.")
    except GroupCallNotFoundError:
        await m.reply_text("No active voice chat found. Please start one, then try /join again.")
    except Exception as e:
        await m.reply_text(f"Join failed: {e}")

@app.on_message(filters.command("play") & filters.group)
async def cmd_play(_, m: Message):
    cid = m.chat.id
    if len(m.command) < 2:
        await m.reply_text("Usage: `/play <song name | YouTube URL>`", quote=True)
        return
    query = m.text.split(None, 1)[1].strip()
    await m.reply_text("Searching…")
    try:
        stream_url, title, duration, page_url = _ydl_extract(query)
        track = Track(title=title, url=stream_url, page_url=page_url, requester=m.from_user.mention if m.from_user else "Someone", duration=duration)
        queues[cid].append(track)
        await m.reply_text(f"Queued: **{track.title}** {f'`[{track.duration}]`' if track.duration else ''}\nRequested by {track.requester}")

        # If nothing is playing, start the player loop
        if now_playing[cid] is None:
            asyncio.create_task(play_next(cid))
    except GroupCallNotFoundError:
        await m.reply_text("No active voice chat. Start one, then /join and /play again.")
    except Exception as e:
        await m.reply_text(f"Failed to queue: {e}")

@app.on_message(filters.command("queue") & filters.group)
async def cmd_queue(_, m: Message):
    cid = m.chat.id
    np = now_playing[cid]
    q = list(queues[cid])
    if not np and not q:
        await m.reply_text("Queue is empty.")
        return
    lines = []
    if np:
        lines.append(f"**Now Playing:** {np.title} {f'`[{np.duration}]`' if np.duration else ''}\n")
    if q:
        for i, t in enumerate(q, 1):
            lines.append(f"{i}. {t.title} {f'`[{t.duration}]`' if t.duration else ''}")
    await m.reply_text("\n".join(lines))

@app.on_message(filters.command("skip") & filters.group)
async def cmd_skip(_, m: Message):
    cid = m.chat.id
    if now_playing[cid] is None:
        await m.reply_text("Nothing to skip.")
        return
    now_playing[cid] = None  # signal player loop to advance
    try:
        if queues[cid]:
            nxt = queues[cid][0]
            await pytgcalls.change_stream(cid, AudioPiped(nxt.url))
            # The loop will pop and set now_playing; here we just inform.
            await m.reply_text("⏭️ Skipping…")
        else:
            await pytgcalls.leave_group_call(cid)
            await m.reply_text("⏹️ Stopped (queue empty).")
    except Exception:
        await m.reply_text("Skip failed, but I will try to play next.")

@app.on_message(filters.command("pause") & filters.group)
async def cmd_pause(_, m: Message):
    try:
        await pytgcalls.pause_stream(m.chat.id)
        await m.reply_text("⏸️ Paused.")
    except Exception as e:
        await m.reply_text(f"Pause failed: {e}")

@app.on_message(filters.command("resume") & filters.group)
async def cmd_resume(_, m: Message):
    try:
        await pytgcalls.resume_stream(m.chat.id)
        await m.reply_text("▶️ Resumed.")
    except Exception as e:
        await m.reply_text(f"Resume failed: {e}")

@app.on_message(filters.command("leave") & filters.group)
async def cmd_leave(_, m: Message):
    try:
        queues[m.chat.id].clear()
        now_playing[m.chat.id] = None
        await pytgcalls.leave_group_call(m.chat.id)
        await m.reply_text("Left the voice chat. Bye!")
    except Exception as e:
        await m.reply_text(f"Leave failed: {e}")

# ── Entrypoint ────────────────────────────────────────────────────────────────

async def main():
    print("Starting Krishnavi Music Bot…")
    await app.start()
    await pytgcalls.start()
    print("Bot is up. Press Ctrl+C to stop.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

# ──────────────────────────────────────────────────────────────────────────────
# requirements.txt (create this file in your project)
#
# pyrogram==2.*
# tgcrypto
# pytgcalls
# yt-dlp
# ffmpeg-python
# python-dotenv
# ──────────────────────────────────────────────────────────────────────────────
# .env.example (copy to .env and fill values, or set as environment variables)
#
# API_ID=123456
# API_HASH=0123456789abcdef0123456789abcdef
# BOT_TOKEN=123456:AA...your_bot_token_here
# SESSION_NAME=krishnavi_session
# ──────────────────────────────────────────────────────────────────────────────
# Run locally (Linux/macOS):
#   1) Install FFmpeg (e.g., `sudo apt install ffmpeg`).
#   2) python3 -m venv .venv && source .venv/bin/activate
#   3) pip install -r requirements.txt
#   4) export $(grep -v '^#' .env | xargs)   # or set env vars manually
#   5) python bot.py
# Add the bot to a supergroup, start a voice chat, and send /play in the chat.
# ──────────────────────────────────────────────────────────────────────────────
