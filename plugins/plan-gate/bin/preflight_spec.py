#!/usr/bin/env python3
"""Mechanical SPEC preflight: checks what the spec CLAIMS about the repo.

⚠️ Why it exists, with a number. An adversarial review of one spec found 10 defects in two rounds.
Three of them were claims about code the author had read in the same session and described wrongly
afterwards -- "the service already rejects values over the cap" (it does not) and "the state stays
untouched" (only the `status` does). The reviewer is a safety net, not a quality mechanism. If the
spec is only right because someone reviews it afterwards, the work was outsourced.

The question "is this claim true?" is undecidable. The question "is this claim CITED, and does the
citation resolve?" is mechanical -- and it is the act of opening the file to cite that catches the
mistake. This module does the second one.

⚠️ THE CUT CAME FROM MEASUREMENT, not taste. Run against a corpus of 73 real specs:

  - claim about the repo, any             : 1342 in total, ~18 per spec, 4.8% cited
  - GUARANTEE claim ("ja impede",
    "garante", "sempre", "nunca", "intocado"):   83 in total, ~2.0 per spec, 4% cited
  - quantitative concept without a number : 204 of 302 (68%), ~4.5 per spec

That is why only the middle group BLOCKS. Demanding a citation on all 18 would be ~18 escapes per
spec, and a gate that is escaped every time is dead -- if it is escaped half the time, the problem
is the calibration of the check, not who uses it. The other two groups WARN.

Use as module:  result = evaluate(spec, repo)
Use as script:  preflight_spec.py [--json] <spec.md>
"""
# Name table, pt (INTERNAL preflight_spec.py) -> en. Literal one-to-one translation: behavior,
# regexes and constants are identical; only the names and the user-facing text changed (text goes
# through i18n.t("spec.*"); pt-BR reproduces the original text).
#
#   constants / patterns
#     PARECE_CODIGO -> LOOKS_LIKE_CODE      IDENT -> IDENT       CITACAO -> CITATION
#     GARANTIA -> GUARANTEE                 INTENCAO -> INTENT   QUANTITATIVO -> QUANTITATIVE
#     TEM_VALOR -> HAS_VALUE                PONTEIRO -> POINTER  GRUPOS -> GROUPS
#     (the Portuguese words INSIDE the regexes are kept: they match pt-BR prose)
#   functions
#     frases -> sentences                   simbolos_da_frase -> symbols_of_sentence
#     tem_citacao -> has_citation           _partes -> _parts
#     checar_citacoes -> check_citations    checar_garantias -> check_guarantees
#     checar_afirmacoes -> check_claims     checar_quantitativos -> check_quantities
#     _achar -> _find                       checar_sustentacao -> check_support
#     _curta -> _short                      avaliar -> evaluate
#     relatorio -> report                   main -> main
#   NEW (not in the INTERNAL): json_report (the `--json` check mode), _json_exit_code,
#     _evaluate_counted / the `examined` counters of check_* (vacuity), _inside_git (fallback guard),
#     _not_evaluated_report, _t / _use_repo (i18n helpers).
#   group keys (evaluate): garantias citacoes sustentacao fora_do_repo quantitativos afirmacoes
#     -> guarantees citations support outside-repo quantities claims   (stable ids, same as --json)
#   states: OK NAO_APLICAVEL ACHADO -> ok not_applicable findings
#   CLI: <spec.md> + NEW --json
import json, os, re, shutil, subprocess, sys

# ⚠️ Bootstrap of the sibling modules: the test helper `import_bin` does not touch sys.path.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import i18n
import preflight_plan as pp
from search import CouldNotCheck

_LANG_REPO = None


def _use_repo(repo):
    """Language of the user-facing text follows the repo's `.claude/plan-gate.json`."""
    global _LANG_REPO
    _LANG_REPO = repo


def _t(key, **kwargs):
    return i18n.t(key, repo=_LANG_REPO, **kwargs)


# ---------------------------------------------------------------- sentence recognition

# Identifier that looks like code: has parentheses, underscore, extension, slash or arrow.
# ⚠️ Without this filter, `status` and `plano` (prose in backticks) would become "nonexistent symbol".
LOOKS_LIKE_CODE = re.compile(r"[A-Za-z0-9_]+\(\)|_|\.(?:py|php|ts|tsx|js|json|ya?ml|sh|md)\b|/|::|->")
IDENT = re.compile(r"`([^`\n]{2,60})`")

