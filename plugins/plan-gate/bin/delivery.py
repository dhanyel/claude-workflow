#!/usr/bin/env python3
"""delivery: the MR/PR report -- build the description, publish it, read it back, comment the ⏱ table (spec §6).

    delivery.py platform
    delivery.py render --input F --out O [--comment-out C]
    delivery.py publish --title T --source S --target B --body-file F     (ok -> stamps `delivered`)
    delivery.py update --ref N --body-file F
    delivery.py verify --ref N --expected-len L
    delivery.py comment --issue N --body-file F
    delivery.py notes --issue N                                            (id, length, ⏱ heading of each note)
    delivery.py open-request --branch S

Every command prints ONE JSON object on stdout and exits 0 only when its outcome is `ok`
(1 error, 2 bad usage, 3 refused, 4 retryable, 5 unknown).

⚠️ R55/R58 -- the scope: this module CREATES a request, rewrites its DESCRIPTION, READS it back and adds a NOTE to
an issue. It never pushes, merges, closes or reopens anything. GET reads (a request, its notes); POST creates a
request or a note; PUT
(GitLab, `merge_requests/<iid>`) and PATCH (GitHub, `pulls/<n>`) carry the description/body and nothing else.
test_delivery checks every request against that allowlist and reads the code for anything else.

⚠️ R70 -- a token is sent only to a host the person's ENVIRONMENT names: GITLAB_TOKEN to gitlab.com or the
host of GITLAB_HOST; GITHUB_TOKEN to api.github.com or the host of GH_HOST (GitHub Enterprise, `/api/v3`). The repo
config never adds a host -- it is committed with the repo, so a cloned repo could name its own. A host outside the
set (bitbucket.org, codeberg.org, the origin's own host, a repo `api_url`) gets no token -- `checked_target` refuses
it. An explicit `base_url` passed to the library functions is the caller's decision.

⚠️ R78 -- and the PROJECT comes from `origin`, so the origin must live on that same API host (github.com counts as
api.github.com), or on the host the environment names. Otherwise a hostile origin plus a repo `api_url` pointing at
a trusted host would spend the token on whatever project the hostile origin names.

⚠️ R71 -- a token never travels in cleartext: a token-bearing base is https. Plain http is bound only when the
person wrote the scheme in the environment (`GITLAB_HOST=http://host`, `GH_HOST=http://host`), and only for that
exact host and port. A repo `api_url` with http is never bound -- it cannot downgrade a trusted host.

⚠️ R64 -- GitLab runs a line starting with `/` as a quick action (`/close`, `/merge`): every free text and every
GitLab body gets a zero-width space before such a slash.

⚠️ Tokens come from GITHUB_TOKEN / GITLAB_TOKEN, travel only in a header (`Authorization: Bearer` / `PRIVATE-TOKEN`)
and are scrubbed from every returned dict, error text and JSON line. The `origin` URL is never echoed: it may
carry credentials.

⚠️ R53 -- outcomes by STATUS: 2xx ok; 429 retryable; other 4xx refused; 5xx, 1xx, 3xx, or no answer AFTER the
request left: unknown -- the request may exist, so the caller looks it up (`open-request`, or `notes` for a comment) before ANY retry. Nothing here
retries. A failure before sending, or a status outside 100-599, is an error.
"""
import argparse, json, os, re, socket, sys, urllib.error, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import config, i18n, timeline

PLUGIN = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LANGS = ("en", "pt-BR")
GITHUB_API = "https://api.github.com"
TOKEN_VARS = {"github": "GITHUB_TOKEN", "gitlab": "GITLAB_TOKEN"}
POST_TIMEOUT = 30
LOOKUP_TIMEOUT = 10          # R56: `phase.py start` never waits longer than this for the remote
MAX_BODY_BYTES = 1 << 20
RC = {"ok": 0, "error": 1, "refused": 3, "retryable": 4, "unknown": 5}
MR_PLACEHOLDERS = ("summary", "changes", "verification", "timing", "closes")
COMMENT_PLACEHOLDERS = ("timing", "request")
RE_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
RE_NUMBER = re.compile(r"#?([1-9]\d*)")
GITLAB_COM = "gitlab.com"
GITHUB_API_HOST = "api.github.com"
HOST_VARS = {"github": "GH_HOST", "gitlab": "GITLAB_HOST"}
ZWSP = "\u200b"
RE_SLASH_LINE = re.compile(r"^([ \t]*)/", re.M)
RE_SCP = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>[^/\s].*)$")


class DeliveryError(Exception):
    """Nothing was sent: no remote, no token, a bad argument. `code` is short and never carries a secret."""
    def __init__(self, code, host=None):
        super().__init__(code if host is None else f"{code}: {host}")
        self.code = code if host is None else f"{code}: {host}"
        self.host = host


