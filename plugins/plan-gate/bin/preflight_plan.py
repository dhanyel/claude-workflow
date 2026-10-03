#!/usr/bin/env python3
"""Mechanical plan preflight: runs LOCALLY the checks that are pure grep.

Why it exists: checks 1, 5 and 6 of the contract ("does the method exist?", "is the constant
used?", "does X exist in the repo?") are binary-answer greps. Letting the reviewer discover them
alone costs one tool call per symbol -- and EACH call resends the whole context, which on round 3
was already over 131k tokens on the last turn. Run here, it costs zero tokens and the reviewer
receives the table ready.

Use as module:  table = preflight(plan, repo)
Use as script:  preflight_plan.py [--human|--prompt|--gate|--json] <plan.md>
"""
# Name table, pt (INTERNAL preflight_plano.py) -> en. Literal one-to-one translation: behavior,
# regexes and constants are identical; only the names and the user-facing text changed (text goes
# through i18n.t("preflight.*"); pt-BR reproduces the original text).
#
#   constants / patterns
#     TETO_DE_SIMBOLOS -> SYMBOL_CAP        EXCLUIR -> EXCLUDE            RUIDO -> NOISE
#     BUILTINS -> BUILTINS                  PALAVRAS_DE_CONTROLE -> CONTROL_WORDS
#     GLOBS_DE_TESTE -> TEST_GLOBS          RE_CHAMADA -> RE_CALL         RE_CAMPO -> RE_FIELD
#     MINIMO_DE_CITACOES -> MIN_CITATIONS   TETO_DE_CHARS -> CHAR_CAP
#     TETO_DE_LINHAS_POR_ARQ -> LINE_CAP_PER_FILE      NUNCA_INJETAR -> NEVER_INJECT
#     EXTENSOES_DE_ARQUIVO -> FILE_EXTENSIONS          RE_CAMINHO -> RE_PATH
#     GRUPOS -> GROUPS                      RE_CERCA -> RE_FENCE          RE_SECAO_FILES -> RE_FILES_SECTION
#     RE_BLOCO_DE_COMMIT -> RE_COMMIT_BLOCK RE_ITEM_DE_ARQUIVO -> RE_FILE_ITEM
#     DEFINICAO_POR_LINGUAGEM -> DEFINITION_BY_LANGUAGE   INCLUI_VENDOR -> INCLUDES_VENDOR
#     NORMALIZA_LINGUAGEM -> NORMALIZE_LANGUAGE           RE_BLOCO -> RE_BLOCK
#     RE_CHAMADA_CODIGO -> RE_CODE_CALL     RE_COMENTARIO -> RE_COMMENT   RE_TEXTO_JSX -> RE_JSX_TEXT
#     RUIDO_DE_PYTHON -> PYTHON_NOISE       BUILTINS_DO_RELATORIO -> REPORT_BUILTINS
#     RE_COMENTARIO_NO_FIM -> RE_TRAILING_COMMENT         PADROES_FAIL_OPEN -> FAIL_OPEN_PATTERNS
#     RE_HEADING_DE_TASK -> RE_TASK_HEADING RE_LINGUAGEM -> RE_LANGUAGE
#     LINGUAGENS_SUPORTADAS -> SUPPORTED_LANGUAGES        RE_RECEPTOR -> RE_RECEIVER
#     RE_CABECA_DA_CADEIA -> RE_CHAIN_HEAD  RE_ATRIB_DE_CHAMADA -> RE_CALL_ASSIGNMENT
#     RE_DECLARACAO_DE_CAMPO -> RE_FIELD_DECLARATION      SEMPRE_CONTA -> ALWAYS_COUNTS
#     RE_NOME_NO_FIM -> RE_TRAILING_NAME    TERMOS_DE_DECISAO -> DECISION_TERMS
#     (RE_ALIAS, RE_CONST, RE_STRING keep their names)
#
#   classes / exceptions
#     Resultado -> Result  (estado, motivo, achados -> state, reason, findings; rotulo -> label;
#                           libera -> allows)    NaoDeuParaChecar -> search.CouldNotCheck
#     states: OK NAO_APLICAVEL ACHADO NAO_AVALIADO -> ok not_applicable findings not_evaluated
#     group keys (run_groups): aliases simbolos constantes fail_open arquivos commits cobertura are
#       KEPT as the internal dict keys (golden R10); `--json` exposes the stable English ids.
#
#   functions
#     _ripgrep -> _ripgrep                  _definicao -> _definition     _contar -> _count
#     linhas_do_plano -> plan_lines         extrair -> extract            _em_lotes -> _in_batches
#     _rg_bruto -> _raw_rg                  checar_em_lote -> check_in_batch
#     _blocos_de_codigo -> _code_blocks     campos_orfaos -> orphan_fields
#     parece_arquivo -> looks_like_file     _resolver -> _resolve         arquivos_citados -> cited_files
#     com_fronteira -> with_boundary        ler_plano -> read_plan
#     linguagem_da_tabela -> table_language config_de_alias -> alias_config
#     _decidir -> _decide                   executar_grupos -> run_groups
#     relatorio_humano -> human_report      sem_comentarios -> without_comments
#     so_codigo -> code_only                rodar -> run                  repo_do -> repo_of
#     existe_no_repo -> exists_in_repo      blocos_de_codigo -> code_blocks
#     tasks_do_plano -> tasks_of_plan       task_de -> task_of
#     checar_aliases -> check_aliases       linguagem_dominante -> dominant_language
#     receptor_de -> receiver_of            criados_pelo_plano -> created_by_plan
#     propagar_de_fora -> propagate_from_outside          checar_simbolos -> check_symbols
#     checar_constantes -> check_constants  checar_fail_open -> check_fail_open
#     checar_arquivos_citados -> check_cited_files        checar_commits -> check_commits
#     checar_cobertura_da_verificacao -> check_verification_coverage
#     checar_promessas_da_spec -> check_spec_promises     preflight -> preflight
#   NEW (not in the INTERNAL): json_report (the `--json` check mode), _t / _use_repo (i18n helpers).
#
#   CLI flags: --humano --prompt --portao --promessas --incremental --spec
#           -> --human  --prompt --gate   --promises   --incremental --spec     (+ NEW --json)
import argparse, json, os, re, shutil, subprocess, sys

# ⚠️ Bootstrap of the sibling modules: the test helper `import_bin` does not touch sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import i18n
import search
from search import CouldNotCheck
from collections import Counter

_LANG_REPO = None


def _use_repo(repo):
    """Language of the user-facing text follows the repo's `.claude/plan-gate.json`."""
    global _LANG_REPO
    _LANG_REPO = repo


def _t(key, **kwargs):
    return i18n.t(key, repo=_LANG_REPO, **kwargs)


SYMBOL_CAP = 60   # the most cited; a big table costs more than it saves
EXCLUDE = ["docs", "vendor", "node_modules", ".git", "dist", "build", "coverage",
           ".wt", "tmp", "var", "generated"]

# Words that show up in backticks but are not code symbols.
NOISE = {
    "NULL", "TRUE", "FALSE", "NONE", "GET", "POST", "PUT", "PATCH", "DELETE",
    "WHERE", "SELECT", "INSERT", "UPDATE", "AND", "OR", "NOT", "JOIN", "ORDER",
    "GROUP", "LIMIT", "TODO", "FIXME", "JSON", "HTTP", "HTTPS", "API", "URL",
    "ID", "UUID", "SQL", "CSV", "PDF", "UTC", "BRT",
}

# Language builtins: they show up in the plan, never as a "repo symbol".
BUILTINS = {
    # PHP
    "trim", "count", "sprintf", "implode", "explode", "in_array", "strlen",
    "str_replace", "substr", "declare", "intval", "floatval", "strval", "empty",
    "isset", "unset", "array_map", "array_filter", "array_keys", "array_values",
    "array_merge", "is_numeric", "is_scalar", "is_array", "is_string", "is_null",
    "json_encode", "json_decode", "usort", "sort", "max", "min", "abs", "round",
    "sprintf", "printf", "preg_match", "preg_replace", "number_format",
    # JS/TS
    "map", "filter", "forEach", "reduce", "push", "pop", "shift", "slice",
    "splice", "join", "split", "then", "catch", "parse", "stringify", "keys",
    "values", "entries", "includes", "indexOf", "find", "some", "every",
    "toString", "valueOf", "concat", "replace", "match", "test", "resolve",
    # Python
    "len", "str", "int", "float", "list", "dict", "set", "print", "open",
    "range", "enumerate", "zip", "sorted", "append", "items", "get", "format",
}


# `if (`, `foreach (`, `catch (` -- syntax, not a method call.
CONTROL_WORDS = {
    "if", "for", "foreach", "while", "switch", "catch", "elseif", "else",
    "return", "function", "fn", "def", "class", "match", "with", "and", "or",
    "not", "in", "as", "use", "new", "echo", "print", "assert", "yield", "await",
    "async", "lambda", "except", "raise", "throw", "typeof", "instanceof",
}

# A definition in a test/fixture is worse evidence than one in production code.
TEST_GLOBS = ["!**/[Tt]est*/**", "!**/*[Tt]est.*", "!**/*_test.*",
              "!**/*.spec.*", "!**/_files/**", "!**/fixtures/**"]


