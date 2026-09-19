#!/usr/bin/env python3
"""
Dedupe a plain-text file of loosely structured content -- e.g. a messy notes
file, an export of a password-manager secure note, a scratch document that
accumulated duplicate entries over time.

RUN THIS YOURSELF IN YOUR OWN TERMINAL on files that already live on your
disk (or your clipboard). It never talks to any network or external API --
it only ever reads and writes plain text.

Usage:
  # one or more input files, last argument is always the output file
  python3 dedupe_text.py INPUT.txt OUTPUT.txt
  python3 dedupe_text.py INPUT1.txt INPUT2.txt INPUT3.txt OUTPUT.txt

  # single argument = read from the clipboard instead, write to that file
  python3 dedupe_text.py OUTPUT.txt

  # optional tuning, works with either form
  python3 dedupe_text.py INPUT.txt OUTPUT.txt --near-threshold 0.85

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


def is_listy_block(lines):
    """True for a genuine flat list of bare short entries (app names,
    hostnames, bare URLs) -- no line has a structural colon, and most
    lines are one or two bare tokens. A record with 'Label:' lines never
    qualifies, no matter how short its lines are."""
    non_empty = [l for l in lines if l.strip()]
    return (
        len(non_empty) >= 3
        and not any(has_structural_colon(l) for l in non_empty)
        and sum(1 for l in non_empty if len(l.split()) <= 2) / len(non_empty) > 0.7
    )


def merge_duplicate_url_labels(blocks):
    """For a block that pairs exactly one URL with one or more label lines
    (e.g. 'alpha-tool' / 'https://example.com/x/') -- as opposed to a bare
    multi-item list, which is left to dedupe_wordlist_lines -- if that same
    URL already appeared in an earlier block, migrate this block's labels
    up into that earlier block (as additional aliases, inserted right
    before the URL line) instead of leaving them behind as an orphan.
    The whole duplicate block is then dropped. Blocks with zero URLs, or
    more than one URL, are left untouched -- ambiguous shapes aren't worth
    guessing about."""
    seen_target = {}  # normalized_url -> index into `out`
    out = []
    merged = 0
    for b in blocks:
        lines = b.split("\n")
        if is_listy_block(lines):
            out.append(b)
            continue
        url_matches = [(i, URL_LINE_RE.match(l.strip())) for i, l in enumerate(lines)]
        url_matches = [(i, m) for i, m in url_matches if m]
        if len(url_matches) != 1:
            out.append(b)
            continue
        url_idx, url_match = url_matches[0]
        key = normalize_url(url_match.group(1))
        labels = [l for i, l in enumerate(lines) if i != url_idx and l.strip()]
        if key in seen_target:
            target_i = seen_target[key]
            target_lines = out[target_i].split("\n")
            t_url_idxs = [i for i, l in enumerate(target_lines) if URL_LINE_RE.match(l.strip())]
            insert_at = t_url_idxs[0] if t_url_idxs else len(target_lines)
            existing = {l.strip() for l in target_lines}
            new_labels = [lab for lab in labels if lab.strip() not in existing]
            if new_labels:
                target_lines[insert_at:insert_at] = new_labels
                out[target_i] = "\n".join(target_lines)
            merged += 1
        else:
            seen_target[key] = len(out)
            out.append(b)
    return out, merged


def dedupe_wordlist_lines(blocks):
    """Inside any block that looks like a flat list of short lines (no
    key:value / heading structure -- e.g. a bare list of hostnames or URLs),
    dedupe individual lines while preserving first-seen order. Tracks seen
    lines GLOBALLY across every listy block (not reset per block), so the
    same entry repeated in two separate list-style blocks still gets caught."""
    out = []
    seen = set()
    for b in blocks:
        lines = b.split("\n")
        if is_listy_block(lines):
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

    blocks = split_blocks(text)
    blocks = merge_stray_continuations(blocks)
    blocks = [dedupe_exact_lines_within_block(b) for b in blocks]
    original_block_count = len(blocks)
    blocks, url_labels_merged = merge_duplicate_url_labels(blocks)
    blocks = dedupe_wordlist_lines(blocks)
    kept, dropped = dedupe_blocks(blocks, args.near_threshold)

    print(f"Duplicate-URL blocks merged (label moved to earlier entry): {url_labels_merged}")
    print(f"Original blocks: {original_block_count}")
    print(f"Kept blocks:     {len(kept)}")
    print(f"Dropped blocks:  {len(dropped)}")
    for i, reason, matched in dropped:
        print(f"  - block #{i} dropped ({reason}), duplicate of block #{matched}")

    new_text = "\n\n".join(kept) + "\n"
    with open(output_path, "w") as f:
        f.write(new_text)

    print(f"\nWrote deduped output to: {output_path}")
    print("Review it yourself before using it in place of the original.")


if __name__ == "__main__":
    main()