class NotSent(Exception):
    """Raised by a transport when the request provably never left (bad URL, DNS failure, connection refused)."""


class RequestFailed(Exception):
    """A read (lookup) that did not answer 2xx. `reason` = the status, `no token`, or the error class."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ----------------------------------------------------------------------------------------------- secrets

def token(plat):
    """The token of `plat` from GITHUB_TOKEN / GITLAB_TOKEN, or None. Never printed."""
    if plat not in TOKEN_VARS:
        return None
    return os.environ.get(TOKEN_VARS[plat], "").strip() or None


def _secrets(extra=()):
    found = set()
    for var in TOKEN_VARS.values():
        value = os.environ.get(var) or ""
        found.update(s for s in (value, value.strip()) if s)
    found.update(s for s in extra if s)
    return sorted(found, key=len, reverse=True)


def scrub(value, extra=()):
    """`value` with every token (and every `extra` secret) replaced by `***` in every VALUE, at any depth."""
    secrets = _secrets(extra)

    def walk(v):
        if isinstance(v, str):
            for s in secrets:
                v = v.replace(s, "***")
            return v
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}          # R63: keys are ours, never a secret
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        return v
    return walk(value)


def _describe(exc):
    return scrub(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)


# ------------------------------------------------------------------------------------------------ remote

def parse_remote(url):
    """(scheme, host, port, path) of a git remote -- `git@host:g/sub/p.git`, `ssh://…`, `https://…`.

    ValueError (never naming the URL, which may carry credentials) when it is not a host remote.
    """
    url = (url or "").strip()
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        try:
            host, port = parts.hostname, parts.port
        except ValueError:
            raise ValueError("the remote has an invalid port") from None
        scheme, path = parts.scheme.lower(), parts.path
    else:
        m = RE_SCP.match(url)
        if not m:
            raise ValueError("the remote is not a host remote")
        scheme, host, port, path = "ssh", m.group("host"), None, m.group("path")
    # R68: `git@evil.com@gitlab.com:g/p` -- one reader takes the host before the second `@`, another after it. The
    # binding check and the connection must never disagree, so an ambiguous host is no remote at all.
    if "@" in (host or "") or ("://" in url and urllib.parse.urlsplit(url).netloc.count("@") > 1):
        raise ValueError("the remote host is ambiguous")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        raise ValueError("the remote is not a host remote")
    return scheme, host.lower(), port, path


def _remote_secrets(url):
    """The user and password of an http(s) remote: `https://oauth2:<token>@host/…` is a token in disguise."""
    if "://" not in (url or ""):
        return ()
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        return ()
    return tuple(s for s in (parts.username, parts.password) if s)


def platform(remote_url):
    """`github` for a github.com remote -- or for the GitHub Enterprise host named in GH_HOST --, `gitlab` for
    anything else (R57, R70)."""
    host = parse_remote(remote_url)[1]
    return "github" if host == "github.com" or host == gh_host() else "gitlab"


def api_base(plat, remote_url):
    """GitHub: api.github.com (or `https://<host>/api/v3` for another host); GitLab: `https://<host>/api/v4`.

    R62/R71: https -- the only exception is the person's own `GITLAB_HOST` / `GH_HOST` written as `http://host`
    for this very host: then `http://<that host[:port]>`. An https remote keeps its port; the port of an ssh or
    http remote belongs to that protocol, so it is dropped.
    """
    scheme, host, port, _ = parse_remote(remote_url)
    if plat == "github" and host == "github.com":
        return GITHUB_API
    http = env_http(plat)
    if http and http[0] == host:
        base = "http://" + (host if http[1] == 80 else f"{host}:{http[1]}")
    else:
        base = "https://" + (f"{host}:{port}" if port and scheme == "https" else host)
    return base + ("/api/v3" if plat == "github" else "/api/v4")


def target(repo):
    """(platform, project, api base) of `origin`, with the config overrides `platform` and `api_url` (R57)."""
    cfg = config.load(repo)
    remote = timeline._git(repo, "remote", "get-url", "origin")
    if not remote:
        raise DeliveryError("no-remote")
    try:
        _, host, _, project = parse_remote(remote)
    except ValueError:
        raise DeliveryError("unrecognised-remote") from None
    plat = cfg["platform"] or platform(remote)
    project_path(plat, project)                     # a GitHub project is owner/repo, nothing else
    base = (cfg["api_url"] or api_base(plat, remote)).rstrip("/")
    return plat, project, base


def _host(url):
    value = (url or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    try:
        return (urllib.parse.urlsplit(value).hostname or "").lower() or None
    except ValueError:
        return None


def env_http(plat):
    """R71: (host, port) when the person wrote GITLAB_HOST / GH_HOST of `plat` as `http://host[:port]` -- the only
    way plain http is ever bound. None otherwise (unset, bare host, https, unparseable)."""
    value = (os.environ.get(HOST_VARS[plat]) or "").strip() if plat in HOST_VARS else ""
    if not value.lower().startswith("http://"):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        host, port = (parts.hostname or "").lower(), parts.port or 80
    except ValueError:
        return None
    return (host, port) if host else None


def gh_host():
    """The GitHub Enterprise host the person named in GH_HOST (the gh CLI convention), or None."""
    return _host(os.environ.get("GH_HOST"))


def bound_hosts(plat):
    """R70: the API hosts the token of `plat` may be sent to -- named ONLY by the person's environment.

    GITLAB_TOKEN: gitlab.com and the host of GITLAB_HOST. GITHUB_TOKEN: api.github.com and the host of GH_HOST.
    ⚠️ The repo config (`.claude/plan-gate.json`, committed with the repo) never adds a host: a hostile host X
    serving a repo whose config says `api_url: X` would otherwise receive the token. `api_url` and `platform`
    only pick a URL inside this set.
    """
    if plat == "gitlab":
        hosts = {GITLAB_COM, _host(os.environ.get("GITLAB_HOST"))}
    elif plat == "github":
        hosts = {GITHUB_API_HOST, gh_host()}
    else:
        hosts = set()
    return {h for h in hosts if h}


def origin_host(repo):
    """The host of `origin` in `repo`, or None (no repo, no remote, not a host remote)."""
    remote = timeline._git(repo, "remote", "get-url", "origin") if repo else None
    try:
        return parse_remote(remote)[1] if remote else None
    except ValueError:
        return None


def _origin_matches(plat, origin, host):
    """R78: is `origin` the API host itself (github.com = api.github.com), or the host the environment names?"""
    if not origin:
        return False
    if origin == host or (plat == "github" and origin == "github.com" and host == GITHUB_API_HOST):
        return True
    named = gh_host() if plat == "github" else _host(os.environ.get("GITLAB_HOST")) if plat == "gitlab" else None
    return origin == named


def token_bound(repo, plat, base):
    """(bound?, host): may the token of `plat` go to `base` for the project of `repo`'s origin?

    R70: the API host is one the environment names. R71: and the scheme is https -- or http on exactly the host and
    port the person wrote as `http://…` in GITLAB_HOST / GH_HOST. A repo `api_url` never downgrades a host.
    R78: and the origin (where the project name comes from) is that API host or the environment's host -- when it
    is not, the host returned is the origin's.
    """
    host = _host(base)
    if host not in bound_hosts(plat):
        return False, host
    origin = origin_host(repo)
    if not _origin_matches(plat, origin, host):
        return False, origin or host
    try:
        parts = urllib.parse.urlsplit((base or "").strip())
        scheme, port = parts.scheme.lower(), parts.port
    except ValueError:
        return False, host
    if scheme == "https":
        return True, host
    if scheme == "http":
        return env_http(plat) == (host, port or 80), host
    return False, host


def checked_target(repo):
    """`target(repo)`, refused (DeliveryError `token-not-bound-to-host: <host>`) when its API host is not one the
    token is bound to, or the origin is not on it (R70/R71/R78). Nothing is sent to an unbound host -- not even
    without the token."""
    plat, project, base = target(repo)
    bound, host = token_bound(repo, plat, base)
    if not bound:
        raise DeliveryError("token-not-bound-to-host", host)
    return plat, project, base


def project_path(plat, project):
    """`/repos/{owner}/{repo}` or `/projects/{url-encoded group/sub/proj}`."""
    if plat == "github":
        parts = (project or "").split("/")
        if len(parts) != 2 or not all(parts):
            raise DeliveryError("github-project-is-not-owner/repo")
        return "/repos/" + "/".join(urllib.parse.quote(p, safe="") for p in parts)
    if plat == "gitlab":
        if not project:
            raise DeliveryError("no-project")
        return "/projects/" + urllib.parse.quote(project, safe="")
    raise DeliveryError("unknown-platform")


def _base(plat, base_url):
    if base_url:
        return base_url.rstrip("/")
    if plat == "github":
        return GITHUB_API
    raise DeliveryError("no-api-url")


def _number(value):
    m = RE_NUMBER.fullmatch(str(value).strip()) if value is not None else None
    if not m:
        raise DeliveryError("not-a-number")
    return m.group(1)


def neutralise(text):
    """R64: a zero-width space before the `/` of every line that starts with one (after blanks), so GitLab never
    reads `/close` or `/merge` in our text as a quick action. Idempotent."""
    return RE_SLASH_LINE.sub(lambda m: m.group(1) + ZWSP + "/", text) if isinstance(text, str) else text


def describe_request(plat, item):
    """(`!9` / `#9`, its web URL) of an MR/PR as the API returned it."""
    if plat == "github":
        return f"#{item.get('number')}", item.get("html_url") or ""
    return f"!{item.get('iid')}", item.get("web_url") or ""


# --------------------------------------------------------------------------------------------- transport

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirected POST would come back as a GET without a body: return the 3xx itself instead."""
    def redirect_request(self, *args, **kwargs):
        return None


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None


