"""
dumpyarabot
"""
import re

import aiohttp
from aiogram import Bot, Dispatcher, types

from dumpyarabot import BOT_TOKEN, ALLOWED_USERS, ALLOWED_CHATS, JENKINS_TOKEN

# Initialize bot and dispatcher
BOT = Bot(token=BOT_TOKEN)
DP = Dispatcher(BOT)


@DP.message_handler(commands=['dump'], commands_prefix='/', chat_id=ALLOWED_CHATS)
async def jenkins_bridge(message: types.Message):
    pattern = re.compile(
        r"(https?://(www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()!@:%_+.~#?&//=]*))")
    url = pattern.search(message.text)
    if not url:
        await message.answer("Not a valid URL!")
        return
    url = url.group(1)
    # await message.answer(url)
    params = (
        ('token', JENKINS_TOKEN),
        ('URL', url),
    )

    async with aiohttp.ClientSession() as session:
        async with session.get('http://0.0.0.0:8090/job/dumpyara/buildWithParameters',
                               params=params) as resp:
            if resp.status in (200, 201):
                await message.answer("Job started!")
            else:
                await message.answer("Something went wrong!")
