from redbot.core import commands


class MCorp(commands.Cog):
    """Fonctionnalités principales de M Corp."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def mcorp(self, ctx):
        """Vérifie que M Corp fonctionne."""
        await ctx.send("M Corp est opérationnelle.")
