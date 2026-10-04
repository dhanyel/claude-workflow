import json, os, unittest
from .helpers import PLUGIN, import_bin

i18n = import_bin("i18n")


class TestI18n(unittest.TestCase):
    def test_locales_have_the_same_keys(self):
        loaded = {}
        for lang in ("en", "pt-BR"):
            with open(os.path.join(PLUGIN, "locales", f"{lang}.json")) as f:
                loaded[lang] = set(json.load(f))
        self.assertEqual(loaded["en"], loaded["pt-BR"])

    def test_unsupported_language_falls_back_to_en(self):
        self.assertEqual(i18n.resolve_language("fr"), "en")
        self.assertEqual(i18n.resolve_language("pt-BR"), "pt-BR")
