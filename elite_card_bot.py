"""
ELT Elite Card Bot  (v1)
=======================================
An application panel in the same idea as a "Student Card" panel, but made for ELT:

    Members press "Get your Card" -> fill a short form -> moderators review it
    -> on Accept the member gets the Elite Card role (which opens the Elite Lounge category).

What it does
    /cardpanel   (admin)  posts the panel (embed + banner + button) in the current channel
    /removecard  (mods)   takes the Elite Card role away from a member and clears their record

    - The form has 3 big paragraph boxes. The review card shows every answer with a big ## heading.
    - Review buttons: Accept / Interview / Deny (Deny asks for a reason and DMs it to the member).
    - One open application per member. After a Deny the member must wait DENY_COOLDOWN_HOURS.
    - Members who already have the role can't apply again.
    - Everything survives restarts (persistent buttons + a small JSON file).

Requirements:
    pip install "discord.py>=2.4"

Before running:
    - Create the Elite Card role and the review channel, then set CARD_ROLE_ID and REVIEW_CHANNEL_ID
      (below, or as environment variables in Railway > Variables).
    - Make the Elite Lounge category visible ONLY to that role (the bot just hands out the role).
    - SERVER MEMBERS INTENT enabled in the Developer Portal.
    - Invite with the "bot" and "applications.commands" scopes.
    - The bot needs Manage Roles, and its role must sit ABOVE the Elite Card role.
    - Put your banner picture next to this file as  card_banner.png  (or .jpg / .gif). Optional.
    - DISCORD_TOKEN env variable. This bot needs its OWN Discord application + token.
    - Railway: add a Volume mounted at /data so applications survive redeploys.
"""

from __future__ import annotations

import os
import re
import json
import time
import shutil
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

# =========================== CONFIG ===========================
TOKEN = os.environ.get("DISCORD_TOKEN")

GUILD_ID = 1410440666747633707  # ELT server ID
GUILD = discord.Object(id=GUILD_ID)


def _int_env(name: str, default: int = 0) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value.isdigit() else default


# >>> FILL THESE TWO IN (or set them as Railway variables) <<<
CARD_ROLE_ID = _int_env("CARD_ROLE_ID", 0)          # the role a member gets when they are accepted
REVIEW_CHANNEL_ID = _int_env("REVIEW_CHANNEL_ID", 0)  # where moderators review applications

MOD_ROLE_ID = 1513904125086011402    # moderators (and above) can review applications
ADMIN_ROLE_ID = 1513904120803889243  # high rank: can post the panel

# Names shown to members - change them here and everything updates.
CARD_NAME = "Elite Card"
LOUNGE_NAME = "Elite Lounge"

DENY_COOLDOWN_HOURS = 24      # how long a denied member waits before applying again
DM_APPLICANTS = True          # DM the member when their application is accepted / denied / interviewed

APPLY_EMOJI = "\U0001FAAA"    # 🪪  (the only emoji on the panel - the button)

PURPLE = discord.Color.purple()

HERE = os.path.dirname(os.path.abspath(__file__))
BANNER_CANDIDATES = [os.path.join(HERE, f"card_banner.{ext}") for ext in ("png", "jpg", "jpeg", "gif", "webp")]

DATA_DIR = (
    os.environ.get("DATA_DIR")
    or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or HERE
)
DB_PATH = os.path.join(DATA_DIR, "card_applications.json")
# ================================================================

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

# applications[member_id] = {"status": pending|interview|accepted|denied, "updated": unix seconds,
#                            "channel_id": int, "message_id": int}
applications: dict[int, dict] = {}
_apply_lock = asyncio.Lock()


# ====================== small database ======================
def load_db() -> dict[int, dict]:
    for path in (DB_PATH, DB_PATH + ".bak"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return {int(k): v for k, v in data.items()}
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"⚠️ Couldn't read {path}: {e}")
            try:
                shutil.copyfile(path, f"{path}.corrupt-{int(time.time())}")
            except Exception:
                pass
    return {}


def save_db():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        tmp = DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in applications.items()}, f, ensure_ascii=False)
        if os.path.exists(DB_PATH):
            try:
                shutil.copyfile(DB_PATH, DB_PATH + ".bak")
            except Exception:
                pass
        os.replace(tmp, DB_PATH)
    except Exception as e:
        print(f"❌ Couldn't save {DB_PATH}: {e}")


