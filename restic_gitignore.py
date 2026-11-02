#!/usr/bin/env python3
"""restic-gitignore: back up a directory tree with restic while honouring .gitignore.

Walks a tree, applies .gitignore rules the way git does (nested files, `!` negation,
trailing `/`, leading `/`, `**`, character classes, escapes, comments, trailing
spaces), and writes either

  * a file list for `restic backup --files-from-verbatim` (default), or a
    NUL-separated list for `--files-from-raw` (`-0`), or
  * an exclude file for `restic backup --exclude-file` (`--format exclude`).

SAFETY: .gitignore often lists files you *do* want in a backup (.env, keys, local
databases). So by default only ignored build/vendor output (the build-dirs preset) is
dropped and everything else is kept; `--all-ignored` drops everything git ignores. A
built-in keep list (plus `--keep`) is never dropped, even inside ignored directories.
Outside a git repository no .gitignore applies unless `--outside-repo`. Tracked files
are never dropped, and a repository counts only if git confirms it (needs `git` on PATH).
The rules the tool must never break are in INVARIANTS.md.

Python 3.8+, standard library only. MIT licence. Built by AI agents (thousand-ships).
"""
import argparse
import bisect
import os
import shutil
import stat
import subprocess
import sys

__version__ = "0.3.0"

