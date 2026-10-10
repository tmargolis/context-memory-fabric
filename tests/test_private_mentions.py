import unittest

from scripts.check_private_mentions import scan


class PrivacyCheckTests(unittest.TestCase):
    def test_case_insensitive_and_line_accurate(self):
        self.assertEqual(scan("public\nEXAMPLE PERSON\n", [r"example person"]), [(2, 1)])

    def test_scoped_pattern_avoids_public_substrings(self):
        self.assertEqual(scan("someexample example", [r"\bexample\b"]), [(1, 1)])

    def test_codebase_has_no_private_mentions(self):
        import subprocess
        # Run the check script, allowing missing denylist (returns 0) but failing if denylist is present and finds matches
        result = subprocess.run(["python3", "scripts/check_private_mentions.py", "--allow-missing"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"Privacy check failed:\n{result.stdout}")