# Accepted citation: `file.ext:123` or markdown link `(path#L123)`.
CITATION = re.compile(r"(?P<arq>[\w./-]+\.(?:py|php|ts|tsx|js|json|ya?ml|sh|md)):(?P<lin>\d+)"
                      r"|\((?P<arq2>[^)\s#]+)#L(?P<lin2>\d+)(?:-L\d+)?\)")

# ⚠️ GUARANTEE verb -- the cut that blocks. These are sentences that claim CAPABILITY of what
# already exists, not the intention of what will be done. These are the ones that burned me.
# ⚠️ Bare `sempre`/`nunca` were taken out after measuring: on the author's own spec they caught
# "`docs/superpowers/` nunca e commitado" (working policy) and "nunca digitada" (human process) --
# 2 of the 3 false positives. An adverb does not tell a claim about the CODE from a rule about
# PEOPLE. A capability verb does.
GUARANTEE = re.compile(
    r"\b(j[aá] (?:faz|existe|impede|garante|cobre|trata|valida|resolve)"
    r"|impede|garante|assegura"
    r"|n[aã]o (?:faz|existe|cobre|toca|altera|muda)"
    r"|s[oó] (?:aceita|permite|roda|existe)"
    r"|(?:sempre|nunca|jamais) (?:retorna|devolve|grava|le|lê|chama|roda|falha|aprova)"
    r"|intocad|inalterad)\b", re.I)

# An INTENT sentence is not a claim about the repo: it talks about the future, and the future cannot be cited.
INTENT = re.compile(
    r"\b(vai|vamos|passa a|passar[aá]|deve|dever[aá]|precisa[rm]?[aá]?|requisito|proposta"
    r"|ser[aá]|entra|fica para|queremos|proponho|sugiro)\b", re.I)

QUANTITATIVE = re.compile(
    r"\b(teto|limite|m[aá]ximo|m[ií]nimo|timeout|prazo|quota|or[cç]amento|deadline|retentativa)\b",
    re.I)
HAS_VALUE = re.compile(
    r"\b\d+\s*(s|seg|segundos?|min|minutos?|h|horas?|ms|[kmg]i?b|bytes?|tokens?|req|%|x|vezes|dias?)\b"
    r"|\b\d{2,}\b", re.I)
# "see the table below" is a legitimate pointer to where the number lives.
POINTER = re.compile(r"\b(ver|conforme|abaixo|acima|tabela|configur[aá]vel)\b", re.I)


def sentences(text):
    """Prose in sentences, WITHOUT code blocks.

    ⚠️ A code block holds no claim in prose -- letting it in made every example line become
    "claim without citation".
    """
    clean = re.sub(r"```.*?```", "", text, flags=re.S)
    clean = re.sub(r"(?m)^\s{4,}\S.*$", "", clean)          # indented block
    for piece in re.split(r"(?<=[.;:!?])\s+|\n\n|\n\|", clean):
        s = piece.strip().strip("|").strip()
        if s:
            yield s


def symbols_of_sentence(s):
    return [i for i in IDENT.findall(s) if LOOKS_LIKE_CODE.search(i)]


def has_citation(s):
    return bool(CITATION.search(s))


def _parts(m):
    return (m.group("arq") or m.group("arq2"), int(m.group("lin") or m.group("lin2")))


# ---------------------------------------------------------------- the checks

