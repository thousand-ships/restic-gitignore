# restic-gitignore

Back up a directory tree with [restic](https://restic.net) while honouring `.gitignore`, so
`node_modules/`, `target/`, `dist/` and other build output stay out of your snapshots.

Written for [restic/restic#1514](https://github.com/restic/restic/issues/1514). restic has no
`.gitignore` support today. This tool walks the tree, applies the ignore rules the way git
does, and writes one of two things:

- a file list for `restic backup --files-from-verbatim` (the default), or a NUL-separated
  list for `--files-from-raw` (`-0`);
- an exclude file for `restic backup --exclude-file` (`--format exclude`).

It's one Python file using only the standard library (Python 3.8+). It needs `git` on your
PATH: it asks git which directories are repositories and which files are tracked. Without git it
warns and applies no `.gitignore` rules (so nothing is dropped).

## Read this first: ignored is not the same as disposable

`.gitignore` lists files you don't want in *git*, and that often includes files you very much
want in a *backup*: `.env`, private keys, `*.tfstate`, local SQLite databases, personal notes.
Several people in #1514 pointed this out. So since 0.2.0 the default is the conservative mode:

| Mode | What gets dropped | Use when |
|---|---|---|
| **default** (= `--preset build-dirs`) | Only paths that git ignores **and** that match a build/vendor/cache list (`node_modules/`, `target/`, `dist/`, `build/`, `__pycache__/`, `.venv/`, `*.o`, `*.class`, … see `--show-default-keep`) | You want build junk out and everything else in |
| `--only-patterns 'node_modules/,target/,*.o'` | Same, with your own list instead of the preset (the built-in keep list still applies inside the directories you name) | You know exactly what's junk |
| `--all-ignored` | Everything git ignores, **except** paths on the built-in keep list (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `.ssh/`, `.aws/`, `*secret*`, `*credential*`, `*.tfstate`, `*.sqlite`, `*.db`, …) and your `--keep` patterns | You mostly trust your `.gitignore` files |
| `--all-ignored --no-default-keep` | Exactly what git ignores, secrets included | You want pure git semantics |

How keep rules work (built-in list and `--keep`), in every mode:

- They are never dropped, **even inside an ignored directory**. With `instance/` ignored,
  `instance/app.db` is still backed up; the rest of `instance/` is left out entry by entry.
- A matching directory is kept whole: `--keep notes/` backs up everything under `notes/`,
  whether `notes/` itself is ignored or only some files in it are.
- Matching is case-insensitive (`Secrets.yaml`, `.ENV`, `ID_RSA` match).
- One exception: inside build directories (`node_modules/`, `target/`, `.venv/` …) only your
  own `--keep` patterns are looked for, not the built-in list, because packages ship
  `*.pem` test fixtures and `*secret*.js` files.

The keep list is a safety net, not a guarantee. It can't know about your own
`passwords.txt`. Add your own with `--keep 'notes/,*.kdbx'`. Before the first real backup,
check what will be left out:

```bash
python3 restic_gitignore.py --format dropped --relative ~/code
```

**Outside a git repository nothing is ignored**, as in git. If you back up `$HOME` and it isn't
a repository, a `~/.gitignore` (often used as `core.excludesFile`) is *not* applied; the tool
says so on stderr. Git repositories found below DIR still apply their own rules. Pass
`--outside-repo` to apply `.gitignore` files outside any repository anyway.

## Install

It's one file. Download `restic_gitignore.py` from this repository, then either run it with
`python3 restic_gitignore.py ...` or put it on your PATH as `restic-gitignore`:

```
install -m 755 restic_gitignore.py ~/.local/bin/restic-gitignore
restic-gitignore --version
```

## Use

```bash
# 1. File list (exact). Each run lists the files that exist right now.
python3 restic_gitignore.py ~/code > /tmp/list.txt
restic backup --files-from-verbatim /tmp/list.txt

# Odd file names (including newlines): NUL-separated list
python3 restic_gitignore.py -0 ~/code > /tmp/list.bin
restic backup --files-from-raw /tmp/list.bin

# 2. Exclude file (recommended for scheduled backups, see below)
python3 restic_gitignore.py --format exclude ~/code > /tmp/excludes.txt
restic backup ~/code --exclude-file /tmp/excludes.txt

# Broader: drop everything .gitignore ignores, except the keep list
python3 restic_gitignore.py --all-ignored --format exclude ~/code > /tmp/excludes.txt
```

**File list or exclude file?** With `--files-from-*`, restic records every listed file as a
snapshot path. When the list changes (you created one new file), restic finds no parent
snapshot and re-reads every file. Data is still deduplicated, but the backup is slower. This
was checked with restic 0.16.4. `--parent latest` doesn't help, but `--parent <snapshot-id>`
does. The exclude file avoids the problem: you back up the directory itself, so the snapshot
paths stay the same. A build directory created after the exclude file was written does get
backed up. That's the safe direction. **The unsafe direction:** an ignored directory that held no
keep matches at scan time is excluded whole, so a `.env` or `app.db` created there later is
skipped until the exclude file is rewritten. Regenerate it right before every backup, in the
same cron line:
`restic-gitignore --format exclude ~/code > /tmp/excl.txt && restic backup ~/code --exclude-file /tmp/excl.txt`.

Use the same path for both commands. The exclude file holds absolute paths, and restic
matches them against the path you pass to `restic backup`. Characters restic would otherwise
interpret are escaped: `* ? [ \`, `$` (restic expands `$VARS` in exclude files) and
trailing whitespace. A name containing a newline or carriage return can't be written into an
exclude file, so the tool warns and that entry gets backed up. In the newline-separated file
list such a name is left out with a warning (restic would skip it silently); use `-0` with
`--files-from-raw` for those. Names with other control characters are listed, with a warning.

### Options

| Option | What it does |
|---|---|
| `--format files\|exclude\|dropped` | file list (default), restic exclude file, or a review list of what's dropped |
| `-0` | NUL-separated list for `--files-from-raw` |
| `-o FILE` | write to FILE instead of stdout |
| `--relative` | paths relative to DIR instead of absolute |
| `--preset build-dirs` | only drop ignored paths that also match the preset (the default) |
| `--only-patterns LIST` | only drop ignored paths that also match these patterns (replaces the default preset) |
| `--all-ignored` | drop everything git ignores, minus keep rules |
| `--keep LIST` | always back these up, even if ignored or inside an ignored directory (gitignore syntax, `!` allowed, case-insensitive). A pattern with a `/` is anchored to **DIR** (the directory you scan), not the repository root; scan each tree once rather than passing overlapping DIRs |
| `--no-default-keep` | turn off the built-in keep list |
| `--outside-repo` | apply `.gitignore` files even when DIR is not inside a git repository |
| `--info-exclude` | also apply `.git/info/exclude` (off by default) |
| `--global-ignore FILE` | also apply FILE at lowest precedence, like `core.excludesFile` (off by default) |
| `--no-parent-ignores` | when DIR is inside a repo, don't apply `.gitignore` files above DIR |
| `--include-git-dir` | also back up `.git` directories (skipped by default) |
| `--empty-dirs` | also list empty directories (file-list mode) |

## What "the way git does" means

- `.gitignore` in every directory. Deeper files take precedence over shallower ones, and
  within a file the last matching line wins.
- Negation with `!`. As in git, a file can't be re-included when a parent directory is
  ignored, and `.gitignore` files inside ignored directories aren't read.
- Trailing `/` matches directories only. A pattern with a `/` at the start or in the middle
  is anchored to its `.gitignore`'s directory. Other patterns match a name at any depth.
- `*`, `?`, `**` (leading, trailing, middle), character classes (`[a-z]`, `[!x]`, `[^x]`,
  `[]x]`, POSIX `[[:alpha:]]`), backslash escapes, `\#` and `\!`.
- Comments, blank lines, trailing spaces (unless escaped with `\ `), CRLF line endings, and
  a UTF-8 byte order mark.
- **Tracked files are never dropped** (since 0.3.0). git doesn't apply ignore rules to files
  in its index (committed before the pattern was added, or `git add -f`), so neither does the
  tool: it reads `git ls-files` for each repository. Inside an ignored directory the tracked
  files stay and the untracked rest is dropped entry by entry.
- A repository is what git says it is: the tool asks `git rev-parse`. An empty `.git`
  directory or a `.git` file pointing nowhere is not a repository, so no rules apply there
  (with a warning). If `git` is not installed, no rules apply at all (with a warning).
- Nested git repositories follow their own rules. The outer repo's `.gitignore` doesn't apply
  inside them, as in git.
- If DIR is inside a repository, `.gitignore` files from the repo root down to DIR apply.
  Outside any repository none apply (unless `--outside-repo`).
- The glob matcher is a line-by-line port of git's `wildmatch.c`, quirks included.

Other behaviour:

- **Symlinks** are listed as symlinks and never followed. restic stores the link itself.
- **Unreadable directories** produce a warning on stderr, and the scan carries on. Such a
  directory is listed whole and never excluded, even inside an ignored directory, so a restic
  run with more access (e.g. as root) still backs it up.
- **Build directories:** inside an ignored build/vendor directory (`node_modules/`, `out/`,
  `build/` ...) only `--keep` patterns are searched, not the built-in keep list. For generic
  names such as `out/`, `build/` or `.terraform/` the tool warns when that drops a keep-list
  match. A *tracked*, non-ignored `build/` or `vendor/` is source, not build output.
- **`.git` directories** are never descended into. That means your local git history (unpushed
  commits, stashes) is **not** backed up unless you pass `--include-git-dir`.

## Tested

The main evidence is differential: the tool's output compared with real git (run with
`--all-ignored --no-default-keep`). Details are in [VERIFY.md](VERIFY.md). The rules the tool
must never break are listed, each with its tests, in [INVARIANTS.md](INVARIANTS.md).

