# Stone Island Hunter

Telegram bot for finding second-hand Stone Island and C.P. Company listings from RSS feeds.

## Features

- Telegram commands: `/start`, `/stop`, `/status`, `/check`, and `/help`.
- RSS and Atom feed parsing.
- Brand matching in listing titles and descriptions.
- Price filtering with a configurable EUR budget.
- EUR, USD, GBP, and CHF price recognition (foreign-currency conversions are approximate fixed rates).
- Sends matching listings with a link and an image when the feed provides one.
- Persistent subscriber and seen-listing files to reduce duplicate notifications.
- Automatic checks at a configurable interval.

## Railway variables

| Variable | Required | Default | Description |
|---|---:|---:|---|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from BotFather. Keep it private. |
| `RSS_FEED_URLS` | No | empty | RSS/Atom feed URLs separated by semicolons. |
| `MAX_PRICE_EUR` | No | `100` | Maximum listing price converted to EUR. |
| `POLL_SECONDS` | No | `600` | Automatic check interval; minimum 60 seconds. |
| `RSS_TIMEOUT_SECONDS` | No | `20` | Network timeout for each feed. |
| `LOG_LEVEL` | No | `INFO` | Logging level. |

## Run

Deploy the repository on Railway using the included Dockerfile. After deployment, open the bot in Telegram and send `/start`. Use `/check` to run a manual check and `/status` to inspect the active configuration.

## Important limitations

The bot only searches feeds explicitly supplied in `RSS_FEED_URLS`; it does not crawl every marketplace or search the whole internet. Add legitimate RSS/Atom feeds from marketplaces that provide them and comply with their terms. Some marketplaces do not provide public feeds or restrict automated access.

Currency conversion rates are approximate constants in `bot.py`, not live exchange rates. A listing's authenticity cannot be established from its title or feed description; always inspect the seller's listing yourself.
