"""
ELT Elite Study Card Bot  (v2)
=======================================
A simple panel: a member presses "Get your Card" and instantly gets the Elite Study Card role.

Commands
    !cardpanel            (admin)  posts the panel (embed + banner + button) in the current channel
    !removecard @member   (mods)   takes the Elite Study Card role away from a member

Requirements:
    pip install "discord.py>=2.4"

Before running:
    - Make the study category visible ONLY to the role (the bot just hands out the role).
    - SERVER MEMBERS INTENT and MESSAGE CONTENT INTENT enabled in the Developer Portal
      (Bot > Privileged Gateway Intents).
    - The bot needs: View Channel, Send Messages, Embed Links, Attach Files, Manage Messages
      (it deletes your !cardpanel message to keep the channel clean) and Manage Roles.
    - The bot's role must sit ABOVE the Elite Study Card role.
    - Put your banner picture next to this file as  card_banner.png  (or .jpg / .gif). Optional.
    - DISCORD_TOKEN env variable. This bot needs its OWN Discord application + token.
"""

from __future__ import annotations

import os

import discord
from discord.ext import commands

# =========================== CONFIG ===========================
TOKEN = os.environ.get("DISCORD_TOKEN")

GUILD_ID = 1410440666747633707  # ELT server ID


def _int_env(name: str, default: int = 0) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value.isdigit() else default


# The role a member gets when they press the button
CARD_ROLE_ID = _int_env("CARD_ROLE_ID", 1547024762444714004)

MOD_ROLE_ID = 1513904125086011402    # moderators (and above) can use !removecard
ADMIN_ROLE_ID = 1513904120803889243  # high rank: can post the panel

# Names shown to members - change them here and everything updates.
CARD_NAME = "Elite Study Card"
AREA_NAME = "Elite Study"

APPLY_EMOJI = "\U0001FAAA"  # the only emoji on the panel - the button

PURPLE = discord.Color.purple()

HERE = os.path.dirname(os.path.abspath(__file__))
BANNER_CANDIDATES = [os.path.join(HERE, f"card_banner.{ext}") for ext in ("png", "jpg", "jpeg", "gif", "webp")]
# ================================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # needed for the "!" commands (enable it in the Developer Portal too)

bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents, help_command=None)


# ====================== helpers ======================
def is_admin(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.id == member.guild.owner_id or member.guild_permissions.administrator:
        return True
    role = member.guild.get_role(ADMIN_ROLE_ID)
    return bool(role and member.top_role >= role)


def is_mod(member: discord.abc.User) -> bool:
    if is_admin(member):
        return True
    if not isinstance(member, discord.Member):
        return False
    role = member.guild.get_role(MOD_ROLE_ID)
    return bool(role and member.top_role >= role)


def role_problem(guild: discord.Guild) -> str | None:
    """Returns a message if the role isn't set up correctly, otherwise None."""
    role = guild.get_role(CARD_ROLE_ID)
    if role is None:
        return f"The {CARD_NAME} role wasn't found - check CARD_ROLE_ID."
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return "I'm missing the Manage Roles permission."
    if role >= me.top_role:
        return f"The {CARD_NAME} role is above my role - move my role higher."
    return None


# ====================== the panel ======================
def build_panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title=CARD_NAME,
        description=(
            f"Want access to **{AREA_NAME}**?\n"
            f"Get your **{CARD_NAME}** and unlock the study-only category.\n"
            "\n"
            "### How it works\n"
            "> 1. Press **Get your Card** below\n"
            f"> 2. You instantly get the **{CARD_NAME}** role\n"
            f"> 3. **{AREA_NAME}** opens for you\n"
            "\n"
            "**Respect the rules** - the card can be removed at any time.\n"
            "\n"
            "Press the button below to get your card."
        ),
        color=PURPLE,
    )
    embed.set_footer(
        text=f"ELITE LEADERS COMMUNITY • {CARD_NAME}",
        icon_url=guild.icon.url if guild.icon else None,
    )
    return embed


class CardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Get your Card",
        emoji=APPLY_EMOJI,
        style=discord.ButtonStyle.secondary,
        custom_id="elitestudycard_get",
    )
    async def get_card(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("Use this in the server.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        problem = role_problem(guild)
        if problem:
            print(f"[card] ERROR: {problem}")
            return await interaction.followup.send(
                "The card isn't working right now. Please tell a staff member.", ephemeral=True
            )

        role = guild.get_role(CARD_ROLE_ID)
        if role in member.roles:
            return await interaction.followup.send(f"You already have the **{CARD_NAME}**.", ephemeral=True)

        try:
            await member.add_roles(role, reason=f"{CARD_NAME} button")
        except discord.Forbidden:
            print("[card] ERROR: Forbidden while adding the role (check my role position / Manage Roles)")
            return await interaction.followup.send(
                "I couldn't give you the card. Please tell a staff member.", ephemeral=True
            )
        except discord.HTTPException as e:
            print(f"[card] ERROR: couldn't give the role to {member}: {e}")
            return await interaction.followup.send("Something went wrong. Please try again.", ephemeral=True)

        print(f"[card] {member} got the {CARD_NAME}")
        await interaction.followup.send(
            f"You got the **{CARD_NAME}**. **{AREA_NAME}** is now open for you.", ephemeral=True
        )


# ====================== commands (prefix: !) ======================
async def _tidy(ctx: commands.Context):
    """Deletes the message that ran the command (needs Manage Messages; silently skipped otherwise)."""
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass


@bot.command(name="cardpanel")
@commands.guild_only()
async def cardpanel_cmd(ctx: commands.Context):
    """!cardpanel - posts the Elite Study Card panel in this channel (high rank only)."""
    if not is_admin(ctx.author):
        return await ctx.reply("This command is for the high rank only.", delete_after=8)

    embed = build_panel_embed(ctx.guild)
    kwargs = {}
    banner = next((p for p in BANNER_CANDIDATES if os.path.exists(p)), None)
    if banner:
        filename = os.path.basename(banner)
        kwargs["file"] = discord.File(banner, filename=filename)
        embed.set_image(url=f"attachment://{filename}")

    try:
        await ctx.channel.send(embed=embed, view=CardView(), **kwargs)
    except discord.HTTPException as e:
        print(f"[card] ERROR: couldn't post the panel: {e}")
        return await ctx.reply(
            "I couldn't post the panel here - check that I can send messages, embed links and attach files.",
            delete_after=15,
        )

    await _tidy(ctx)
    notes = []
    if banner is None:
        notes.append("No `card_banner.png` found next to the bot, so the panel has no banner image.")
    problem = role_problem(ctx.guild)
    if problem:
        notes.append(f"Warning: {problem}")
    if notes:
        await ctx.send("\n".join(notes), delete_after=20)


@bot.command(name="removecard")
@commands.guild_only()
async def removecard_cmd(ctx: commands.Context, member: discord.Member):
    """!removecard @member - takes the Elite Study Card role away (moderators or higher)."""
    if not is_mod(ctx.author):
        return await ctx.reply("This command is for moderators or higher.", delete_after=8)

    role = ctx.guild.get_role(CARD_ROLE_ID)
    if role is None:
        return await ctx.reply(f"The {CARD_NAME} role wasn't found - check CARD_ROLE_ID.", delete_after=10)
    if role not in member.roles:
        return await ctx.reply(
            f"{member.mention} doesn't have the {CARD_NAME}.",
            delete_after=10,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    try:
        await member.remove_roles(role, reason=f"{CARD_NAME} removed by {ctx.author}")
    except discord.Forbidden:
        return await ctx.reply("I can't remove that role - check my role position.", delete_after=10)
    except discord.HTTPException as e:
        return await ctx.reply(f"Failed to remove the role: {e}", delete_after=10)

    await ctx.reply(
        f"Removed the **{CARD_NAME}** from {member.mention}.",
        delete_after=15,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.CommandNotFound, commands.NoPrivateMessage)):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.reply("Usage: `!removecard @member`", delete_after=10)
    if isinstance(error, (commands.MemberNotFound, commands.BadArgument)):
        return await ctx.reply("I couldn't find that member.", delete_after=10)
    print(f"[card] ERROR: !{ctx.command} {type(error).__name__}: {error}")


# ====================== events ======================
@bot.event
async def setup_hook():
    bot.add_view(CardView())  # keeps the button working after restarts


@bot.event
async def on_ready():
    print(f"[card] Logged in as {bot.user} (ID: {bot.user.id})")
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(f"[card] ERROR: guild {GUILD_ID} not found")
        return
    problem = role_problem(guild)
    print(f"[card] WARN: {problem}" if problem else f"[card] OK: role {CARD_ROLE_ID} is ready")


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in Railway's Variables tab, then redeploy.")

bot.run(TOKEN)