def check_citations(text, repo, spec_dir=(".",)):
    """Every `file:line` citation has to exist, and the line has to exist in the file.

    ⚠️ Zero false positives by construction: either the file is there at the stated line, or it is not.
    """
    findings, unverifiable = [], []
    examined = 0                # citations that resolved to a file in this repo and were read
    seen = set()
    for m in CITATION.finditer(text):
        arq, lin = _parts(m)
        if (arq, lin) in seen:
            continue
        seen.add((arq, lin))
        # ⚠️ `pp._resolve` returns (path RELATIVE to the repo, exact), not a string.
        # Treating the return as a path made `os.path.isfile` receive a tuple.
        path = _find(repo, arq, spec_dir[0])
        if not path:
            # the spec may cite with a path relative to itself (`../../../shared/x.py`)
            for base in (repo, os.path.dirname(os.path.abspath(spec_dir[0]))):
                attempt = os.path.normpath(os.path.join(base, arq))
                if os.path.isfile(attempt):
                    path = attempt
                    break
        if not path or not os.path.isfile(path):
            # ⚠️ A file that is not in this repo is NOT a wrong citation: adjudicated in 4 of 6
            # samples, all legitimate references to ANOTHER system. It cannot
            # be checked from here, so it goes to `unverifiable` -- warns, does not block. Accusing
            # as wrong what one cannot check is the vice this module exists to fight.
            unverifiable.append(_t("spec.citation_outside", arq=arq, lin=lin))
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                total = sum(1 for _ in f)
        except OSError as e:
            findings.append(_t("spec.citation_unreadable", arq=arq, lin=lin, error=e))
            continue
        examined += 1
        if lin > total:
            # this is the 100%-precision case: the file is here and the line does not exist in it
            findings.append(_t("spec.citation_past_end", arq=arq, lin=lin, total=total))
    return findings, unverifiable, examined


def check_guarantees(text):
    """A GUARANTEE claim about the repo needs a citation. This is the group that blocks.

    ⚠️ Measured: ~2.0 per spec in the 73-spec corpus. It is the right size for a gate --
    minutes of verification, and it covers exactly the class of error the adversarial review found
    in me.
    """
    findings = []
    examined = 0                # sentences that passed every filter and were tested for a citation
    for s in sentences(text):
        # ⚠️ A sentence in quotes or in a block quote is REPRODUCING text (including the correction
        # of an old mistake), not claiming it again. Without this, the section "I wrote wrongly
        # that X" became a finding -- punishing whoever documents their own mistake is perverse.
        if s.lstrip().startswith(">") or re.search(r'["“”].*["“”]', s):
            continue
        if INTENT.search(s) or not GUARANTEE.search(s):
            continue
        if not symbols_of_sentence(s):
            continue
        examined += 1
        if has_citation(s):
            continue
        findings.append(_short(s))
    return findings, examined


def check_claims(text):
    """Ordinary claim about the repo without a citation. WARNS, does not block (~18 per spec)."""
    findings = []
    examined = 0                # sentences that passed every filter (each one becomes a finding)
    for s in sentences(text):
        if INTENT.search(s) or GUARANTEE.search(s) or has_citation(s):
            continue
        if not symbols_of_sentence(s):
            continue
        examined += 1
        findings.append(_short(s))
    return findings, examined


def check_quantities(text):
    """Quantitative concept without a number or a pointer. WARNS (68% of the occurrences)."""
    findings = []
    examined = 0                # sentences that carry a quantitative term
    for s in sentences(text):
        if not QUANTITATIVE.search(s):
            continue
        examined += 1
        if HAS_VALUE.search(s) or POINTER.search(s):
            continue
        findings.append(_short(s))
    return findings, examined


def _find(repo, arq, spec):
    """Absolute path of the cited file, or None. Ambiguity does NOT become 'does not exist'.

    ⚠️ `pp._resolve` returns (relative, exact) and gives None when the basename matches in more
    than one place. Measured: one file name existed in two folders, and the
    three right citations in the author's spec were rejected for it. Rejecting for ambiguity is accusing
    as wrong whoever wrote it right.
    """
    direct = os.path.normpath(os.path.join(repo, arq))
    if os.path.isfile(direct):
        return direct
    relative = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(spec)), arq))
    if os.path.isfile(relative):
        return relative
    base = os.path.basename(arq)
    candidates = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in pp.EXCLUDE and not d.startswith(".")]
        if base in files:
            candidates.append(os.path.join(root, base))
        if len(candidates) > 8:
            break
    return candidates[0] if candidates else None


