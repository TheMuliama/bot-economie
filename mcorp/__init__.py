async def setup(bot):
    from .mcorp import MCorp

    await bot.add_cog(MCorp(bot))
