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


if __name__ == "__main__":
    unittest.main()