applications = load_db()
print(f"📁 Applications file: {DB_PATH} ({len(applications)} saved)")


# ====================== helpers ======================
def is_admin(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.id == member.guild.owner_id or member.guild_permissions.administrator:
        return True
    role = member.guild.get_role(ADMIN_ROLE_ID)
    return bool(role and member.top_role >= role)


def is_reviewer(member: discord.abc.User) -> bool:
    if is_admin(member):
        return True
    if not isinstance(member, discord.Member):
        return False
    role = member.guild.get_role(MOD_ROLE_ID)
    return bool(role and member.top_role >= role)


def get_field(embed: discord.Embed, name: str) -> str | None:
    for f in embed.fields:
        if f.name == name:
            return f.value
    return None


def set_field(embed: discord.Embed, name: str, value: str, inline: bool = True):
    for i, f in enumerate(embed.fields):
        if f.name == name:
            embed.set_field_at(i, name=name, value=value, inline=inline)
            return
    embed.add_field(name=name, value=value, inline=inline)


def setup_problem(guild: discord.Guild) -> str | None:
    """Returns a message if the bot hasn't been configured yet."""
    if not CARD_ROLE_ID or guild.get_role(CARD_ROLE_ID) is None:
        return f"The {CARD_NAME} role isn't set up yet — an admin needs to set CARD_ROLE_ID."
    if not REVIEW_CHANNEL_ID or not isinstance(guild.get_channel(REVIEW_CHANNEL_ID), discord.TextChannel):
        return "The review channel isn't set up yet — an admin needs to set REVIEW_CHANNEL_ID."
    return None


async def dm_applicant(member: discord.abc.User, guild: discord.Guild, title: str, text: str, reason: str | None = None):
    if not DM_APPLICANTS or getattr(member, "bot", False):
        return False
    embed = discord.Embed(title=title, description=text, color=PURPLE, timestamp=discord.utils.utcnow())
    if reason:
        embed.add_field(name="Reason", value=f">>> {reason}"[:1024], inline=False)
    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    try:
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# ====================== the panel ======================
def build_panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title=CARD_NAME,
        description=(
            f"Want access to the **{LOUNGE_NAME}**?\n"
            f"Apply for your **{CARD_NAME}** to get verified and unlock the elite-only category.\n"
            "\n"
            "### How it works\n"
            f"> 1. Press **Get your Card** and fill in the short form\n"
            "> 2. Tell us who you are and why you want in\n"
            "> 3. Our team reviews your application\n"
            f"> 4. Once accepted, you get the **{CARD_NAME}** role and the **{LOUNGE_NAME}** opens for you\n"
            "\n"
            "**Real answers only** — please be honest. Fake or careless applications will be denied.\n"
            "\n"
            f"**Ready for the {LOUNGE_NAME}?**\n"
            "Press the button below to apply."
        ),
        color=PURPLE,
    )
    embed.set_footer(
        text=f"ELITE LEADERS COMMUNITY • {CARD_NAME}",
        icon_url=guild.icon.url if guild.icon else None,
    )
    return embed


class ApplyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Get your Card",
        emoji=APPLY_EMOJI,
        style=discord.ButtonStyle.secondary,
        custom_id="elitecard_apply",
    )
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("Use this in the server.", ephemeral=True)

        problem = setup_problem(guild)
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)

        role = guild.get_role(CARD_ROLE_ID)
        if role in member.roles:
            return await interaction.response.send_message(f"You already have the **{CARD_NAME}**.", ephemeral=True)

        rec = applications.get(member.id)
        if rec and rec.get("status") in ("pending", "interview"):
            return await interaction.response.send_message(
                f"You already have an application waiting for review. Please be patient.", ephemeral=True
            )
        if rec and rec.get("status") == "denied":
            ready_at = rec.get("updated", 0) + DENY_COOLDOWN_HOURS * 3600
            if time.time() < ready_at:
                return await interaction.response.send_message(
                    f"Your last application was denied. You can apply again "
                    f"{discord.utils.format_dt(datetime.fromtimestamp(ready_at, tz=timezone.utc), 'R')}.",
                    ephemeral=True,
                )

        await interaction.response.send_modal(ApplyModal())


