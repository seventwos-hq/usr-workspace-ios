#!/usr/bin/env python3
"""Check text that will become public (commit messages, branch/tag names,
PR titles and bodies, and optionally added file lines) for terms and patterns
that should not be published.

Only the category and location of each hit is printed, never the matched text.
Exit status: 0 clean, 1 hits found, 2 usage or input error (including an
unreadable configured denylist file).

Denylist: env DISCLOSURE_DENYLIST (newline or comma separated terms,
case-insensitive whole-word match) and/or env DISCLOSURE_DENYLIST_FILE
(path to a file, one term per line, '#' comments allowed). Terms of 5+
characters also match common digit/symbol swaps (e.g. 3 for e, 0 for o).

Allowances (to keep false positives low):
- version strings such as 1.2.3 or 1.2.3.4-rc are not IPs unless they are
  four octets 0-255 not preceded by 'v' and not followed by another '.digit';
  0.0.0.0 and 127.0.0.1 are allowed;
- 'This reverts commit <sha>' lines are ignored for the long-hex check, and
  40/64-char hex that names an object in the local git repo is allowed;
- hex strings are only flagged at 32+ chars, base64-like at 40+ chars
  that mix letters and digits and are not plain file paths.

File content (--content BASE): lines added between BASE and HEAD are checked
for denylist terms and credentials only. Files under .forgejo/ are skipped,
and terms listed in .forgejo/disclosure-allow.txt on BASE are exempt there.
Files that are not plain UTF-8 are also scanned in every UTF-16/32 reading.
With --content-warn, content hits are reported but do not fail the check.
"""
import argparse
import codecs
import ipaddress
import os
import re
import subprocess
import sys

OCT = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IPV4 = re.compile(r"(?<![\w.])(?<!v)" + OCT + r"(?:\." + OCT + r"){3}(?!\.?\d)(?![\w-])")
IPV4_OK = {"0.0.0.0", "127.0.0.1"}
IPV6 = re.compile(
    r"(?<![\w:])(?:(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
    r"|(?:[0-9a-fA-F]{1,4}:){1,7}:(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0,6})?"
    r"|::(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4})(?![\w:])")

CREDENTIAL = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}|\bglpat-[A-Za-z0-9_-]{20,}"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|-----BEGIN [A-Z ]+-----"
    r"|\bxox[abpre]-[A-Za-z0-9-]{10,}"
    r"|\bAIza[0-9A-Za-z_-]{35}"
    r"|\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"
    r"|\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|\bph[cx]_[A-Za-z0-9]{20,}"
    r"|\btskey-[A-Za-z0-9-]{10,}"
    r"|\b(?:ntn|secret)_[A-Za-z0-9]{30,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")

PATTERNS = [
    ("private-url", re.compile(
        r"https?://(?:10\.|127\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|localhost\b)", re.I)),
    ("internal-host", re.compile(
        r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:local|lan|internal|intranet|corp|home\.arpa|ts\.net)\b",
        re.I)),
    ("internal-path", re.compile(
        r"(?<![\w.])(?:/mnt/[\w.-]|/opt/data\b|/volume\d+/|/home/[a-z_][\w-]*/|/Users/[\w.-]+/"
        r"|\b[A-Za-z]:\\Users\\[\w.-]+)", re.I)),
    # dotted host with any port, or a bare name with a 4-5 digit port (not image:tag like node:20)
    ("host-port", re.compile(
        r"\b(?:[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+:\d{2,5}|[a-z][a-z0-9-]*:\d{4,5})\b(?![:.\d])", re.I)),
    ("port", re.compile(r"(?<![\w:/])(?<!\d):\d{2,5}\b(?![:.\d])")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")),
    ("uuid", re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("cloud-resource-id", re.compile(r"/subscriptions/[^\s/]+", re.I)),
    ("secret-name", re.compile(
        r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:TOKEN|SECRET|KEY|PASSWORD|PASSWD)(?:_[A-Z0-9]+)*\b"
        r"|\b[A-Z][A-Z0-9]*(?:TOKEN|SECRET|PASSWORD|PASSWD)\b"
        r"|\b(?:TOKEN|SECRET|PASSWORD|API)_[A-Z0-9_]+\b")),
    ("credential", CREDENTIAL),
    ("long-hex", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("long-base64", re.compile(r"(?<![\w+/-])[A-Za-z0-9+/_-]{40,}={0,2}")),
    ("co-author-trailer", re.compile(r"^\s*co-authored-by:", re.I | re.M)),
    ("default-merge-text", re.compile(
        r"^Merge (?:pull request #\d+ from|branch '|remote-tracking branch ')", re.M)),
]
REVERT = re.compile(r"^This reverts commit [0-9a-f]+\.?$", re.I)
LEET = {"a": "a4@", "b": "b8", "e": "e3", "g": "g9", "i": "i1!|", "l": "l1|", "o": "o0",
        "s": "s5$", "t": "t7"}
ALLOW_FILE = ".forgejo/disclosure-allow.txt"


class InputError(Exception):
    pass


def term_rx(term):
    if len(term) < 5:
        return re.escape(term)
    out = []
    for ch in term:
        alts = LEET.get(ch.lower())
        out.append("[" + re.escape(alts + alts.upper()) + "]" if alts else re.escape(ch))
    return "".join(out)


def compile_terms(terms):
    terms = sorted({t.strip() for t in terms if t.strip()}, key=len, reverse=True)
    if not terms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(term_rx(t) for t in terms) + r")(?!\w)", re.I)


