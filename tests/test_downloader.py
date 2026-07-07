import unittest
from torbox_downloader.downloader import parse_size_to_bytes, is_file_size_matching

class TestDownloaderUtils(unittest.TestCase):
    def test_parse_size_to_bytes(self):
        self.assertEqual(parse_size_to_bytes("45B"), 45)
        self.assertEqual(parse_size_to_bytes("304KB"), 304 * 1024)
        self.assertEqual(parse_size_to_bytes("2MB"), 2 * 1024 * 1024)
        self.assertEqual(parse_size_to_bytes("1.1 GB"), int(1.1 * 1024 * 1024 * 1024))
        self.assertIsNone(parse_size_to_bytes("invalid"))

    def test_is_file_size_matching_small(self):
        # Small file (<10MB) requires strict 1MB tolerance
        self.assertTrue(is_file_size_matching(5 * 1024 * 1024, "5MB"))
        self.assertTrue(is_file_size_matching(5.5 * 1024 * 1024, "5MB")) # within 1MB
        self.assertFalse(is_file_size_matching(7 * 1024 * 1024, "5MB")) # outside 1MB

    def test_is_file_size_matching_large(self):
        # Large file (>=10MB) uses relative 50%-200% tolerance
        # Expected size: 1GB = 1073741824 bytes
        expected_size_str = "1GB"
        
        # 1.35 GB = 1449551462 bytes (~135% of expected size, within 50%-200% tolerance)
        self.assertTrue(is_file_size_matching(1449551462, expected_size_str))
        
        # 0.6 GB = 644245094 bytes (~60% of expected size, within 50%-200% tolerance)
        self.assertTrue(is_file_size_matching(644245094, expected_size_str))
        
        # 250 MB = 262144000 bytes (24% of expected size, outside 50%-200% tolerance)
        self.assertFalse(is_file_size_matching(262144000, expected_size_str))

if __name__ == '__main__':
    unittest.main()
