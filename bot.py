import asyncio
import html
from html.parser import HTMLParser
import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit

import feedparser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("stone_island_hunter")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MAX_EUR = float(os.getenv("MAX_PRICE_EUR", "100"))
CHECK_INTERVAL = max(60, int(os.getenv("POLL_SECONDS", "600")))
REQUEST_TIMEOUT = max(5, int(os.getenv("RSS_TIMEOUT_SECONDS", "20")))
SUBSCRIBERS_FILE = Path(os.getenv("SUBSCRIBERS_FILE", "subscribers.json"))
SEEN_FILE = Path(os.getenv("SEEN_FILE", "seen.json"))

BRANDS = ("stone island", "c.p. company", "cp company", "cp-company")
FEED_URLS = [
    value.strip()
    for value in os.getenv("RSS_FEED_URLS", "").split(";")
    if value.strip()
]
EUR_RATES = {"EUR": 1.0, "USD": 0.93, "GBP": 1.19, "CHF": 1.06}
subscribers = set()
seen = set()
check_lock = asyncio.Lock()
last_check_stats = {}

PRICE_PATTERNS = (
    (r"€\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*€|\bEUR\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*EUR\b", "EUR"),
    (r"\$\s*([0-9]+(?:[.,][0-9]{1,2})?)|\bUSD\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*USD\b", "USD"),
    (r"£\s*([0-9]+(?:[.,][0-9]{1,2})?)|\bGBP\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*GBP\b", "GBP"),
    (r"\bCHF\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*CHF\b", "CHF"),
)


def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        logger.exception("Не удалось прочитать %s; используются значения по умолчанию", path)
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False
        ) as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            temp_name = file.name
        os.replace(temp_name, path)
    except OSError:
        logger.exception("Не удалось сохранить %s", path)
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def extract_price(text):
    text = clean_text(text)
    for pattern, currency in PRICE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            number = next((part for part in match.groups() if part), None)
            if number:
                try:
                    return float(number.replace(",", ".")), currency
                except ValueError:
                    continue
    return None


def normalize_link(link):
    link = html.unescape(str(link or "")).strip()
    if not link:
        return ""
    parts = urlsplit(link)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    # Remove tracking parameters while preserving the actual listing path.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def entry_text(entry):
    chunks = [entry.get("title", ""), entry.get("summary", "")]
    for item in entry.get("content", []) or []:
        chunks.append(item.get("value", ""))
    return clean_text(" ".join(str(chunk or "") for chunk in chunks))


def get_image(entry):
    for item in entry.get("media_thumbnail", []) or []:
        url = item.get("url")
        if url and str(url).startswith(("http://", "https://")):
            return str(url)
    for item in entry.get("media_content", []) or []:
        url = item.get("url")
        if url and str(url).startswith(("http://", "https://")):
            return str(url)
    for item in entry.get("links", []) or []:
        if item.get("rel") == "enclosure" and str(item.get("type", "")).startswith("image/"):
            url = item.get("href")
            if url:
                return str(url)
    return None


def matching_item(entry):
    title = clean_text(entry.get("title", "Без названия"))
    description = entry_text(entry)
    searchable = (title + " " + description).lower()
    if not any(brand in searchable for brand in BRANDS):
        return None

    price_data = extract_price(title + " " + description)
    if not price_data:
        return None

    amount, currency = price_data
    euros = amount * EUR_RATES[currency]
    link = normalize_link(entry.get("link"))
    if euros > MAX_EUR or not link or amount <= 0:
        return None

    return {
        "title": title[:250],
        "amount": amount,
        "currency": currency,
        "euros": euros,
        "link": link,
        "image": get_image(entry),
    }


def download_feed(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "StoneIslandHunter/2.0 (+RSS reader)"}
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


async def send_item(bot, item):
    caption = (
        f"🧥 {item['title']}\n\n"
        f"💰 {item['amount']:.2f} {item['currency']} "
        f"(примерно {item['euros']:.2f} €)\n\n"
        f"🔗 {item['link']}"
    )
    if not subscribers:
        return True

    delivered = 0
    for chat_id in tuple(subscribers):
        try:
            if item.get("image"):
                try:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=item["image"],
                        caption=caption[:1024],
                    )
                except Exception:
                    logger.warning("Не удалось отправить фото; отправляю текст", exc_info=True)
                    await bot.send_message(chat_id=chat_id, text=caption)
            else:
                await bot.send_message(chat_id=chat_id, text=caption)
            delivered += 1
        except Exception:
            logger.exception("Не удалось отправить объявление пользователю %s", chat_id)
    return delivered > 0


