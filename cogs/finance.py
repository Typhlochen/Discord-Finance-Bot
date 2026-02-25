from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database as db

EXPIRY_DAYS = 1

CONFIRM_ID = "confirm_debt"
DENY_ID    = "deny_debt"


class ConfirmDebtView(discord.ui.View):
    """
    Persistent view attached to every pending debt request message.
    Uses static custom_ids so it survives bot restarts.
    State is looked up from the database by message_id.
    """

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Confirm", style=discord.ButtonStyle.green, custom_id=CONFIRM_ID
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        request = await db.get_pending_request(
            self.bot.db_pool, interaction.message.id
        )
        if request is None:
            await interaction.response.send_message(
                "This request has already been resolved.", ephemeral=True
            )
            return

        if interaction.user.id != request["debtor_id"]:
            await interaction.response.send_message(
                "Only the person being charged can confirm this request.",
                ephemeral=True,
            )
            return

        await db.add_debt(
            self.bot.db_pool,
            creditor_id=request["creditor_id"],
            debtor_id=request["debtor_id"],
            amount=float(request["amount"]),
            note=request["note"],
        )
        await db.delete_pending_request(self.bot.db_pool, interaction.message.id)

        note_text = f" \u2014 *{request['note']}*" if request["note"] else ""
        embed = discord.Embed(
            title="Debt Confirmed",
            description=(
                f"<@{request['debtor_id']}> confirmed owing "
                f"**${float(request['amount']):,.2f}** "
                f"to <@{request['creditor_id']}>{note_text}."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(
        label="Deny", style=discord.ButtonStyle.red, custom_id=DENY_ID
    )
    async def deny(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        request = await db.get_pending_request(
            self.bot.db_pool, interaction.message.id
        )
        if request is None:
            await interaction.response.send_message(
                "This request has already been resolved.", ephemeral=True
            )
            return

        if interaction.user.id != request["debtor_id"]:
            await interaction.response.send_message(
                "Only the person being charged can deny this request.",
                ephemeral=True,
            )
            return

        await db.delete_pending_request(self.bot.db_pool, interaction.message.id)

        embed = discord.Embed(
            title="Request Denied",
            description=(
                f"<@{request['debtor_id']}> denied the request of "
                f"**${float(request['amount']):,.2f}** "
                f"from <@{request['creditor_id']}>."
            ),
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=embed, view=None)


class Finance(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.check_pending_requests.start()

    def cog_unload(self) -> None:
        self.check_pending_requests.cancel()

    # ------------------------------------------------------------------
    # Background task — runs every 5 minutes
    # ------------------------------------------------------------------
    @tasks.loop(minutes=5)
    async def check_pending_requests(self) -> None:
        # 1. Send 1-hour warning to debtors who haven't been reminded
        to_remind = await db.get_requests_to_remind(self.bot.db_pool)
        for req in to_remind:
            channel = self.bot.get_channel(req["channel_id"])
            if channel:
                expires_ts = int(req["expires_at"].timestamp())
                await channel.send(
                    f"<@{req['debtor_id']}>, you have less than **1 hour** to respond to a "
                    f"debt request of **${float(req['amount']):,.2f}** from "
                    f"<@{req['creditor_id']}>! It expires <t:{expires_ts}:R>."
                )
            await db.mark_reminded(self.bot.db_pool, req["message_id"])

        # 2. Expire overdue requests
        expired = await db.get_expired_requests(self.bot.db_pool)
        for req in expired:
            channel = self.bot.get_channel(req["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(req["message_id"])
                    embed = discord.Embed(
                        title="Request Expired",
                        description=(
                            f"<@{req['creditor_id']}>'s request of "
                            f"**${float(req['amount']):,.2f}** from "
                            f"<@{req['debtor_id']}> expired without a response."
                        ),
                        color=discord.Color.light_grey(),
                    )
                    await msg.edit(embed=embed, view=None)
                except (discord.NotFound, discord.Forbidden):
                    pass
            await db.delete_pending_request(self.bot.db_pool, req["message_id"])

    @check_pending_requests.before_loop
    async def before_check(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # /request
    # ------------------------------------------------------------------
    @app_commands.command(
        name="request",
        description="Request money that a member owes you.",
    )
    @app_commands.describe(
        member="The member who owes you money",
        amount="How much they owe (e.g. 12.50)",
        note="Optional note (e.g. 'dinner last Friday')",
    )
    async def request(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: float,
        note: str | None = None,
    ) -> None:
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot request money from yourself.", ephemeral=True
            )
            return

        if amount <= 0:
            await interaction.response.send_message(
                "Amount must be greater than 0.", ephemeral=True
            )
            return

        expires_at = datetime.now(timezone.utc) + timedelta(days=EXPIRY_DAYS)
        expires_ts = int(expires_at.timestamp())

        note_text = f" \u2014 *{note}*" if note else ""
        embed = discord.Embed(
            title="Debt Confirmation Pending",
            description=(
                f"{member.mention}, {interaction.user.mention} is requesting "
                f"**${amount:,.2f}** from you{note_text}.\n\n"
                f"Press **Confirm** if you agree, or **Deny** to reject.\n"
                f"Expires <t:{expires_ts}:R>."
            ),
            color=discord.Color.yellow(),
        )

        view = ConfirmDebtView(self.bot)
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()

        await db.add_pending_request(
            self.bot.db_pool,
            message_id=msg.id,
            channel_id=msg.channel.id,
            creditor_id=interaction.user.id,
            debtor_id=member.id,
            amount=round(amount, 2),
            note=note,
            expires_at=expires_at,
        )

    # ------------------------------------------------------------------
    # /pay
    # ------------------------------------------------------------------
    @app_commands.command(
        name="pay",
        description="Pay off money you owe to a member.",
    )
    @app_commands.describe(
        member="The member you are paying",
        amount="How much you are paying (e.g. 12.50)",
    )
    async def pay(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: float,
    ) -> None:
        # Defer immediately so Discord doesn't time out during the DB call
        await interaction.response.defer()

        if member.id == interaction.user.id:
            await interaction.followup.send("You cannot pay yourself.", ephemeral=True)
            return

        if amount <= 0:
            await interaction.followup.send("Amount must be greater than 0.", ephemeral=True)
            return

        overpayment = await db.apply_payment(
            self.bot.db_pool,
            creditor_id=member.id,
            debtor_id=interaction.user.id,
            amount=round(amount, 2),
        )

        if overpayment >= round(amount, 2):
            await interaction.followup.send(
                f"You have no recorded debt to {member.mention}.",
                ephemeral=True,
            )
            return

        paid = round(amount - overpayment, 2)
        lines = [
            f"{interaction.user.mention} paid **${paid:,.2f}** to {member.mention}."
        ]
        if overpayment > 0:
            lines.append(
                f"Note: **${overpayment:,.2f}** could not be applied because it exceeded your remaining debt."
            )

        embed = discord.Embed(
            title="Payment Applied",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /debts
    # ------------------------------------------------------------------
    @app_commands.command(
        name="debts",
        description="See how much you owe and how much you are owed.",
    )
    async def debts(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id

        owed_to_me = await db.get_owed_to_user(self.bot.db_pool, user_id)
        i_owe = await db.get_owed_by_user(self.bot.db_pool, user_id)

        embed = discord.Embed(
            title=f"Debts for {interaction.user.display_name}",
            color=discord.Color.gold(),
        )

        if owed_to_me:
            lines = []
            total = 0.0
            for row in owed_to_me:
                member = interaction.guild.get_member(row["debtor_id"])
                name = member.mention if member else f"<@{row['debtor_id']}>"
                lines.append(f"{name} owes you **${float(row['total']):,.2f}**")
                total += float(row["total"])
            lines.append(f"\nTotal owed to you: **${total:,.2f}**")
            embed.add_field(name="They owe you", value="\n".join(lines), inline=False)
        else:
            embed.add_field(
                name="They owe you", value="Nobody owes you anything.", inline=False
            )

        if i_owe:
            lines = []
            total = 0.0
            for row in i_owe:
                member = interaction.guild.get_member(row["creditor_id"])
                name = member.mention if member else f"<@{row['creditor_id']}>"
                lines.append(f"You owe {name} **${float(row['total']):,.2f}**")
                total += float(row["total"])
            lines.append(f"\nTotal you owe: **${total:,.2f}**")
            embed.add_field(name="You owe", value="\n".join(lines), inline=False)
        else:
            embed.add_field(
                name="You owe", value="You don't owe anyone anything.", inline=False
            )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Finance(bot))