def load_denylist(env=None):
    env = os.environ if env is None else env
    terms = []
    raw = env.get("DISCLOSURE_DENYLIST")
    if raw:
        terms += re.split(r"[\n,]", raw)
    path = env.get("DISCLOSURE_DENYLIST_FILE")
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                terms += [l for l in fh.read().splitlines() if not l.lstrip().startswith("#")]
        except (OSError, UnicodeError):
            print("error: denylist file not readable", file=sys.stderr)
            return None
    return compile_terms(terms)


def valid_ipv6(s):
    try:
        ipaddress.IPv6Address(s)
    except ValueError:
        return False
    return True


def is_object_id(g, known_object):
    return (known_object is not None and len(g) in (40, 64)
            and re.fullmatch(r"[0-9a-fA-F]+", g) is not None and known_object(g.lower()))


def find(line, deny, known_object=None, only=None):
    """Return sorted unique (col, category) hits in one line."""
    found = []
    if deny is not None:
        found += [(m.start(), "denylist") for m in deny.finditer(line)]
    found += [(m.start(), "ipv4") for m in IPV4.finditer(line) if m.group() not in IPV4_OK]
    found += [(m.start(), "ipv6") for m in IPV6.finditer(line) if valid_ipv6(m.group())]
    revert = bool(REVERT.match(line.strip()))
    for cat, rx in PATTERNS:
        if revert and cat in ("long-hex", "long-base64"):
            continue
        for m in rx.finditer(line):
            g = m.group()
            if cat == "long-base64" and not (re.search(r"\d", g) and re.search(r"[A-Za-z]", g)):
                continue
            if cat in ("long-hex", "long-base64") and is_object_id(g, known_object):
                continue
            found.append((m.start(), cat))
    return sorted({f for f in found if only is None or f[1] in only})


def scan(text, where, deny, known_object=None):
    """Return list of 'where line L col C: category' strings."""
    return ["%s line %d col %d: %s" % (where, ln, col + 1, cat)
            for ln, line in enumerate(text.splitlines(), 1)
            for col, cat in find(line, deny, known_object)]


def git(*args):
    out = subprocess.run(("git",) + args, check=True, capture_output=True).stdout
    return out.decode("utf-8")  # strict: undecodable output is an input error


def git_object_exists(sha):
    try:
        return subprocess.run(("git", "cat-file", "-e", sha), capture_output=True).returncode == 0
    except OSError:
        return False


