#!/usr/bin/env python3
"""Fail if the working tree contains identifiers or secret-shaped strings.

Two layers:

1. Built-in patterns: emails, IPv4 and tailnet addresses, absolute home-dir
   paths, and token-shaped strings. These hold for any fork.
2. An optional, git-ignored denylist, `.identifiers.local` at the repo root:
   one term per line, `#` comments allowed. Put your own name, places, gear,
   accounts and anything else that should never be published. Terms match
   case-insensitively and only as whole terms: a term that starts or ends with
   a letter cannot touch another letter there, one that starts or ends with a
   digit cannot touch another digit ("kg" hits "100kg", not "background").
   A line `allow: <text>` exempts any line containing <text> (for example an
   authorship credit that has to name you).

Scans files git would publish (tracked plus untracked-not-ignored), so
git-ignored instance data such as a demo store is never read. Outside a git
checkout it walks the tree. Exits 1 and prints file:line for each hit.

Usage: python scripts/scan_identifiers.py [ROOT]
"""
import os
import re
import subprocess
import sys

DENYLIST = ".identifiers.local"

# Some patterns are assembled from pieces so this file does not match itself.
PATTERNS = {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}",
    "ipv4": r"(?<![\d.])(\d{1,3}\.){3}\d{1,3}(?![\d.])",
    "tailnet-ip": r"100\.\d+\.\d+\.\d+",
    "tailnet-name": r"ts" + r"\.net",
    "home-path": "/ho" + "me/",
    "openai-style-key": r"(?<![A-Za-z0-9])" + "sk" + r"-[A-Za-z0-9_-]{16,}",
    "google-key": "AI" + "za",
    "github-token": "gh" + "p_",
    "slack-token": "xo" + "x[abprs]?-?",
    "long-hex": r"\b[0-9a-fA-F]{32,}\b",
    # 40+ base64-alphabet characters containing both a letter and a digit.
    # Underscores and dashes are excluded so snake_case names and rules of
    # dashes in comments do not match.
    "long-base64": r"(?=[A-Za-z0-9+/=]*\d)(?=[A-Za-z0-9+/=]*[A-Za-z])[A-Za-z0-9+/=]{40,}",
}
COMPILED = {k: re.compile(v) for k, v in PATTERNS.items()}
# Public, non-personal strings a built-in pattern would otherwise flag: the
# commit-attribution trailer for AI-written code.
BUILTIN_ALLOW = {"email": ("noreply@anthropic.com",)}


def _edge(ch: str, side: str) -> str:
    if ch.isalpha():
        return r"(?<![A-Za-z])" if side == "start" else r"(?![A-Za-z])"
    if ch.isdigit():
        return r"(?<!\d)" if side == "start" else r"(?!\d)"
    return ""


def term_pattern(term: str) -> re.Pattern:
    body = r"\s+".join(re.escape(w) for w in term.split())
    return re.compile(_edge(term[0], "start") + body + _edge(term[-1], "end"), re.I)


def load_denylist(root: str):
    terms, allow = {}, []
    path = os.path.join(root, DENYLIST)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return terms, allow
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("allow:"):
            allow.append(line[6:].strip())
        else:
            terms[f"denylist:{line}"] = term_pattern(line)
    return terms, allow


def files(root: str):
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, check=True).stdout
        names = [n for n in out.decode().split("\0") if n]
        return [os.path.join(root, n) for n in names
                if os.path.isfile(os.path.join(root, n))]
    except (OSError, subprocess.CalledProcessError):
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            found += [os.path.join(dirpath, fn) for fn in filenames]
        return found


def scan(root):
    terms, allow = load_denylist(root)
    checks = {**COMPILED, **terms}
    hits = []
    for path in files(root):
        if os.path.basename(path) == DENYLIST:
            continue
        rel = os.path.relpath(path, root)
        for name, rx in terms.items():
            if rx.search(rel):
                hits.append((path, 0, name + " (in file name)", rel))
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except (UnicodeDecodeError, OSError):
            hits.append((path, 0, "binary-or-unreadable", ""))
            continue
        for i, line in enumerate(lines, 1):
            if any(a in line for a in allow):
                continue
            for name, rx in checks.items():
                if any(a in line for a in BUILTIN_ALLOW.get(name, ())):
                    continue
                if rx.search(line):
                    hits.append((path, i, name, line.strip()[:100]))
    return hits


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    n_terms = len(load_denylist(root)[0])
    hits = scan(root)
    for path, line, name, text in hits:
        print(f"{os.path.relpath(path, root)}:{line}: {name}: {text}")
    print(f"{len(hits)} hit(s); {len(PATTERNS)} built-in patterns, "
          f"{n_terms} denylist term(s) from {DENYLIST}"
          f"{'' if n_terms else ' (absent: create it to scan for your own identifiers)'}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
