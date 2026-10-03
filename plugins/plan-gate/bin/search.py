#!/usr/bin/env python3
"""Repo search, with two engines behind the same interface.

  - `rg`  : fast, and what the owner's machine has;
  - `git` : `git ls-files --cached --others --exclude-standard` + `re`, for where there is no
            ripgrep -- the Phase 3 container, for example.

⚠️ Why `git ls-files` and not an `os.walk` with its own ignore rules: reimplementing
`.gitignore` is a source of SILENT divergence between the engines, and silent divergence here
means "the preflight did not find the symbol" -- the gate OPENS, which is the forbidden direction.
git is already a hard dependency (`preflight_plan.repo_of()` invokes it) and gives the exact
semantics for free.

⚠️ KNOWN and declared difference: a file ignored by `.gitignore` but force-added
(`git add -f`) shows up in the `git` engine and not in `rg`.
"""
# Name table, pt (INTERNO busca.py) -> en. Behavior, constants and regexes are identical.
#
#   module-level
#     Achado                 -> Hit            (fields arquivo, linha, texto -> file, line, text)
#     TEM_RG                 -> HAS_RG         (read ONCE at import: shutil.which("rg"))
#     NaoDeuParaChecar       -> CouldNotCheck
#
#   ^def
#     motor                  -> engine
#     _glob_para_regex       -> _glob_to_regex
#     _excluida              -> _excluded
#     _rodar                 -> _run
#     _e_repo_git            -> _is_git_repo
#     _arquivos_do_git       -> _git_files
#     procurar               -> find           (kwargs fixo, excluir, globs, max_por_arquivo,
#                                               apenas_casado, timeout -> fixed, exclude, globs,
#                                               max_per_file, only_matched, timeout)
#     listar_arquivos        -> list_files     (kwargs glob, excluir, timeout -> glob, exclude, timeout)
#
# Error messages (CouldNotCheck) go through i18n.t(); pt-BR reproduces the original text.
import os, re, shutil, subprocess
from collections import namedtuple

import i18n

Hit = namedtuple("Hit", "file line text")
HAS_RG = bool(shutil.which("rg"))


class CouldNotCheck(Exception):
    """Search missing, timeout, unreadable output.

    ⚠️ NEVER confuse with "searched and found nothing": returning "" on failure makes the result
    reach the groups indistinguishable from absence, and with the gate reading that, a search
    with a timeout would OPEN the gate.
    """


def engine():
    return "rg" if HAS_RG else "git"


def _glob_to_regex(pattern):
    r"""`!**/[Tt]est*/**` -> regex that matches the RELATIVE path, with rg's semantics.

    ⚠️ `fnmatch.translate` does NOT work, and the divergence is silent in BOTH directions --
    measured on 2026-09-21 with the real globs of `preflight_plan.TEST_GLOBS`:

      - `fnmatch('**/[Tt]est*/**')` does NOT match `tests/a.py` at the root (it requires a slash
        before) and `rg` does. The python engine would find a symbol in a TEST file that `rg`
        skips, and `_definition` would report a test definition as if it were production;
      - fnmatch's `*` CROSSES the slash (`*.py` matches `sub/dir/a.py`); rg's does not.
    """
    p = pattern.lstrip("!")
    parts, i = [], 0
    while i < len(p):
        if p.startswith("**/", i):
            parts.append(r"(?:[^/]+/)*")          # any depth, ZERO included
            i += 3
        elif p.startswith("/**", i):
            parts.append(r"(?:/.*)?")
            i += 3
        elif p[i] == "*":
            parts.append(r"[^/]*")                 # does NOT cross the slash
            i += 1
        elif p[i] == "?":
            parts.append(r"[^/]")
            i += 1
        elif p[i] == "[":
            j = p.index("]", i)
            parts.append(p[i:j + 1])
            i = j + 1
        else:
            parts.append(re.escape(p[i]))
            i += 1
    return re.compile("".join(parts) + r"\Z")


def _excluded(rel, exclude, globs):
    """⚠️ A POSITIVE glob also filters, and in the opposite direction: `rg` only returns what
    matches it. Ignoring it here left the `git` engine returning MORE than `rg`, silently --
    asymmetry between the two sibling functions (`list_files` already honored the positive one).
    """
    parts = rel.split(os.sep)
    if any(d in parts for d in exclude):
        return True
    if any(_glob_to_regex(g).match(rel) for g in globs if g.startswith("!")):
        return True
    if any(not g.startswith("!") for g in globs):
        # ⚠️ REFUSE instead of silently diverging. `rg` matches a glob without `/` against the
        # basename at ANY depth; the translator here does not cross the slash, so the `git` engine
        # would return less. No caller passes a positive glob today (the three pass only negative
        # ones), and half support is worse than none: silent divergence between the engines is
        # exactly what this module exists to prevent.
        raise CouldNotCheck(i18n.t("search.positive_glob"))
    return False


