import discord
from discord import app_commands
from discord.ext import commands

import database as db


class Finance(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

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

        await db.add_debt(
            self.bot.db_pool,
            creditor_id=interaction.user.id,
            debtor_id=member.id,
            amount=round(amount, 2),
            note=note,
        )

        note_text = f" \u2014 *{note}*" if note else ""
        embed = discord.Embed(
            title="Money Requested",
            description=(
                f"{interaction.user.mention} has requested "
                f"**${amount:,.2f}** from {member.mention}{note_text}."
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed)

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

        overpayment = await db.apply_payment(
            self.bot.db_pool,
            creditor_id=member.id,
            debtor_id=interaction.user.id,
            amount=round(amount, 2),
        )

        if overpayment >= round(amount, 2):
            # Nothing was reduced — no debt exists
            await interaction.response.send_message(
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
        await interaction.response.send_message(embed=embed)

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
            embed.add_field(
                name="They owe you",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="They owe you",
                value="Nobody owes you anything.",
                inline=False,
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
            embed.add_field(
                name="You owe",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="You owe",
                value="You don't owe anyone anything.",
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Finance(bot))