def urllib_transport(timeout=POST_TIMEOUT):
    """The default `transport(method, url, headers, body) -> (status, parsed json or None)`."""
    opener = urllib.request.build_opener(_NoRedirect)

    def transport(method, url, headers, body):
        headers = dict(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
        except ValueError:
            raise NotSent("invalid URL") from None
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, _json(response.read())
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
            return e.code, _json(raw)
        except urllib.error.URLError as e:
            # nothing left the machine: the name did not resolve, or nobody accepted the connection
            if isinstance(e.reason, (socket.gaierror, ConnectionRefusedError)):
                raise NotSent(type(e.reason).__name__) from None
            if isinstance(e.reason, str) and e.reason.startswith("unknown url type"):
                raise NotSent("invalid URL") from None
            raise
        except ValueError:
            # http.client validates the URL and the headers locally, before any byte is sent
            raise NotSent("invalid request") from None
    return transport


def _headers(plat, tok):
    if plat == "github":
        return {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "plan-gate-delivery"}
    return {"PRIVATE-TOKEN": tok, "Accept": "application/json", "User-Agent": "plan-gate-delivery"}


def classify(status):
    """R53. `ok` | `refused` | `retryable` | `unknown` | `error`, by the status alone."""
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        return "error"
    if 200 <= status <= 299:
        return "ok"
    if status == 429:
        return "retryable"
    if 400 <= status <= 499:
        return "refused"
    # 5xx -- and 1xx/3xx, which no API here answers to a POST: the server got it and did not say it refused it
    return "unknown"


def _send(plat, method, url_of, payload, transport, next_step="open-request"):
    """POST/PUT/PATCH with the R53 classification. `url_of` builds the URL; anything it raises means nothing was
    sent. On `unknown`, `next` names the read that settles it before any retry."""
    sent = False
    try:
        url = url_of()
        tok = token(plat)
        if not tok:
            raise DeliveryError(f"no-token ({TOKEN_VARS[plat]} is not set)")
        headers = _headers(plat, tok)
        call = transport or urllib_transport(POST_TIMEOUT)
        sent = True                                  # from here on, the request may have reached the server
        status, body = call(method, url, headers, payload)
    except DeliveryError as e:
        return {"outcome": "error", "status": None, "sent": False, "error": e.code}
    except NotSent as e:
        return {"outcome": "error", "status": None, "sent": False, "error": _describe(e)}
    except Exception as e:
        if not sent:
            return {"outcome": "error", "status": None, "sent": False, "error": _describe(e)}
        return {"outcome": "unknown", "status": None, "sent": True, "error": _describe(e), "next": next_step}
    outcome = classify(status)
    result = {"outcome": outcome, "status": status if isinstance(status, int) else scrub(repr(status)),
              "sent": True}
    if body is not None:
        result["body"] = scrub(body)        # R63: only what the server echoed is free text; the rest is ours
    if outcome == "unknown":
        result["next"] = next_step
    return result


def _get(plat, url, transport, timeout):
    tok = token(plat)
    if not tok:
        raise DeliveryError("no-token")
    return (transport or urllib_transport(timeout))("GET", url, _headers(plat, tok), None)


# ------------------------------------------------------------------------------------------------ the API

def publish(platform, project, source, target, title, body, transport=None, base_url=None):
    """Open the MR/PR `source` -> `target`. A dict: outcome, status, sent, body (parsed), error, next."""
    if platform == "github":
        payload = {"title": title, "head": source, "base": target, "body": body}
        kind = "/pulls"
    else:
        payload = {"source_branch": source, "target_branch": target, "title": title, "description": neutralise(body)}
        kind = "/merge_requests"
    return _send(platform, "POST", lambda: _base(platform, base_url) + project_path(platform, project) + kind,
                 payload, transport)


def update(platform, project, ref, body, transport=None, base_url=None):
    """R58: rewrite the description of MR/PR `ref` -- and nothing else (no state, no merge keys). Same dict as
    publish; on `unknown` the read that settles it is `verify`."""
    if platform == "github":
        method, kind, payload = "PATCH", "/pulls/", {"body": body}
    else:
        method, kind, payload = "PUT", "/merge_requests/", {"description": neutralise(body)}
    return _send(platform, method, lambda: _base(platform, base_url) + project_path(platform, project) + kind
                 + _number(ref), payload, transport, next_step="verify")


def comment_issue(platform, project, issue, body, transport=None, base_url=None):
    """Add a note (GitLab) / comment (GitHub) to issue `issue`. Same dict as publish."""
    kind = "/comments" if platform == "github" else "/notes"
    payload = {"body": body if platform == "github" else neutralise(body)}
    return _send(platform, "POST", lambda: _base(platform, base_url) + project_path(platform, project) + "/issues/"
                 + _number(issue) + kind, payload, transport)


def verify_detail(platform, project, ref, expected_len, transport=None, base_url=None, timeout=POST_TIMEOUT):
    """{ok, status, length, error}: ok only on a 2xx whose description/body has at least `expected_len` chars."""
    try:
        if isinstance(expected_len, bool) or not isinstance(expected_len, int) or expected_len < 1:
            raise DeliveryError("expected-len-must-be-positive")
        kind = "/pulls/" if platform == "github" else "/merge_requests/"
        url = _base(platform, base_url) + project_path(platform, project) + kind + _number(ref)
        status, body = _get(platform, url, transport, timeout)
    except DeliveryError as e:
        return {"ok": False, "status": None, "length": None, "error": e.code}
    except Exception as e:
        return {"ok": False, "status": None, "length": None, "error": _describe(e)}
    if classify(status) != "ok":
        return {"ok": False, "status": status, "length": None}
    text = body.get("body" if platform == "github" else "description") if isinstance(body, dict) else None
    length = len(text) if isinstance(text, str) else 0
    return {"ok": length >= expected_len, "status": status, "length": length}


def verify(platform, project, ref, expected_len, transport=None, base_url=None):
    """True only when the MR/PR read back has a description of at least `expected_len` chars."""
    return verify_detail(platform, project, ref, expected_len, transport=transport, base_url=base_url)["ok"]


def open_request_for(platform, project, branch, transport=None, base_url=None, timeout=LOOKUP_TIMEOUT):
    """The first open MR/PR from `branch` (as the API returned it), or None when there is none.

    Any failure raises RequestFailed -- None always means "the remote said there is none", never "unknown".
    """
    try:
        path = _base(platform, base_url) + project_path(platform, project)
        if platform == "github":
            owner = project.split("/")[0]
            url = path + "/pulls?" + urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": "open"})
        else:
            url = path + "/merge_requests?" + urllib.parse.urlencode({"source_branch": branch, "state": "opened"})
        status, body = _get(platform, url, transport, timeout)
    except DeliveryError as e:
        raise RequestFailed("no token" if e.code == "no-token" else e.code) from None
    except Exception as e:
        raise RequestFailed(type(e).__name__) from None
    if classify(status) != "ok":
        raise RequestFailed(str(status))
    if not isinstance(body, list):
        raise RequestFailed("unexpected answer")
    return scrub(body[0]) if body else None


RE_TIMING_HEADING = re.compile(r"^#{1,6}[ \t]*⏱", re.M)


def notes(platform, project, issue, transport=None, base_url=None, timeout=LOOKUP_TIMEOUT):
    """R80: [{id, length, timing}] of the notes (GitLab) / comments (GitHub) of issue `issue` -- `timing` when the
    note has the ⏱ heading of the delivery comment. The read that settles a comment whose outcome was `unknown`.
    Any failure raises RequestFailed (one page of up to 100 notes)."""
    try:
        kind = "/comments" if platform == "github" else "/notes"
        url = (_base(platform, base_url) + project_path(platform, project) + "/issues/" + _number(issue) + kind
               + "?" + urllib.parse.urlencode({"per_page": 100}))
        status, body = _get(platform, url, transport, timeout)
    except DeliveryError as e:
        raise RequestFailed("no token" if e.code == "no-token" else e.code) from None
    except Exception as e:
        raise RequestFailed(type(e).__name__) from None
    if classify(status) != "ok":
        raise RequestFailed(str(status))
    if not isinstance(body, list):
        raise RequestFailed("unexpected answer")
    out = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = item.get("body") if isinstance(item.get("body"), str) else ""
        out.append({"id": item.get("id"), "length": len(text), "timing": bool(RE_TIMING_HEADING.search(text))})
    return out


def default_branch(platform, project, transport=None, base_url=None, timeout=LOOKUP_TIMEOUT):
    """The project's default branch as the API says, or None on any failure."""
    try:
        url = _base(platform, base_url) + project_path(platform, project)
        status, body = _get(platform, url, transport, timeout)
    except Exception:
        return None
    if classify(status) != "ok" or not isinstance(body, dict):
        return None
    value = body.get("default_branch")
    return value if isinstance(value, str) and value else None


def closes_line(issue, target, default_branch):
    """`Closes #N` -- always in English, the keyword the platforms read -- only when `target` is the default branch
    (R54). Otherwise None: the platform would not close the issue anyway, so the link is made by hand."""
    if not issue or not target or not default_branch or target != default_branch:
        return None
    return f"Closes #{_number(issue)}"


# ----------------------------------------------------------------------------------------------- templates

def template_path(repo, value):
    """The absolute path of the repo's `mr_template`. ValueError when it is absolute, climbs out of the repo
    (`..`, a link), or is not a readable file -- never a silent fallback to the plugin's template."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"mr_template must be a non-empty path relative to the repository (got {value!r})")
    if repo is None:
        raise ValueError(f"mr_template {value!r} is relative to a repository, and none was given")
    v = value.replace("\\", "/")
    if v.startswith(("/", "~")) or os.path.isabs(value):
        raise ValueError(f"mr_template {value!r} must be relative to the repository")
    if ".." in v.split("/"):
        raise ValueError(f"mr_template {value!r} must not contain '..'")
    root = os.path.realpath(repo)
    full = os.path.realpath(os.path.join(root, value))
    if os.path.commonpath([full, root]) != root:
        raise ValueError(f"mr_template {value!r} resolves outside the repository")
    if not os.path.isfile(full):
        raise ValueError(f"mr_template {value!r} is not a file ({full})")
    return full


def _read_template(path):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(MAX_BODY_BYTES + 1)
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"cannot read the template {path}: {e}") from e
    if len(text) > MAX_BODY_BYTES:
        raise ValueError(f"the template {path} is larger than {MAX_BODY_BYTES} bytes")
    return text


def _fill(text, values, required, path):
    present = set(RE_PLACEHOLDER.findall(text))
    unknown = present - set(values)
    if unknown:
        raise ValueError(f"the template {path} has unknown placeholders: {sorted(unknown)}")
    missing = [p for p in required if p not in present]
    if missing:
        raise ValueError(f"the template {path} lacks the placeholders: {missing}")
    out = RE_PLACEHOLDER.sub(lambda m: values[m.group(1)], text)     # one pass: values are never rescanned
    return re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"


def _code(cmd):
    return f"`` {cmd} ``" if "`" in cmd else f"`{cmd}`"


def _as_list(value):
    if value is None:
        return []
    return [neutralise(value)] if isinstance(value, str) else [neutralise(str(v)) for v in value]


def _verification(ran, not_run, lang):
    ran, not_run = _as_list(ran), _as_list(not_run)
    lines = [f"- {_code(c)}" for c in ran] or [timeline._t("delivery.ran_none", lang)]
    lines.append("")
    if not_run:
        lines.append(timeline._t("delivery.not_run", lang))
        lines.extend(f"- {_code(c)}" for c in not_run)
    else:
        lines.append(timeline._t("delivery.not_run_none", lang))
    return "\n".join(lines)


def _template(kind, lang, template, repo):
    if template is None:
        return os.path.join(PLUGIN, "templates", lang, kind)
    return template_path(repo, template)


def render(summary, changes, verification, not_run, timing_md, closes, lang, template=None, repo=None):
    """The MR/PR description. `template` = `config.load(repo)["mr_template"]` (relative to `repo`), else the
    plugin's. Without `closes` it warns on stderr: the issue has to be linked and closed by hand (R54)."""
    lang = lang if lang in LANGS else "en"
    path = _template("mr.md", lang, template, repo)
    values = {"summary": neutralise((summary or "").strip()), "changes": neutralise((changes or "").strip()),
              "verification": _verification(verification, not_run, lang), "timing": (timing_md or "").strip(),
              "closes": closes or ""}
    out = _fill(_read_template(path), values, MR_PLACEHOLDERS, path)
    if not closes:
        print(timeline._t("delivery.no_closes", lang), file=sys.stderr)
    return out


def render_comment(timing_md, lang, request_url=None):
    """The issue comment: the ⏱ table and, when known, the link to the MR/PR."""
    lang = lang if lang in LANGS else "en"
    path = _template("issue-comment.md", lang, None, None)
    request = timeline._t("delivery.comment_request", lang, url=request_url) if request_url else ""
    return _fill(_read_template(path), {"timing": (timing_md or "").strip(), "request": request},
                 COMMENT_PLACEHOLDERS[:1], path)


# ------------------------------------------------------------------------------------------------------ CLI

def _read_body(path):
    with open(path, encoding="utf-8") as f:
        text = f.read(MAX_BODY_BYTES + 1)
    if len(text) > MAX_BODY_BYTES:
        raise DeliveryError("body-file-too-large")
    return text


def _issue_of(repo, branch):
    """The issue of the demand: the last `/phase start #N`, else the number in the branch name."""
    for event in reversed(timeline.events(repo, branch)):
        detail = event.get("detail")
        if event.get("event") == "start" and isinstance(detail, dict) and detail.get("issue"):
            return str(detail["issue"])
    return timeline.issue_ref(branch)


def _default_branch(repo, plat, project, base, transport):
    if token(plat) and token_bound(repo, plat, base)[0]:
        found = default_branch(plat, project, transport=transport, base_url=base)
        if found:
            return found
    head = timeline._git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    prefix = "refs/remotes/origin/"
    return head[len(prefix):] if head and head.startswith(prefix) else None


def _cmd_render(args, repo, plat, project, base, transport):
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("target"), str) or not data["target"]:
        raise DeliveryError("input-needs-target")
    lang = i18n.language(repo)
    branch = timeline.demand(repo)
    # the table comes from the timeline, never from the caller: a number nobody read from a clock is not a number
    timing = timeline.render(timeline.report(timeline.events(repo, branch)), lang)
    issue = str(data["issue"]) if data.get("issue") else _issue_of(repo, branch)
    default = _default_branch(repo, plat, project, base, transport)
    closes = closes_line(issue, data["target"], default) if issue else None
    cfg = config.load(repo)
    body = render(data.get("summary"), data.get("changes"), data.get("verification"), data.get("not_run"), timing,
                  closes, lang, template=cfg["mr_template"], repo=repo)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(body)
    # R81: the branch this description is about and its target -- `publish --source/--target` must repeat them
    result = {"outcome": "ok", "body_file": args.out, "length": len(body.strip()), "closes": closes,
              "issue": issue, "default_branch": default, "branch": branch, "target": data["target"], "warnings": []}
    if not closes:
        result["warnings"].append(i18n.t("delivery.no_closes", repo))
    if args.comment_out:
        comment = render_comment(timing, lang, request_url=data.get("request_url"))
        with open(args.comment_out, "w", encoding="utf-8") as f:
            f.write(comment)
        result.update(comment_file=args.comment_out, comment_length=len(comment.strip()))
    return result


