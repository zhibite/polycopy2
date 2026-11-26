import unittest

from polycopy.api import ClobApi


class FakeHttp:
    """Return canned responses keyed by the endpoint's last path segment."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def post_json(self, url, body):
        self.calls.append((url, body))
        return self.responses.get(url.rsplit("/", 1)[-1])

    def get_json(self, url, params=None):
        raise AssertionError("unexpected GET")


class ClobPricesTest(unittest.TestCase):
    def test_midpoints_batch(self):
        http = FakeHttp({"midpoints": {"11": "0.475", "22": "0.3"}})
        prices = ClobApi(http).midpoints(["11", "22"])
        self.assertEqual(prices, {"11": 0.475, "22": 0.3})
        self.assertEqual(http.calls[0][1],
                         [{"token_id": "11"}, {"token_id": "22"}])

    def test_current_prices_falls_back_to_last_trade(self):
        http = FakeHttp({
            "midpoints": {"11": "0.475"},
            "last-trades-prices": [
                {"token_id": "22", "price": "0.61", "side": "BUY"},
            ],
        })
        prices = ClobApi(http).current_prices(["11", "22", "33"])
        self.assertEqual(prices, {"11": 0.475, "22": 0.61})
        self.assertEqual(http.calls[1][1],
                         [{"token_id": "22"}, {"token_id": "33"}])

    def test_bad_values_are_ignored(self):
        http = FakeHttp({"midpoints": {"11": "oops", "22": "5", "33": "0"}})
        self.assertEqual(ClobApi(http).midpoints(["11", "22", "33"]),
                         {"33": 0.0})

    def test_empty_input_skips_http(self):
        http = FakeHttp({})
        self.assertEqual(ClobApi(http).current_prices([]), {})
        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
