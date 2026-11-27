import asyncio
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


def extract_euro_price(text):
    patterns = [r"(?:€|EUR)\s?([0-9][0-9.,]*)", r"([0-9][0-9.,]*)\s?(?:€|EUR)"]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            raw = match.group(1).replace(".", "").replace(",", ".")
            try:
                return float(raw)
            except ValueError:
                pass
    return None


def matching_item(entry):
    title = entry.get("title", "")
    summary = re.sub("<[^>]+>", " ", entry.get("summary", ""))
    text = f"{title} {summary}".lower()
    if not any(brand in text for brand in BRANDS):
        return None
    price = extract_euro_price(f"{title} {summary}")
    if price is None or price > MAX_PRICE:
        return None
    link = entry.get("link", "")
    if not link:
        return None
    return title, price, link


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.add(update.effective_chat.id)
    await update.message.reply_text(
        "🏝 Stone Island Hunter включён!\n"
        f"Ищу Stone Island и C.P. Company до {MAX_PRICE:.0f} € по RSS-источникам.\n"
        "Команды: /status — настройки, /help — помощь.\n\n"
        "Важно: бот фильтрует объявления, но не может гарантировать подлинность вещи."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Бренды: Stone Island, C.P. Company\n"
        f"Максимальная цена: {MAX_PRICE:.0f} €\n"
        "География: весь мир (зависит от подключённых источников)\n"
        f"Подключено RSS-источников: {len(FEED_URLS)}"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я присылаю новые объявления из подключённых RSS-лент. "
        "Цена должна быть указана в евро и не превышать лимит.\n"
        "Добавьте RSS-ссылки в переменную RSS_FEED_URLS в Railway "
        "(несколько ссылок разделяются точкой с запятой)."
    )


async def poll_feeds(app: Application):
    while True:
        for url in FEED_URLS:
            try:
                feed = await asyncio.to_thread(feedparser.parse, url)
                for entry in feed.entries:
                    item = matching_item(entry)
                    if not item:
                        continue
                    title, price, link = item
                    key = entry.get("id") or link
                    if key in seen:
                        continue
                    seen.add(key)
                    message = (
                        "🆕 <b>Новое объявление</b>\n"
                        f"{title[:250]}\n"
                        f"Цена: <b>{price:.2f} €</b>\n"
                        f'<a href="{link}">Открыть объявление</a>\n\n'
                        "⚠️ Подлинность не подтверждена. Проверьте бирки, "
                        "серийный номер/Certilogo (если применимо), продавца и фото."
                    )
                    for chat_id in list(subscribers):
                        try:
                            await app.bot.send_message(chat_id, message, parse_mode="HTML")
                        except Exception:
                            log.exception("Не удалось отправить сообщение в чат %s", chat_id)
            except Exception:
                log.exception("Ошибка чтения RSS: %s", url)
        await asyncio.sleep(POLL_SECONDS)


async def post_init(app: Application):
    app.create_task(poll_feeds(app))


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("help", help_command))
    app.run_polling()


if __name__ == "__main__":
    main()
