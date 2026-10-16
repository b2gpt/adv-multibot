import asyncio, contextvars, ipaddress, json, logging, os, re, shutil, socket, subprocess, tarfile, time, uuid, zipfile
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pyrogram import Client, enums, filters, idle
from pyrogram.file_id import FileId
from pyrogram.types import (InlineKeyboardButton as B, InlineKeyboardMarkup as KB,
                            InputMediaDocument, InputMediaPhoto)

try:  # newer kurigram uses ReplyParameters, old pyrogram uses reply_to_message_id
    from pyrogram.types import ReplyParameters
except ImportError:
    ReplyParameters = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))  # parallel jobs
SUPPORT_GROUP = os.getenv("SUPPORT_GROUP", "")  # e.g. @MyHelpGroup (optional)
OTHER_BOTS = os.getenv("OTHER_BOTS", "")  # optional: "Name|https://t.me/bot,Name2|https://t.me/bot2"
WATERMARK = os.getenv("WATERMARK", "codextrms").strip()  # empty string = watermark OFF
WM_FONT = os.getenv("WM_FONT", "")  # optional: path of a .ttf font for the watermark
MAX_RANGE = int(os.getenv("MAX_RANGE", "2000"))  # max messages between two links
CHANNEL_COPY_GAP = max(1.0, float(os.getenv("CHANNEL_COPY_GAP", "1.5")))
PERMANENT_CHANNEL_ID = int(os.getenv("PERMANENT_CHANNEL_ID", "-1004289007980"))
MAX_DIRECT_BYTES = int(os.getenv("MAX_DIRECT_BYTES", str(2 * 1024 ** 3)))
MAX_SOCIAL_BYTES = int(os.getenv("MAX_SOCIAL_BYTES", str(2 * 1024 ** 3)))
MAX_EXTRACTED_BYTES = int(os.getenv("MAX_EXTRACTED_BYTES", str(5 * 1024 ** 3)))
MAX_ARCHIVE_FILES = int(os.getenv("MAX_ARCHIVE_FILES", "500"))
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "multi_converter_bot")

WORK = Path("downloads")
THUMBS = Path("thumbs")
USERS_FILE = Path("users.json")
SETTINGS_FILE = Path("settings.json")
sem = asyncio.Semaphore(MAX_PARALLEL)
sessions: dict[int, dict] = {}
social_requests: dict[int, dict] = {}
channel_copy_requests: dict[int, dict] = {}
channel_copy_lock = asyncio.Lock()

app = Client("videobot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


def rkw(mid):
    """kwargs to send a message as a reply to message id `mid`."""
    if not mid:
        return {}
    if ReplyParameters is not None:
        return {"reply_parameters": ReplyParameters(message_id=mid)}
    return {"reply_to_message_id": mid}


def archive_targets(extra=None):
    targets = [PERMANENT_CHANNEL_ID]
    if extra is not None and extra not in targets:
        targets.append(extra)
    return targets

# ---------------------------------------------------------------- permissions
users: dict[str, str] = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else {}
mongo_client = None
mongo_users = None
mongo_metadata = None


def save_users():
    USERS_FILE.write_text(json.dumps(users))


async def initialize_user_store():
    global mongo_client, mongo_users, mongo_metadata
    if not MONGODB_URI:
        logging.info("MONGODB_URI not set; using local users.json for access permissions")
        return

    from pymongo import AsyncMongoClient

    mongo_client = AsyncMongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    await mongo_client.admin.command("ping")
    database = mongo_client[MONGODB_DATABASE]
    mongo_users = database["allowed_users"]
    mongo_metadata = database["_bot_metadata"]

    migration_id = "users_json_migration_v1"
    migration = await mongo_metadata.find_one({"_id": migration_id})
    if migration is None:
        if await mongo_users.count_documents({}) == 0 and users:
            for uid, name in users.items():
                await mongo_users.update_one({"_id": uid},
                                             {"$set": {"username": name}}, upsert=True)
            logging.info("Imported %d existing grants from users.json into MongoDB", len(users))
        await mongo_metadata.update_one({"_id": migration_id},
                                        {"$setOnInsert": {"completed": True}}, upsert=True)

    users.clear()
    async for document in mongo_users.find({}, {"username": 1}):
        users[str(document["_id"])] = document.get("username", "Unknown")
    logging.info("Loaded %d allowed users from MongoDB", len(users))


async def persist_user_grant(uid: int, name: str):
    user_id = str(uid)
    if mongo_users is not None:
        await mongo_users.update_one({"_id": user_id}, {"$set": {"username": name}}, upsert=True)
    else:
        users[user_id] = name
        save_users()
        return
    users[user_id] = name


async def persist_user_deny(uid: int) -> bool:
    user_id = str(uid)
    if mongo_users is not None:
        result = await mongo_users.delete_one({"_id": user_id})
        if result.deleted_count == 0:
            return False
    elif users.pop(user_id, None) is not None:
        save_users()
        return True
    else:
        return False
    users.pop(user_id, None)
    return True


def is_allowed(uid: int) -> bool:
    return uid == OWNER_ID or str(uid) in users


async def _allowed(_, __, update):
    return bool(update.from_user and is_allowed(update.from_user.id))

allowed = filters.create(_allowed)
owner = filters.user(OWNER_ID)

# ---------------------------------------------------------------- per-user settings
usettings: dict[str, dict] = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
DEF = {"as_video": True, "rename": False, "caption": None, "buttons": [], "jobs": 0}


def settings(uid) -> dict:
    d = usettings.setdefault(str(uid), {})
    for k, v in DEF.items():
        d.setdefault(k, list(v) if isinstance(v, list) else v)
    return d


def save_settings():
    SETTINGS_FILE.write_text(json.dumps(usettings))


def user_thumb(uid):
    p = THUMBS / f"{uid}.jpg"
    return str(p) if uid and p.exists() else None


# ---------------------------------------------------------------- coloured buttons
# Telegram Bot API "style": success = green, danger = red, default = app colour (purple in your theme)
def btn(text, data, style=None):
    if style:
        try:
            return B(text, callback_data=data, style=getattr(enums.ButtonStyle, style.upper()))
        except (TypeError, AttributeError):
            pass  # old pyrogram/kurigram -> plain button (pip install -U kurigram for colours)
    return B(text, callback_data=data)


def G(t, d):  # green
    return btn(t, d, "success")


def P(t, d):  # purple (default)
    return btn(t, d)


def R(t, d):  # red
    return btn(t, d, "danger")


def grid(items, per=3, back=True):
    rows = [[G(t, d) for t, d in items[i:i + per]] for i in range(0, len(items), per)]
    if back:
        rows.append([P("Main Menu", "m|menu")])
    return KB(rows)


def back_kb():
    return KB([[P("Main Menu", "m|menu")]])


async def resolve(c, m):
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user
    if len(m.command) < 2:
        return None
    arg = m.command[1].lstrip("@")
    return await c.get_users(int(arg) if arg.lstrip("-").isdigit() else arg)


@app.on_message(filters.command("grant") & owner)
async def grant(c, m):
    try:
        u = await resolve(c, m)
        if not u:
            return await m.reply("Use: /grant @username  (ya user id / reply)")
    except Exception as e:
        await m.reply(f"❌ User nahi mila: {e}\nUser ko pehle bot /start karne bolo ya user id use karo.")
        return
    try:
        await persist_user_grant(u.id, u.username or u.first_name)
    except Exception:
        logging.exception("Failed to save access grant for user %s", u.id)
        return await m.reply("❌ Permission database me save nahi hui. MongoDB connection check karo.")
    await m.reply(f"✅ Access diya: {u.mention} (`{u.id}`)")


@app.on_message(filters.command(["deny", "denid"]) & owner)
async def deny(c, m):
    try:
        u = await resolve(c, m)
        if not u:
            return await m.reply("Use: /deny @username  (ya user id / reply)")
    except Exception as e:
        await m.reply(f"❌ {e}")
        return
    try:
        removed = await persist_user_deny(u.id)
    except Exception:
        logging.exception("Failed to remove access for user %s", u.id)
        return await m.reply("❌ Permission database update nahi hui. MongoDB connection check karo.")
    if not removed:
        return await m.reply("Ye user list me nahi tha.")
    await m.reply(f"🚫 Access hata diya: {u.mention}")


@app.on_message(filters.command("users") & owner)
async def list_users(c, m):
    txt = "\n".join(f"• {n} (`{i}`)" for i, n in users.items()) or "Koi user nahi."
    await m.reply(f"**Allowed users:**\n{txt}")


@app.on_message(filters.command("info") & filters.private & allowed)
async def info_cmd(c, m):
    """/info -> your own info.  Owner: /info @username (ya user id / reply) -> kisi ki bhi info."""
    try:
        u = m.from_user
        if m.from_user.id == OWNER_ID and (m.reply_to_message or len(m.command) > 1):
            u = await resolve(c, m) or m.from_user
    except Exception as e:
        return await m.reply(f"❌ User nahi mila: {e}\nUser ko pehle bot /start karne bolo ya user id use karo.")
    uid = u.id
    st = usettings.get(str(uid)) or dict(DEF)
    acc = "👑 Owner" if uid == OWNER_ID else "✅ Allowed" if str(uid) in users else "❌ Not allowed"
    await m.reply(
        "👤 **User Info**\n\n"
        f"**Name:** {u.mention}\n"
        f"**Username:** {'@' + u.username if getattr(u, 'username', None) else '—'}\n"
        f"**ID:** `{uid}`\n"
        f"**Premium:** {'Yes' if getattr(u, 'is_premium', False) else 'No'}\n"
        f"**DC:** {getattr(u, 'dc_id', None) or '—'}\n"
        f"**Language:** {getattr(u, 'language_code', None) or '—'}\n"
        "**Account created:** Telegram API ye date provide nahi karta.\n"
        f"**Access:** {acc}\n\n"
        "⚙️ **Bot Data**\n"
        f"Jobs done: {st.get('jobs', 0)}\n"
        f"Upload as: {'🎥 Video' if st.get('as_video', True) else '📁 File'}\n"
        f"Rename File: {'✅ ON' if st.get('rename') else '❌ OFF'}\n"
        f"Custom caption: {'✅' if st.get('caption') else '❌'}\n"
        f"Buttons: {sum(len(r) for r in st.get('buttons', []))}\n"
        f"Custom thumbnail: {'✅' if user_thumb(uid) else '❌ (auto cover)'}")

# ---------------------------------------------------------------- thumbnails (/addthumb /delthumb)
async def save_thumb(c, m, pm):
    THUMBS.mkdir(exist_ok=True)
    uid = m.from_user.id
    raw = await c.download_media(pm, file_name=str(THUMBS / f"{uid}_raw.jpg"))
    if not raw:
        raise RuntimeError("Download fail")
    try:
        await run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw), "-vf",
                  "scale=320:320:force_original_aspect_ratio=decrease", "-frames:v", "1", "-q:v", "4",
                  str(THUMBS / f"{uid}.jpg"))
    finally:
        Path(raw).unlink(missing_ok=True)
    await m.reply("✅ Thumbnail save ho gaya. Ab aapke har upload ke video par yahi thumbnail lagega.\n"
                  "Hatane ke liye: /delthumb")


async def save_thumb_safe(c, m, pm):
    try:
        await save_thumb(c, m, pm)
    except Exception as e:
        await m.reply(f"❌ Thumbnail save nahi hua: `{str(e)[-300:]}`")


def is_img(m):
    return bool(m and (m.photo or (m.document and (m.document.mime_type or "").startswith("image/"))))


@app.on_message(filters.command("addthumb") & filters.private & allowed)
async def addthumb_cmd(c, m):
    """/addthumb as reply to a photo, or photo with caption /addthumb, or /addthumb then send a photo."""
    if is_img(m):
        return await save_thumb_safe(c, m, m)
    if is_img(m.reply_to_message):
        return await save_thumb_safe(c, m, m.reply_to_message)
    sessions.setdefault(m.from_user.id, {})["wait"] = "addthumb"
    await m.reply("🖼️ Ab ek **photo** bhejo — wahi aapke videos ka thumbnail ban jayegi.\n"
                  "(Ya kisi photo par reply karke /addthumb likho.)  Cancel: /cancel")


@app.on_message(filters.command("delthumb") & filters.private & allowed)
async def delthumb_cmd(c, m):
    p = THUMBS / f"{m.from_user.id}.jpg"
    if p.exists():
        p.unlink()
        return await m.reply("🗑 Thumbnail hata diya. Ab pehle jaisa auto cover aayega.")
    await m.reply("Aapka koi custom thumbnail set nahi hai.")

# ---------------------------------------------------------------- /start menu (Home / Functions / Commands / Disclaimer)
def start_text(u) -> str:
    sup = f"If any issues arise, report them at 👉 {SUPPORT_GROUP}\n\n" if SUPPORT_GROUP else ""
    return (f"**Hi** {u.mention}\n\nI am **Video Converter**.\n\n"
            "Many useful functions are available with Max Option — send me any video and choose an action.\n\n"
            f"{sup}**Legal Disclaimer:** /disclaimer")


def start_kb():
    return KB([[G("Other Bots", "s|bots"), G("Functions", "s|func")],
               [G("Commands", "s|cmds"), G("Archive Tools", "s|archive")],
               [G("Disclaimer", "s|disc")],
               [R("Close", "s|close")]])


COMMANDS_TXT = (
    "/start - Main menu\n\n"
    "/settings - Upload as Video/File, Rename File ON/OFF\n\n"
    "/addthumb - Apna thumbnail set karo (photo bhejo / photo par reply)\n\n"
    "/delthumb - Thumbnail hatao (auto cover wapas)\n\n"
    "/info - Apni info (Owner: /info @username). Account creation date Telegram nahi deta.\n\n"
    "YouTube / Instagram video link bhejo - video download karke yahin milega\n\n"
    "/bulk - Bahut saari videos / t.me links ki range ek saath process\n\n"
    "/forward channel_id - Current bulk ke originals aur results additional channel me bhejo\n\n"
    "/channelcopy - Accessible channel se media ko destination channel me copy karo\n\n"
    "/extracturl - Direct link se ZIP / 7Z / RAR / TAR extract\n\n"
    "/makearchive - Direct file link(s) se archive create\n\n"
    "/merge - Videos merge karo\n\n"
    "/done - Collection khatam, process shuru\n\n"
    "/cancel - Current session cancel\n\n"
    "/disclaimer - Legal disclaimer\n\n"
    "**Owner only:**\n/grant - User ko access do\n\n/deny - Access hatao\n\n/users - Allowed users list")