def _summary(plat, result):
    """The dict of publish/comment without the full API body (it is long and the caller needs ref and url)."""
    out = {k: v for k, v in result.items() if k != "body"}
    body = result.get("body")
    if result["outcome"] == "ok" and isinstance(body, dict):
        ref_key, url_key = ("number", "html_url") if plat == "github" else ("iid", "web_url")
        out["ref"], out["url"] = body.get(ref_key), body.get(url_key)
    elif isinstance(body, dict) and ("message" in body or "errors" in body):
        out["api_message"] = _api_message(body)       # scrubbed with every free text in _emit
    return out


def _api_message(body):
    """B-M6: `message`, plus GitHub's `errors` array (`Validation Failed` alone hides why: an existing PR, a bad
    base...). Each error is its `message`, else its `field`/`code`. Without `errors`, `message` as the API sent it."""
    message, errors = body.get("message"), body.get("errors")
    if not isinstance(errors, list) or not errors:
        return message
    texts = []
    for e in errors:
        if isinstance(e, dict):
            texts.append(str(e.get("message") or " ".join(str(e[k]) for k in ("field", "code") if e.get(k))
                             or json.dumps(e, ensure_ascii=False)))
        else:
            texts.append(str(e))
    head = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False) if message else ""
    return f"{head}: {'; '.join(texts)}" if head else "; ".join(texts)


