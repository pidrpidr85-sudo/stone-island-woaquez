import unittest

from bot import extract_price, matching_item, normalize_link


class PriceParsingTests(unittest.TestCase):
    def test_euro_symbol(self):
        self.assertEqual(extract_price("Stone Island jacket €85"), (85.0, "EUR"))

    def test_usd_suffix(self):
        self.assertEqual(extract_price("C.P. Company jacket 90 USD"), (90.0, "USD"))

    def test_gbp_prefix(self):
        self.assertEqual(extract_price("£75.50"), (75.5, "GBP"))

    def test_chf_suffix(self):
        self.assertEqual(extract_price("120 CHF"), (120.0, "CHF"))


class ListingTests(unittest.TestCase):
    def test_brand_and_price_in_description(self):
        entry = {
            "title": "Vintage jacket",
            "summary": "Original listing says Stone Island, price 80 EUR",
            "link": "https://example.com/item/123?utm_source=rss",
        }
        item = matching_item(entry)
        self.assertIsNotNone(item)
        self.assertEqual(item["euros"], 80.0)
        self.assertEqual(item["link"], "https://example.com/item/123")

    def test_over_budget_listing_is_ignored(self):
        entry = {
            "title": "Stone Island jacket",
            "summary": "Price: 150 EUR",
            "link": "https://example.com/item/456",
        }
        self.assertIsNone(matching_item(entry))

    def test_invalid_link_is_rejected(self):
        entry = {
            "title": "Stone Island jacket",
            "summary": "Price: 50 EUR",
            "link": "javascript:alert(1)",
        }
        self.assertIsNone(matching_item(entry))

    def test_tracking_query_is_removed(self):
        self.assertEqual(
            normalize_link("https://example.com/item/1?utm_campaign=x"),
            "https://example.com/item/1",
        )


class NaturalSearchTests(unittest.TestCase):
    def test_daily_rental_request(self):
        from bot import parse_natural_search
        query, budget, days, city = parse_natural_search(
            "Сними квартиру посуточно до 250 BYN на два дня в Минске"
        )
        self.assertEqual(query, "квартиру посуточно")
        self.assertEqual(budget, 250)
        self.assertEqual(days, 2)
        self.assertEqual(city, "Минск")

    def test_any_price_does_not_create_budget(self):
        from bot import parse_natural_search
        query, budget, days, city = parse_natural_search(
            "Найди Stone Island Off Dye в Бресте, любая цена вообще"
        )
        self.assertEqual(query, "Stone Island Off Dye")
        self.assertIsNone(budget)
        self.assertIsNone(days)
        self.assertEqual(city, "Брест")


    def test_all_belarus_phrase_is_not_sent_as_search_keyword(self):
        from bot import parse_natural_search
        query, budget, days, city = parse_natural_search(
            "Найди Stone Island Off Dye по всей Беларуси"
        )
        self.assertEqual(query, "Stone Island Off Dye")
        self.assertIsNone(city)

    def test_numeric_budget_without_currency(self):
        from bot import parse_natural_search
        query, budget, days, city = parse_natural_search(
            "найди комнату до 300 в Гродно"
        )
        self.assertEqual(query, "комнату")
        self.assertEqual(budget, 300)
        self.assertEqual(city, "Гродно")

    def test_budget_with_compact_ruble_suffix(self):
        from bot import parse_natural_search
        query, budget, days, city = parse_natural_search(
            "сними квартиру до 250р. в Минске"
        )
        self.assertEqual(query, "квартиру")
        self.assertEqual(budget, 250)
        self.assertEqual(city, "Минск")