FUNCTIONS_TXT = (
    "**🎬 Video Functions**\n\n"
    "• 🖼️ Thumb/Cover Extractor, Screenshots, Manual Shots\n"
    "• ✏️ Caption And Buttons Editor, Metadata Editor, Video Renamer\n"
    "• 🎥 Stream Mapper / Remover / Extractor\n"
    "• ✂️ Video Trimmer, Videos Splitter, Generate Sample, Video Merger\n"
    "• 🔇 Remove Audio, Merge 🎥 And 🎵, Subtitle Merger\n"
    "• 🎵 Video To Audio, Audio Converter\n"
    "• 🗜 Video Optimizer, 🔄 Video Converter\n"
    "• 📡 To M3U8 (HLS) with All Quality (parallel)\n"
    "• ℹ️ Media Information, 📂 Create Archive\n"
    "• 🖼 Custom Thumbnail (/addthumb), 💧 Moving Watermark\n"
    "• 📦 Bulk: videos ya t.me link range, har video ka alag progress; originals/results permanent channel + optional /forward destination me\n"
    "• 🌐 Direct URL se archive extract/create (ZIP, 7Z, RAR, TAR)\n"
    "• 📥 YouTube / Instagram public video links download karke Telegram par bhejo\n"
    "• Live Progress ⚡ button on every download / process / upload")

DISCLAIMER_TXT = (
    "**⚠️ Legal Disclaimer**\n\n"
    "Is bot ko sirf aisi files par use karo jinka aapke paas right/permission ho. "
    "Copyrighted ya illegal content process karne ki zimmedari poori tarah user ki hai. "
    "Bot owner kisi bhi misuse ke liye responsible nahi hai.")


def nav_kb(cur):
    other = ("Commands", "s|cmds") if cur == "func" else ("Functions", "s|func")
    return KB([[P("Home", "s|home"), P(*other)], [R("Close", "s|close")]])


@app.on_message(filters.command(["start", "help"]) & filters.private)
async def start(c, m):
    if not is_allowed(m.from_user.id):
        return await m.reply(f"⛔ Aapko permission nahi hai.\nOwner se /grant karwao.\nYour ID: `{m.from_user.id}`")
    await m.reply(start_text(m.from_user), reply_markup=start_kb())


@app.on_message(filters.command("disclaimer") & filters.private & allowed)
async def disclaimer_cmd(c, m):
    await m.reply(DISCLAIMER_TXT, reply_markup=KB([[R("Close", "s|close")]]))


@app.on_callback_query(filters.regex(r"^s\|") & allowed)
async def start_cb(c, q):
    k = q.data.split("|")[1]
    await q.answer()
    if k == "close":
        return await q.message.delete()
    if k == "home":
        return await q.message.edit(start_text(q.from_user), reply_markup=start_kb())
    if k == "cmds":
        return await q.message.edit(COMMANDS_TXT, reply_markup=nav_kb("cmds"))
    if k == "func":
        return await q.message.edit(FUNCTIONS_TXT, reply_markup=nav_kb("func"))
    if k == "archive":
        return await q.message.edit(
            "🌐 **Direct Link Archive Tools**\n\n"
            "Archive extract: `/extracturl` then direct ZIP / 7Z / RAR / TAR URL.\n"
            "Archive create: `/makearchive` then one or more direct file URLs.",
            reply_markup=KB([[G("📥 Extract Archive", "a|url_extract")],
                             [G("📦 Create Archive", "a|url_create")],
                             [P("Home", "s|home"), R("Close", "s|close")]]))
    if k == "disc":
        return await q.message.edit(DISCLAIMER_TXT, reply_markup=nav_kb("cmds"))
    if k == "bots":
        rows = []
        for part in filter(None, (x.strip() for x in OTHER_BOTS.split(","))):
            if "|" in part:
                n, u = part.split("|", 1)
                rows.append([B(n.strip(), url=u.strip())])
        rows += [[P("Home", "s|home")], [R("Close", "s|close")]]
        txt = "**🤖 Other Bots**" if len(rows) > 2 else "Abhi koi aur bot add nahi hai.\n(`OTHER_BOTS` env me add kar sakte ho.)"
        return await q.message.edit(txt, reply_markup=KB(rows))

# ---------------------------------------------------------------- /settings
def settings_text(uid):
    st = settings(uid)
    return ("⚙️ **Settings**\n\n"
            f"Upload as: {'🎥 Video' if st['as_video'] else '📁 File'}\n"
            f"Rename File: {'✅ ON' if st['rename'] else '❌ OFF'}\n"
            f"Thumbnail: {'✅ Custom (/delthumb)' if user_thumb(uid) else 'Auto cover (/addthumb)'}")


def settings_kb(uid):
    st = settings(uid)
    return KB([[G(f"Upload as: {'🎥 Video' if st['as_video'] else '📁 File'}", "set|as")],
               [G(f"Rename File: {'✅ ON' if st['rename'] else '❌ OFF'}", "set|ren")],
               [R("Close", "s|close")]])


@app.on_message(filters.command("settings") & filters.private & allowed)
async def settings_cmd(c, m):
    await m.reply(settings_text(m.from_user.id), reply_markup=settings_kb(m.from_user.id))


@app.on_callback_query(filters.regex(r"^set\|") & allowed)
async def set_cb(c, q):
    uid, k = q.from_user.id, q.data.split("|")[1]
    st = settings(uid)
    if k == "as":
        st["as_video"] = not st["as_video"]
    elif k == "ren":
        st["rename"] = not st["rename"]
    save_settings()
    await q.answer("Saved ✅")
    try:
        await q.message.edit(settings_text(uid), reply_markup=settings_kb(uid))
    except Exception:
        pass

# ---------------------------------------------------------------- ffmpeg helpers
async def run(*cmd):
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    if p.returncode:
        raise RuntimeError(err.decode(errors="ignore")[-600:])
    return out.decode(errors="ignore")


async def ff(*args):
    job = CUR.get()
    if job is None:  # no progress tracking
        return await run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args)
    chk(job)
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    job.procs.add(p)
    idx = CUR_IDX.get()
    err_task = asyncio.create_task(p.stderr.read())  # drain stderr so ffmpeg never blocks
    while True:
        line = await p.stdout.readline()
        if not line:
            break
        line = line.decode(errors="ignore").strip()
        if line.startswith(("out_time_us=", "out_time_ms=")):  # both are microseconds
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if us > 0 and job.stage == "convert":
                if idx is None:
                    job.cur = min(us / 1e6, job.total) if job.total else us / 1e6
                else:  # parallel tasks: sum of every task's progress
                    job.cparts[idx] = us / 1e6
                    job.cur = min(sum(job.cparts.values()), job.total) if job.total else sum(job.cparts.values())
    await p.wait()
    err = (await err_task).decode(errors="ignore")
    job.procs.discard(p)
    chk(job)
    if p.returncode:
        raise RuntimeError(err[-600:])
    return ""


TIME_RE = re.compile(r"^\d+(:\d{1,2}){0,2}(\.\d+)?$")
AUDIO = {  # fmt: (codec, supports bitrate)
    "mp3": ("libmp3lame", True), "wav": ("pcm_s16le", False), "ac3": ("ac3", True),
    "aac": ("aac", True), "flac": ("flac", False), "wma": ("wmav2", True),
    "m4a": ("aac", True), "opus": ("libopus", True), "ogg": ("libvorbis", True),
}
IMG = {".jpg", ".jpeg", ".png", ".webp"}
VID_EXT = {".mp4", ".mkv", ".m4v", ".mov", ".avi"}


