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
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("stone_island_hunter")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MAX_EUR = float(os.getenv("MAX_PRICE_EUR", "100"))
CHECK_INTERVAL = max(60, int(os.getenv("POLL_SECONDS", "600")))
REQUEST_TIMEOUT = max(5, int(os.getenv("RSS_TIMEOUT_SECONDS", "20")))
KUFAR_MAX_CHECKS = min(100, max(1, int(os.getenv("KUFAR_MAX_CHECKS", "30"))))
KUFAR_MAX_RESULTS = min(20, max(1, int(os.getenv("KUFAR_MAX_RESULTS", "10"))))
KUFAR_CONCURRENCY = min(8, max(1, int(os.getenv("KUFAR_CONCURRENCY", "5"))))
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
        "🏝 WOAQUEZ Finder включён!\n\n"
        "RSS-уведомления Stone Island и C.P. Company активны "
        f"(лимит {MAX_EUR:.0f} €).\n\n"
        "Чтобы искать на Kufar, просто напиши запрос обычным сообщением — команды не нужны.\n"
        "Например: «Сними квартиру посуточно до 250 BYN на два дня».\n\n"
        "Команды: /status, /check, /stop, /help."
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
        self.text_parts = []
        self.hidden_tags = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {key.lower(): value for key, value in attrs if key and value}
        if tag in ("script", "style", "noscript", "svg"):
            self.hidden_tags.append(tag)
        if tag == "a":
            href = values.get("href", "")
            if href:
                self.links.append(html.unescape(href))
        if tag == "meta":
            key = values.get("property") or values.get("name")
            value = values.get("content")
            if key and value:
                self.meta[key.lower()] = clean_text(value)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.hidden_tags:
            self.hidden_tags.reverse()
            self.hidden_tags.remove(tag)
            self.hidden_tags.reverse()

    def handle_data(self, data):
        if data and not self.hidden_tags:
            value = clean_text(data)
            if value:
                self.text_parts.append(value)



def normalize_kufar_item_link(value):
    value = html.unescape(str(value or "")).strip()
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("/"):
        value = "https://www.kufar.by" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        return ""
    if (parts.hostname or "").lower() not in ("kufar.by", "www.kufar.by"):
        return ""
    if not re.fullmatch(r"/item/\d+/?", parts.path):
        return ""
    return urlunsplit(("https", "www.kufar.by", parts.path.rstrip("/"), "", ""))


