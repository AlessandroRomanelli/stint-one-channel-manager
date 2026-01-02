import os
import json
import asyncio
from pathlib import Path
from typing import Dict, Optional, Any

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

def _env_int(name: str) -> int:
    v = os.getenv(name)
    if not v or not v.isdigit():
        raise RuntimeError(f"Missing/invalid {name} in .env")
    return int(v)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")

DATA_PATH = Path("data.json")

LIFETIME_SECONDS = 300  # 5 minutes

GROUPS = {
    "iracing": {
        "label": "IRACING VOICE CHANNELS",
        "category_id": _env_int("CATEGORY_IRACING"),
        "panel_id": _env_int("PANEL_IRACING"),
        "presets": [
            "stintONE Motorsport",
            "stintONE Motorsport Black",
            "stintONE Motorsport White",
            "stintONE Motorsport Blue",
            "stintONE Motorsport Gold",
            "stintONE Motorsport Silver",
        ],
    },
    "training": {
        "label": "IRACING TRAINING AREA",
        "category_id": _env_int("CATEGORY_TRAINING"),
        "panel_id": _env_int("PANEL_TRAINING"),
        "presets": [
            "Training",
            "Training I",
            "Training II",
            "Training III",
        ],
    },
    "live": {
        "label": "LIVE ON STREAM",
        "category_id": _env_int("CATEGORY_LIVE"),
        "panel_id": _env_int("PANEL_LIVE"),
        "presets": [
            "stintONE Motorsport LIVE",
            "stintONE Black LIVE",
            "stintONE White LIVE",
            "stintONE Blue LIVE",
            "stintONE Gold LIVE",
            "stintONE Silver LIVE",
            "stintONE Rosé LIVE",
        ],
    },
}

def load_state() -> dict:
    if not DATA_PATH.exists():
        return {"channels": {}}  # channel_id -> {group_key, created_at, last_empty_at}
    try:
        return json.loads(DATA_PATH.read_text("utf-8"))
    except Exception:
        return {"channels": {}}

def save_state(state: dict) -> None:
    DATA_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

state = load_state()
tracked: Dict[str, Dict[str, Any]] = state.get("channels", {})  # keys are str(channel_id)

# channel_id -> asyncio.Task
delete_tasks: Dict[int, asyncio.Task] = {}

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

def now_mono() -> float:
    return asyncio.get_running_loop().time()

def track_channel(channel_id: int, group_key: str, created_at: float):
    tracked[str(channel_id)] = {
        "group_key": group_key,
        "created_at": created_at,
        "last_empty_at": created_at,  # assume empty at creation; will update on join/leave events
    }
    state["channels"] = tracked
    save_state(state)

def untrack_channel(channel_id: int):
    tracked.pop(str(channel_id), None)
    state["channels"] = tracked
    save_state(state)

def get_track(channel_id: int) -> Optional[dict]:
    return tracked.get(str(channel_id))

def cancel_delete(channel_id: int):
    t = delete_tasks.pop(channel_id, None)
    if t and not t.done():
        t.cancel()

async def schedule_delete(channel: discord.VoiceChannel):
    info = get_track(channel.id)
    if not info:
        return

    # Cancel any existing timer and create a fresh one.
    cancel_delete(channel.id)

    created_at = float(info.get("created_at", 0))
    last_empty_at = float(info.get("last_empty_at", created_at))

    # Delete at max(created+5min, empty+5min)
    due = max(created_at + LIFETIME_SECONDS, last_empty_at + LIFETIME_SECONDS)
    delay = max(0.0, due - now_mono())

    async def _runner():
        try:
            await asyncio.sleep(delay)

            fresh = channel.guild.get_channel(channel.id)
            if not fresh or not isinstance(fresh, discord.VoiceChannel):
                untrack_channel(channel.id)
                return

            # Only delete if still empty at deletion time
            if len(fresh.members) > 0:
                return

            try:
                await fresh.delete(reason="Auto-delete temp voice channel (TTL)")
            except Exception:
                return

            untrack_channel(channel.id)
        finally:
            delete_tasks.pop(channel.id, None)

    delete_tasks[channel.id] = asyncio.create_task(_runner())

def used_voice_names(category: discord.CategoryChannel) -> set[str]:
    return {c.name for c in category.channels if isinstance(c, discord.VoiceChannel)}

