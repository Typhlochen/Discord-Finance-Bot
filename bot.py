import asyncio
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

import database as db
from cogs.finance import ConfirmDebtView, ConfirmPaymentView

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.db_pool = None

    async def setup_hook(self) -> None:
        self.db_pool = await db.create_pool()
        await db.init_db(self.db_pool)

        await self.load_extension("cogs.finance")

        # Re-attach persistent views so in-flight requests/payments survive restarts
        self.add_view(ConfirmDebtView(self))
        self.add_view(ConfirmPaymentView(self))

        # Sync slash commands globally (can take up to 1 hour to propagate).
        # For instant updates during development, sync to a specific guild:
        #   await self.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))
        await self.tree.sync()
        print("Slash commands synced.")

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (ID: {self.user.id})")

    async def on_error(self, event: str, *args, **kwargs) -> None:
        import traceback
        print(f"Unhandled error in {event}:")
        traceback.print_exc()

    async def on_tree_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        import traceback
        print(f"Slash command error in /{interaction.command.name if interaction.command else '?'}:")
        traceback.print_exc()
        msg = "An unexpected error occurred. Please try again."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

    async def close(self) -> None:
        if self.db_pool:
            await self.db_pool.close()
        await super().close()


async def main() -> None:
    async with Bot() as bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
