# ──────────────────────────────────────────────────────────────────────────────
# Krishnavi Music Bot – plays songs in Telegram Voice Chats with queue support
# Stack: Python 3.10+, Pyrogram v2, PyTgCalls, yt-dlp, FFmpeg
# Commands: /start, /help, /join, /play <query|YouTube URL>, /queue, /skip,
#           /pause, /resume, /leave
# Env: set API_ID, API_HASH, BOT_TOKEN (and optional SESSION_NAME)
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
from pytgcalls.exceptions import GroupCallNotFoundError, NotInGroupCallError
import yt_dlp

# ── Config ────────────────────────────────────────────────────────────────────
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SESSION_NAME = os.getenv("SESSION_NAME", "krishnavi_session")

if not (API_ID and API_HASH and BOT_TOKEN):
    raise SystemExit("Please set API_ID, API_HASH, BOT_TOKEN in environment.")

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
    url: str            # direct audio stream url
    page_url: str       # original page/video url
    requester: str
    duration: Optional[str] = None

# Per-chat queues/state
queues: Dict[int, Deque[Track]] = defaultdict(deque)
now_playing: Dict[int, Optional[Track]] = defaultdict(lambda: None)
locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _ydl_extract(query: str) -> Tuple[str, str, Optional[str], str]:
    """Return (stream_url, title, duration, page_url) from query or URL."""
    q = query if URL_RE.match(query) else f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
        info = ydl.extract_info(q, download=False)
        if "entries" in info:
            info = info["entries"][0]
        title = info.get("title") or "Unknown"
        duration = None
        if info.get("duration"):
            secs = int(info["duration"])
            h, rem = divmod(secs, 3600)
            m, s = divmod(rem, 60)
            duration = f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
        page_url = info.get("webpage_url") or info.get("original_url") or query

        # get best audio-only stream url
        stream_url = info.get("url")
        if not stream_url or not info.get("acodec"):
            fmts = info.get("formats") or []
            audio = next((f for f in fmts if f.get("acodec") and not f.get("video_codec")), None)
            stream_url = (audio or (fmts[-1] if fmts else {})).get("url")
        if not stream_url:
            raise RuntimeError("Couldn't extract audio stream URL")
        return stream_url, title, duration, page_url

async def _play_loop(chat_id: int) -> None:
    """Plays queued tracks sequentially in a chat's voice chat."""
    async with locks[chat_id]:
        while queues[chat_id]:
            track = queues[chat_id].popleft()
            now_playing[chat_id] = track
            try:
                # Try to join and start playing; if already in call, change stream
                try:
                    await pytgcalls.join_group_call(chat_id, AudioPiped(track.url))
                except Exception:
                    await pytgcalls.change_stream(chat_id, AudioPiped(track.url))
            except GroupCallNotFoundError:
                # No active voice chat; stop and inform
                now_playing[chat_id] = None
                return
            except Exception:
                now_playing[chat_id] = None
                # continue to next track
                continue

            # Wait until track finishes or /skip clears now_playing
            # We poll every few seconds since we don't have end-of-stream hooks.
            for _ in range(60 * 60 * 4):  # safety cap ~4h
                await asyncio.sleep(5)
                if now_playing[chat_id] is None:
                    break

        # queue finished
        try:
            await pytgcalls.leave_group_call(chat_id)
        except Exception:
            pass
        now_playing[chat_id] = None

# ── Commands ──────────────────────────────────────────────────────────────────
@app.on_message(filters.command(["start", "help"]))
async def start_help(_, m: Message):
    text = (
        "**Krishnavi Music Bot**\n\n"
        "Add me to a group, start a voice chat, then use:\n"
        "• `/play <song name | YouTube URL>` — queue a song\n"
        "• `/queue` — show queue\n"
        "• `/skip` — skip current track\n"
        "• `/pause` / `/resume` — control playback\n"
        "• `/leave` — leave voice chat\n\n"
        "Tip: `/play https://youtu.be/...` also works."
    )
    await m.reply_text(text)

@app.on_message(filters.command("join") & filters.group)
async def join_info(_, m: Message):
    await m.reply_text("Start a **voice chat** in this group first. Then just use `/play` — I will join automatically.")

@app.on_message(filters.command("play") & filters.group)
async def cmd_play(_, m: Message):
    if len(m.command) < 2:
        await m.reply_text("Usage: `/play <song name | YouTube URL>`", quote=True)
        return

    cid = m.chat.id
    query = m.text.split(None, 1)[1].strip()
    status = await m.reply_text("🔎 Searching…")

    try:
        stream_url, title, duration, page_url = _ydl_extract(query)
        requester = m.from_user.mention if m.from_user else "Someone"
        track = Track(title=title, url=stream_url, page_url=page_url, requester=requester, duration=duration)
        queues[cid].append(track)
        await status.edit_text(
            f"✅ Queued: **{track.title}** {f'`[{track.duration}]`' if track.duration else ''}\n"
            f"Requested by {track.requester}"
        )

        if now_playing[cid] is None:
            asyncio.create_task(_play_loop(cid))

    except GroupCallNotFoundError:
        await status.edit_text("❌ No active voice chat. Start one, then `/play` again.")
    except Exception as e:
        await status.edit_text(f"❌ Failed to queue: `{e}`")

@app.on_message(filters.command("queue") & filters.group)
async def cmd_queue(_, m: Message):
    cid = m.chat.id
    np = now_playing[cid]
    q = list(queues[cid])
    if not np and not q:
        await m.reply_text("🗒️ Queue is empty.")
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
    # Signal loop to move on
    now_playing[cid] = None
    try:
        if queues[cid]:
            nxt = queues[cid][0]
            await pytgcalls.change_stream(cid, AudioPiped(nxt.url))
            await m.reply_text("⏭️ Skipping…")
        else:
            await pytgcalls.leave_group_call(cid)
            await m.reply_text("⏹️ Stopped (queue ended).")
    except (NotInGroupCallError, GroupCallNotFoundError):
        await m.reply_text("Stopped.")
    except Exception:
        await m.reply_text("Skip failed, but I will try to continue.")

@app.on_message(filters.command("pause") & filters.group)
async def cmd_pause(_, m: Message):
    try:
        await pytgcalls.pause_stream(m.chat.id)
        await m.reply_text("⏸️ Paused.")
    except Exception as e:
        await m.reply_text(f"Pause failed: `{e}`")

@app.on_message(filters.command("resume") & filters.group)
async def cmd_resume(_, m: Message):
    try:
        await pytgcalls.resume_stream(m.chat.id)
        await m.reply_text("▶️ Resumed.")
    except Exception as e:
        await m.reply_text(f"Resume failed: `{e}`")

@app.on_message(filters.command("leave") & filters.group)
async def cmd_leave(_, m: Message):
    cid = m.chat.id
    queues[cid].clear()
    now_playing[cid] = None
    try:
        await pytgcalls.leave_group_call(cid)
        await m.reply_text("👋 Left the voice chat.")
    except Exception as e:
        await m.reply_text(f"Leave failed: `{e}`")

# ── Entrypoint ────────────────────────────────────────────────────────────────
async def main():
    print("Starting Krishnavi Music Bot…")
    await app.start()
    await pytgcalls.start()
    print("Bot is running. Press Ctrl+C to stop.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