# ---- UI ----
class PresetButtonsView(discord.ui.View):
    """Buttons for one category/group. One message per group in its panel channel."""
    def __init__(self, group_key: str):
        super().__init__(timeout=None)
        self.group_key = group_key
        g = GROUPS[group_key]
        presets = g["presets"]

        # Buttons: max 25 per message (we’re below that).
        for preset in presets:
            custom_id = f"create_vc:{group_key}:{preset}"
            self.add_item(PresetButton(label=preset, custom_id=custom_id))

class PresetButton(discord.ui.Button):
    def __init__(self, label: str, custom_id: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction):
        # custom_id format: create_vc:<group_key>:<preset_name>
        try:
            _, group_key, preset_name = self.custom_id.split(":", 2)
        except Exception:
            await interaction.response.send_message("Bad button config.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        g = GROUPS.get(group_key)
        if not g:
            await interaction.response.send_message("Unknown group.", ephemeral=True)
            return

        category = guild.get_channel(g["category_id"])
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Category ID is wrong or missing.", ephemeral=True)
            return

        # If exists already, do not create another.
        existing = discord.utils.get(category.channels, name=preset_name)
        if isinstance(existing, discord.VoiceChannel):
            await interaction.response.send_message("That channel already exists.", ephemeral=True)
            return

        # Create channel, inherit category overwrites
        try:
            created = await guild.create_voice_channel(
                name=preset_name,
                category=category,
                overwrites=dict(category.overwrites),
                reason=f"Create preset voice channel ({group_key})",
            )
        except Exception as e:
            await interaction.response.send_message(f"Create failed: {e!r}", ephemeral=True)
            return

        # Track and schedule deletion (5 min after creation unless occupied)
        created_at = now_mono()
        track_channel(created.id, group_key, created_at)

        # If empty now, schedule delete at creation+5min
        if len(created.members) == 0:
            await schedule_delete(created)

        await interaction.response.send_message(f"Created **{preset_name}**.", ephemeral=True)

# ---- Bot ----
class MyBot(commands.Bot):
    async def setup_hook(self):
        # Register persistent views (one per group)
        self.group_views: Dict[str, PresetButtonsView] = {}
        for group_key in GROUPS.keys():
            v = PresetButtonsView(group_key)
            self.group_views[group_key] = v
            self.add_view(v)

bot = MyBot(command_prefix="!", intents=intents)

async def ensure_panel_message(panel: discord.TextChannel, group_key: str):
    """Ensure the panel has a single bot message with the right buttons."""
    # Try to find an existing bot message with components.
    async for msg in panel.history(limit=50):
        if msg.author == bot.user and msg.components:
            # Keep it. (If you want strict matching, we can check custom_ids too.)
            return

    g = GROUPS[group_key]
    await panel.send(
        f"**{g['label']}**\n"
        f"Click a button to create a voice channel.\n"
        f"Channels auto-delete 5 minutes after creation or 5 minutes after they become empty.",
        view=bot.group_views[group_key],
    )

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")

    # Ensure the button message exists in each panel channel
    for guild in bot.guilds:
        for group_key, g in GROUPS.items():
            panel = guild.get_channel(g["panel_id"])
            if isinstance(panel, discord.TextChannel):
                try:
                    await ensure_panel_message(panel, group_key)
                except Exception:
                    pass

    # Cleanup tracked channels that no longer exist
    for guild in bot.guilds:
        for channel_id_str in list(tracked.keys()):
            cid = int(channel_id_str)
            ch = guild.get_channel(cid)
            if ch is None:
                untrack_channel(cid)
                continue
            if isinstance(ch, discord.VoiceChannel) and len(ch.members) == 0:
                # If empty, ensure it has a timer
                await schedule_delete(ch)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # If someone joined a tracked temp channel, cancel deletion timer
    if after.channel and isinstance(after.channel, discord.VoiceChannel):
        info = get_track(after.channel.id)
        if info:
            # channel is occupied: cancel delete
            cancel_delete(after.channel.id)

    # If someone left a tracked temp channel and it became empty, schedule deletion
    if before.channel and isinstance(before.channel, discord.VoiceChannel):
        info = get_track(before.channel.id)
        if info:
            ch = before.channel
            if len(ch.members) == 0:
                # Update last_empty_at and schedule delete
                info["last_empty_at"] = now_mono()
                save_state(state)
                await schedule_delete(ch)

bot.run(TOKEN)
