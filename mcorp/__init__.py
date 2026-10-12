async def setup(bot):
    # Keep package imports lightweight: the dashboard does not require Red.
    from .mcorp import MCorp

    await bot.add_cog(MCorp(bot))
