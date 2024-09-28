# dumpbot

## Link
https://t.me/dumpyarabot

## Commands

### `/dump [URL] [options]`

Initiate a new firmware dump process.

#### Parameters:
- `URL`: The URL of the firmware to be dumped (required)

#### Options:
- `a`: Use alternative dumper
- `f`: Force a new dump even if an existing one is found
- `b`: Add the dump to the blacklist

#### Usage Examples:
- Basic usage: `/dump https://example.com/firmware.zip`
- Use alternative dumper: `/dump https://example.com/firmware.zip a`
- Force new dump: `/dump https://example.com/firmware.zip f`
- Add to blacklist: `/dump https://example.com/firmware.zip b`
- Combine options: `/dump https://example.com/firmware.zip afb`

### `/cancel [job_id]`

Cancel an ongoing dump process.

#### Parameters:
- `job_id`: The ID of the job to be cancelled (required)

#### Usage Example:
- `/cancel 123`