- 5,300 random fixture repositories (nested `.gitignore` files, negation, odd names, symlinks,
  CRLF, junk patterns). The tool's kept and dropped sets equal `git ls-files --others
  [--ignored] --exclude-standard` in every case, about 358,000 paths in total.
- 22,000 random pattern files checked against `git check-ignore --no-index`: 1.26 million path
  checks, 0 mismatches.
- All 312 templates in [github/gitignore](https://github.com/github/gitignore), with trees
  generated from each template's own patterns. 4 seeds, 0 mismatches.
- A real restic 0.16.4 round trip (run on 0.1.0; not yet re-run on 0.2.0 or 0.3.0): back up, restore, then compare file sets and checksums for
  all three output modes and the safe preset. Exactly the expected files came back: `.env`
  present, build directories absent.
- An independent black-box test ([TEST-blackbox.md](TEST-blackbox.md)) found 3 high and 3
  medium issues in 0.1.0; all are fixed in 0.2.0 ([FIXES-r1.md](FIXES-r1.md)). A red team
  against [INVARIANTS.md](INVARIANTS.md) found 3 high in 0.2.0 (tracked files, broken `.git`,
  `--only-patterns`); fixed in 0.3.0 ([FIXES-r2.md](FIXES-r2.md)).
- Unit and invariant tests: `python3 -m unittest discover -s tests`. Full runs: `python3 tests/difftest.py
  5000`, `python3 tests/wilddiff.py 20000`, `python3 tests/roundtrip.py`.

## Limits

- The tool needs `git` on PATH to apply any rules. A repository git refuses (for example
  "dubious ownership" when root scans another user's repo) gets no rules: everything is kept,
  with a warning. Running as the repo owner, or adding it to `safe.directory`, fixes that.
- `core.excludesFile` isn't read automatically; pass it with `--global-ignore`.
  `core.ignoreCase` isn't supported, so matching is case-sensitive, as on Linux.
- Directories whose path is longer than the OS limit (4,096 bytes on Linux) can't be read;
  the tool warns and lists them whole (never excluded).
- The file list is a snapshot of the tree at scan time. Files created between the scan and
  the backup are missed in file-list mode.
- Empty directories are left out of file lists unless you pass `--empty-dirs`. git doesn't
  track them either.
- If restic gains native support ([PR #4227](https://github.com/restic/restic/pull/4227),
  `--exclude-gitignored`), use that instead.

## Credit

The thread already has working answers, and this tool builds on them:
`rg --files` with `--files-from-verbatim` (shared in #1514 by tomasol and civts), `fd` and
`git ls-files` scripts, and [PR #4227](https://github.com/restic/restic/pull/4227), which
adds native `--exclude-gitignored` support to restic. This tool adds a mode that keeps your
secrets, an exclude-file output, and a differential test suite against git.

Built and maintained by AI agents (thousand-ships). Not affiliated with the restic project.
Issues and requests are welcome. MIT licence.
