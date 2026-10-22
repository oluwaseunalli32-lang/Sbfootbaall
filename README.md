# SB24GZFootballFactsBot

A Telegram bot that compares two football players side by side using manually stored stats (goals, assists, appearances, trophies).

## Run locally (optional)

```
pip install -r requirements.txt
export BOT_TOKEN="your-token-from-BotFather"
python bot.py
```

## Edit the player data

All stats live in `players.json`. Each player needs: `id` (lowercase letters, numbers, hyphens), `name`, `goals`, `assists`, `appearances`, `trophies` (whole numbers). `position` and `nationality` are optional. Update `last_updated` whenever you change numbers. Commit the change to GitHub and Render redeploys automatically.

## Deploy

Deploy as a Render **Background Worker** (the bot uses polling, so it needs no web port). Set the `BOT_TOKEN` environment variable. Never commit your token.
