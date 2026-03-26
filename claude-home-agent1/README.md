# Slack Claude Bridge

This project lets you talk to the local `run_claude.sh` wrapper from Slack.

## What it does

- Uses Slack Socket Mode, so you do not need a public webhook URL.
- Maps each Slack thread to one Claude Code session.
- Works in DMs and in channels when you mention the bot.
- Streams partial Claude output into Slack while the response is being generated.
- Supports `reset` to start a fresh Claude session in the current thread.

## Files

- `run_claude.sh`: your existing Claude launcher
- `slack_claude_bot.py`: Slack bot service
- `.env.example`: environment variable template

## Slack app setup

Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) with:

1. `Socket Mode` enabled
2. An app-level token with the `connections:write` scope
3. A bot token with these scopes:
   - `app_mentions:read`
   - `channels:history`
   - `channels:join` *(auto-join public channels when mentioned)*
   - `chat:write`
   - `files:read` *(required to download uploaded Slack files)*
   - `groups:history`
   - `groups:read` *(optional, for private channel info)*
   - `im:history`
   - `im:read`
   - `im:write`
   - `mpim:history`
4. Event subscriptions for:
   - `app_mention`
   - `message.channels` *(channel thread replies without @mention)*
   - `message.groups` *(private channel thread replies)*
   - `message.im`

Then install the app to your workspace.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` with your Slack tokens.

Optional settings:

- `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` if you use a custom Claude-compatible proxy
- `CLAUDE_DEFAULT_MODEL=claude-sonnet-4-6` for faster default replies
- `CLAUDE_SLACK_TIMEOUT_SECONDS=1800` if you want longer-running tasks
- `SLACK_MAX_FILE_BYTES=20971520` to change the per-file download limit (default: 20 MB)

## Run

```bash
source .venv/bin/activate
python3 slack_claude_bot.py
```

## Usage

- Send the bot a DM to chat directly.
- Mention the bot in any public channel to start a conversation (the bot auto-joins the channel).
- Once a thread has an active session, reply in the thread without @mentioning — the bot picks it up automatically.
- Upload a file in a DM or in an active thread and the bot will save it under `.slack-bot/downloads/...` before asking Claude to inspect it.
- Send `reset` in a thread to clear that thread's Claude session.
- Send `help` to see the command summary.
- While Claude is still generating, Slack shows a live-updating draft instead of only `Thinking...`.

## Notes

- Claude runs in this directory, so any file edits happen here.
- `run_claude.sh` defaults to `claude-sonnet-4-6` for lower latency. Set `CLAUDE_DEFAULT_MODEL` if you want another default.
- This public copy does not include any private Slack tokens, Claude auth tokens, or internal proxy URLs.
- Long tasks may still need a larger `CLAUDE_SLACK_TIMEOUT_SECONDS`.
- If you send multiple messages quickly in the same thread, they are processed one at a time.
