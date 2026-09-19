# text-dedupe

A small, dependency-free Python script for cleaning up a messy plain-text
document that accumulated duplicate entries over time — a notes file, a
scratch document, an export from some other tool — where the content is a
mix of shapes: multi-line records, flat lists, and one-off lines, rather
than a uniform table or a list of identical rows.

It never talks to any network or external API. It only ever reads a
plain-text file (or your clipboard) and writes a plain-text file. That's
intentional — this is meant to be safe to run on sensitive personal data
(license keys, account records, anything else) without that data going
anywhere except your own disk.

## Why not just `sort -u` or `awk '!seen[$0]++'`?

Those dedupe whole *lines*. A real-world messy notes file is usually a mix
of several different shapes in one document:

- multi-line **records** (a name, then a few `Label: value` lines) that
  need to be treated as one atomic unit, not deduped line-by-line
- flat **lists** (bare item names, bare URLs) where duplicate lines within
  the list genuinely should be removed
- entries that are *almost* identical but **must never be merged**, because
  the one thing that differs is the part that matters (e.g. two license
  records for the same product with two different serial numbers)

A plain line-based or whole-file dedupe either misses cross-block
duplicates entirely, or is dangerous enough to silently merge two
genuinely different records because the surrounding text happens to be 95%
identical. This script is built around avoiding both failure modes.

## What it does

- Splits the document into blocks separated by blank lines, and treats each
  block as an atomic record.
- Re-attaches a block that starts with a bare continuation label (`License
  Key:`, `Serial:`, `Key:`, `Code:`, `Number:`, `User ID:`, `Registration
  Key:`, `Activation Code:`) back onto the block above it — this heals a
  record that got accidentally split by a stray blank line (a common
  copy/paste artifact).
- Drops an **exact duplicate line within the same block** (e.g. a
  boilerplate `Name:` line accidentally typed twice in one record).
- Drops a block that's an **exact duplicate** of an earlier block.
- Drops a block that's a **near duplicate** of an earlier block (similarity
  ratio ≥ `--near-threshold`, default 0.90) — this catches entries
  duplicated with minor formatting differences (e.g. from a bad sync)
  even when the text isn't byte-identical.
- **Never merges two blocks on text similarity alone if either one contains
  a license-key/serial/ID-number-shaped identifier.** In that case the
  identifiers must match *exactly* for the blocks to be considered
  duplicates at all — this is what keeps "Widget Pro 3" (serial ending
  `-4`) and "Widget Pro 4" (serial ending `-4` too, but a different
  product) from ever being treated as the same entry just because most of
  the surrounding text matches.
- Merges a **label+URL block that duplicates an earlier one**, normalizing
  away a trailing slash and scheme/host case so `https://Example.com/x/`
  and `https://example.com/x` are recognized as the same URL. Rather than
  deleting the duplicate outright, its label is moved up to join the
  first-seen block as an additional alias (inserted right before the URL
  line), and the now-empty duplicate block is dropped entirely — so
  `alpha-tool` / `https://.../x/` followed later by `beta-tool` /
  `https://.../x` becomes one block with both labels stacked above the one
  surviving URL, instead of leaving `beta-tool` behind as an orphan. This
  only applies to a block pairing exactly one URL with label line(s) —
  a block with zero URLs, or more than one, is left untouched as too
  ambiguous to guess about.
- Within any block that looks like a genuine flat list (no `Label:` lines
  anywhere in it, mostly short bare tokens — a record with `Label:` lines
  never qualifies), dedupes individual list lines document-wide while
  preserving first-seen order.
- Only ever prints counts and short markers like `block #4 dropped
  (exact), duplicate of block #1` — never the actual content of what was
  removed or kept.

## Usage

```
# one input file
python3 dedupe_text.py INPUT.txt OUTPUT.txt

# multiple input files (e.g. duplicate copies you exported separately) --
# the last argument is always the output file, everything before it is
# concatenated first and then deduped as one document
python3 dedupe_text.py INPUT1.txt INPUT2.txt INPUT3.txt OUTPUT.txt

# clipboard instead of a file: copy the text, then pass just the output path
python3 dedupe_text.py OUTPUT.txt

# tune how aggressive near-duplicate block matching is (0-1, default 0.90)
python3 dedupe_text.py INPUT.txt OUTPUT.txt --near-threshold 0.85
```

Requires Python 3, no external dependencies (stdlib only). Clipboard mode
uses macOS's `pbpaste`.

## Example

[`examples/input1.txt`](examples/input1.txt) and
[`examples/input2.txt`](examples/input2.txt) are entirely fake data
(made-up names, emails, and keys) standing in for two exports of the same
underlying document — e.g. two backups/syncs taken at different times.
Between them they cover every case the script handles: exact duplicate
records repeated across the two files, overlapping items in a bare word
list, a URL duplicated across the two files (with a trailing-slash
difference), two different products with similar-looking serials that must
stay separate even when one copy is repeated, and a record that got
accidentally split by a stray blank line in one file but not the other.

Run:

```
python3 dedupe_text.py examples/input1.txt examples/input2.txt examples/sample-out.txt
```

Output:

```
Duplicate-URL blocks merged (label moved to earlier entry): 1
Original blocks: 15
Kept blocks:     11
Dropped blocks:  3
  - block #8 dropped (exact), duplicate of block #1
  - block #10 dropped (exact), duplicate of block #5
  - block #12 dropped (exact), duplicate of block #7

Wrote deduped output to: examples/sample-out.txt
```

[`examples/sample-out.txt`](examples/sample-out.txt) is the result — note
that the two "Data Rescue 3" entries with *different* serial numbers both
survive (they're different licenses), "Data Rescue 4" survives (different
product, different serial), items unique to only one input file (like
`brand-new-tool`) are kept, and the "SampleSync Pro" record merges cleanly
into one block even though its `License Key:` line was separated from the
rest by a stray blank line in `input1.txt` but not in `input2.txt`. Also
note `alpha-tool` and `beta-tool` end up stacked as two labels over one
surviving URL — `beta-tool`'s URL (same page, just a trailing-slash
difference) was recognized as a duplicate of `alpha-tool`'s, so its label
was moved up to join `alpha-tool`'s block instead of being left behind as
an orphan.

## Recommended workflow for sensitive content

1. Export the content to a plain-text file (or just copy it to your
   clipboard).
2. Write the result to a scratch output file first, and review that file
   yourself before doing anything else with it.
3. Diff the input and output yourself (e.g. `diff note-in.txt
   note-out.txt`) to see exactly what changed.
4. Once you're satisfied, copy the deduped content back to wherever it
   belongs, and delete the plain-text scratch files — they hold plaintext
   on disk.

## License

MIT — see [LICENSE](LICENSE).