def _run(cmd, timeout, cwd):
    if not shutil.which(cmd[0]):
        raise CouldNotCheck(i18n.t("search.not_installed", cmd=cmd[0]))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise CouldNotCheck(i18n.t("search.timed_out", cmd=cmd[0], timeout=timeout)) from None
    except OSError as e:
        raise CouldNotCheck(i18n.t("search.failed", cmd=cmd[0], error=e)) from None
    if r.returncode not in (0, 1):      # rg: 0 found, 1 not found, 2+ a real error
        raise CouldNotCheck(i18n.t("search.exited", cmd=cmd[0], code=r.returncode,
                                   stderr=r.stderr.strip()[:120]))
    return r.stdout


def _is_git_repo(repo, timeout):
    """⚠️ `os.path.isdir(.git)` is FALSE in a worktree: there `.git` is a FILE pointing to the real
    directory. The engine fell into os.walk, which reads no `.gitignore` -- and this module's
    docstring promised exactly the opposite. Direction of the error: finding a symbol in an
    ignored/generated file and reporting it as existing in the repo, which OPENS the gate.

    Measured on 2026-09-21, and on the NORMAL path: the workspace CLAUDE.md says to use a worktree
    for every task. Asking git is the only answer that holds in both cases.
    """
    try:
        return bool(_run(["git", "rev-parse", "--git-dir"], timeout, repo).strip())
    except CouldNotCheck as e:
        # ⚠️ Only "not a git repo" falls into os.walk. ANY other git failure propagates.
        # The broad `except` traded a common fail-open for a RARE AND SILENT one: git with a
        # timeout, missing, or exiting 128 over `dubious ownership` (repo bind-mounted in a
        # container with a different uid -- exactly the python:slim scenario of Phase 3) became
        # os.walk without .gitignore, and a symbol in a generated file passed as existing. The
        # gate OPENS, which is the forbidden direction.
        if "not a git repository" in str(e).lower():
            return False
        raise


def _git_files(repo, timeout):
    """Tracked + untracked-not-ignored, HIDDEN included -- the set of `rg --hidden`."""
    if not _is_git_repo(repo, timeout):
        out = []
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d != ".git"]
            out += [os.path.relpath(os.path.join(root, f), repo) for f in files]
        return out
    raw = _run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
               timeout, repo)
    return [l for l in raw.splitlines() if l.strip()]


def find(repo, pattern, *, fixed=False, exclude=(), globs=(), max_per_file=None,
         only_matched=False, timeout=25):
    """Matching lines. `file` is always RELATIVE to the repo.

    `only_matched` is rg's `-o`: one entry per OCCURRENCE, with `text` being only the matched
    snippet. Whoever counts uses depends on it -- two occurrences on the same line are two uses,
    and counting lines would return one.
    """
    if HAS_RG:
        # ⚠️ `--hidden` makes rg DESCEND into `.git/` and find the hooks/*.sample -- `git ls-files`
        # never lists them, and the two engines diverged there (measured on 2026-09-21).
        # `.git/` is not code: the right engine is git's, and rg aligns to it.
        cmd = ["rg", "--hidden", "--glob", "!**/.git/**",
               "--no-heading", "--line-number", "--color", "never"]
        if fixed:
            cmd.append("--fixed-strings")
        if only_matched:
            cmd.append("-o")
        if max_per_file:
            cmd += ["-m", str(max_per_file)]
        for d in exclude:
            cmd += ["--glob", f"!**/{d}/**"]
        for g in globs:
            cmd += ["--glob", g]
        # ⚠️ Search "." with cwd in the repo, NEVER the absolute path: rg's globs are matched
        # against the WHOLE path, so `!**/tmp/**` would exclude a repo living in /tmp/xxx -- and
        # the search would return "does not exist" for everything, silently.
        cmd += ["--", pattern, "."]
        hits = []
        for line in _run(cmd, timeout, repo).splitlines():
            if not line.strip():
                continue
            path, _, rest = line.partition(":")
            num, _, text = rest.partition(":")
            if num.isdigit():
                hits.append(Hit(os.path.normpath(path), int(num), text))
        return hits

    target = re.compile(re.escape(pattern) if fixed else pattern)
    hits, per_file = [], {}
    for rel in _git_files(repo, timeout):
        if _excluded(rel, exclude, globs):
            continue
        try:
            with open(os.path.join(repo, rel), encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    matches = list(target.finditer(line))
                    if not matches:
                        continue
                    if max_per_file and per_file.get(rel, 0) >= max_per_file:
                        break
                    per_file[rel] = per_file.get(rel, 0) + 1
                    if only_matched:
                        hits += [Hit(rel, i, m.group(0)) for m in matches]
                    else:
                        hits.append(Hit(rel, i, line.rstrip("\n")))
        except (UnicodeDecodeError, OSError):
            continue    # binary or unreadable: `rg` skips them too
    return hits


def list_files(repo, *, glob=None, exclude=(), timeout=20):
    if HAS_RG:
        cmd = ["rg", "--hidden", "--glob", "!**/.git/**", "--files"]
        for d in exclude:
            cmd += ["--glob", f"!**/{d}/**"]
        if glob:
            cmd += ["--glob", glob]
        cmd.append(".")
        return [os.path.normpath(l) for l in _run(cmd, timeout, repo).splitlines()
                if l.strip()]
    out = []
    for rel in _git_files(repo, timeout):
        if _excluded(rel, exclude, ()):
            continue
        if glob and not _glob_to_regex(glob).match(rel):
            continue
        out.append(rel)
    return out
