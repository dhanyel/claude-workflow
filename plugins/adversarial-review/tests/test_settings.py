import os, stat, tempfile, unittest
from .helpers import import_bin

settings = import_bin("settings")
TOKEN = "s3cr3t-token-value"


def env(**kw):
    return {f"ADVERSARIAL_REVIEW_{k}": v for k, v in kw.items()}


class TestSettings(unittest.TestCase):
    def refused(self, environ, key):
        with self.assertRaises(settings.SettingsError) as cm:
            settings.resolve(environ)
        self.assertEqual(cm.exception.key, key)
        self.assertNotIn(TOKEN, str(cm.exception) + repr(cm.exception.kwargs))

    def test_no_backend_is_refused(self):
        self.refused({}, "settings.no_backend")

    def test_unknown_backend_is_refused_never_defaulted(self):
        self.refused(env(BACKEND="gpt"), "settings.unknown_backend")

    def test_codex_needs_nothing_else(self):
        s = settings.resolve(env(BACKEND="codex"))
        self.assertEqual((s.backend, s.token, s.endpoint_url), ("codex", "", ""))

    def test_opencode_needs_provider_slash_model(self):
        self.refused(env(BACKEND="opencode"), "settings.opencode_model")
        for bad in ("glm-5.3", "zai/", "/glm"):
            self.refused(env(BACKEND="opencode", MODEL=bad), "settings.opencode_model")
        self.assertEqual(settings.resolve(env(BACKEND="opencode", MODEL="zai-coding-plan/glm-5.3")).model,
                         "zai-coding-plan/glm-5.3")

    def test_endpoint_needs_model_url_and_token(self):
        base = env(BACKEND="endpoint", MODEL="glm-5.3", ENDPOINT_URL="https://gw.example", ENDPOINT_TOKEN=TOKEN)
        s = settings.resolve(base)
        self.assertEqual((s.endpoint_url, s.endpoint_host, s.token), ("https://gw.example", "gw.example", TOKEN))
        self.refused(dict(base, ADVERSARIAL_REVIEW_MODEL=""), "settings.endpoint_model")
        self.refused(dict(base, ADVERSARIAL_REVIEW_ENDPOINT_URL=""), "settings.endpoint_url")
        self.refused({k: v for k, v in base.items() if k != "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN"}, "settings.no_token")

    def test_http_only_on_loopback(self):
        base = env(BACKEND="endpoint", MODEL="m", ENDPOINT_TOKEN=TOKEN)
        for ok in ("http://127.0.0.1:8789", "http://localhost:8789", "http://[::1]:8789", "https://gw.example/v1"):
            settings.resolve(dict(base, ADVERSARIAL_REVIEW_ENDPOINT_URL=ok))
        for bad in ("http://gw.example", "ftp://gw.example", "https://user:pw@gw.example", "https://gw.example?x=1",
                    "https://gw.example#f", "https:///nohost", "https://gw .example"):
            self.refused(dict(base, ADVERSARIAL_REVIEW_ENDPOINT_URL=bad), "settings.endpoint_url")

    def test_token_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "token")
            with open(path, "w") as f:
                f.write(TOKEN + "\n")
            base = env(BACKEND="endpoint", MODEL="m", ENDPOINT_URL="https://gw.example", ENDPOINT_TOKEN_FILE=path)
            os.chmod(path, 0o644)
            self.refused(base, "settings.token_file_mode")
            os.chmod(path, 0o600)
            self.assertEqual(settings.resolve(base).token, TOKEN)
            self.refused(dict(base, ADVERSARIAL_REVIEW_ENDPOINT_TOKEN=TOKEN), "settings.token_both")
            self.refused(dict(base, ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE=d), "settings.token_file_not_regular")

    def test_repr_never_shows_the_token(self):
        s = settings.resolve(env(BACKEND="endpoint", MODEL="m", ENDPOINT_URL="https://gw.example",
                                 ENDPOINT_TOKEN=TOKEN))
        self.assertNotIn(TOKEN, repr(s))

    def test_thinking(self):
        self.assertTrue(settings.resolve(env(BACKEND="codex", THINKING="on")).thinking)
        self.assertFalse(settings.resolve(env(BACKEND="codex")).thinking)

    def test_a_raw_token_in_the_file_variable_is_never_echoed(self):
        base = env(BACKEND="endpoint", MODEL="m", ENDPOINT_URL="https://gw.example")
        self.refused(dict(base, ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE=TOKEN), "settings.token_file_not_absolute")
        self.refused(dict(base, ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE="/nonexistent/" + TOKEN),
                     "settings.token_file_unreadable")

    def refused_file(self, key, content=None, mode=0o600, prepare=None):
        """Point the token variable at a file made in a temp dir (`prepare(dir, path)` may return another path)."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "token")
            if content is not None:
                with open(path, "wb") as f:
                    f.write(content)
                os.chmod(path, mode)
            target = prepare(d, path) if prepare else path
            environ = env(BACKEND="endpoint", MODEL="m", ENDPOINT_URL="https://gw.example", ENDPOINT_TOKEN_FILE=target)
            with self.assertRaises(settings.SettingsError) as cm:
                settings.resolve(environ)
            self.assertEqual(cm.exception.key, key)
            self.assertNotIn(TOKEN, str(cm.exception) + repr(cm.exception.kwargs))
            self.assertIsNone(cm.exception.__context__)

    def test_symlink_to_a_group_readable_target_is_refused(self):
        def prep(d, path):
            link = os.path.join(d, "link")
            os.symlink(path, link)
            return link
        self.refused_file("settings.token_file_mode", content=(TOKEN + "\n").encode(), mode=0o644, prepare=prep)

    def test_a_fifo_is_refused_without_blocking(self):
        def prep(d, path):
            fifo = os.path.join(d, "fifo")
            os.mkfifo(fifo, 0o600)
            return fifo
        self.refused_file("settings.token_file_not_regular", prepare=prep)

    def test_inner_whitespace_in_the_file_is_refused(self):
        self.refused_file("settings.token_empty", content=b"abc def\n")

    def test_an_oversized_file_is_refused_not_truncated(self):
        self.refused_file("settings.token_file_too_big", content=b"a" * (settings.MAX_TOKEN_BYTES + 1))

    def test_a_non_utf8_file_is_refused_without_echoing_it(self):
        self.refused_file("settings.token_file_not_text", content=b"\xff\xfe" + TOKEN.encode())