def check_support(text, repo, spec):
    """Does the citation support the sentence? Does the cited symbol appear IN THAT region of the file?

    ⚠️ Replaced the "symbol exists in the repo" check, which was conceptually wrong for a spec. A
    plan CREATES things; a spec PROPOSES things. Measured on my spec: of the 7 accused symbols, 2
    existed (a dict key and an environment variable name, which are not `def`), 2 were external
    vocabulary (a container tool's option name, a vendor API's field name) and 3 were names the spec itself
    proposed. Seven out of seven false.

    This one is truly mechanical: if the sentence says `foo` and cites `a.py:10`, then `foo` has to
    appear near line 10 of `a.py`. It is exactly the "I cited the wrong line" mistake.
    """
    findings = []
    examined = 0                # citations whose cited region was read and compared with the symbols
    WINDOW = 12
    for s in sentences(text):
        m = CITATION.search(s)
        if not m:
            continue
        arq, lin = _parts(m)
        path = _find(repo, arq, spec)
        if not path:
            continue                      # the other group complains about the file
        names = [re.sub(r"\(\)$", "", i).strip() for i in symbols_of_sentence(s)]
        names = [n for n in names if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{2,}", n)
                 and n not in pp.NOISE and not n.endswith((".py", ".json", ".md"))]
        if not names:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        examined += 1
        excerpt = "\n".join(lines[max(0, lin - 1 - WINDOW):lin + WINDOW])
        absent = [n for n in names if n not in excerpt]
        if absent and len(absent) == len(names):
            findings.append(_t("spec.citation_unsupported", arq=arq, lin=lin,
                               absent=", ".join('`' + n + '`' for n in absent), window=WINDOW))
    return findings, None, examined


def _short(s, n=110):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


# ---------------------------------------------------------------- report

# (id, English title for reference only, blocks). The shown title is localized: `_t("spec.group." + id)`.
GROUPS = (
    ("guarantees", "Guarantee claim without proof", True),
    ("citations", "Citation that does not resolve", True),
    ("support", "Citation that does not support the sentence", True),
    ("outside-repo", "Citation to outside this repo", False),
    ("quantities", "Quantitative requirement without a value", False),
    ("claims", "Claim about the repo without a citation", False),
)


def _group_title(group_id):
    return _t("spec.group." + group_id)


def evaluate(spec, repo):
    """Returns {group: (state, [findings], blocks)}. States are the same as the plan preflight's."""
    return _evaluate_counted(spec, repo)[0]


def _evaluate_counted(spec, repo):
    """(evaluate's result, how many items the checks EXAMINED in total).

    ⚠️ The count comes from the checks themselves, so it cannot drift from them: a sentence the
    checks skip (intent, quoted, block quote, no code symbol) and a citation to another repo are
    NOT examined, however many symbols they carry.
    """
    _use_repo(repo)
    with open(spec, encoding="utf-8", errors="replace") as f:
        text = f.read()
    out = {}
    guarantees, n_guarantees = check_guarantees(text)
    out["guarantees"] = (guarantees, None)
    cits, unverifiable, n_cits = check_citations(text, repo, (spec,))
    out["citations"] = (cits, None)
    out["outside-repo"] = (unverifiable, None)
    supp, reason, n_support = check_support(text, repo, spec)
    out["support"] = (supp, reason)
    quantities, n_quantities = check_quantities(text)
    out["quantities"] = (quantities, None)
    claims, n_claims = check_claims(text)
    out["claims"] = (claims, None)
    examined = n_guarantees + n_cits + n_support + n_quantities + n_claims

    result = {}
    for key, _title, blocks in GROUPS:
        findings, not_applicable = out[key]
        if not_applicable:
            result[key] = ("not_applicable", [not_applicable], blocks)
        elif findings:
            result[key] = ("findings", findings, blocks)
        else:
            result[key] = ("ok", [], blocks)
    return result, examined


def report(spec, repo):
    """Text for people to read + whether there is a BLOCKING finding."""
    res = evaluate(spec, repo)
    lines = [_t("spec.header", name=os.path.basename(spec)), ""]
    block = False
    for key, _title, blocks in GROUPS:
        state, findings, _ = res[key]
        mark = {"ok": "OK  ", "findings": "  ! ", "not_applicable": "N/A "}[state]
        suffix = "" if blocks else _t("spec.warning_suffix")
        n = len(findings) if state == "findings" else ""
        lines.append(f"[{mark}] {_group_title(key)}{suffix}" + (f" — {n}" if n else ""))
        for a in findings[:8]:
            lines.append(f"        - {a}")
        if len(findings) > 8:
            lines.append(_t("spec.and_more", n=len(findings) - 8))
        if state == "findings" and blocks:
            block = True
    lines.append("")
    if block:
        lines.append(_t("spec.blocked_1"))
        lines.append(_t("spec.blocked_2"))
        lines.append(_t("spec.blocked_3"))
    else:
        lines.append(_t("spec.clean"))
    return "\n".join(lines), block


