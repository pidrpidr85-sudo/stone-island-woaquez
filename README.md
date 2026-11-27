# Stone Island Hunter

Telegram bot starter for second-hand Stone Island and C.P. Company listings up to €100.

## Current scope
- Telegram `/start`, `/status`, and `/help` commands.
- Reads RSS feeds configured in `RSS_FEED_URLS`.
- Filters listing title/summary for the two brands and prices shown in EUR.
- Sends newly seen matching listings to Telegram subscribers.

## Important limitations
This is a starter implementation, not a finished worldwide marketplace search engine.
No RSS sources are configured by default. Add legitimate RSS feeds from marketplaces
that provide them and comply with their terms. Some marketplaces do not provide public
RSS feeds or restrict automated access. Do not assume a listing is authentic: the bot
cannot verify authenticity from a title or listing text.

## Railway variables
- `TELEGRAM_BOT_TOKEN`: token from BotFather (keep private).
- `MAX_PRICE_EUR`: defaults to `100`.
- `POLL_SECONDS`: defaults to `600`.
- `RSS_FEED_URLS`: RSS URLs separated by semicolons.

Deploy as a Docker project on Railway. Start the bot with `/start` after deployment.