# ---------------------------------------------------------------------------
# wildmatch: a direct port of git's wildmatch.c (bytes in, bytes out)
# ---------------------------------------------------------------------------
WM_MATCH, WM_NOMATCH, WM_ABORT_ALL, WM_ABORT_TO_STARSTAR = 0, 1, -1, -2
_SLASH, _STAR, _QMARK, _LBRACK, _RBRACK, _BSLASH = 47, 42, 63, 91, 93, 92
_GLOB_SPECIAL = frozenset(b"*?[\\")
_PUNCT = frozenset(b"!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def _cclass(name, c):
    """POSIX class test with git's ASCII-only ctype. None = unknown class."""
    if name == b"alnum":
        return (48 <= c <= 57) or (65 <= c <= 90) or (97 <= c <= 122)
    if name == b"alpha":
        return (65 <= c <= 90) or (97 <= c <= 122)
    if name == b"blank":
        return c in (32, 9)
    if name == b"cntrl":
        return c < 32 or c == 127
    if name == b"digit":
        return 48 <= c <= 57
    if name == b"graph":
        return 33 <= c <= 126
    if name == b"lower":
        return 97 <= c <= 122
    if name == b"print":
        return 32 <= c <= 126
    if name == b"punct":
        return c in _PUNCT
    if name == b"space":
        return c in (32, 9, 10, 13)
    if name == b"upper":
        return 65 <= c <= 90
    if name == b"xdigit":
        return (48 <= c <= 57) or (65 <= c <= 70) or (97 <= c <= 102)
    return None


def _dowild(p, pi, t, ti, pathname):
    plen, tlen = len(p), len(t)
    while pi < plen:
        p_ch = p[pi]
        if ti >= tlen and p_ch != _STAR:
            return WM_ABORT_ALL
        t_ch = t[ti] if ti < tlen else 0
        if p_ch == _BSLASH:
            pi += 1
            p_ch = p[pi] if pi < plen else 0
            if t_ch != p_ch:
                return WM_NOMATCH
        elif p_ch == _QMARK:
            if pathname and t_ch == _SLASH:
                return WM_NOMATCH
        elif p_ch == _STAR:
            pi += 1
            if pi < plen and p[pi] == _STAR:
                prev = pi - 2
                pi += 1
                while pi < plen and p[pi] == _STAR:
                    pi += 1
                nxt = p[pi] if pi < plen else 0
                if (prev < 0 or p[prev] == _SLASH) and (
                        nxt == 0 or nxt == _SLASH or
                        (nxt == _BSLASH and pi + 1 < plen and p[pi + 1] == _SLASH)):
                    if nxt == _SLASH and _dowild(p, pi + 1, t, ti, pathname) == WM_MATCH:
                        return WM_MATCH
                    match_slash = True
                else:
                    match_slash = False
            else:
                match_slash = not pathname
            if pi >= plen:
                if not match_slash and t.find(b"/", ti) >= 0:
                    return WM_NOMATCH
                return WM_MATCH
            if not match_slash and p[pi] == _SLASH:
                slash = t.find(b"/", ti)
                if slash < 0:
                    return WM_NOMATCH
                ti = slash + 1
                pi += 1
                continue
            while True:
                if t_ch == 0:
                    break
                if p[pi] not in _GLOB_SPECIAL:
                    pc = p[pi]
                    while ti < tlen and (match_slash or t[ti] != _SLASH):
                        if t[ti] == pc:
                            break
                        ti += 1
                    t_ch = t[ti] if ti < tlen else 0
                    if t_ch != pc:
                        return WM_NOMATCH
                m = _dowild(p, pi, t, ti, pathname)
                if m != WM_NOMATCH:
                    if not match_slash or m != WM_ABORT_TO_STARSTAR:
                        return m
                elif not match_slash and t_ch == _SLASH:
                    return WM_ABORT_TO_STARSTAR
                ti += 1
                t_ch = t[ti] if ti < tlen else 0
            return WM_ABORT_ALL
        elif p_ch == _LBRACK:
            pi += 1
            p_ch = p[pi] if pi < plen else 0
            if p_ch == 94:  # '^' is an alias for '!'
                p_ch = 33
            negated = p_ch == 33
            if negated:
                pi += 1
                p_ch = p[pi] if pi < plen else 0
            prev_ch = 0
            matched = False
            while True:
                if p_ch == 0:
                    return WM_ABORT_ALL
                if p_ch == _BSLASH:
                    pi += 1
                    p_ch = p[pi] if pi < plen else 0
                    if p_ch == 0:
                        return WM_ABORT_ALL
                    if t_ch == p_ch:
                        matched = True
                elif p_ch == 45 and prev_ch and pi + 1 < plen and p[pi + 1] != _RBRACK:
                    pi += 1
                    p_ch = p[pi]
                    if p_ch == _BSLASH:
                        pi += 1
                        p_ch = p[pi] if pi < plen else 0
                        if p_ch == 0:
                            return WM_ABORT_ALL
                    if prev_ch <= t_ch <= p_ch:
                        matched = True
                    p_ch = 0
                elif p_ch == _LBRACK and pi + 1 < plen and p[pi + 1] == 58:
                    s = pi + 2
                    pi = s
                    while pi < plen and p[pi] != _RBRACK:
                        pi += 1
                    if pi >= plen:
                        return WM_ABORT_ALL
                    i = pi - s - 1
                    if i < 0 or p[pi - 1] != 58:
                        pi = s - 2
                        p_ch = _LBRACK
                        if t_ch == p_ch:
                            matched = True
                    else:
                        r = _cclass(p[s:s + i], t_ch)
                        if r is None:
                            return WM_ABORT_ALL
                        if r:
                            matched = True
                        p_ch = 0
                elif t_ch == p_ch:
                    matched = True
                prev_ch = p_ch
                pi += 1
                p_ch = p[pi] if pi < plen else 0
                if p_ch == _RBRACK:
                    break
            if matched == negated or (pathname and t_ch == _SLASH):
                return WM_NOMATCH
        else:
            if t_ch != p_ch:
                return WM_NOMATCH
        pi += 1
        ti += 1
    return WM_MATCH if ti >= tlen else WM_NOMATCH


def wildmatch(pattern, text, pathname=True):
    """True if `text` matches git glob `pattern` (both bytes)."""
    return _dowild(pattern, 0, text, 0, pathname) == WM_MATCH


# ---------------------------------------------------------------------------
# .gitignore parsing and matching (port of git dir.c)
# ---------------------------------------------------------------------------
def _simple_length(p):
    for i, c in enumerate(p):
        if c in _GLOB_SPECIAL:
            return i
    return len(p)


def _trim_trailing_spaces(buf):
    last_space = None
    i, n = 0, len(buf)
    while i < n:
        c = buf[i]
        if c == 32:
            if last_space is None:
                last_space = i
        elif c == _BSLASH:
            i += 1
            if i >= n:
                return buf
            last_space = None
        else:
            last_space = None
        i += 1
    return buf[:last_space] if last_space is not None else buf


class Pattern:
    __slots__ = ("pat", "negated", "mustbedir", "nodir", "prefix", "endswith",
                 "base", "source", "lineno", "raw")

    def __init__(self, raw, base=b"", source="", lineno=0):
        self.raw = raw
        p = raw
        self.negated = p[:1] == b"!"
        if self.negated:
            p = p[1:]
        self.mustbedir = p.endswith(b"/")
        full = p
        if self.mustbedir:
            p = p[:-1]
        self.nodir = b"/" not in p
        self.prefix = min(_simple_length(full), len(p))
        rest = full[1:]
        self.endswith = full[:1] == b"*" and _simple_length(rest) == len(rest)
        self.pat = p
        self.base = base          # directory of the ignore file, relative to the repo top
        self.source = source
        self.lineno = lineno

    def match(self, path, basename, is_dir):
        """path: relative to the repo top, '/'-separated bytes."""
        if self.mustbedir and not is_dir:
            return False
        pat = self.pat
        if self.nodir:
            if self.prefix == len(pat):
                return pat == basename
            if self.endswith:
                tail = pat[1:]
                return len(tail) <= len(basename) and basename.endswith(tail)
            return wildmatch(pat, basename, pathname=False)
        prefix = self.prefix
        if pat[:1] == b"/":
            pat = pat[1:]
            prefix -= 1
        base = self.base
        if base:
            if not path.startswith(base) or path[len(base):len(base) + 1] != b"/":
                return False
            name = path[len(base) + 1:]
        else:
            name = path
        if prefix > 0:
            if prefix > len(name) or name[:prefix] != pat[:prefix]:
                return False
            pat = pat[prefix:]
            name = name[prefix:]
            if not pat and not name:
                return True
        return wildmatch(pat, name, pathname=True)


def parse_ignore(data, base=b"", source=""):
    """Parse the bytes of a .gitignore-style file into a list of Patterns."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    out = []
    for lineno, line in enumerate(data.split(b"\n"), 1):
        if not line or line[:1] == b"#":
            continue
        if line.endswith(b"\r"):
            line = line[:-1]
        line = _trim_trailing_spaces(line)
        if not line:
            continue
        pat = Pattern(line, base, source, lineno)
        if not pat.pat:
            continue
        out.append(pat)
    return out


def last_match(plist, path, basename, is_dir):
    for pat in reversed(plist):
        if pat.match(path, basename, is_dir):
            return pat
    return None


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------
PRESETS = {
    # Build output, dependency and cache directories that can be recreated.
    "build-dirs": [
        "node_modules/", "bower_components/", "jspm_packages/", ".pnpm-store/", ".yarn/cache/",
        "target/", "dist/", "build/", "out/", "_build/", "cmake-build-*/", "CMakeFiles/",
        ".gradle/", ".m2/repository/", "DerivedData/", "Pods/", "vendor/", ".bundle/",
        "__pycache__/", "*.pyc", "*.pyo", ".venv/", "venv/", ".tox/", ".nox/", ".eggs/",
        "*.egg-info/", ".mypy_cache/", ".pytest_cache/", ".ruff_cache/", ".hypothesis/",
        "htmlcov/", "coverage/", ".nyc_output/",
        ".next/", ".nuxt/", ".svelte-kit/", ".angular/", ".parcel-cache/", ".turbo/",
        ".vite/", ".cache/", ".terraform/", ".stack-work/", "dist-newstyle/", "elm-stuff/",
        ".dart_tool/", "zig-cache/", ".zig-cache/", "zig-out/",
        "*.o", "*.obj", "*.a", "*.lib", "*.so", "*.dylib", "*.dll", "*.class",
        "*.exe",
    ],
}
DEFAULT_PRESET = "build-dirs"

# Files .gitignore commonly lists that a backup should still contain. Matched
# case-insensitively, at any depth, also inside ignored directories (see INVARIANTS.md).
DEFAULT_KEEP = [
    ".env", ".env.*", "*.env", ".envrc", ".netrc", ".npmrc", ".pypirc", ".pgpass", ".my.cnf",
    ".htpasswd", "*.pem", "*.key", "*.crt", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx",
    "*.gpg", "*.asc", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".ssh/", ".gnupg/",
    ".aws/", "**/.kube/config", "**/.docker/config.json", "token.json", "wallet.dat",
    "*secret*", "*credential*", "*.tfvars", "*.tfvars.json", "*.tfstate", "*.tfstate.backup",
    "*.sqlite", "*.sqlite3", "*.db",
]
DEFAULT_KEEP_SOURCE = "default-keep"


# ---------------------------------------------------------------------------
# walking
# ---------------------------------------------------------------------------
def _ci(p):
    """Lower-cased copy of a keep pattern (keep rules match case-insensitively)."""
    return Pattern(p.raw.lower(), p.base, p.source, p.lineno)


def default_keep_patterns():
    return [Pattern(os.fsencode(k), source=DEFAULT_KEEP_SOURCE) for k in DEFAULT_KEEP]


class Options:
    def __init__(self, keep=(), only=None, info_exclude=False, global_ignore=None,
                 parent_ignores=True, include_git_dir=False, empty_dirs=False,
                 warn=None, outside_repo=False, junk=None):
        # keep: list[Pattern] (base b"", relative to the scan root). Patterns whose source is
        # DEFAULT_KEEP_SOURCE are the built-in list; any other source counts as explicit.
        self.keep = [_ci(p) for p in keep]
        self.keep_explicit = [p for p in self.keep if p.source != DEFAULT_KEEP_SOURCE]
        self.only = only            # list[Pattern] or None (None: drop everything git ignores)
        # junk: regenerable build output. Inside it only explicit keep patterns are searched
        # for. Always the build-dirs preset unless given explicitly: it is NOT derived from
        # `only` (--only-patterns 'logs/' must not silence the built-in keep list in logs/).
        if junk is None:
            junk = [Pattern(os.fsencode(x)) for x in PRESETS[DEFAULT_PRESET]]
        self.junk = junk
        self.info_exclude = info_exclude
        self.global_ignore = global_ignore or []   # bytes of the global ignore file
        self.parent_ignores = parent_ignores
        self.include_git_dir = include_git_dir
        self.empty_dirs = empty_dirs
        self.outside_repo = outside_repo   # apply .gitignore files outside any git repo
        self.warn = warn or (lambda msg: sys.stderr.write("restic-gitignore: warning: %s\n" % msg))


class Result:
    def __init__(self):
        self.kept = []           # relative paths (bytes) of entries to back up
        self.dropped = []        # (relative path, is_dir) of pruned entries
        self.rescued = []        # relative paths kept by keep rules although they'd be dropped
        self.tracked = []        # relative paths kept because git tracks them (ignore rules
                                 # do not apply to tracked files)
        self.unapplied = []      # .gitignore files skipped because they are outside any repo
        self.warnings = 0


def _read_ignore_file(path, base, warn, label):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError as e:
        warn("cannot stat %s: %s" % (os.fsdecode(path), e.strerror))
        return []
    if stat.S_ISLNK(st.st_mode):
        warn("not following symlinked %s (git does not either): %s" % (label, os.fsdecode(path)))
        return []
    if not stat.S_ISREG(st.st_mode):
        return []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        warn("cannot read %s, treating it as empty: %s" % (os.fsdecode(path), e.strerror))
        return []
    pats = parse_ignore(data, base, os.fsdecode(path))
    for p in pats:
        if not p.negated and p.pat in (b"*", b"/*", b"**", b"/**"):
            warn("%s:%d ignores everything (%r); with --all-ignored that drops the whole tree "
                 "below it except keep-rule matches"
                 % (os.fsdecode(path), p.lineno, os.fsdecode(p.pat)))
    return pats


def _join(a, b):
    return a + b"/" + b if a else b


def _hit(plist, rel, name, is_dir):
    m = last_match(plist, rel, name, is_dir) if plist else None
    return m is not None and not m.negated


def _keep_hit(plist, rel, name, is_dir):
    return bool(plist) and _hit(plist, rel.lower(), name.lower(), is_dir)


class Frame:
    """Ignore state for one git 'world' (a repository, or the scan root if no repo).
    An inactive frame (outside any repository, no --outside-repo) ignores nothing."""
    __slots__ = ("low", "active", "tracked")

    def __init__(self, low, active=True, tracked=None):
        self.low = low         # lower-precedence lists: [info/exclude, global] (checked in order)
        self.active = active
        self.tracked = tracked or Tracked()   # index entries, relative to the repo top

    def ignored(self, lists, rel, name, is_dir):
        """lists: tuple of per-directory pattern lists, shallow -> deep."""
        if not self.active:
            return False
        for plist in reversed(lists):
            m = last_match(plist, rel, name, is_dir)
            if m is not None:
                return not m.negated
        for plist in self.low:
            m = last_match(plist, rel, name, is_dir)
            if m is not None:
                return not m.negated
        return False


def _git_env():
    # Drop GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE ... so git answers about the path we ask
    # about, not about whatever repository the caller's environment points at.
    keep = ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in keep}


def _git(path, *args):
    """Run git in `path`. Returns (stdout bytes or None on failure, stderr text)."""
    try:
        p = subprocess.run(["git", "-C", path] + list(args), env=_git_env(),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    except OSError as e:
        return None, str(e)
    err = p.stderr.decode("utf-8", "replace").strip().splitlines()
    return (p.stdout if p.returncode == 0 else None), (err[0] if err else "")


def git_available():
    return shutil.which("git") is not None


def _is_git_directory(d):
    """git's is_git_directory(): HEAD, plus objects/ and refs/ (via commondir if any)."""
    if not os.path.lexists(os.path.join(d, b"HEAD")):
        return False
    common = d
    try:
        with open(os.path.join(d, b"commondir"), "rb") as f:
            c = f.read().strip()
        if c:
            common = os.path.join(d, c)
    except OSError:
        pass
    return (os.path.isdir(os.path.join(common, b"objects")) and
            os.path.isdir(os.path.join(common, b"refs")))


def _has_dotgit(path):
    return os.path.lexists(os.path.join(path, b".git"))


def _looks_like_repo(path):
    """Cheap structural check of path/.git (a git directory, or a 'gitdir:' file pointing
    to one). Necessary but not sufficient: git must still confirm (see _repo_top)."""
    dg = os.path.join(path, b".git")
    if os.path.isdir(dg):
        return _is_git_directory(dg)
    try:
        with open(dg, "rb") as f:
            data = f.read(4096)
    except OSError:
        return False
    if not data.startswith(b"gitdir:"):
        return False
    target = data[7:].strip()
    if not target:
        return False
    return _is_git_directory(os.path.join(path, target))


def _repo_top(path):
    """Real path of the work tree that git says `path` belongs to, or (None, reason)."""
    out, err = _git(path, "rev-parse", "--show-toplevel")
    if out is None:
        return None, err or "git rev-parse failed"
    top = out.rstrip(b"\n")
    if not top:
        return None, "not inside a work tree"
    return os.path.realpath(top), ""


def _tracked(path, full_name):
    """Set of paths git tracks (index entries) below `path`, '/'-separated bytes, relative to
    the repository top (full_name) or to `path`. None if git cannot read the index."""
    args = ["ls-files", "-z", "--cached"] + (["--full-name"] if full_name else [])
    out, err = _git(path, *args)
    if out is None:
        return None, err
    return [x for x in out.split(b"\0") if x], ""


class Tracked:
    """Index entries of one repository, with their ancestor directories."""
    __slots__ = ("files", "dirs", "sorted")

    def __init__(self, paths=()):
        self.files = set(paths)
        self.dirs = set()
        for p in self.files:
            i = p.rfind(b"/")
            while i > 0:
                d = p[:i]
                if d in self.dirs:
                    break
                self.dirs.add(d)
                i = d.rfind(b"/")
        self.sorted = sorted(self.files)

    def below(self, d):
        """Tracked paths strictly below directory d (relative to the repo top)."""
        if d not in self.dirs:
            return []
        pre = d + b"/"
        i = bisect.bisect_left(self.sorted, pre)
        out = []
        while i < len(self.sorted) and self.sorted[i].startswith(pre):
            out.append(self.sorted[i])
            i += 1
        return out


def _low_lists(top, opts):
    low = []
    if opts.info_exclude:
        low.append(_read_ignore_file(os.path.join(top, b".git", b"info", b"exclude"), b"",
                                     opts.warn, ".git/info/exclude"))
    if opts.global_ignore:
        low.append(parse_ignore(opts.global_ignore, b"", "global ignore file"))
    return low


# Junk directories that are package trees: they routinely ship *.pem, *secret*.js and test
# databases, so built-in keep matches inside them are not worth a warning (M4).
_PACKAGE_TREES = frozenset([
    b"node_modules", b"bower_components", b"jspm_packages", b".pnpm-store", b".venv", b"venv",
    b"target", b"vendor", b".tox", b".nox", b".eggs", b"Pods", b".gradle", b".bundle",
    b"site-packages", b"__pycache__", b".m2", b".yarn", b"dist-newstyle", b".stack-work",
    b"elm-stuff", b".dart_tool"])


def scan(root, opts=None):
    """Walk `root` (str or bytes) and return a Result with paths relative to root.

    The walk is iterative (no recursion limit on depth). Output order is a depth-first
    pre-order with entries sorted by name."""
    opts = opts or Options()
    res = Result()
    warn_inner = opts.warn
    warned = set()

    def warn(msg):
        if msg in warned:
            return
        warned.add(msg)
        res.warnings += 1
        warn_inner(msg)

    root_abs = os.path.abspath(os.fsencode(root))
    root_real = os.path.realpath(root_abs)
    have_git = git_available()
    if not have_git:
        warn("git not found: cannot confirm repositories or tracked files, so no .gitignore "
             "rules are applied%s" % (" except through --outside-repo (tracked files are NOT "
                                      "protected)" if opts.outside_repo else ""))

    def repo_frame(path, label_rel, full_name):
        """Frame for a repository rooted at (or containing) `path`, confirmed by git.
        Returns (frame or None, top real path or None). Deny-by-default: if git does not
        confirm the repository or cannot read its index, no rules are applied there."""
        if not have_git:
            return None, None
        top, why = _repo_top(path)
        if top is None:
            if _has_dotgit(path):
                warn("%s has a .git entry but git does not accept it as a repository (%s); "
                     "treating it as an ordinary directory" % (os.fsdecode(path), why))
            return None, None
        paths, why = _tracked(path, full_name)
        if paths is None:
            warn("cannot list tracked files of the repository at %s (%s); applying no "
                 "ignore rules there" % (os.fsdecode(top), why))
            return Frame([], False), top
        return Frame([], True, Tracked(paths)), top

    # Find the enclosing repository by asking git (as git does: an empty .git directory or a
    # dangling gitdir: file is not a repository). Outside any repository git applies no
    # ignore rules at all; so do we, unless --outside-repo.
    top, prefix = root_abs, b""
    frame, gtop = repo_frame(root_abs, b"", opts.parent_ignores)
    if frame is not None:
        if gtop != root_real and opts.parent_ignores:
            top = gtop
            prefix = os.path.relpath(root_real, gtop).replace(os.sep.encode(), b"/")
            if prefix.startswith(b".."):   # cannot happen with a sane git; be safe
                top, prefix = root_abs, b""
        if frame.active:
            frame.low = _low_lists(top, opts)
    else:
        frame = Frame(_low_lists(top, opts) if opts.outside_repo else [], opts.outside_repo)
    lists = []
    if prefix and frame.active:
        cur_rel, cur_abs = b"", top
        lists.append(_read_ignore_file(os.path.join(top, b".gitignore"), b"", warn, ".gitignore"))
        for part in prefix.split(b"/"):
            cur_rel = _join(cur_rel, part)
            cur_abs = os.path.join(cur_abs, part)
            if frame.ignored(tuple(lists), cur_rel, part, True):
                warn("%s is itself ignored by the enclosing repository's rules; "
                     "applying only the rules found inside it" % os.fsdecode(root_abs))
                lists = []
                break
            if cur_abs != root_real:
                lists.append(_read_ignore_file(os.path.join(cur_abs, b".gitignore"), cur_rel,
                                               warn, ".gitignore"))

    def listdir(abs_dir, srel):
        try:
            return sorted(os.scandir(abs_dir), key=lambda e: e.name)
        except OSError as e:
            # Deny-by-default (M1): we cannot see what is inside, so the directory itself is
            # listed for backup and never excluded; a restic run with more access backs it up.
            warn("cannot read directory %s: %s (listed whole, never excluded)"
                 % (os.fsdecode(abs_dir), e.strerror))
            if srel:
                res.kept.append(srel)
            return None

    def isdir(e):
        try:
            return e.is_dir(follow_symlinks=False)
        except OSError as err:
            warn("cannot stat %s: %s" % (os.fsdecode(e.path), err))
            return False

    # Stack of directories being listed. Kinds:
    # "n" normal: apply ignore rules; "w" whole: keep everything (a keep rule matched);
    # "p" partial: inside a dropped directory that contains keep matches or tracked files.
    stack = []

    def push_normal(abs_dir, srel, rel, lists, frame, anc_ign, anc_only, anc_junk):
        entries = listdir(abs_dir, srel)
        if entries is None:
            return
        if not entries and opts.empty_dirs and srel:
            res.kept.append(srel)
        if any(e.name == b".gitignore" for e in entries):
            if not frame.active:
                res.unapplied.append(_join(srel, b".gitignore"))
            elif not anc_ign:
                lists = lists + (_read_ignore_file(os.path.join(abs_dir, b".gitignore"), rel,
                                                   warn, ".gitignore"),)
        stack.append(("n", iter(entries), srel, rel, lists, frame, anc_ign, anc_only, anc_junk))

    def push_whole(abs_dir, srel):
        entries = listdir(abs_dir, srel)
        if entries is None:
            return
        if not entries and opts.empty_dirs:
            res.kept.append(srel)
        stack.append(("w", iter(entries), srel))

    def push_partial(abs_dir, srel, kset, aset, tset):
        entries = listdir(abs_dir, srel)
        if entries is not None:
            stack.append(("p", iter(entries), srel, kset, aset, tset))

    def git_entry(e, s):
        if not opts.include_git_dir:
            res.dropped.append((s, True))
        elif isdir(e):
            push_whole(e.path, s)
        else:
            res.kept.append(s)

    def find_keeps(abs_dir, srel, junk_ctx, pats_all, pats_junk):
        """Paths below a dropped directory that a keep rule matches, plus directories that
        cannot be read (deny-by-default, M1). Inside build/junk directories only `pats_junk`
        is searched for."""
        found = []
        todo = [(abs_dir, srel, junk_ctx)]
        while todo:
            a, s, j = todo.pop()
            try:
                entries = sorted(os.scandir(a), key=lambda e: e.name)
            except OSError as err:
                warn("cannot read directory %s: %s (listed whole, never excluded)"
                     % (os.fsdecode(a), err.strerror))
                found.append(s)
                continue
            pats = pats_junk if j else pats_all
            for e in entries:
                name = e.name
                if name == b".git":
                    continue
                p = _join(s, name)
                d = isdir(e)
                if _keep_hit(pats, p, name, d):
                    found.append(p)
                    continue
                if d:
                    jj = j or _hit(opts.junk, p, name, True)
                    if jj and not pats_junk:
                        continue
                    todo.append((e.path, p, jj))
        return found

    def drop_dir(abs_dir, s, r, frm, junk_ctx):
        if junk_ctx and not opts.keep_explicit or not opts.keep:
            found = []
            if junk_ctx and opts.keep and abs_dir.rsplit(b"/", 1)[-1] not in _PACKAGE_TREES:
                # M4: say so when the scoped junk exception drops built-in keep matches.
                skipped = find_keeps(abs_dir, s, False, opts.keep, [])
                skipped = [x for x in skipped if x != s]
                if skipped:
                    warn("%s is build output, so %d built-in keep-list match(es) inside it "
                         "are dropped (e.g. %s); pass --keep to keep them"
                         % (os.fsdecode(s), len(skipped),
                            os.fsdecode(skipped[0])))
        else:
            found = find_keeps(abs_dir, s, junk_ctx, opts.keep, opts.keep_explicit)
        # H1: git never ignores tracked files, wherever they sit.
        tracked = [s + t[len(r):] for t in frm.tracked.below(r)] if frm.active else []
        if not found and not tracked:
            res.dropped.append((s, True))
            return
        kset, tset, aset = set(found), set(tracked), {s}
        for k in found + tracked:
            p = k
            while True:
                p = p[:p.rfind(b"/")]
                if p in aset:
                    break
                aset.add(p)
        if s in kset:          # the directory itself could not be read: keep it listed
            res.kept.append(s)
            return
        push_partial(abs_dir, s, kset, aset, tset)

    push_normal(root_abs, b"", prefix, tuple(lists), frame, False, False, False)
    use_only = opts.only is not None
    while stack:
        top_item = stack[-1]
        e = next(top_item[1], None)
        if e is None:
            stack.pop()
            continue
        kind, name = top_item[0], e.name
        s = _join(top_item[2], name)
        if kind == "w":
            if name == b".git":
                git_entry(e, s)
            elif isdir(e):
                push_whole(e.path, s)
            else:
                res.kept.append(s)
            continue
        if kind == "p":
            kset, aset, tset = top_item[3], top_item[4], top_item[5]
            d = isdir(e)
            if s in kset:
                res.rescued.append(s)
                if d:
                    push_whole(e.path, s)
                else:
                    res.kept.append(s)
            elif s in tset:
                res.tracked.append(s)
                if d:      # a tracked gitlink (submodule): keep it whole
                    push_whole(e.path, s)
                else:
                    res.kept.append(s)
            elif d and s in aset:
                push_partial(e.path, s, kset, aset, tset)
            else:
                res.dropped.append((s, d))
            continue
        _, _, srel, rel, lists, frm, anc_ign, anc_only, anc_junk = top_item
        if name == b".git":
            git_entry(e, s)
            continue
        r = _join(rel, name)
        d = isdir(e)
        git_ign = anc_ign or frm.ignored(lists, r, name, d)
        tracked_self = git_ign and r in frm.tracked.files
        if tracked_self:
            git_ign = False      # H1: ignore rules do not apply to tracked paths
        only_hit = (anc_only or _hit(opts.only, s, name, d)) if use_only else True
        junk_hit = anc_junk or _hit(opts.junk, s, name, d)
        drop = git_ign and only_hit
        if (drop or d) and _keep_hit(opts.keep, s, name, d):
            if drop:
                res.rescued.append(s)
            if d:
                push_whole(e.path, s)   # a matching directory is kept whole
            else:
                res.kept.append(s)
            continue
        if drop:
            if d:
                drop_dir(e.path, s, r, frm, junk_hit)
            else:
                res.dropped.append((s, False))
            continue
        if not d:
            if tracked_self:
                res.tracked.append(s)
            res.kept.append(s)
            continue
        # A directory is "inside" a preset/junk match only when that ancestor was itself
        # ignored (M3): a tracked, non-ignored build/ or vendor/ is source, not output.
        inh_only = only_hit and use_only and git_ign
        inh_junk = junk_hit and git_ign
        nested = None
        if not git_ign and _has_dotgit(e.path):
            # Nested repository: its own ignore world, like git, but only if git agrees.
            nested, ntop = repo_frame(e.path, s, False)
            if nested is not None and ntop != os.path.realpath(e.path):
                nested = None          # git sees it as part of an enclosing repository
        if tracked_self:
            res.tracked.append(s)
        if nested is not None:
            if nested.active:
                nested.low = _low_lists(e.path, opts)
            push_normal(e.path, s, b"", (), nested, False, inh_only, inh_junk)
        else:
            push_normal(e.path, s, r, lists, frm, git_ign, inh_only, inh_junk)

    if res.unapplied:
        warn("%s is not inside a git repository, so its .gitignore files were NOT applied "
             "(git ignores them too): %s%s. Pass --outside-repo to apply them."
             % (os.fsdecode(root_abs),
                b", ".join(res.unapplied[:3]).decode("utf-8", "replace"),
                " and %d more" % (len(res.unapplied) - 3) if len(res.unapplied) > 3 else ""))
    return res


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def restic_escape(path):
    """Escape an absolute path as a literal restic exclude pattern, or None if it
    cannot be expressed (restic reads exclude files line by line)."""
    if b"\n" in path or b"\r" in path:
        return None
    out = bytearray()
    for i, c in enumerate(path):
        ch = bytes((c,))
        if ch in (b"*", b"?", b"[", b"\\"):
            out += b"\\" + ch
        elif ch == b"$":                       # restic expands $VARS in exclude files
            out += b"[$]"
        elif ch.isspace() and (i == len(path) - 1):
            out += b"[" + ch + b"]"            # restic trims whitespace at line ends
        else:
            out += ch
    return bytes(out)


_CONTROL = frozenset(range(32)) | {127}


def has_control(path):
    return any(c in _CONTROL for c in path)


def build_parser():
    ap = argparse.ArgumentParser(
        prog="restic-gitignore",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="List what to back up from a tree, honouring .gitignore like git does.",
        epilog="""default behaviour (since 0.2.0):
  Only paths that .gitignore ignores AND that are regenerable build output
  (node_modules/, target/, dist/, .venv/, *.o ...; see --show-default-keep) are
  dropped. Everything else is backed up, including ignored .env files, notes and
  local databases. Paths matching the keep list or --keep are never dropped, even
  inside ignored directories. Outside a git repository no .gitignore applies
  (as in git) unless --outside-repo. Files git tracks are never dropped, and rules
  apply only in repositories git itself confirms (git must be on PATH). .git
  directories are skipped. Symlinks are listed, never followed. Unreadable
  directories are listed whole, never excluded.

broader: --all-ignored                      drop everything git ignores, except keep-list matches
exact:   --all-ignored --no-default-keep    drop exactly what git ignores, secrets included

examples:
  restic-gitignore ~/code > list.txt && restic backup --files-from-verbatim list.txt
  restic-gitignore -0 ~/code > list.bin && restic backup --files-from-raw list.bin
  restic-gitignore --format exclude ~/code > excl.txt && restic backup ~/code --exclude-file excl.txt
""")
    ap.add_argument("roots", nargs="*", metavar="DIR", help="directories to scan (default .)")
    ap.add_argument("--format", choices=["files", "exclude", "dropped"], default="files",
                    help="files: list for --files-from-verbatim (default); exclude: "
                         "--exclude-file of dropped paths; dropped: review what is left out")
    ap.add_argument("-0", "--null", action="store_true",
                    help="NUL-separate the file list (for restic --files-from-raw)")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    ap.add_argument("--relative", action="store_true",
                    help="print paths relative to DIR instead of absolute (files/dropped)")
    ap.add_argument("--all-ignored", action="store_true",
                    help="drop everything git ignores (minus keep rules), not only build output")
    ap.add_argument("--preset", action="append", choices=sorted(PRESETS),
                    help="only drop ignored paths that also match this preset "
                         "(build-dirs is the default)")
    ap.add_argument("--only-patterns", metavar="LIST",
                    help="comma-separated gitignore-style patterns; only ignored paths "
                         "matching one of them are dropped (e.g. 'node_modules/,target/,*.o')")
    ap.add_argument("--keep", action="append", default=[], metavar="GLOBS",
                    help="comma-separated gitignore-style patterns always backed up, even if "
                         "ignored or inside an ignored directory; a matching directory is kept "
                         "whole; case-insensitive; repeatable")
    ap.add_argument("--no-default-keep", action="store_true",
                    help="do not apply the built-in keep list")
    ap.add_argument("--show-default-keep", action="store_true",
                    help="print the built-in keep list and presets, then exit")
    ap.add_argument("--outside-repo", action="store_true",
                    help="apply .gitignore files even when DIR is not inside a git repository "
                         "(git itself ignores them there)")
    ap.add_argument("--info-exclude", action="store_true",
                    help="also apply each repository's .git/info/exclude")
    ap.add_argument("--global-ignore", metavar="FILE",
                    help="also apply this file at lowest precedence (like core.excludesFile)")
    ap.add_argument("--no-parent-ignores", action="store_true",
                    help="do not apply .gitignore files above DIR in an enclosing repository")
    ap.add_argument("--include-git-dir", action="store_true",
                    help="back up .git directories too (unfiltered)")
    ap.add_argument("--empty-dirs", action="store_true",
                    help="also list empty directories (files format)")
    ap.add_argument("-q", "--quiet", action="store_true", help="no summary on stderr")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return ap


def _split(values):
    out = []
    for v in values:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.show_default_keep:
        print("default keep list (case-insensitive):\n  " + "\n  ".join(DEFAULT_KEEP))
        for k, v in PRESETS.items():
            print("preset %s%s:\n  %s" % (k, " (default)" if k == DEFAULT_PRESET else "",
                                          "\n  ".join(v)))
        return 0
    if args.all_ignored and (args.preset or args.only_patterns):
        sys.stderr.write("restic-gitignore: --all-ignored cannot be combined with --preset "
                         "or --only-patterns\n")
        return 2
    roots = args.roots or ["."]
    keep = [] if args.no_default_keep else default_keep_patterns()
    keep += [Pattern(os.fsencode(k), source="--keep") for k in _split(args.keep)]
    only = None
    if not args.all_ignored:
        only_src = []
        for p in args.preset or ([] if args.only_patterns else [DEFAULT_PRESET]):
            only_src += PRESETS[p]
        if args.only_patterns:
            only_src += _split([args.only_patterns])
        only = [Pattern(os.fsencode(k)) for k in only_src]
    global_data = None
    if args.global_ignore:
        try:
            with open(args.global_ignore, "rb") as f:
                global_data = f.read()
        except OSError as e:
            sys.stderr.write("restic-gitignore: cannot read %s: %s\n" % (args.global_ignore, e))
            return 2
    opts = Options(keep=keep, only=only, info_exclude=args.info_exclude,
                   global_ignore=global_data, parent_ignores=not args.no_parent_ignores,
                   include_git_dir=args.include_git_dir, empty_dirs=args.empty_dirs,
                   outside_repo=args.outside_repo)

    out = open(args.output, "wb") if args.output else sys.stdout.buffer
    sep = b"\0" if args.null else b"\n"
    status = 0
    try:
        for root in roots:
            if not os.path.isdir(root):
                sys.stderr.write("restic-gitignore: not a directory: %s\n" % root)
                status = 2
                continue
            res = scan(root, opts)
            root_b = os.fsencode(root)
            base = root_b if args.relative else os.path.abspath(root_b)

            def full(rel):
                if args.relative and len(roots) == 1:
                    return rel
                return os.path.join(base, rel)

            if args.format == "files":
                for rel in res.kept:
                    p = full(rel)
                    if not args.null and (b"\n" in p or b"\r" in p):
                        # restic --files-from-verbatim splits on \n and strips a trailing \r,
                        # so such a name would be skipped by restic with exit status 0.
                        sys.stderr.write("restic-gitignore: warning: %r contains a newline or "
                                         "carriage return and is NOT in this list; use -0 with "
                                         "--files-from-raw to include it\n" % p)
                        continue
                    if not args.null and has_control(p):
                        sys.stderr.write("restic-gitignore: warning: %r contains control "
                                         "characters; listed, but -0 with --files-from-raw is "
                                         "safer for such names\n" % p)
                    out.write(p + sep)
            elif args.format == "dropped":
                for rel, is_dir in res.dropped:
                    out.write(full(rel) + (b"/" if is_dir else b"") + sep)
            else:
                abs_base = os.path.abspath(root_b)
                for rel, _ in res.dropped:
                    esc = restic_escape(os.path.join(abs_base, rel))
                    if esc is None:
                        sys.stderr.write("restic-gitignore: warning: cannot express %r as a restic "
                                         "pattern; it will be backed up\n" % rel)
                        continue
                    out.write(esc + b"\n")
            if not args.quiet:
                nd = sum(1 for _, d in res.dropped if d)
                sys.stderr.write(
                    "restic-gitignore: %s: %d entries to back up, %d dropped (%d directories), "
                    "%d ignored entries kept by keep rules\n"
                    % (root, len(res.kept), len(res.dropped), nd, len(res.rescued)))
                if res.tracked:
                    shown = b", ".join(res.tracked[:5]).decode("utf-8", "replace")
                    sys.stderr.write("restic-gitignore: kept because git tracks them (ignore "
                                     "rules do not apply to tracked files): %s%s\n"
                                     % (shown, " ..." if len(res.tracked) > 5 else ""))
                if res.rescued:
                    shown = b", ".join(res.rescued[:5]).decode("utf-8", "replace")
                    sys.stderr.write("restic-gitignore:   kept although ignored: %s%s\n"
                                     % (shown, " ..." if len(res.rescued) > 5 else ""))
    finally:
        if args.output:
            out.close()
        else:
            out.flush()
    return status


if __name__ == "__main__":
    sys.exit(main())
