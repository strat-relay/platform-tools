import unittest

from .paging import TF_SECONDS, merge_pages, normalize_page, page_ranges


class PagingTests(unittest.TestCase):
    def test_half_open_pages_have_no_boundary_duplicate_or_gap(self):
        for tf in ("M5", "M15", "H1", "H4"):
            step = TF_SECONDS[tf]
            ranges = page_ranges(0, step*7, tf, 3)
            self.assertEqual(ranges, [(0, step*3), (step*3, step*6), (step*6, step*7)])
            rows = [{"time": i*step} for i in range(7)]
            pages = [normalize_page(rows, a, b, tf) for a,b in ranges]
            merged = merge_pages(pages)
            self.assertEqual([x["time"] for x in merged], [i*step for i in range(7)])

    def test_defensive_dedup_and_sort(self):
        merged = merge_pages([[{"time": 900}, {"time": 0}], [{"time": 900}, {"time": 1800}]])
        self.assertEqual([x["time"] for x in merged], [0, 900, 1800])

    def test_non_aligned_explicit_timestamp_range_preserves_contract(self):
        rows = [{"time": 300}, {"time": 600}, {"time": 900}]
        self.assertEqual([x["time"] for x in normalize_page(rows, 450, 901, "M5")], [600, 900])


if __name__ == "__main__":
    unittest.main()