def fetch_kufar_page(url):
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in ("kufar.by", "www.kufar.by"):
        raise ValueError("Разрешены только публичные страницы kufar.by")
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
            link = normalize_kufar_item_link(href)
            if link and link not in item_links:
                item_links.append(link)
        for path in re.findall(r"/item/\d+", page):
            link = normalize_kufar_item_link(path)
            if link and link not in item_links:
                item_links.append(link)
        return {
            "status": getattr(response, "status", 200),
            "content_type": response.headers.get("Content-Type", ""),
            "bytes": len(raw),
            "meta": parser.meta,
            "page_text": " ".join(parser.text_parts)[:120000],
            "item_links": item_links[:KUFAR_MAX_CHECKS],
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


def kufar_city(meta, page_text=""):
    # Prefer listing metadata; use visible page text only as a fallback.
    metadata_text = " ".join((meta.get("og:title", ""), meta.get("og:description", "")))
    return detect_belarus_city(metadata_text) or detect_belarus_city(page_text)


def kufar_price(meta, page_text=""):
    amount = meta.get("product:price:amount")
    currency = meta.get("product:price:currency", "").upper()
    if amount:
        try:
            return f"{float(amount.replace(',', '.')):g} {currency or 'BYN'}"
        except ValueError:
            pass
    text = " ".join((meta.get("og:title", ""), meta.get("og:description", ""), page_text))
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


BELARUS_CITY_FORMS = {
    "минск": ("Минск", r"Минск(?:е|а|у|ом)?"),
    "гродно": ("Гродно", r"Гродно"),
    "брест": ("Брест", r"Брест(?:е|а|у|ом)?"),
    "витебск": ("Витебск", r"Витебск(?:е|а|у|ом)?"),
    "гомель": ("Гомель", r"Гомел(?:ь|е|я|ю|ем)"),
    "могилев": ("Могилёв", r"Могил[её]в(?:е|а|у|ом)?"),
    "барановичи": ("Барановичи", r"Баранович(?:и|ах|ам|ами)"),
    "борисов": ("Борисов", r"Борисов(?:е|а|у|ом)?"),
    "пинск": ("Пинск", r"Пинск(?:е|а|у|ом)?"),
    "орша": ("Орша", r"Орш(?:а|е|и|у|ей)"),
    "молодечно": ("Молодечно", r"Молодечн(?:о|е)"),
    "жодино": ("Жодино", r"Жодино"),
    "солигорск": ("Солигорск", r"Солигорск(?:е|а|у|ом)?"),
    "лида": ("Лида", r"Лид(?:а|е|ы|у|ой)"),
    "новополоцк": ("Новополоцк", r"Новополоцк(?:е|а|у|ом)?"),
    "полоцк": ("Полоцк", r"Полоцк(?:е|а|у|ом)?"),
    "слуцк": ("Слуцк", r"Слуцк(?:е|а|у|ом)?"),
    "мозырь": ("Мозырь", r"Мозыр(?:ь|е|я|ю|ем)"),
    "речица": ("Речица", r"Речиц(?:а|е|ы|у|ей)"),
    "жлобин": ("Жлобин", r"Жлобин(?:е|а|у|ом)?"),
    "светлогорск": ("Светлогорск", r"Светлогорск(?:е|а|у|ом)?"),
    "волковыск": ("Волковыск", r"Волковыск(?:е|а|у|ом)?"),
    "сморгонь": ("Сморгонь", r"Сморгон(?:ь|и|ью)"),
    "осиповичи": ("Осиповичи", r"Осипович(?:и|ах|ам|ами)"),
}


def detect_belarus_city(value):
    value = clean_text(value).lower().replace("ё", "е")
    for canonical, pattern in BELARUS_CITY_FORMS.values():
        match = re.search(r"(?<![а-я])" + pattern.lower().replace("ё", "е") + r"(?![а-я])", value, re.IGNORECASE)
        if match:
            return canonical
    return None


def parse_natural_search(raw_text):
    text = clean_text(raw_text)
    requested_city = detect_belarus_city(text)
    if requested_city:
        for canonical, pattern in BELARUS_CITY_FORMS.values():
            if canonical == requested_city:
                text = re.sub(
                    r"(?<![а-я])" + pattern + r"(?![а-я])",
                    " ",
                    text,
                    flags=re.IGNORECASE,
                )
                break

    days = None
    day_match = re.search(
        r"\b(?:на\s*)?(один|одну|два|две|три|четыре|пять|[1-9]\d*)\s*(?:день|дня|дней|сутки|суток)\b",
        text,
        re.IGNORECASE,
    )
    if day_match:
        day_word = day_match.group(1).lower()
        day_numbers = {
            "один": 1, "одну": 1, "два": 2, "две": 2,
            "три": 3, "четыре": 4, "пять": 5,
        }
        days = day_numbers.get(day_word)
        if days is None:
            try:
                days = int(day_word)
            except ValueError:
                days = None
        text = text[:day_match.start()] + " " + text[day_match.end():]

    budget = None
    budget_match = re.search(
        r"\b(?:до|максимум|бюджет(?:ом)?\s*)\s*([0-9][0-9\s]*(?:[.,][0-9]{1,2})?)\s*(?:BYN|б\.?\s*р\.?|р\.?|руб(?:лей|ля)?)?(?=\s|$|[,!?])",
        text,
        re.IGNORECASE,
    )
    if budget_match:
        try:
            budget = float(re.sub(r"\s+", "", budget_match.group(1)).replace(",", "."))
            text = text[:budget_match.start()] + " " + text[budget_match.end():]
        except ValueError:
            budget = None

    text = re.sub(
        r"\b(?:найди|найти|ищи|поискать|подбери|подобрать|сними|снять|покажи|показать)\b",
        " ", text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:мне|пожалуйста|объявления?|варианты|вообще|в городе|городе|по всей беларуси|всей беларуси)\b",
        " ", text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:любая цена|любую цену|любой ценой|без ограничений(?: по цене)?|без лимита(?: по цене)?)\b",
        " ", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"^\s*(?:в|на|по|для)\s+|\s+(?:в|на|по|для)\s*[,!.?]*\s*$", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,.!?")
    return text, budget, days, requested_city


