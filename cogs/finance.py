from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database as db

EXPIRY_DAYS = 1

CONFIRM_ID         = "confirm_debt"
DENY_ID            = "deny_debt"
CONFIRM_PAY_ID     = "confirm_payment"
DENY_PAY_ID        = "deny_payment"


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


class ConfirmPaymentView(discord.ui.View):
    """Persistent view for payment confirmations. Only the creditor can respond."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Confirm Receipt", style=discord.ButtonStyle.green, custom_id=CONFIRM_PAY_ID
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        payment = await db.get_pending_payment(
            self.bot.db_pool, interaction.message.id
        )
        if payment is None:
            await interaction.response.send_message(
                "This payment has already been resolved.", ephemeral=True
            )
            return

        if interaction.user.id != payment["creditor_id"]:
            await interaction.response.send_message(
                "Only the person receiving the payment can confirm it.",
                ephemeral=True,
            )
            return

        overpayment = await db.apply_payment(
            self.bot.db_pool,
            creditor_id=payment["creditor_id"],
            debtor_id=payment["debtor_id"],
            amount=float(payment["amount"]),
        )
        await db.delete_pending_payment(self.bot.db_pool, interaction.message.id)

        paid = round(float(payment["amount"]) - overpayment, 2)
        embed = discord.Embed(
            title="Payment Confirmed",
            description=(
                f"<@{payment['creditor_id']}> confirmed receiving "
                f"**${paid:,.2f}** from <@{payment['debtor_id']}>."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(
        label="Deny", style=discord.ButtonStyle.red, custom_id=DENY_PAY_ID
    )
    async def deny(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        payment = await db.get_pending_payment(
            self.bot.db_pool, interaction.message.id
        )
        if payment is None:
            await interaction.response.send_message(
                "This payment has already been resolved.", ephemeral=True
            )
            return

        if interaction.user.id != payment["creditor_id"]:
            await interaction.response.send_message(
                "Only the person receiving the payment can deny it.",
                ephemeral=True,
            )
            return

        await db.delete_pending_payment(self.bot.db_pool, interaction.message.id)

        embed = discord.Embed(
            title="Payment Denied",
            description=(
                f"<@{payment['creditor_id']}> denied the payment of "
                f"**${float(payment['amount']):,.2f}** from <@{payment['debtor_id']}>."
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

        # 3. Send 1-hour warning for pending payments
        payments_to_remind = await db.get_payments_to_remind(self.bot.db_pool)
        for pay in payments_to_remind:
            channel = self.bot.get_channel(pay["channel_id"])
            if channel:
                expires_ts = int(pay["expires_at"].timestamp())
                await channel.send(
                    f"<@{pay['creditor_id']}>, you have less than **1 hour** to confirm a "
                    f"payment of **${float(pay['amount']):,.2f}** from "
                    f"<@{pay['debtor_id']}>! It expires <t:{expires_ts}:R>."
                )
            await db.mark_payment_reminded(self.bot.db_pool, pay["message_id"])

        # 4. Expire overdue payments
        expired_payments = await db.get_expired_payments(self.bot.db_pool)
        for pay in expired_payments:
            channel = self.bot.get_channel(pay["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(pay["message_id"])
                    embed = discord.Embed(
                        title="Payment Expired",
                        description=(
                            f"<@{pay['debtor_id']}>'s payment of "
                            f"**${float(pay['amount']):,.2f}** to "
                            f"<@{pay['creditor_id']}> expired without confirmation."
                        ),
                        color=discord.Color.light_grey(),
                    )
                    await msg.edit(embed=embed, view=None)
                except (discord.NotFound, discord.Forbidden):
                    pass
            await db.delete_pending_payment(self.bot.db_pool, pay["message_id"])

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
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot pay yourself.", ephemeral=True
            )
            return

        if amount <= 0:
            await interaction.response.send_message(
                "Amount must be greater than 0.", ephemeral=True
            )
            return

        expires_at = datetime.now(timezone.utc) + timedelta(days=EXPIRY_DAYS)
        expires_ts = int(expires_at.timestamp())

        embed = discord.Embed(
            title="Payment Confirmation Pending",
            description=(
                f"{member.mention}, {interaction.user.mention} is claiming to have paid "
                f"you **${amount:,.2f}**.\n\n"
                f"Press **Confirm Receipt** if you received it, or **Deny** if you didn't.\n"
                f"Expires <t:{expires_ts}:R>."
            ),
            color=discord.Color.yellow(),
        )

        view = ConfirmPaymentView(self.bot)
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()

        await db.add_pending_payment(
            self.bot.db_pool,
            message_id=msg.id,
            channel_id=msg.channel.id,
            creditor_id=member.id,
            debtor_id=interaction.user.id,
            amount=round(amount, 2),
            expires_at=expires_at,
        )

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
