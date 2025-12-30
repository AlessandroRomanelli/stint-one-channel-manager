import os
import json
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TRIGGER_CHANNEL_ID = int(os.getenv("TRIGGER_CHANNEL_ID", "0"))
CATEGORY_IRACING = int(os.getenv("CATEGORY_IRACING", "0"))
CATEGORY_TRAINING = int(os.getenv("CATEGORY_TRAINING", "0"))
CATEGORY_LIVE = int(os.getenv("CATEGORY_LIVE", "0"))

if not TOKEN or not TRIGGER_CHANNEL_ID or not CATEGORY_IRACING or not CATEGORY_TRAINING or not CATEGORY_LIVE:
    raise RuntimeError("Missing env vars. Set DISCORD_TOKEN, TRIGGER_CHANNEL_ID, CATEGORY_IRACING, CATEGORY_TRAINING, CATEGORY_LIVE")

# ---- Your presets ----
PRESETS = {
    "iracing": [
        "stintONE Motorsport",
        "stintONE Motorsport Black",
        "stintONE Motorsport White",
        "stintONE Motorsport Blue",
        "stintONE Motorsport Gold",
        "stintONE Motorsport Silver",
    ],
    "training": [
        "Training",
        "Training I",
        "Training II",
        "Training III",
    ],
    "live": [
        "stintONE Motorsport LIVE",
        "stintONE Black LIVE",
        "stintONE White LIVE",
        "stintONE Blue LIVE",
        "stintONE Gold LIVE",
        "stintONE Silver LIVE",
        "stintONE Rosé LIVE",
    ],
}

GROUP_LABEL = {
    "iracing": "IRACING VOICE CHANNELS",
    "training": "IRACING TRAINING AREA",
    "live": "LIVE ON STREAM",
}

CATEGORY_BY_GROUP = {
    "iracing": CATEGORY_IRACING,
    "training": CATEGORY_TRAINING,
    "live": CATEGORY_LIVE,
}

DELETE_DELAY_SECONDS = 15.0
DM_VIEW_TIMEOUT_SECONDS = 90.0

DATA_PATH = Path("data.json")

def load_data() -> dict:
    if not DATA_PATH.exists():
        return {"temp_channel_ids": []}
    try:
        return json.loads(DATA_PATH.read_text("utf-8"))
    except Exception:
        return {"temp_channel_ids": []}

def save_data(payload: dict) -> None:
    DATA_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

state = load_data()
temp_channel_ids: set[int] = set(state.get("temp_channel_ids", []))
delete_tasks: dict[int, asyncio.Task] = {}

def persist():
    save_data({"temp_channel_ids": sorted(temp_channel_ids)})

# ---- Discord setup ----
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---- Pending requests (user joined trigger) ----
# user_id -> Pending(guild_id)
@dataclass
class Pending:
    guild_id: int

pending: dict[int, Pending] = {}

def used_voice_names_in_category(category: discord.CategoryChannel) -> set[str]:
    names = set()
    for ch in category.channels:
        if isinstance(ch, discord.VoiceChannel):
            names.add(ch.name)
    return names

async def schedule_delete_if_empty(channel: discord.VoiceChannel, delay: float = DELETE_DELAY_SECONDS):
    if channel.id not in temp_channel_ids:
        return
    if channel.id in delete_tasks:
        return
    if len(channel.members) > 0:
        return

    async def _runner():
        try:
            await asyncio.sleep(delay)
            fresh = channel.guild.get_channel(channel.id)
            if fresh is None or not isinstance(fresh, discord.VoiceChannel):
                temp_channel_ids.discard(channel.id)
                persist()
                return
            if len(fresh.members) > 0:
                return
            try:
                await fresh.delete(reason="Temp voice channel empty")
            except Exception:
                return
            temp_channel_ids.discard(channel.id)
            persist()
        finally:
            delete_tasks.pop(channel.id, None)

    delete_tasks[channel.id] = asyncio.create_task(_runner())

