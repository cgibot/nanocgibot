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

# ------------------ Config ------------------
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
HUGGINGFACE_API_KEY = os.environ["HUGGINGFACE_API_KEY"]
HUGGINGFACE_MODEL = os.environ.get("HUGGINGFACE_MODEL","naviernan/nano-banana")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET","images")
MAX_PER_USER_PER_DAY = int(os.environ.get("MAX_PER_USER_PER_DAY",9999))
GLOBAL_MONTHLY_LIMIT = int(os.environ.get("GLOBAL_MONTHLY_LIMIT",9999))

# ------------------ Supabase ------------------
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------------ Discord bot ------------------
intents = Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)

# ------------------ Database ------------------
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

# ------------------ Hugging Face API ------------------
async def hf_generate(prompt):
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {HUGGINGFACE_API_KEY}"}
        payload = {"inputs": prompt}
        async with session.post(f"https://api-inference.huggingface.co/models/{HUGGINGFACE_MODEL}", headers=headers, json=payload) as resp:
            if resp.status == 200:
                data = await resp.read()
                return data
            text = await resp.text()
            raise RuntimeError(f"HuggingFace generate failed {resp.status}: {text}")

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
@app_commands.command(name="generate", description="Generate an image")
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
        img_bytes = await hf_generate(prompt)
        seq, filename = await save_image(uid, img_bytes, prompt)
        await increment_user_usage(uid)
        await increment_global_usage()
        bio = io.BytesIO(img_bytes)
        bio.seek(0)
        await interaction.followup.send(file=File(bio, filename=f"img_{seq}.png"))
    except Exception as e:
        await interaction.followup.send(f"Generation failed: {e}", ephemeral=True)

bot.tree.add_command(generate)

bot.run(DISCORD_BOT_TOKEN)
