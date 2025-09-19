# bot.py
import os
import asyncio
from dotenv import load_dotenv

import yt_dlp
import discord
from discord.ext import commands

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # optional: set a guild id for instant dev sync

intents = discord.Intents.default()  # message_content not required for slash commands
bot = commands.Bot(command_prefix="!", intents=intents)

# per-guild queues and announcement-channel mapping
queues = {}  # guild_id -> list of query/url strings
text_channel_for_guild = {}  # guild_id -> text channel id

ydl_opts = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
}

ffmpeg_options = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


async def ensure_voice_connected(interaction: discord.Interaction):
    """Connect bot to user's voice channel (or return existing voice client)."""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return None

    vc = guild.voice_client
    if vc and vc.is_connected():
        return vc

    if interaction.user.voice and interaction.user.voice.channel:
        try:
            return await interaction.user.voice.channel.connect()
        except Exception as e:
            await interaction.response.send_message(f"Failed to connect: {e}", ephemeral=True)
            return None
    else:
        await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
        return None


async def _play_next_or_stop(guild_id: int):
    """Internal: play next item from queue for guild_id."""
    if not queues.get(guild_id):
        return  # queue empty

    url = queues[guild_id].pop(0)
    guild = bot.get_guild(guild_id)
    if not guild:
        return

    vc = guild.voice_client
    if not vc:
        return

    # extract stream URL and metadata
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "entries" in info and isinstance(info["entries"], list):
                info = info["entries"][0]
            stream_url = info.get("url")
            title = info.get("title", "Unknown")
    except Exception as e:
        text_channel_id = text_channel_for_guild.get(guild_id)
        if text_channel_id:
            chan = bot.get_channel(text_channel_id)
            if chan:
                await chan.send(f"⚠️ Failed to extract audio: {e}")
        return

    def after_play(err):
        if err:
            print("Player error:", err)
        fut = asyncio.run_coroutine_threadsafe(_play_next_or_stop(guild_id), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print("Error scheduling next:", e)

    source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_options)
    try:
        vc.play(source, after=after_play)
    except Exception as e:
        text_channel_id = text_channel_for_guild.get(guild_id)
        if text_channel_id:
            chan = bot.get_channel(text_channel_id)
            if chan:
                await chan.send(f"⚠️ Playback error: {e}")
        return

    # announce track in saved text channel
    text_channel_id = text_channel_for_guild.get(guild_id)
    if text_channel_id:
        chan = bot.get_channel(text_channel_id)
        if chan:
            await chan.send(f"▶️ Now playing: **{title}**")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    # Sync commands:
    try:
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            await bot.tree.sync(guild=guild)
            print(f"Synced commands to DEV_GUILD_ID {DEV_GUILD_ID}")
        else:
            await bot.tree.sync()
            print("Synced global commands")
    except Exception as e:
        print("Failed to sync commands:", e)


# ------- Slash commands --------

@bot.tree.command(name="join", description="Make the bot join your voice channel")
async def join(interaction: discord.Interaction):
    vc = await ensure_voice_connected(interaction)
    if vc:
        await interaction.response.send_message("✅ Joined your voice channel", ephemeral=True)


@bot.tree.command(name="leave", description="Make the bot leave the voice channel")
async def leave(interaction: discord.Interaction):
    guild = interaction.guild
    if guild and guild.voice_client:
        await guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Left the voice channel", ephemeral=True)
    else:
        await interaction.response.send_message("I'm not connected.", ephemeral=True)


@bot.tree.command(name="play", description="Play a song (YouTube URL or search term)")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    vc = guild.voice_client
    if not vc or not vc.is_connected():
        vc = await ensure_voice_connected(interaction)
        if not vc:
            return

    # store the channel for announcements, and add to queue
    text_channel_for_guild.setdefault(guild.id, interaction.channel_id)
    queues.setdefault(guild.id, [])
    queues[guild.id].append(query)

    if not vc.is_playing():
        await _play_next_or_stop(guild.id)
        await interaction.followup.send("🎶 Added and started playing.", ephemeral=True)
    else:
        await interaction.followup.send("✅ Added to queue.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip current song")
async def skip(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild or not guild.voice_client or not guild.voice_client.is_playing():
        await interaction.response.send_message("🚫 Nothing is playing.", ephemeral=True)
        return
    guild.voice_client.stop()
    await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)


@bot.tree.command(name="pause", description="Pause playback")
async def pause(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild or not guild.voice_client or not guild.voice_client.is_playing():
        await interaction.response.send_message("🚫 Nothing is playing.", ephemeral=True)
        return
    guild.voice_client.pause()
    await interaction.response.send_message("⏸️ Paused.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild or not guild.voice_client or not guild.voice_client.is_paused():
        await interaction.response.send_message("🚫 Nothing is paused.", ephemeral=True)
        return
    guild.voice_client.resume()
    await interaction.response.send_message("▶️ Resumed.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the play queue (next 10 items)")
async def show_queue(interaction: discord.Interaction):
    guild = interaction.guild
    q = queues.get(guild.id, [])
    if not q:
        await interaction.response.send_message("🚫 Queue is empty.", ephemeral=True)
        return
    lines = [f"{i+1}. {item}" for i, item in enumerate(q[:10])]
    await interaction.response.send_message("🎶 Queue:\n" + "\n".join(lines), ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        print("Error: BOT_TOKEN is not set.")
    else:
        bot.run(TOKEN)
