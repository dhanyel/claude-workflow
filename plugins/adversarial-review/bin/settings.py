#!/usr/bin/env python3
"""settings: which backend reviews, with what model and credential -- from the USER'S ENVIRONMENT only.

⚠️ Never from the repo's .claude/plan-gate.json: a hostile repo must not choose where the plan and the key go
(same reasoning as plan-gate R70/R71/R78). ⚠️ Nothing is defaulted: no backend means a refusal that explains the
three options -- a silent fallback once spent 11 min of somebody's personal quota.
"""
import dataclasses, os, stat, urllib.parse

BACKENDS = ("codex", "opencode", "endpoint")
ENV_BACKEND = "ADVERSARIAL_REVIEW_BACKEND"
ENV_MODEL = "ADVERSARIAL_REVIEW_MODEL"
ENV_URL = "ADVERSARIAL_REVIEW_ENDPOINT_URL"
ENV_TOKEN = "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN"
ENV_TOKEN_FILE = "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE"
ENV_THINKING = "ADVERSARIAL_REVIEW_THINKING"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
MAX_TOKEN_BYTES = 4096


class SettingsError(ValueError):
    """A refusal. `key` is a locale key; `kwargs` fill its placeholders and NEVER carry the token."""

    def __init__(self, key, **kwargs):
        super().__init__(key)
        self.key, self.kwargs = key, kwargs


@dataclasses.dataclass(frozen=True)
class Settings:
    backend: str
    model: str = ""
    endpoint_url: str = ""
    token: str = dataclasses.field(default="", repr=False)
    thinking: bool = False

    @property
    def endpoint_host(self):
        return urllib.parse.urlsplit(self.endpoint_url).hostname or ""


def url_problem(url):
    """Why `url` cannot be the endpoint, or None. https, or http only on loopback; no credentials, query or
    fragment (a URL ends up in error texts and in the review's route line)."""
    if not url or any(c.isspace() for c in url):
        return "empty or contains whitespace"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http"):
        return "must start with https://"
    if not parts.hostname:
        return "must name a host"
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        return "http:// is accepted only for 127.0.0.1, ::1 or localhost"
    if "@" in parts.netloc:
        return "must not carry credentials"
    if parts.query or parts.fragment or "?" in url or "#" in url:
        return "must not have a query or a fragment"
    try:
        parts.port
    except ValueError:
        return "has an invalid port"
    return None


def _read_token_file(path):
    # ⚠️ Errors name the VARIABLE, never its value: people paste the raw token into ..._TOKEN_FILE by mistake.
    if not os.path.isabs(path):
        raise SettingsError("settings.token_file_not_absolute", var=ENV_TOKEN_FILE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)  # NONBLOCK: a FIFO must not hang the review
    except OSError as e:
        raise SettingsError("settings.token_file_unreadable", var=ENV_TOKEN_FILE, error=e.strerror) from None
    try:
        # One fd for the checks and the read: no stat/open race.
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SettingsError("settings.token_file_not_regular", path=path)
        if st.st_mode & 0o077:
            raise SettingsError("settings.token_file_mode", path=path, mode=oct(st.st_mode & 0o777))
        try:
            data = b""
            while len(data) <= MAX_TOKEN_BYTES:
                chunk = os.read(fd, MAX_TOKEN_BYTES + 1 - len(data))
                if not chunk:
                    break
                data += chunk
        except OSError as e:
            raise SettingsError("settings.token_file_unreadable", var=ENV_TOKEN_FILE, error=e.strerror) from None
    finally:
        os.close(fd)
    if len(data) > MAX_TOKEN_BYTES:
        raise SettingsError("settings.token_file_too_big", limit=MAX_TOKEN_BYTES)
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        bad = True
    if bad:  # raised outside the except block: __context__ would keep the bytes
        raise SettingsError("settings.token_file_not_text")


def _token(env):
    raw, path = env.get(ENV_TOKEN), env.get(ENV_TOKEN_FILE)
    if raw and path:
        raise SettingsError("settings.token_both")
    if raw:
        token = raw.strip()
    elif path:
        token = _read_token_file(os.path.expanduser(path))
    else:
        raise SettingsError("settings.no_token")
    if not token or any(c.isspace() for c in token):
        raise SettingsError("settings.token_empty")
    return token


def resolve(environ=None):
    env = os.environ if environ is None else environ
    backend = (env.get(ENV_BACKEND) or "").strip()
    if not backend:
        raise SettingsError("settings.no_backend")
    if backend not in BACKENDS:
        raise SettingsError("settings.unknown_backend", value=backend, choices=", ".join(BACKENDS))
    model = (env.get(ENV_MODEL) or "").strip()
    thinking = (env.get(ENV_THINKING) or "").strip().lower() == "on"
    if backend == "codex":
        return Settings("codex", model=model, thinking=thinking)
    if backend == "opencode":
        provider, sep, upstream = model.partition("/")
        if not (sep and provider and upstream):
            raise SettingsError("settings.opencode_model", value=model)
        return Settings("opencode", model=model, thinking=thinking)
    if not model or any(c.isspace() for c in model):
        raise SettingsError("settings.endpoint_model", value=model)
    url = (env.get(ENV_URL) or "").strip()
    problem = url_problem(url)
    if problem:
        raise SettingsError("settings.endpoint_url", problem=problem)
    return Settings("endpoint", model=model, endpoint_url=url.rstrip("/"), token=_token(env), thinking=thinking)
