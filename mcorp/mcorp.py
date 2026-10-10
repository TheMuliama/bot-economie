from redbot.core import commands, bank


class MCorp(commands.Cog):
    """Fonctionnalités principales de M Corp."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def solde(self, ctx):
        """Affiche le solde bancaire de l'utilisateur."""
        balance = await bank.get_balance(ctx.author)
        currency = await bank.get_currency_name(ctx.guild)

        await ctx.send(
            f"{ctx.author.mention}, votre solde est de "
            f"{balance} {currency}."
        )
    
    @commands.command()
    @commands.is_owner()
    async def crediter(
        self,
        ctx,
        membre: commands.MemberConverter,
        montant: int
    ):
        """Ajoute des crédits à un utilisateur."""
        if montant <= 0:
            await ctx.send(
                "Le montant doit être supérieur à 0."
            )
            return

        await bank.deposit_credits(
            membre,
            montant
        )

        nouveau_solde = await bank.get_balance(
            membre
        )

        currency = await bank.get_currency_name(
            ctx.guild
        )

        await ctx.send(
            f"{montant} {currency} ajoutés à "
            f"{membre.mention}.\n"
            f"Nouveau solde : {nouveau_solde} {currency}."
        )
