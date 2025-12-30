import os
import json
import asyncio
from pathlib import Path
from typing import Optional, Dict, Tuple

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

def _env_int(name: str) -> int:
    val = os.getenv(name)
    if not val or not val.isdigit():
        raise RuntimeError(f"Missing/invalid {name} in .env")
    return int(val)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")

PANEL_CHANNEL_ID = _env_int("PANEL_CHANNEL_ID")

TRIGGER_IRACING = _env_int("TRIGGER_IRACING")
CATEGORY_IRACING = _env_int("CATEGORY_IRACING")

TRIGGER_TRAINING = _env_int("TRIGGER_TRAINING")
CATEGORY_TRAINING = _env_int("CATEGORY_TRAINING")

TRIGGER_LIVE = _env_int("TRIGGER_LIVE")
CATEGORY_LIVE = _env_int("CATEGORY_LIVE")

# ---- presets ----
GROUPS = {
    "iracing": {
        "label": "IRACING VOICE CHANNELS",
        "trigger_id": TRIGGER_IRACING,
        "category_id": CATEGORY_IRACING,
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
        "trigger_id": TRIGGER_TRAINING,
        "category_id": CATEGORY_TRAINING,
        "presets": [
            "Training",
            "Training I",
            "Training II",
            "Training III",
        ],
    },
    "live": {
        "label": "LIVE ON STREAM",
        "trigger_id": TRIGGER_LIVE,
        "category_id": CATEGORY_LIVE,
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

DELETE_DELAY_SECONDS = 15.0
PICKER_TIMEOUT_SECONDS = 90.0
PENDING_TTL_SECONDS = 180.0  # user has 3 minutes after joining trigger to pick a name

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

# pending: user_id -> (guild_id, group_key, expires_at_monotonic)
pending: Dict[int, Tuple[int, str, float]] = {}

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

def group_for_trigger(channel_id: int) -> Optional[str]:
    for key, g in GROUPS.items():
        if g["trigger_id"] == channel_id:
            return key
    return None

def used_voice_names_in_category(category: discord.CategoryChannel) -> set[str]:
    return {ch.name for ch in category.channels if isinstance(ch, discord.VoiceChannel)}

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

def pending_ok(user_id: int, guild_id: int) -> Optional[str]:
    item = pending.get(user_id)
    if not item:
        return None
    g_id, group_key, expires = item
    if g_id != guild_id:
        return None
    if asyncio.get_event_loop().time() > expires:
        pending.pop(user_id, None)
        return None
    return group_key

async def send_temporary_message(
    channel: discord.TextChannel,
    content: str,
    delay: float = 15.0,
):
    try:
        msg = await channel.send(content)
    except Exception:
        return

    async def _deleter():
        try:
            await asyncio.sleep(delay)
            await msg.delete()
        except Exception:
            pass

    asyncio.create_task(_deleter())

# ---------- UI ----------
class NamePickerView(discord.ui.View):
    def __init__(self, user_id: int, guild_id: int, group_key: str):
        super().__init__(timeout=PICKER_TIMEOUT_SECONDS)
        self.user_id = user_id
        self.guild_id = guild_id
        self.group_key = group_key

        self.name_select = discord.ui.Select(
            placeholder="Pick a channel name",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="__noop__")],
        )
        self.name_select.callback = self.on_name_selected
        self.add_item(self.name_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This picker is not for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        # Don’t force-clear pending here; TTL handles it
        pass

    async def refresh_options(self) -> bool:
        guild = bot.get_guild(self.guild_id)
        if guild is None:
            return False

        g = GROUPS[self.group_key]
        category = guild.get_channel(g["category_id"])
        if category is None or not isinstance(category, discord.CategoryChannel):
            return False

        used = used_voice_names_in_category(category)
        available = [n for n in g["presets"] if n not in used]

        if not available:
            self.name_select.options = [discord.SelectOption(label="No free names available", value="__noop__")]
            return True

        self.name_select.options = [discord.SelectOption(label=n, value=n) for n in available]
        return True

    async def on_name_selected(self, interaction: discord.Interaction):
        chosen_name = self.name_select.values[0]
        if chosen_name == "__noop__":
            await interaction.response.send_message("No channel name available right now.", ephemeral=True)
            return

        # Must still be pending for that group
        group_key = pending_ok(self.user_id, self.guild_id)
        if group_key != self.group_key:
            await interaction.response.send_message("Join a trigger voice channel again, then retry.", ephemeral=True)
            return

        guild = bot.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("I can’t access that server anymore.", ephemeral=True)
            pending.pop(self.user_id, None)
            self.stop()
            return

        try:
            member = await guild.fetch_member(self.user_id)
        except Exception:
            await interaction.response.send_message("I can’t fetch your member info.", ephemeral=True)
            return

        # Must still be in the matching trigger channel
        g = GROUPS[self.group_key]
        if not member.voice or not member.voice.channel or member.voice.channel.id != g["trigger_id"]:
            await interaction.response.send_message(
                "Please stay in the trigger voice channel, then pick again.",
                ephemeral=True,
            )
            return

        category = guild.get_channel(g["category_id"])
        if category is None or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Category ID is wrong or missing.", ephemeral=True)
            return

        # Race check
        used = used_voice_names_in_category(category)
        if chosen_name in used:
            await interaction.response.send_message("That name was just taken. Open the picker again.", ephemeral=True)
            return


        try:
            created = await guild.create_voice_channel(
                name=chosen_name,
                category=category,
                reason=f"Create preset temp voice channel ({self.group_key})",
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

class SharedPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Pick a channel name", style=discord.ButtonStyle.primary, custom_id="open_name_picker")
    async def open_picker(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return

        user_id = interaction.user.id
        group_key = pending_ok(user_id, guild_id)
        if not group_key:
            await interaction.response.send_message(
                "Join one of the trigger voice channels first, then click again.",
                ephemeral=True,
            )
            return

        view = NamePickerView(user_id=user_id, guild_id=guild_id, group_key=group_key)
        ok = await view.refresh_options()
        if not ok:
            await interaction.response.send_message("Config error: missing category or perms.", ephemeral=True)
            return

        label = GROUPS[group_key]["label"]
        await interaction.response.send_message(
            f"{label}\nPick a channel name:",
            view=view,
            ephemeral=True,
        )

PANEL_VIEW: SharedPanelView | None = None

@bot.event
async def setup_hook():
    global PANEL_VIEW
    PANEL_VIEW = SharedPanelView()
    bot.add_view(PANEL_VIEW)  # registers persistent view (timeout=None)


async def ensure_panel_message(channel: discord.TextChannel):
    if PANEL_VIEW is None:
        return
    async for msg in channel.history(limit=30):
        if msg.author == bot.user and msg.components:
            return
    await channel.send(
        "Join a trigger voice channel, then click the button to pick a channel name.",
        view=PANEL_VIEW,
    )

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")
    if PANEL_VIEW is None:
        return
    bot.add_view(PANEL_VIEW)

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

    # Ensure shared panel message exists
    for guild in bot.guilds:
        panel = guild.get_channel(PANEL_CHANNEL_ID)
        if isinstance(panel, discord.TextChannel):
            try:
                await ensure_panel_message(panel)
            except Exception:
                pass

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Cleanup when leaving a temp channel
    if before.channel is not None and before.channel != after.channel:
        if isinstance(before.channel, discord.VoiceChannel):
            await schedule_delete_if_empty(before.channel)

    # Joined a trigger?
    if after.channel is None:
        return

    group_key = group_for_trigger(after.channel.id)
    if not group_key:
        return
    if member.bot:
        return

    # Save pending with TTL
    expires = asyncio.get_event_loop().time() + PENDING_TTL_SECONDS
    pending[member.id] = (member.guild.id, group_key, expires)

    # Ping them in the shared panel channel
    panel = member.guild.get_channel(PANEL_CHANNEL_ID)
    if isinstance(panel, discord.TextChannel):
        label = GROUPS[group_key]["label"]
        await send_temporary_message(
            panel,
            f"{member.mention} {label}: click **Pick a channel name** above.",
            delay=15.0,  # change to 10–20 if you want
        )


bot.run(TOKEN)
