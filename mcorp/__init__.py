from .mcorp import MCorp


async def setup(bot):
    await bot.add_cog(MCorp(bot))
