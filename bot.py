entry.get("description", ""),
    ]

    for item in entry.get("content", []):
        parts.append(item.get("value", ""))

    return html.unescape(" ".join(str(x) for x in parts))


def extract_price(text):
    text = html.unescape(str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)

    currencies = {
        "€": "EUR",
        "EUR": "EUR",
        "$": "USD",
        "USD": "USD",
        "£": "GBP",
        "GBP": "GBP",
        "CHF": "CHF",
        "PLN": "PLN",
        "CZK": "CZK",
        "SEK": "SEK",
        "NOK": "NOK",
        "DKK": "DKK",
        "JPY": "JPY",
        "CAD": "CAD",
        "AUD": "AUD",
        "CNY": "CNY",
        "HUF": "HUF",
        "RON": "RON",
        "TRY": "TRY",
    }

    number = r"([0-9]+(?:[.,][0-9]{1,2})?)"

    for symbol, currency in currencies.items():
        if symbol in ("$", "€", "£"):
            patterns = [
                re.escape(symbol) + r"\s*" + number,
                number + r"\s*" + re.escape(symbol),
            ]
        else:
            patterns = [
                r"\b" + symbol + r"\s*" + number,
                number + r"\s*\b" + symbol + r"\b",
            ]

        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                amount = float(match.group(1).replace(",", "."))
                return amount, currency

    return None


def price_in_euros(amount, currency):
    rate = rates.get(currency)

    if not rate:
        return None

    return amount / rate


def get_image(entry):
    for item in entry.get("media_content", []):
        if item.get("url"):
            return item["url"]

    for item in entry.get("media_thumbnail", []):
        if item.get("url"):
            return item["url"]

    for item in entry.get("enclosures", []):
        if item.get("href"):
            return item["href"]

    text = extract_text(entry)
    match = re.search(
        r'<img[^>]+src=["\']([^"\']+)',
        text,
        re.I
    )

    return match.group(1) if match else None


def matching_item(entry):
    title = entry.get("title", "").lower()

    if not any(brand in title for brand in BRANDS):
        return None

    text = extract_text(entry)
    price_data = extract_price(text)

    if not price_data:
        return None

    amount, currency = price_data
    euros = price_in_euros(amount, currency)

    if euros is None or euros > MAX_EUR:
        return None

    link = entry.get("link")

    if not link:
        return None

    return {
        "title": entry.get("title", "Объявление"),
        "price": amount,
        "currency": currency,
        "link": link,
        "image": get_image(entry),
        "euros": euros,
    }


async def send_item(bot, item):
    caption = (
        f"🧥 {item['title']}\n\n"
        f"💰 Цена: {item['price']:.2f} {item['currency']}\n"
        f"≈ {item['euros']:.2f} €\n\n"
        f"🔗 {item['link']}"
    )

    for chat_id in list(subscribers):
        try:
            if item["image"]:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=item["image"],
                    caption=caption
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=caption
                )

        except Exception as error:
            logger.warning("Could not send message: %s", error)


async def check_feeds(bot):
    if not FEED_URLS:
        return 0

    if time.time() - last_rate_update > 3600:
        await asyncio.to_thread(update_rates)

    found = 0

    for feed_url in FEED_URLS:
        try:
            feed = await asyncio.to_thread(feedparser.parse, feed_url)

            for entry in feed.entries:
                link = entry.get("link", "")

                if not link or link in seen:
                    continue

                item = matching_item(entry)

                if item:
                    seen.add(link)
                    found += 1
                    await send_item(bot, item)

        except Exception as error:
            logger.warning("Feed error: %s", error)

    return found


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.add(update.effective_chat.id)

    await update.message.reply_text(
        "🏝 Stone Island Hunter включён!\n\n"
        "Ищу Stone Island и C.P. Company.\n"
        "Лимит: 100 € по курсу.\n"
        "Поддерживаются разные валюты.\n\n"
        "Команды: /status, /check, /help"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Бренды: Stone Island, C.P. Company\n"
        f"Лимит: 100 € по курсу\n"
        f"RSS-источников: {len(FEED_URLS)}\n"
        f"Интервал проверки: 10 минут"
    )


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Проверяю объявления...")

    found = await check_feeds(context.bot)

    await update.message.reply_text(
        f"Проверка завершена. Найдено подходящих объявлений: {found}"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — включить уведомления\n"
        "/status — настройки бота\n"
        "/check — проверить сейчас\n"
        "/help — помощь"
    )


async def scheduled_check(application):
    while True:
        try:
            await check_feeds(application.bot)
        except Exception as error:
            logger.warning("Scheduled check error: %s", error)

        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(application):
    await asyncio.to_thread(update_rates)
    application.create_task(scheduled_check(application))


def main():
    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("check", check_now))
    application.add_handler(CommandHandler("help", help_command))

    application.run_polling()


if __name__ == "__main__":
    main()
    