async def run_kufar_search(update: Update, query: str, max_budget=None, duration_days=None, requested_city=None):
    if not query:
        await update.effective_message.reply_text(
            "Напиши, что нужно найти. Например: «сними квартиру посуточно до 250 BYN на два дня»."
        )
        return

    search_query = " ".join(part for part in (query, requested_city) if part)
    search_url = (
        "https://www.kufar.by/listings?query="
        + quote_plus(search_query)
        + "&rgn=all"
    )
    budget_note = ""
    if max_budget is not None:
        if duration_days and duration_days > 1:
            budget_note = (
                f" Бюджет всего {max_budget:g} BYN на {duration_days} дн. "
                f"(ориентир до {max_budget / duration_days:g} BYN в сутки)."
            )
        else:
            budget_note = f" Бюджет до {max_budget:g} BYN."
    progress = await update.effective_message.reply_text(
        f"🔎 Ищу «{query}»" + (f" в городе {requested_city}" if requested_city else " по всей Беларуси") + f".{budget_note}"
    )
    try:
        results = await asyncio.to_thread(fetch_kufar_page, search_url)
        links = results.get("item_links", [])
        if not links:
            await progress.edit_text(
                "Не удалось найти ссылки на отдельные объявления в результатах.\n"
                f"Открой поиск вручную: {search_url}"
            )
            return

        sent = 0
        checked = 0
        skipped_city = 0
        skipped_budget = 0
        unknown_price = 0
        fetch_errors = 0
        daily_limit = (
            max_budget / duration_days
            if max_budget is not None and duration_days and duration_days > 1
            else max_budget
        )

        async def inspect_listing(link, semaphore):
            async with semaphore:
                try:
                    page = await asyncio.to_thread(fetch_kufar_page, link)
                    meta = page.get("meta", {})
                    page_text = page.get("page_text", "")
                    return {
                        "link": link,
                        "title": meta.get("og:title") or meta.get("twitter:title") or "Объявление Kufar",
                        "image": meta.get("og:image"),
                        "price": kufar_price(meta, page_text),
                        "city": kufar_city(meta, page_text),
                        "error": None,
                    }
                except Exception as exc:
                    logger.warning("Не удалось проверить объявление %s: %s", link, exc)
                    return {"link": link, "error": exc}

        links_to_check = links[:KUFAR_MAX_CHECKS]
        semaphore = asyncio.Semaphore(KUFAR_CONCURRENCY)
        inspected = await asyncio.gather(
            *(inspect_listing(link, semaphore) for link in links_to_check)
        )
        fetch_errors = sum(1 for item in inspected if item.get("error"))
        checked = len(inspected) - fetch_errors

        for item in inspected:
            if sent >= KUFAR_MAX_RESULTS:
                break
            if item.get("error"):
                continue
            link = item["link"]
            title = item["title"]
            image_url = item["image"]
            price = item["price"]
            city = item["city"]

            # A requested city must be confirmed by the public listing page.
            if requested_city and city != requested_city:
                skipped_city += 1
                continue

            if daily_limit is not None and price:
                amount_match = re.search(r"([0-9][0-9 ]*(?:[.,][0-9]{1,2})?)", price)
                if amount_match:
                    amount = float(amount_match.group(1).replace(" ", "").replace(",", "."))
                    if amount > daily_limit:
                        skipped_budget += 1
                        continue
            elif daily_limit is not None and not price:
                unknown_price += 1

            caption_parts = [f"📌 {title[:220]}"]
            caption_parts.append(f"📍 {city or 'Город не указан'}")
            caption_parts.append(
                f"💰 {price}" if price else
                "💰 Цена не указана — проверь бюджет по ссылке у продавца"
            )
            if duration_days and max_budget is not None:
                caption_parts.append(
                    f"🗓 {duration_days} дн. · бюджет до {max_budget:g} BYN всего"
                )
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

        summary = (
            f"✅ Поиск завершён.\n"
            f"Карточек отправлено: {sent}.\n"
            f"Объявлений проверено: {checked} из {len(links)} найденных ссылок."
        )
        if requested_city:
            summary += f"\nГород не удалось подтвердить или он не совпал: {skipped_city}."
        if daily_limit is not None:
            summary += f"\nОтсеяно из-за превышения бюджета: {skipped_budget}."
            summary += f"\nС неизвестной ценой показано: {unknown_price}."
        else:
            summary += "\nОграничения по цене нет."
        if fetch_errors:
            summary += f"\nНе удалось загрузить карточки: {fetch_errors}."
        summary += f"\nПроверено ссылок: {len(links_to_check)}; параллельных загрузок: {KUFAR_CONCURRENCY}."
        summary += f"\nВсе результаты: {search_url}"
        await progress.edit_text(summary)
    except Exception as exc:
        logger.exception("Ошибка поиска Kufar")
        await progress.edit_text(
            f"❌ Не удалось обработать поиск Kufar: {type(exc).__name__}: {str(exc)[:180]}\n"
            f"Ссылка: {search_url}"
        )


async def search_kufar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_query = " ".join(context.args).strip()
    query, budget, days, city = parse_natural_search(raw_query)
    await run_kufar_search(update, query, budget, days, city)


async def natural_language_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return
    query, budget, days, city = parse_natural_search(message.text)
    if not query:
        await message.reply_text("Напиши, что нужно найти на Kufar.")
        return
    await run_kufar_search(update, query, budget, days, city)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "⚙️ Stone Island Hunter\n"
        "Бренды: Stone Island, C.P. Company\n"
        f"Максимальная цена: {MAX_EUR:.0f} €\n"
        f"RSS-источников: {len(FEED_URLS)}\n"
        f"Подписчиков: {len(subscribers)}\n"
        f"Интервал проверки: {CHECK_INTERVAL // 60} мин.\n"
        f"Kufar: до {KUFAR_MAX_CHECKS} проверок, максимум {KUFAR_MAX_RESULTS} карточек."
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
        "/search <запрос> — поиск на Kufar командой (необязательно)\n"
        "/testkufar — проверить получение данных Kufar\n"
        "Можно просто написать запрос сообщением, например:\n"
        "«Найди Stone Island Off Dye в Минске, любая цена»\n"
        "«Сними квартиру посуточно до 250 BYN на два дня»\n"
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language_search))
    application.add_handler(CommandHandler("testkufar", test_kufar))
    application.add_handler(CommandHandler("help", help_command))
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
