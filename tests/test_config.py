import tempfile
import tomllib
import unittest
from pathlib import Path

import lofi


class LoadConfigTests(unittest.TestCase):
    def _write(self, text: str) -> Path:
        f = tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False
        )
        f.write(text)
        f.close()
        return Path(f.name)

    def test_parses_well_formed_config(self) -> None:
        path = self._write(
            '[synthwave]\n'
            'url = "https://example.com/sw"\n'
            'title = "synthwave radio"\n'
            '\n'
            '[lofi]\n'
            'url = "https://example.com/lo"\n'
        )
        bookmarks = lofi.load_config(path)
        self.assertEqual(len(bookmarks), 2)
        self.assertEqual(bookmarks[0].name, "synthwave")
        self.assertEqual(bookmarks[0].url, "https://example.com/sw")
        self.assertEqual(bookmarks[0].title, "synthwave radio")
        self.assertEqual(bookmarks[1].name, "lofi")
        self.assertEqual(bookmarks[1].url, "https://example.com/lo")
        self.assertIsNone(bookmarks[1].title)

    def test_malformed_toml_raises(self) -> None:
        path = self._write("[synthwave\nurl = ?\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            lofi.load_config(path)

    def test_entry_without_url_raises(self) -> None:
        path = self._write('[synthwave]\ntitle = "x"\n')
        with self.assertRaisesRegex(ValueError, "missing 'url'"):
            lofi.load_config(path)


class SeedConfigTests(unittest.TestCase):
    def test_creates_file_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "streams.toml"
            self.assertFalse(path.exists())
            lofi.seed_config_if_missing(path)
            self.assertTrue(path.exists())
            bookmarks = lofi.load_config(path)
            names = [b.name for b in bookmarks]
            self.assertEqual(names, ["synthwave", "lofi", "relax"])

    def test_leaves_existing_file_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "streams.toml"
            path.write_text('[only]\nurl = "https://x"\n')
            lofi.seed_config_if_missing(path)
            bookmarks = lofi.load_config(path)
            self.assertEqual([b.name for b in bookmarks], ["only"])


if __name__ == "__main__":
    unittest.main()
