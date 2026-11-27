import asyncio
import html
import logging
import os
import re

import feedparser
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MAX_PRICE = float(os.getenv("MAX_PRICE_EUR", "100"))
FEED_URLS = [u.strip() for u in os.getenv("RSS_FEED_URLS", "").split(";") if u.strip()]
BRANDS = ("stone island", "c.p. company", "cp company", "c.p.company")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "600"))

seen = set()
subscribers = set()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stone-island-hunter")


def extract_price(text):
    match = re.search(r"(?:€|EUR)\s?([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s?(?:€|EUR)", text, re.I)
    if not match:
        return None
    raw = (match.group(1) or match.group(2)).replace(",", ".")
    return float(raw)


def get_image(entry):
    for item in entry.get("media_content", []):
        if item.get("url"):
            return item["url"]
    for item in entry.get("media_thumbnail", []):
        if item.get("url"):
            return item["url"]
    for item in entry.get("enclosures", []):
        if item.get("type", "").startswith("image/"):
            return item.get("href")
    summary = entry.get("summary", "")
    match = re.search(r'<img[^>]+src=["\']([^"\']+)', summary, re.I)
    return match.group(1) if match else None


def matching_item(entry):
    title = entry.get("title", "")
    summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
    text = f"{title} {summary}".lower()

    if not any(brand in text for brand in BRANDS):
        return None

    price = extract_price(f"{title} {summary}")
    if price is None or price > MAX_PRICE:
        return None

    link = entry.get("link", "")
    if not link:
        return None

    return title, price, link, get_image(entry)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.add(update.effective_chat.id)
    await update.message.reply_text(
        "🏝 Stone Island Hunter включён!\n"
        f"Ищу Stone Island и C.P. Company до {MAX_PRICE:.0f} €.\n"
        "Команды: /status и /help.\n"
        "Подлинность вещей бот не подтверждает."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бренды: Stone Island, C.P. Company\n"
        f"Максимальная цена: {MAX_PRICE:.0f} €\n"
        f"RSS-источников: {len(FEED_URLS)}"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот проверяет подключённые RSS-ленты каждые "
        f"{POLL_SECONDS // 60} минут.\n"
        "Новые объявления отправляются с ценой, ссылкой и фото, "
        "если источник предоставляет изображение."
    )


async def poll_feeds(app):
    while True:
        for url in FEED_URLS:
            try:
                feed = await asyncio.to_thread(feedparser.parse, url)
                for entry in feed.entries:
                    item = matching_item(entry)
                    if not item:
                        continue

                    title, price, link, photo = item
                    key = entry.get("id") or link
                    if key in seen:
                        continue
                    seen.add(key)

                    caption = (
                        "🆕 <b>Новое объявление</b>\n"
                        f"{html.escape(title[:250])}\n"
                        f"Цена: <b>{price:.2f} €</b>\n"
                        f'<a href="{html.escape(link, quote=True)}">Открыть объявление</a>\n'
                        "⚠️ Подлинность не подтверждена."
                    )

                    for chat_id in list(subscribers):
                        try:
                            if photo:
                                await app.bot.send_photo(
                                    chat_id, photo=photo,
                                    caption=caption, parse_mode="HTML"
                                )
                            else:
                                await app.bot.send_message(
                                    chat_id, caption, parse_mode="HTML"
                                )
                        except Exception:
                            log.exception("Ошибка отправки сообщения")
            except Exception:
                log.exception("Ошибка RSS: %s", url)

        await asyncio.sleep(POLL_SECONDS)


async def post_init(app):
    app.create_task(poll_feeds(app))


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("help", help_command))
    app.run_polling()


if __name__ == "__main__":
    main()
