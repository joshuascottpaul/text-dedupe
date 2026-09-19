#!/usr/bin/env python3
"""
Dedupe a plain-text file (e.g. a .txt export of a password-manager secure
note).

RUN THIS YOURSELF IN YOUR OWN TERMINAL on files that already live on your
disk (or your clipboard). It never talks to any network or password-manager
CLI/API -- it only ever reads and writes plain text.

Usage:
  # one or more input files, last argument is always the output file
  python3 dedupe_note.py INPUT.txt OUTPUT.txt
  python3 dedupe_note.py INPUT1.txt INPUT2.txt INPUT3.txt OUTPUT.txt

  # single argument = read from the clipboard instead, write to that file
  python3 dedupe_note.py OUTPUT.txt

  # optional tuning, works with either form
  python3 dedupe_note.py INPUT.txt OUTPUT.txt --near-threshold 0.85

Only counts and short "block #N dropped" markers are printed -- never the
block content itself.
"""
import argparse
import difflib
import re
import subprocess
import sys


URL_LINE_RE = re.compile(r'^(https?://\S+)$', re.IGNORECASE)
URL_SPLIT_RE = re.compile(r'^(https?://[^/]+)(/.*)?$', re.IGNORECASE)


def normalize_url(u):
    """Lowercase scheme+host (case-insensitive by spec), keep path case
    (URL paths are case-sensitive), strip one trailing slash."""
    u = u.strip()
    m = URL_SPLIT_RE.match(u)
    if not m:
        return u.lower()
    scheme_host = m.group(1).lower()
    path = m.group(2) or ""
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return scheme_host + path


def dedupe_urls_globally(text):
    """Drop exact/near-identical whole-line URLs wherever they occur in the
    document, regardless of which paragraph/block they're in -- fixes the
    case where the same URL is duplicated across two different blocks
    rather than repeated within a single block."""
    lines = text.split("\n")
    seen = set()
    out_lines = []
    dropped = 0
    for line in lines:
        m = URL_LINE_RE.match(line.strip())
        if m:
            key = normalize_url(m.group(1))
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
        out_lines.append(line)
    return "\n".join(out_lines), dropped


def split_blocks(text):
    raw = text.replace("\r\n", "\n")
    blocks = [b.strip("\n") for b in raw.split("\n\n")]
    return [b for b in blocks if b.strip()]


# A block whose FIRST line is a bare "License Key:" / "Serial:" / "Key:" /
# "Code:" / "Number:" / "User ID:" / "Registration Key:" / "Activation
# Code:" line (no product name before it) is not a new record -- it's a
# continuation of the record above it that got orphaned by a stray blank
# line (a common copy/paste artifact). Re-attach it instead of leaving it
# floating as its own one-line block.
CONTINUATION_FIRST_LINE_RE = re.compile(
    r'(?i)^\s*(?:license\s*code|license\s*key|registration\s*key|'
    r'activation\s*code|serial(?:\s*number)?|key|code|number|user\s*id)\s*:'
)


def merge_stray_continuations(blocks):
    merged = []
    for b in blocks:
        first_line = b.split("\n", 1)[0]
        if merged and CONTINUATION_FIRST_LINE_RE.match(first_line):
            merged[-1] = merged[-1] + "\n" + b
        else:
            merged.append(b)
    return merged


def dedupe_exact_lines_within_block(block):
    """Drop an exact repeat of a line WITHIN the same block (e.g. a
    boilerplate 'Name: Joshua Paul' line accidentally typed twice in one
    record). Never touches lines across different blocks -- that cross-
    block case is handled separately and much more conservatively."""
    seen = set()
    out = []
    for line in block.split("\n"):
        key = line.strip()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(line)
    return "\n".join(out)


def normalize(b):
    return "\n".join(line.strip().lower() for line in b.strip().split("\n") if line.strip())


# License keys, serials, agreement numbers, activation codes: dash-delimited
# alnum groups (e.g. "6100-1564-2483-6441-2", "ITG-123-292-928-181-032") or a
# bare run of 6+ digits (e.g. an AppleCare/agreement number). Case preserved
# on purpose -- these identifiers are compared exactly, never fuzzy-matched.
IDENTIFIER_RE = re.compile(r'\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+){2,}\b|\b\d{6,}\b|\b[A-Za-z]+\d[A-Za-z0-9]{5,}\b')

# Whatever follows a "Serial:" / "License Key:" / "Registration Key:" /
# "Key:" / "Code:" / "Number:" / "User ID:" label -- captured verbatim
# (any internal format: dashes, spaces, glued letters+digits) since the
# label itself is the strongest signal that this line IS the identifier,
# regardless of how it's punctuated.
LABELED_KEY_RE = re.compile(
    r'(?im)^\s*[^\n:]*?(?:license\s*code|license\s*key|registration\s*key|'
    r'activation\s*code|serial(?:\s*number)?|key|code|number|user\s*id)\s*:\s*(.+?)\s*$'
)