def collect(a):
    items = []
    if a.title is not None:
        items.append(("title", a.title))
    body = a.body
    if a.body_file:
        with open(a.body_file, encoding="utf-8") as fh:
            body = fh.read()
    if body is not None:
        items.append(("body", body))
    if a.branch:
        items.append(("branch", a.branch))
    for r in a.refs or []:
        items.append(("ref", r))
    for tf in a.text_file or []:
        with open(tf, encoding="utf-8") as fh:
            items.append(("text", fh.read()))
    if a.range:
        # raw commit objects are read whole; embedded NUL bytes are rejected
        for sha in git("rev-list", a.range).split():
            if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
                raise InputError("malformed commit id")
            raw = subprocess.run(("git", "cat-file", "commit", sha),
                                 check=True, capture_output=True).stdout
            if b"\x00" in raw:
                raise InputError("commit object contains a NUL byte")
            head, sep, msg = raw.decode("utf-8").partition("\n\n")
            short = sha[:10]
            people = [l.split(" ", 1)[1] for l in head.split("\n")
                      if l.startswith(("author ", "committer "))]
            items.append(("commit %s" % short, msg))
            items.append(("commit %s author" % short, "\n".join(people)))
    return items


GIT_ESC = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_path(s):
    """Decode a path from a diff header, including Git's C-style quoted form."""
    if not (len(s) >= 2 and s[0] == '"' and s[-1] == '"'):
        return s
    raw, out, i = s[1:-1].encode("utf-8", "surrogateescape"), bytearray(), 0
    while i < len(raw):
        c = raw[i]
        if c == 92 and i + 1 < len(raw):
            nxt = chr(raw[i + 1])
            if nxt in "0123" and i + 3 < len(raw):
                out.append(int(raw[i + 1:i + 4], 8))
                i += 4
                continue
            out.append(GIT_ESC.get(nxt, raw[i + 1]))
            i += 2
            continue
        out.append(c)
        i += 1
    return out.decode("utf-8", "replace")


def added_lines(base):
    """Yield (file_no, path, line_no, text) for lines added between base and HEAD.

    file_no follows `git diff --name-only --diff-filter=AMCT BASE...HEAD` order.
    Hunk line counts are consumed exactly, so added content can never be read
    as a file header. Diff attributes from the checkout are ignored.
    """
    out = subprocess.run(("git", "-c", "core.quotePath=true", "diff", "-U0", "--text", "--no-textconv",
                          "--no-color", "--no-ext-diff", "--no-renames", "--diff-filter=AMCT",
                          "--src-prefix=a/", "--dst-prefix=b/", base + "...HEAD"),
                         check=True, capture_output=True).stdout.decode("utf-8", "replace")
    path, num, ln, old_left, new_left, type_change = None, 0, 0, 0, 0, False
    for line in out.split("\n"):
        if old_left > 0 or new_left > 0:
            if line.startswith("+"):
                new_left -= 1
                if path is not None:
                    yield num, path, ln, line[1:]
                ln += 1
            elif line.startswith("-"):
                old_left -= 1
            elif line.startswith("\\"):
                pass
            else:
                old_left -= 1
                new_left -= 1
                ln += 1
            continue
        if line.startswith("diff --git "):
            path = None
            # with --diff-filter=AMCT a deletion block is only ever the first half of a type change
            if not type_change:
                num += 1
            type_change = False
        elif line.startswith("deleted file mode "):
            type_change = True
        elif line.startswith("+++ "):
            name = unquote_path(line[4:].rstrip("\t"))
            if name == "/dev/null":
                path = None
            elif name.startswith("b/"):
                path = name[2:]
            else:
                raise InputError("unparseable file header in diff")
        elif line.startswith("@@ "):
            m = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                raise InputError("malformed hunk header")
            old_left = int(m.group(1) or 1)
            ln = int(m.group(2))
            new_left = int(m.group(3) or 1)


def content_allow(base):
    p = subprocess.run(("git", "show", "%s:%s" % (base, ALLOW_FILE)), capture_output=True)
    if p.returncode != 0:
        return None
    lines = p.stdout.decode("utf-8", "replace").splitlines()
    return compile_terms([l for l in lines if not l.lstrip().startswith("#")])


WIDE_CODECS = tuple((enc, off) for enc, step in (("utf-16-le", 2), ("utf-16-be", 2),
                                                 ("utf-32-le", 4), ("utf-32-be", 4))
                    for off in range(step))