def _ripgrep(repo, pattern, regex_type=True, no_test=False):
    """Returns (n_occurrences, first_evidence 'file:line')."""
    try:
        hits = search.find(repo, pattern, fixed=not regex_type, exclude=EXCLUDE,
                           globs=TEST_GLOBS if no_test else (),
                           max_per_file=1, timeout=25)
    except CouldNotCheck:
        return (-1, "(timeout)")
    if not hits:
        return (0, "")
    return (len(hits), f"{hits[0].file}:{hits[0].line}")


def _definition(repo, pattern):
    """Prefers the definition in production code; falls to a test only if there is none."""
    _, where = _ripgrep(repo, pattern, no_test=True)
    if where:
        return where
    _, where = _ripgrep(repo, pattern)
    return f"{where} {_t('preflight.test_suffix')}" if where else ""


def _count(repo, pattern):
    """Only the total count of occurrences (no -m 1)."""
    try:
        # ⚠️ `only_matched=True` because this counts OCCURRENCES, not lines -- it was
        # `rg --count-matches`. Without it, `usa(X, X, X)` counted 1 instead of 3, and the number
        # that feeds the decorative-constant check came out undercounted.
        return len(search.find(repo, pattern, exclude=EXCLUDE, only_matched=True,
                               timeout=25))
    except CouldNotCheck:
        return -1


# No space before the "(": in code it is `method(`, in prose it is "word (parentheses)". The space
# separates the two cases better than trying to pair ``` fences across 4 thousand lines -- an odd
# fence misaligns the whole rest of the file, and that is how `segmentoDeSecretKey()` escaped.
RE_CALL = re.compile(r"(?:->|::|\.)?\b([A-Za-z_][A-Za-z0-9_]*)\(")


def plan_lines(text, names):
    """{name: [line_number, ...]} -- where the PLAN cites each symbol.

    The preflight says "does not exist in the repo"; the reviewer still needed a grep just to find
    WHERE the plan invokes it (it happened on rounds 11 and 12 of ai-enrich). The plan text is
    already here, so this comes for free.
    """
    target = set(names)
    found = {}
    for i, line in enumerate(text.splitlines(), 1):
        for m in RE_CALL.finditer(line):
            n = m.group(1)
            if n in target:
                found.setdefault(n, [])
                if i not in found[n]:
                    found[n].append(i)
        for m in re.finditer(r"`([A-Za-z_][A-Za-z0-9_]*(?:::|->|\.)?[A-Za-z0-9_]*)\(", line):
            n = re.split(r"::|->|\.", m.group(1))[-1]
            if n in target:
                found.setdefault(n, [])
                if i not in found[n]:
                    found[n].append(i)
    return found


