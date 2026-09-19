# onepassword-note-dedupe

A small, dependency-free Python script for cleaning up a messy plain-text
secure note (the kind of freeform note that accumulates duplicate entries
over time — often from a bad password-manager sync) — for example a
1Password "Secure Note" full of AppleCare records, software license keys,
and bare lists of app names or URLs, with the occasional duplicate block or
duplicate line mixed in.

It never talks to any network, API, or password-manager CLI. It only ever
reads a plain-text file (or your clipboard) and writes a plain-text file.
That's intentional — this is meant to be safe to run on sensitive personal
data without that data going anywhere except your own disk.

## Why not just `sort -u` or `awk '!seen[$0]++'`?

Those dedupe whole *lines*. A secure note like this is really a mix of
several different shapes in one file:

- multi-line **records** (a product name, then a few `Label: value` lines)
  that need to be treated as one atomic unit, not deduped line-by-line
- flat **lists** (bare app names, bare URLs) where duplicate lines within
  the list genuinely should be removed
- entries that are *almost* identical but **must never be merged**, because
  the one thing that differs is the part that matters (e.g. two software
  licenses for the same product with two different serial numbers)

A plain line-based or whole-file dedupe either misses cross-block
duplicates entirely, or is dangerous enough to silently merge two
genuinely different license keys because the surrounding text happens to
be 95% identical. This script is built around avoiding both failure modes.

## What it does

- Splits the note into blocks separated by blank lines, and treats each
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
  ratio ≥ `--near-threshold`, default 0.90) — this catches the "duplicated
  a couple of times because of a bad sync" case even when the text isn't
  byte-identical.
- **Never merges two blocks on text similarity alone if either one contains
  a license-key/serial/agreement-number-shaped identifier.** In that case
  the identifiers must match *exactly* for the blocks to be considered
  duplicates at all — this is what keeps "Data Rescue 3" (serial ending
  `-4`) and "Data Rescue 4" (serial ending `-4` too, but a different
  product) from ever being treated as the same entry just because most of
  the surrounding text matches.
- Drops duplicate **URLs**, matched document-wide (not just within one
  block), normalizing away a trailing slash and scheme/host case so
  `https://Example.com/x/` and `https://example.com/x` are recognized as
  the same URL.
- Within any block that looks like a genuine flat list (no `Label:` lines
  anywhere in it, mostly short bare tokens — the AppleCare-style record
  above never qualifies, since it has `Label:` lines), dedupes individual
  list lines document-wide while preserving first-seen order.
- Only ever prints counts and short markers like `block #4 dropped
  (exact), duplicate of block #1` — never the actual content of what was
  removed or kept.

## Usage

```
# one input file
python3 dedupe_note.py INPUT.txt OUTPUT.txt

# multiple input files (e.g. duplicate copies you exported separately) --
# the last argument is always the output file, everything before it is
# concatenated first and then deduped as one document
python3 dedupe_note.py INPUT1.txt INPUT2.txt INPUT3.txt OUTPUT.txt

# clipboard instead of a file: copy the note's text, then pass just the
# output path
python3 dedupe_note.py OUTPUT.txt

# tune how aggressive near-duplicate block matching is (0-1, default 0.90)
python3 dedupe_note.py INPUT.txt OUTPUT.txt --near-threshold 0.85
```

Requires Python 3, no external dependencies (stdlib only). Clipboard mode
uses macOS's `pbpaste`.

## Example

[`examples/sample-input.txt`](examples/sample-input.txt) is entirely fake
data (made-up names, emails, and license keys) shaped like a real secure
note, showing every case the script handles: exact duplicate records, an
AppleCare-style entry duplicated by a bad sync, a bare word list with a
repeat, a URL duplicated across two different blocks (with a trailing-slash
difference), two different products with similar-looking serials that must
stay separate, and a record accidentally split by a stray blank line.

Run:

```
python3 dedupe_note.py examples/sample-input.txt examples/sample-output.txt
```

Output:

```
Duplicate URL lines removed (document-wide): 1
Original blocks: 13
Kept blocks:     11
Dropped blocks:  2
  - block #2 dropped (exact), duplicate of block #1
  - block #10 dropped (exact), duplicate of block #7

Wrote deduped output to: examples/sample-output.txt
```

[`examples/sample-output.txt`](examples/sample-output.txt) is the result —
note that the two "Data Rescue 3" entries with *different* serial numbers
both survive (they're different licenses), "Data Rescue 4" survives
(different product, different serial), and the "SampleSync Pro" record
stays intact as one block even though its `License Key:` line was
originally separated from the rest by a blank line in the input.

## Recommended workflow for a real secure note

1. Export the note's content to a plain-text file (or just copy it to your
   clipboard).
2. Run the script in `--dry-run`-style fashion by writing to a scratch
   output file first, and review that file yourself before doing anything
   else with it.
3. Diff the input and output yourself (e.g. `diff note-in.txt
   note-out.txt`) to see exactly what changed.
4. Once you're satisfied, copy the deduped content back into your password
   manager by hand, and delete the plain-text scratch files — they hold
   plaintext on disk.

## License

MIT — see [LICENSE](LICENSE).
