#!/usr/bin/env python3
"""
Build the standalone browser version of the GCode merger.

Injects gcode_merger.py into web/template.html and writes a single
self-contained gcode_merger_web.html at the project root.

    python web/build_web.py

Why a build step instead of pasting the Python into the HTML: the merger stays
single-source. Edit gcode_merger.py (the CLI keeps working), rerun this, and the
browser build picks the change up. Nothing to keep in sync by hand.

The only external thing the generated page fetches is the Pyodide runtime from
jsDelivr; see the README for going fully offline.
"""

import sys
from pathlib import Path

# Pyodide release to load in the browser. Pyodide's version scheme tracks the
# bundled CPython (314.x.y == CPython 3.14). Bump deliberately and re-test:
# the merger is plain stdlib, so upgrades are normally uneventful.
PYODIDE_VERSION = "314.0.3"

WEB_DIR = Path(__file__).resolve().parent
PROJECT_DIR = WEB_DIR.parent

TEMPLATE = WEB_DIR / "template.html"
SOURCE = PROJECT_DIR / "gcode_merger.py"
PROFILES = PROJECT_DIR / "printer_profiles.py"
OUTPUT = PROJECT_DIR / "gcode_merger_web.html"

PLACEHOLDER = "__GCODE_MERGER_PY__"
PROFILES_PLACEHOLDER = "__PRINTER_PROFILES_PY__"

# The two .py files at the repo root ARE the CLI. They are injected verbatim, so
# the page cannot drift from the shell as long as the page is only ever produced
# by this script -- which is why editing gcode_merger_web.html by hand is a bug,
# not a shortcut. _check_stale() catches the case where someone did.


def _check_stale(output, sources):
    """Warn if the committed page is older than anything that feeds it."""
    if not output.exists():
        return
    built = output.stat().st_mtime
    stale = [s.name for s in sources if s.stat().st_mtime > built]
    if stale:
        print(f"NOTE: {output.name} was older than {', '.join(stale)} "
              f"-- rebuilding now.", file=sys.stderr)


def main():
    for path in (TEMPLATE, SOURCE, PROFILES):
        if not path.exists():
            print(f"ERROR: missing {path}", file=sys.stderr)
            return 1

    template = TEMPLATE.read_text(encoding="utf-8")
    source = SOURCE.read_text(encoding="utf-8")
    profiles = PROFILES.read_text(encoding="utf-8")

    _check_stale(OUTPUT, (TEMPLATE, SOURCE, PROFILES))

    # The Python is embedded in <script type="text/x-python"> blocks. HTML ends
    # a raw-text element at the first '</script', so that sequence anywhere in
    # the Python would silently truncate the page. Fail loudly instead.
    for name, text in (("gcode_merger.py", source), ("printer_profiles.py", profiles)):
        if "</script" in text.lower():
            print(f"ERROR: {name} contains '</script', which would break the "
                  "embedded <script> block. Split the literal before embedding.",
                  file=sys.stderr)
            return 1

    for placeholder in (PLACEHOLDER, PROFILES_PLACEHOLDER):
        if placeholder not in template:
            print(f"ERROR: {placeholder} not found in {TEMPLATE.name}", file=sys.stderr)
            return 1

    html = template.replace(PLACEHOLDER, source)
    html = html.replace(PROFILES_PLACEHOLDER, profiles)
    html = html.replace("__PYODIDE_VERSION__", PYODIDE_VERSION)

    # newline="" disables Windows' \n -> \r\n translation. The sources are all
    # LF, so without this the generated page is the one CRLF file in the repo:
    # a needlessly noisy diff, and a build whose output depends on the OS it ran
    # on rather than only on its inputs.
    OUTPUT.write_text(html, encoding="utf-8", newline="")

    print(f"Built {OUTPUT}")
    print(f"  merger source : {len(source):,} bytes ({SOURCE.name})")
    print(f"  printer table : {len(profiles):,} bytes ({PROFILES.name})")
    print(f"  page total    : {len(html):,} bytes")
    print(f"  pyodide       : {PYODIDE_VERSION}")
    print("\nOpen it by double-clicking the file. First run downloads the")
    print("Pyodide runtime (~10 MB, cached by the browser afterwards).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
