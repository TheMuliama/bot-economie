import os

import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")


if TOKEN is None:
    raise RuntimeError(
        "DISCORD_TOKEN absent du fichier .env"
    )

if GUILD_ID is None:
    raise RuntimeError(
        "GUILD_ID absent du fichier .env"
    )

GUILD_ID = int(GUILD_ID)


intents = discord.Intents.default()

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)

    await tree.sync(guild=guild)

    print("==============================")
    print("M CORP BOT CONNECTÉ")
    print(f"Nom : {client.user}")
    print(f"ID  : {client.user.id}")
    print("==============================")


@tree.command(
    name="mcorp-status",
    description="Afficher le statut de M Corp",
    guild=discord.Object(id=GUILD_ID)
)
async def mcorp_status(
    interaction: discord.Interaction
):
    await interaction.response.send_message(
        "M Corp est opérationnelle.",
        ephemeral=True
    )


client.run(TOKEN)