def parse_duration(t: str) -> int | None:
    m = re.fullmatch(r"(\d+)\s*([smh]?)", t.strip().lower())
    if not m:
        return None
    return int(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[m[2]]


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


async def media_info(src: Path, msg_date=None) -> str:
    j = json.loads(await run("ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(src)))
    f = j["format"]
    ftags = {k.lower(): v for k, v in (f.get("tags") or {}).items()}
    date_keys = ("creation_time", "date", "encoded_date", "date_recorded", "year")
    ct = next((ftags[k] for k in date_keys if ftags.get(k)), None)
    if not ct:  # maybe only the streams carry a date
        for s in j["streams"]:
            stags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
            ct = next((stags[k] for k in date_keys if stags.get(k)), None)
            if ct:
                break
    if ct:
        ct = str(ct).strip().replace("T", " ")
    lines = [f"📄 **{src.name}**", f"Size: {human(f.get('size', 0))}",
             f"Duration: {float(f.get('duration', 0)):.1f}s", f"Bitrate: {int(f.get('bit_rate', 0)) // 1000} kbps",
             f"Container: {f.get('format_long_name', '')}",
             f"📅 Media creation date: {ct or 'metadata me date available nahi hai'}"]
    if msg_date:
        try:
            lines.append(f"📨 Telegram upload date: {msg_date.strftime('%d %b %Y, %H:%M UTC')}")
        except Exception:
            pass
    for key, label in (("title", "Title"), ("artist", "Artist"), ("comment", "Comment"), ("encoder", "Encoder")):
        if ftags.get(key):
            lines.append(f"{label}: {ftags[key]}")
    for s in j["streams"]:
        t = s["codec_type"]
        if t == "video":
            lines.append(f"🎞 Video: {s['codec_name']} {s.get('width')}x{s.get('height')} {s.get('r_frame_rate')} fps")
        elif t == "audio":
            lines.append(f"🔊 Audio: {s['codec_name']} {s.get('sample_rate')}Hz {s.get('channels')}ch {s.get('tags', {}).get('language', '')}")
        elif t == "subtitle":
            lines.append(f"💬 Sub: {s['codec_name']} {s.get('tags', {}).get('language', '')}")
    return "\n".join(lines)


async def get_streams(src: Path) -> list:
    j = json.loads(await run("ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(src)))
    return j.get("streams", [])


def sdesc(s) -> str:
    t, tags, c = s.get("codec_type"), s.get("tags", {}), s.get("codec_name", "?")
    lang = tags.get("language", "")
    if t == "video":
        d = f"🎞 {c} {s.get('width')}x{s.get('height')}"
    elif t == "audio":
        d = f"🔊 {c} {s.get('channels', '?')}ch {lang}"
    elif t == "subtitle":
        d = f"💬 {c} {lang}"
    else:
        d = f"📎 {t} {c}"
    return f"#{s['index']} {d}".strip()[:40]

# ---------------------------------------------------------------- progress tracking
CUR: contextvars.ContextVar = contextvars.ContextVar("cur_job", default=None)
CUR_IDX: contextvars.ContextVar = contextvars.ContextVar("cur_idx", default=None)  # parallel sub-task id
jobs: dict[str, "Job"] = {}


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, uid, status, dc=None):
        self.id = uuid.uuid4().hex[:8]
        self.uid, self.status, self.dc = uid, status, dc
        self.cancelled = False
        self.tag = ""
        self.procs: set = set()
        self.cparts: dict[int, float] = {}
        self.parts: dict[int, tuple[int, int]] = {}
        self.label = ""
        self.reset("queued")
        jobs[self.id] = self

    def reset(self, stage, label=""):
        self.stage, self.label = stage, label
        self.cur = self.total = 0
        self.parts = {}
        self.cparts = {}
        self.t0 = time.time()

    def cancel(self):
        self.cancelled = True
        for p in list(self.procs):
            if p.returncode is None:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass


def chk(job):
    if job and job.cancelled:
        raise Cancelled()


STAGE_TXT = {
    "queued": "⏳ Your Media file is in Queue....",
    "download": "📥 Your Media file is Downloading....",
    "convert": "⚙️ Your Media file is Processing....",
    "upload": "📤 Your Media file is Uploading....",
}


def job_kb(job):
    return KB([[G("Progress ⚡", f"pg|{job.id}"), R("Cancel ❌", f"cx|{job.id}")]])


async def set_stage(job, stage, label=""):
    chk(job)
    job.reset(stage, label)
    txt = STAGE_TXT[stage] + (f"\n{label}" if label else "")
    if job.tag:
        txt = f"🔢 Video {job.tag}\n" + txt
    try:
        await job.status.edit(txt, reply_markup=job_kb(job))
    except Exception:
        pass  # MessageNotModified / flood -> ignore


def bar(pct, n=10):
    k = max(0, min(n, int(pct / (100 / n))))
    return "■" * k + "□" * (n - k)


def fmt_t(sec):
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def render(job) -> str:
    """Text shown in the popup when user taps the Progress button (Telegram alert limit = 200 chars)."""
    if job.stage == "queued":
        return "⏳ Queue me hai... apni baari ka wait karo."
    pct = job.cur / job.total * 100 if job.total else 0
    el = max(time.time() - job.t0, 0.001)
    rate = job.cur / el
    eta = (job.total - job.cur) / rate if rate else 0
    b = bar(pct)
    tag = f" ({job.label})" if job.label else ""
    if job.stage == "convert":
        if not job.total:
            return f"Processing....{tag}\nPlease wait, ffmpeg chal raha hai ⏳"
        return (f"Processing....{tag} [{b}]\n{pct:.2f}% ⇨ {hms(job.cur)} Of {hms(job.total)} "
                f"⇨ Speed : {rate:.1f}x ⇨ Time Left : {fmt_t(eta)}")
    head = {"download": "Downloading....", "upload": "Uploading...."}[job.stage]
    if job.stage == "download" and job.dc:
        head += f" from DC{job.dc}"
    if not job.total:
        return f"{head}{tag}\nPlease wait ⏳"
    return (f"{head}{tag} [{b}]\n{pct:.2f}% ⇨ {human(job.cur)} Of {human(job.total)} "
            f"⇨ Speed : {human(rate)}/s ⇨ Time Left : {fmt_t(eta)}")


async def progress_watch(job):
    while jobs.get(job.id) is job:
        await asyncio.sleep(3)
        if jobs.get(job.id) is not job:
            break
        tag = f"🔢 Video {job.tag}\n" if job.tag else ""
        try:
            await job.status.edit(tag + render(job), reply_markup=job_kb(job))
        except Exception as e:
            logging.debug("Progress update failed for job %s: %s", job.id, e)


async def stop_progress_watch(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def prog_cb(job, idx=0):
    """Pyrogram progress callback (works for both download & upload)."""
    async def _cb(cur, total):
        if job.cancelled:
            app.stop_transmission()
            return
        job.parts[idx] = (cur, total)
        job.cur = sum(v[0] for v in job.parts.values())
        job.total = sum(v[1] for v in job.parts.values())
    return _cb


def dc_of(msg):
    try:
        media = msg.video or msg.document or msg.audio
        return FileId.decode(media.file_id).dc_id
    except Exception:
        return None


async def probe(src: Path) -> dict:
    try:
        j = json.loads(await run("ffprobe", "-v", "error", "-select_streams", "v:0",
                                 "-show_entries", "stream=width,height:format=duration", "-of", "json", str(src)))
        st = (j.get("streams") or [{}])[0]
        return {"dur": float(j.get("format", {}).get("duration") or 0),
                "w": int(st.get("width") or 0), "h": int(st.get("height") or 0)}
    except Exception:
        return {"dur": 0.0, "w": 0, "h": 0}


async def make_thumb(src: Path, dur: float):
    t = src.with_name(src.stem + "_thumb.jpg")
    try:
        await run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(min(1.0, dur / 2) if dur else 0),
                  "-i", str(src), "-frames:v", "1", "-vf", "scale=320:-2", str(t))
        return str(t) if t.exists() else None
    except Exception:
        return None


def to_secs(t: str) -> float:
    n = 0.0
    for x in t.split(":"):
        n = n * 60 + float(x)
    return n

# ---------------------------------------------------------------- watermark (light text, jumps to a random place every few seconds)
_FONTS = [WM_FONT, "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
          "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
          "/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf"]
FONT = next((f for f in _FONTS if f and Path(f).exists()), None)
if FONT:
    FONT = FONT.replace("\\", "/").replace(":", "\\:")

# PRE  = watermark the source first, then run the action on it (needed for HLS segments / subtitle merge)
# POST = run the action, then watermark every video it produced
PRE_WM = {"hls", "hlsall", "sub", "zip", "meta", "rename"}
POST_WM = {"trim", "split", "opt", "conv", "sample", "noaud", "mux", "smap", "sext"}


def wm_filter(h):
    size = max(14, int((h or 720) / 30))
    T = 6  # position changes every 6 seconds

    def r(a):  # deterministic pseudo-random 0..1 that changes every T seconds
        v = f"sin((floor(t/{T})+1)*{a})*43758.5453"
        return f"({v}-floor({v}))"
    text = WATERMARK.replace("'", "").replace(":", "\\:")
    font = f"fontfile='{FONT}':" if FONT else ""
    return (f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
            f"drawtext={font}text='{text}':fontcolor=white@0.35:fontsize={size}:"
            f"shadowcolor=black@0.25:shadowx=1:shadowy=1:"
            f"x='(w-text_w)*{r(12.9898)}':y='(h-text_h)*{r(78.233)}'")


async def add_wm(src: Path, wdir: Path, info: dict) -> Path:
    """Create a watermarked copy or fail instead of delivering an unwatermarked file."""
    wdir.mkdir(parents=True, exist_ok=True)
    suf = src.suffix.lower() if src.suffix.lower() in VID_EXT else ".mp4"
    o = wdir / f"{src.stem}{suf}"
    base = ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-vf", wm_filter(info.get("h")),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    subs = ["-map", "0:s?", "-c:s", "copy"]
    tail = ["-movflags", "+faststart"] if suf in (".mp4", ".m4v", ".mov") else []
    attempts = [[*base, *subs, "-c:a", "copy", *tail],
                [*base, *subs, "-c:a", "aac", "-b:a", "160k", *tail],
                [*base, "-c:a", "aac", "-b:a", "160k", *tail]]
    last = None
    for args in attempts:
        try:
            await ff(*args, str(o))
            return o
        except RuntimeError as e:
            o.unlink(missing_ok=True)
            last = e
    raise RuntimeError(f"Watermark processing failed: {str(last)[-300:]}") from last


async def wm_outputs(job, files, wdir):
    out = []
    for i, f in enumerate(files, 1):
        if f.suffix.lower() not in VID_EXT:
            out.append(f)
            continue
        await set_stage(job, "convert", f"💧 Watermark {i}/{len(files)}" if len(files) > 1 else "💧 Watermark")
        inf = await probe(f)
        job.total = inf["dur"]
        out.append(await add_wm(f, wdir, inf))
    return out

# ---------------------------------------------------------------- actions
async def copy_try(src, out, tag, *args):
    """ffmpeg stream-copy into the same container; if the container can't hold it -> mkv."""
    o = out / f"{src.stem}{tag}{src.suffix}"
    try:
        await ff(*args, str(o))
        return [o]
    except RuntimeError:
        o.unlink(missing_ok=True)
        o = out / f"{src.stem}{tag}.mkv"
        await ff(*args, str(o))
        return [o]


async def act_trim(src, out, p):
    o = out / f"{src.stem}_trim{src.suffix}"
    job = CUR.get()
    if job:
        try:
            job.total = max(to_secs(p["end"]) - to_secs(p["start"]), 1)
        except ValueError:
            pass
    await ff("-ss", p["start"], "-to", p["end"], "-i", str(src), "-c", "copy", "-avoid_negative_ts", "make_zero", str(o))
    return [o]


async def act_split(src, out, p):
    pat = out / f"{src.stem}_part%03d{src.suffix}"
    await ff("-i", str(src), "-map", "0", "-c", "copy", "-f", "segment", "-segment_time", str(p["secs"]),
             "-reset_timestamps", "1", str(pat))
    return sorted(out.glob(f"{src.stem}_part*{src.suffix}"))


async def act_aud(src, out, p):
    codec, br = AUDIO[p["fmt"]]
    o = out / f"{src.stem}.{p['fmt']}"
    args = ["-i", str(src), "-vn", "-c:a", codec]
    if br:
        args += ["-b:a", f"{p['q']}k"]
    await ff(*args, str(o))
    return [o]


async def act_opt(src, out, p):
    o = out / f"{src.stem}_opt.mp4"
    await ff("-i", str(src), "-c:v", "libx264", "-preset", "medium", "-crf", str(p["crf"]), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(o))
    return [o]


async def act_conv(src, out, p):
    fmt = p["fmt"]
    o = out / f"{src.stem}.{'mp4' if fmt == 'stream' else fmt}"
    if fmt in ("mp4", "mkv", "m4v"):
        try:
            await ff("-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", str(o))
            return [o]
        except RuntimeError:
            pass
    if fmt == "avi":
        await ff("-i", str(src), "-c:v", "libx264", "-c:a", "libmp3lame", str(o))
    else:
        await ff("-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-movflags", "+faststart", str(o))
    return [o]


async def act_hls(src, out, p):
    d = out / "hls"
    d.mkdir(parents=True, exist_ok=True)
    base = ["-i", str(src), "-f", "hls", "-hls_time", "10", "-hls_list_size", "0", "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(d / "seg_%04d.ts")]
    pl = str(d / "index.m3u8")
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
    if p["q"] == "orig":
        h = (await probe(src))["h"]
        if not h:
            streams = await get_streams(src)
            h = next((int(s["height"]) for s in streams
                      if s.get("codec_type") == "video" and s.get("height")), 0)
        label = f"original_{h}p" if h else "original"  # zip name tells which quality the original is
        try:
            await ff(*base, "-c", "copy", pl)
        except RuntimeError:
            for f in d.iterdir():
                f.unlink()
            await ff(*base, *enc, pl)
    else:
        label = f"{p['q']}p"
        await ff(*base, "-vf", f"scale=-2:{p['q']}", *enc, pl)
    z = out / f"{src.stem}_hls_{label}.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        for f in d.iterdir():
            zf.write(f, f.name)
    return [z]


async def act_hls_all(src, out, p):
    """Original + every lower quality at once (parallel ffmpeg). Qualities equal/above the source height are skipped
    (the 'original_XXXp' zip already covers them)."""
    info = await probe(src)
    qs = ["orig"] + [q for q in (360, 480, 720, 1080) if not info["h"] or q < info["h"]]
    job = CUR.get()
    if job:
        job.total = info["dur"] * len(qs)

    async def one(i, q):
        CUR_IDX.set(i)  # each gather() task has its own context copy
        d = out / f"q{i}"
        d.mkdir(parents=True, exist_ok=True)
        return await act_hls(src, d, {"q": str(q)})

    res = await asyncio.gather(*[one(i, q) for i, q in enumerate(qs)], return_exceptions=True)
    errs = [r for r in res if isinstance(r, BaseException)]
    if errs:
        raise next((e for e in errs if isinstance(e, Cancelled)), errs[0])
    return [f for r in res for f in r]


async def act_thumb(src, out, p):
    dur = (await probe(src))["dur"]
    t = p["t"]
    if t == "auto":
        sec = dur / 2
    elif t.startswith("p"):
        sec = dur * int(t[1:]) / 100
    else:
        sec = to_secs(t)
    if dur:
        sec = min(max(sec, 0), max(dur - 0.1, 0))
    o = out / f"{src.stem}_cover.jpg"
    await ff("-ss", f"{sec:.2f}", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(o))
    if not o.exists() or not o.stat().st_size:
        raise RuntimeError("Frame nahi nikla (time video se bada hai?)")
    return [o]


async def act_shots(src, out, p):
    dur = (await probe(src))["dur"]
    if "times" in p:
        secs = [to_secs(t) for t in p["times"]]
    else:
        n = p["n"]
        secs = [dur * (i + 0.5) / n for i in range(n)]
    files, job = [], CUR.get()
    for i, sec in enumerate(secs, 1):
        o = out / f"{src.stem}_shot{i:02d}.jpg"
        try:
            await ff("-ss", f"{sec:.2f}", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(o))
        except RuntimeError:
            continue
        if o.exists() and o.stat().st_size:
            files.append(o)
        if job and job.total:
            job.cur = job.total * i / len(secs)
    if not files:
        raise RuntimeError("Koi screenshot nahi bana (time video se bada hai?)")
    return files


async def act_sample(src, out, p):
    dur = (await probe(src))["dur"]
    n = p["secs"]
    start = max(0.0, dur / 2 - n / 2) if dur > n else 0.0
    job = CUR.get()
    if job:
        job.total = min(n, dur) if dur else n
    o = out / f"{src.stem}_sample{src.suffix}"
    await ff("-ss", f"{start:.2f}", "-i", str(src), "-t", str(n), "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
             "-avoid_negative_ts", "make_zero", str(o))
    return [o]


async def act_noaud(src, out, p):
    return await copy_try(src, out, "_noaudio", "-i", str(src), "-map", "0:v", "-c", "copy")


async def act_meta(src, out, p):
    args = ["-i", str(src), "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-map_metadata", "0"]
    for k, v in p["meta"].items():
        args += ["-metadata", f"{k}={v}"]
    return await copy_try(src, out, "", *args)


async def act_smap(src, out, p):
    maps = [x for i in p["maps"] for x in ("-map", f"0:{i}")]
    return await copy_try(src, out, p.get("tag", "_mapped"), "-i", str(src), *maps, "-c", "copy")


EXT = {"aac": "aac", "mp3": "mp3", "ac3": "ac3", "eac3": "eac3", "flac": "flac", "opus": "opus", "vorbis": "ogg",
       "dts": "dts", "truehd": "mka", "subrip": "srt", "ass": "ass", "ssa": "ass", "webvtt": "vtt", "mov_text": "srt",
       "hdmv_pgs_subtitle": "sup", "dvd_subtitle": "mks"}


async def act_sext(src, out, p):
    st = p["stream"]
    codec, t = st.get("codec_name", ""), st.get("codec_type", "")
    ext = EXT.get(codec) or ("mkv" if t == "video" else "mka" if t == "audio" else "mks")
    if codec.startswith("pcm_"):
        ext = "wav"
    o = out / f"{src.stem}_{t}{st['index']}.{ext}"
    args = ["-i", str(src), "-map", f"0:{st['index']}"]
    args += ["-c:s", "srt"] if codec == "mov_text" else ["-c", "copy"]
    await ff(*args, str(o))
    return [o]


async def act_mux(src, out, p):
    return await copy_try(src, out, "_merged", "-i", str(src), "-i", str(p["extra_path"]),
                          "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest")


async def act_sub(src, out, p):
    sub = Path(p["extra_path"])
    n = len([s for s in await get_streams(src) if s.get("codec_type") == "subtitle"])
    codec = "ass" if sub.suffix.lower() in (".ass", ".ssa") else "srt"
    o = out / f"{src.stem}_sub.mkv"
    await ff("-i", str(src), "-i", str(sub), "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map", "1:0",
             "-c", "copy", f"-c:s:{n}", codec, str(o))
    return [o]


async def act_zip(src, out, p):
    z = out / f"{src.stem}.zip"

    def _zip():
        with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
            zf.write(src, src.name)
    await asyncio.to_thread(_zip)
    return [z]


def direct_urls(text):
    urls = [line.strip().strip("<>") for line in text.splitlines() if line.strip()]
    if not urls or len(urls) > 20:
        raise ValueError("1 se 20 direct URLs bhejo (har URL alag line me).")
    for url in urls:
        validate_public_url(url)
    return urls


def validate_public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Sirf valid public http/https direct links allowed hain.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                                      type=socket.SOCK_STREAM)
    except OSError as e:
        raise ValueError(f"URL hostname resolve nahi hua: {parsed.hostname}") from e
    if not addresses or any(not ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in addresses):
        raise ValueError("Private/local network URLs allowed nahi hain.")


class PublicRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_direct(url, destination, job):
    opener = build_opener(PublicRedirectHandler())
    request = Request(url, headers={"User-Agent": "VideoConverterBot/1.0"})
    try:
        response = opener.open(request, timeout=30)
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"Direct link download failed: {e}") from e
    with response:
        length = response.headers.get("Content-Length")
        total = int(length) if length and length.isdigit() else 0
        if total > MAX_DIRECT_BYTES:
            raise RuntimeError(f"File MAX_DIRECT_BYTES se badi hai ({human(total)}).")
        job.total, job.cur = total, 0
        message = Message()
        message["content-disposition"] = response.headers.get("Content-Disposition", "")
        filename = message.get_filename()
        if filename:
            filename = unquote(filename)
        else:
            filename = Path(unquote(urlsplit(response.geturl()).path)).name
        filename = clean_name(Path(filename or "").name) or "downloaded_file"
        target = destination / filename
        if not target.suffix:
            raise RuntimeError("Link se file extension nahi mili; direct file URL ya filename wala link bhejo.")
        destination.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        with target.open("wb") as output:
            while True:
                if job.cancelled:
                    raise Cancelled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_DIRECT_BYTES:
                    raise RuntimeError(f"File MAX_DIRECT_BYTES se badi hai ({human(downloaded)}).")
                output.write(chunk)
                job.cur = downloaded
                if not total:
                    job.total = downloaded
        if not downloaded:
            target.unlink(missing_ok=True)
            raise RuntimeError("Direct link se empty file mili.")
        return target


async def download_direct(url, destination, job):
    validate_public_url(url)
    return await asyncio.to_thread(_download_direct, url, destination, job)


def _safe_member_path(root, name):
    normalized = str(name).replace("\\", "/")
    parts = normalized.split("/")
    if normalized.startswith("/") or (parts and ":" in parts[0]) or any(part == ".." for part in parts):
        raise RuntimeError(f"Unsafe archive path reject kiya: {name[:120]}")
    target = (root / Path(*[part for part in parts if part not in ("", ".")])).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as e:
        raise RuntimeError(f"Unsafe archive path reject kiya: {name[:120]}") from e
    return target


def _extract_zip(archive, destination):
    total = 0
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise RuntimeError(f"Archive me {MAX_ARCHIVE_FILES} se zyada entries hain.")
        for member in members:
            target = _safe_member_path(destination, member.filename)
            mode = member.external_attr >> 16
            if member.flag_bits & 1:
                raise RuntimeError("Password-protected ZIP abhi supported nahi hai.")
            if (mode & 0o170000) == 0o120000:
                raise RuntimeError("Archive ke symbolic links extract nahi kiye jaate.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += member.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise RuntimeError(f"Extracted data MAX_EXTRACTED_BYTES ({human(MAX_EXTRACTED_BYTES)}) se badi hai.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _extract_tar(archive, destination):
    total = 0
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        if len(members) > MAX_ARCHIVE_FILES:
            raise RuntimeError(f"Archive me {MAX_ARCHIVE_FILES} se zyada entries hain.")
        for member in members:
            target = _safe_member_path(destination, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError("Archive ke links/devices/special entries extract nahi kiye jaate.")
            total += member.size
            if total > MAX_EXTRACTED_BYTES:
                raise RuntimeError(f"Extracted data MAX_EXTRACTED_BYTES ({human(MAX_EXTRACTED_BYTES)}) se badi hai.")
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                raise RuntimeError(f"Archive entry read nahi hui: {member.name}")
            with src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _extract_7z(archive, destination):
    try:
        import py7zr
    except ImportError as e:
        raise RuntimeError("7z support ke liye requirements.txt install karo (py7zr).") from e
    with py7zr.SevenZipFile(archive, mode="r") as zf:
        members = zf.list()
        if len(members) > MAX_ARCHIVE_FILES:
            raise RuntimeError(f"Archive me {MAX_ARCHIVE_FILES} se zyada entries hain.")
        total = 0
        for member in members:
            _safe_member_path(destination, member.filename)
            if getattr(member, "is_symlink", False):
                raise RuntimeError("Archive ke symbolic links extract nahi kiye jaate.")
            if not member.is_directory:
                total += member.uncompressed or 0
                if total > MAX_EXTRACTED_BYTES:
                    raise RuntimeError(f"Extracted data MAX_EXTRACTED_BYTES ({human(MAX_EXTRACTED_BYTES)}) se badi hai.")
        zf.extractall(path=destination)


def _extract_rar(archive, destination):
    try:
        import rarfile
    except ImportError as e:
        raise RuntimeError("RAR support ke liye requirements.txt install karo (rarfile).") from e
    try:
        tool = _archive_tool("RAR_EXECUTABLE", ("unrar", "rar", "7z", "7zz", "winrar.exe"),
                             (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files\WinRAR\UnRAR.exe",
                              r"C:\Program Files\WinRAR\WinRAR.exe"))
        if tool:
            if Path(tool).name.lower().startswith(("7z", "7zz")):
                rarfile.SEVENZIP_TOOL = tool
            else:
                rarfile.UNRAR_TOOL = tool
            rarfile.CURRENT_SETUP = None
        with rarfile.RarFile(archive) as rf:
            members = rf.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                raise RuntimeError(f"Archive me {MAX_ARCHIVE_FILES} se zyada entries hain.")
            total = 0
            for member in members:
                target = _safe_member_path(destination, member.filename)
                if member.is_symlink():
                    raise RuntimeError("Archive ke symbolic links extract nahi kiye jaate.")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += member.file_size
                if total > MAX_EXTRACTED_BYTES:
                    raise RuntimeError(f"Extracted data MAX_EXTRACTED_BYTES ({human(MAX_EXTRACTED_BYTES)}) se badi hai.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with rf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"RAR extraction failed; install UnRAR/7-Zip backend: {e}") from e


def _archive_type(path):
    name = path.name.lower()
    for ext in (".tar.bz2", ".tar.gz", ".tar.xz", ".tbz2", ".tgz", ".txz", ".zip", ".7z", ".rar", ".tar"):
        if name.endswith(ext):
            return ext
    return ""


def _archive_tool(env_name, names, windows_paths=()):
    configured = os.getenv(env_name)
    if configured:
        candidate = shutil.which(configured) or (configured if Path(configured).is_file() else None)
        if candidate:
            return candidate
    for name in names:
        candidate = shutil.which(name)
        if candidate:
            return candidate
    for path in windows_paths:
        if Path(path).is_file():
            return path
    return None


def _extract_archive(archive, destination):
    ext = _archive_type(archive)
    if not ext:
        raise RuntimeError("Archive format pehchaan nahi aaya. ZIP, 7z, RAR ya TAR link bhejo.")
    destination.mkdir(parents=True, exist_ok=True)
    if ext == ".zip":
        _extract_zip(archive, destination)
    elif ext == ".7z":
        _extract_7z(archive, destination)
    elif ext == ".rar":
        _extract_rar(archive, destination)
    else:
        _extract_tar(archive, destination)
    files = [p for p in destination.rglob("*") if p.is_file()]
    if not files:
        raise RuntimeError("Archive me bhejne layak koi file nahi mili.")
    return files


def _create_archive(files, destination, archive_format):
    base = clean_name(files[0].stem) or "archive"
    suffixes = {"zip": ".zip", "7z": ".7z", "rar": ".rar", "tar": ".tar",
                "tar.gz": ".tar.gz", "tar.bz2": ".tar.bz2", "tar.xz": ".tar.xz"}
    if archive_format not in suffixes:
        raise RuntimeError("Archive format supported nahi hai.")
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"{base}{suffixes[archive_format]}"
    if archive_format == "zip":
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            used = set()
            for i, path in enumerate(files, 1):
                name = path.name
                if name in used:
                    name = f"{i}_{name}"
                used.add(name)
                zf.write(path, name)
    elif archive_format == "7z":
        try:
            import py7zr
        except ImportError as e:
            raise RuntimeError("7z support ke liye requirements.txt install karo (py7zr).") from e
        with py7zr.SevenZipFile(output, mode="w") as zf:
            used = set()
            for i, path in enumerate(files, 1):
                name = path.name if path.name not in used else f"{i}_{path.name}"
                used.add(name)
                zf.write(path, name)
    elif archive_format == "rar":
        tool = _archive_tool("RAR_EXECUTABLE", ("rar", "rar.exe", "winrar", "winrar.exe"),
                             (r"C:\Program Files\WinRAR\rar.exe", r"C:\Program Files\WinRAR\WinRAR.exe"))
        if not tool:
            raise RuntimeError("RAR create ke liye server par WinRAR/RAR command-line tool install hona chahiye.")
        result = subprocess.run([tool, "a", "-ep1", str(output), *(str(f) for f in files)],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"RAR create failed: {result.stderr[-400:] or result.stdout[-400:]}")
    else:
        mode = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}[archive_format]
        with tarfile.open(output, mode) as tf:
            used = set()
            for i, path in enumerate(files, 1):
                name = path.name if path.name not in used else f"{i}_{path.name}"
                used.add(name)
                tf.add(path, arcname=name)
    return output


def archive_format_kb():
    return KB([[G("ZIP", "ar|zip"), G("7Z", "ar|7z"), G("RAR", "ar|rar")],
               [G("TAR", "ar|tar"), G("TAR.GZ", "ar|tar.gz")],
               [G("TAR.BZ2", "ar|tar.bz2"), G("TAR.XZ", "ar|tar.xz")],
               [P("Cancel", "ar|cancel")]])


async def archive_url_job(c, uid, chat, urls, mode, archive_format, status):
    d = WORK / uuid.uuid4().hex
    job = Job(uid, status)
    progress_task = asyncio.create_task(progress_watch(job))
    try:
        if mode == "extract" and len(urls) != 1:
            raise RuntimeError("Ek baar me ek archive URL bhejo.")
        downloaded = []
        for i, url in enumerate(urls, 1):
            await set_stage(job, "download", f"Direct link {i}/{len(urls)}")
            downloaded.append(await download_direct(url, d / f"in{i:03d}", job))
        out = d / "out"
        out.mkdir(parents=True, exist_ok=True)
        if mode == "extract":
            await set_stage(job, "convert", "Archive extract ho raha hai")
            extracted = await asyncio.to_thread(_extract_archive, downloaded[0], out)
            for i, path in enumerate(extracted, 1):
                chk(job)
                await set_stage(job, "upload", f"Extracted file {i}/{len(extracted)}")
                await c.send_document(chat, str(path), caption=path.name,
                                      progress=prog_cb(job, i), **rkw(status.id))
        else:
            await set_stage(job, "convert", f"{archive_format.upper()} archive ban raha hai")
            job.total = sum(path.stat().st_size for path in downloaded)
            archive = await asyncio.to_thread(_create_archive, downloaded, out, archive_format)
            chk(job)
            await set_stage(job, "upload", archive.name)
            await c.send_document(chat, str(archive), caption=archive.name,
                                  progress=prog_cb(job), **rkw(status.id))
        await stop_progress_watch(progress_task)
        await status.edit(f"✅ {('Extraction' if mode == 'extract' else 'Archive creation')} complete.")
    except Exception as e:
        await fail(status, job, e)
    finally:
        await stop_progress_watch(progress_task)
        jobs.pop(job.id, None)
        shutil.rmtree(d, ignore_errors=True)


SOCIAL_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
    "instagram.com", "www.instagram.com", "instagr.am",
    "drive.google.com", "docs.google.com",
    "terabox.com", "www.terabox.com", "1024terabox.com", "www.1024terabox.com",
    "teraboxapp.com", "www.teraboxapp.com", "terabox.app", "www.terabox.app",
    "teraboxlink.com", "www.teraboxlink.com", "1024tera.com",
    "dubox.com", "www.dubox.com",
    "mirrobox.com", "www.mirrobox.com", "nephobox.com", "www.nephobox.com",
    "mediafire.com", "www.mediafire.com", "mega.nz", "www.mega.nz",
    "filestore.to", "www.filestore.to",
}
SOCIAL_MEDIA_EXTS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".flv",
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav",
}
YOUTUBE_CHOICES = {
    "best": "bestvideo*+bestaudio/best",
    "2160": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
    "1440": "bestvideo[height<=1440]+bestaudio/best[height<=1440]",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "360": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "mp3": "bestaudio/best",
}


def social_video_url(text):
    for match in re.finditer(r"https?://[^\s<>]+", text, re.IGNORECASE):
        candidate = match.group(0).rstrip(".,!?;:)]}")
        try:
            parsed = urlsplit(candidate)
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if (parsed.scheme.lower() == "https" and host in SOCIAL_HOSTS and
                parsed.path not in ("", "/") and not parsed.username and not parsed.password):
            return candidate
    return None


def youtube_quality_kb():
    return KB([[G("Best", "dl|best"), G("2160p", "dl|2160"), G("1440p", "dl|1440")],
               [G("1080p", "dl|1080"), G("720p", "dl|720"), G("480p", "dl|480")],
               [G("360p", "dl|360"), G("🎵 MP3", "dl|mp3")],
               [R("Cancel", "dl|cancel")]])


def _download_mega(url, destination, job):
    executable = os.getenv("MEGADL_EXECUTABLE") or shutil.which("megadl")
    if not executable:
        raise RuntimeError("MEGA links ke liye server par megatools ka `megadl` install karo.")
    destination.mkdir(parents=True, exist_ok=True)
    log_path = destination / ".megadl.log"
    log_file = log_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [executable, "--path", str(destination), url],
            stdout=subprocess.DEVNULL, stderr=log_file)
    except Exception:
        log_file.close()
        log_path.unlink(missing_ok=True)
        raise
    job.procs.add(process)
    try:
        while process.poll() is None:
            if job.cancelled:
                process.kill()
                process.wait()
                raise Cancelled()
            time.sleep(0.25)
        process.communicate()
        if process.returncode:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
            raise RuntimeError(f"MEGA download failed: {detail}")
    finally:
        job.procs.discard(process)
        log_file.close()
        log_path.unlink(missing_ok=True)
    files = [p for p in destination.iterdir() if p.is_file()]
    if not files:
        raise RuntimeError("MEGA se koi file download nahi hui.")
    path = max(files, key=lambda p: p.stat().st_size)
    if path.stat().st_size > MAX_SOCIAL_BYTES:
        raise RuntimeError(f"Media MAX_SOCIAL_BYTES limit ({human(MAX_SOCIAL_BYTES)}) se badi hai.")
    return path, path.name


def _download_social_video(url, destination, job, choice="best"):
    if choice not in YOUTUBE_CHOICES:
        raise ValueError("Download quality option valid nahi hai.")
    if (urlsplit(url).hostname or "").lower().rstrip(".") in {"mega.nz", "www.mega.nz"}:
        return _download_mega(url, destination, job)

    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadCancelled

    destination.mkdir(parents=True, exist_ok=True)

    def progress(data):
        if job.cancelled:
            raise DownloadCancelled()
        if data.get("status") not in ("downloading", "finished"):
            return
        job.cur = int(data.get("downloaded_bytes") or data.get("total_bytes") or 0)
        job.total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)

    options = {
        "format": YOUTUBE_CHOICES[choice],
        "merge_output_format": "mp4",
        "outtmpl": str(destination / "%(title).100B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "max_filesize": MAX_SOCIAL_BYTES,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress],
    }
    if choice == "mp3":
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    cookies = os.getenv("YTDLP_COOKIES")
    if cookies:
        if not Path(cookies).is_file():
            raise FileNotFoundError(f"YTDLP_COOKIES file nahi mili: {cookies}")
        options["cookiefile"] = cookies
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
    downloaded_files = [p for p in destination.iterdir()
                        if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    media = [p for p in downloaded_files if p.suffix.lower() in SOCIAL_MEDIA_EXTS]
    if not media:
        media = downloaded_files
    if not media:
        raise RuntimeError("Link se koi file download nahi hui.")
    path = max(media, key=lambda p: p.stat().st_size)
    if path.stat().st_size > MAX_SOCIAL_BYTES:
        raise RuntimeError(f"Media MAX_SOCIAL_BYTES limit ({human(MAX_SOCIAL_BYTES)}) se badi hai.")
    return path, info.get("title") or path.name


async def social_video_job(c, uid, chat, url, source_message, choice="best", status=None):
    d = WORK / uuid.uuid4().hex
    status = status or await source_message.reply("⏳ Link se media download starting...")
    job = Job(uid, status)
    progress_task = asyncio.create_task(progress_watch(job))
    channel_errors = []
    try:
        await set_stage(job, "queued")
        async with sem:
            await set_stage(job, "download", f"Link media ({choice})")
            path, title = await asyncio.to_thread(_download_social_video, url, d, job, choice)
            chk(job)
            await set_stage(job, "upload", title[:80])
            await send_out(c, chat, path, job=job, uid=uid, reply_to=source_message.id)
            for target in archive_targets():
                try:
                    await set_stage(job, "upload", f"Original → channel {target}")
                    await deliver_channel_copy(c, target, path, f"📥 Original: {path.name}", job, 0)
                except Exception as e:
                    if isinstance(e, Cancelled):
                        raise
                    logging.exception("Social video channel upload failed to %s", target)
                    channel_errors.append(f"{target}: {str(e)[-180:]}")
        user_settings = settings(uid)
        user_settings["jobs"] = user_settings.get("jobs", 0) + 1
        save_settings()
        if channel_errors:
            await status.edit("✅ Media deliver ho gaya.\n\n⚠️ Channel upload issue:\n" +
                              "\n".join(channel_errors), reply_markup=None)
        else:
            await status.delete()
    except Exception as e:
        await fail(status, job, e)
    finally:
        await stop_progress_watch(progress_task)
        jobs.pop(job.id, None)
        shutil.rmtree(d, ignore_errors=True)


async def request_archive_urls(c, m, mode, text=None):
    uid = m.from_user.id
    s = sessions.setdefault(uid, {})
    s["wait"] = "archive_extract_urls" if mode == "extract" else "archive_create_urls"
    prompt = ("📥 Archive ka direct URL bhejo (ZIP / 7Z / RAR / TAR)." if mode == "extract"
              else "📦 Ek ya multiple direct file URLs bhejo (har URL alag line par).")
    if text:
        try:
            urls = direct_urls(text)
        except (ValueError, OSError) as e:
            return await m.reply(f"❌ {e}")
        s["wait"] = None
        if mode == "extract":
            status = await m.reply("⏳ Archive extraction starting...")
            return asyncio.create_task(archive_url_job(c, uid, m.chat.id, urls, mode, None, status))
        s["archive_urls"] = urls
        return await m.reply("Archive format choose karo:", reply_markup=archive_format_kb())
    await m.reply(prompt + "\n\nCancel: /cancel")


async def act_rename(src, out, p):
    o = out / src.name
    shutil.move(str(src), str(o))
    return [o]


ACTIONS = {"hlsall": act_hls_all, "trim": act_trim, "split": act_split, "aud": act_aud, "opt": act_opt,
           "conv": act_conv, "hls": act_hls, "thumb": act_thumb, "shots": act_shots, "sample": act_sample,
           "noaud": act_noaud, "meta": act_meta, "smap": act_smap, "sext": act_sext, "mux": act_mux,
           "sub": act_sub, "zip": act_zip, "rename": act_rename}


def _merge_sig(streams):
    """Compact fingerprint of the streams that must match for a lossless (copy) concat."""
    sig = []
    for s in streams:
        t = s.get("codec_type")
        if t == "video" and not s.get("disposition", {}).get("attached_pic"):
            sig.append(("v", s.get("codec_name"), s.get("width"), s.get("height"),
                        s.get("pix_fmt"), s.get("r_frame_rate")))
        elif t == "audio":
            sig.append(("a", s.get("codec_name"), s.get("sample_rate"), s.get("channels")))
    return sig


def merge_mode(all_st):
    """'copy' if every file has identical video/audio layout (fast, lossless), else 'encode'."""
    sigs = [_merge_sig(st) for st in all_st]
    ok = bool(sigs[0]) and any(x[0] == "v" for x in sigs[0]) and all(x == sigs[0] for x in sigs)
    return "copy" if ok else "encode"


def _concat_path(p: Path) -> str:
    return p.resolve().as_posix().replace("'", "'\\''")


async def _merge_copy(srcs, out, name):
    lst = out / "list.txt"
    lst.write_text("".join(f"file '{_concat_path(s)}'\n" for s in srcs), encoding="utf-8")
    args = ["-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v", "-map", "0:a?", "-c", "copy"]
    try:
        o = out / f"{name}.mp4"
        await ff(*args, "-movflags", "+faststart", str(o))
    except RuntimeError:  # streams the mp4 container can't hold -> mkv
        o.unlink(missing_ok=True)
        o = out / f"{name}.mkv"
        await ff(*args, str(o))
    return o


async def _merge_encode(srcs, out, name, all_st, durs):
    """Re-encode everything to one common format. Works with any mix of codecs / sizes / fps,
    and videos without audio get silent audio so the join never fails."""
    vids = [(s.get("width") or 0, s.get("height") or 0) for st in all_st for s in st
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    W, H = max(vids, key=lambda x: x[0] * x[1]) if vids else (1280, 720)
    W, H = (W // 2 * 2) or 1280, (H // 2 * 2) or 720  # libx264 needs even sizes
    n = len(srcs)
    inputs = [x for s in srcs for x in ("-i", str(s))]
    vf, af, nxt = [], [], n
    for i, st in enumerate(all_st):
        vf.append(f"[{i}:v:0]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]")
        if any(s.get("codec_type") == "audio" for s in st):
            src_a = f"[{i}:a:0]"
        else:  # no audio track -> add silence of the same length
            inputs += ["-f", "lavfi", "-t", f"{max(durs[i], 0.1):.3f}", "-i",
                       "anullsrc=channel_layout=stereo:sample_rate=44100"]
            src_a = f"[{nxt}:a]"
            nxt += 1
        af.append(f"{src_a}aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
    cat = "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    o = out / f"{name}.mp4"
    await ff(*inputs, "-filter_complex", ";".join(vf + af + [cat]), "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(o))
    return o


async def act_merge(srcs, out, mode="encode", all_st=None, durs=None):
    name = f"{srcs[0].stem}_merged"
    all_st = all_st or [await get_streams(s) for s in srcs]
    durs = durs or [(await probe(s))["dur"] for s in srcs]
    job = CUR.get()
    if mode == "copy":
        try:
            return [await _merge_copy(srcs, out, name)]
        except RuntimeError:  # copy didn't work after all -> fall back to re-encode
            for f in out.glob(f"{name}.*"):
                f.unlink(missing_ok=True)
            if job:
                job.label = "Re-encoding"
    return [await _merge_encode(srcs, out, name, all_st, durs)]

# ---------------------------------------------------------------- caption / buttons / rename helpers
def parse_buttons(txt):
    """'Text - https://url | Text2 - https://url2'  (one line = one row)"""
    rows = []
    for line in txt.splitlines():
        row = []
        for part in line.split("|"):
            if " - " not in part:
                continue
            t, u = part.split(" - ", 1)
            t, u = t.strip(), u.strip()
            if t and re.match(r"^(https?://|tg://)\S+$", u):
                row.append([t, u])
        if row:
            rows.append(row)
    return rows[:8]


def url_kb(st):
    if not st.get("buttons"):
        return None
    return KB([[B(t, url=u) for t, u in row] for row in st["buttons"]])


def clean_name(txt):
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", txt).strip(" .")[:100]


def apply_newname(files, p):
    name = p.get("newname")
    if not name:
        return files
    out = []
    for i, f in enumerate(files, 1):
        base = name[:-len(f.suffix)] if f.suffix and name.lower().endswith(f.suffix.lower()) else name
        hls_match = re.search(r"_hls_(original(?:_\d+p)?|\d+p)\.zip$", f.name, re.IGNORECASE)
        if hls_match:
            suffix = f"_hls_{hls_match[1]}"
        else:
            suffix = f"_{i}" if len(files) > 1 else ""
        n = f.with_name(base + suffix + f.suffix)
        f.rename(n)
        out.append(n)
    return out

# ---------------------------------------------------------------- sending / jobs
async def send_out(c, chat, p: Path, cap=None, job=None, idx=0, uid=None, reply_to=None):
    """Upload with file name as caption (title) — or the user's custom caption/buttons.
    Sent as a reply to the original video. Uses the user's /addthumb thumbnail if set."""
    st = settings(uid) if uid else dict(DEF)
    e = p.suffix.lower()
    if cap is None:
        cap = st["caption"].replace("{filename}", p.name) if st.get("caption") else (p.name if e not in IMG else "")
    hls_match = re.search(r"_hls_(original(?:_\d+p)?|\d+p)\.zip$", p.name, re.IGNORECASE)
    if hls_match:
        quality = hls_match[1]
        if quality.lower().startswith("original"):
            resolution = quality.split("_", 1)[1] if "_" in quality else "resolution unavailable"
            quality = f"Original ({resolution})"
        details = f"🎞 Quality: {quality}\n📦 Archive size: {human(p.stat().st_size)}"
        cap = f"{cap[:900]}\n\n{details}".strip() if cap else details
    kw = rkw(reply_to)
    if job:
        kw["progress"] = prog_cb(job, idx)
    mk = url_kb(st)
    if mk:
        kw["reply_markup"] = mk
    ut = user_thumb(uid)
    tkw = {"thumb": ut} if ut else {}
    if not st["as_video"]:
        await c.send_document(chat, str(p), caption=cap, **tkw, **kw)
    elif e in (".mp4", ".m4v"):
        info = await probe(p)
        thumb = ut or await make_thumb(p, info["dur"])  # custom thumbnail, else the usual auto cover
        await c.send_video(chat, str(p), caption=cap, supports_streaming=True, duration=int(info["dur"]),
                           width=info["w"], height=info["h"], thumb=thumb, **kw)
    elif e in (".mp3", ".m4a", ".flac"):
        await c.send_audio(chat, str(p), caption=cap, **kw)
    elif e in IMG:
        await c.send_photo(chat, str(p), caption=cap, **kw)
    else:
        await c.send_document(chat, str(p), caption=cap, **tkw, **kw)
    chk(job)


async def deliver(c, chat, files, job, uid, parallel=False, reply_to=None):
    st = settings(uid)
    if len(files) > 1 and all(f.suffix.lower() in IMG for f in files):  # screenshots -> albums of 10
        await set_stage(job, "upload", f"{len(files)} photos")
        M = InputMediaPhoto if st["as_video"] else InputMediaDocument
        for i in range(0, len(files), 10):
            await c.send_media_group(chat, [M(str(f)) for f in files[i:i + 10]], **rkw(reply_to))
            chk(job)
    elif parallel and len(files) > 1:  # all uploads at the same time
        await set_stage(job, "upload", f"{len(files)} files parallel")
        res = await asyncio.gather(
            *[send_out(c, chat, f, job=job, idx=i, uid=uid, reply_to=reply_to) for i, f in enumerate(files)],
            return_exceptions=True)
        errs = [r for r in res if isinstance(r, BaseException)]
        if errs:
            raise next((e for e in errs if isinstance(e, Cancelled)), errs[0])
    else:
        for i, f in enumerate(files, 1):
            await set_stage(job, "upload", f"{i}/{len(files)}" if len(files) > 1 else "")
            await send_out(c, chat, f, job=job, uid=uid, reply_to=reply_to)


async def deliver_channel_copy(c, chat, path, caption, job, idx):
    await c.send_document(chat, str(path), caption=caption[:1000],
                          progress=prog_cb(job, idx))
    chk(job)


async def download(c, msg, d: Path, job=None, idx=0) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    kw = {"progress": prog_cb(job, idx)} if job else {}
    p = await c.download_media(msg, file_name=f"{d}/", **kw)
    if not p:  # stop_transmission() -> None
        raise Cancelled()
    return Path(p)


def drop_local(s):
    """Forget (and delete) a pre-downloaded file kept for the stream pickers."""
    loc = s.pop("local", None)
    s.pop("streams", None)
    s.pop("pick", None)
    if loc:
        shutil.rmtree(loc[1], ignore_errors=True)


async def fail(status, job, e):
    try:
        if isinstance(e, Cancelled) or job.cancelled:
            await status.edit("❌ Cancelled.")
        else:
            await status.edit(f"❌ Error:\n`{str(e)[-500:]}`")
    except Exception:
        pass


async def single_job(c, chat, msg, p: dict, status, local=None, uid=None, tag="", forward_chat=None) -> bool:
    d = local[1] if local else WORK / uuid.uuid4().hex
    uid = uid or (msg.from_user.id if msg.from_user else 0)
    reply_to = msg.id if msg.chat and msg.chat.id == chat else status.id
    targets = archive_targets(forward_chat)
    archive_errors = []
    job = Job(uid, status, dc_of(msg))
    job.tag = tag
    CUR.set(job)
    progress_task = asyncio.create_task(progress_watch(job))
    try:
        await set_stage(job, "queued")
        async with sem:
            src = local[0] if local else None
            coros = []
            if src is None:
                coros.append(download(c, msg, d / "in", job, 0))
            if p.get("extra"):
                coros.append(download(c, p["extra"], d / "in2", job, 1))
            if coros:
                await set_stage(job, "download")
                res = await asyncio.gather(*coros)
                if src is None:
                    src = res.pop(0)
                if res:
                    p = {**p, "extra_path": res[0]}
            for i, target in enumerate(targets, 1):
                try:
                    await set_stage(job, "upload", f"Original → channel {i}/{len(targets)}")
                    await deliver_channel_copy(c, target, src, f"📥 Original: {src.name}", job, i)
                except Exception as e:
                    if isinstance(e, Cancelled):
                        raise
                    logging.exception("Original channel upload failed to %s", target)
                    archive_errors.append(f"{target} (original): {str(e)[-180:]}")
            if p["act"] == "info":
                report = await media_info(src, msg.date)
                for target in targets:
                    try:
                        await c.send_message(target, f"ℹ️ Media Information\n\n{report}")
                    except Exception as e:
                        logging.exception("Media info post failed to %s", target)
                        archive_errors.append(f"{target} (info): {str(e)[-180:]}")
                text = report
                if archive_errors:
                    text += "\n\n⚠️ Channel upload issue:\n" + "\n".join(archive_errors)
                await status.edit(text)
                return not archive_errors
            info = await probe(src)
            out = d / "out"
            out.mkdir(parents=True, exist_ok=True)
            if WATERMARK and p["act"] in PRE_WM:
                await set_stage(job, "convert", "💧 Watermark")
                job.total = info["dur"]
                src = await add_wm(src, d / "wm", info)
            await set_stage(job, "convert")
            job.total = info["dur"]
            files = await ACTIONS[p["act"]](src, out, p)
            if WATERMARK and p["act"] in POST_WM:
                files = await wm_outputs(job, files, d / "wm")
            files = apply_newname(files, p)
            await deliver(c, chat, files, job, uid, parallel=p.get("parallel", False), reply_to=reply_to)
            for target in targets:
                for i, file in enumerate(files, 1):
                    try:
                        await set_stage(job, "upload", f"Processed → channel {i}/{len(files)}")
                        await deliver_channel_copy(c, target, file,
                                                   f"✅ Processed: {file.name}", job, i)
                    except Exception as e:
                        if isinstance(e, Cancelled):
                            raise
                        logging.exception("Processed output upload failed to %s", target)
                        archive_errors.append(f"{target} ({file.name}): {str(e)[-180:]}")
        s_ = settings(uid)
        s_["jobs"] = s_.get("jobs", 0) + 1
        save_settings()
        await stop_progress_watch(progress_task)
        if archive_errors:
            await status.edit("✅ Processing complete\n\n⚠️ Channel copy issue:\n" +
                              "\n".join(archive_errors), reply_markup=None)
        elif reply_to == status.id:
            await status.edit("✅ Done", reply_markup=None)
        else:
            await status.delete()
        return not archive_errors
    except Exception as e:
        await fail(status, job, e)
        return False
    finally:
        await stop_progress_watch(progress_task)
        jobs.pop(job.id, None)
        shutil.rmtree(d, ignore_errors=True)


async def prefetch(c, uid, chat, msg, status, kind):
    """Download the video first so the stream picker can show its real streams."""
    d = WORK / uuid.uuid4().hex
    job = Job(uid, status, dc_of(msg))
    CUR.set(job)
    s = sessions.setdefault(uid, {})
    try:
        await set_stage(job, "queued")
        async with sem:
            await set_stage(job, "download")
            src = await download(c, msg, d / "in", job)
        streams = await get_streams(src)
        if not streams:
            raise RuntimeError("Is file me koi stream nahi mili.")
        s["local"], s["streams"] = (src, d), streams
        s["pick"] = {"kind": kind, "streams": streams, "sel": []}
        await status.edit(pick_text(s["pick"]), reply_markup=pick_kb(s["pick"]))
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        await fail(status, job, e)
    finally:
        jobs.pop(job.id, None)


async def merge_job(c, chat, msgs, status, uid=None, forward_chat=None):
    d = WORK / uuid.uuid4().hex
    uid = uid or (msgs[0].from_user.id if msgs[0].from_user else 0)
    reply_to = msgs[0].id if msgs[0].chat and msgs[0].chat.id == chat else status.id
    targets = archive_targets(forward_chat)
    archive_errors = []
    job = Job(uid, status, dc_of(msgs[0]))
    progress_task = asyncio.create_task(progress_watch(job))
    CUR.set(job)
    try:
        await set_stage(job, "queued")
        async with sem:
            await set_stage(job, "download", f"{len(msgs)} videos")
            srcs = await asyncio.gather(*[download(c, m, d / f"in{i}", job, i) for i, m in enumerate(msgs)])
            for source_index, src in enumerate(srcs, 1):
                for target_index, target in enumerate(targets, 1):
                    try:
                        await set_stage(job, "upload",
                                        f"Original {source_index}/{len(srcs)} → channel {target_index}/{len(targets)}")
                        await deliver_channel_copy(c, target, src, f"📥 Original: {src.name}", job,
                                                   source_index * target_index)
                    except Exception as e:
                        if isinstance(e, Cancelled):
                            raise
                        logging.exception("Merge source upload failed to %s", target)
                        archive_errors.append(f"{target} ({src.name}): {str(e)[-180:]}")
            infos = [await probe(x) for x in srcs]
            all_st = [await get_streams(x) for x in srcs]
            mode = merge_mode(all_st)
            await set_stage(job, "convert",
                            "⚡ Fast merge (no quality loss)" if mode == "copy"
                            else "🔄 Videos ke formats alag hain, re-encode karke merge ho raha hai")
            job.total = sum(i["dur"] for i in infos)
            out = d / "out"
            out.mkdir(parents=True, exist_ok=True)
            files = await act_merge(srcs, out, mode, all_st, [i["dur"] for i in infos])
            if WATERMARK:
                files = await wm_outputs(job, files, d / "wm")
            for f in files:
                await set_stage(job, "upload")
                await send_out(c, chat, f, job=job, uid=uid, reply_to=reply_to)
            for f in files:
                for target in targets:
                    try:
                        await set_stage(job, "upload", f"Processed output → channel {target}")
                        await deliver_channel_copy(c, target, f, f"✅ Processed merge: {f.name}", job, 0)
                    except Exception as e:
                        if isinstance(e, Cancelled):
                            raise
                        logging.exception("Merged output upload failed to %s", target)
                        archive_errors.append(f"{target} ({f.name}): {str(e)[-180:]}")
        if archive_errors:
            await status.edit("✅ Merge complete\n\n⚠️ Channel copy issue:\n" +
                              "\n".join(archive_errors), reply_markup=None)
        elif reply_to == status.id:
            await status.edit("✅ Merge complete", reply_markup=None)
        else:
            await status.delete()
    except Exception as e:
        await fail(status, job, e)
    finally:
        await stop_progress_watch(progress_task)
        jobs.pop(job.id, None)
        shutil.rmtree(d, ignore_errors=True)


NO_RENAME = {"info"}


async def launch_bulk(c, uid, chat, p, status=None):
    """Run one action on every video of the bulk selection. Every video gets its own status message
    (reply to that video) with its own Progress / Cancel buttons."""
    s = sessions.get(uid, {})
    files = s.get("bulkfiles") or []
    forward_chat = s.get("forward_chat")
    drop_local(s)
    sessions.pop(uid, None)
    n = len(files)
    if not n:
        txt = "Session expire. /bulk dobara chalao."
        return await (status.edit(txt) if status else c.send_message(chat, txt))
    prog = {"ok": 0, "fail": 0}
    head = status or await c.send_message(chat, "⚙️ Bulk starting...")

    async def upd(done=False):
        t = ("✅ Bulk complete" if done else "⚙️ Bulk running") + \
            f": ✅ {prog['ok']} | ❌ {prog['fail']} / {n}  (parallel: {MAX_PARALLEL})"
        try:
            await head.edit(t)
        except Exception:
            pass

    bsem = asyncio.Semaphore(MAX_PARALLEL)

    async def one(i, m_):
        async with bsem:  # status message is created only when this video starts
            ok = False
            try:
                text = f"⏳ Video #{i}/{n}"
                st = await (m_.reply(text) if m_.chat.id == chat else c.send_message(chat, text))
                ok = await single_job(c, chat, m_, p, st, uid=uid, tag=f"#{i}/{n}",
                                      forward_chat=forward_chat)
            except Exception as e:
                logging.warning("bulk item %s failed: %s", i, e)
        prog["ok" if ok else "fail"] += 1
        await upd()

    async def runall():
        await upd()
        await asyncio.gather(*[one(i + 1, m_) for i, m_ in enumerate(files)])
        await upd(done=True)

    asyncio.create_task(runall())


async def resolve_forward_channel(c, target):
    try:
        chat = await c.get_chat(target)
        chat_type = str(chat.type).lower().split(".")[-1]
        if chat_type not in ("channel", "supergroup"):
            raise ValueError("Destination ek channel ya supergroup hona chahiye.")
        me = await c.get_me()
        member = await c.get_chat_member(chat.id, me.id)
        member_status = str(member.status).lower().split(".")[-1]
        if member_status not in ("administrator", "owner", "creator"):
            raise ValueError("Bot ko destination channel/group ka admin banao.")
        rights = getattr(member, "privileges", None)
        if chat_type == "channel" and rights and not getattr(rights, "can_post_messages", False):
            raise ValueError("Bot admin rights me channel par post karne ki permission nahi hai.")
        return chat
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Channel resolve nahi hua ya bot ko access nahi hai: {str(e)[-200:]}") from e


async def launch(c, uid, chat, p, status=None):
    """Start a job. Bulk selection -> launch_bulk. Single video -> asks for a new name first if 'Rename File' is ON."""
    s = sessions.setdefault(uid, {})
    if s.get("bulkfiles"):
        return await launch_bulk(c, uid, chat, p, status)
    msg = s.get("single")
    if not msg:
        txt = "Session expire. Video dobara bhejo."
        return await (status.edit(txt) if status else c.send_message(chat, txt))
    if settings(uid)["rename"] and p["act"] not in NO_RENAME and not p.get("newname"):
        s["wait"], s["pending"] = "newname", p
        txt = "✏️ **Rename File** ON hai — output ka naya naam bhejo (extension ke bina)."
        return await (status.edit(txt, reply_markup=back_kb()) if status else c.send_message(chat, txt))
    if status is None:
        status = await c.send_message(chat, "Starting...")
    asyncio.create_task(single_job(c, chat, msg, p, status, s.pop("local", None), uid=uid))

# ---------------------------------------------------------------- UI
def menu_text(uid):
    up = "🎥 **Video**" if settings(uid)["as_video"] else "📁 **File**"
    return (f"📤 Original + processed copies are also sent to `{PERMANENT_CHANNEL_ID}`.\n\n"
            "✶ If you want New Name in Output video, Then Activate ✅ **Rename File**.\n\n"
            f"✶ Your File will be Uploaded as {up}  (You can change it in /settings)\n\n"
            "**Please choose your desired action below** 👇")


def main_kb():
    return KB([
        [G("🖼️ Thumb/Cover Extractor", "a|thumb")],
        [G("✏️ Caption And Buttons Editor", "a|cap")],
        [G("🎥 Metadata Editor", "a|meta"), G("🎥 Stream Mapper", "a|smap")],
        [G("🎵 Stream Remover", "a|srem"), G("🎵 Stream Extractor", "a|sext")],
        [P("✂️ Video Trimmer", "a|trim"), G("➕ Video Merger", "a|merge")],
        [G("🔇 Remove Audio", "a|noaud"), G("Merge 🎥 And 🎵", "a|mux")],
        [G("🎵 Audio Converter", "a|aud"), G("🎥 Videos Splitter", "a|split")],
        [G("🖼️ Screenshots", "a|shots"), G("🖼️ Manual Shots", "a|mshots")],
        [G("🎥 Generate Sample", "a|sample"), P("🎵 Video To Audio", "a|toaud")],
        [G("🎥 Video Optimizer", "a|opt"), G("🎥 Subtitle Merger", "a|sub")],
        [P("🎥 Video Converter", "a|conv"), P("✏️ Video Renamer", "a|ren")],
        [P("Media Information", "a|info"), G("📂 Create Archive", "a|zip")],
        [G("🌐 Extract Archive URL", "a|url_extract"), G("🌐 Create Archive from URL", "a|url_create")],
        [G("📡 To M3U8 (HLS)", "a|hls")],
        [R("Cancel ❌", "a|cancel")],
    ])


def bulk_kb():
    """Offer the normal processing actions; parameter choices are reused for every selected video."""
    return KB([
        [G("🖼️ Thumb/Cover Extractor", "a|thumb")],
        [G("✏️ Caption And Buttons Editor", "a|cap"), G("🎥 Metadata Editor", "a|meta")],
        [G("🎥 Stream Mapper", "a|smap"), G("🎵 Stream Remover", "a|srem")],
        [G("🎵 Stream Extractor", "a|sext"), G("Merge 🎥 And 🎵", "a|mux")],
        [G("🎥 Subtitle Merger", "a|sub"), G("✏️ Video Renamer", "a|ren")],
        [P("✂️ Video Trimmer", "a|trim"), G("➕ Merge All Videos", "a|merge")],
        [G("🔇 Remove Audio", "a|noaud"), G("🎥 Videos Splitter", "a|split")],
        [G("🎵 Audio Converter", "a|aud"), P("🎵 Video To Audio", "a|toaud")],
        [G("🖼️ Screenshots", "a|shots"), G("🖼️ Manual Shots", "a|mshots")],
        [G("🎥 Generate Sample", "a|sample"), G("🎥 Video Optimizer", "a|opt")],
        [P("🎥 Video Converter", "a|conv"), P("Media Information", "a|info")],
        [G("📂 Create Archive", "a|zip"), G("📡 To M3U8 (HLS)", "a|hls")],
        [G("🌐 Extract Archive URL", "a|url_extract"), G("🌐 Create Archive from URL", "a|url_create")],
        [R("Cancel ❌", "a|cancel")],
    ])


def menu_view(uid, s):
    bf = s.get("bulkfiles")
    if bf:
        up = "🎥 **Video**" if settings(uid)["as_video"] else "📁 **File**"
        extra = f"📤 Extra channel → `{s['forward_chat']}`\n" if s.get("forward_chat") else ""
        return (f"📦 **Bulk:** {len(bf)} videos selected\n\n"
                f"✶ Files will be Uploaded as {up}  (/settings)\n\n"
                f"📤 Original + processed copies → `{PERMANENT_CHANNEL_ID}`\n"
                f"{extra}\n"
                "Stream options me first video ki selected stream indexes sab par apply honge.\n\n"
                "**Kya process karna hai?** 👇", bulk_kb())
    return menu_text(uid), main_kb()


def cap_text(uid):
    st = settings(uid)
    return ("✏️ **Caption And Buttons Editor**\n\n"
            f"Caption: {'✅ Custom set' if st['caption'] else 'Default (file name)'}\n"
            f"Buttons: {sum(len(r) for r in st['buttons'])} link(s)\n\n"
            "Ye saari uploads par lagega.")


def cap_kb():
    return KB([[G("✏️ Set Caption", "ce|cap"), G("🔘 Set Buttons", "ce|btn")],
               [R("♻️ Reset", "ce|reset")], [P("Main Menu", "m|menu")]])


def pick_text(pk):
    return {"smap": "🎥 **Stream Mapper**\nJo streams KEEP karni hain select karo (jis order me tap karoge usi order me output banega), phir ✅ Done.",
            "srem": "🎵 **Stream Remover**\nJo streams HATANI hain select karo, phir ✅ Done.",
            "sext": "🎵 **Stream Extractor**\nJis stream ko extract karna hai us par tap karo."}[pk["kind"]]


def pick_kb(pk):
    rows, sel, kind = [], pk["sel"], pk["kind"]
    for st in pk["streams"]:
        i = st["index"]
        if kind == "sext":
            rows.append([P(sdesc(st), f"st|{i}")])
        elif i in sel:
            rows.append([G(f"{'✅' + str(sel.index(i) + 1) if kind == 'smap' else '🗑'} {sdesc(st)}", f"st|{i}")])
        else:
            rows.append([P(f"⬜ {sdesc(st)}", f"st|{i}")])
    if kind != "sext":
        rows += [[G("✅ Done", "st|ok")], [P("Select All", "st|all"), P("Clear", "st|none")]]
    rows.append([R("Cancel ❌", "a|cancel")])
    return KB(rows)


def is_video(m):
    return bool(m.video or (m.document and (m.document.mime_type or "").startswith("video/")))


video_filter = filters.create(lambda _, __, m: is_video(m))

# ---------------------------------------------------------------- t.me message links (bulk range)
LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]{4,}))/(?:\d+/)?(\d+)")


def parse_links(txt):
    out = []
    for m in LINK_RE.finditer(txt or ""):
        chat = int("-100" + m[1]) if m[1] else m[2]
        out.append((chat, int(m[3])))
    return out


async def add_links(m, s, links):
    rg = s.setdefault("ranges", [])

    async def add(chat, a, b):
        a, b = min(a, b), max(a, b)
        if b - a + 1 > MAX_RANGE:
            return await m.reply(f"❌ Range bahut badi hai (max {MAX_RANGE} messages).")
        rg.append((chat, a, b))
        await m.reply(f"✅ Range added: {a} → {b}  (~{b - a + 1} messages).\nAur links bhejo, video bhejo, ya /done.")

    if len(links) >= 2:
        (c1, a), (c2, b) = links[0], links[-1]
        s.pop("pend", None)
        if str(c1).lower() != str(c2).lower():
            return await m.reply("❌ Dono links ek hi group/channel ke hone chahiye.")
        return await add(c1, a, b)
    c1, a = links[0]
    pd = s.pop("pend", None)
    if pd and str(pd[0]).lower() == str(c1).lower():
        return await add(c1, pd[1], a)
    if pd:  # different chat -> the earlier lone link was a single message
        rg.append((pd[0], pd[1], pd[1]))
    s["pend"] = (c1, a)
    await m.reply("✅ Start link mil gaya. Ab **end link** bhejo (ya /done = sirf yahi ek message).")


async def fetch_range(c, chat, a, b):
    """Videos between message ids a..b. Text / photos / other files are skipped."""
    vids, skipped = [], 0
    ids = list(range(a, b + 1))
    for i in range(0, len(ids), 100):
        got = await c.get_messages(chat, ids[i:i + 100])
        if not isinstance(got, list):
            got = [got]
        for x in got:
            if x and not getattr(x, "empty", False) and is_video(x):
                vids.append(x)
            else:
                skipped += 1
    return vids, skipped


def has_copyable_media(message):
    return any(getattr(message, name, None) for name in (
        "animation", "audio", "document", "photo", "video", "video_note", "voice", "sticker"))


async def copy_channel_range(c, source, start_id, end_id, destination, status):
    copied = skipped = failed = 0
    source_chat = await c.get_chat(source)
    if source_chat.id == destination:
        raise ValueError("Source aur destination channel alag hone chahiye.")
    bot_user = await c.get_me()
    await c.get_chat_member(source_chat.id, bot_user.id)

    first, last = sorted((start_id, end_id))
    await status.edit(f"⏳ Queue me hai. Is waqt ek hi channel-copy job chalega.\nRange: {first} → {last}")
    async with channel_copy_lock:
        await status.edit(f"📥 Copy shuru: `{first} → {last}`")
        for batch_start in range(first, last + 1, 100):
            ids = list(range(batch_start, min(batch_start + 100, last + 1)))
            messages = await c.get_messages(source_chat.id, ids)
            if not isinstance(messages, list):
                messages = [messages]
            by_id = {msg.id: msg for msg in messages if msg}
            for message_id in ids:
                message = by_id.get(message_id)
                if not message or getattr(message, "empty", False) or not has_copyable_media(message):
                    skipped += 1
                    continue
                if getattr(message, "has_protected_content", False):
                    skipped += 1
                    continue
                try:
                    await asyncio.wait_for(c.copy_message(destination, source_chat.id, message_id), timeout=45)
                except asyncio.TimeoutError:
                    failed += 1
                    logging.warning("Timed out copying channel message %s:%s", source_chat.id, message_id)
                    continue
                except Exception as e:
                    if e.__class__.__name__ == "FloodWait":
                        wait = max(1, int(getattr(e, "value", 1)))
                        logging.warning("Telegram requested FloodWait of %s seconds", wait)
                        await asyncio.sleep(wait)
                        try:
                            await asyncio.wait_for(
                                c.copy_message(destination, source_chat.id, message_id), timeout=45)
                        except Exception:
                            failed += 1
                            logging.exception("Retry after FloodWait failed for message %s", message_id)
                            continue
                    elif e.__class__.__name__ in ("MessageIdInvalid", "MessageEmpty", "MessageNotFound"):
                        skipped += 1
                        continue
                    else:
                        failed += 1
                        logging.warning("Could not copy message %s:%s: %s",
                                        source_chat.id, message_id, e)
                        continue
                copied += 1
                await asyncio.sleep(CHANNEL_COPY_GAP)
                if copied % 10 == 0:
                    await status.edit(
                        f"📥 Channel copy running\n"
                        f"✅ Copied: {copied} | ⏭ Skipped: {skipped} | ⚠️ Failed: {failed}\n"
                        f"Last message: {message_id}/{last}")
    await status.edit(
        f"✅ Channel copy complete\n"
        f"✅ Copied: {copied}\n⏭ Skipped (deleted/text/protected): {skipped}\n"
        f"⚠️ Failed: {failed}")


async def run_channel_copy(c, source, start_id, end_id, destination, status):
    try:
        await copy_channel_range(c, source, start_id, end_id, destination, status)
    except Exception as e:
        logging.exception("Channel-copy job failed")
        try:
            await status.edit(f"❌ Channel copy stopped:\n`{str(e)[-400:]}`")
        except Exception:
            logging.exception("Could not report channel-copy job failure")


@app.on_message(filters.private & allowed & filters.command("channelcopy"))
async def channelcopy_cmd(c, m):
    channel_copy_requests[m.from_user.id] = {"step": "source"}
    await m.reply(
        "📦 **Channel media copy**\n\n"
        "Start aur end message link bhejo (ek message me dono ya alag-alag):\n"
        "`https://t.me/channel/119` `https://t.me/channel/220`\n\n"
        "Private source ke liye bot ko pehle channel me add karein; bot khud join nahi karega. "
        "Phir destination channel ID / `@username` bhejein. Bot destination me admin hona chahiye.\n"
        f"Ek range max {MAX_RANGE} messages hai. Deleted/non-media/protected messages skip honge; "
        f"copies ke beech kam se kam {CHANNEL_COPY_GAP:g}s gap rahega.\n"
        "User-account login ya protected-content bypass nahi hota. Cancel setup: /copycancel")


@app.on_message(filters.private & allowed & filters.command("copycancel"))
async def channelcopy_cancel_cmd(c, m):
    if channel_copy_requests.pop(m.from_user.id, None) is None:
        return await m.reply("Koi channel-copy setup pending nahi hai.")
    await m.reply("Pending channel-copy setup cancel ho gaya. Running/queued copy ko ye command stop nahi karta.")


@app.on_message(filters.private & allowed & (filters.video | filters.document) & video_filter)
async def on_video(c, m):
    s = sessions.setdefault(m.from_user.id, {})
    if s.get("mode") in ("bulk", "merge"):
        s["files"].append(m)
        return await m.reply(f"✅ Added #{len(s['files'])}  (/done jab ho jaye)")
    drop_local(s)
    s.clear()
    s["single"] = m
    await m.reply(menu_text(m.from_user.id), reply_markup=main_kb())


@app.on_message(filters.private & allowed & filters.photo)
async def on_photo(c, m):
    s = sessions.setdefault(m.from_user.id, {})
    if s.get("wait") == "addthumb":
        s["wait"] = None
        return await save_thumb_safe(c, m, m)
    await m.reply("Photo ko thumbnail banane ke liye pehle /addthumb bhejo.")


@app.on_message(filters.private & allowed & (filters.audio | filters.voice | filters.document))
async def on_extra(c, m):
    """Second file for 'Merge 🎥 And 🎵' (audio) and 'Subtitle Merger' (srt/ass/vtt); also image-as-file for /addthumb."""
    uid = m.from_user.id
    w = sessions.get(uid, {}).get("wait")
    if w == "addthumb":
        if not is_img(m):
            return await m.reply("❌ Photo bhejo (jpg / png).")
        sessions[uid]["wait"] = None
        return await save_thumb_safe(c, m, m)
    if w not in ("mux", "sub"):
        return await m.reply("Pehle video bhejo, phir action choose karo.")
    if w == "sub":
        name = (m.document.file_name if m.document else "") or ""
        if not name.lower().endswith((".srt", ".ass", ".ssa", ".vtt")):
            return await m.reply("❌ Subtitle file bhejo (.srt / .ass / .ssa / .vtt)")
    elif not (m.audio or m.voice or (m.document and (m.document.mime_type or "").startswith("audio/"))):
        return await m.reply("❌ Audio file bhejo (mp3 / m4a / aac ...)")
    sessions[uid]["wait"] = None
    await launch(c, uid, m.chat.id, {"act": w, "extra": m})


@app.on_message(filters.private & allowed & filters.command("bulk"))
async def bulk_cmd(c, m):
    drop_local(sessions.setdefault(m.from_user.id, {}))
    sessions[m.from_user.id] = {"mode": "bulk", "files": []}
    await m.reply("📦 **Bulk mode ON**\n\n"
                  "• Videos seedha bhejo, ya\n"
                  "• Group/channel ke message links bhejo — start aur end:\n"
                  "`https://t.me/channel/119`  `https://t.me/channel/220`\n"
                  "(ek message me dono link, ya alag alag). Beech ke text/photo/file skip ho jayenge, sirf videos aayengi.\n\n"
                  f"Original + processed copies permanent channel `{PERMANENT_CHANNEL_ID}` me bhi jayengi.\n"
                  "Optional: `/forward -1001234567890` se is bulk ke liye extra channel set karo.\n"
                  "Bot ko destination channels me post permission wala admin banao.\n"
                  "Phir /done. Source group/channel me bot ko member hona chahiye aur messages dekhne ki access honi chahiye.")


@app.on_message(filters.private & allowed & filters.command("forward"))
async def forward_cmd(c, m):
    s = sessions.get(m.from_user.id)
    if not s or s.get("mode") != "bulk":
        return await m.reply("Pehle `/bulk` chalao, videos/links add karo, phir `/forward <channel_id>` bhejo.")
    if len(m.command) != 2:
        return await m.reply("Format: `/forward -1001234567890` ya `/forward @channelusername`")
    raw_target = m.command[1].strip()
    target = int(raw_target) if raw_target.lstrip("-").isdigit() else raw_target
    try:
        channel = await resolve_forward_channel(c, target)
    except ValueError as e:
        return await m.reply(f"❌ {e}")
    s["forward_chat"] = channel.id
    await m.reply(f"✅ Is bulk ke liye extra destination set: **{channel.title}** (`{channel.id}`).\n"
                  f"Original + processed files permanent channel `{PERMANENT_CHANNEL_ID}` ke saath yahan bhi jayengi.")


@app.on_message(filters.private & allowed & filters.command("merge"))
async def merge_cmd(c, m):
    drop_local(sessions.setdefault(m.from_user.id, {}))
    sessions[m.from_user.id] = {"mode": "merge", "files": []}
    await m.reply("🔗 Merge mode ON. Videos order me bhejo, phir /done.")


@app.on_message(filters.private & allowed & filters.command(["extracturl", "makearchive"]))
async def archive_url_cmd(c, m):
    mode = "extract" if m.command[0].lower() == "extracturl" else "create"
    drop_local(sessions.setdefault(m.from_user.id, {}))
    sessions[m.from_user.id] = {}
    text = m.text.split(None, 1)[1] if len(m.command) > 1 else None
    return await request_archive_urls(c, m, mode, text)


@app.on_message(filters.private & allowed & filters.command("cancel"))
async def cancel_cmd(c, m):
    social_requests.pop(m.from_user.id, None)
    drop_local(sessions.get(m.from_user.id, {}))
    sessions.pop(m.from_user.id, None)
    await m.reply("Cancelled ✅")


@app.on_message(filters.private & allowed & filters.command("done"))
async def done_cmd(c, m):
    uid = m.from_user.id
    s = sessions.get(uid)
    if not s or s.get("mode") not in ("bulk", "merge") or not (s.get("files") or s.get("ranges") or s.get("pend")):
        return await m.reply("Pehle /bulk ya /merge chalao aur files / links bhejo.")
    if s["mode"] == "merge":
        if len(s["files"]) < 2:
            return await m.reply("Kam se kam 2 videos chahiye.")
        files = s.pop("files")
        forward_chat = s.get("forward_chat")
        sessions.pop(uid, None)
        st = await m.reply("Starting...")
        return asyncio.create_task(merge_job(c, m.chat.id, files, st, uid=uid,
                                             forward_chat=forward_chat))

    # ---- bulk: resolve link ranges into video messages, then show the full action menu
    if s.get("pend"):
        c1, a = s.pop("pend")
        s.setdefault("ranges", []).append((c1, a, a))
    st, skipped, errs = None, 0, []
    if s.get("ranges"):
        st = await m.reply("🔎 Links se videos dhoond raha hu...")
        for chat_, a, b in s["ranges"]:
            try:
                v, sk = await fetch_range(c, chat_, a, b)
                s["files"].extend(v)
                skipped += sk
            except Exception as e:
                errs.append(f"{chat_}: {str(e)[:150]}")
        s["ranges"] = []
    if not s["files"]:
        txt = "❌ Koi video nahi mila." + ("\n" + "\n".join(errs) if errs else "")
        txt += "\nBot ko source group/channel me add karke message access verify karo." if errs else ""
        sessions.pop(uid, None)
        return await (st.edit(txt) if st else m.reply(txt))
    files = s.pop("files")
    s.pop("mode", None)
    s["bulkfiles"], s["single"] = files, files[0]
    note = f"✅ {len(files)} videos mil gayi"
    if s.get("forward_chat"):
        note += f"\n📤 Extra channel: `{s['forward_chat']}`"
    if skipped:
        note += f"  ({skipped} text/file/non-video skip)"
    if errs:
        note += "\n⚠️ " + "\n⚠️ ".join(errs)
    text, kb = menu_view(uid, s)
    if st:
        await st.edit(note + "\n\n" + text, reply_markup=kb)
    else:
        await m.reply(note + "\n\n" + text, reply_markup=kb)


@app.on_message(filters.private & allowed & filters.text, group=1)
async def on_text(c, m):
    uid = m.from_user.id
    copy_request = channel_copy_requests.get(uid)
    if copy_request and not m.text.startswith("/"):
        if copy_request["step"] == "source":
            links = parse_links(m.text)
            if not links:
                return await m.reply("❌ Source ke valid `t.me` message link bhejo.")
            if len(links) >= 2:
                (source, first), (source2, last) = links[0], links[-1]
                if str(source).lower() != str(source2).lower():
                    return await m.reply("❌ Start/end links ek hi source channel ke hone chahiye.")
                first, last = min(first, last), max(first, last)
                if last - first + 1 > MAX_RANGE:
                    return await m.reply(f"❌ Range zyada badi hai (max {MAX_RANGE} messages).")
                copy_request.update(step="destination", source=source, first=first, last=last)
                return await m.reply("✅ Range set.\nAb destination channel ka `-100...` ID ya `@username` bhejo.")
            source, message_id = links[0]
            if copy_request["step"] == "source_end":
                if str(source).lower() != str(copy_request["source"]).lower():
                    return await m.reply("❌ End link start wale source channel ka hona chahiye.")
                first, last = sorted((copy_request["first"], message_id))
                if last - first + 1 > MAX_RANGE:
                    return await m.reply(f"❌ Range zyada badi hai (max {MAX_RANGE} messages).")
                copy_request.update(step="destination", first=first, last=last)
                return await m.reply("✅ Range set.\nAb destination channel ka `-100...` ID ya `@username` bhejo.")
            copy_request.update(step="source_end", source=source, first=message_id)
            return await m.reply("✅ Start link mila. Ab end message link bhejo.")

        target_text = m.text.strip()
        target_link = re.fullmatch(r"https?://t\.me/([A-Za-z0-9_]{4,})/?", target_text, re.IGNORECASE)
        if target_link:
            target_text = "@" + target_link.group(1)
        target = int(target_text) if target_text.lstrip("-").isdigit() else target_text.lstrip("@")
        try:
            source_chat = await c.get_chat(copy_request["source"])
            await c.get_chat_member(source_chat.id, (await c.get_me()).id)
            destination = await resolve_forward_channel(c, target)
            if source_chat.id == destination.id:
                return await m.reply("❌ Source aur destination channel alag hone chahiye.")
        except Exception as e:
            return await m.reply(f"❌ Channel access/permission check fail: {str(e)[-250:]}")
        request = channel_copy_requests.pop(uid)
        status = await m.reply("⏳ Channel-copy job queue me add hua.")
        asyncio.create_task(run_channel_copy(
            c, source_chat.id, request["first"], request["last"], destination.id, status))
        return

    s = sessions.get(uid)
    if s and s.get("wait") in ("archive_extract_urls", "archive_create_urls") and not m.text.startswith("/"):
        mode = "extract" if s["wait"] == "archive_extract_urls" else "create"
        try:
            urls = direct_urls(m.text)
        except (ValueError, OSError) as e:
            return await m.reply(f"❌ {e}")
        s["wait"] = None
        if mode == "extract":
            status = await m.reply("⏳ Archive extraction starting...")
            return asyncio.create_task(archive_url_job(c, uid, m.chat.id, urls, mode, None, status))
        s["archive_urls"] = urls
        return await m.reply("Archive format choose karo:", reply_markup=archive_format_kb())
    if s and s.get("mode") == "bulk" and not m.text.startswith("/"):
        links = parse_links(m.text)
        if links:
            return await add_links(m, s, links)
    if (not s or not s.get("wait")) and not m.text.startswith("/"):
        url = social_video_url(m.text)
        if url:
            host = (urlsplit(url).hostname or "").lower().rstrip(".")
            if host in {"youtube.com", "www.youtube.com", "m.youtube.com",
                        "music.youtube.com", "youtu.be"}:
                status = await m.reply(
                    "🎞 YouTube quality ya audio format choose karo:",
                    reply_markup=youtube_quality_kb())
                social_requests[uid] = {"url": url, "source": m, "status": status}
                return
            return asyncio.create_task(social_video_job(c, uid, m.chat.id, url, m))
    if not s or not s.get("wait") or m.text.startswith("/"):
        return
    w, txt = s["wait"], m.text.strip()

    if w == "caption":
        settings(uid)["caption"] = m.text.markdown[:1000]
        save_settings()
        s["wait"] = None
        return await m.reply("✅ Caption save ho gaya.\n\n" + cap_text(uid), reply_markup=cap_kb())
    if w == "buttons":
        rows = parse_buttons(txt)
        if not rows:
            return await m.reply("❌ Format galat hai.\n`Text - https://link`  (ek line = ek row, same row me `|` se alag)")
        settings(uid)["buttons"] = rows
        save_settings()
        s["wait"] = None
        return await m.reply("✅ Buttons save ho gaye.\n\n" + cap_text(uid), reply_markup=cap_kb())

    if w == "newname":
        p, name = s.pop("pending", None), clean_name(txt)
        if not p or not name:
            return await m.reply("❌ Valid naam bhejo.")
        p = {**p, "newname": name}
    elif w == "ren":
        name = clean_name(txt)
        if not name:
            return await m.reply("❌ Valid naam bhejo.")
        p = {"act": "rename", "newname": name}
    elif w == "trim":
        parts = txt.split()
        if len(parts) != 2 or not all(TIME_RE.match(x) for x in parts):
            return await m.reply("Format: `start end`  e.g. `00:01:00 00:02:30`")
        p = {"act": "trim", "start": parts[0], "end": parts[1]}
    elif w == "split":
        secs = parse_duration(txt)
        if not secs:
            return await m.reply("Format: `600` (sec) ya `10m` / `30s` / `1h`")
        p = {"act": "split", "secs": secs}
    elif w == "meta":
        meta = {k.lower(): v.strip() for k, v in re.findall(r"^\s*([A-Za-z_]{2,30})\s*[=:]\s*(.+?)\s*$", txt, re.M)}
        if not meta:
            return await m.reply("Format: `title=My Video`\n`artist=Name`\n`comment=Hello`  (ek line = ek field)")
        p = {"act": "meta", "meta": meta}
    elif w == "mshots":
        times = [t for t in re.split(r"[,\s]+", txt) if t]
        if not times or len(times) > 20 or not all(TIME_RE.match(t) for t in times):
            return await m.reply("Format: `00:00:10 00:01:30 125`  (max 20 times)")
        p = {"act": "shots", "times": times}
    elif w == "thumbtime":
        if not TIME_RE.match(txt):
            return await m.reply("Format: `00:01:30` ya seconds `90`")
        p = {"act": "thumb", "t": txt}
    else:
        return
    s["wait"] = None
    await launch(c, uid, m.chat.id, p)


@app.on_callback_query(filters.regex(r"^(pg|cx)\|") & allowed)
async def job_cb(c, q):
    act, jid = q.data.split("|", 1)
    job = jobs.get(jid)
    if not job or job.uid != q.from_user.id:
        return await q.answer("Ye job khatam ho chuka hai ya expire ho gaya.", show_alert=True)
    if act == "pg":
        await q.answer(render(job), show_alert=True)  # popup like the reference bot
    else:
        job.cancel()
        await q.answer("Cancelling...")


async def pick_cb(c, q, s, k):
    pk = s.get("pick")
    if not pk:
        return await q.answer("Session expire. Video dobara bhejo.", show_alert=True)
    uid, chat = q.from_user.id, q.message.chat.id
    all_idx = [x["index"] for x in pk["streams"]]
    kind, sel = pk["kind"], pk["sel"]
    if k[1] == "ok":
        maps = sel if kind == "smap" else [i for i in all_idx if i not in sel]
        if not maps:
            return await q.answer("Kam se kam 1 stream chahiye.", show_alert=True)
        await q.answer()
        return await launch(c, uid, chat, {"act": "smap", "maps": maps,
                                           "tag": "_mapped" if kind == "smap" else "_removed"}, q.message)
    await q.answer()
    if k[1] == "all":
        pk["sel"] = list(all_idx)
    elif k[1] == "none":
        pk["sel"] = []
    else:
        i = int(k[1])
        if kind == "sext":
            st = next(x for x in pk["streams"] if x["index"] == i)
            return await launch(c, uid, chat, {"act": "sext", "stream": st}, q.message)
        sel.remove(i) if i in sel else sel.append(i)
    try:
        await q.message.edit_reply_markup(pick_kb(pk))
    except Exception:
        pass


@app.on_callback_query(allowed)
async def cb(c, q):
    uid, chat = q.from_user.id, q.message.chat.id
    s = sessions.setdefault(uid, {})
    k = q.data.split("|")
    if k[0] == "st":
        return await pick_cb(c, q, s, k)
    await q.answer()

    if k[0] == "dl":
        request = social_requests.pop(uid, None)
        if k[1] == "cancel":
            if request:
                await q.message.edit("YouTube download cancelled.")
            return
        if not request or k[1] not in YOUTUBE_CHOICES:
            return await q.message.edit("Download choice expire ho gaya. Link dobara bhejo.")
        await q.message.edit("⏳ YouTube download starting...")
        return asyncio.create_task(social_video_job(
            c, uid, chat, request["url"], request["source"], k[1], q.message))

    if k[0] == "a" and k[1] in ("url_extract", "url_create"):
        mode = "extract" if k[1] == "url_extract" else "create"
        s["wait"] = "archive_extract_urls" if mode == "extract" else "archive_create_urls"
        s.pop("archive_urls", None)
        prompt = ("📥 Archive ka direct URL bhejo (ZIP / 7Z / RAR / TAR)." if mode == "extract"
                  else "📦 Ek ya multiple direct file URLs bhejo (har URL alag line par).")
        kb = back_kb() if s.get("single") else KB([[P("Archive Tools", "s|archive")]])
        return await q.message.edit(prompt + "\n\nCancel: /cancel", reply_markup=kb)
    if k[0] == "ar":
        if k[1] == "cancel":
            s.pop("wait", None)
            s.pop("archive_urls", None)
            return await q.message.edit("Archive operation cancelled.")
        urls = s.pop("archive_urls", None)
        if not urls:
            return await q.message.edit("Session expire. `/makearchive` dobara chalao.")
        s.pop("wait", None)
        await q.message.edit("⏳ Archive creation starting...")
        return asyncio.create_task(archive_url_job(c, uid, chat, urls, "create", k[1], q.message))

    msg = s.get("single")
    if not msg:
        return await q.message.edit("Session expire. Video dobara bhejo.")

    async def ed(text, kb=None):
        await q.message.edit(text, reply_markup=kb)

    async def go(p):
        await launch(c, uid, chat, p, q.message)

    if k[0] == "m":  # Main Menu
        s["wait"] = None
        s.pop("pending", None)
        t, kb = menu_view(uid, s)
        await ed(t, kb)

    elif k[0] == "a":
        a = k[1]
        s["wait"] = None
        s.pop("pending", None)
        if a == "cancel":
            drop_local(s)
            sessions.pop(uid, None)
            return await q.message.delete()
        if a == "trim":
            s["wait"] = "trim"
            await ed("✂️ `start end` bhejo\nExample: `00:01:00 00:02:30`", back_kb())
        elif a == "split":
            s["wait"] = "split"
            await ed("🪓 Har part ki duration bhejo\nExample: `600` ya `10m`", back_kb())
        elif a == "merge":
            if s.get("bulkfiles"):  # bulk menu -> merge ALL selected videos in order
                files = s["bulkfiles"]
                if len(files) < 2:
                    return await ed("Kam se kam 2 videos chahiye.", bulk_kb())
                forward_chat = s.get("forward_chat")
                sessions.pop(uid, None)
                await q.message.edit("Starting...")
                return asyncio.create_task(merge_job(c, chat, files, q.message, uid=uid,
                                                     forward_chat=forward_chat))
            drop_local(s)
            sessions[uid] = {"mode": "merge", "files": [msg]}
            await ed("🔗 Is video ko add kar liya. Aur videos bhejo, phir /done.")
        elif a == "aud":
            await ed("Audio format:", grid([(f.upper(), f"af|{f}") for f in AUDIO]))
        elif a == "toaud":
            await go({"act": "aud", "fmt": "mp3", "q": "192"})
        elif a == "opt":
            await ed("Compression level:", grid([("Light", "o|24"), ("Medium", "o|28"), ("Strong", "o|32")]))
        elif a == "conv":
            await ed("Convert to:", grid(
                [("MP4", "c|mp4"), ("MKV", "c|mkv"), ("AVI", "c|avi"), ("M4V", "c|m4v"), ("Stream", "c|stream")]))
        elif a == "hls":
            await ed("M3U8 quality:", grid(
                [("Original", "h|orig"), ("360p", "h|360"), ("480p", "h|480"), ("720p", "h|720"), ("1080p", "h|1080"),
                 ("🎯 All Quality", "h|all")]))
        elif a == "info":
            await go({"act": "info"})
        elif a == "zip":
            await go({"act": "zip"})
        elif a == "noaud":
            await go({"act": "noaud"})
        elif a == "thumb":
            s["wait"] = "thumbtime"
            await ed("🖼️ **Thumb/Cover Extractor**\n\nKis position ka frame chahiye? Button dabao ya time bhejo "
                     "(e.g. `00:01:30`).",
                     grid([("🎯 Auto (Middle)", "t|auto"), ("10%", "t|p10"), ("25%", "t|p25"),
                           ("50%", "t|p50"), ("75%", "t|p75"), ("90%", "t|p90")]))
        elif a == "shots":
            await ed("🖼️ **Screenshots** — kitne chahiye?", grid([(str(n), f"n|{n}") for n in (3, 5, 8, 10, 15, 20)]))
        elif a == "mshots":
            s["wait"] = "mshots"
            await ed("🖼️ **Manual Shots**\nTimes bhejo (space se alag):\n`00:00:10 00:01:30 125`\n(max 20)", back_kb())
        elif a == "sample":
            await ed("🎥 **Generate Sample** — kitne time ka?",
                     grid([("30s", "sm|30"), ("60s", "sm|60"), ("90s", "sm|90"), ("2 min", "sm|120"), ("3 min", "sm|180")]))
        elif a == "meta":
            s["wait"] = "meta"
            await ed("🎥 **Metadata Editor**\nFields bhejo (ek line = ek field):\n`title=My Video`\n`artist=Name`\n"
                     "`comment=Hello`\n`year=2026`", back_kb())
        elif a == "cap":
            await ed(cap_text(uid), cap_kb())
        elif a == "mux":
            s["wait"] = "mux"
            await ed("🎵 Ab **audio file** bhejo jo video ke saath merge karni hai.\n(Video ki original audio replace ho jayegi.)", back_kb())
        elif a == "sub":
            s["wait"] = "sub"
            await ed("🎥 **Subtitle Merger**\nAb subtitle file bhejo (`.srt` / `.ass` / `.ssa` / `.vtt`). Output `.mkv` hoga.", back_kb())
        elif a == "ren":
            s["wait"] = "ren"
            await ed("✏️ **Video Renamer**\nNaya naam bhejo (extension ke bina).", back_kb())
        elif a in ("smap", "srem", "sext"):
            loc = s.get("local")
            if loc and Path(loc[0]).exists() and s.get("streams"):  # already downloaded -> no re-download
                s["pick"] = {"kind": a, "streams": s["streams"], "sel": []}
                await ed(pick_text(s["pick"]), pick_kb(s["pick"]))
            else:
                drop_local(s)
                asyncio.create_task(prefetch(c, uid, chat, msg, q.message, a))

    elif k[0] == "ce":  # caption & buttons editor
        if k[1] == "cap":
            s["wait"] = "caption"
            await ed("✏️ Naya caption bhejo.\n`{filename}` likhoge to file ka naam aa jayega.",
                     KB([[P("Back", "a|cap")]]))
        elif k[1] == "btn":
            s["wait"] = "buttons"
            await ed("🔘 Buttons bhejo (ek line = ek row, same row me `|` se alag):\n"
                     "`Channel - https://t.me/xyz`\n`Site - https://a.com | Help - https://b.com`",
                     KB([[P("Back", "a|cap")]]))
        elif k[1] == "reset":
            st = settings(uid)
            st["caption"], st["buttons"] = None, []
            save_settings()
            await ed(cap_text(uid), cap_kb())

    elif k[0] == "t":
        s["wait"] = None
        await go({"act": "thumb", "t": k[1]})
    elif k[0] == "n":
        await go({"act": "shots", "n": int(k[1])})
    elif k[0] == "sm":
        await go({"act": "sample", "secs": int(k[1])})
    elif k[0] == "af":
        if not AUDIO[k[1]][1]:
            return await go({"act": "aud", "fmt": k[1]})
        await ed("Quality (bitrate):", grid([(f"{b}k", f"aq|{k[1]}|{b}") for b in (64, 96, 128, 192, 256, 320)]))
    elif k[0] == "aq":
        await go({"act": "aud", "fmt": k[1], "q": k[2]})
    elif k[0] == "o":
        await go({"act": "opt", "crf": int(k[1])})
    elif k[0] == "c":
        await go({"act": "conv", "fmt": k[1]})
    elif k[0] == "h":
        await go({"act": "hlsall", "parallel": True} if k[1] == "all" else {"act": "hls", "q": k[1]})


@app.on_message(filters.private & ~allowed)
async def denied(c, m):
    await m.reply(f"⛔ Permission nahi hai. Owner se /grant karwao.\nYour ID: `{m.from_user.id}`")


async def main():
    try:
        await initialize_user_store()
        await app.start()
        me = await app.get_me()
        print(f"\n✅ Bot chalu hai: @{me.username}  |  Owner ID: {OWNER_ID}", flush=True)
        print(f"👉 Telegram me @{me.username} ko /start bhejo. Band karne ke liye Ctrl+C.\n", flush=True)
        await idle()
    finally:
        if app.is_connected:
            await app.stop()
        if mongo_client is not None:
            await mongo_client.close()


if __name__ == "__main__":
    WORK.mkdir(exist_ok=True)
    THUMBS.mkdir(exist_ok=True)
    print("⏳ Telegram se connect ho raha hai...", flush=True)
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass