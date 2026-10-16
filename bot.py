import asyncio, contextvars, json, logging, os, re, shutil, time, uuid, zipfile
from pathlib import Path

from pyrogram import Client, enums, filters, idle
from pyrogram.file_id import FileId
from pyrogram.types import (InlineKeyboardButton as B, InlineKeyboardMarkup as KB,
                            InputMediaDocument, InputMediaPhoto)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))  # parallel jobs
SUPPORT_GROUP = os.getenv("SUPPORT_GROUP", "")  # e.g. @MyHelpGroup (optional)
OTHER_BOTS = os.getenv("OTHER_BOTS", "")  # optional: "Name|https://t.me/bot,Name2|https://t.me/bot2"

WORK = Path("downloads")
USERS_FILE = Path("users.json")
SETTINGS_FILE = Path("settings.json")
sem = asyncio.Semaphore(MAX_PARALLEL)
sessions: dict[int, dict] = {}

app = Client("videobot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------------------------------------------------------------- permissions
users: dict[str, str] = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else {}


def save_users():
    USERS_FILE.write_text(json.dumps(users))


def is_allowed(uid: int) -> bool:
    return uid == OWNER_ID or str(uid) in users


async def _allowed(_, __, update):
    return bool(update.from_user and is_allowed(update.from_user.id))

allowed = filters.create(_allowed)
owner = filters.user(OWNER_ID)

# ---------------------------------------------------------------- per-user settings
usettings: dict[str, dict] = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
DEF = {"as_video": True, "rename": False, "caption": None, "buttons": []}


def settings(uid) -> dict:
    d = usettings.setdefault(str(uid), {})
    for k, v in DEF.items():
        d.setdefault(k, list(v) if isinstance(v, list) else v)
    return d


def save_settings():
    SETTINGS_FILE.write_text(json.dumps(usettings))


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
        users[str(u.id)] = u.username or u.first_name
        save_users()
        await m.reply(f"✅ Access diya: {u.mention} (`{u.id}`)")
    except Exception as e:
        await m.reply(f"❌ User nahi mila: {e}\nUser ko pehle bot /start karne bolo ya user id use karo.")


@app.on_message(filters.command(["deny", "denid"]) & owner)
async def deny(c, m):
    try:
        u = await resolve(c, m)
        if not u:
            return await m.reply("Use: /deny @username  (ya user id / reply)")
        if users.pop(str(u.id), None) is None:
            return await m.reply("Ye user list me nahi tha.")
        save_users()
        await m.reply(f"🚫 Access hata diya: {u.mention}")
    except Exception as e:
        await m.reply(f"❌ {e}")


@app.on_message(filters.command("users") & owner)
async def list_users(c, m):
    txt = "\n".join(f"• {n} (`{i}`)" for i, n in users.items()) or "Koi user nahi."
    await m.reply(f"**Allowed users:**\n{txt}")

# ---------------------------------------------------------------- /start menu (Home / Functions / Commands / Disclaimer)
def start_text(u) -> str:
    sup = f"If any issues arise, report them at 👉 {SUPPORT_GROUP}\n\n" if SUPPORT_GROUP else ""
    return (f"**Hi** {u.mention}\n\nI am **Video Converter**.\n\n"
            "Many useful functions are available with Max Option — send me any video and choose an action.\n\n"
            f"{sup}**Legal Disclaimer:** /disclaimer")


def start_kb():
    return KB([[G("Other Bots", "s|bots"), G("Functions", "s|func")],
               [G("Commands", "s|cmds"), G("Disclaimer", "s|disc")],
               [R("Close", "s|close")]])


COMMANDS_TXT = (
    "/start - Main menu\n\n"
    "/settings - Upload as Video/File, Rename File ON/OFF\n\n"
    "/bulk - Bahut saari files ek saath (parallel convert)\n\n"
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
            f"Rename File: {'✅ ON' if st['rename'] else '❌ OFF'}")


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
    if job is None:  # bulk etc. -> no progress tracking
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


async def media_info(src: Path) -> str:
    j = json.loads(await run("ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(src)))
    f = j["format"]
    lines = [f"📄 **{src.name}**", f"Size: {human(f.get('size', 0))}",
             f"Duration: {float(f.get('duration', 0)):.1f}s", f"Bitrate: {int(f.get('bit_rate', 0)) // 1000} kbps",
             f"Container: {f.get('format_long_name', '')}"]
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
    try:
        await job.status.edit(STAGE_TXT[stage] + (f"\n{label}" if label else ""), reply_markup=job_kb(job))
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
        try:
            await ff(*base, "-c", "copy", pl)
        except RuntimeError:
            for f in d.iterdir():
                f.unlink()
            await ff(*base, *enc, pl)
    else:
        await ff(*base, "-vf", f"scale=-2:{p['q']}", *enc, pl)
    z = out / f"{src.stem}_hls_{'original' if p['q'] == 'orig' else str(p['q']) + 'p'}.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        for f in d.iterdir():
            zf.write(f, f.name)
    return [z]


async def act_hls_all(src, out, p):
    """Every quality at once (parallel ffmpeg). Qualities above the source height are skipped."""
    info = await probe(src)
    qs = ["orig"] + [q for q in (360, 480, 720, 1080) if not info["h"] or q <= info["h"]]
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
        n = f.with_name(base + (f"_{i}" if len(files) > 1 else "") + f.suffix)
        f.rename(n)
        out.append(n)
    return out

# ---------------------------------------------------------------- sending / jobs
async def send_out(c, chat, p: Path, cap=None, job=None, idx=0, uid=None):
    """Upload with file name as caption (title) — or the user's custom caption/buttons."""
    st = settings(uid) if uid else dict(DEF)
    e = p.suffix.lower()
    if cap is None:
        cap = st["caption"].replace("{filename}", p.name) if st.get("caption") else (p.name if e not in IMG else "")
    kw = {}
    if job:
        kw["progress"] = prog_cb(job, idx)
    mk = url_kb(st)
    if mk:
        kw["reply_markup"] = mk
    if not st["as_video"]:
        await c.send_document(chat, str(p), caption=cap, **kw)
    elif e in (".mp4", ".m4v"):
        info = await probe(p)
        thumb = await make_thumb(p, info["dur"])
        await c.send_video(chat, str(p), caption=cap, supports_streaming=True, duration=int(info["dur"]),
                           width=info["w"], height=info["h"], thumb=thumb, **kw)
    elif e in (".mp3", ".m4a", ".flac"):
        await c.send_audio(chat, str(p), caption=cap, **kw)
    elif e in IMG:
        await c.send_photo(chat, str(p), caption=cap, **kw)
    else:
        await c.send_document(chat, str(p), caption=cap, **kw)
    chk(job)


async def deliver(c, chat, files, job, uid, parallel=False):
    st = settings(uid)
    if len(files) > 1 and all(f.suffix.lower() in IMG for f in files):  # screenshots -> albums of 10
        await set_stage(job, "upload", f"{len(files)} photos")
        M = InputMediaPhoto if st["as_video"] else InputMediaDocument
        for i in range(0, len(files), 10):
            await c.send_media_group(chat, [M(str(f)) for f in files[i:i + 10]])
            chk(job)
    elif parallel and len(files) > 1:  # all uploads at the same time
        await set_stage(job, "upload", f"{len(files)} files parallel")
        res = await asyncio.gather(*[send_out(c, chat, f, job=job, idx=i, uid=uid) for i, f in enumerate(files)],
                                   return_exceptions=True)
        errs = [r for r in res if isinstance(r, BaseException)]
        if errs:
            raise next((e for e in errs if isinstance(e, Cancelled)), errs[0])
    else:
        for i, f in enumerate(files, 1):
            await set_stage(job, "upload", f"{i}/{len(files)}" if len(files) > 1 else "")
            await send_out(c, chat, f, job=job, uid=uid)


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


async def single_job(c, chat, msg, p: dict, status, local=None):
    d = local[1] if local else WORK / uuid.uuid4().hex
    uid = msg.from_user.id if msg.from_user else 0
    job = Job(uid, status, dc_of(msg))
    CUR.set(job)
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
            if p["act"] == "info":
                await status.edit(await media_info(src))
                return
            info = await probe(src)
            await set_stage(job, "convert")
            job.total = info["dur"]
            out = d / "out"
            out.mkdir(parents=True, exist_ok=True)
            files = apply_newname(await ACTIONS[p["act"]](src, out, p), p)
            await deliver(c, chat, files, job, uid, parallel=p.get("parallel", False))
        await status.delete()
    except Exception as e:
        await fail(status, job, e)
    finally:
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


async def bulk_job(c, chat, msg, p, idx, total, prog):
    d = WORK / uuid.uuid4().hex
    uid = msg.from_user.id if msg.from_user else None
    try:
        async with sem:  # max MAX_PARALLEL files at one time
            src = await download(c, msg, d / "in")
            out = d / "out"
            out.mkdir(parents=True, exist_ok=True)
            for f in await ACTIONS[p["act"]](src, out, p):
                await send_out(c, chat, f, uid=uid)
        prog["ok"] += 1
    except Exception as e:
        prog["fail"] += 1
        await c.send_message(chat, f"❌ File #{idx} fail: `{str(e)[-300:]}`")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        try:
            await prog["msg"].edit(f"⚙️ Bulk: ✅ {prog['ok']} | ❌ {prog['fail']} / {total}")
        except Exception:
            pass


async def merge_job(c, chat, msgs, status):
    d = WORK / uuid.uuid4().hex
    uid = msgs[0].from_user.id if msgs[0].from_user else 0
    job = Job(uid, status, dc_of(msgs[0]))
    CUR.set(job)
    try:
        await set_stage(job, "queued")
        async with sem:
            await set_stage(job, "download", f"{len(msgs)} videos")
            srcs = await asyncio.gather(*[download(c, m, d / f"in{i}", job, i) for i, m in enumerate(msgs)])
            infos = [await probe(x) for x in srcs]
            all_st = [await get_streams(x) for x in srcs]
            mode = merge_mode(all_st)
            await set_stage(job, "convert",
                            "⚡ Fast merge (no quality loss)" if mode == "copy"
                            else "🔄 Videos ke formats alag hain, re-encode karke merge ho raha hai")
            job.total = sum(i["dur"] for i in infos)
            out = d / "out"
            out.mkdir(parents=True, exist_ok=True)
            for f in await act_merge(srcs, out, mode, all_st, [i["dur"] for i in infos]):
                await set_stage(job, "upload")
                await send_out(c, chat, f, job=job, uid=uid)
        await status.delete()
    except Exception as e:
        await fail(status, job, e)
    finally:
        jobs.pop(job.id, None)
        shutil.rmtree(d, ignore_errors=True)


NO_RENAME = {"info"}


async def launch(c, uid, chat, p, status=None):
    """Start a single-video job (asks for a new name first if 'Rename File' is ON)."""
    s = sessions.setdefault(uid, {})
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
    asyncio.create_task(single_job(c, chat, msg, p, status, s.pop("local", None)))

# ---------------------------------------------------------------- UI
def menu_text(uid):
    up = "🎥 **Video**" if settings(uid)["as_video"] else "📁 **File**"
    return ("✶ If you want New Name in Output video, Then Activate ✅ **Rename File**.\n\n"
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
        [G("📡 To M3U8 (HLS)", "a|hls")],
        [R("Cancel ❌", "a|cancel")],
    ])


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


@app.on_message(filters.private & allowed & (filters.audio | filters.voice | filters.document))
async def on_extra(c, m):
    """Second file for 'Merge 🎥 And 🎵' (audio) and 'Subtitle Merger' (srt/ass/vtt)."""
    uid = m.from_user.id
    w = sessions.get(uid, {}).get("wait")
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
    await m.reply("📦 Bulk mode ON. Videos bhejo, phir /done.")


@app.on_message(filters.private & allowed & filters.command("merge"))
async def merge_cmd(c, m):
    drop_local(sessions.setdefault(m.from_user.id, {}))
    sessions[m.from_user.id] = {"mode": "merge", "files": []}
    await m.reply("🔗 Merge mode ON. Videos order me bhejo, phir /done.")


@app.on_message(filters.private & allowed & filters.command("cancel"))
async def cancel_cmd(c, m):
    drop_local(sessions.get(m.from_user.id, {}))
    sessions.pop(m.from_user.id, None)
    await m.reply("Cancelled ✅")


@app.on_message(filters.private & allowed & filters.command("done"))
async def done_cmd(c, m):
    s = sessions.get(m.from_user.id)
    if not s or s.get("mode") not in ("bulk", "merge") or not s["files"]:
        return await m.reply("Pehle /bulk ya /merge chalao aur files bhejo.")
    if s["mode"] == "merge":
        if len(s["files"]) < 2:
            return await m.reply("Kam se kam 2 videos chahiye.")
        files = s.pop("files")
        sessions.pop(m.from_user.id, None)
        st = await m.reply("Starting...")
        return asyncio.create_task(merge_job(c, m.chat.id, files, st))
    await m.reply(f"{len(s['files'])} files. Sab ko kya banana hai?", reply_markup=grid([
        ("MP4", "b|mp4"), ("MKV", "b|mkv"), ("MP3", "b|mp3"),
        ("M3U8 720p", "b|h720"), ("M3U8 original", "b|horig"), ("Optimize", "b|opt")], back=False))


@app.on_message(filters.private & allowed & filters.text, group=1)
async def on_text(c, m):
    uid = m.from_user.id
    s = sessions.get(uid)
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

    if k[0] == "b":  # bulk
        files = s.get("files")
        if not files:
            return await q.message.edit("Session expire. /bulk dobara chalao.")
        p = {"mp4": {"act": "conv", "fmt": "mp4"}, "mkv": {"act": "conv", "fmt": "mkv"},
             "mp3": {"act": "aud", "fmt": "mp3", "q": "192"}, "h720": {"act": "hls", "q": "720"},
             "horig": {"act": "hls", "q": "orig"}, "opt": {"act": "opt", "crf": 28}}[k[1]]
        sessions.pop(uid, None)
        prog = {"ok": 0, "fail": 0, "msg": await q.message.edit(f"⚙️ Bulk: 0/{len(files)} (parallel: {MAX_PARALLEL})")}
        total = len(files)
        asyncio.create_task(asyncio.gather(
            *[bulk_job(c, chat, m, p, i + 1, total, prog) for i, m in enumerate(files)]))
        return

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
        await ed(menu_text(uid), main_kb())

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
    await app.start()
    me = await app.get_me()
    print(f"\n✅ Bot chalu hai: @{me.username}  |  Owner ID: {OWNER_ID}", flush=True)
    print(f"👉 Telegram me @{me.username} ko /start bhejo. Band karne ke liye Ctrl+C.\n", flush=True)
    await idle()
    await app.stop()


if __name__ == "__main__":
    WORK.mkdir(exist_ok=True)
    print("⏳ Telegram se connect ho raha hai...", flush=True)
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass