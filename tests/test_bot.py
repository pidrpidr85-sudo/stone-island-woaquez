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

    def test_numeric_budget_without_currency(self):
        from bot import parse_natural_search
        query, budget, days, city = parse_natural_search(
            "найди комнату до 300 в Гродно"
        )
        self.assertEqual(query, "комнату")
        self.assertEqual(budget, 300)
        self.assertEqual(city, "Гродно")


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

    def test_numeric_budget_without_currency(self):
        from bot import parse_natural_search
        query, budget, days, city = parse_natural_search(
            "найди комнату до 300 в Гродно"
        )
        self.assertEqual(query, "комнату")
        self.assertEqual(budget, 300)
        self.assertEqual(city, "Гродно")


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


if __name__ == "__main__":
    unittest.main()
