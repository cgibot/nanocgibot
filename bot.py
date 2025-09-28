import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import os
import json
import random
import time
from datetime import datetime, timedelta
import sqlite3
from typing import Dict, List, Optional

# Bot setup with all intents needed
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Configuration
HF_API_KEY = os.getenv('HF_API_KEY')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
ADMIN_USER_IDS = [int(x) for x in os.getenv('ADMIN_USER_IDS', '').split(',') if x]

# Models available (all free on Hugging Face)
MODELS = {
    'flux': {
        'url': 'https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell',
        'name': '⚡ FLUX Schnell (Fast)',
        'description': 'Lightning fast, great quality'
    },
    'sd3': {
        'url': 'https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-3-medium-diffusers',
        'name': '🎨 Stable Diffusion 3',
        'description': 'High quality, versatile'
    },
    'playground': {
        'url': 'https://api-inference.huggingface.co/models/playgroundai/playground-v2.5-1024px-aesthetic',
        'name': '🌟 Playground v2.5',
        'description': 'Aesthetic and artistic'
    },
    'anime': {
        'url': 'https://api-inference.huggingface.co/models/cagliostrolab/animagine-xl-3.1',
        'name': '🎌 Animagine XL',
        'description': 'Perfect for anime/manga style'
    }
}

# Style presets
STYLE_PRESETS = {
    'photorealistic': 'photorealistic, highly detailed, professional photography, 8k resolution, sharp focus',
    'anime': 'anime style, manga, cel shading, vibrant colors, detailed character design',
    'artistic': 'digital art, concept art, trending on artstation, detailed, professional',
    'cyberpunk': 'cyberpunk style, neon lights, futuristic, sci-fi, dark atmosphere',
    'fantasy': 'fantasy art, magical, ethereal, mystical, detailed environment',
    'vintage': 'vintage style, retro, aged, classic, nostalgic atmosphere',
    'minimalist': 'minimalist design, clean, simple, elegant composition',
    'surreal': 'surreal art, dreamlike, abstract, unusual perspective, creative'
}

# Random prompt components
PROMPT_SUBJECTS = [
    'a majestic dragon', 'a cyberpunk city', 'a magical forest', 'a space station',
    'a steampunk robot', 'a crystal cave', 'a floating island', 'a neon-lit street',
    'an ancient temple', 'a cosmic nebula', 'a mechanical butterfly', 'a glass pyramid'
]

PROMPT_STYLES = [
    'in photorealistic style', 'as digital art', 'in anime style', 'as concept art',
    'in watercolor style', 'as oil painting', 'in cyberpunk aesthetic', 'in fantasy style'
]

PROMPT_LIGHTING = [
    'with dramatic lighting', 'bathed in golden light', 'under moonlight', 'with neon glow',
    'in soft natural light', 'with cinematic lighting', 'glowing from within', 'backlit silhouette'
]

