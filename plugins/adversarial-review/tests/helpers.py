"""Test helpers: an environment isolated from the machine (no real HOME, state, tokens or backend choice)."""
import contextlib, importlib.util, os, subprocess, sys, tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(TESTS)
ROOT = os.path.dirname(os.path.dirname(PLUGIN))
BIN = os.path.join(PLUGIN, "bin")
GATE_BIN = os.path.join(ROOT, "plugins", "plan-gate", "bin")
FAKES = os.path.join(TESTS, "fakes")
SWITCHES = ("PLAN_GATE", "PORTAO_DE_PLANO", "PLAN_GATE_LANG", "PLAN_GATE_CONTENT_HASH", "PLAN_GATE_DIR", "GITLAB_TOKEN", "GITHUB_TOKEN",
            "GITLAB_HOST", "GH_HOST", "ADVERSARIAL_REVIEW_BACKEND", "ADVERSARIAL_REVIEW_MODEL",
            "ADVERSARIAL_REVIEW_ENDPOINT_URL", "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN",
            "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE", "ADVERSARIAL_REVIEW_THINKING", "ADVERSARIAL_REVIEW_DIR",
            "ZAI_API_KEY", "ZAI_UPSTREAM", "OPENCODE_CONFIG", "CLAUDE_PLUGIN_ROOT")
REAL_HOME = os.path.realpath(os.path.expanduser("~"))


def isolated_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in SWITCHES}
    env.update(HOME=tempfile.mkdtemp(prefix="ar-home-"), PLAN_GATE_DIR=tempfile.mkdtemp(prefix="ar-gate-"),
               ADVERSARIAL_REVIEW_DIR=tempfile.mkdtemp(prefix="ar-state-"))
    env.update(extra)
    assert os.path.realpath(env["HOME"]) != REAL_HOME
    return env


def import_bin(name):
    if BIN not in sys.path:
        sys.path.insert(0, BIN)
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(script, *args, env=None, cwd=None, stdin=None):
    path = script if os.path.isabs(script) else os.path.join(BIN, script)
    p = subprocess.run([sys.executable, path, *args], input=stdin, capture_output=True, text=True,
                       env=env if env is not None else isolated_env(), cwd=cwd)
    return p.returncode, p.stdout, p.stderr


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


@contextlib.contextmanager
def fake_repo():
    with tempfile.TemporaryDirectory(prefix="ar-repo-") as d:
        repo = os.path.realpath(d)
        def git(*a):
            subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        for sub in ("plans", "specs"):
            os.makedirs(os.path.join(repo, "docs", "superpowers", sub))
        write(os.path.join(repo, "README.md"), "# Repo\n")
        git("add", "README.md")
        git("commit", "-q", "-m", "init")
        yield repo



def contract_of(backend):
    """The backend contract an object exposes: the flags' values and which callables exist."""
    return {"CAN_REPAIR": getattr(backend, "CAN_REPAIR", None), "CAN_MODEL": getattr(backend, "CAN_MODEL", None),
            "callables": sorted(n for n in ("review", "repair", "route") if callable(getattr(backend, n, None)))}


_SHAPES = []


def real_backend_shapes():
    """{(CAN_REPAIR, CAN_MODEL)} of the backends that ship -- read from the modules, never written by hand."""
    if not _SHAPES:
        _SHAPES.extend({(m.CAN_REPAIR, m.CAN_MODEL) for m in (import_bin("backend_codex"),
                                                             import_bin("backend_opencode"))})
    return set(_SHAPES)


class FakeBackend:
    """A test backend shaped LIKE a real one: same flags, and `repair` only when the real one repairs.

    ⚠️ Rule 14: a hand-written fake declares the interface you IMAGINE. Built with `FakeBackend.like(module)`, it
    cannot expose more or less than the real backend -- and Tasks 11/13 assert contract_of(fake) == contract_of(real).

    Each reply is a `loop.Result`, or a function `kw -> Result` (which may write kw["output_json"], edit the plan...).
    """
    def __init__(self, can_repair, can_model, replies, route="fake"):
        real = real_backend_shapes()
        assert (can_repair, can_model) in real, f"no real backend has {(can_repair, can_model)}; real: {real}"
        self.CAN_REPAIR, self.CAN_MODEL, self._replies, self._route = can_repair, can_model, list(replies), route
        self.calls = []
        if not can_repair:
            self.repair = None          # absent, as in a backend that cannot repair

    @classmethod
    def like(cls, module, replies, route="fake"):
        return cls(module.CAN_REPAIR, module.CAN_MODEL, replies, route)

    def _answer(self, kind, kw):
        self.calls.append((kind, kw))
        action = self._replies.pop(0)
        return action(kw) if callable(action) else action

    def review(self, **kw):
        return self._answer("review", kw)

    def repair(self, **kw):  # noqa: F811 -- replaced by None in __init__ when the real backend cannot repair
        return self._answer("repair", kw)

    def route(self, settings):
        return self._route
