""" dumpyara Bot """
from aiogram.utils import executor

from dumpyarabot.dumpyarabot import DP


if __name__ == '__main__':
    executor.start_polling(DP)
