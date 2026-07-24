"""Lua scripts that keep the order of Telegram message edits.

The bot shows the progress of a job in one Telegram message. The bot edits
that message for each step. Two workers and the bot can send edits at the
same time. Thus the edits can arrive in the wrong order.

Two Redis keys control the order. Each key applies to one Telegram message.
The name of each key contains the chat and the message:

- The counter key gives an order number to each edit.
- The watermark key holds the order number of the last edit that Telegram
  accepted.

An edit whose order number is less than the watermark is too old. The bot
discards such an edit. Example: step 20 of 25 fails and waits for a retry.
Step 21 of 25 then succeeds and moves the watermark. Step 20 is now too old.
If the bot sent step 20, the display would show an earlier step.

Each script does a compare and a write. Redis runs a script from the start to
the end and does no other command at the same time. Thus no worker can change
a key between the compare and the write. Python code that did a GET and then
a SET would not be safe.

Note: one script is safe, but the full cycle is not one operation. The bot
does the stale test, then sends the edit to Telegram, then moves the
watermark. Only the bot process reads the queue. The arq workers only write
to the queue. Two bot processes must not read the queue at the same time.
Two such processes can send edits in the wrong order.
"""

# Give the next edit of one Telegram message its order number.
#
# If Redis loses the counter key but keeps the watermark key, the counter
# starts again at 1. All new edits would then be less than the watermark, and
# the bot would discard all of them. The message would stay at the same step
# for all time. To stop this, start the counter at the watermark.
#
# KEYS[1]: the counter key.
# KEYS[2]: the watermark key.
# ARGV[1]: the time-to-live of the key, in seconds.
#
# Returns the new order number.
STAMP_EDIT_SEQUENCE: str = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    local applied = tonumber(redis.call('GET', KEYS[2]))
    if applied then
        redis.call('SET', KEYS[1], applied)
    end
end
local sequence = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return sequence
"""

# Tell if a later edit replaced this edit.
#
# KEYS[1]: the watermark key.
# ARGV[1]: the order number of the edit.
#
# Returns 1 if the edit is too old, or 0 if the edit is current.
IS_STALE_EDIT: str = """
local applied = tonumber(redis.call('GET', KEYS[1]))
local incoming = tonumber(ARGV[1])
if applied and incoming < applied then
    return 1
end
return 0
"""

# Move the watermark forward after Telegram accepts an edit.
#
# The script writes only if the order number is more than the watermark. Thus
# a slow edit that arrives late cannot move the watermark to the rear.
#
# KEYS[1]: the watermark key.
# ARGV[1]: the order number of the edit.
# ARGV[2]: the time-to-live of the key, in seconds.
#
# Returns 1 if the script moved the watermark, or 0 if it did not.
MARK_EDIT_APPLIED: str = """
local applied = tonumber(redis.call('GET', KEYS[1]))
local incoming = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
if not applied or incoming > applied then
    redis.call('SET', KEYS[1], incoming, 'EX', ttl)
    return 1
end
if incoming == applied then
    redis.call('EXPIRE', KEYS[1], ttl)
end
return 0
"""

# Put an edit in the queue again, but only if a later edit did not replace it.
#
# The compare and the write happen in one script. Two Redis commands would let
# another worker move the watermark between the two commands. The bot would
# then put an edit in the queue that it must discard.
#
# KEYS[1]: the watermark key.
# KEYS[2]: the destination queue.
# ARGV[1]: the order number of the edit.
# ARGV[2]: 'zset' for a delayed retry, or 'list' for an immediate retry.
# ARGV[3]: the score for the delayed set, as a UNIX time.
# ARGV[4]: the message, as JSON.
#
# Returns 1 if the script put the message in the queue, or 0 if the message
# is too old.
REQUEUE_EDIT_IF_CURRENT: str = """
local applied = tonumber(redis.call('GET', KEYS[1]))
local incoming = tonumber(ARGV[1])
if applied and incoming < applied then
    return 0
end
if ARGV[2] == 'zset' then
    redis.call('ZADD', KEYS[2], ARGV[3], ARGV[4])
else
    redis.call('LPUSH', KEYS[2], ARGV[4])
end
return 1
"""

# Keep the status text of a job, but only if the text is not older than the
# text in Redis. The value has this format: "v1\n<sequence>\n<time>\n<text>".
#
# KEYS[1]: the status-text key.
# ARGV[1]: the order number of the text.
# ARGV[2]: the time of the text, as a UNIX time.
# ARGV[3]: the time-to-live of the key, in seconds.
# ARGV[4]: the full value to write.
#
# Returns 1 if the script wrote the text, or 0 if the text is too old.
STORE_LATEST_STATUS_TEXT: str = """
local current = redis.call('GET', KEYS[1])
local next_seq = tonumber(ARGV[1])
local next_ts = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local current_seq = nil
local current_ts = nil
if current and string.sub(current, 1, 3) == "v1\\n" then
    local rest = string.sub(current, 4)
    local first_newline = string.find(rest, "\\n", 1, true)
    if first_newline then
        current_seq = tonumber(string.sub(rest, 1, first_newline - 1))
        local rest_after_seq = string.sub(rest, first_newline + 1)
        local second_newline = string.find(rest_after_seq, "\\n", 1, true)
        if second_newline then
            current_ts = tonumber(string.sub(rest_after_seq, 1, second_newline - 1))
        end
    end
end

if not current_seq or next_seq > current_seq or (next_seq == current_seq and (not current_ts or next_ts >= current_ts)) then
    redis.call('SET', KEYS[1], ARGV[4], 'EX', ttl)
    return 1
end
return 0
"""
