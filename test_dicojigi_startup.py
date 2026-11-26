"""Offline startup smoke test; does not open a web port or connect to Discord."""
import os
import unittest
from unittest.mock import AsyncMock, patch

import discord
from discord import app_commands
from bot import GUILD_ID, MyBot


class Startup(unittest.IsolatedAsyncioTestCase):
    async def test_existing_buttons_and_character_command_registered(self):
        with patch.dict(os.environ, {}, clear=True):
            bot = MyBot()
            try:
                with patch.object(app_commands.CommandTree, 'sync', new_callable=AsyncMock) as sync:
                    await bot.setup_hook()
                    sync.assert_awaited_once_with(guild=discord.Object(id=GUILD_ID))
                command = bot.tree.get_command('내캐릭터', guild=discord.Object(id=GUILD_ID))
                self.assertIsNotNone(command)
                ids = {c.custom_id for view in bot.persistent_views for c in view.children}
                self.assertIn('verify_btn', ids)
                self.assertIn('persistent_streamer_auth_btn', ids)
                self.assertGreaterEqual(len(ids), 4)
            finally:
                await bot.close()