def _cmd_publish(args, repo, plat, project, base, transport):
    """R58: an ok publish stamps `delivered` right here, so the stamp cannot be forgotten. Any other outcome
    stamps nothing -- after an `unknown`, the stamp waits for `open-request` to find the request.
    R81: the stamp goes to the demand of THIS checkout, so `--source` must be this checkout's branch."""
    if args.source != timeline.demand(repo):
        raise DeliveryError("source-is-not-this-checkout")
    out = _summary(plat, publish(plat, project, args.source, args.target, args.title, _read_body(args.body_file),
                                 transport=transport, base_url=base))
    if out["outcome"] == "ok":
        try:
            timeline.append(repo, "delivered", "delivery", {"ref": out.get("ref")})
            out["delivered"] = True
        except Exception as e:             # the request exists: report it ok, and say the stamp is missing
            out["delivered"] = False
            out["warnings"] = [f"delivered was not stamped ({_describe(e)}): run phase.py delivered"]
    return out


def _cmd_update(args, repo, plat, project, base, transport):
    return _summary(plat, update(plat, project, args.ref, _read_body(args.body_file), transport=transport,
                                 base_url=base))


def _cmd_comment(args, repo, plat, project, base, transport):
    result = comment_issue(plat, project, args.issue, _read_body(args.body_file), transport=transport, base_url=base)
    out = _summary(plat, result)
    if result["outcome"] == "ok" and isinstance(result.get("body"), dict):
        out["ref"] = result["body"].get("id")
    return out