class ApplyModal(discord.ui.Modal, title=f"{CARD_NAME} Application"):
    who = discord.ui.TextInput(
        label="Name or nickname",
        style=discord.TextStyle.paragraph,
        placeholder="What should we call you?",
        required=True,
        max_length=300,
    )
    why = discord.ui.TextInput(
        label=f"Why do you want the {CARD_NAME}?",
        style=discord.TextStyle.paragraph,
        placeholder="Tell us a bit about yourself and why you should get in.",
        required=True,
        max_length=900,
    )
    extra = discord.ui.TextInput(
        label="Anything else we should know? (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Optional.",
        required=False,
        max_length=900,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        await interaction.response.defer(ephemeral=True)

        problem = setup_problem(guild) if guild else "Use this in the server."
        if problem:
            return await interaction.followup.send(problem, ephemeral=True)

        async with _apply_lock:
            rec = applications.get(member.id)
            if rec and rec.get("status") in ("pending", "interview"):
                return await interaction.followup.send("You already have an application waiting for review.", ephemeral=True)

            channel = guild.get_channel(REVIEW_CHANNEL_ID)
            embed = build_review_embed(member, self.who.value, self.why.value, self.extra.value)
            try:
                sent = await channel.send(embed=embed, view=review_view(member.id))
            except discord.HTTPException as e:
                print(f"❌ Couldn't post the application: {e}")
                return await interaction.followup.send(
                    "Something went wrong sending your application. Please try again in a moment.", ephemeral=True
                )

            applications[member.id] = {
                "status": "pending",
                "updated": time.time(),
                "channel_id": sent.channel.id,
                "message_id": sent.id,
            }
            save_db()

        await interaction.followup.send(
            f"Your **{CARD_NAME}** application was sent. A moderator will review it soon — "
            "you will get a DM when there is an answer.",
            ephemeral=True,
        )
        print(f"📨 {CARD_NAME} application from {member}")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"❌ Apply modal error: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong. Please try again.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)
        except discord.HTTPException:
            pass


# ====================== review card ======================
def build_review_embed(member: discord.Member, who: str, why: str, extra: str) -> discord.Embed:
    parts = [
        "## Applicant",
        f"{member.mention} • `{member.id}`",
        f"-# Account created {discord.utils.format_dt(member.created_at, 'R')}"
        + (f" • Joined server {discord.utils.format_dt(member.joined_at, 'R')}" if member.joined_at else ""),
        "",
        "## Name or nickname",
        f">>> {who.strip()}",
        "",
        f"## Why they want the {CARD_NAME}",
        f">>> {why.strip()}",
    ]
    if extra and extra.strip():
        parts += ["", "## Anything else", f">>> {extra.strip()}"]

    embed = discord.Embed(
        title=f"{CARD_NAME} Application",
        description="\n".join(parts)[:4000],
        color=PURPLE,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Pending", icon_url=member.guild.icon.url if member.guild.icon else None)
    embed.timestamp = discord.utils.utcnow()
    return embed


def _status_of(message: discord.Message | None) -> str | None:
    if message is None or not message.embeds:
        return None
    return message.embeds[0].footer.text


class ReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"elitecard_(?P<action>accept|interview|deny)_(?P<uid>[0-9]+)",
):
    def __init__(self, action: str, member_id: int, disabled: bool = False):
        if action == "accept":
            label, emoji = "Accept", "✅"
        elif action == "interview":
            label, emoji = "Interview", None   # no emoji on this one
        else:
            label, emoji = "Deny", "❌"
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"elitecard_{action}_{member_id}",
                disabled=disabled,
            )
        )
        self.action = action
        self.member_id = member_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"], int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        if not is_reviewer(interaction.user):
            return await interaction.response.send_message("Only moderators can review applications.", ephemeral=True)

        message = interaction.message
        status = _status_of(message)
        if message is None or not message.embeds:
            return await interaction.response.send_message("Couldn't find the application.", ephemeral=True)
        if status in ("Accepted", "Denied"):
            return await interaction.response.send_message("This application was already handled.", ephemeral=True)

        guild = interaction.guild

        if self.action == "deny":
            return await interaction.response.send_modal(DenyModal(self.member_id))

        embed = message.embeds[0].copy()

        if self.action == "interview":
            if status == "Interview":
                return await interaction.response.send_message("Already marked for interview.", ephemeral=True)
            embed.color = PURPLE
            set_field(embed, "Interview", interaction.user.mention)
            embed.set_footer(text="Interview", icon_url=embed.footer.icon_url)
            await interaction.response.edit_message(embed=embed, view=review_view(self.member_id, interview=True))
            _set_record(self.member_id, "interview", message)

            target = guild.get_member(self.member_id)
            if target is not None:
                await dm_applicant(
                    target, guild, f"{CARD_NAME} — interview",
                    f"A moderator looked at your **{CARD_NAME}** application in **{guild.name}** and would like to "
                    "talk to you first. Please keep your DMs open — someone will contact you shortly.",
                )
            return

        # accept
        await interaction.response.defer()
        target = guild.get_member(self.member_id)
        if target is None:
            try:
                target = await guild.fetch_member(self.member_id)
            except discord.HTTPException:
                target = None
        if target is None:
            return await interaction.followup.send("That member isn't in the server anymore.", ephemeral=True)

        role = guild.get_role(CARD_ROLE_ID)
        if role is None:
            return await interaction.followup.send("The Elite Card role wasn't found — check CARD_ROLE_ID.", ephemeral=True)
        try:
            await target.add_roles(role, reason=f"{CARD_NAME} accepted by {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                "I can't give that role — move my role above it and give me Manage Roles.", ephemeral=True
            )
        except discord.HTTPException as e:
            print(f"❌ Couldn't give the card role to {target}: {e}")
            return await interaction.followup.send("Something went wrong giving the role. Try again.", ephemeral=True)

        embed.color = PURPLE
        set_field(embed, "Accepted by", f"{interaction.user.mention} • {discord.utils.format_dt(discord.utils.utcnow(), 'R')}", inline=False)
        embed.set_footer(text="Accepted", icon_url=embed.footer.icon_url)
        await interaction.edit_original_response(embed=embed, view=review_view(self.member_id, disabled=True))
        _set_record(self.member_id, "accepted", message)

        await dm_applicant(
            target, guild, f"{CARD_NAME} — accepted",
            f"Congratulations! Your **{CARD_NAME}** application in **{guild.name}** was accepted. "
            f"The **{LOUNGE_NAME}** is now open for you.",
        )
        print(f"✅ {target} accepted for the {CARD_NAME} by {interaction.user}")


