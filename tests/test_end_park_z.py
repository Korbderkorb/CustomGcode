#!/usr/bin/env python3
"""
The end-of-print park height, for every printer shape the registry can produce.

    python tests/test_end_park_z.py

WHY THIS EXISTS
---------------
The merger lifts clear of the finished print before running the reference's
teardown. Getting that height wrong in one direction drags the nozzle through
the print; wrong in the other it drives the carriage into its own Z limit.

An earlier version measured the print height from the wrong file -- it read the
*reference* object's Z instead of the custom geometry's -- so the lift could be
below the actual print. The response was to abandon clearance entirely and park
at the printer's Z limit every time: unconditionally safe, but a 480 mm crawl on
a tall machine.

The height is now measured twice from the custom moves, independently, and the
two must agree before the clearance park is trusted. This file pins that rule
down: the arithmetic, the clamp, the floor, the override, and -- the important
one -- that a disagreement falls back to the old safe behaviour instead of
guessing.

The last group runs the rule against every printer in the registry, so a printer
added later cannot quietly produce a park height that is below the print or above
the machine.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gcode_merger as gm
import printer_profiles

CLEARANCE = gm.END_PARK_CLEARANCE_MM
FLOOR = gm.END_PARK_MIN_Z_MM

failures = 0


def check(name, cond, extra=None):
    global failures
    if cond:
        print(f"  PASS  {name}")
    else:
        failures += 1
        print(f"  FAIL  {name}" + (f"  -> {extra}" if extra is not None else ""))


class FakeMerger:
    """
    The smallest thing _resolve_end_park_z needs.

    It reads self.report (for the volume checker's independent measurement) and
    logs; nothing else. Binding the real method to this keeps the test on the
    actual implementation rather than a copy of it.
    """

    def __init__(self, checked_z, height_source="printer registry (test)"):
        self.report = {
            "build_volume": {"geometry_z": [0.0, checked_z]},
            "warnings": [],
        }
        self._height_source = height_source

    def log(self, *_args, **_kwargs):
        pass

    def _get_height_source(self):
        return self._height_source

    resolve = gm.GCodeMerger._resolve_end_park_z


def resolve(measured, printable_height, checked=None):
    """Run the real resolver. `checked` defaults to agreeing with `measured`."""
    m = FakeMerger(measured if checked is None else checked)
    park, source = m.resolve(measured, printable_height)
    return park, source, m.report["warnings"]


print("\n[1] the ordinary case: lift a fixed distance above the print")
park, source, warns = resolve(76.55, 480.0)
check("parks CLEARANCE above the print", abs(park - (76.55 + CLEARANCE)) < 1e-9, park)
check("well short of the machine limit", park < 480.0, park)
check("source names the rule", "above the print" in source, source)
check("no warnings", warns == [], warns)

park, _, _ = resolve(118.55, 250.0)
check("same rule on a shorter machine", abs(park - (118.55 + CLEARANCE)) < 1e-9, park)


print("\n[2] a tiny print still parks at a sane height")
park, _, _ = resolve(0.6, 480.0)
check("floored at END_PARK_MIN_Z_MM", park == FLOOR, park)
check("floor is above the print", park > 0.6)


print("\n[3] a print near the ceiling clamps to the machine, and says so")
park, source, warns = resolve(476.0, 480.0)
check("never commands past the Z limit", park == 480.0, park)
check("source explains the clamp", "Z limit" in source, source)
check("warns about the reduced clearance", any("clearance is only" in w for w in warns), warns)

park, _, warns = resolve(460.0, 480.0)
check("exactly CLEARANCE below the limit is not clamped",
      abs(park - 480.0) < 1e-9, park)
check("and does not warn", warns == [], warns)


print("\n[4] unknown machine height: still clears the print, cannot clamp")
park, _, warns = resolve(76.55, None)
check("uses the clearance rule", abs(park - (76.55 + CLEARANCE)) < 1e-9, park)
check("no fallback warning", warns == [], warns)


print("\n[5] the cross-check is what makes this safe")
# This is the old bug: the measured height comes from somewhere else and is
# wrong. Parking at measured+20 would be BELOW the real print.
park, source, warns = resolve(100.0, 480.0, checked=76.55)
check("disagreement falls back to the printer Z limit", park == 480.0, park)
check("does not park on the bad measurement", park != 100.0 + CLEARANCE, park)
check("says why in a warning", any("disagree" in w for w in warns), warns)

park, _, warns = resolve(0.0, 480.0, checked=0.0)
check("no measured geometry also falls back", park == 480.0, park)
check("and warns", any("no geometry height" in w for w in warns), warns)

park, _, _ = resolve(76.55, None, checked=100.0)
check("fallback with no known limit uses the documented constant",
      park == gm.END_PARK_FALLBACK_Z, park)

# Agreement is exact arithmetic over the same moves, so the tolerance is tight.
park, _, warns = resolve(76.55, 480.0, checked=76.5500001)
check("floating-point noise still counts as agreement",
      abs(park - (76.55 + CLEARANCE)) < 1e-6, park)
park, _, warns = resolve(76.55, 480.0, checked=76.75)
check("a real 0.2 mm divergence does not", park == 480.0, park)


print("\n[6] every printer in the registry, at several print heights")
reg = printer_profiles.PRINTER_PROFILES
bad = []
for slug, prof in reg.items():
    limit = float(prof["height"])
    for frac in (0.001, 0.01, 0.25, 0.5, 0.9, 0.99, 1.0):
        measured = limit * frac
        park, _, _ = resolve(measured, limit)
        if park < measured:                      # would drag through the print
            bad.append((slug, measured, park, "below print"))
        if park > limit:                         # would drive into the Z limit
            bad.append((slug, measured, park, "above machine"))
        if park < FLOOR and measured < FLOOR and limit >= FLOOR:
            bad.append((slug, measured, park, "under floor"))
check(f"all {len(reg)} printers x 7 heights stay between print and limit",
      not bad, bad[:5])

tall = max(reg.items(), key=lambda kv: kv[1]["height"])
short = min(reg.items(), key=lambda kv: kv[1]["height"])
print(f"        tallest: {tall[0]} ({tall[1]['height']} mm), "
      f"shortest: {short[0]} ({short[1]['height']} mm)")

# A machine shorter than the floor is not in the registry today, but nothing
# stops one being added. The clamp must still win over the floor.
park, _, _ = resolve(5.0, 20.0)
check("clamp beats the floor on a very short machine", park == 20.0, park)


print("\n[7] end_park_z override still wins outright")
# The override is applied before the resolver is consulted, so this asserts the
# call site's contract rather than the resolver's.
src = Path(__file__).resolve().parent.parent / "gcode_merger.py"
text = src.read_text(encoding="utf-8")
check("override is checked first",
      'override = self.config.get("end_park_z")' in text
      and text.index('override = self.config.get("end_park_z")')
          < text.index("park_z, z_source = self._resolve_end_park_z"))

print()
print("ALL PARK-Z CHECKS PASSED" if not failures else f"{failures} CHECK(S) FAILED")
sys.exit(1 if failures else 0)
