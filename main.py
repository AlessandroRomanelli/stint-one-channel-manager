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
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")

def _env_int(name: str) -> int:
    val = os.getenv(name)
    if not val or not val.isdigit():
        raise RuntimeError(f"Missing/invalid {name} in .env")
    return int(val)

CATEGORY_IRACING = _env_int("CATEGORY_IRACING")
TRIGGER_IRACING = _env_int("TRIGGER_IRACING")

CATEGORY_TRAINING = _env_int("CATEGORY_TRAINING")
TRIGGER_TRAINING = _env_int("TRIGGER_TRAINING")

CATEGORY_LIVE = _env_int("CATEGORY_LIVE")
TRIGGER_LIVE = _env_int("TRIGGER_LIVE")

DATA_PATH = Path("data.json")

# ---- Your presets ----
PRESETS_IRACING = [
    "IRACING VOICE CHANNELS",
    "stintONE Motorsport",
    "stintONE Motorsport Black",
    "stintONE Motorsport White",
    "stintONE Motorsport Blue",
    "stintONE Motorsport Gold",
    "stintONE Motorsport Silver",
]

PRESETS_TRAINING = [
    "IRACING TRAINING AREA",
    "Training",
    "Training I",
    "Training II",
    "Training III",
]

PRESETS_LIVE = [
    "LIVE ON STREAM",
    "stintONE Motorsport LIVE",
    "stintONE Black LIVE",
    "stintONE White LIVE",
    "stintONE Blue LIVE",
    "stintONE Gold LIVE",
    "stintONE Silver LIVE",
    "stintONE Rosé LIVE",
]

# If you *don’t* want the header titles to be used as actual channel names, keep this True.
SKIP_HEADERS = True
DELETE_DELAY_SECONDS = 15.0

HEADER_NAMES = {"IRACING VOICE CHANNELS", "IRACING TRAINING AREA", "LIVE ON STREAM"}


@dataclass(frozen=True)
class GroupConfig:
    name: str
    category_id: int
    trigger_channel_id: int
    presets: list[str]


GROUPS: list[GroupConfig] = [
    GroupConfig("iracing", CATEGORY_IRACING, TRIGGER_IRACING, PRESETS_IRACING),
    GroupConfig("training", CATEGORY_TRAINING, TRIGGER_TRAINING, PRESETS_TRAINING),
    GroupConfig("live", CATEGORY_LIVE, TRIGGER_LIVE, PRESETS_LIVE),
]

# ---- persistence ----
def load_data() -> dict:
    if not DATA_PATH.exists():
        return {"temp_channel_ids": [], "allocations": {}}  # allocations: channel_id -> preset_name
    try:
        return json.loads(DATA_PATH.read_text("utf-8"))
    except Exception:
        return {"temp_channel_ids": [], "allocations": {}}

def save_data(payload: dict) -> None:
    DATA_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


state = load_data()
temp_channel_ids: set[int] = set(state.get("temp_channel_ids", []))
allocations: dict[str, str] = dict(state.get("allocations", {}))  # str(channel_id) -> preset_name
delete_tasks: dict[int, asyncio.Task] = {}


def persist():
    save_data(
        {
            "temp_channel_ids": sorted(temp_channel_ids),
            "allocations": allocations,
        }
    )


# ---- discord ----
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


def group_for_trigger(channel_id: int) -> Optional[GroupConfig]:
    for g in GROUPS:
        if g.trigger_channel_id == channel_id:
            return g
    return None


def normalize_presets(presets: list[str]) -> list[str]:
    if not SKIP_HEADERS:
        return presets
    return [p for p in presets if p not in HEADER_NAMES]


def active_preset_names_in_category(category: discord.CategoryChannel) -> set[str]:
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
                # already deleted or not accessible
                temp_channel_ids.discard(channel.id)
                allocations.pop(str(channel.id), None)
                persist()
                return

            if len(fresh.members) > 0:
                return

            try:
                await fresh.delete(reason="Temp preset voice channel empty")
            except Exception:
                return

            temp_channel_ids.discard(channel.id)
            allocations.pop(str(channel.id), None)
            persist()
        finally:
            delete_tasks.pop(channel.id, None)

    delete_tasks[channel.id] = asyncio.create_task(_runner())


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")

    # Cleanup empty temp channels created earlier (after restart)
    removed = False
    for guild in bot.guilds:
        for cid in list(temp_channel_ids):
            ch = guild.get_channel(cid)
            if ch is None:
                temp_channel_ids.discard(cid)
                allocations.pop(str(cid), None)
                removed = True
                continue
            if isinstance(ch, discord.VoiceChannel) and len(ch.members) == 0:
                try:
                    await ch.delete(reason="Cleanup empty temp channel after restart")
                    temp_channel_ids.discard(cid)
                    allocations.pop(str(cid), None)
                    removed = True
                except Exception:
                    pass

    if removed:
        persist()


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # If member left a channel, maybe delete it
    if before.channel is not None and before.channel != after.channel:
        if isinstance(before.channel, discord.VoiceChannel):
            await schedule_delete_if_empty(before.channel)

    # If member joined a trigger, create a preset channel
    if after.channel is None:
        return

    group = group_for_trigger(after.channel.id)
    if not group:
        return

    if member.bot:
        return

    guild = member.guild
    category = guild.get_channel(group.category_id)
    if category is None or not isinstance(category, discord.CategoryChannel):
        return

    used_names = active_preset_names_in_category(category)
    presets = normalize_presets(group.presets)

    # Choose first free preset name that is not currently used as a voice channel name
    chosen_name = None
    for name in presets:
        if name not in used_names:
            chosen_name = name
            break

    if not chosen_name:
        # No free preset available; optionally you could DM the user or move them back.
        # For now: do nothing.
        return

    try:
        created = await guild.create_voice_channel(
            name=chosen_name,
            category=category,
            reason=f"Create preset temp voice channel ({group.name})",
        )

        temp_channel_ids.add(created.id)
        allocations[str(created.id)] = chosen_name
        persist()

        await member.move_to(created, reason="Move to preset temp channel")
    except Exception as e:
        print("Failed to create/move:", repr(e))


bot.run(TOKEN)