def extract_identifiers(block):
    """Return the set of license-key/serial/agreement-number-shaped tokens
    in a block. Two blocks that both contain identifiers are only eligible
    to be treated as duplicates of each other if these sets are identical --
    this is what stops e.g. 'Data Rescue 3' (serial ...-6441-2) and
    'Data Rescue 4' (serial ...-6441-2 but different product) or two 'Data
    Rescue 3' entries with different serials from ever being merged just
    because the surrounding text is very similar. Also stops e.g. two
    entries for the same product with different label-prefixed keys
    ('Serial:id172991162212uks', 'License Key: A 6BD5 279D EA32 ...') from
    being merged even when neither matches the dash/digit-run shape."""
    ids = set(IDENTIFIER_RE.findall(block))
    for line in block.split("\n"):
        m = LABELED_KEY_RE.match(line)
        if m:
            ids.add(m.group(1).strip())
    return frozenset(ids)


def has_structural_colon(line):
    """True if the line has a colon that isn't just a URL scheme separator
    -- i.e. it looks like a 'Label: value' record field, not a bare list
    entry. A block containing even one such line is a record, never a
    flat list, regardless of how short its lines are."""
    s = re.sub(r'^https?://', '', line.strip(), flags=re.IGNORECASE)
    return ":" in s


def dedupe_wordlist_lines(blocks):
    """Inside any block that looks like a flat list of short lines (no
    key:value / heading structure -- e.g. a bare list of hostnames or URLs),
    dedupe individual lines while preserving first-seen order. Tracks seen
    lines GLOBALLY across every listy block (not reset per block), so the
    same entry repeated in two separate list-style blocks still gets caught.
    A block is disqualified from "listy" entirely if ANY line has a
    structural colon -- that's the signal it's a multi-field record (e.g.
    'Name: Joshua Paul', 'License Key: ...'), never a bare list, even if
    its lines happen to be short. Without this guard, a record's short
    lines could get compared against the SAME global seen-set as unrelated
    word lists, silently deleting a boilerplate line (like a repeated
    'Name:' or 'Email:' line) from a completely different, later record."""
    out = []
    seen = set()
    for b in blocks:
        lines = b.split("\n")
        non_empty = [l for l in lines if l.strip()]
        listy = (
            len(non_empty) >= 3
            and not any(has_structural_colon(l) for l in non_empty)
            and sum(1 for l in non_empty if len(l.split()) <= 2) / len(non_empty) > 0.7
        )
        if listy:
            new_lines = []
            for l in lines:
                m = URL_LINE_RE.match(l.strip())
                key = normalize_url(m.group(1)) if m else l.strip().lower()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                new_lines.append(l)
            out.append("\n".join(new_lines))
        else:
            out.append(b)
    return out


def dedupe_blocks(blocks, near_threshold):
    seen = []  # list of (normalized_text, identifier_set, original_index)
    kept = []
    dropped = []  # (original_index, reason, matched_kept_original_index)
    for i, b in enumerate(blocks):
        n = normalize(b)
        ids = extract_identifiers(b)

        exact_idx = None
        for sn, sids, orig_j in seen:
            if ids != sids:
                continue  # identifiers present and differing -- never a match, full stop
            if sn == n:
                exact_idx = orig_j
                break
        if exact_idx is not None:
            dropped.append((i, "exact", exact_idx))
            continue

        near_idx = None
        near_ratio = 0.0
        for sn, sids, orig_j in seen:
            if ids != sids:
                continue  # same guard for near-matching: identifiers must match exactly
            ratio = difflib.SequenceMatcher(None, n, sn).ratio()
            if ratio >= near_threshold and ratio > near_ratio:
                near_idx = orig_j
                near_ratio = ratio
        if near_idx is not None:
            dropped.append((i, f"near({near_ratio:.2f})", near_idx))
            continue

        seen.append((n, ids, i))
        kept.append(b)
    return kept, dropped


def read_clipboard():
    out = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True)
    return out.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+",
                     help="one or more input .txt files followed by the output .txt file; "
                          "if only one path is given, input is read from the clipboard instead")
    ap.add_argument("--near-threshold", type=float, default=0.90,
                     help="similarity ratio (0-1) above which two blocks count as near-duplicates (default 0.90)")
    args = ap.parse_args()

    if len(args.files) == 1:
        output_path = args.files[0]
        text = read_clipboard()
        print("Read input from clipboard.")
    else:
        *input_paths, output_path = args.files
        parts = []
        for p in input_paths:
            with open(p, "r") as f:
                parts.append(f.read())
        text = "\n\n".join(parts)
        print(f"Read {len(input_paths)} input file(s).")

    if not text.strip():
        print("Input is empty.", file=sys.stderr)
        sys.exit(1)

    text, url_dropped = dedupe_urls_globally(text)

    blocks = split_blocks(text)
    blocks = merge_stray_continuations(blocks)
    blocks = [dedupe_exact_lines_within_block(b) for b in blocks]
    blocks = dedupe_wordlist_lines(blocks)
    kept, dropped = dedupe_blocks(blocks, args.near_threshold)

    print(f"Duplicate URL lines removed (document-wide): {url_dropped}")
    print(f"Original blocks: {len(blocks)}")
    print(f"Kept blocks:     {len(kept)}")
    print(f"Dropped blocks:  {len(dropped)}")
    for i, reason, matched in dropped:
        print(f"  - block #{i} dropped ({reason}), duplicate of block #{matched}")

    new_text = "\n\n".join(kept) + "\n"
    with open(output_path, "w") as f:
        f.write(new_text)

    print(f"\nWrote deduped output to: {output_path}")
    print("Review it yourself, then copy the content back into 1Password by hand.")


if __name__ == "__main__":
    main()