# Database setup
def init_db():
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    
    # Users table for tracking usage
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, 
                  images_generated INTEGER DEFAULT 0,
                  last_used TEXT,
                  total_usage INTEGER DEFAULT 0)''')
    
    # Generated images log
    c.execute('''CREATE TABLE IF NOT EXISTS images
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  prompt TEXT,
                  model TEXT,
                  timestamp TEXT,
                  guild_id INTEGER)''')
    
    conn.commit()
    conn.close()

# User management
class UserManager:
    @staticmethod
    def get_user_stats(user_id: int) -> Dict:
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        conn.close()
        
        if result:
            return {
                'user_id': result[0],
                'images_generated': result[1],
                'last_used': result[2],
                'total_usage': result[3]
            }
        return {'user_id': user_id, 'images_generated': 0, 'last_used': None, 'total_usage': 0}
    
    @staticmethod
    def update_user_usage(user_id: int):
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        now = datetime.now().isoformat()
        
        c.execute("""INSERT OR REPLACE INTO users 
                     (user_id, images_generated, last_used, total_usage)
                     VALUES (?, 
                            COALESCE((SELECT images_generated FROM users WHERE user_id = ?), 0) + 1,
                            ?,
                            COALESCE((SELECT total_usage FROM users WHERE user_id = ?), 0) + 1)""",
                 (user_id, user_id, now, user_id))
        conn.commit()
        conn.close()
    
    @staticmethod
    def reset_daily_usage():
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("UPDATE users SET images_generated = 0")
        conn.commit()
        conn.close()

    @staticmethod
    def log_image_generation(user_id: int, prompt: str, model: str, guild_id: int):
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute("INSERT INTO images (user_id, prompt, model, timestamp, guild_id) VALUES (?, ?, ?, ?, ?)",
                 (user_id, prompt, model, now, guild_id))
        conn.commit()
        conn.close()

# Cooldown manager
cooldowns = {}

def check_cooldown(user_id: int, cooldown_seconds: int = 60) -> Optional[float]:
    """Check if user is on cooldown. Returns remaining time or None if not on cooldown."""
    if user_id in cooldowns:
        elapsed = time.time() - cooldowns[user_id]
        if elapsed < cooldown_seconds:
            return cooldown_seconds - elapsed
    return None

def set_cooldown(user_id: int):
    """Set cooldown for user."""
    cooldowns[user_id] = time.time()

# Rate limiting
def check_daily_limit(user_id: int, limit: int = 20) -> bool:
    """Check if user has exceeded daily limit."""
    stats = UserManager.get_user_stats(user_id)
    return stats['images_generated'] >= limit

@bot.event
async def on_ready():
    print(f'🚀 {bot.user} is now online and ready!')
    print(f'📊 Serving {len(bot.guilds)} servers')
    
    # Initialize database
    init_db()
    
    # Start background tasks
    reset_daily_limits.start()
    
    try:
        synced = await bot.tree.sync()
        print(f'✅ Synced {len(synced)} slash commands')
    except Exception as e:
        print(f'❌ Failed to sync commands: {e}')

async def generate_image_hf(prompt: str, model_key: str = 'flux') -> Optional[bytes]:
    """Generate image using Hugging Face API."""
    model = MODELS.get(model_key, MODELS['flux'])
    
    headers = {
        "Authorization": f"Bearer {HF_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {"inputs": prompt}
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(model['url'], headers=headers, json=payload) as response:
                if response.status == 200:
                    return await response.read()
                else:
                    error_text = await response.text()
                    print(f"API Error: {response.status} - {error_text}")
                    return None
        except Exception as e:
            print(f"Request Error: {e}")
            return None

@bot.tree.command(name='generate', description='🎨 Generate an AI image with your prompt')
async def generate_image(
    interaction: discord.Interaction, 
    prompt: str, 
    model: str = 'flux',
    style: str = None,
    private: bool = True
):
    """Main image generation command."""
    user_id = interaction.user.id
    
    # Check cooldown
    remaining_cooldown = check_cooldown(user_id, 45)  # 45 second cooldown
    if remaining_cooldown:
        await interaction.response.send_message(
            f"⏰ Please wait {remaining_cooldown:.0f} more seconds before generating another image.",
            ephemeral=True
        )
        return
    
    # Check daily limit
    if check_daily_limit(user_id, 25):  # 25 images per day
        await interaction.response.send_message(
            "📊 You've reached your daily limit of 25 images. Limits reset every 24 hours!",
            ephemeral=True
        )
        return
    
    # Validate model
    if model not in MODELS:
        model = 'flux'
    
    # Apply style preset if specified
    enhanced_prompt = prompt
    if style and style in STYLE_PRESETS:
        enhanced_prompt = f"{prompt}, {STYLE_PRESETS[style]}"
    
    # Defer response
    await interaction.response.defer(ephemeral=private)
    
    try:
        # Set cooldown
        set_cooldown(user_id)
        
        # Generate image
        image_data = await generate_image_hf(enhanced_prompt, model)
        
        if image_data:
            # Create file
            file = discord.File(
                fp=asyncio.BytesIO(image_data),
                filename="generated_image.png"
            )
            
            # Create embed
            embed = discord.Embed(
                title="🎨 AI Generated Image",
                color=0x00ff00,
                timestamp=datetime.now()
            )
            
            if not private:
                embed.add_field(name="📝 Prompt", value=f"```{prompt}```", inline=False)
            
            embed.add_field(name="🤖 Model", value=MODELS[model]['name'], inline=True)
            if style:
                embed.add_field(name="🎭 Style", value=style.title(), inline=True)
            embed.add_field(name="👤 Created by", value=interaction.user.mention, inline=True)
            
            embed.set_image(url="attachment://generated_image.png")
            embed.set_footer(text="🔥 Ultimate AI Art Bot | React with ❤️ if you like it!")
            
            # Update user stats
            UserManager.update_user_usage(user_id)
            UserManager.log_image_generation(user_id, prompt, model, interaction.guild.id if interaction.guild else 0)
            
            # Send result
            message = await interaction.followup.send(
                file=file,
                embed=embed,
                ephemeral=private
            )
            
            # Add reactions for public images
            if not private and message:
                try:
                    await message.add_reaction("❤️")
                    await message.add_reaction("🔥")
                    await message.add_reaction("🎨")
                except:
                    pass  # Ignore reaction errors
            
        else:
            await interaction.followup.send(
                "❌ Failed to generate image. The AI service might be busy. Please try again in a moment.",
                ephemeral=True
            )
            
    except Exception as e:
        await interaction.followup.send(
            f"❌ An unexpected error occurred: {str(e)}",
            ephemeral=True
        )

@bot.tree.command(name='random', description='🎲 Generate a random AI image')
async def random_generate(interaction: discord.Interaction, model: str = 'flux'):
    """Generate image with random prompt."""
    subject = random.choice(PROMPT_SUBJECTS)
    style_desc = random.choice(PROMPT_STYLES)
    lighting = random.choice(PROMPT_LIGHTING)
    
    random_prompt = f"{subject} {style_desc} {lighting}"
    
    await generate_image.callback(interaction, random_prompt, model, None, False)

@bot.tree.command(name='styles', description='🎭 View available style presets')
async def view_styles(interaction: discord.Interaction):
    """Show available style presets."""
    embed = discord.Embed(
        title="🎭 Available Style Presets",
        color=0x0099ff,
        description="Use these with the `/generate` command!"
    )
    
    for style, description in STYLE_PRESETS.items():
        embed.add_field(
            name=f"🎨 {style.title()}",
            value=f"`/generate prompt:{description[:50]}...`",
            inline=False
        )
    
    embed.set_footer(text="Example: /generate prompt:a cat style:anime")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name='models', description='🤖 View available AI models')
async def view_models(interaction: discord.Interaction):
    """Show available AI models."""
    embed = discord.Embed(
        title="🤖 Available AI Models",
        color=0xff9900,
        description="Choose your preferred AI model!"
    )
    
    for key, model in MODELS.items():
        embed.add_field(
            name=model['name'],
            value=f"{model['description']}\n`model:{key}`",
            inline=True
        )
    
    embed.set_footer(text="Example: /generate prompt:a robot model:anime")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name='stats', description='📊 View your usage statistics')
async def user_stats(interaction: discord.Interaction):
    """Show user statistics."""
    stats = UserManager.get_user_stats(interaction.user.id)
    
    embed = discord.Embed(
        title=f"📊 Stats for {interaction.user.display_name}",
        color=0x9966cc
    )
    
    embed.add_field(name="🎨 Today's Images", value=f"{stats['images_generated']}/25", inline=True)
    embed.add_field(name="🏆 Total Generated", value=stats['total_usage'], inline=True)
    embed.add_field(name="⏰ Cooldown", value="45 seconds", inline=True)
    
    if stats['last_used']:
        last_used = datetime.fromisoformat(stats['last_used'])
        embed.add_field(name="🕒 Last Used", value=last_used.strftime("%Y-%m-%d %H:%M"), inline=True)
    
    remaining = 25 - stats['images_generated']
    embed.add_field(name="🎯 Remaining Today", value=f"{remaining} images", inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name='help', description='❓ Get help with the bot')
async def help_command(interaction: discord.Interaction):
    """Comprehensive help command."""
    embed = discord.Embed(
        title="🎨 Ultimate AI Art Bot - Help",
        color=0x00ff00,
        description="Generate stunning AI images with advanced features!"
    )
    
    embed.add_field(
        name="🎨 `/generate`",
        value="Generate AI images\n`prompt:` Your description\n`model:` AI model to use\n`style:` Style preset\n`private:` Keep prompt hidden",
        inline=False
    )
    
    embed.add_field(
        name="🎲 `/random`",
        value="Generate random AI image\n`model:` Choose AI model",
        inline=False
    )
    
    embed.add_field(
        name="📊 `/stats`",
        value="View your usage statistics",
        inline=True
    )
    
    embed.add_field(
        name="🎭 `/styles`",
        value="View style presets",
        inline=True
    )
    
    embed.add_field(
        name="🤖 `/models`",
        value="View AI models",
        inline=True
    )
    
    embed.add_field(
        name="🔧 Features",
        value="• Private prompts by default\n• 25 images/day limit\n• 45s cooldown\n• Multiple AI models\n• Style presets\n• Usage statistics",
        inline=False
    )
    
    embed.add_field(
        name="💡 Pro Tips",
        value="• Be specific in prompts\n• Try different models\n• Use style presets\n• Check `/stats` regularly",
        inline=False
    )
    
    embed.set_footer(text="🚀 Powered by Hugging Face • 100% Free • Open Source")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Admin commands
@bot.tree.command(name='admin_stats', description='📈 View bot statistics (Admin only)')
async def admin_stats(interaction: discord.Interaction):
    """Admin statistics command."""
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message("❌ Admin only command!", ephemeral=True)
        return
    
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    
    # Get total stats
    c.execute("SELECT COUNT(*) FROM images")
    total_images = c.fetchone()[0]
    
    c.execute("SELECT COUNT(DISTINCT user_id) FROM users")
    total_users = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM images WHERE DATE(timestamp) = DATE('now')")
    today_images = c.fetchone()[0]
    
    conn.close()
    
    embed = discord.Embed(
        title="📈 Bot Statistics (Admin)",
        color=0xff0000
    )
    
    embed.add_field(name="👥 Total Users", value=total_users, inline=True)
    embed.add_field(name="🎨 Total Images", value=total_images, inline=True)
    embed.add_field(name="📅 Today's Images", value=today_images, inline=True)
    embed.add_field(name="🏠 Servers", value=len(bot.guilds), inline=True)
    embed.add_field(name="⚡ Latency", value=f"{bot.latency*1000:.0f}ms", inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Background task to reset daily limits
@tasks.loop(hours=24)
async def reset_daily_limits():
    """Reset daily usage limits."""
    UserManager.reset_daily_usage()
    print("🔄 Daily usage limits reset!")

# Autocomplete for commands
@generate_image.autocomplete('model')
@random_generate.autocomplete('model')
async def model_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for model selection."""
    return [
        discord.app_commands.Choice(name=model['name'], value=key)
        for key, model in MODELS.items()
        if current.lower() in model['name'].lower()
    ][:25]

@generate_image.autocomplete('style')
async def style_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for style selection."""
    return [
        discord.app_commands.Choice(name=style.title(), value=style)
        for style in STYLE_PRESETS.keys()
        if current.lower() in style.lower()
    ][:25]

# Error handling
@bot.event
async def on_app_command_error(interaction: discord.Interaction, error):
    """Global error handler for slash commands."""
    if interaction.response.is_done():
        await interaction.followup.send(f"❌ An error occurred: {str(error)}", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ An error occurred: {str(error)}", ephemeral=True)

if __name__ == "__main__":
    if not DISCORD_TOKEN or not HF_API_KEY:
        print("❌ Missing environment variables!")
        print("Required: DISCORD_TOKEN, HF_API_KEY")
        exit(1)
    
    print("🚀 Starting Ultimate AI Art Bot...")
    bot.run(DISCORD_TOKEN)