def _cmd_verify(args, repo, plat, project, base, transport):
    d = verify_detail(plat, project, args.ref, args.expected_len, transport=transport, base_url=base)
    return dict(d, outcome="ok" if d["ok"] else "error", expected_len=args.expected_len)


def _cmd_notes(args, repo, plat, project, base, transport):
    try:
        found = notes(plat, project, args.issue, transport=transport, base_url=base)
    except RequestFailed as e:
        return {"outcome": "error", "error": e.reason}
    return {"outcome": "ok", "issue": _number(args.issue), "notes": found}


def _cmd_open_request(args, repo, plat, project, base, transport):
    try:
        found = open_request_for(plat, project, args.branch, transport=transport, base_url=base)
    except RequestFailed as e:
        return {"outcome": "error", "error": e.reason}
    if found is None:
        return {"outcome": "ok", "request": None}
    ref, url = describe_request(plat, found)
    return {"outcome": "ok", "request": ref, "ref": found.get("number" if plat == "github" else "iid"), "url": url}


def _cmd_platform(args, repo, plat, project, base, transport):
    return {"outcome": "ok", "platform": plat, "project": project, "api_url": base,
            "token_bound": token_bound(repo, plat, base)[0]}


COMMANDS = {"platform": _cmd_platform, "render": _cmd_render, "publish": _cmd_publish, "update": _cmd_update,
            "verify": _cmd_verify, "comment": _cmd_comment, "notes": _cmd_notes, "open-request": _cmd_open_request}
