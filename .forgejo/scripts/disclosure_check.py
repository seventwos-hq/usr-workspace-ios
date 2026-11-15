#!/usr/bin/env python3
"""Check text that will become public (commit messages, branch/tag names,
PR titles and bodies) for terms and patterns that should not be published.

Only the category and location of each hit is printed, never the matched text.
Exit status: 0 clean, 1 hits found, 2 usage error.

Denylist: env DISCLOSURE_DENYLIST (newline or comma separated terms,
case-insensitive whole-word match) and/or env DISCLOSURE_DENYLIST_FILE
(path to a file, one term per line, '#' comments allowed).

Allowances (to keep false positives low):
- version strings such as 1.2.3 or 1.2.3.4-rc are not IPs unless they are
  four octets 0-255 not preceded by 'v' and not followed by another '.digit';
- 'This reverts commit <sha>' lines are ignored for the long-hex check;
- hex strings are only flagged at 32+ chars, base64-like at 40+ chars
  that mix letters and digits.
"""
import argparse
import os
import re
import subprocess
import sys

OCT = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IPV4 = re.compile(r"(?<![\w.])(?<!v)" + OCT + r"(?:\." + OCT + r"){3}(?!\.?\d)(?![\w-])")
IPV6 = re.compile(
    r"(?<![\w:])(?:(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
    r"|(?:[0-9a-fA-F]{1,4}:){1,7}:(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0,6})?"
    r"|::(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4})(?![\w:])")

PATTERNS = [
    ("private-url", re.compile(
        r"https?://(?:10\.|127\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|localhost\b)", re.I)),
    ("internal-host", re.compile(
        r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:local|lan|internal|intranet|corp|home\.arpa)\b", re.I)),
    ("host-port", re.compile(r"\b[a-z][a-z0-9.-]*:\d{2,5}\b(?!:)", re.I)),
    ("port", re.compile(r"(?<![\w:/])(?<!\d):\d{2,5}\b(?![:.\d])")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("uuid", re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("cloud-resource-id", re.compile(r"/subscriptions/[^\s/]+", re.I)),
    ("secret-name", re.compile(
        r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_?(?:TOKEN|SECRET|KEY|PASSWORD|PASSWD|API)(?:_[A-Z0-9]+)*\b"
        r"|\b(?:TOKEN|SECRET|PASSWORD|API)_[A-Z0-9_]+\b")),
    ("credential", re.compile(
        r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}|\bglpat-[A-Za-z0-9_-]{20,}"
        r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|-----BEGIN [A-Z ]+-----|\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("long-hex", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("long-base64", re.compile(r"(?<![\w+/])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])"
                               r"[A-Za-z0-9+/_-]{40,}={0,2}")),
    ("co-author-trailer", re.compile(r"^\s*co-authored-by:", re.I | re.M)),
    ("default-merge-text", re.compile(
        r"^Merge (?:pull request #\d+ from|branch '|remote-tracking branch ')", re.M)),
]
REVERT = re.compile(r"^This reverts commit [0-9a-f]+\.?$", re.I)


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
        except OSError:
            print("warning: denylist file not readable", file=sys.stderr)
    terms = sorted({t.strip() for t in terms if t.strip()}, key=len, reverse=True)
    if not terms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)", re.I)


def scan(text, where, deny):
    """Return list of 'where line L col C: category' strings."""
    hits = []
    for ln, line in enumerate(text.splitlines(), 1):
        found = []
        if deny is not None:
            found += [(m.start(), "denylist") for m in deny.finditer(line)]
        found += [(m.start(), "ipv4") for m in IPV4.finditer(line)]
        found += [(m.start(), "ipv6") for m in IPV6.finditer(line) if m.group().count(":") >= 2
                  and re.search(r"[0-9a-fA-F]", m.group())]
        revert = bool(REVERT.match(line.strip()))
        for cat, rx in PATTERNS:
            if revert and cat in ("long-hex", "long-base64"):
                continue
            found += [(m.start(), cat) for m in rx.finditer(line)]
        seen = set()
        for col, cat in sorted(found):
            if (col, cat) in seen:
                continue
            seen.add((col, cat))
            hits.append("%s line %d col %d: %s" % (where, ln, col + 1, cat))
    return hits


def git(*args):
    return subprocess.run(("git",) + args, check=True, capture_output=True, text=True).stdout


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
        out = git("log", "--format=%h%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e", a.range)
        for rec in out.split("\x1e"):
            rec = rec.strip("\n")
            if not rec:
                continue
            sha, an, ae, cn, ce, msg = rec.split("\x00", 5)
            items.append(("commit %s" % sha, msg))
            items.append(("commit %s author" % sha, "%s\n%s\n%s\n%s" % (an, ae, cn, ce)))
    return items


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--branch")
    p.add_argument("--range", help="git revision range base..head")
    p.add_argument("--refs", nargs="*", help="tag/branch names")
    p.add_argument("--text-file", action="append", help="plain text file to check")
    p.add_argument("--no-author-check", action="store_true",
                   help="do not check commit author/committer names and emails")
    a = p.parse_args(argv)
    deny = load_denylist()
    if deny is None:
        print("warning: DISCLOSURE_DENYLIST is not set; running pattern checks only",
              file=sys.stderr)
    try:
        items = collect(a)
    except (OSError, subprocess.CalledProcessError) as e:
        print("error: could not read input (%s)" % type(e).__name__, file=sys.stderr)
        return 2
    if not items:
        p.print_usage(sys.stderr)
        return 2
    hits = []
    for where, text in items:
        if a.no_author_check and where.endswith(" author"):
            continue
        if where.endswith(" author"):
            # public author identity: only the denylist applies
            hits += [h for h in scan(text, where, deny) if h.endswith(": denylist")]
        else:
            hits += scan(text, where, deny)
    for h in hits:
        print(h)
    print("disclosure check: %d hit(s)" % len(hits), file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