class KufarTitleFilteringTests(unittest.TestCase):
    def test_rejects_off_dye_title_with_multiple_brands_in_parentheses(self):
        from bot import is_off_dye_keyword_spam
        title = "Худи Jordan off dye (Nike adidas stone island), цена 120 р."
        self.assertTrue(is_off_dye_keyword_spam(title, "Stone Island Off Dye"))

    def test_keeps_off_dye_title_with_simple_authenticity_note(self):
        from bot import is_off_dye_keyword_spam
        self.assertFalse(
            is_off_dye_keyword_spam(
                "Stone Island Off Dye (оригинал)", "Stone Island Off Dye"
            )
        )

    def test_filter_does_not_affect_other_searches(self):
        from bot import is_off_dye_keyword_spam
        self.assertFalse(
            is_off_dye_keyword_spam(
                "Худи Jordan (Nike adidas stone island)", "худи Jordan"
            )
        )

    def test_removes_kufar_listing_number_from_title(self):
        from bot import clean_kufar_title
        self.assertEqual(
            clean_kufar_title(
                "Худи Jordan off dye - Объявление №1097622249"
            ),
            "Худи Jordan off dye",
        )

    def test_removes_kufar_seo_suffix_and_number(self):
        from bot import clean_kufar_title
        self.assertEqual(
            clean_kufar_title(
                "Худи Jordan off dye, цена 120 р. купить в Гродно на Куфаре - Объявление №1097622249"
            ),
            "Худи Jordan off dye",
        )


class KufarParsingTests(unittest.TestCase):
    def test_city_forms(self):
        from bot import detect_belarus_city
        self.assertEqual(detect_belarus_city("Объявление купить в Минске"), "Минск")
        self.assertEqual(detect_belarus_city("Сдаётся квартира в Новополоцке"), "Новополоцк")
        self.assertIsNone(detect_belarus_city("Доставка по Беларуси"))

    def test_price_from_metadata(self):
        from bot import kufar_price
        self.assertEqual(
            kufar_price({"og:title": "Куртка, цена 950 р."}),
            "950 BYN",
        )

    def test_price_from_visible_page_text(self):
        from bot import kufar_price
        self.assertEqual(
            kufar_price({}, "Цена 1 200 р. Состояние отличное"),
            "1200 BYN",
        )

    def test_missing_price_is_not_invented(self):
        from bot import kufar_price
        self.assertIsNone(kufar_price({}, "Цена договорная"))

    def test_only_real_kufar_listing_links_are_accepted(self):
        from bot import normalize_kufar_item_link
        self.assertEqual(
            normalize_kufar_item_link("/item/123456"),
            "https://www.kufar.by/item/123456",
        )
        self.assertEqual(
            normalize_kufar_item_link("https://www.kufar.by/item/123456?ref=feed"),
            "https://www.kufar.by/item/123456",
        )
        self.assertEqual(
            normalize_kufar_item_link("https://evil.example/item/123456"),
            "",
        )
        self.assertEqual(
            normalize_kufar_item_link("https://www.kufar.by/account/login"),
            "",
        )



class FavoritesMiniAppTests(unittest.TestCase):
    def test_favorite_app_url_contains_listing_fields(self):
        from urllib.parse import parse_qs, urlsplit
        from bot import favorite_app_url
        import bot

        previous = bot.WEBAPP_URL
        try:
            bot.WEBAPP_URL = "https://pidrpidr85-sudo.github.io/stone-island-woaquez/"
            url = favorite_app_url({
                "link": "https://www.kufar.by/item/123456",
                "title": "Stone Island Off Dye",
                "city": "Минск",
                "price": "950 BYN",
                "image": "https://images.example/item.jpg",
            })
            parsed = urlsplit(url)
            values = parse_qs(parsed.query)
            self.assertEqual(values["save"], ["123456"])
            self.assertEqual(values["title"], ["Stone Island Off Dye"])
            self.assertEqual(values["city"], ["Минск"])
            self.assertEqual(values["price"], ["950 BYN"])
            self.assertEqual(values["link"], ["https://www.kufar.by/item/123456"])
        finally:
            bot.WEBAPP_URL = previous

    def test_favorite_app_rejects_non_kufar_item_path(self):
        from bot import favorite_app_url
        import bot

        previous = bot.WEBAPP_URL
        try:
            bot.WEBAPP_URL = "https://pidrpidr85-sudo.github.io/stone-island-woaquez/"
            self.assertEqual(
                favorite_app_url({"link": "https://evil.example/item/123"}),
                bot.WEBAPP_URL,
            )
        finally:
            bot.WEBAPP_URL = previous


if __name__ == "__main__":
    unittest.main()