def _set_record(member_id: int, status: str, message: discord.Message | None):
    rec = applications.setdefault(member_id, {})
    rec["status"] = status
    rec["updated"] = time.time()
    if message is not None:
        rec["channel_id"] = message.channel.id
        rec["message_id"] = message.id
    save_db()


def review_view(member_id: int, interview: bool = False, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewButton("accept", member_id, disabled))
    view.add_item(ReviewButton("interview", member_id, disabled or interview))
    view.add_item(ReviewButton("deny", member_id, disabled))
    return view


class DenyModal(discord.ui.Modal, title="Deny Application"):
    reason = discord.ui.TextInput(
        label="Why is this application denied?",
        style=discord.TextStyle.paragraph,
        placeholder="The member gets this reason by DM.",
        required=True,
        max_length=400,
    )

    def __init__(self, member_id: int):
        super().__init__()
        self.member_id = member_id

    async def on_submit(self, interaction: discord.Interaction):
        message = interaction.message
        if message is None or not message.embeds:
            return await interaction.response.send_message("Couldn't find the application.", ephemeral=True)
        if _status_of(message) in ("Accepted", "Denied"):
            return await interaction.response.send_message("This application was already handled.", ephemeral=True)

        reason = self.reason.value.strip()
        embed = message.embeds[0].copy()
        embed.color = PURPLE
        set_field(embed, "Denied by", f"{interaction.user.mention} • {discord.utils.format_dt(discord.utils.utcnow(), 'R')}", inline=False)
        set_field(embed, "Reason", f">>> {reason}"[:1024], inline=False)
        embed.set_footer(text="Denied", icon_url=embed.footer.icon_url)
        await interaction.response.edit_message(embed=embed, view=review_view(self.member_id, disabled=True))
        _set_record(self.member_id, "denied", message)

        guild = interaction.guild
        target = guild.get_member(self.member_id) if guild else None
        sent = False
        if target is not None:
            sent = await dm_applicant(
                target, guild, f"{CARD_NAME} — not accepted",
                f"Your **{CARD_NAME}** application in **{guild.name}** was not accepted this time. "
                f"You can apply again after {DENY_COOLDOWN_HOURS} hours.",
                reason=reason,
            )
        await interaction.followup.send(
            "Denied. The member was sent the reason by DM." if sent
            else "Denied. I couldn't DM the member (their DMs are closed or they left).",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"❌ Deny modal error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)


# ====================== commands ======================
@bot.tree.command(name="cardpanel", description=f"Post the {CARD_NAME} application panel in this channel. High rank only.", guild=GUILD)
@app_commands.guild_only()
async def cardpanel_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("This command is for the high rank only.", ephemeral=True)

    problem = setup_problem(interaction.guild)
    await interaction.response.defer(ephemeral=True)

    embed = build_panel_embed(interaction.guild)
    kwargs = {}
    banner = next((p for p in BANNER_CANDIDATES if os.path.exists(p)), None)
    if banner:
        filename = os.path.basename(banner)
        kwargs["file"] = discord.File(banner, filename=filename)
        embed.set_image(url=f"attachment://{filename}")

    try:
        await interaction.channel.send(embed=embed, view=ApplyView(), **kwargs)
    except discord.HTTPException as e:
        print(f"❌ Couldn't post the panel: {e}")
        return await interaction.followup.send(
            "I couldn't post the panel here — check that I can send messages, embed links and attach files.",
            ephemeral=True,
        )

    notes = []
    if banner is None:
        notes.append("No `card_banner.png` found next to the bot, so the panel has no banner image.")
    if problem:
        notes.append(f"⚠️ {problem}")
    await interaction.followup.send("Panel posted." + ("\n" + "\n".join(notes) if notes else ""), ephemeral=True)


@bot.tree.command(name="removecard", description=f"Take the {CARD_NAME} role away from a member.", guild=GUILD)
@app_commands.describe(member=f"The member who loses the {CARD_NAME}")
@app_commands.guild_only()
async def removecard_cmd(interaction: discord.Interaction, member: discord.Member):
    if not is_reviewer(interaction.user):
        return await interaction.response.send_message("This command is for moderators or higher.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    role = interaction.guild.get_role(CARD_ROLE_ID)
    if role is None:
        return await interaction.followup.send("The Elite Card role wasn't found — check CARD_ROLE_ID.", ephemeral=True)
    if role not in member.roles:
        return await interaction.followup.send(f"{member.mention} doesn't have the {CARD_NAME}.", ephemeral=True)

    try:
        await member.remove_roles(role, reason=f"{CARD_NAME} removed by {interaction.user}")
    except discord.Forbidden:
        return await interaction.followup.send("I can't remove that role — check my role position.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.followup.send(f"Failed to remove the role: {e}", ephemeral=True)

    applications.pop(member.id, None)
    save_db()
    await interaction.followup.send(f"Removed the **{CARD_NAME}** from {member.mention}. They can apply again.", ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"❌ /{interaction.command.name if interaction.command else '?'} error: {type(error).__name__}: {error}")
    msg = "Something went wrong running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ====================== events ======================
@bot.event
async def setup_hook():
    bot.add_view(ApplyView())
    bot.add_dynamic_items(ReviewButton)
    try:
        synced = await bot.tree.sync(guild=GUILD)
        print(f"🔄 Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"❌ Command sync failed: {e}")


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(f"❌ Guild {GUILD_ID} not found!")
        return
    problem = setup_problem(guild)
    if problem:
        print(f"⚠️ {problem}")
    role = guild.get_role(CARD_ROLE_ID) if CARD_ROLE_ID else None
    if role is not None and guild.me and role >= guild.me.top_role:
        print(f"⚠️ The {CARD_NAME} role is ABOVE my role — move my role higher or I can't give it.")
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID")) and not (
        os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    ):
        print("🚨 NO VOLUME: applications are deleted on every redeploy. Add a Volume mounted at /data in Railway.")


@bot.event
async def on_member_remove(member: discord.Member):
    """A member who leaves with an open application: close it so the card doesn't stay pending."""
    if member.guild.id != GUILD_ID:
        return
    rec = applications.get(member.id)
    if not rec or rec.get("status") not in ("pending", "interview"):
        return
    channel = member.guild.get_channel(rec.get("channel_id", 0))
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(rec["message_id"])
        if msg.embeds and msg.embeds[0].footer.text not in ("Accepted", "Denied", "Member left"):
            embed = msg.embeds[0].copy()
            embed.set_footer(text="Member left", icon_url=embed.footer.icon_url)
            await msg.edit(embed=embed, view=review_view(member.id, disabled=True))
    except discord.HTTPException:
        pass
    applications.pop(member.id, None)
    save_db()


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in Railway's Variables tab, then redeploy.")

bot.run(TOKEN)
