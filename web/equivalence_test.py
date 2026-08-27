#!/usr/bin/env python3
"""
Prove the browser build and the CLI produce the same bytes.

    python web/equivalence_test.py

This is the test for the one promise the project makes: same inputs, same
settings, same output, whichever front end you used. It does not trust that the
two share code -- it runs both and compares the result.

The browser side is not simulated. The generated page's own ``py-driver`` block
is pulled out of gcode_merger_web.html and executed under CPython, and its
``run_merge()`` is called exactly as the page calls it, with the same JSON
config the Tweak card would send. The CLI side runs gcode_merger.py as a
subprocess over an identical copy of the same folder.

Needs real sliced files in testdata/ -- see the README. Without them it skips,
loudly, rather than passing on nothing.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
PROJECT_DIR = WEB_DIR.parent
PAGE = PROJECT_DIR / "gcode_merger_web.html"
CLI = PROJECT_DIR / "gcode_merger.py"
TESTDATA = PROJECT_DIR / "testdata"

# Each case is run twice -- once through the page's run_merge(), once through
# the CLI -- and the two outputs are compared byte for byte. The tweaked cases
# exist because identity settings would pass even if apply_tweaks() were wired
# up wrongly on one side.
CASES = [
    {
        "name": "bambu, no tweaks",
        "inputs": ["PETG.gcode.3mf", "wire6.gcode"],
        "web": {},
        "cli": [],
    },
    {
        "name": "bambu, tweaked",
        "inputs": ["PETG.gcode.3mf", "wire6.gcode"],
        "web": {"speed_multiplier": 0.8, "flow_multiplier": 1.1, "bed_leveling": True},
        "cli": ["--speed-multiplier", "0.8", "--flow-multiplier", "1.1", "--bed-leveling"],
    },
    {
        "name": "marlin, no tweaks",
        "inputs": ["EN4MAX_Cube.gcode", "geometry.gcode"],
        "web": {},
        "cli": [],
    },
    {
        "name": "marlin, tweaked",
        "inputs": ["EN4MAX_Cube.gcode", "geometry.gcode"],
        "web": {"speed_multiplier": 1.25, "flow_multiplier": 0.9, "bed_leveling": False},
        "cli": ["--speed-multiplier", "1.25", "--flow-multiplier", "0.9", "--no-bed-leveling"],
    },
]


def page_driver():
    """The py-driver block out of the generated page, as source."""
    if not PAGE.exists():
        sys.exit(f"ERROR: {PAGE.name} not found. Run: python web/build_web.py")
    m = re.search(r'<script type="text/x-python" id="py-driver">\n(.*?)\n</script>',
                  PAGE.read_text(encoding="utf-8"), re.S)
    if not m:
        sys.exit(f"ERROR: no py-driver block in {PAGE.name}. Was it edited by hand?")
    return m.group(1)


def run_as_page(driver, project, config):
    """Call the page's own run_merge() under CPython, against `project`."""
    harness = (
        f"import sys; sys.path.insert(0, {str(PROJECT_DIR)!r})\n"
        # The page pins PROJECT to the Pyodide filesystem; point it at a real
        # folder instead. Nothing else about the driver is touched.
        + driver.replace('PROJECT = Path("/project")', f'PROJECT = Path({str(project)!r})')
        # The merger logs to stdout, which Pyodide captures; here it would land
        # in front of the JSON, so mark the payload and split on the marker.
        + f"\nprint('@@@' + run_merge({json.dumps(json.dumps(config))}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", harness], capture_output=True, text=True)
    if "@@@" not in proc.stdout:
        return None, (proc.stdout[-1500:] + proc.stderr[-1500:])
    result = json.loads(proc.stdout.split("@@@", 1)[1].strip())
    if not result.get("ok"):
        return None, result.get("error", "unknown error")
    return result, None


def run_as_cli(project, args):
    proc = subprocess.run([sys.executable, str(CLI), str(project)] + args,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return proc.stderr[-1500:]
    return None


def payload(path):
    """
    What to compare.

    A .3mf is a zip, and zip archives embed timestamps, so two runs of the same
    merge never produce identical archives. The merged gcode inside them does,
    and that is the thing that reaches the printer.
    """
    if path.name.endswith(".3mf"):
        with zipfile.ZipFile(path) as z:
            return z.read("Metadata/plate_1.gcode"), "plate_1.gcode inside the .3mf"
    return path.read_bytes(), path.name


def main():
    missing = sorted({f for c in CASES for f in c["inputs"]
                      if not (TESTDATA / f).exists()})
    if missing:
        print(f"SKIPPED: testdata/ is missing {', '.join(missing)}.")
        print("         See the README for what to put there. Nothing was verified.")
        return 0

    driver = page_driver()
    failures = 0

    for case in CASES:
        work = Path(tempfile.mkdtemp(prefix="gcode_equiv_"))
        try:
            web_dir, cli_dir = work / "web", work / "cli"
            for d in (web_dir, cli_dir):
                d.mkdir()
                for f in case["inputs"]:
                    shutil.copy(TESTDATA / f, d / f)

            result, err = run_as_page(driver, web_dir, case["web"])
            if err:
                print(f"[FAIL] {case['name']}: the page's run_merge() failed\n{err}")
                failures += 1
                continue

            err = run_as_cli(cli_dir, case["cli"])
            if err:
                print(f"[FAIL] {case['name']}: the CLI failed\n{err}")
                failures += 1
                continue

            name = Path(result["output_path"]).name
            cli_out = cli_dir / name
            if not cli_out.exists():
                print(f"[FAIL] {case['name']}: the CLI produced no {name} "
                      f"(the page produced one)")
                failures += 1
                continue

            web_bytes, label = payload(web_dir / name)
            cli_bytes, _ = payload(cli_out)

            if web_bytes == cli_bytes:
                print(f"[ OK ] {case['name']}: {label} identical ({len(web_bytes):,} bytes)")
                continue

            print(f"[FAIL] {case['name']}: {label} differs "
                  f"(page {len(web_bytes):,} / cli {len(cli_bytes):,} bytes)")
            wl = web_bytes.decode(errors="replace").splitlines()
            cl = cli_bytes.decode(errors="replace").splitlines()
            shown = 0
            for i, (a, b) in enumerate(zip(wl, cl)):
                if a != b:
                    print(f"       line {i + 1}:")
                    print(f"         page: {a[:110]}")
                    print(f"         cli : {b[:110]}")
                    shown += 1
                    if shown == 5:
                        break
            if not shown:
                print(f"       identical for {min(len(wl), len(cl))} lines, "
                      f"then one output ends")
            failures += 1
        finally:
            shutil.rmtree(work, ignore_errors=True)

    print()
    if failures:
        print(f"{failures} MISMATCH(ES) -- the page and the CLI do not agree.")
        return 1
    print(f"ALL EQUIVALENT ({len(CASES)} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
