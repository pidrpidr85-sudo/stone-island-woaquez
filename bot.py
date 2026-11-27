import os

import re

import html

import json

import time

import logging

import asyncio

import urllib.request

import xml.etree.ElementTree as ET

from telegram import Update

from telegram.ext import (

    Application,

    CommandHandler,

    ContextTypes,

)

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s %(levelname)s %(message)s"

)

logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

MAX_EUR = 100

CHECK_INTERVAL = 600



BRANDS = ["stone island", "c.p. company", "cp company"]

FEED_URLS = [

    url.strip()

    for url in os.getenv("RSS_FEED_URLS", "").split(";")

    if url.strip()

]

SUBSCRIBERS_FILE = "subscribers.json"

SEEN_FILE = "seen.json"

subscribers = set()

seen = set()

rates = {"EUR": 1.0, "USD": 1.08, "GBP": 0.84, "CHF": 0.94}

def load_json(filename, default):

    try:

        with open(filename, "r", encoding="utf-8") as file:

            return json.load(file)

    except Exception:

        return default

def save_json(filename, data):

    try:

        with open(filename, "w", encoding="utf-8") as file:

            json.dump(data, file, ensure_ascii=False)

    except Exception:

        logger.exception("Не удалось сохранить %s", filename)

subscribers = set(load_json(SUBSCRIBERS_FILE, []))

seen = set(load_json(SEEN_FILE, []))

def download_feed(url):

    request = urllib.request.Request(

        url,

        headers={"User-Agent": "StoneIslandHunter/1.0"}

    )

    with urllib.request.urlopen(request, timeout=20) as response:

        return response.read()

def extract_price(text):

    patterns = [

        (r"€\s*(\d+(?:[.,]\d{1,2})?)", "EUR"),

        (r"(\d+(?:[.,]\d{1,2})?)\s*€", "EUR"),

        (r"\bEUR\s*(\d+(?:[.,]\d{1,2})?)", "EUR"),

        (r"\$\s*(\d+(?:[.,]\d{1,2})?)", "USD"),

        (r"\bUSD\s*(\d+(?:[.,]\d{1,2})?)", "USD"),

        (r"£\s*(\d+(?:[.,]\d{1,2})?)", "GBP"),

        (r"\bGBP\s*(\d+(?:[.,]\d{1,2})?)", "GBP"),

        (r"\bCHF\s*(\d+(?:[.,]\d{1,2})?)", "CHF"),

        (r"(\d+(?:[.,]\d{1,2})?)\s*CHF\b", "CHF"),

    ]

    for pattern, currency in patterns:

        match = re.search(pattern, text, re.I)

        if match:

            return float(match.group(1).replace(",", ".")), currency

    return None


def get_image(entry):

    for child in entry.iter():

        tag = child.tag.lower().split("}")[-1]

        if tag in ("thumbnail", "content", "enclosure"):

            url = child.attrib.get("url") or child.attrib.get("href")

            if url and url.startswith("http"):

                return url

    description = " ".join(

        child.text or ""

        for child in entry.iter()

    )

    match = re.search(

        r'<img[^>]+src=["\']([^"\']+)',

        description,

        re.I

    )

    return match.group(1) if match else None

def matching_item(entry):

    title = ""

    link = ""

    for child in entry:

        tag = child.tag.lower().split("}")[-1]

        if tag == "title":

            title = child.text or ""

        elif tag == "link":

            link = child.attrib.get("href") or child.text or ""

    title_lower = title.lower()

    if not any(brand in title_lower for brand in BRANDS):

        return None

    description = " ".join(

        child.text or ""

        for child in entry.iter()

    )

    price_data = extract_price(description)

    if not price_data:

        price_data = extract_price(title)

    if not price_data:

        return None

    amount, currency = price_data

    euro_rates = {

        "EUR": 1.0,

        "USD": 0.93,

        "GBP": 1.19,

        "CHF": 1.06,

    }

    euros = amount * euro_rates[currency]

    if euros > MAX_EUR or not link:

        return None

    return {

        "title": title,

        "amount": amount,

        "currency": currency,

        "euros": euros,

        "link": link,

        "image": get_image(entry),

    }

async def send_item(bot, item):

    caption = (

        f"🧥 {item['title']}\n\n"

        f"💰 {item['amount']:.2f} {item['currency']}\n"

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

        except Exception:

            logger.exception("Ошибка отправки объявления")

async def check_feeds(bot):

    if not FEED_URLS:

        return 0

    found = 0

    for url in FEED_URLS:

        try:

            data = await asyncio.to_thread(download_feed, url)

            root = ET.fromstring(data)

            for entry in root.iter():

                tag = entry.tag.lower().split("}")[-1]

                if tag not in ("item", "entry"):

                    continue

                item = matching_item(entry)

                if not item or item["link"] in seen:

                    continue

                seen.add(item["link"])

                save_json(SEEN_FILE, list(seen)[-5000:])

                await send_item(bot, item)

                found += 1

        except Exception:

            logger.exception("Ошибка RSS: %s", url)

    return found

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id

    subscribers.add(chat_id)

    save_json(SUBSCRIBERS_FILE, list(subscribers))

    await update.message.reply_text(

        "🏝 Stone Island Hunter запущен!\n\n"

        "Ищу Stone Island и C.P. Company.\n"

        "Бюджет: до 100 €.\n"

        "Команды: /status /check /help"

    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(

        f"Бренды: Stone Island, C.P. Company\n"

        f"Бюджет: {MAX_EUR} €\n"

        f"RSS-источников: {len(FEED_URLS)}"

    )

async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = await update.message.reply_text(

        "🔎 Проверка запущена..."

    )

    if not FEED_URLS:

        await message.edit_text(

            "⚠️ RSS-источники не подключены.\n"

            "Нужно добавить RSS_FEED_URLS в Railway Variables."

        )

        return

    try:

        found = await asyncio.wait_for(

            check_feeds(context.bot),

            timeout=90

        )

        await message.edit_text(

            f"✅ Проверка завершена.\n"

            f"Найдено новых объявлений: {found}"

        )

    except asyncio.TimeoutError:

        await message.edit_text("Проверка заняла больше 90 секунд.")

    except Exception as error:

        logger.exception("Ошибка /check")

        await message.edit_text(f"Ошибка: {str(error)[:200]}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(

        "/start — включить уведомления\n"

        "/status — настройки\n"

        "/check — проверить объявления\n"

        "/help — помощь"

    )

async def scheduled_check(application):

    while True:

        try:

            await check_feeds(application.bot)

        except Exception:

            logger.exception("Ошибка автоматической проверки")

        await asyncio.sleep(CHECK_INTERVAL)

async def post_init(application):

    logger.info("Stone Island Hunter запущен")

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











































































































