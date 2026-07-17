import unittest

from app.chunking import split_text


class SplitTextTests(unittest.TestCase):
    def test_short_and_empty_text(self) -> None:
        self.assertEqual(split_text("", 10), [])
        self.assertEqual(split_text("hello", 10), ["hello"])

    def test_preserves_every_character(self) -> None:
        text = "Первый абзац.\n\nВторой абзац с emoji 🤖 и длинным окончанием."
        chunks = split_text(text, 20)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(0 < len(chunk) <= 20 for chunk in chunks))

    def test_hard_split_when_no_boundary_exists(self) -> None:
        self.assertEqual(split_text("abcdefghij", 4), ["abcd", "efgh", "ij"])

    def test_rejects_non_positive_limit(self) -> None:
        with self.assertRaises(ValueError):
            split_text("x", 0)