# the commands that send the token; `platform` and `render` must still work against an unbound host
FREE_TEXT = ("error", "api_message", "warnings", "url", "body")     # never `request`, `ref`: ids are ours
SENDS_TOKEN = {"publish", "update", "verify", "comment", "notes", "open-request"}


def _parser():
    p = argparse.ArgumentParser(prog="delivery.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("platform")
    r = sub.add_parser("render")
    r.add_argument("--input", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--comment-out")
    pub = sub.add_parser("publish")
    for name in ("--title", "--source", "--target", "--body-file"):
        pub.add_argument(name, required=True)
    u = sub.add_parser("update")
    u.add_argument("--ref", required=True)
    u.add_argument("--body-file", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--ref", required=True)
    v.add_argument("--expected-len", required=True, type=int)
    c = sub.add_parser("comment")
    c.add_argument("--issue", required=True)
    c.add_argument("--body-file", required=True)
    n = sub.add_parser("notes")
    n.add_argument("--issue", required=True)
    o = sub.add_parser("open-request")
    o.add_argument("--branch", required=True)
    return p


def _message(outcome, repo):
    """The localized line for `outcome`; the raw value when there is none -- `_emit` never raises."""
    if outcome not in RC:
        return str(outcome)
    for where in (repo, None):
        try:
            return i18n.t(f"delivery.outcome.{outcome}", where)
        except Exception:
            continue
    return str(outcome)


def _emit(result, repo, extra_secrets):
    """R63: the fixed fields (`outcome`, `next`, `status`, ids, platform…) are printed as built; only the free-text
    fields -- error texts and what the server echoed -- are scrubbed."""
    outcome = result.get("outcome")
    result = dict(result, message=_message(outcome, repo))
    for key in FREE_TEXT:
        if key in result:
            result[key] = scrub(result[key], extra_secrets)
    for warning in result.get("warnings", []):
        print(warning, file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return RC.get(outcome, 1)


def main(argv, transport=None):
    args = _parser().parse_args(argv)
    repo, secrets = None, ()
    try:
        repo = timeline.repo_root(os.getcwd())
        if repo is None:
            raise DeliveryError("not-a-git-repository")
        secrets = _remote_secrets(timeline._git(repo, "remote", "get-url", "origin"))
        plat, project, base = checked_target(repo) if args.cmd in SENDS_TOKEN else target(repo)
        result = COMMANDS[args.cmd](args, repo, plat, project, base, transport)
    except DeliveryError as e:
        result = {"outcome": "error", "error": e.code}
    except Exception as e:
        result = {"outcome": "error", "error": _describe(e)}
    return _emit(result, repo, secrets)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