def wide_decodings(raw):
    """Every UTF-16/32 reading of a blob, both byte orders and every alignment.

    Used for blobs that are not plain UTF-8 text. BOMs and NUL layouts are not
    trusted to pick one encoding, so all readings are scanned.
    """
    for enc, off in WIDE_CODECS:
        yield raw[off:].decode(enc, "replace")


def blob_at_head(path):
    kind = subprocess.run(("git", "cat-file", "-t", "HEAD:" + path), capture_output=True)
    if kind.returncode != 0 or kind.stdout.strip() != b"blob":
        return None  # e.g. a submodule
    return subprocess.run(("git", "cat-file", "blob", "HEAD:" + path),
                          check=True, capture_output=True).stdout


def is_utf8_text(raw):
    if b"\x00" in raw:
        return False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def scan_content(base, deny):
    """File names are never printed; files are numbered in `git diff --name-only BASE...HEAD` order.

    Added lines are scanned as-is and, when they contain NULs, with NULs removed.
    Changed files that are not plain UTF-8 text are also read whole at HEAD and
    scanned in every UTF-16/32 reading.
    """
    allow = content_allow(base)

    def check(t):
        found = set(find(t, None, only={"credential"}))
        if deny is not None:
            # an allowed term exempts a denied match only when it covers the whole match
            found.update((m.start(), "denylist") for m in deny.finditer(t)
                         if allow is None or not allow.fullmatch(m.group()))
        return found

    hits, seen = [], set()

    def add(num, ln, found):
        for col, cat in sorted(found):
            h = "file %d line %d col %d: %s" % (num, ln, col + 1, cat)
            if h not in seen:
                seen.add(h)
                hits.append(h)

    done = set()
    for num, path, ln, text in added_lines(base):
        if path.startswith(".forgejo/"):
            continue
        found = check(text)
        if "\x00" in text:
            found |= check(text.replace("\x00", ""))
        add(num, ln, found)
        if path in done:
            continue
        done.add(path)
        raw = blob_at_head(path)
        if raw is None or is_utf8_text(raw):
            continue
        for decoded in wide_decodings(raw):
            for wln, line in enumerate(decoded.splitlines(), 1):
                add(num, wln, check(line))
    return hits

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--branch")
    p.add_argument("--range", help="git revision range base..head")
    p.add_argument("--refs", nargs="*", help="tag/branch names")
    p.add_argument("--text-file", action="append", help="plain text file to check")
    p.add_argument("--content", metavar="BASE",
                   help="also check file lines added between BASE and HEAD")
    p.add_argument("--content-warn", action="store_true",
                   help="report file content hits without failing")
    p.add_argument("--no-author-check", action="store_true",
                   help="do not check commit author/committer names and emails")
    a = p.parse_args(argv)
    deny = load_denylist()
    if deny is None:
        if os.environ.get("DISCLOSURE_DENYLIST") or os.environ.get("DISCLOSURE_DENYLIST_FILE"):
            print("error: configured denylist is unavailable or empty", file=sys.stderr)
            return 2
        print("warning: DISCLOSURE_DENYLIST is not set; running pattern checks only",
              file=sys.stderr)
    try:
        items = collect(a)
        content_hits = scan_content(a.content, deny) if a.content else []
    except (OSError, UnicodeDecodeError, InputError, subprocess.CalledProcessError) as e:
        print("error: could not read input (%s)" % type(e).__name__, file=sys.stderr)
        return 2
    if not items and not a.content:
        p.print_usage(sys.stderr)
        return 2
    known = git_object_exists if a.range else None
    hits = []
    for where, text in items:
        if where.endswith(" author"):
            if not a.no_author_check:
                # public author identity: only the denylist applies
                hits += [h for h in scan(text, where, deny) if h.endswith(": denylist")]
        else:
            hits += scan(text, where, deny, known)
    for h in hits:
        print(h)
    for h in content_hits:
        print(("warning: " if a.content_warn else "") + h)
    print("disclosure check: %d hit(s), %d file content hit(s)%s"
          % (len(hits), len(content_hits), " (warn only)" if a.content_warn else ""),
          file=sys.stderr)
    return 1 if hits or (content_hits and not a.content_warn) else 0


if __name__ == "__main__":
    sys.exit(main())
