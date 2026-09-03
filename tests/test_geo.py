import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import unittest

from src.geo import haversine_km


class HaversineTest(unittest.TestCase):
    # Kitsilano Beach and the Vancouver Maritime Museum, a few hundred metres
    # apart, and Marine Gateway well to the south.
    KITS_BEACH = (49.2753, -123.1532)
    MARITIME_MUSEUM = (49.2775, -123.1478)
    MARINE_GATEWAY = (49.2097, -123.1162)

    def test_same_point_is_zero(self):
        self.assertEqual(haversine_km(*self.KITS_BEACH, *self.KITS_BEACH), 0.0)

    def test_short_distance_is_a_few_hundred_metres(self):
        km = haversine_km(*self.KITS_BEACH, *self.MARITIME_MUSEUM)
        self.assertGreater(km, 0.3)
        self.assertLess(km, 0.7)

    def test_longer_distance_is_several_kilometres(self):
        km = haversine_km(*self.KITS_BEACH, *self.MARINE_GATEWAY)
        self.assertGreater(km, 7)
        self.assertLess(km, 10)

    def test_symmetric(self):
        there = haversine_km(*self.KITS_BEACH, *self.MARINE_GATEWAY)
        back = haversine_km(*self.MARINE_GATEWAY, *self.KITS_BEACH)
        self.assertAlmostEqual(there, back, places=9)

    def test_orders_correctly(self):
        near = haversine_km(*self.KITS_BEACH, *self.MARITIME_MUSEUM)
        far = haversine_km(*self.KITS_BEACH, *self.MARINE_GATEWAY)
        self.assertLess(near, far)


if __name__ == "__main__":
    unittest.main()