# ---- UI View ----
class TempChannelPicker(discord.ui.View):
    def __init__(self, user_id: int, guild_id: int):
        super().__init__(timeout=DM_VIEW_TIMEOUT_SECONDS)
        self.user_id = user_id
        self.guild_id = guild_id
        self.group: Optional[str] = None

        self.group_select = discord.ui.Select(
            placeholder="Pick a group",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=GROUP_LABEL["iracing"], value="iracing"),
                discord.SelectOption(label=GROUP_LABEL["training"], value="training"),
                discord.SelectOption(label=GROUP_LABEL["live"], value="live"),
            ],
        )
        self.group_select.callback = self.on_group_selected
        self.add_item(self.group_select)

        self.name_select = discord.ui.Select(
            placeholder="Pick a channel name",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Pick a group first", value="__noop__")],
            disabled=True,
        )
        self.name_select.callback = self.on_name_selected
        self.add_item(self.name_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This picker is not for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        # If they don’t pick in time, just drop the pending state.
        pending.pop(self.user_id, None)

    async def on_group_selected(self, interaction: discord.Interaction):
        self.group = self.group_select.values[0]

        guild = bot.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("I can’t access that server anymore.", ephemeral=True)
            pending.pop(self.user_id, None)
            self.stop()
            return

        category = guild.get_channel(CATEGORY_BY_GROUP[self.group])
        if category is None or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("That category ID is wrong or missing.", ephemeral=True)
            return

        used = used_voice_names_in_category(category)
        available = [n for n in PRESETS[self.group] if n not in used]

        if not available:
            self.name_select.options = [discord.SelectOption(label="No free names in this group", value="__noop__")]
            self.name_select.disabled = True
        else:
            # Discord select max options is 25; your lists are well under that.
            self.name_select.options = [discord.SelectOption(label=n, value=n) for n in available]
            self.name_select.disabled = False

        await interaction.response.edit_message(content="Pick a channel name:", view=self)

    async def on_name_selected(self, interaction: discord.Interaction):
        if not self.group:
            await interaction.response.send_message("Pick a group first.", ephemeral=True)
            return

        chosen_name = self.name_select.values[0]
        if chosen_name == "__noop__":
            await interaction.response.send_message("No channel name available.", ephemeral=True)
            return

        guild = bot.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("I can’t access that server anymore.", ephemeral=True)
            pending.pop(self.user_id, None)
            self.stop()
            return

        # Fetch member (works even if not cached)
        try:
            member = await guild.fetch_member(self.user_id)
        except Exception:
            await interaction.response.send_message("I can’t fetch your member info in that server.", ephemeral=True)
            return

        # User must still be in a voice channel (usually the trigger)
        if not member.voice or not member.voice.channel:
            await interaction.response.send_message("Join the trigger voice channel again, then retry.", ephemeral=True)
            pending.pop(self.user_id, None)
            self.stop()
            return

        category = guild.get_channel(CATEGORY_BY_GROUP[self.group])
        if category is None or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("That category ID is wrong or missing.", ephemeral=True)
            return

        # Final availability check (race-safe)
        used = used_voice_names_in_category(category)
        if chosen_name in used:
            await interaction.response.send_message("That name was just taken. Pick another.", ephemeral=True)
            return


        try:
            created = await guild.create_voice_channel(
                name=chosen_name,
                category=category,
                reason=f"Create preset temp voice channel ({self.group})",
            )
            temp_channel_ids.add(created.id)
            persist()

            await member.move_to(created, reason="Move to selected temp channel")
        except Exception as e:
            await interaction.response.send_message(f"Failed to create/move: {e!r}", ephemeral=True)
            return

        await interaction.response.edit_message(content=f"Created: **{chosen_name}**", view=None)
        pending.pop(self.user_id, None)
        self.stop()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")

    # Cleanup empty temp channels after restart
    removed = False
    for guild in bot.guilds:
        for cid in list(temp_channel_ids):
            ch = guild.get_channel(cid)
            if ch is None:
                temp_channel_ids.discard(cid)
                removed = True
                continue
            if isinstance(ch, discord.VoiceChannel) and len(ch.members) == 0:
                try:
                    await ch.delete(reason="Cleanup empty temp channel after restart")
                    temp_channel_ids.discard(cid)
                    removed = True
                except Exception:
                    pass
    if removed:
        persist()

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # If user left/moved out of a channel, maybe delete it
    if before.channel is not None and before.channel != after.channel:
        if isinstance(before.channel, discord.VoiceChannel):
            await schedule_delete_if_empty(before.channel)

    # Joined trigger?
    if after.channel is None or after.channel.id != TRIGGER_CHANNEL_ID:
        return
    if member.bot:
        return

    # Store pending
    pending[member.id] = Pending(guild_id=member.guild.id)

    # DM picker
    try:
        dm = await member.create_dm()
        view = TempChannelPicker(user_id=member.id, guild_id=member.guild.id)
        await dm.send(
            "Pick a group, then pick a channel name. I will create it and move you in.",
            view=view,
        )
    except discord.Forbidden:
        # They blocked DMs from server members
        # Nothing else we can do without a dedicated text channel.
        pass

bot.run(TOKEN)