def feed_error_context(raw, exc):
    line = getattr(exc, "getLineNumber", lambda: 0)()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if not line or line > len(lines):
        return ""
    first = max(1, line - 1)
    last = min(len(lines), line + 1)
    return " | ".join(
        f"L{number}: {lines[number - 1][:240]!r}"
        for number in range(first, last + 1)
    )[:650]

async def check_feeds(bot):
    global last_check_stats
    if not FEED_URLS:
        last_check_stats = {"feeds": 0, "entries": 0, "brand": 0, "price": 0, "under_limit": 0, "duplicates": 0, "errors": 0}
        return 0

    async with check_lock:
        found = 0
        stats = {"feeds": len(FEED_URLS), "entries": 0, "brand": 0, "price": 0, "under_limit": 0, "duplicates": 0, "errors": 0, "feed_info": [], "error_info": []}
        for url in FEED_URLS:
            try:
                raw = await asyncio.to_thread(download_feed, url)
                parsed = feedparser.parse(raw)
                info = {"bytes": len(raw), "title": clean_text(parsed.feed.get("title", ""))[:100], "bozo": str(parsed.bozo_exception)[:180] if parsed.bozo else "", "error_context": feed_error_context(raw, parsed.bozo_exception) if parsed.bozo else ""}
                stats["feed_info"].append(info)
                if parsed.bozo:
                    logger.warning("RSS вернул предупреждение для %s: %s", url, parsed.bozo_exception)

                for entry in parsed.entries:
                    stats["entries"] += 1
                    title = clean_text(entry.get("title", "Без названия"))
                    description = entry_text(entry)
                    searchable = (title + " " + description).lower()
                    if not any(brand in searchable for brand in BRANDS):
                        continue
                    stats["brand"] += 1
                    price_data = extract_price(title + " " + description)
                    if not price_data:
                        continue
                    stats["price"] += 1
                    amount, currency = price_data
                    if amount <= 0 or amount * EUR_RATES[currency] > MAX_EUR:
                        continue
                    stats["under_limit"] += 1
                    item = matching_item(entry)
                    if not item:
                        continue
                    if item["link"] in seen:
                        stats["duplicates"] += 1
                        continue
                    if await send_item(bot, item):
                        seen.add(item["link"])
                        save_json(SEEN_FILE, sorted(seen)[-5000:])
                        found += 1
            except Exception as exc:
                stats["errors"] += 1
                stats["error_info"].append(f"{type(exc).__name__}: {str(exc)[:220]}")
                logger.exception("Ошибка обработки RSS-источника %s", url)
        last_check_stats = stats
        logger.info("Статистика RSS-проверки: %s", stats)
        return found


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("Открой личный чат с ботом и отправь /start.")
        return
    subscribers.add(chat.id)
    save_json(SUBSCRIBERS_FILE, sorted(subscribers))
    await update.effective_message.reply_text(
        "🏝 Stone Island Hunter включён!\n\n"
        "Ищу Stone Island и C.P. Company до "
        f"{MAX_EUR:.0f} €.\n"
        "Уведомления включены. Команды: /status, /check, /stop, /help."
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat:
        subscribers.discard(chat.id)
        save_json(SUBSCRIBERS_FILE, sorted(subscribers))
    await update.effective_message.reply_text("Уведомления отключены. Чтобы включить снова, отправь /start.")



class KufarMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = {key.lower(): value for key, value in attrs if key and value}
        if tag.lower() == "a":
            href = values.get("href", "")
            if href:
                self.links.append(html.unescape(href))
        if tag.lower() == "meta":
            key = values.get("property") or values.get("name")
            value = values.get("content")
            if key and value:
                self.meta[key.lower()] = clean_text(value)



def fetch_kufar_page(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; StoneIslandHunter/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read(2_000_000)
        page = raw.decode("utf-8", errors="replace")
        parser = KufarMetaParser()
        parser.feed(page)
        item_links = []
        for href in parser.links:
            if re.search(r"/item/\d+", href):
                if href.startswith("/"):
                    href = "https://www.kufar.by" + href
                link = normalize_link(href)
                if link and link not in item_links:
                    item_links.append(link)
        for path in re.findall(r"/item/\d+", page):
            link = "https://www.kufar.by" + path
            if link not in item_links:
                item_links.append(link)
        return {
            "status": getattr(response, "status", 200),
            "content_type": response.headers.get("Content-Type", ""),
            "bytes": len(raw),
            "meta": parser.meta,
            "item_links": item_links[:30],
        }



async def test_kufar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = await update.effective_message.reply_text(
        "🔎 Проверяю доступность Kufar из среды бота..."
    )
    pages = [
        ("Объявление", "https://www.kufar.by/item/1097455335"),
        ("Поиск по всей Беларуси", "https://www.kufar.by/listings?query=stone+island+off+dye&rgn=all"),
    ]
    lines = ["🧪 Проверка Kufar"]
    for label, url in pages:
        try:
            result = await asyncio.to_thread(fetch_kufar_page, url)
            meta = result["meta"]
            lines.append(
                f"\n{label}: HTTP {result['status']}, "
                f"{result['bytes']} байт, {result['content_type'][:60]}"
            )
            for key, title in (
                ("og:title", "Название"),
                ("og:image", "Фото"),
                ("og:description", "Описание"),
                ("product:price:amount", "Цена"),
                ("product:price:currency", "Валюта"),
            ):
                value = meta.get(key)
                if value:
                    lines.append(f"{title}: {value[:350]}")
            if not any(meta.get(key) for key in ("og:title", "og:image", "product:price:amount")):
                lines.append("Карточные метаданные в HTML не найдены.")
        except urllib.error.HTTPError as exc:
            lines.append(f"\n{label}: Kufar ответил HTTP {exc.code}.")
        except Exception as exc:
            lines.append(f"\n{label}: ошибка {type(exc).__name__}: {str(exc)[:180]}")
    lines.append("\nЭто только проверка обычного публичного доступа; обход защиты не выполняется.")
    await message.edit_text("\n".join(lines))


def kufar_price(meta):
    amount = meta.get("product:price:amount")
    currency = meta.get("product:price:currency", "").upper()
    if amount:
        try:
            return f"{float(amount.replace(',', '.')):g} {currency or 'BYN'}"
        except ValueError:
            pass
    text = " ".join((meta.get("og:title", ""), meta.get("og:description", "")))
    match = re.search(
        r"(?:цена\s*)?([0-9][0-9\s]*(?:[.,][0-9]{1,2})?)\s*(?:р\.|BYN|бел(?:арус(?:ских)?)?\.?\s*руб(?:лей|ля)?\.?)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    amount = re.sub(r"\s+", "", match.group(1)).replace(",", ".")
    try:
        return f"{float(amount):g} BYN"
    except ValueError:
        return None


async def search_kufar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text(
            "Напиши, что искать. Пример: /search Stone Island Off Dye"
        )
        return

    search_url = (
        "https://www.kufar.by/listings?query="
        + quote_plus(query)
        + "&rgn=all"
    )
    progress = await update.effective_message.reply_text(
        f"🔎 Ищу «{query}» на Kufar по всей Беларуси..."
    )
    try:
        results = await asyncio.to_thread(fetch_kufar_page, search_url)
        links = results.get("item_links", [])
        if not links:
            await progress.edit_text(
                "Не удалось найти ссылки на отдельные объявления в HTML результатов.\n"
                f"Открой поиск вручную: {search_url}"
            )
            return

        sent = 0
        for link in links[:10]:
            try:
                page = await asyncio.to_thread(fetch_kufar_page, link)
                meta = page.get("meta", {})
                title = meta.get("og:title") or meta.get("twitter:title") or "Объявление Kufar"
                description = meta.get("og:description", "")
                image_url = meta.get("og:image")
                price = kufar_price(meta)
                caption_parts = [f"🧥 {title[:220]}"]
                if description:
                    caption_parts.append(description[:350])
                caption_parts.append(f"💰 {price or 'Цена не указана'}")
                caption = "\n\n".join(caption_parts)
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Открыть объявление на Kufar", url=link)]]
                )
                if image_url and image_url.startswith(("https://", "http://")):
                    try:
                        await update.effective_message.reply_photo(
                            photo=image_url,
                            caption=caption[:1024],
                            reply_markup=keyboard,
                        )
                    except Exception:
                        logger.warning("Не удалось отправить фото Kufar; отправляю текст", exc_info=True)
                        await update.effective_message.reply_text(
                            caption[:4000], reply_markup=keyboard
                        )
                else:
                    await update.effective_message.reply_text(
                        caption[:4000], reply_markup=keyboard
                    )
                sent += 1
            except Exception:
                logger.exception("Не удалось получить объявление Kufar: %s", link)

        await progress.edit_text(
            f"✅ Поиск завершён. Карточек отправлено: {sent}.\n"
            f"Найдено ссылок: {len(links)}.\n"
            f"Все результаты: {search_url}"
        )
    except Exception as exc:
        logger.exception("Ошибка поиска Kufar")
        await progress.edit_text(
            f"❌ Не удалось обработать поиск Kufar: {type(exc).__name__}: {str(exc)[:180]}\n"
            f"Ссылка: {search_url}"
        )



async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "⚙️ Stone Island Hunter\n"
        "Бренды: Stone Island, C.P. Company\n"
        f"Максимальная цена: {MAX_EUR:.0f} €\n"
        f"RSS-источников: {len(FEED_URLS)}\n"
        f"Подписчиков: {len(subscribers)}\n"
        f"Интервал проверки: {CHECK_INTERVAL // 60} мин."
    )


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = await update.effective_message.reply_text("🔎 Проверяю подключённые RSS-источники...")
    if not FEED_URLS:
        await message.edit_text(
            "⚠️ RSS-источники не подключены. Добавь RSS_FEED_URLS в Railway Variables "
            "(несколько адресов разделяются точкой с запятой)."
        )
        return
    try:
        found = await asyncio.wait_for(check_feeds(context.bot), timeout=120)
        stats = last_check_stats
        details = (f"\nЗаписей в ленте: {stats.get('entries', 0)}"
                   f"\nСовпадений по бренду: {stats.get('brand', 0)}"
                   f"\nС ценой: {stats.get('price', 0)}"
                   f"\nДо 100 €: {stats.get('under_limit', 0)}"
                   f"\nУже встречались: {stats.get('duplicates', 0)}"
                   f"\nОшибок источника: {stats.get('errors', 0)}")
        for info in stats.get("feed_info", []):
            details += f"\nРазмер ответа: {info.get('bytes', 0)} байт"
            if info.get("title"):
                details += f"\nНазвание ленты: {info['title']}"
            if info.get("bozo"):
                details += f"\nПредупреждение RSS: {info['bozo']}"
            if info.get("error_context"):
                details += f"\nФрагмент у ошибки: {info['error_context']}"
        for error in stats.get("error_info", []):
            details += f"\nОшибка: {error}"
        await message.edit_text(f"✅ Проверка завершена. Новых объявлений отправлено: {found}.{details}")
    except asyncio.TimeoutError:
        await message.edit_text("⌛ Проверка не уложилась в 120 секунд. Посмотри логи Railway.")
    except Exception:
        logger.exception("Ошибка команды /check")
        await message.edit_text("❌ Ошибка проверки. Подробности записаны в логи Railway.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/start — включить уведомления\n"
        "/stop — отключить уведомления\n"
        "/status — настройки бота\n"
        "/check — проверить RSS-источники\n"
        "/search <запрос> — найти на Kufar по всей Беларуси\n"
        "/testkufar — проверить получение данных Kufar\n"
        "Пример: /search Stone Island Off Dye\n"
        "/help — список команд"
    )


async def scheduled_check(application):
    while True:
        try:
            found = await check_feeds(application.bot)
            if found:
                logger.info("Автоматическая проверка: отправлено новых объявлений: %s", found)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка автоматической проверки")
        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(application):
    logger.info(
        "Бот запущен. RSS=%s, подписчиков=%s, лимит=%.2f EUR, интервал=%ss",
        len(FEED_URLS), len(subscribers), MAX_EUR, CHECK_INTERVAL,
    )
    application.create_task(scheduled_check(application))


def main():
    if not TOKEN:
        raise RuntimeError("Не задана переменная окружения TELEGRAM_BOT_TOKEN в Railway Variables.")
    if MAX_EUR <= 0:
        raise RuntimeError("MAX_PRICE_EUR должен быть больше нуля.")
    subscribers.update(int(value) for value in load_json(SUBSCRIBERS_FILE, []) if str(value).lstrip("-").isdigit())
    seen.update(str(value) for value in load_json(SEEN_FILE, []) if value)
    for url in FEED_URLS:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise RuntimeError("RSS_FEED_URLS содержит некорректный адрес.")

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("check", check_now))
    application.add_handler(CommandHandler("search", search_kufar))
    application.add_handler(CommandHandler("testkufar", test_kufar))
    application.add_handler(CommandHandler("help", help_command))
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