def extract(text):
    """Cited methods (with parentheses) and constants (UPPER_SNAKE).

    Reads the INLINE backticks and also the CODE written in the plan body. The plan writes the
    code it intends to generate inside fenced blocks, and that is where the method it invents
    without creating lives -- on 25/08/2026 `segmentoDeSecretKey()` of Task 8 cost a whole round
    because it was only cited inside a block. A framework call does not pollute: it has uses > 0
    and falls in the "defined outside" bucket; the bucket that matters is the zero-use one.
    """
    methods = Counter()

    for m in re.finditer(r"`([A-Za-z_][A-Za-z0-9_]*(?:::|->|\.)?([A-Za-z_][A-Za-z0-9_]*)?)\(\s*[^`]{0,40}\)`", text):
        raw = m.group(1)
        # only the last segment is kept: Class::method -> method
        name = re.split(r"::|->|\.", raw)[-1]
        if (len(name) >= 3 and name.upper() not in NOISE
                and name not in BUILTINS and not name.isupper()):
            methods[name] += 1

    for m in RE_CALL.finditer(text):
        name = m.group(1)
        if (len(name) >= 3 and name.upper() not in NOISE
                and name not in BUILTINS and name not in CONTROL_WORDS
                and not name.isupper()):
            methods[name] += 1

    constants = Counter()
    for m in re.finditer(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`", text):
        name = m.group(1)
        if name not in NOISE and len(name) >= 5:
            constants[name] += 1
    return methods, constants


# ⚠️ `SEM_RIPGREP` was removed: the only point that consumed it disappeared when the `git` engine
# came to cover the machine without ripgrep. A constant with zero uses is an unkept promise
# (rule 17b) -- and this one promised a degradation mode that no longer exists.


def _in_batches(names, size=120):
    names = list(names)
    for i in range(0, len(names), size):
        yield names[i:i + size]


def _raw_rg(repo, pattern, *, only_matched=False, globs=()):
    """Lines "file:number:text", as rg used to print them -- the callers slice them that way.

    ⚠️ `only_matched` was the raw `-o` of rg. Swapping for a search without it left `uses`
    ZEROED, because whoever counts uses matches the text against the symbol name -- and the whole
    line does not match. A silently ignored parameter is the defect that looks like zeal.
    """
    try:
        hits = search.find(repo, pattern, exclude=EXCLUDE, globs=globs,
                           only_matched=only_matched, timeout=60)
    except CouldNotCheck:
        return ""
    return "".join(f"{a.file}:{a.line}:{a.text}\n" for a in hits)


def check_in_batch(repo, names):
    """A handful of rg calls for ALL the symbols, instead of 2 per name.

    Ranking by frequency and cutting at top-N hid exactly the finding that matters: a method
    that is invented and never created tends to be cited ONCE. With the batch, checking all fits.

    Returns {name: (definition_or_empty, n_uses)}.
    """
    res = {n: ["", 0] for n in names}
    for batch in _in_batches(names):
        alt = "|".join(re.escape(n) for n in batch)

        # uses: any call `name(`
        out = _raw_rg(repo, rf"\b({alt})\s*\(", only_matched=True)
        for line in out.splitlines():
            m = re.search(r":(\d+):\s*([A-Za-z_][A-Za-z0-9_]*)\s*\($", line)
            if m and m.group(2) in res:
                res[m.group(2)][1] += 1

        # definition: preferring production; then test, if only there
        for no_test in (True, False):
            globs = TEST_GLOBS if no_test else ()
            out = _raw_rg(
                repo,
                rf"(?:function|def|fn)\s+({alt})\s*\("
                rf"|(?:public|private|protected|static)[^\n]{{0,60}}\s({alt})\s*\("
                rf"|\b({alt})\s*[:=]\s*(?:async\s+)?(?:function|\()"
                # ⚠️ A class method in TS/JS has no keyword at all:
                # `  reconciliar(pedido) {`. Anchored at line start + `{` or `:` at the end so as
                # NOT to match a call (`  outro(1, 2);` ends in `;`).
                rf"|^\s*(?:async\s+)?({alt})\s*\([^;]*\)\s*(?:\{{|:\s*[A-Za-z_])",
                only_matched=True, globs=globs)
            for line in out.splitlines():
                m = re.match(r"(.+?):(\d+):(.*)$", line)
                if not m:
                    continue
                name = next((n for n in batch if re.search(rf"\b{re.escape(n)}\b", m.group(3))), None)
                if name and not res[name][0]:
                    # ⚠️ NO relpath: `search.find` already returns a path relative TO THE REPO,
                    # and `relpath` resolves the first argument against the PROCESS CWD -- running
                    # from a parent folder with the plan in another repo, it printed
                    # `../.wt/…/plugins/…/plugins/…`, a path that does not exist. It is the same
                    # `../../..` that the comment of `_resolve` documents; the other three callers
                    # were fixed and this one was left behind.
                    where = f"{m.group(1)}:{m.group(2)}"
                    res[name][0] = where if no_test else f"{where} {_t('preflight.test_suffix')}"
    return {k: tuple(v) for k, v in res.items()}


# --- Check 7: field declared and not propagated ------------------------------
# Born from a real plan: THREE distinct review blocks were the SAME
# defect -- a new field declared at one end (`OpcoesDaRodada`) and never read in the middle of the
# chain that should carry it:
#
#   round 2:  `email`               declared, `rodar()` did not receive it
#   round 3:  `somentePrioritarios` arrived at `execute`, died before `executarCanal`
#   round 8:  `destacaPrioritarios` declared, no branch read it
#
# In all three the plan carried a COMMENT explaining the intended behavior in place of the line
# that produces it. A comment is not propagation -- same rule as the CLAUDE.md ("a comment that
# describes a risk is not protection against it").
#
# Check 5 already catches this for a CONSTANT (UPPER_SNAKE). An object field is lowercase and
# escaped entirely.

# `name: type` / `name?: type` inside a code block, with ; or , at the end.
RE_FIELD = re.compile(
    r"^\s*(?:readonly\s+)?([a-z][A-Za-z0-9_]{2,})\??\s*:\s*[^;,{]{2,}[;,]?\s*$")

def _code_blocks(text):
    """[(first_line, [lines])] of each block fenced by triple backticks."""
    blocks, current, start, inside = [], [], 0, False
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            if inside:
                blocks.append((start, current))
                current = []
            else:
                start = i + 1
            inside = not inside
        elif inside:
            current.append(line)
    return blocks


def orphan_fields(text):
    """Symbols the plan DECLARES and cites only once -- suspected of not being propagated.

    Decides nothing: shows where each one appears. Only reading tells "declared and read" from
    "declared and forgotten" -- but a SINGLE citation is a declaration with no consumption at all,
    and that is mechanical.
    """
    declared = {}
    for start, lines in _code_blocks(text):
        for j, line in enumerate(lines):
            line_n = start + j
            m = RE_FIELD.match(line)
            # ⚠️ UPPER_SNAKE stays OUT: it is a constant, already covered by check 5. Counting in
            # both places only produces noise, and noise teaches the reviewer to ignore the section.
            if m and not m.group(1).isupper():
                declared.setdefault(m.group(1), line_n)

    if not declared:
        return "", 0

    every = {}
    for i, line in enumerate(text.splitlines(), 1):
        for name in declared:
            if re.search(r"\b" + re.escape(name) + r"\b", line):
                every.setdefault(name, []).append(i)

    orphans = [(n, d) for n, d in sorted(declared.items()) if len(every.get(n, [])) <= 1]

    if not orphans:
        return _t("preflight.orphans_none", n=len(declared)), 0

    return _t("preflight.orphans_found", n=len(orphans),
              items="; ".join(_t("preflight.orphan_item", name=n, line=l) for n, l in orphans)), len(orphans)


# --- Check 6: the files the plan cites ---------------------------------------
# Measured on 25/08/2026 on megaleilao: the reviewer touched `.deploy/nginx_monitor.sh` in 9 of 10
# calls, `entry.sh` and `.gitlab-ci.yml` in 6 each. The four together came to 3,478 tokens; the
# review cost 82,296. Delivering the content ONCE costs less than it rereading on every turn with
# the context already at 76k.
# Injecting EVERYTHING the plan cites does not pay: in a code repo the plan cites 20+ files read
# once each, and the prompt becomes 20k tokens without killing any rereading. The measured waste
# was in the FEW files read MANY times -- on megaleilao `nginx_monitor.sh` was cited 10x in the plan
# and read in 9 of 10 calls; `entry.sh`, cited 7x, read 6x. The citation count predicts rereading,
# so it is what decides who gets in.
MIN_CITATIONS = 3
CHAR_CAP = 24_000     # ~6k tokens of injected content, in total
LINE_CAP_PER_FILE = 600   # above this it only announces; the reader takes the slice it wants

# They exist, are cited, and injecting them is waste: no plan-review finding comes out of a lockfile.
NEVER_INJECT = {"composer.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                "poetry.lock", "Gemfile.lock", "go.sum", "Cargo.lock"}

# ⚠️ `Pedido.reconciliar` matches `name.ext` as well as `servico.ts`. Without a list of real
# extensions, every dotted symbol in the plan became a "cited file that does not exist in the repo"
# -- noise the reviewer has to refute, and noise in a preflight costs the trust in the whole preflight.
FILE_EXTENSIONS = {
    "php", "js", "jsx", "ts", "tsx", "vue", "py", "rb", "go", "rs", "java", "kt",
    "sh", "bash", "sql", "html", "css", "scss", "less", "twig", "phtml", "blade",
    "json", "jsonc", "yml", "yaml", "toml", "ini", "env", "xml", "xsd", "csv",
    "md", "txt", "lock", "cfg", "conf", "vhost", "service", "dist", "example",
}


def looks_like_file(cited):
    """True when the text in backticks is a file path, not `Class.method`."""
    if "/" in cited:
        return True
    base = os.path.basename(cited)
    if base in ("Dockerfile", "Makefile") or base.startswith("Dockerfile"):
        return True
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    return ext in FILE_EXTENSIONS


RE_PATH = re.compile(
    r"`([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*\.[A-Za-z][A-Za-z0-9]{0,9}"
    r"|Dockerfile[A-Za-z0-9_.\-]*|Makefile)`")


def _resolve(repo, cited):
    """Returns (path_in_repo, exact). exact=False when it only matched by name."""
    direct = os.path.join(repo, cited)
    if os.path.isfile(direct):
        return os.path.relpath(direct, repo), True
    base = os.path.basename(cited)
    try:
        hits = search.list_files(repo, glob=f"**/{base}", exclude=EXCLUDE, timeout=20)
    except CouldNotCheck:
        return None, False
    # ⚠️ The path already comes RELATIVE to the repo. Passing it through relpath(..., repo) would
    # resolve against the process cwd and produce `../../..` -- a path that does not exist.
    return (hits[0], False) if len(hits) == 1 else (None, False)


def cited_files(text, repo):
    """Returns (markdown_block, n_injected)."""
    cited = Counter()
    for m in RE_PATH.finditer(text):
        c = m.group(1)
        # An absolute path is a deploy target (/opt/...), not a repo file.
        # "..." is an ellipsis in the text, not a path.
        if c.startswith("/") or "..." in c or len(c) > 120 or not looks_like_file(c):
            continue
        cited[c] += 1

    resolved, missing, approximate = {}, [], []
    for c, freq in cited.most_common():
        target, exact = _resolve(repo, c)
        if target is None:
            missing.append(c)
        else:
            if not exact:
                # ⚠️ Matching by name can HIDE a path that does not exist: a plan that MOVES
                # `.deploy/x.sh` to `bin/x.sh` would match both to the same file. The reviewer
                # needs to know it was approximated.
                approximate.append((c, target))
            # dedupe: `entry.sh` and `.deploy/entry.sh` are the same file
            resolved[target] = resolved.get(target, 0) + freq

    if not resolved and not missing:
        return "", 0

    lines = [_t("preflight.c6_title"), ""]

    if missing:
        lines += [
            _t("preflight.c6_missing", items=", ".join(f"`{x}`" for x in sorted(missing))),
            "",
            _t("preflight.c6_missing_note"),
            "",
        ]

    if approximate:
        lines += [
            _t("preflight.c6_approx_head"),
            "",
        ] + [_t("preflight.c6_approx_item", cited=c, shown=a) for c, a in approximate] + [
            "",
            _t("preflight.c6_approx_note"),
            "",
        ]

    spent, injected, on_demand = 0, [], []
    for target, freq in sorted(resolved.items(), key=lambda kv: -kv[1]):
        try:
            content = open(os.path.join(repo, target), encoding="utf-8",
                           errors="replace").read()
        except OSError:
            continue
        n_lines = content.count("\n") + 1
        fits = (freq >= MIN_CITATIONS
                and os.path.basename(target) not in NEVER_INJECT
                and n_lines <= LINE_CAP_PER_FILE
                and spent + len(content) <= CHAR_CAP)
        if fits:
            spent += len(content)
            injected.append((target, content, n_lines, freq))
        else:
            on_demand.append((target, n_lines, freq))

    if injected:
        lines += [
            _t("preflight.c6_injected_1"),
            _t("preflight.c6_injected_2"),
            _t("preflight.c6_injected_3"),
            "",
        ]
        for target, content, n_lines, freq in injected:
            # A fence longer than any run of backticks in the file itself: an injected .md has ```
            # in the body and would close the block early, scrambling the rest of the prompt.
            longest = max((len(m) for m in re.findall(r"`+", content)), default=0)
            fence = "`" * max(3, longest + 1)
            lines += [_t("preflight.c6_file_head", target=target, n_lines=n_lines, freq=freq),
                      "", fence]
            for i, l in enumerate(content.splitlines(), 1):
                lines.append(f"{i:6d}\t{l}")
            lines += [fence, ""]

    if on_demand:
        lines += [
            _t("preflight.c6_on_demand", minimum=MIN_CITATIONS),
            "",
            ", ".join(_t("preflight.c6_on_demand_item", target=a, n=n, f=f)
                      for a, n, f in sorted(on_demand, key=lambda x: -x[2])),
            "",
        ]
    return "\n".join(lines), len(injected)


# ⚠️ `CouldNotCheck` lives in `search.py` and is IMPORTED at the top. There used to be a local class
# with the same name here, defined AFTER the import -- it shadowed the imported one, and the result
# was the worst possible: `search` raised one class and `with_boundary` caught another, so the
# operational failure ESCAPED the boundary instead of becoming not_evaluated.


class Result:
    """State of ONE check group.

    ⚠️ An empty list is not proof of anything by itself. `ok` requires that the extractor ran,
    recognized the structure and examined at least one item OF THE GROUP ITSELF. The border between
    not_applicable and not_evaluated is a single question: did the extractor recognize the
    structure? If it did and there was no item, it is not_applicable (releases); if there was
    something to examine and we did not know how to read it, it is not_evaluated (refuses).
    """
    STATES = ("ok", "not_applicable", "findings", "not_evaluated")

    def __init__(self, state, reason=None, findings=()):
        assert state in self.STATES, state
        assert (state == "findings") == bool(findings), f"{state} with findings={list(findings)}"
        assert (state in ("not_applicable", "not_evaluated")) == bool(reason), f"{state} without reason"
        self.state, self.reason, self.findings = state, reason, list(findings)

    def label(self):
        return {"ok": "OK  ", "not_applicable": "n/a ", "not_evaluated": " -- "}.get(
            self.state, f"{len(self.findings):<4}")

    def allows(self):
        return self.state in ("ok", "not_applicable")


def with_boundary(fn):
    """SINGLE BOUNDARY of operational failure.

    ⚠️ Enumerating the expected exceptions already failed once: `run()` caught only
    TimeoutExpired and returned "" for the rest, and "" arrives indistinguishable from "not found".
    """
    try:
        findings = fn()
    except CouldNotCheck as e:
        return Result("not_evaluated", str(e))
    except (OSError, ValueError, UnicodeError, re.error, TypeError, KeyError,
            IndexError, AttributeError) as e:
        return Result("not_evaluated", f"{type(e).__name__}: {e}")
    if findings and not isinstance(findings, (list, tuple)):
        return Result("not_evaluated", _t("preflight.not_a_list", type=type(findings).__name__))
    return Result("findings", findings=list(findings)) if findings else Result("ok")


def read_plan(plan):
    """Bytes -> text, decoding STRICTLY.

    ⚠️ `errors="replace"` turned an invalid byte into U+FFFD and went on as if nothing had
    happened: the extractor read a text that is NOT the file, and the result came out as a
    successful check.
    """
    try:
        with open(plan, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise CouldNotCheck(_t("preflight.cannot_read_plan", error=e))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CouldNotCheck(_t("preflight.plan_not_utf8", byte=e.start))


# (internal key, stable English id). The title is localized: `_t("preflight.group." + id)`.
GROUPS = (
    ("aliases", "aliases"),
    ("simbolos", "invoked-symbols"),
    ("constantes", "decorative-constants"),
    ("fail_open", "tolerant-defaults"),
    ("arquivos", "cited-files"),
    ("commits", "created-not-committed"),
    ("cobertura", "verification-gap"),
)


def _group_title(group_id):
    return _t("preflight.group." + group_id)


# ⚠️ GENERIC fence: counts every fenced block, even one RE_BLOCK cannot close. Without this, a plan
# whose fences RE_BLOCK does not match (odd fence, nested ~~~) would come out not_applicable -- "there
# is no code" -- when the right answer is not_evaluated: there is code and we did not know how to read
# it. That is how `segmentoDeSecretKey()` escaped once.
RE_FENCE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})", re.M)
RE_FILES_SECTION = re.compile(r"^\s*\*\*Files:\*\*", re.M)
RE_COMMIT_BLOCK = re.compile(r"^\s*```(?:bash|sh|shell)\b[^\n]*\n(?:.*?)^\s*```", re.S | re.M)
RE_FILE_ITEM = re.compile(r"^\s*[-*]\s*(Create|Modify|Test)\s*:\s*`([^`]+)`", re.M | re.I)

# ⚠️ MEASURED, and the cause is not what the old comment said. The report gave 18 false findings on
# a PHP framework plan not because `function method(` does not match -- it matches -- but because
# EXCLUDE cuts `vendor/`, and in such frameworks the whole framework lives there. So: for a language whose
# framework lives in vendor/, the SECOND pass (does it exist in the repo?) includes vendor/.
DEFINITION_BY_LANGUAGE = {
    "py": r"(?:async\s+)?def\s+{esc}\s*\(",
    "php": r"function\s+{esc}\s*\(",
    "ts": (r"(?:function|fn)\s+{esc}\s*\("
           r"|(?:public|private|protected|static)[^\n]{{0,60}}\s{esc}\s*\("
           r"|\b{esc}\s*[:=]\s*(?:async\s+)?(?:function|\()"
           r"|^\s*(?:async\s+)?{esc}\s*\([^;]*\)\s*\{{"),
}
INCLUDES_VENDOR = {"php"}
NORMALIZE_LANGUAGE = {"typescript": "ts", "tsx": "ts", "javascript": "ts", "js": "ts",
                      "jsx": "ts", "ts": "ts", "python": "py", "py": "py", "php": "php"}


def table_language(text):
    """Normalized key (`ts`/`py`/`php`), or None if the language is not in the table."""
    return NORMALIZE_LANGUAGE.get(dominant_language(text) or "")


def alias_config(repo):
    """Path of the alias config, None if there is none, False if there is one and it does NOT parse.

    ⚠️ THREE answers on purpose. With two, a broken config becomes "has no alias" -- which is
    not_applicable, which RELEASES.
    """
    for name in ("tsconfig.json", "jsconfig.json", "composer.json", "package.json"):
        path = os.path.join(repo, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            return False
        try:
            json.loads(re.sub(r"//[^\n]*", "", content))   # tsconfig accepts comments
        except ValueError:
            return False
        if '"paths"' in content or '"autoload"' in content or '"imports"' in content:
            return path
    return None


def _decide(structure_ok, not_evaluated_reason, has_item, not_applicable_reason, fn):
    """The two applicability questions, in the order that matters.

    ⚠️ `structure_ok` comes FIRST. Reversing it would make a plan with a fenced block in an unknown
    language come out not_applicable ("there is no item") instead of not_evaluated -- and
    not_applicable releases. The order is the difference between refusing and approving on the void.
    """
    if not structure_ok:
        return Result("not_evaluated", not_evaluated_reason)
    if not has_item:
        return Result("not_applicable", not_applicable_reason)
    return with_boundary(fn)


def run_groups(text, repo, tasks):
    """{key: Result}, one per group, cell by cell of the applicability table."""
    _use_repo(repo)
    # ⚠️ No dividing by 2: an ODD fence (opens and never closes) is exactly the case that must
    # become not_evaluated, and `//2` erased it -- 1 fence became 0 and the group came out
    # not_applicable, which releases. The structure is OK when each readable block consumes two
    # fences and none is left over.
    fences = len(RE_FENCE.findall(text))
    blocks = list(RE_BLOCK.finditer(text))
    lang = table_language(text)
    has_files_section = bool(RE_FILES_SECTION.search(text))
    items = RE_FILE_ITEM.findall(text)
    created = [c for c in items if c[0].lower() in ("create", "test")]
    commit_blocks = RE_COMMIT_BLOCK.findall(text)
    has_git_add = any("git add" in b for b in commit_blocks)
    alias_cfg = alias_config(repo)
    languages = ", ".join(sorted(set(NORMALIZE_LANGUAGE)))
    # ⚠️ Five of the seven groups receive `tasks` and decide based on it ("does some EARLIER task
    # create this symbol?"). Without a recognized task they do not "find nothing": they had no way
    # to find. That is what produced 10 false findings in a real plan.
    no_tasks = not tasks
    no_task_reason = _t("preflight.reason_no_task")

    return {
        "aliases": _decide(
            alias_cfg is not False, _t("preflight.reason_alias_broken"),
            bool(alias_cfg), _t("preflight.reason_no_alias"),
            lambda: check_aliases(text, repo)),

        "simbolos": _decide(
            not no_tasks and (fences == 2 * len(blocks)) and (not blocks or lang is not None),
            (no_task_reason if no_tasks else
             _t("preflight.reason_fences", fences=fences, blocks=len(blocks))
             if fences != 2 * len(blocks)
             else _t("preflight.reason_language", languages=languages)),
            bool(blocks), _t("preflight.reason_no_code_block"),
            lambda: check_symbols(text, repo, tasks)),

        "constantes": _decide(
            not no_tasks and (not blocks or lang is not None),
            (no_task_reason if no_tasks else _t("preflight.reason_constant_language")),
            bool(RE_CONST.search(text)), _t("preflight.reason_no_constant"),
            lambda: check_constants(text, tasks)),

        "fail_open": _decide(
            not no_tasks and fences == 2 * len(blocks),
            (no_task_reason if no_tasks else
             _t("preflight.reason_fences", fences=fences, blocks=len(blocks))),
            bool(blocks), _t("preflight.reason_no_code_block"),
            lambda: check_fail_open(text, tasks)),

        "arquivos": _decide(
            not has_files_section or bool(items),
            _t("preflight.reason_files_unrecognized"),
            has_files_section, _t("preflight.reason_no_files_section"),
            lambda: check_cited_files(text, repo)),

        # ⚠️ Predicate per ITEM OF THE GROUP ITSELF. And "no commit block" is not_applicable, not
        # not_evaluated: the plan simply does not promise a commit, and there is nothing to examine.
        # not_evaluated stays for when there IS a commit block and no `git add` was recognized
        # inside it -- then there was something to read and we did not read it. The previous
        # version classified every short plan as not_evaluated, which would make the gate refuse
        # almost always and push everyone toward the escape.
        "commits": _decide(
            not no_tasks, no_task_reason,
            bool(created), _t("preflight.reason_no_creates"),
            lambda: check_commits(text, tasks)),

        "cobertura": _decide(
            bool(tasks), _t("preflight.reason_no_task_heading"),
            bool(created), _t("preflight.reason_no_verifiable_item"),
            lambda: check_verification_coverage(text, tasks)),
    }


def human_report(plan, repo):
    """Runs the seven groups and returns (text, findings, not_evaluated).

    ⚠️ The signature is exactly this. The spec-promises advisory does NOT enter here: it is
    heuristic (6 findings, all noise, on the first real run), lives only in the `--promises` branch
    of the CLI and never changes the exit code. Passing `spec` through this function was the path for
    it to leak into the gate.
    """
    _use_repo(repo)
    try:
        plan_text = read_plan(plan)
    except CouldNotCheck as e:
        not_evaluated = [(_group_title(gid), str(e)) for _, gid in GROUPS]
        body = "\n".join(_t("preflight.unreadable_line", title=_group_title(gid), reason=e) for _, gid in GROUPS)
        return _t("preflight.header_unreadable", name=os.path.basename(plan)) + "\n\n" + body, [], not_evaluated

    tasks = tasks_of_plan(plan_text)
    lines = [_t("preflight.header", plan=os.path.relpath(plan, repo), n=len(tasks)), ""]
    findings, not_evaluated = [], []
    results = run_groups(plan_text, repo, tasks)
    for key, gid in GROUPS:
        title = _group_title(gid)
        r = results[key]
        lines.append(f"[{r.label()}] {title}")
        if r.reason:
            # ⚠️ The label a person reads is `NOT EVALUATED`/`NOT APPLICABLE`, with a space: it is
            # what the SKILL.md and the README document, and what the wave 1 tests fixed. The
            # internal state uses `_` -- an internal refactor does not change distributed vocabulary.
            lines.append(f"        {_t('preflight.state.' + r.state)}: {r.reason}")
            if r.state == "not_evaluated":
                # ⚠️ Only here. In not_applicable there was nothing to check, and saying "absence
                # of a check" would be a false alarm -- the opposite of the problem the state exists
                # to solve.
                lines.append("        " + _t("preflight.not_ok"))
        for x in r.findings:
            lines.append(f"        - {x}")
            findings.append((title, x))
        if r.state == "not_evaluated":
            not_evaluated.append((title, r.reason))

    lines.append("")
    if not_evaluated:
        lines.append(_t("preflight.summary_not_run", n=len(not_evaluated)))
    if findings:
        lines.append(_t("preflight.summary_findings", n=len(findings)))
    if not findings and not not_evaluated:
        lines.append(_t("preflight.summary_clean_1"))
        lines.append(_t("preflight.summary_clean_2"))
        lines.append(_t("preflight.summary_clean_3"))
    return "\n".join(lines), findings, not_evaluated


# =============================================================================
# The seven groups of the HUMAN REPORT, coming from `pre-voo-do-plano.py` (18/09/2026).
#
# ⚠️ They were two programs with the same purpose and checks that had already diverged: check 7
# (orphan fields) existed only here, and extractor fixes in one did not reach the other. Collisions
# were resolved like this, and not by 'choosing the best':
#   RE_CHAMADA of the report -> RE_CODE_CALL (it accepts a space before the `(`)
#   BUILTINS of the report   -> REPORT_BUILTINS
#   EXCLUIR                  -> the list in this file, with 'tmp' added (union)
# =============================================================================

RE_BLOCK = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n(.*?)^[ \t]*\1", re.S | re.M)

RE_CODE_CALL = re.compile(r"(?:->|::|\.)?\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

RE_COMMENT = re.compile(
    r"/\*.*?\*/|(?<![:'\"])//[^\n]*|^\s*#[^\n]*|\{/\*.*?\*/\}", re.S | re.M)

RE_STRING = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`", re.S)

RE_JSX_TEXT = re.compile(r">[^<>{}\n]{3,}(?=[{<])")

# ⚠️ Added on 18/09, measured on the wave 2 plan: wave 1.5 put Python in the language table, and the
# extractor -- calibrated for TS/JS -- started accusing 12 "invented methods" that are stdlib and
# unittest. `self.assertX()` is the worst of them: `self` is in ALWAYS_COUNTS (on purpose, to catch
# `this.methodThatDoesNotExist()`), so the receiver rule does not cut it. The right way out here is
# a noise list, not loosening the rule.
PYTHON_NOISE = {
    # builtins that show up as a call in a code block
    "enumerate", "float", "super", "zip", "sorted", "reversed", "isinstance", "getattr",
    "setattr", "hasattr", "repr", "format", "divmod", "round", "abs", "sum", "any", "all",
    "bytes", "bytearray", "frozenset", "staticmethod", "classmethod", "property",
    # str/list/dict/datetime methods that any code uses
    "startswith", "endswith", "splitlines", "rsplit", "lstrip", "rstrip", "partition",
    "rpartition", "setdefault", "total_seconds", "isoformat", "timestamp", "astimezone",
    "strftime", "strptime", "hexdigest", "encode", "decode", "readlines", "writelines",
    "expanduser", "expandvars", "realpath", "relpath", "normpath", "makedirs", "replace",
    # unittest: `self.assertX` is framework, not a promise of the plan
    "assertEqual", "assertNotEqual", "assertTrue", "assertFalse", "assertIs", "assertIsNot",
    "assertIsNone", "assertIsNotNone", "assertIn", "assertNotIn", "assertRaises",
    "assertRaisesRegex", "assertAlmostEqual", "assertGreater", "assertGreaterEqual",
    "assertLess", "assertLessEqual", "assertRegex", "assertNotRegex", "assertCountEqual",
    "assertListEqual", "assertDictEqual", "assertSetEqual", "skipTest", "subTest",
    "setUpClass", "tearDownClass", "addCleanup", "assertWarns", "assertLogs",
}

REPORT_BUILTINS = PYTHON_NOISE | {
    "if", "for", "foreach", "while", "switch", "catch", "return", "function", "def", "class",
    "new", "await", "async", "typeof", "instanceof", "throw", "yield", "import", "export",
    "console", "log", "error", "warn", "map", "filter", "forEach", "reduce", "push", "join",
    "split", "slice", "then", "catch", "parse", "stringify", "keys", "values", "includes",
    "find", "some", "every", "toString", "concat", "replace", "match", "test", "resolve",
    "reject", "Number", "String", "Boolean", "Array", "Object", "Math", "JSON", "Date",
    "Promise", "Set", "Map", "expect", "it", "describe", "beforeEach", "afterEach", "vi",
    "trim", "toFixed", "slice", "sort", "len", "str", "int", "print", "range", "setState",
    "useState", "useEffect", "require", "toEqual", "toBe", "not", "toHaveBeenCalled",
    # vitest / jest
    "toMatch", "toBeCloseTo", "toThrow", "toHaveBeenCalledTimes", "toHaveBeenCalledWith",
    "mockResolvedValue", "mockRejectedValue", "mockImplementation", "clearAllMocks",
    "fn", "mock", "resolves", "rejects", "toBeUndefined", "toContain", "toHaveLength",
    "toBeTruthy", "toBeFalsy", "toBeNull", "toBeGreaterThan", "toBeLessThan",
    "toHaveProperty", "toStrictEqual", "toBeInstanceOf", "clearAllTimers", "useFakeTimers",
    "assign", "readFileSync", "existsSync", "basename", "relpath",
    # JS/TS builtins that show up as `X.y()`
    "isSafeInteger", "isInteger", "isFinite", "isNaN", "round", "abs", "from", "of", "now",
    "toISOString", "padStart", "padEnd", "setTimeout", "clearTimeout", "all", "race",
    # React
    "useMemo", "useCallback", "useRef",
}


def without_comments(code: str) -> str:
    return RE_COMMENT.sub(" ", code)


# ⚠️ A Python comment at the END of the line (`x = 1  # note (issue)`). RE_COMMENT only caught `#` at
# the start of the line -- enough while the extractor was TS/JS, where `//` covers both cases. With
# Python in the table (wave 1.5), the comment text started to be scanned for calls: "hang at
# startup (opencode#35870)" became an invented `inicializacao()`. Applied AFTER RE_STRING, so that
# `"#!/bin/sh"` does not become a comment.
RE_TRAILING_COMMENT = re.compile(r"(?<!['\"])#[^\n]*")


def code_only(code: str) -> str:
    """No comment, no string and no JSX text -- what is left is a real call."""
    clean = RE_COMMENT.sub(" ", code)
    clean = RE_STRING.sub('""', clean)
    clean = RE_TRAILING_COMMENT.sub(" ", clean)
    return RE_JSX_TEXT.sub("><", clean)
RE_ALIAS = re.compile(r"['\"](@[A-Za-z][A-Za-z0-9_]*)/")
RE_CONST = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")

# Defaults that turn absence into a plausible value. On a decision or money path, each of these
# has already cost a block. The label is localized text, only shown -- never matched on.
FAIL_OPEN_PATTERNS = [
    (r"\?\?\s*0\b", "preflight.fo.nullish_zero"),
    (r"\|\|\s*0\b", "preflight.fo.or_zero"),
    (r"\?\?\s*\[\]", "preflight.fo.nullish_list"),
    (r"\|\|\s*\[\]", "preflight.fo.or_list"),
    (r"\?\?\s*\{\}", "preflight.fo.nullish_object"),
    (r"getattr\([^,]+,\s*[^,]+,\s*(\{\}|\[\]|0|None)\)", "preflight.fo.getattr"),
    (r"catch\s*\([^)]*\)\s*\{\s*\}", "preflight.fo.empty_catch"),
    (r"catch\s*\{\s*set\w+\(\[\]\)", "preflight.fo.catch_empty_list"),
    (r"in\s+str\(e\)", "preflight.fo.substring_status"),
]
# Flagged in any context; the other patterns only next to a decision term (check_fail_open).
ALWAYS_FLAGGED = {"preflight.fo.empty_catch", "preflight.fo.catch_empty_list", "preflight.fo.substring_status"}


def run(cmd, timeout=25, cwd=None):
    """⚠️ Does NOT swallow the exception. The previous version returned "" for any failure, and ""
    reaches the groups indistinguishable from "searched and found nothing" -- with the gate reading
    that result, an `rg` with a timeout would open the gate."""
    if not shutil.which(cmd[0]):
        raise CouldNotCheck(_t("preflight.not_installed", cmd=cmd[0]))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise CouldNotCheck(_t("search.timed_out", cmd=cmd[0], timeout=timeout))
    except OSError as e:
        raise CouldNotCheck(_t("search.failed", cmd=cmd[0], error=e))
    if r.returncode not in (0, 1):      # rg: 0 found, 1 not found, 2+ a real error
        raise CouldNotCheck(_t("search.exited", cmd=cmd[0], code=r.returncode,
                               stderr=r.stderr.strip()[:120]))
    return r.stdout


def repo_of(path):
    r = run(["git", "-C", os.path.dirname(os.path.abspath(path)),
             "rev-parse", "--show-toplevel"])
    return r.strip() or None


def exists_in_repo(repo, pattern):
    """Returns the first evidence 'file:line', or ''."""
    hits = search.find(repo, pattern, exclude=EXCLUDE, max_per_file=1)
    return f"{hits[0].file}:{hits[0].line}" if hits else ""


def code_blocks(text):
    return [m.group(2) for m in RE_BLOCK.finditer(text)]


# ⚠️ Measured on 18/09/2026 against 20 real plans: `### Task N:` covers 14 of them,
# `## Task N:` and `## Task N — ` cover another 3, and the rest have no task heading at all (those
# fall in the "no task recognized" path, in main). Before this, the 3 in the middle gave ZERO tasks
# -- and without tasks the rule "does an earlier task create this symbol?" has nothing to consult,
# so every library symbol became "invented method": 10 findings, 10 false, in a real plan.
RE_TASK_HEADING = re.compile(
    r"^#{2,4}\s+Task\s+(\d+)\s*(?:[:\-\u2013\u2014]\s*)?(.*)$", re.M)


def tasks_of_plan(text):
    """[(number, title, start, end)] -- to say in WHICH task each finding lives."""
    marks = [(m.start(), int(m.group(1)), m.group(2).strip())
             for m in RE_TASK_HEADING.finditer(text)]
    out = []
    for i, (pos, num, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((num, title, pos, end))
    return out


def task_of(tasks, pos):
    for num, title, start, end in tasks:
        if start <= pos < end:
            return num
    return None


# ---------------------------------------------------------------- checks

def check_aliases(text, repo):
    """An alias used in the plan has to exist in tsconfig AND in vitest.config."""
    findings = []
    used = Counter()
    for block in code_blocks(text):
        for m in RE_ALIAS.finditer(block):
            used[m.group(1)] += 1
    if not used:
        return findings

    def aliases_of(file, key):
        p = os.path.join(repo, file)
        if not os.path.isfile(p):
            return None
        with open(p, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        return set(re.findall(r"['\"](@[A-Za-z][A-Za-z0-9_]*)", content))

    ts = aliases_of("tsconfig.json", "paths")
    vt = aliases_of("vitest.config.ts", "alias") or aliases_of("vitest.config.js", "alias")

    for alias, n in used.most_common():
        missing = []
        if ts is not None and alias not in ts:
            missing.append("tsconfig.json")
        if vt is not None and alias not in vt:
            missing.append("vitest.config")
        # ⚠️ If the plan itself has a step that ADDS the alias, it is not a defect -- it is the
        # fix. Accusing that teaches the owner to ignore the whole section.
        if missing and re.search(
                rf"['\"]{re.escape(alias)}['\"]\s*:\s*path\.resolve", text):
            continue
        if missing:
            findings.append(_t("preflight.alias_missing", alias=alias, n=n, missing=", ".join(missing)))
    return findings


RE_LANGUAGE = re.compile(r"^[ \t]*`{3,}\s*([A-Za-z+#]+)", re.M)


def dominant_language(text):
    c = Counter(m.group(1).lower() for m in RE_LANGUAGE.finditer(text))
    for junk in ("bash", "sh", "shell", "console", "text", "diff", "json", "yaml", "xml"):
        c.pop(junk, None)
    return c.most_common(1)[0][0] if c else None


SUPPORTED_LANGUAGES = {"typescript", "ts", "tsx", "javascript", "js", "jsx"}


RE_RECEIVER = re.compile(r"([A-Za-z_$][\w$]*)\s*\.\s*$")
RE_CHAIN_HEAD = re.compile(r"([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*\.\s*$")
RE_CALL_ASSIGNMENT = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:await\s+)?([A-Za-z_$][\w$]*)\s*[\.\(]")
# ⚠️ A field declaration (`travados: string[]`) is not an invocation. Without this, every plan that
# declares an interface contributes one false finding per field -- it happened with `travados` in a
# real plan.
RE_FIELD_DECLARATION = re.compile(r"^\s*[A-Za-z_$][\w$]*\??\s*:\s*[A-Za-z_$\[\{]")
ALWAYS_COUNTS = {"this", "self", "$this"}


RE_TRAILING_NAME = re.compile(r"([A-Za-z_$][\w$]*)\s*$")


def receiver_of(clean, name_pos):
    r"""Name of the receiver, or the HEAD of the chain when the receiver is a call.

    ⚠️ `expect(x).toBeVisible()` has receiver `)` -- not a name. Treating that as a "bare call" made
    `toBeVisible` count as a promise of the plan. What matters is who OPENS the chain: if `expect`
    is not ours, nothing that comes from it is.

    ⚠️ The parenthesis must be BALANCED, not matched by regex: the real case is
    `expect(card.getByRole('button')).toBeVisible()`, with nesting. A `\([^()]*\)` does not match
    that and returns None -- which the rule reads as a "bare call" and keeps the finding.
    """
    before = clean[:name_pos].rstrip()
    if not before.endswith("."):
        return None
    before = before[:-1].rstrip()
    if before.endswith(")"):
        level, i = 0, len(before) - 1
        while i >= 0:
            if before[i] == ")":
                level += 1
            elif before[i] == "(":
                level -= 1
                if level == 0:
                    break
            i -= 1
        if i < 0:
            return None
        before = before[:i]
    m = RE_TRAILING_NAME.search(before)
    return m.group(1) if m else None


def created_by_plan(text):
    """Names the plan DECLARES.

    ⚠️ A callback parameter does NOT count: `test('...', async ({ page }) => ...)` receives `page`
    from the framework -- the plan only names it. Counting that as creation made `page` ours, and
    with it every `const card = page.getBy...`.
    """
    names = set()
    for m in re.finditer(r"\b(?:const|let|var|class|function|type|interface|def)\s+([A-Za-z_$][\w$]*)", text):
        names.add(m.group(1))
    for m in re.finditer(r"\b(?:const|let|var)\s*[\{\[]([^}\]]+)[\}\]]\s*=", text):
        for part in m.group(1).split(","):
            n = part.split(":")[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", n):
                names.add(n)
    return names


def propagate_from_outside(text, created):
    """`const card = page.getByTestId(...)` -> `card` is also from outside.

    Without this the rule cuts one line and lets the next through, which is the same noise. A short
    fixed point: a chain in a test plan has 2 or 3 links, not 30.
    """
    from_outside = set()
    for _ in range(5):
        changed = False
        for m in RE_CALL_ASSIGNMENT.finditer(text):
            target, origin = m.group(1), m.group(2)
            if (origin not in created or origin in from_outside) and target not in from_outside:
                from_outside.add(target); changed = True
        if not changed:
            break
    return from_outside


def check_symbols(text, repo, tasks):
    """An invoked method has to exist in the repo OR be created by an EARLIER task.

    ⚠️ Calibrated for TS/JS. Run on an approved PHP plan, it gave 18
    findings because `$this->method()` and `Class::method()` do not match the definition patterns
    here. Instead of shouting where I cannot judge, I declare that I did NOT evaluate -- saying
    "OK" about what was not checked is worse than not checking.
    """
    findings = []
    invocations = {}   # name -> smallest position of use
    created = created_by_plan(text)
    ours = created - propagate_from_outside(text, created)
    for m in RE_BLOCK.finditer(text):
        base = m.start(2)
        clean = code_only(m.group(2))
        for c in RE_CODE_CALL.finditer(clean):
            name = c.group(1)
            if len(name) < 4 or name in REPORT_BUILTINS or name.isupper():
                continue
            line = clean[clean.rfind("\n", 0, c.start(1)) + 1:c.start(1)]
            if RE_FIELD_DECLARATION.match(line + name):
                continue      # `travados: string[]` is not an invocation
            # `Logfy.create(...)`, `Number.isSafeInteger(...)`: a method of an imported
            # module/class. Checking would require resolving the import, and the symbol is not the
            # plan's. A lowercase receiver (`this.x()`, `pedido.y()`) still counts.
            # ⚠️ `c.start()` points at the `.` (the separator is in the regex); the name starts at
            # `c.start(1)`. Using the former made `Logfy.create(` escape the filter, and `create`
            # became an "invented method" in every plan that logs.
            before = clean[max(0, c.start(1) - 40):c.start(1)]
            if re.search(r"\b([A-Z][A-Za-z0-9_]*)\s*\.\s*$", before):
                continue
            # ⚠️ Receiver rule, calibrated on 13 real plans (18/09): 42 findings -> 19, zero true
            # ones lost. A method on an object the plan does not create is a library's, not a
            # promise of the plan.
            receiver = receiver_of(clean, c.start(1))
            if receiver is not None and receiver not in ALWAYS_COUNTS and receiver not in ours:
                continue
            pos = base + c.start()
            if name not in invocations or pos < invocations[name]:
                invocations[name] = pos

    for name, use_pos in sorted(invocations.items(), key=lambda kv: kv[1]):
        esc = re.escape(name)
        # 1. does the PLAN create the symbol? Includes class, type, destructuring and parameter --
        #    `setCarregando` is born from `const [x, setCarregando] = useState()`, and a class
        #    instantiated with `new X()` is defined by `class X`.
        creation = re.search(
            rf"(export\s+)?(async\s+)?function\s+{esc}\b"
            rf"|(export\s+)?(abstract\s+)?class\s+{esc}\b"
            rf"|(export\s+)?(const|let|var)\s+{esc}\s*[:=]"
            rf"|(export\s+)?(type|interface)\s+{esc}\b"
            rf"|def\s+{esc}\b"
            rf"|\[[^\]]*\b{esc}\b[^\]]*\]\s*="          # destructuring
            rf"|\bconst\s+\{{[^}}]*\b{esc}\b[^}}]*\}}\s*="  # object destructuring
            rf"|^\s*(public|private|protected|static|async)?\s*{esc}\s*\("  # class method
            rf"|\([^)]*\b{esc}\s*[:,)]"                    # function parameter
            rf"|function\s+\w+\s*\([^)]*\b{esc}\b"         # same, classic form
            rf"|{esc}\s*(<[^>]*>)?\s*\([^)]*\)\s*(:[^{{]+)?\{{",
            text, re.M)
        if creation:
            t_creates, t_uses = task_of(tasks, creation.start()), task_of(tasks, use_pos)
            if t_creates and t_uses and t_creates > t_uses:
                findings.append(_t("preflight.symbol_order", name=name, created=t_creates, used=t_uses))
            continue
        # 2. does it exist in the repo?
        where = exists_in_repo(
            repo,
            rf"(function|def|fn)\s+{esc}\s*\("
            rf"|(public|private|protected|static)[^\n]{{0,60}}\s{esc}\s*\("
            rf"|\b{esc}\s*[:=]\s*(async\s+)?(function|\()")
        if not where:
            uses = exists_in_repo(repo, rf"\b{esc}\s*\(")
            if not uses:
                findings.append(_t("preflight.symbol_invented", name=name, task=task_of(tasks, use_pos)))
    return findings


def check_constants(text, tasks):
    """A constant the plan defines and never consumes is a decorative constraint."""
    findings = []
    defined = {}
    for m in re.finditer(r"^\s*(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\s*[:=]", text, re.M):
        defined.setdefault(m.group(1), m.start())
    for name, pos in defined.items():
        if len(name) < 5:
            continue
        # The definition counts as 1. Two occurrences = defined and used once, which is enough.
        # Only one = defined and never consumed.
        uses = len(re.findall(rf"\b{re.escape(name)}\b", text))
        if uses <= 1:
            findings.append(_t("preflight.constant_decorative", name=name, task=task_of(tasks, pos), uses=uses))
    return findings


# ⚠️ `?? 0` is not a defect by itself: `invoice?.id ?? 0` is RIGHT, because there zero means
# "no invoice" and that is exactly what the rule reads. The defect is turning absence into a plausible
# value where someone will DECIDE with that number -- money, quantity, comparison. Without this
# filter the first real run gave 9 findings, 7 of them legitimate in the code. A finding the owner
# discards is worse than no finding: it teaches ignoring the list.
# The terms are matched against the code line, so they stay in the language the code is written in
# (pt terms of the original team's code + their English equivalents).
DECISION_TERMS = re.compile(
    r"total|valor|preco|preço|price|amount|aliquota|alíquota|ipi|delta|quantidade|"
    r"qtd|saldo|custo|frete|desconto|comparar|compara",
    re.I)


def check_fail_open(text, tasks):
    findings = []
    for m in RE_BLOCK.finditer(text):
        base, block = m.start(2), m.group(2)
        clean = without_comments(block)
        for pattern, label_key in FAIL_OPEN_PATTERNS:
            label = _t(label_key)
            for h in re.finditer(pattern, clean):
                line_n = clean[:h.start()].count("\n") + 1
                lines = clean.splitlines()
                if line_n > len(lines):
                    continue
                snippet = lines[line_n - 1].strip()[:76]
                # An empty `catch` and status-by-substring are a defect in any context; the numeric
                # defaults only when someone decides with that value.
                # ⚠️ Decided by the label KEY (A-M1), never by the words of its translation: the INTERNAL
                # tested the label text, which a translator could change without knowing it is a decision.
                always = label_key in ALWAYS_FLAGGED
                if not always and not DECISION_TERMS.search(snippet):
                    continue
                findings.append(_t("preflight.fail_open_finding", task=task_of(tasks, base + h.start()),
                                   label=label, snippet=snippet))
    return findings


def check_cited_files(text, repo):
    """`Modify: path` has to exist. `Create:` has to NOT exist.

    ⚠️ A plan that touches two repos (API + GUI) cites a path relative to the OTHER repo. Resolving
    only against `repo` would give a false positive on every GUI file. It also tries the sibling
    repos in the parent directory.
    """
    findings = []
    siblings = [repo]
    parent = os.path.dirname(repo)
    if os.path.isdir(parent):
        siblings += [os.path.join(parent, d) for d in os.listdir(parent)
                     if os.path.isdir(os.path.join(parent, d, ".git"))]

    def finds(target):
        for base in siblings:
            if os.path.isfile(os.path.join(base, target)):
                return True
            # `web-gui/src/x.tsx` cited from the parent
            if os.path.isfile(os.path.join(os.path.dirname(base), target)):
                return True
        # ⚠️ A module plan cites a path relative TO THE MODULE (`Model/Publisher.php`), not to the
        # repo root. Run on an already-approved module plan, the exact-path ceiling gave 30
        # false positives. It falls to a suffix search before declaring absence: "X does not exist"
        # is a claim about the world and has a burden of proof (rule 18).
        for path in search.list_files(repo, glob=f"**/{os.path.basename(target)}",
                                      exclude=EXCLUDE):
            if path.endswith(target):
                return True
        return False

    for m in re.finditer(r"^-\s*(Modify|Create|Test):\s*`([^`]+)`", text, re.M):
        action, target = m.group(1), m.group(2).split(":")[0].strip()
        if target.startswith("@") or " " in target:
            continue
        exists = finds(target)
        if action == "Modify" and not exists:
            findings.append(_t("preflight.modify_missing", target=target))
        # ⚠️ There is NO check of `Create:` on a file that already exists. Run on an
        # already-approved module plan, it gave 11 false positives -- and by construction: that plan HAS
        # ALREADY BEEN EXECUTED, so the files it ordered created exist. The check does not
        # distinguish "wrong plan" from "already implemented plan", and a check that does not
        # distinguish trains the owner to ignore the whole list.
    return findings


def check_commits(text, tasks):
    """A file the plan creates has to enter some `git add`.

    Found by an adversarial review on round 4: the reconciliation files were created and no commit command added
    them. Whoever executes the plan follows the steps to the letter and commits without them -- and
    the defect only shows at deploy, as a missing file.
    """
    findings = []
    # `git add a.ts b.ts \` + newline: the `\` is consumed by [^\n]+, so the continuation has to
    # enter the same alternation, not after it.
    # ⚠️ `\\\n` comes BEFORE `[^\n]` in the alternation: the regex tries left to right, and `[^\n]`
    # would match the backslash alone, killing the line continuation.
    adds = " ".join(re.findall(r"git add ((?:\\\n|[^\n])+)", text))
    for m in re.finditer(r"^-\s*(Create|Test):\s*`([^`]+)`", text, re.M):
        target = m.group(2).split(":")[0].strip()
        if target.startswith("@") or " " in target:
            continue
        # it is enough for the path to appear in some `git add` of the plan
        if target not in adds and os.path.basename(target) not in adds:
            findings.append(_t("preflight.not_committed", target=target, task=task_of(tasks, m.start())))
    return findings


def check_verification_coverage(text, tasks):
    """The task's verification command has to cover the files it creates.

    Found by an adversarial review on round 5: the task started creating `ReconciliarPedidoUseCase.ts`, and the
    conference step stayed `tsc --noEmit | grep "GerarRascunhos"`. The filter excludes exactly the
    new file -- the step goes green without having looked at it. It is the acceptance gate whose
    comparator does not reach what changed.
    """
    findings = []
    for num, title, start, end in tasks:
        body = text[start:end]
        created = [m.group(2).split(":")[0].strip()
                   for m in re.finditer(r"^-\s*(Create|Test):\s*`([^`]+)`", body, re.M)]
        created = [c for c in created if not c.startswith("@") and " " not in c]
        if not created:
            continue
        # ⚠️ `tsc` and `vitest` prove DIFFERENT things: vitest runs on esbuild, which strips types
        # without checking them. If the task has a `tsc` step, its filter has to reach the new file
        # -- test coverage does not replace the type-check.
        has_tsc = "tsc --noEmit" in body
        filters = " ".join(
            re.findall(r"tsc --noEmit[^\n]*grep\s+(?:-\w+\s+)*[\"']([^\"']+)[\"']", body))
        vitest_targets = " ".join(re.findall(r"vitest run ([^\n`]+)", body))
        if not has_tsc and not vitest_targets:
            continue
        # ⚠️ The comparison is "some term of the filter appears in the PATH", not "the basename is
        # inside the filter". The filter `ListarCandidatos` covers `ListarCandidatosUseCase.ts`, and
        # `routes/orders` covers `src/routes/orders/index.ts` -- both would slip through with the inverted
        # comparison.
        terms = [t.strip() for t in re.split(r"[|\s]+", filters) if len(t.strip()) >= 4]
        for file in created:
            if file.endswith((".test.ts", ".test.tsx", ".spec.ts")):
                continue  # a test file does not need to enter the tsc filter
            if has_tsc and not any(t in file for t in terms):
                findings.append(_t("preflight.coverage_tsc", task=num, file=file, filters=filters[:50]))
                continue
            if any(t in file for t in terms):
                continue
            # `vitest run X.test.ts` exercises and compiles the `X.ts` it imports: compare by the
            # STEM, without `.test` and without extension.
            stem = re.sub(r"(\.test)?\.[jt]sx?$", "", file)
            tested_stems = {re.sub(r"(\.test)?\.[jt]sx?$", "", t)
                            for t in re.split(r"\s+", vitest_targets) if t.strip()}
            if any(stem == ts or stem.startswith(ts.rstrip("/") + "/")
                   for ts in tested_stems):
                continue
            findings.append(_t("preflight.coverage_none", task=num, file=file,
                               filters=(filters or vitest_targets)[:60]))
    return findings


def check_spec_promises(plan_text, spec):
    """Advisory: bold/⚠️ statements of the spec with no echo in the plan."""
    if not spec:
        return []
    with open(spec, encoding="utf-8", errors="replace") as fh:
        spec_text = fh.read()
    plan_lower = plan_text.lower()
    findings = []
    for m in re.finditer(r"(?:⚠️\s*)?\*\*([^*\n]{25,140})\*\*", spec_text):
        phrase = m.group(1)
        terms = [t.lower() for t in re.findall(r"`([A-Za-z_][\w./-]{3,})`", phrase)]
        terms += [w.lower() for w in re.findall(r"\b([a-zA-Z]{7,})\b", phrase)]
        if not terms:
            continue
        if not any(t in plan_lower for t in set(terms)):
            findings.append(_t("preflight.no_echo", phrase=phrase[:110]))
    return findings


def preflight(plan, repo, incremental=False):
    """incremental=True omits the injection of files.

    Measured on 25/08/2026: on round 1 the reviewer touched source files in 10 of 10 calls (the
    same `.sh` in 9 of them); on the incremental round, ZERO -- it works from the diff and the open
    blocks, and only reads plan/spec. Injecting source there would be cost without savings. The
    symbol tables stay in both: the incremental one starts precisely with a grep for the symbols
    of the open blocks.
    """
    # ⚠️ There is no longer "no search" -- there is slower search. The `if not shutil.which("rg")`
    # guard used to live here and skipped the WHOLE symbol table, which is the path where the
    # preflight said "could not check" and the gate opened. The `git` engine covers the machine
    # without ripgrep, the Phase 3 container included.

    _use_repo(repo)
    with open(plan, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    methods, constants = extract(text)

    lines = [
        "",
        _t("preflight.p_title"),
        "",
        # ⚠️ Declare the engine that RAN, not a presumed one. This text goes into the reviewer's
        # prompt, and saying "ripgrep" when it ran through `git` is lying to whoever will review --
        # the same family as declaring the requested model: declare what is known.
        _t("preflight.p_searched", repo_name=os.path.basename(repo),
           engine=_t("preflight.p_engine_rg") if search.engine() == "rg" else _t("preflight.p_engine_git"),
           excluded="/`, `".join(EXCLUDE[:6])),
        "",
    ]

    if methods:
        defined, nonexistent, from_outside = [], [], []
        batch = check_in_batch(repo, methods.keys())
        for name, freq in methods.most_common():
            where, uses = batch.get(name, ("", 0))
            if where:
                defined.append((name, where, uses, freq))
            elif uses > 0:
                from_outside.append(name)          # framework/vendor: used, defined outside
            else:
                nonexistent.append((name, freq))

        lines += [_t("preflight.p_check1_title"), ""]

        # The bucket that matters comes first.
        # Inline lists, not tables: with 200+ symbols the `|...|` of each line costs more than the
        # data and buries the signal.
        if nonexistent:
            where_in_plan = plan_lines(text, [n for n, _ in nonexistent])

            def _ref(n):
                ls = where_in_plan.get(n, [])
                if not ls:
                    return f"`{n}()`"
                cut = ",".join(str(x) for x in ls[:3])
                return _t("preflight.plan_ref", name=n, cut=cut) + ("+" if len(ls) > 3 else "")

            lines += [
                _t("preflight.p_missing", n=len(nonexistent)),
                "",
                "; ".join(_ref(n) for n, _ in nonexistent),
                "",
            ]

        if defined:
            lines += [
                _t("preflight.p_defined", n=len(defined)),
                "",
                "; ".join(f"`{n}()` {where}" for n, where, _, _ in defined),
                "",
            ]

        if from_outside:
            lines += [
                _t("preflight.p_outside") + ", ".join(f"`{n}()`" for n in sorted(from_outside)),
                "",
            ]

    if constants:
        lines += [
            _t("preflight.p_constants_title"),
            "",
            _t("preflight.p_constants_header"),
            "|---|---|---|---|",
        ]
        for name, freq in constants.most_common(SYMBOL_CAP):
            esc = re.escape(name)
            definition_pattern = rf"(const|define\s*\(\s*['\"]|final\s+\w+\s+)?\b{esc}\b\s*(=|:|,|\))"
            where = _definition(repo, definition_pattern)
            uses = _count(repo, rf"\b{esc}\b")
            if not where:
                where = _t("preflight.p_does_not_exist") if uses == 0 else _t("preflight.p_defined_outside")
            lines.append(f"| `{name}` | {where} | {uses} | {freq} |")
        lines.append("")

    orphans_block, _n_orphans = orphan_fields(text)
    if orphans_block:
        lines += [orphans_block]

    if not incremental:
        files_block, _ = cited_files(text, repo)
        if files_block:
            lines += [files_block]

    lines += [
        _t("preflight.p_how_title"),
        "",
        _t("preflight.p_how_1"),
        "",
        _t("preflight.p_how_2"),
        "",
        _t("preflight.p_how_3"),
        "",
        _t("preflight.p_ran_incremental" if incremental else "preflight.p_ran_full"),
        _t("preflight.p_how_4"),
        "",
    ]
    return "\n".join(lines)


def json_report(plan, repo):
    """Check mode: {"status": "pass"|"fail", "findings": [{"group", "state", "count", "items"}]}.

    Lists EVERY group (stable English ids; a `reason` is added when the group has one).
    ⚠️ `status` is "pass" only if no group is in findings/not_evaluated AND at least one is `ok`:
    a plan with nothing the checks could examine (all not_applicable) is VACUOUS, and a vacuous
    pass is the gate opening on nothing -- it fails with a `nothing-to-check` entry.
    """
    _use_repo(repo)
    try:
        text = read_plan(plan)
    except CouldNotCheck as e:
        results = {key: Result("not_evaluated", str(e)) for key, _ in GROUPS}
    else:
        results = run_groups(text, repo, tasks_of_plan(text))
    entries = []
    for key, gid in GROUPS:
        r = results[key]
        entry = {"group": gid, "state": r.state, "count": len(r.findings), "items": list(r.findings)}
        if r.reason:
            entry["reason"] = r.reason
        entries.append(entry)
    states = [e["state"] for e in entries]
    blocked = any(s in ("findings", "not_evaluated") for s in states)
    vacuous = not blocked and "ok" not in states
    if vacuous:
        entries.append({"group": "nothing-to-check", "state": "not_evaluated", "count": 0, "items": [],
                        "reason": _t("preflight.nothing_to_check")})
    return {"status": "fail" if blocked or vacuous else "pass", "findings": entries}


def _json_exit_code(report):
    """Same scheme as the human CLI: 2 = something did not run, 1 = finding (or vacuous), 0 = pass."""
    real = [e for e in report["findings"] if e["group"] != "nothing-to-check"]
    if any(e["state"] == "not_evaluated" for e in real):
        return 2
    return 0 if report["status"] == "pass" else 1


def main(argv=None):
    p = argparse.ArgumentParser(
        description=_t("preflight.cli_description"),
        epilog=_t("preflight.cli_epilog"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("plan")
    p.add_argument("--spec", default=None)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--human", action="store_true", help=_t("preflight.cli_human"))
    mode.add_argument("--prompt", action="store_true", help=_t("preflight.cli_prompt"))
    mode.add_argument("--gate", action="store_true", help=_t("preflight.cli_gate"))
    mode.add_argument("--json", action="store_true", help=_t("preflight.cli_json"))
    p.add_argument("--promises", action="store_true", help=_t("preflight.cli_promises"))
    p.add_argument("--incremental", action="store_true", help=_t("preflight.cli_incremental"))
    a = p.parse_args(argv)

    plan = os.path.abspath(a.plan)
    if not os.path.isfile(plan):
        sys.exit(_t("preflight.plan_not_found", plan=plan))
    repo = repo_of(plan)
    if not repo:
        sys.exit(_t("preflight.not_in_repo"))
    _use_repo(repo)

    if a.prompt:
        print(preflight(plan, repo, incremental=a.incremental))
        return 0

    if a.json:
        report = json_report(plan, repo)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return _json_exit_code(report)

    text, findings, not_evaluated = human_report(plan, repo)
    if not a.gate:
        print(text)
        if a.promises:
            # ⚠️ Advisory: runs ONLY here, outside human_report, and never enters the exit code.
            # Heuristic by term overlap, it gave 6 findings -- all noise -- on the first real run.
            print("\n" + _t("preflight.promises_head"))
            try:
                for x in check_spec_promises(read_plan(plan), a.spec):
                    print(f"        - {x}")
            except CouldNotCheck as e:
                print("        " + _t("preflight.promises_not_run", error=e))
    if not_evaluated:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
