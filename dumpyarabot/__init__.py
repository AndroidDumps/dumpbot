""" Dumpyara Bot initialization"""
import json
import logging
import sys
from os.path import dirname

import decouple

# WORK_DIR = dirname(__file__)
PARENT_DIR = '/'.join(dirname(__file__).split('/')[:-1])

# read bog config
with open(f'{PARENT_DIR}/config.json', 'r') as f:
    CONFIG = json.load(f)
BOT_TOKEN = CONFIG['bot_token']
ALLOWED_USERS = CONFIG['allowed_users']
ALLOWED_CHATS = CONFIG['allowed_chats']
JENKINS_TOKEN = decouple.config('TOKEN')

# set logging
FORMATTER = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s'
                              '[%(module)s.%(funcName)s:%(lineno)d]: %(message)s')
OUT = logging.StreamHandler(sys.stdout)
ERR = logging.StreamHandler(sys.stderr)
OUT.setFormatter(FORMATTER)
ERR.setFormatter(FORMATTER)
OUT.setLevel(logging.INFO)
ERR.setLevel(logging.WARNING)
LOGGER = logging.getLogger()
LOGGER.addHandler(OUT)
LOGGER.addHandler(ERR)
LOGGER.setLevel(logging.INFO)
TG_LOGGER = logging.getLogger(__name__)