def json_report(spec, repo):
    """Check mode: {"status": "pass"|"fail", "findings": [{"group", "state", "count", "items"}]}.

    Lists EVERY group (stable English ids). Two rules of the spec:
      - `status` only looks at the BLOCKING groups: a non-blocking warning never fails it.
      - VACUITY: every group without a finding is `ok`, so "at least one ok" proves nothing here.
        `pass` needs at least 1 item the checks EXAMINED (a citation resolved in this repo, or a
        sentence that passed the checks' filters); otherwise `fail` with a `nothing-to-check` entry.
    """
    res, examined = _evaluate_counted(spec, repo)
    entries = []
    for key, _title, _blocks in GROUPS:
        state, findings, _ = res[key]
        entries.append({"group": key, "state": state, "count": len(findings), "items": list(findings)})
    blocked = any(res[key][0] == "findings" for key, _t_, blocks in GROUPS if blocks)
    vacuous = not blocked and examined == 0
    if vacuous:
        entries.append({"group": "nothing-to-check", "state": "not_evaluated", "count": 0, "items": [],
                        "reason": _t("spec.nothing_to_check")})
    return {"status": "fail" if blocked or vacuous else "pass", "findings": entries}


def _json_exit_code(result):
    """rc 0 if and only if status == "pass" (1 otherwise, like the human CLI on a block)."""
    return 0 if result["status"] == "pass" else 1


def _inside_git(folder):
    """True inside a git work tree, False ONLY when git says it is not a git repository.

    Anything else (git not installed, timeout, exit other than 0/128-not-a-repository, such as
    "dubious ownership") raises CouldNotCheck. LC_ALL=C pins the message git is matched on.
    """
    if not shutil.which("git"):
        raise CouldNotCheck(_t("preflight.not_installed", cmd="git"))
    try:
        r = subprocess.run(["git", "-C", folder, "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=25,
                           env=dict(os.environ, LC_ALL="C"))
    except subprocess.TimeoutExpired:
        raise CouldNotCheck(_t("search.timed_out", cmd="git", timeout=25))
    except OSError as e:
        raise CouldNotCheck(_t("search.failed", cmd="git", error=e))
    if r.returncode == 0:
        return r.stdout.strip() == "true"
    if r.returncode == 128 and "not a git repository" in r.stderr:
        return False
    raise CouldNotCheck(_t("search.exited", cmd="git", code=r.returncode, stderr=r.stderr.strip()[:120]))


def _not_evaluated_report(error):
    """--json when the checks could not even start: every group not_evaluated, status fail."""
    entries = [{"group": key, "state": "not_evaluated", "count": 0, "items": [], "reason": str(error)}
               for key, _title, _blocks in GROUPS]
    return {"status": "fail", "findings": entries}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if not argv:
        print(_t("spec.usage"), file=sys.stderr)
        return 2
    spec = argv[0]
    if not os.path.isfile(spec):
        print(_t("spec.not_found", spec=spec), file=sys.stderr)
        return 2
    folder = os.path.dirname(os.path.abspath(spec))
    _use_repo(folder)
    try:
        # ⚠️ DEVIATION from the INTERNAL, which crashed with a traceback when the spec is not inside
        # a git work tree. Fall back to the spec's folder ONLY in that case (decided up front, by git
        # itself). Any other failure (git missing, timeout, dubious ownership...) is "could not
        # check": it must not turn into a pass, so it takes the not-evaluated path (rc 2).
        repo = (pp.repo_of(os.path.abspath(spec)) or folder) if _inside_git(folder) else folder
    except CouldNotCheck as e:
        if as_json:
            result = _not_evaluated_report(e)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(e, file=sys.stderr)
        return 2
    _use_repo(repo)
    if as_json:
        result = json_report(spec, repo)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return _json_exit_code(result)
    text, block = report(spec, repo)
    print(text)
    return 1 if block else 0


if __name__ == "__main__":
    sys.exit(main())
