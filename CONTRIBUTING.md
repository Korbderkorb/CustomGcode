# Contributing

Read `README.md` first — it describes what this project is, how both front ends are
used, and why the design is the way it is. This file holds the rules that are easy to
break while editing it.

Nothing here is Grasshopper-side. The `.gh` definitions are edited in Grasshopper and
committed as-is; the notes below are about the merger and the browser build.

## The one rule

`gcode_merger_web.html` is **generated**. Never edit it.

Edit `gcode_merger.py`, `printer_profiles.py` or `web/template.html`, then:

```bash
python web/build_web.py
```

A change that lands only in the HTML is a change the CLI does not have. That is exactly
the failure this repo is arranged to prevent, and it is invisible until a print goes
wrong.

## Where logic belongs

Merge logic, the pre-flight analysis and the tweak arithmetic live in `gcode_merger.py`.
The page's `py-driver` block is glue: it moves files into the Pyodide filesystem and
turns return values into JSON. If you find yourself writing a calculation in the driver
or in JavaScript, it belongs in the module instead — that is the only thing keeping the
two front ends equivalent.

Concretely, both front ends go through the same entry points:

| Feature | Shared implementation |
|---|---|
| Merge | `GCodeMerger.run()` |
| Analyze card / `--analyze` | `GCodeMerger.analyze()` |
| Tweak card / `--speed-multiplier` etc. | `GCodeMerger.apply_tweaks()` |
| Printer table | `printer_profiles.as_dict()` |

`apply_tweaks()` defaults to the identity, so a bare `python gcode_merger.py <dir>` is
unaffected by its existence. Keep it that way.

## Adding a printer

One entry in `PRINTER_PROFILES` in `printer_profiles.py`, then rebuild. No JS change, no
merge-logic change. The page reads the same table through `as_dict()`.

## After changing anything

```bash
python web/build_web.py
python tests/test_end_park_z.py
python web/equivalence_test.py
node web/ui_test.js
```

`tests/test_end_park_z.py` needs no test data and runs in a second — it checks the
end-of-print lift against every printer in the registry. Run it after touching
`_resolve_end_park_z`, the `END_PARK_*` constants, or anything that feeds
`max_z_geometry`.

`equivalence_test.py` is the one that guards the promise: it runs the same merge through
the page's `run_merge()` and through the CLI and compares the bytes. If you touch
`apply_tweaks()`, `analyze()`, or anything either front end passes into them, run it.

Both suites need real sliced files in `testdata/` and skip without them, so a pass with
skips is not a full pass — see the README for what to put there.

## Style

Match the surrounding code. The existing comments explain *why* a thing is done, not what
the line does — particularly where a decision looks arbitrary but is load-bearing (the
file-over-registry priority, transform-only animations, `;GH_CONFIG` resolution order).
Keep that standard; those comments are the reason the tricky parts survive editing.

The `fmtSecs()` in `web/template.html` mirrors Python's `format_duration()`. If either
changes, change both.
