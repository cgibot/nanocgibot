import os
import io
import aiohttp
import aiosqlite
import logging
import datetime
from discord import File, Intents
from discord.ext import commands, tasks
from discord import app_commands
from supabase import create_client

# ------------------ Logging ------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nanobanana-bot")

# ------------------ Config from env ------------------
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
NANOBANANA_API_URL = os.environ.get("NANOBANANA_API_URL","https://nanobananafree.ai")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET","images")
MAX_PER_USER_PER_DAY = int(os.environ.get("MAX_PER_USER_PER_DAY",9999))
GLOBAL_MONTHLY_LIMIT = int(os.environ.get("GLOBAL_MONTHLY_LIMIT",9999))

# ------------------ Supabase client ------------------
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------------ Discord bot ------------------
intents = Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)

# ------------------ Database helpers ------------------
DB_PATH = "./bot.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            user_seq INTEGER,
            filename TEXT,
            prompt TEXT,
            created_at TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            date TEXT,
            user_id TEXT,
            count INTEGER,
            PRIMARY KEY(date,user_id)
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS global_usage (
            month TEXT PRIMARY KEY,
            count INTEGER
        )""")
        await db.commit()

def get_today():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

def get_month():
    return datetime.datetime.utcnow().strftime("%Y-%m")

async def increment_user_usage(user_id):
    today = get_today()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO usage(date,user_id,count) VALUES(?,?,0)", (today,user_id))
        await db.execute("UPDATE usage SET count = count+1 WHERE date=? AND user_id=?", (today,user_id))
        await db.commit()
        cur = await db.execute("SELECT count FROM usage WHERE date=? AND user_id=?", (today,user_id))
        row = await cur.fetchone()
        return row[0] if row else 0

async def get_user_usage(user_id):
    today = get_today()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT count FROM usage WHERE date=? AND user_id=?", (today,user_id))
        row = await cur.fetchone()
        return row[0] if row else 0

async def increment_global_usage():
    month = get_month()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO global_usage(month,count) VALUES(?,0)", (month,))
        await db.execute("UPDATE global_usage SET count=count+1 WHERE month=?", (month,))
        await db.commit()
        cur = await db.execute("SELECT count FROM global_usage WHERE month=?", (month,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def get_global_usage():
    month = get_month()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT count FROM global_usage WHERE month=?", (month,))
        row = await cur.fetchone()
        return row[0] if row else 0

# ------------------ Image helpers ------------------
async def save_image(user_id, image_bytes, prompt):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(user_seq),0) FROM images WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        seq = (row[0] or 0) + 1
        filename = f"{user_id}_{seq}_{int(datetime.datetime.utcnow().timestamp())}.png"
        supabase.storage.from_(SUPABASE_BUCKET).upload(filename, io.BytesIO(image_bytes), {"content-type":"image/png"})
        await db.execute("INSERT INTO images(user_id,user_seq,filename,prompt,created_at) VALUES(?,?,?,?,?)",
                         (user_id, seq, filename, prompt[:400], datetime.datetime.utcnow().isoformat()))
        await db.commit()
    return seq, filename

async def get_image_by_seq(user_id, seq):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT filename,prompt FROM images WHERE user_id=? AND user_seq=?", (user_id,seq))
        row = await cur.fetchone()
        if row is None:
            return None
        filename, prompt = row
        res = supabase.storage.from_(SUPABASE_BUCKET).download(filename)
        return res, prompt

# ------------------ NanoBanana API ------------------
async def nanobanana_generate(prompt):
    async with aiohttp.ClientSession() as session:
        payload = {"prompt": prompt}
        async with session.post(f"{NANOBANANA_API_URL}/generate", json=payload) as resp:
            if resp.status == 200:
                return await resp.read()
            text = await resp.text()
            raise RuntimeError(f"NanoBanana generate failed {resp.status}: {text}")

async def nanobanana_edit(image_bytes, prompt):
    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field("prompt", prompt)
        form.add_field("image", image_bytes, filename="input.png", content_type="image/png")
        async with session.post(f"{NANOBANANA_API_URL}/edit", data=form) as resp:
            if resp.status == 200:
                return await resp.read()
            text = await resp.text()
            raise RuntimeError(f"NanoBanana edit failed {resp.status}: {text}")

# ------------------ Bot events ------------------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user}")
    await init_db()
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")
    weekly_cleanup.start()

# ------------------ Weekly cleanup ------------------
@tasks.loop(hours=24)
async def weekly_cleanup():
    today = datetime.datetime.utcnow().weekday()  # Monday=0, Sunday=6
    if today in (0,6):
        log.info("Starting weekly cleanup...")
        files = supabase.storage.from_(SUPABASE_BUCKET).list()
        for f in files:
            supabase.storage.from_(SUPABASE_BUCKET).remove([f.name])
        log.info("Weekly cleanup finished.")

# ------------------ Slash commands ------------------
@app_commands.command(name="generate", description="Generate an image with NanoBanana")
@app_commands.describe(prompt="Describe the image")
async def generate(interaction, prompt: str):
    uid = str(interaction.user.id)
    if await get_user_usage(uid) >= MAX_PER_USER_PER_DAY:
        await interaction.response.send_message(f"You reached daily limit ({MAX_PER_USER_PER_DAY})", ephemeral=True)
        return
    if await get_global_usage() >= GLOBAL_MONTHLY_LIMIT:
        await interaction.response.send_message("Global monthly limit reached.", ephemeral=True)
        return
    await interaction.response.send_message(f"Generating image for your prompt: `{prompt}`", ephemeral=True)
    try:
        img_bytes = await nanobanana_generate(prompt)
        seq, filename = await save_image(uid, img_bytes, prompt)
        await increment_user_usage(uid)
        await increment_global_usage()
        bio = io.BytesIO(img_bytes)
        bio.seek(0)
        await interaction.followup.send(file=File(bio, filename=f"img_{seq}.png"))
    except Exception as e:
        await interaction.followup.send(f"Generation failed: {e}", ephemeral=True)

@app_commands.command(name="edit", description="Edit one of your images")
@app_commands.describe(id="Image ID", prompt="Edit prompt")
async def edit(interaction, id: int, prompt: str):
    uid = str(interaction.user.id)
    if await get_user_usage(uid) >= MAX_PER_USER_PER_DAY:
        await interaction.response.send_message(f"You reached daily limit ({MAX_PER_USER_PER_DAY})", ephemeral=True)
        return
    data = await get_image_by_seq(uid, id)
    if not data:
        await interaction.response.send_message(f"Image #{id} not found", ephemeral=True)
        return
    await interaction.response.send_message(f"Editing image #{id} with your prompt: `{prompt}`", ephemeral=True)
    try:
        out_bytes = await nanobanana_edit(data[0], prompt)
        seq, filename = await save_image(uid, out_bytes, prompt)
        await increment_user_usage(uid)
        await increment_global_usage()
        bio = io.BytesIO(out_bytes)
        bio.seek(0)
        await interaction.followup.send(file=File(bio, filename=f"edit_{seq}.png"))
    except Exception as e:
        await interaction.followup.send(f"Edit failed: {e}", ephemeral=True)

@app_commands.command(name="myimages", description="List your images")
async def myimages(interaction):
    uid = str(interaction.user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_seq,prompt,created_at FROM images WHERE user_id=? ORDER BY user_seq", (uid,))
        rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("No images yet.", ephemeral=True)
        return
    lines = [f"#{r[0]} — {r[2]} — {r[1][:50]}" for r in rows]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

bot.tree.add_command(generate)
bot.tree.add_command(edit)
bot.tree.add_command(myimages)

bot.run(DISCORD_BOT_TOKEN)
