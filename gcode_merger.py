#!/usr/bin/env python3
"""
GCode Merger - custom Grasshopper geometry into a sliced reference file.

Two reference formats are supported, and they take the SAME path through the
merge. Only the container and the places the machine facts are read from
differ:

  * BambuStudio '.gcode.3mf' -- a zip whose Metadata/plate_1.gcode carries
    HEADER/CONFIG/EXECUTABLE block markers and a full '; key = value' config
    block. Output is a '.gcode.3mf'.
  * Plain Marlin-flavour '.gcode' (Cura, PrusaSlicer, OrcaSlicer, ...) -- one
    text file with no block markers and, from Cura, no machine config at all.
    Output is a '<name>_merged.gcode'.

Everything downstream of parsing is format-independent: geometry extraction,
per-point FLOW / SPEED / PAUSE / TRAVEL, retraction planning, the fatal
build-volume check, and the end-of-print Z safety all run identically.

- Extracts ONLY geometry from the custom file (filters out all preamble)
- Reverse-engineers E-per-mm from the reference itself (see
  _extract_test_extrusion_reference)
- Preserves the reference's own startup/teardown sequences verbatim
"""

import os
import sys
import json
import math
import shutil
import zipfile
import hashlib
import tempfile
import argparse
from pathlib import Path
from datetime import datetime

try:
    import yaml
except ImportError:
    yaml = None  # optional: only used for the legacy config.yaml fallback

# Printer/firmware registry. Shared verbatim with the browser build, which
# writes this module into the Pyodide filesystem before importing the merger.
try:
    import printer_profiles
except ImportError:  # pragma: no cover - only if the file was moved away
    printer_profiles = None


# --- Global limits / defaults (formerly in config.yaml) ---
SPEED_MIN_MMS = 3.0        # clamp floor for effective print speed
SPEED_MAX_MMS = 300.0      # clamp ceiling for effective print speed
MAX_VOL_RATE = 20.0        # mm^3/s volumetric-flow warning threshold
DEFAULT_FILAMENT_D = 1.75  # mm, fallback if reference has no filament_diameter
DEFAULT_FILAMENT_DENSITY = 1.24  # g/cm^3, PLA fallback if reference declares none
DEFAULT_SPEED_MMS = 100.0  # fallback base speed if no ;GH_CONFIG / config.yaml
DEFAULT_FLOW = 1.0         # fallback global flow multiplier
# Keep the reference's own bed-levelling commands unless something explicitly
# turns them off. Preserving the reference start block verbatim is the whole
# premise of the merger, and a silent strip is the more dangerous default --
# especially on Bambu, whose start block is not a plain G29. The browser build
# resolves this identically, so both front ends agree when nothing is declared.
DEFAULT_BED_LEVELING = True

# --- Approach to the first print point (never extrudes) ---
APPROACH_CLEARANCE_MM = 5.0  # travel this far above the first point's Z
MIN_TRAVEL_Z_MM = 10.0       # ...but never travel lower than this

# --- Build-volume validation ---
VOLUME_TOL_MM = 0.001  # absorbs the generator's 3-decimal rounding

# --- Per-point pauses (;GH PAUSE=<ms>) ---
PAUSE_CONFIRM = -1           # sentinel: wait for confirmation on the printer display
DEFAULT_PAUSE_GCODE = "M400 U1"  # fallback if the reference declares no machine_pause_gcode
LONG_PAUSE_WARN_MS = 10000   # warn about ooze/heat above this dwell

# --- Retraction fallbacks, used only if the reference declares none ---
FALLBACK_RETRACT_LENGTH = 0.8    # mm
FALLBACK_RETRACT_SPEED = 30.0    # mm/s
FALLBACK_MIN_TRAVEL_DIST = 1.0   # mm - runs shorter than this never retract

# --- Calibrating E-per-mm from a plain .gcode reference ---
# A Marlin reference has no 'nozzle load line' to reverse-engineer, and its
# purge line is useless for the job: Cura's is a deliberately fat prime bead
# (G1 X265 E30 = 0.30 E/mm, roughly 4x a real print line). So the rate is taken
# from the reference's OWN printing moves instead, which is the honest analogue
# of "what this slicer lays down per mm at these settings".
CAL_MIN_SEGMENT_MM = 0.5     # ignore short segments: 3-decimal rounding dominates them
CAL_MAX_SAMPLES = 40000      # plenty for a stable median, keeps huge files fast

# --- Marlin output ---
MARLIN_OUTPUT_SUFFIX = "_merged"  # custom "wire.gcode" -> output "wire_merged.gcode"

# --- End-of-print park ---
# The nozzle is lifted clear of the finished print before the reference teardown
# runs. Parking at the printer's Z limit also clears it, but on a tall machine
# that is a long crawl to the very top of travel -- on a 480 mm gantry it ends
# with the carriage pressed against its own limit. Lifting a fixed distance
# above the print clears it just as certainly and stops well short.
#
# This depends on knowing the print's true height, which is why it is
# cross-checked: see _resolve_end_park_z.
END_PARK_CLEARANCE_MM = 20.0  # lift this far above the highest geometry Z
END_PARK_MIN_Z_MM = 30.0      # ...but never park lower than this
END_PARK_FALLBACK_Z = 250.0   # last resort when the machine's Z limit is unknown
# The two independent measurements of geometry height must agree this closely
# for the clearance park to be trusted. They are the same arithmetic over the
# same moves, so any real disagreement means one of them is wrong.
Z_AGREEMENT_TOL_MM = 0.01


def format_duration(seconds):
    """Human-readable duration, e.g. '1h 04m', '12m 30s', '45s'."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "n/a"
    if seconds < 0 or seconds != seconds:  # negative or NaN
        return "n/a"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class PrintVolumeError(Exception):
    """Raised when custom geometry falls outside the printer's usable build volume."""


class ReferenceFormatError(Exception):
    """Raised when a reference file cannot be understood well enough to merge."""


class GCodeMerger:
    """Merge custom geometry with a BambuStudio reference, with per-step FLOW + SPEED."""

    def __init__(self, project_dir, config_path, dry_run=False, config=None):
        self.project_dir = Path(project_dir)
        self.script_dir = Path(__file__).parent
        self.config_path = Path(config_path) if config_path else None
        self.dry_run = dry_run

        # Validation
        if not self.project_dir.exists():
            raise FileNotFoundError(f"Project directory not found: {self.project_dir}")

        # config.yaml is now OPTIONAL. Global settings normally come from the custom
        # file's ;GH_CONFIG header; config.yaml (if present) is only a fallback.
        # An explicit dict (CLI flags, or the web page's printer dialog) is
        # layered ON TOP of the yaml rather than replacing it: it is the user
        # telling us about the machine directly, but a config.yaml that also
        # sets, say, end_park_z should keep working alongside it.
        self.config = self._load_config()
        if config:
            self.config.update(config)

        # Find and classify the input files (see _find_inputs).
        self._find_inputs()

        # Resolve global settings: ;GH_CONFIG header > config.yaml > built-in defaults
        self.settings = self._resolve_settings()

        # Temp directory for extraction (.3mf references only)
        self.temp_dir = None

        # Reference gcode, loaded once by _load_reference() and read by every
        # config getter. Keeping ONE accessor is what let the Marlin path drop
        # in: the getters no longer know where the bytes came from.
        self.ref_lines = []
        self.printer = None       # printer_profiles.detect_printer() result
        self.firmware = None      # resolved firmware profile

        # Report data
        self.report = {
            "timestamp": datetime.now().isoformat(),
            "project": self.project_dir.name,
            "reference_file": self.reference_file.name,
            "reference_format": self.ref_format,
            "custom_file": self.custom_gcode.name,
            "moves_processed": 0,
            "e_values_recalculated": 0,
            "f_values_updated": 0,
            "extrusion_stats": {},
            "extracted_settings": {},
            "test_extrusion_reference": {},
            "z_safety_info": {},
            "warnings": [],
            "errors": []
        }

    def _load_config(self):
        """Load configuration from yaml file (optional; returns {} if absent)."""
        if self.config_path is None or not self.config_path.exists():
            return {}
        if yaml is None:
            self.log("PyYAML not available; ignoring config.yaml "
                     "(;GH_CONFIG / defaults are used instead)", "WARNING")
            return {}
        try:
            with open(self.config_path, 'r') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            self.log(f"Could not read config file ({e}); using ;GH_CONFIG / defaults", "WARNING")
            return {}

    # ------------------------------------------------------------------
    # Input discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_grasshopper(path):
        """
        Is this .gcode the Grasshopper geometry export rather than a reference?

        Two files in one folder can now BOTH be plain .gcode -- the Marlin
        reference and the custom geometry -- so filename order is no longer a
        safe way to tell them apart ("EN4MAX_Cube.gcode" sorts before
        "wire.gcode" and would be mistaken for the custom file).

        The generator's own markers are the reliable signal, and the fallback
        for an untagged custom file is the *absence* of a slicer preamble:
        a geometry-only export has no temperature, homing or slicer-banner
        commands anywhere in its first lines.
        """
        gh_markers = 0
        slicer_markers = 0
        try:
            with open(path, 'r', errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 400:
                        break
                    s = line.strip()
                    up = s.upper()
                    if up.startswith(";GH_CONFIG") or up.startswith(";GH_ORIGIN") \
                            or up.startswith(";GH_SETTINGS") or ";GH " in up:
                        gh_markers += 1
                    if (up.startswith(";FLAVOR:") or up.startswith(";GENERATED WITH")
                            or up.startswith("; GENERATED BY") or up.startswith(";TARGET_MACHINE")
                            or up.startswith("M104") or up.startswith("M109")
                            or up.startswith("M140") or up.startswith("M190")
                            or up.startswith("G28") or up.startswith(";LAYER_COUNT")):
                        slicer_markers += 1
        except Exception:
            return False
        if gh_markers:
            return True
        return slicer_markers == 0

    def _find_inputs(self):
        """
        Locate the custom geometry file, the reference, and the output path.

        The custom file is resolved FIRST because the output name derives from
        it, and the output then has to be excluded from the reference search --
        otherwise a repeat run in the same folder consumes its own previous
        output as the reference and produces a doubled, corrupt file.

        Reference preference is .3mf over .gcode: if a folder holds both, the
        BambuStudio project is unambiguously the reference.
        """
        gcodes = [p for p in sorted(self.project_dir.glob("*.gcode"))
                  if p.suffix == ".gcode"]
        if not gcodes:
            raise FileNotFoundError(f"No .gcode file found in {self.project_dir}")

        # A previous Marlin run's output is never an input.
        prior_outputs = {p for p in gcodes if p.stem.endswith(MARLIN_OUTPUT_SUFFIX)}
        candidates = [p for p in gcodes if p not in prior_outputs] or gcodes

        # The browser build knows which file went in which drop zone, so it
        # names them outright. Sniffing is only for the CLI, which has nothing
        # but a directory to go on.
        named_custom = self.config.get("custom_file")
        if named_custom:
            match = [p for p in gcodes if p.name == named_custom]
            if not match:
                raise FileNotFoundError(
                    f"Custom file '{named_custom}' not found in {self.project_dir}")
            self.custom_gcode = match[0]
        else:
            customs = [p for p in candidates if self._looks_like_grasshopper(p)]
            self.custom_gcode = customs[0] if customs else candidates[0]

        named_ref = self.config.get("reference_file")
        if named_ref:
            three_mf = [p for p in self.project_dir.glob("*.gcode.3mf")
                        if p.name == named_ref]
            plain_refs = [p for p in gcodes
                          if p.name == named_ref and p != self.custom_gcode]
            if not three_mf and not plain_refs:
                raise FileNotFoundError(
                    f"Reference file '{named_ref}' not found in {self.project_dir}")
        else:
            three_mf = [p for p in sorted(self.project_dir.glob("*.gcode.3mf"))]
            plain_refs = [p for p in candidates if p != self.custom_gcode]

        if three_mf:
            self.ref_format = "3mf"
            self.output_path = self.project_dir / f"{self.custom_gcode.stem}.gcode.3mf"
            refs = [p for p in three_mf if p.name != self.output_path.name]
            if not refs:
                raise FileNotFoundError(
                    f"No reference .gcode.3mf found in {self.project_dir} "
                    f"(this run's output, {self.output_path.name}, is never used "
                    f"as the reference)")
            self.reference_file = refs[0]
        elif plain_refs:
            self.ref_format = "gcode"
            self.output_path = (self.project_dir /
                                f"{self.custom_gcode.stem}{MARLIN_OUTPUT_SUFFIX}.gcode")
            self.reference_file = plain_refs[0]
        else:
            raise FileNotFoundError(
                f"No reference file found in {self.project_dir}. Expected either a "
                f"BambuStudio '.gcode.3mf' or a sliced Marlin '.gcode' alongside the "
                f"Grasshopper file ({self.custom_gcode.name}).")

        # Kept for backwards compatibility with callers/scripts that read these.
        self.reference_3mf = self.reference_file if self.ref_format == "3mf" else None
        self.output_3mf = self.output_path if self.ref_format == "3mf" else None
        self.report_path = None

    def _find_file(self, pattern, exclude_3mf=False, exclude=None):
        """
        Find first file matching pattern in project directory.

        exclude_3mf -- skip .3mf files (so "*.gcode" can't grab a "*.gcode.3mf")
        exclude     -- a Path to skip, used to keep this run's own output from
                       being selected as the reference on a repeat run
        """
        for file in sorted(self.project_dir.glob(pattern)):
            if exclude_3mf and file.suffix == ".3mf":
                continue
            if exclude is not None and file.name == Path(exclude).name:
                continue
            return file
        return None

    # ------------------------------------------------------------------
    # Reference access -- ONE accessor, two containers
    # ------------------------------------------------------------------

    def _load_reference(self):
        """
        Read the reference gcode into self.ref_lines, whatever it is packed in.

        A .3mf is unzipped into a temp dir first (the rest of the 3mf payload is
        needed later to rebuild the archive). A plain .gcode is read directly.
        After this, nothing downstream needs to know which it was.
        """
        if self.ref_format == "3mf":
            self.temp_dir = Path(tempfile.mkdtemp(prefix="gcode_merge_"))
            self.log(f"Extracting to: {self.temp_dir}")
            self._extract_3mf()
            path = self.temp_dir / "Metadata" / "plate_1.gcode"
            if not path.exists():
                raise ReferenceFormatError(
                    f"{self.reference_file.name} is a .3mf but contains no "
                    f"Metadata/plate_1.gcode. Export it from BambuStudio with "
                    f"'Export plate sliced file', not 'Export project'.")
        else:
            path = self.reference_file

        with open(path, 'r', errors="replace") as f:
            self.ref_lines = f.readlines()

        # Config values live in the CONFIG block when the reference has one;
        # otherwise any '; key = value' comment in the file is fair game
        # (PrusaSlicer/Orca write theirs as a footer, Cura writes none at all).
        config_lines = []
        in_config = False
        saw_block = False
        for line in self.ref_lines:
            if "CONFIG_BLOCK_START" in line:
                in_config, saw_block = True, True
                continue
            if "CONFIG_BLOCK_END" in line:
                in_config = False
                continue
            if in_config:
                config_lines.append(line)
        self._ref_config_lines = config_lines if saw_block else self.ref_lines

        self._detect_printer()

    def _ref_config_value(self, key):
        """
        Read '; key = value' from the reference's config lines.

        The '= ' form is matched deliberately. BambuStudio writes some keys
        twice -- '; filament_diameter: 1.75' in the HEADER block and
        '; filament_diameter = 1.75' in the CONFIG block -- and matching the
        header form first silently threw away the real value.
        """
        needle = "; " + key
        for line in self._ref_config_lines:
            s = line.strip()
            if s.startswith(needle) and "=" in s:
                head, _, val = s.partition("=")
                if head.strip() == needle:
                    return val.strip()
        return None

    def _ref_config_float(self, key):
        """_ref_config_value as a float, taking the first item of a list value."""
        val = self._ref_config_value(key)
        if val is None:
            return None
        if "," in val:
            val = val.split(",")[0]
        try:
            return float(val)
        except ValueError:
            return None

    def _detect_printer(self):
        """
        Identify the machine and its firmware family from the reference.

        Only used where the reference itself is silent. A Bambu .3mf declares
        its own printable_area / printable_height / machine_pause_gcode and
        those always win; a Cura .gcode declares none of them, and then the
        registry match is the only thing standing between the geometry and an
        unchecked build volume.
        """
        if printer_profiles is None:
            self.printer = {"slug": None, "profile": None, "declared_name": None}
            self.firmware = {"label": "Unknown / other", "pause_gcode": None,
                             "parks_on_pause": True, "note": ""}
            self.report["printer"] = {"detected": None, "source": "registry unavailable"}
            return

        self.printer = printer_profiles.detect_printer(self.ref_lines)

        # Firmware: explicit override > registry match > format default.
        fw_key = self.config.get("firmware")
        fw_source = "override"
        if not fw_key:
            if self.printer["profile"]:
                fw_key = self.printer["profile"]["firmware"]
                fw_source = f"registry ({self.printer['profile']['label']})"
            elif self.ref_format == "3mf":
                fw_key = "bambu"
                fw_source = "reference is a BambuStudio .3mf"
            else:
                fw_key = printer_profiles.DEFAULT_FIRMWARE
                fw_source = "default for an unidentified Marlin-flavour file"
        self.firmware = printer_profiles.firmware_profile(fw_key)
        self.firmware_key = fw_key

        if self.printer["slug"]:
            p = self.printer["profile"]
            self.log(f"Printer: {p['label']} "
                     f"(declared as '{self.printer['declared_name']}') | "
                     f"bed {p['bed'][0]}x{p['bed'][1]}x{p['height']} mm | "
                     f"firmware {self.firmware['label']}", "INFO")
        else:
            self.log(f"Printer: not identified"
                     + (f" (reference declares '{self.printer['declared_name']}')"
                        if self.printer.get("declared_name") else "")
                     + f" | firmware assumed {self.firmware['label']} [{fw_source}]",
                     "INFO")

        self.report["printer"] = {
            "detected": self.printer["profile"]["label"] if self.printer["slug"] else None,
            "slug": self.printer["slug"],
            "declared_name": self.printer.get("declared_name"),
            "firmware": fw_key,
            "firmware_label": self.firmware["label"],
            "firmware_source": fw_source,
        }

    def _parse_gh_config(self):
        """
        Read the ';GH_CONFIG' header written by the Grasshopper generator, e.g.
            ;GH_CONFIG SPEED_MMS=100 FLOW=1.0 BED_LEVELING=0
        Returns a dict with any of: speed_mm_s, flow, bed_leveling. Empty if absent.
        """
        found = {}
        try:
            with open(self.custom_gcode, 'r') as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    # The config marker lives in the header; stop at the first real move.
                    if s.startswith("G0 ") or s.startswith("G1 "):
                        break
                    if s.upper().startswith(";GH_CONFIG"):
                        for tok in s.split()[1:]:
                            if "=" not in tok:
                                continue
                            k, v = tok.split("=", 1)
                            k = k.upper()
                            try:
                                if k == "SPEED_MMS":
                                    found["speed_mm_s"] = float(v)
                                elif k == "FLOW":
                                    found["flow"] = float(v)
                                elif k == "BED_LEVELING":
                                    found["bed_leveling"] = v.strip().lower() not in ("0", "false", "")
                                elif k == "BED_X":
                                    found["bed_x"] = float(v)
                                elif k == "BED_Y":
                                    found["bed_y"] = float(v)
                                elif k == "BED_Z":
                                    found["printable_height"] = float(v)
                                elif k == "PAUSE_GCODE":
                                    found["pause_gcode"] = v.replace("_", " ")
                            except ValueError:
                                pass
                        break
        except Exception as e:
            self.log(f"Could not parse ;GH_CONFIG header ({e})", "WARNING")
        return found

    def _parse_gh_origin(self):
        """
        Read the optional ';GH_ORIGIN' header written by the Grasshopper generator:

            ;GH_ORIGIN X128.000 Y128.000 Z50.000 CONFIRM=1
            ;GH_ORIGIN X128.000 Y128.000 Z50.000 PAUSE=5000

        The origin ("0-Point") is a physical registration position, expressed in
        machine coordinates (the Grasshopper space already matches the printer
        volume, so no offset is applied). The nozzle drives there directly after
        the reference preamble and waits, giving you time to place the object you
        want to print onto. It is NOT emitted as geometry -- it rides in the
        header precisely because every G0/G1 in the body is treated as print
        geometry.

        Returns {"x","y","z","confirm","pause_ms"} or None when absent.
        """
        try:
            with open(self.custom_gcode, 'r') as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith("G0 ") or s.startswith("G1 "):
                        break
                    if not s.upper().startswith(";GH_ORIGIN"):
                        continue

                    origin = {"x": None, "y": None, "z": None,
                              "confirm": True, "pause_ms": 0.0}
                    for tok in s.split()[1:]:
                        up = tok.upper()
                        try:
                            if "=" in tok:
                                k, v = tok.split("=", 1)
                                k = k.upper()
                                if k == "CONFIRM":
                                    origin["confirm"] = v.strip().lower() not in ("0", "false", "")
                                elif k == "PAUSE":
                                    origin["pause_ms"] = float(v)
                                    origin["confirm"] = False
                            elif up.startswith("X"):
                                origin["x"] = float(tok[1:])
                            elif up.startswith("Y"):
                                origin["y"] = float(tok[1:])
                            elif up.startswith("Z"):
                                origin["z"] = float(tok[1:])
                        except ValueError:
                            continue

                    if origin["x"] is None or origin["y"] is None or origin["z"] is None:
                        msg = (f"';GH_ORIGIN' header is missing an X/Y/Z coordinate "
                               f"({s}) - origin point ignored")
                        self.log(f"WARNING: {msg}", "WARNING")
                        self.report["warnings"].append(msg)
                        return None
                    return origin
        except Exception as e:
            self.log(f"Could not parse ;GH_ORIGIN header ({e})", "WARNING")
        return None

    def _parse_gh_settings(self):
        """
        Read every ';GH_SETTINGS' header line written by the Grasshopper settings
        components, e.g.

            ;GH_SETTINGS TRAVEL_RETRACT_LENGTH=0.8 TRAVEL_MIN_DIST=1
            ;GH_SETTINGS PAUSE_RETRACT_LENGTH=0.4 PAUSE_MIN_MS=500

        Each component emits its own line and emits ONLY the keys actually set,
        so an unset key falls through to the reference config rather than being
        shadowed by a placeholder. Later lines win on duplicate keys.

        Returns {KEY: float}, empty when no settings lines are present.
        """
        found = {}
        try:
            with open(self.custom_gcode, 'r') as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith("G0 ") or s.startswith("G1 "):
                        break
                    if not s.upper().startswith(";GH_SETTINGS"):
                        continue
                    for tok in s.split()[1:]:
                        if "=" not in tok:
                            continue
                        k, v = tok.split("=", 1)
                        try:
                            found[k.upper()] = float(v)
                        except ValueError:
                            msg = f"Ignoring non-numeric setting '{tok}' in ;GH_SETTINGS"
                            self.log(f"WARNING: {msg}", "WARNING")
                            self.report["warnings"].append(msg)
        except Exception as e:
            self.log(f"Could not parse ;GH_SETTINGS header ({e})", "WARNING")
        return found

    def _get_reference_retraction(self):
        """
        Read the reference's own retraction settings from its CONFIG block:
        retraction_length, retraction_speed, deretraction_speed,
        retraction_minimum_travel, z_hop. These are the defaults that an unset
        ;GH_SETTINGS key falls back to, so behaviour follows whichever
        printer/filament combination sliced the reference.
        """
        # BambuStudio/Orca spelling first, then the PrusaSlicer/Cura spellings a
        # Marlin reference is likely to use.
        keys = {
            "length": ("retraction_length", "retract_length", "retraction_amount"),
            "speed": ("retraction_speed", "retract_speed", "retraction_retract_speed"),
            "deretract_speed": ("deretraction_speed", "deretract_speed",
                                "retraction_prime_speed"),
            "min_dist": ("retraction_minimum_travel", "retract_before_travel",
                         "retraction_min_travel"),
            "zhop": ("z_hop", "retract_lift", "retraction_hop"),
        }
        out = {}
        try:
            for name, cfg_keys in keys.items():
                for cfg_key in cfg_keys:
                    val = self._ref_config_float(cfg_key)
                    if val is not None:
                        out[name] = val
                        break
        except Exception as e:
            self.log(f"Could not read retraction settings from reference: {e}", "WARNING")

        # Cura writes no settings block at all. Its start gcode does contain a
        # real retraction though ("G1 F2700 E-0.5"), which is this printer's own
        # tuned value -- better than a generic fallback constant.
        if "length" not in out or "speed" not in out:
            derived = self._derive_retraction_from_gcode()
            for k, v in derived.items():
                out.setdefault(k, v)
        return out

    def _derive_retraction_from_gcode(self):
        """
        Recover retraction length/speed from the reference's own retract moves.

        Looks for an E-only move with a negative E and a feedrate -- the shape
        every slicer emits for a retraction. Cura's start block ends with
        'G1 F2700 E-0.5', so an Elegoo reference yields 0.5 mm at 45 mm/s
        instead of the generic 0.8 mm at 30 mm/s fallback.
        """
        out = {}
        for line in self.ref_lines[:4000]:
            s = line.split(";", 1)[0].strip()
            if not (s.startswith("G1 ") or s.startswith("G0 ")):
                continue
            tokens = s.split()[1:]
            words = {t[:1].upper(): t[1:] for t in tokens if len(t) > 1}
            if "X" in words or "Y" in words or "Z" in words:
                continue  # a combined move, not a plain retraction
            if "E" not in words or "F" not in words:
                continue
            try:
                e_val = float(words["E"])
                f_val = float(words["F"])
            except ValueError:
                continue
            if e_val < 0 and f_val > 0:
                out["length"] = abs(e_val)
                out["speed"] = f_val / 60.0
                out.setdefault("deretract_speed", f_val / 60.0)
                self.log(f"Retraction derived from the reference's own retract move: "
                         f"{out['length']:.2f} mm @ {out['speed']:.0f} mm/s", "INFO")
                break
        return out

    def _resolve_retraction(self, channel, gh_settings, ref_defaults, enabled):
        """
        Resolve one retraction profile: ;GH_SETTINGS key > reference config > fallback.

        channel is 'TRAVEL' or 'PAUSE'. The two differ deliberately in what an
        empty settings block means:
          - TRAVEL: falls back to the reference's own retraction settings
          - PAUSE:  stays DISABLED (holding melt pressure at a wireframe apex is
                    the point of the dwell; retracting there works against it)
        Returns None when retraction is off for this channel.
        """
        prefix = channel.upper() + "_"
        keys = {
            "length": prefix + "RETRACT_LENGTH",
            "speed": prefix + "RETRACT_SPEED",
            "deretract_speed": prefix + "DERETRACT_SPEED",
            "zhop": prefix + "ZHOP",
        }
        threshold_key = prefix + ("MIN_DIST" if channel.upper() == "TRAVEL" else "MIN_MS")
        has_any = any(k in gh_settings for k in list(keys.values()) + [threshold_key])

        if channel.upper() == "PAUSE":
            # No settings supplied -> no pause retraction at all.
            if not has_any:
                return None
        elif not enabled:
            return None

        def pick(name, ref_name, fallback):
            if keys.get(name) in gh_settings:
                return gh_settings[keys[name]]
            if ref_name and ref_name in ref_defaults:
                return ref_defaults[ref_name]
            return fallback

        prof = {
            "length": pick("length", "length", FALLBACK_RETRACT_LENGTH),
            "speed": pick("speed", "speed", FALLBACK_RETRACT_SPEED),
            "deretract_speed": pick("deretract_speed", "deretract_speed", FALLBACK_RETRACT_SPEED),
            # Z-hop is opt-in: with an explicit travel trajectory the polyline is
            # trusted unless a hop is asked for, so an unset key means 0 (no hop)
            # rather than inheriting the reference's z_hop.
            "zhop": gh_settings.get(keys["zhop"], 0.0),
            "from_settings": has_any,
        }
        if channel.upper() == "TRAVEL":
            prof["min_dist"] = gh_settings.get(
                threshold_key, ref_defaults.get("min_dist", FALLBACK_MIN_TRAVEL_DIST))
        else:
            prof["min_ms"] = gh_settings.get(threshold_key, 0.0)

        if prof["length"] <= 0:
            return None
        return prof

    def _get_pause_gcode(self):
        """
        Resolve the printer's "pause and wait for the operator" command.

        This is only used for a CONFIRMATION pause -- PAUSE=-1 on a point, or
        CONFIRM=1 on the 0-Point. Timed pauses always use G4 and never come
        near this. Getting it wrong means the printer either ignores the
        command and keeps printing, or errors out mid-job, so the resolution
        order is deliberately conservative:

            1. explicit override      (--pause-gcode / config / ;GH_CONFIG)
            2. '; machine_pause_gcode' declared by the reference   (Bambu/Orca)
            3. the firmware family of the detected printer         (registry)
            4. nothing -- and then a confirmation pause is refused rather than
               guessed at, because a guess that silently does nothing turns a
               "stop and let me place the object" into a crash into that object

        Returns (command_or_None, source_description).
        """
        override = (self.config.get("pause_gcode")
                    or self._parse_gh_config().get("pause_gcode"))
        if override:
            return str(override).replace("\\n", "\n"), "explicit override"

        declared = self._ref_config_value("machine_pause_gcode")
        if declared:
            return declared.replace("\\n", "\n"), "reference '; machine_pause_gcode'"

        fw = self.firmware or {}
        if fw.get("pause_gcode"):
            return fw["pause_gcode"], f"{fw['label']} firmware default"

        if self.ref_format == "3mf":
            return DEFAULT_PAUSE_GCODE, "built-in Bambu fallback"
        return None, "unknown"

    def _resolve_settings(self):
        """Merge global settings: ;GH_CONFIG header > config.yaml > built-in defaults."""
        gh = self._parse_gh_config()
        cfg = self.config if isinstance(self.config, dict) else {}
        pb = cfg.get("print_behavior", {}) if isinstance(cfg, dict) else {}
        speed = gh.get("speed_mm_s", cfg.get("speed_mm_s", DEFAULT_SPEED_MMS))
        flow = gh.get("flow", cfg.get("flow_multiplier", DEFAULT_FLOW))
        bed = gh.get("bed_leveling", pb.get("bed_leveling", DEFAULT_BED_LEVELING))
        settings = {
            "speed_mm_s": float(speed),
            "flow": float(flow),
            "bed_leveling": bool(bed),
            "from_header": bool(gh),
        }
        src = "from ;GH_CONFIG header" if gh else ("from config.yaml" if cfg else "from built-in defaults")
        self.log(f"Settings ({src}): speed={settings['speed_mm_s']} mm/s, "
                 f"flow={settings['flow']}, bed_leveling={settings['bed_leveling']}", "INFO")
        return settings

    def _get_filament_diameter(self):
        """Read the filament diameter from the reference config (default 1.75 mm)."""
        for key in ("filament_diameter", "material_diameter"):
            val = self._ref_config_float(key)
            if val:
                return val
        return DEFAULT_FILAMENT_D

    def _get_filament_density(self):
        """Read the filament density in g/cm^3 from the reference (default PLA)."""
        for key in ("filament_density", "material_density"):
            val = self._ref_config_float(key)
            if val:
                return val
        return DEFAULT_FILAMENT_DENSITY

    def _start_block_extruded_e(self, start_lines):
        """
        Total filament (mm) the reference's start gcode pushes out.

        The reference's purge/priming line is copied into the output verbatim,
        so it comes off the spool for real and belongs in the material
        estimate. Only positive E deltas count -- retractions come back.

        Measured from the gcode rather than from the calibration result,
        because the two are only the same thing on a Bambu reference: the
        Marlin path calibrates on the object's own print moves, whose total E
        has nothing to do with priming.
        """
        absolute = True
        rel_all = False
        e_abs = 0.0
        total = 0.0
        for line in start_lines:
            s = line.split(";", 1)[0].strip()
            if not s:
                continue
            head = s.split()[0].upper()
            if head == "M82":
                absolute = True
                continue
            if head == "M83":
                absolute = False
                continue
            if head == "G91":
                rel_all = True
                continue
            if head == "G90":
                rel_all = False
                continue
            if head == "G92":
                for tok in s.split()[1:]:
                    if tok[:1].upper() == "E":
                        try:
                            e_abs = float(tok[1:])
                        except ValueError:
                            e_abs = 0.0
                continue
            if head not in ("G0", "G1"):
                continue
            for tok in s.split()[1:]:
                if tok[:1].upper() != "E":
                    continue
                try:
                    val = float(tok[1:])
                except ValueError:
                    continue
                if absolute and not rel_all:
                    delta = val - e_abs
                    e_abs = val
                else:
                    delta = val
                if delta > 0:
                    total += delta
        return total

    def _build_estimates(self, reference_data):
        """
        Roll the time and material numbers into one report block.

        Deliberately NOT modelled, and said so in the report: acceleration and
        junction deviation (so the time is an optimistic floor, worst on short
        segments), and the heat-up / bed-levelling / teardown sequences. A real
        print takes somewhat longer.
        """
        ept = self.report.get("estimated_print_time") or {}
        stats = self.report.get("extrusion_stats") or {}
        filament_d = self._get_filament_diameter()
        density = self._get_filament_density()
        area = math.pi * (filament_d / 2.0) ** 2

        def grams(e_mm):
            # E (mm of filament) x cross-section (mm^2) = mm^3; 1 cm^3 = 1000 mm^3
            return e_mm * area * density / 1000.0

        geometry_e = float(stats.get("total_e") or 0.0)
        priming_e = self._start_block_extruded_e(
            (reference_data or {}).get("executable_start") or [])
        total_s = float(ept.get("total_seconds") or 0.0)

        estimates = {
            "print_time_s": total_s,
            "print_time_text": format_duration(total_s),
            "printing_time_s": ept.get("printing_time_s", 0.0),
            "travel_time_s": ept.get("travel_time_s", 0.0),
            "dwell_time_s": ept.get("dwell_time_seconds", 0.0),
            "travel_distance_mm": ept.get("travel_distance_mm", 0.0),
            "filament_length_mm": geometry_e,
            "filament_volume_mm3": geometry_e * area,
            "filament_grams": grams(geometry_e),
            "priming_length_mm": priming_e,
            "priming_grams": grams(priming_e),
            "total_grams": grams(geometry_e + priming_e),
            "filament_diameter_mm": filament_d,
            "filament_density_g_cm3": density,
        }
        self.log("Print Estimate:", "INFO")
        self.log(f"  Estimated print time: {estimates['print_time_text']} "
                 f"(motion + dwells; excludes heat-up and teardown)", "INFO")
        self.log(f"  Estimated material:   {estimates['total_grams']:.2f} g "
                 f"({estimates['filament_grams']:.2f} g geometry "
                 f"+ {estimates['priming_grams']:.2f} g priming line)", "INFO")
        self.log(f"  Filament used:        {geometry_e / 1000.0:.2f} m "
                 f"at dia {filament_d} mm, density {density} g/cm^3", "INFO")
        self.report["estimates"] = estimates
        return estimates

    @staticmethod
    def _parse_corner_list(val):
        """
        Reduce a slicer bed-shape corner list to an axis-aligned XY bbox.

        Handles both spellings in the wild:
            BambuStudio/Orca  '; printable_area = 0x0,256x0,256x256,0x256'
            PrusaSlicer       '; bed_shape = 0x0,250x0,250x210,0x210'
        Returns (min_x, min_y, max_x, max_y) or None.
        """
        xs, ys = [], []
        for corner in str(val).split(","):
            corner = corner.strip()
            if "x" not in corner:
                continue
            cx, cy = corner.split("x", 1)
            try:
                xs.append(float(cx))
                ys.append(float(cy))
            except ValueError:
                continue
        return (min(xs), min(ys), max(xs), max(ys)) if xs and ys else None

    def _get_printable_area(self):
        """
        Resolve the usable XY bed rectangle, in the order documented on
        printer_profiles: explicit override > declared by the file > registry.

        The file outranks the registry deliberately. A modified machine states
        the truth in its own profile, and a spec-sheet number must never
        silently overrule it. Returns (min_x, min_y, max_x, max_y) or None --
        None means the X/Y half of the build-volume check is skipped, with a
        warning, rather than run against an invented limit.
        """
        gh = self._parse_gh_config()

        # 1. explicit override
        raw = self.config.get("printable_area")
        if raw:
            area = self._parse_corner_list(raw) if isinstance(raw, str) else tuple(raw)
            if area:
                self._area_source = "explicit override"
                return area
        bed_x = self.config.get("bed_x", gh.get("bed_x"))
        bed_y = self.config.get("bed_y", gh.get("bed_y"))
        if bed_x and bed_y:
            self._area_source = "explicit override"
            return (0.0, 0.0, float(bed_x), float(bed_y))

        # 2. declared by the reference file
        for key in ("printable_area", "bed_shape"):
            val = self._ref_config_value(key)
            if val:
                area = self._parse_corner_list(val)
                if area:
                    self._area_source = f"reference '; {key}'"
                    return area

        # 3. printer registry
        prof = (self.printer or {}).get("profile")
        if prof:
            self._area_source = f"printer registry ({prof['label']})"
            return (0.0, 0.0, float(prof["bed"][0]), float(prof["bed"][1]))

        self._area_source = None
        return None

    def _get_bed_source(self):
        """Where _get_printable_area last got its answer (for the report)."""
        return getattr(self, "_area_source", None)

    def _validate_print_volume(self, custom_moves, origin=None):
        """
        Fail hard if any geometry lies outside the printer's usable build volume.

        The origin ("0-Point") is checked too when present: it is a real machine
        move, so an unreachable one must fail before anything is written.

        Bounds come from the reference file's CONFIG block, so they follow
        whatever machine sliced it: '; printable_area' gives the XY bed
        rectangle, '; printable_height' the Z ceiling. Z<0 is always a violation
        (below the bed) and needs no printer data.

        Raises PrintVolumeError before anything is written, so a print that
        would crash the gantry or scrape the bed never produces an output file.
        A bound the reference does not declare is reported as a warning and
        skipped -- we do not fail geometry against an invented limit.
        """
        if not custom_moves:
            raise PrintVolumeError(
                "No geometry moves found in the custom file. Expected G0/G1 moves "
                f"from the Grasshopper generator in: {self.custom_gcode.name}")

        area = self._get_printable_area()
        printable_height, model = self._get_printer_z_limit()

        below_bed, above_z, outside_xy = [], [], []
        min_z = max_z = None
        min_x = max_x = min_y = max_y = None

        for i, move in enumerate(custom_moves):
            x, y, z = move["x_eff"], move["y_eff"], move["z_eff"]

            if z is not None:
                min_z = z if min_z is None else min(min_z, z)
                max_z = z if max_z is None else max(max_z, z)
                if z < -VOLUME_TOL_MM:
                    below_bed.append((i, z))
                elif printable_height is not None and z > printable_height + VOLUME_TOL_MM:
                    above_z.append((i, z))

            if x is not None:
                min_x = x if min_x is None else min(min_x, x)
                max_x = x if max_x is None else max(max_x, x)
            if y is not None:
                min_y = y if min_y is None else min(min_y, y)
                max_y = y if max_y is None else max(max_y, y)

            if area is not None and x is not None and y is not None:
                ax0, ay0, ax1, ay1 = area
                if (x < ax0 - VOLUME_TOL_MM or x > ax1 + VOLUME_TOL_MM or
                        y < ay0 - VOLUME_TOL_MM or y > ay1 + VOLUME_TOL_MM):
                    outside_xy.append((i, x, y))

        # Record the measured extents for the report either way
        self.report["build_volume"] = {
            "printer_model": model or "unknown",
            "printable_area": list(area) if area else None,
            "printable_height": printable_height,
            "geometry_x": [min_x, max_x],
            "geometry_y": [min_y, max_y],
            "geometry_z": [min_z, max_z],
        }

        # A bound nobody could supply is skipped rather than invented -- but say
        # so loudly and name the fix, because a skipped check reads exactly like
        # a passed one in the report otherwise.
        unknown = (self.printer or {}).get("declared_name") or "this printer"
        if area is None:
            msg = (f"Bed size unknown: the reference declares no printable_area/bed_shape "
                   f"and '{unknown}' is not in the printer registry. X/Y build-volume "
                   f"validation was SKIPPED - pass --bed WIDTHxDEPTH (or --printer <slug>) "
                   f"to enable it")
            self.log(f"WARNING: {msg}", "WARNING")
            self.report["warnings"].append(msg)
        if printable_height is None:
            msg = (f"Z height unknown: the reference declares no printable_height/"
                   f"max_print_height and '{unknown}' is not in the printer registry. "
                   f"Z-ceiling validation was SKIPPED, and the end-of-print park cannot "
                   f"be capped to the machine's limit - pass --height MM (or "
                   f"--printer <slug>) to enable it")
            self.log(f"WARNING: {msg}", "WARNING")
            self.report["warnings"].append(msg)

        # The origin point is a commanded move like any other -> same bounds.
        origin_problems = []
        if origin is not None:
            ox, oy, oz = origin["x"], origin["y"], origin["z"]
            if oz < -VOLUME_TOL_MM:
                origin_problems.append(f"Z {oz:.3f} mm is below the bed")
            elif printable_height is not None and oz > printable_height + VOLUME_TOL_MM:
                origin_problems.append(
                    f"Z {oz:.3f} mm exceeds the printer Z limit ({printable_height:.1f} mm)")
            if area is not None:
                ax0, ay0, ax1, ay1 = area
                if (ox < ax0 - VOLUME_TOL_MM or ox > ax1 + VOLUME_TOL_MM or
                        oy < ay0 - VOLUME_TOL_MM or oy > ay1 + VOLUME_TOL_MM):
                    origin_problems.append(
                        f"X{ox:.3f} Y{oy:.3f} is outside the printable area "
                        f"(X {ax0:.1f}-{ax1:.1f}, Y {ay0:.1f}-{ay1:.1f} mm)")
            self.report["build_volume"]["origin"] = [ox, oy, oz]

        problems = []
        if origin_problems:
            problems.append("  - origin point (0-Point): " + "; ".join(origin_problems))
        if below_bed:
            worst = min(z for _, z in below_bed)
            problems.append(
                f"  - {len(below_bed)} move(s) below the bed (Z < 0): lowest Z = "
                f"{worst:.3f} mm (first at geometry move #{below_bed[0][0] + 1})")
        if above_z:
            worst = max(z for _, z in above_z)
            problems.append(
                f"  - {len(above_z)} move(s) above the printer Z limit "
                f"({printable_height:.1f} mm): highest Z = {worst:.3f} mm "
                f"(first at geometry move #{above_z[0][0] + 1})")
        if outside_xy:
            ax0, ay0, ax1, ay1 = area
            problems.append(
                f"  - {len(outside_xy)} move(s) outside the printable area "
                f"(X {ax0:.1f}-{ax1:.1f}, Y {ay0:.1f}-{ay1:.1f} mm): geometry spans "
                f"X {min_x:.3f}-{max_x:.3f}, Y {min_y:.3f}-{max_y:.3f} "
                f"(first at geometry move #{outside_xy[0][0] + 1})")

        if problems:
            raise PrintVolumeError(
                f"Print geometry is outside the build volume of {model or 'this printer'}:\n"
                + "\n".join(problems)
                + "\nNo output file was written. Move or rescale the geometry in "
                  "Grasshopper, or merge against a reference sliced for a larger printer.")

        bounds = []
        if area is not None:
            bounds.append(f"X {area[0]:.0f}-{area[2]:.0f}, Y {area[1]:.0f}-{area[3]:.0f}")
        if printable_height is not None:
            bounds.append(f"Z 0-{printable_height:.0f}")

        def _span(lo, hi):
            return f"{lo:.1f}-{hi:.1f}" if lo is not None and hi is not None else "n/a"

        self.log(
            f"Build volume OK ({model or 'unknown printer'}"
            + (f": {'; '.join(bounds)} mm" if bounds else "")
            + f") | geometry X {_span(min_x, max_x)}, Y {_span(min_y, max_y)}, "
              f"Z {_span(min_z, max_z)} mm", "INFO")

    def log(self, message, level="INFO"):
        """Print log message"""
        prefix = f"[{level}]" if level != "INFO" else ""
        print(f"{prefix} {message}")

    def analyze(self):
        """
        Pre-flight inspection: what is in these two files, before anything merges.

        Returns a plain dict (JSON-serialisable) describing the geometry, the
        globals that WILL be used, the per-step ;GH tag spread, the pauses, and
        what the reference says about the machine. It writes nothing and mutates
        nothing except the reference cache.

        This is the ONE implementation of the Analyze step: the browser build's
        Analyze card and the CLI's --analyze both call it, so the numbers you see
        in the page are the numbers the CLI prints.

        The settings block reports ``self.settings`` as they currently stand, so
        calling :meth:`apply_tweaks` first previews the tweaked values -- exactly
        what the page's Tweak preview shows.
        """
        try:
            self._load_reference()

            nozzle = self._ref_config_float("nozzle_diameter") or 0.4
            filament_d = self._get_filament_diameter()
            material = self._ref_config_value("filament_type") or ""
            for sep in (";", ","):
                material = material.split(sep)[0]
            material = material.strip() or None

            # Bed-levelling probes live in the start block; 200 lines is well
            # past it on every slicer we handle.
            probe_tokens = ("G29", "QUAD_GANTRY", "Z_TILT", "PROBE_CALIBRATE")
            ref_has_bed_leveling = any(
                tok in line for line in self.ref_lines[:200] for tok in probe_tokens)

            with open(self.custom_gcode, 'r', errors="replace") as f:
                custom_lines = f.readlines()

            gh_config_line = None
            for line in custom_lines:
                s = line.strip()
                if not s:
                    continue
                if s.startswith("G0 ") or s.startswith("G1 "):
                    break
                if s.upper().startswith(";GH_CONFIG"):
                    gh_config_line = s
                    break

            speed_tags, flow_tags = {}, {}
            move_count = 0
            pauses_confirm = pauses_timed = 0
            total_pause_ms = 0.0
            xs, ys, zs = [], [], []

            def tag_value(text, name):
                """Read '<name>=<number>' out of a ;GH comment, or None."""
                for sep in (name + "=", name + " ="):
                    if sep in text:
                        try:
                            return float(text.split(sep, 1)[1].split()[0].split(";")[0])
                        except (ValueError, IndexError):
                            return None
                return None

            for line in custom_lines:
                s = line.strip()
                if s.startswith("G0 ") or s.startswith("G1 "):
                    move_count += 1
                    for tok in s.split():
                        try:
                            if tok.startswith("X"):
                                xs.append(float(tok[1:]))
                            elif tok.startswith("Y"):
                                ys.append(float(tok[1:]))
                            elif tok.startswith("Z"):
                                zs.append(float(tok[1:]))
                        except ValueError:
                            pass

                if ";GH " not in s and not s.upper().startswith(";GH"):
                    continue
                val = tag_value(s, "SPEED")
                if val is not None:
                    speed_tags[val] = speed_tags.get(val, 0) + 1
                val = tag_value(s, "FLOW")
                if val is not None:
                    flow_tags[val] = flow_tags.get(val, 0) + 1
                val = tag_value(s, "PAUSE")
                if val is not None:
                    if val == PAUSE_CONFIRM:
                        pauses_confirm += 1
                    elif val > 0:
                        pauses_timed += 1
                        total_pause_ms += val

            def spread(tags):
                vals = sorted(tags)
                return (vals[0], vals[-1]) if vals else (1.0, 1.0)

            speed_min, speed_max = spread(speed_tags)
            flow_min, flow_max = spread(flow_tags)

            firmware_key = (printer_profiles.DEFAULT_FIRMWARE
                            if printer_profiles is not None else "marlin")
            if printer_profiles is not None:
                det = printer_profiles.detect_printer(self.ref_lines) or {}
                prof = det.get("profile") or {}
                if det.get("slug"):
                    firmware_key = prof.get("firmware", firmware_key)
                # The registry label is the machine's real name; declared_name is
                # whatever the slicer happened to write, and is all we have when
                # the printer is not in the registry.
                printer_label = prof.get("label") or det.get("declared_name")
                printer_slug = det.get("slug")
            else:
                printer_label = printer_slug = None

            return {
                "ok": True,
                "files": {
                    "reference": self.reference_file.name,
                    "reference_format": self.ref_format,
                    "custom": self.custom_gcode.name,
                },
                "geometry": {
                    "move_count": move_count,
                    "x": {"min": min(xs) if xs else None, "max": max(xs) if xs else None},
                    "y": {"min": min(ys) if ys else None, "max": max(ys) if ys else None},
                    "z": {"min": min(zs) if zs else None, "max": max(zs) if zs else None},
                },
                "current_settings": {
                    "base_speed_mm_s": self.settings["speed_mm_s"],
                    "global_flow": self.settings["flow"],
                    "bed_leveling_enabled": self.settings["bed_leveling"],
                    "nozzle_diameter_mm": nozzle,
                    "filament_diameter_mm": filament_d,
                    "material_type": material,
                },
                "per_step_variations": {
                    "speed_tags_found": sum(speed_tags.values()),
                    "speed_tag_values": {str(k): v for k, v in speed_tags.items()},
                    "speed_varied": len(speed_tags) > 1,
                    "speed_min_mult": speed_min,
                    "speed_max_mult": speed_max,
                    "flow_tags_found": sum(flow_tags.values()),
                    "flow_tag_values": {str(k): v for k, v in flow_tags.items()},
                    "flow_varied": len(flow_tags) > 1,
                    "flow_min_mult": flow_min,
                    "flow_max_mult": flow_max,
                },
                "pauses": {
                    "confirm_count": pauses_confirm,
                    "timed_count": pauses_timed,
                    "total_pause_ms": total_pause_ms,
                },
                "reference_info": {
                    "has_bed_leveling": ref_has_bed_leveling,
                    "firmware": firmware_key,
                    "printer_label": printer_label,
                    "printer_slug": printer_slug,
                },
                "gh_config_line": gh_config_line,
            }
        finally:
            # .3mf references unzip into a temp dir; analysis must not leave it behind.
            if self.temp_dir and Path(self.temp_dir).exists():
                shutil.rmtree(self.temp_dir, ignore_errors=True)
                self.temp_dir = None

    def analysis_text(self):
        """Render :meth:`analyze` as the CLI's --analyze block."""
        return format_analysis(self.analyze())

    def _resolve_end_park_z(self, max_z_geometry, printable_height):
        """
        Where to lift to before the reference teardown runs.

        Returns ``(park_z, source)``. The rule is "clear the print by
        END_PARK_CLEARANCE_MM", clamped to the machine's Z limit and floored at
        END_PARK_MIN_Z_MM.

        WHY THE CROSS-CHECK
        -------------------
        An earlier version of this parked relative to the print height and got
        the height from the wrong file -- it picked up the *reference* object's
        Z, which has nothing to do with the custom geometry. Parking below the
        print drags the nozzle through it. The response at the time was to give
        up on clearance and always park at the printer's Z limit, which is
        unconditionally safe but crawls the full height of the machine.

        The height is now measured twice, independently, from the same custom
        moves: once by _validate_print_volume (before anything is written) and
        once by the merge loop. They are the same arithmetic, so they must
        agree. If they ever do not, the measurement is not trustworthy and this
        falls back to the old always-safe behaviour rather than guessing --
        the failure mode is a slow teardown, never a nozzle in the print.
        """
        checked = ((self.report.get("build_volume") or {}).get("geometry_z")
                   or [None, None])[1]
        agrees = (checked is not None
                  and abs(checked - max_z_geometry) <= Z_AGREEMENT_TOL_MM)

        if max_z_geometry <= 0 or not agrees:
            reason = ("no geometry height was measured" if max_z_geometry <= 0 else
                      f"the two geometry-height measurements disagree "
                      f"({max_z_geometry:.3f} vs {checked:.3f} mm)")
            msg = (f"End-of-print park fell back to the printer Z limit because "
                   f"{reason}. The print will still be cleared, but the teardown "
                   f"travels the full height of the machine.")
            self.log(f"WARNING: {msg}", "WARNING")
            self.report["warnings"].append(msg)
            if printable_height is not None:
                return printable_height, (self._get_height_source()
                                          or "reference printable_height")
            return END_PARK_FALLBACK_Z, "fallback default (no printable_height found)"

        park_z = max(max_z_geometry + END_PARK_CLEARANCE_MM, END_PARK_MIN_Z_MM)
        source = f"{END_PARK_CLEARANCE_MM:.0f} mm above the print (max geometry Z)"

        # Never command the machine past its own limit, whatever the print height.
        if printable_height is not None and park_z > printable_height:
            park_z = printable_height
            source = (f"printer Z limit -- the print is within "
                      f"{END_PARK_CLEARANCE_MM:.0f} mm of it")
            clearance = printable_height - max_z_geometry
            if clearance < END_PARK_CLEARANCE_MM:
                msg = (f"End-of-print clearance is only {clearance:.1f} mm: the print "
                       f"reaches {max_z_geometry:.1f} mm and the printer's Z limit is "
                       f"{printable_height:.1f} mm.")
                self.log(f"WARNING: {msg}", "WARNING")
                self.report["warnings"].append(msg)

        return park_z, source

    def apply_tweaks(self, speed_multiplier=1.0, flow_multiplier=1.0,
                     bed_leveling=None):
        """
        Scale the resolved global settings before the merge runs.

        This is the ONE implementation of the Tweak step. The CLI
        (--speed-multiplier / --flow-multiplier / --bed-leveling) and the browser
        build's Tweak Settings card both call it with the same arguments, so a
        given set of tweaks produces byte-identical output from either front end.

        Call it after construction (``self.settings`` is resolved in __init__)
        and before :meth:`run`. Defaults are the identity: with no arguments the
        merge behaves exactly as if this had never been called, which is why a
        plain ``gcode_merger.py <dir>`` is unaffected.

        The originals are recorded in the report so the text report can show
        "Original -> Applied" rather than silently presenting a scaled number as
        if the geometry had asked for it.
        """
        original_speed = self.settings.get("speed_mm_s", DEFAULT_SPEED_MMS)
        original_flow = self.settings.get("flow", DEFAULT_FLOW)
        original_bed_leveling = self.settings.get("bed_leveling", DEFAULT_BED_LEVELING)

        if speed_multiplier is not None and speed_multiplier != 1.0:
            self.settings["speed_mm_s"] = original_speed * speed_multiplier
            self.report["speed_multiplier_applied"] = speed_multiplier
            self.report["original_speed_mm_s"] = original_speed
            self.log(f"Speed multiplier {speed_multiplier}x: "
                     f"{original_speed} -> {self.settings['speed_mm_s']} mm/s", "INFO")

        if flow_multiplier is not None and flow_multiplier != 1.0:
            self.settings["flow"] = original_flow * flow_multiplier
            self.report["flow_multiplier_applied"] = flow_multiplier
            self.report["original_flow"] = original_flow
            self.log(f"Flow multiplier {flow_multiplier}x: "
                     f"{original_flow} -> {self.settings['flow']}", "INFO")

        if bed_leveling is not None:
            self.settings["bed_leveling"] = bool(bed_leveling)
            self.report["bed_leveling_override"] = bool(bed_leveling)
            self.report["original_bed_leveling"] = original_bed_leveling
            self.log(f"Bed leveling override: "
                     f"{'ENABLED' if bed_leveling else 'DISABLED'}", "INFO")

        return self.settings

    def run(self):
        """Execute the merge process"""
        try:
            self.log("Starting GCode merge process...")
            self.log(f"Project: {self.project_dir.name}")
            self.log(f"Reference: {self.reference_file.name} "
                     f"({'BambuStudio .3mf' if self.ref_format == '3mf' else 'sliced .gcode'})")
            self.log(f"Custom: {self.custom_gcode.name}")

            self.log("Loading reference file...")
            self._load_reference()

            self.log("Parsing reference gcode...")
            reference_data = self._parse_reference_gcode()

            self.log("Parsing custom geometry gcode...")
            custom_moves = self._parse_custom_gcode()

            # Optional 0-Point (physical registration position, machine coords)
            origin = self._parse_gh_origin()

            # Fatal check BEFORE any output is produced: geometry that leaves the
            # build volume would crash the gantry or scrape the bed.
            self.log("Validating geometry against the printer's build volume...")
            self._validate_print_volume(custom_moves, origin)

            self.log("Merging and recalculating extrusion/speeds...")
            merged_gcode = self._merge_and_recalculate(reference_data, custom_moves, origin)

            self.log("Calculating estimated print time...")
            print_time = self._calculate_print_time(merged_gcode)
            self.report["estimated_print_time"] = print_time
            self._build_estimates(reference_data)

            # Note: M73 update disabled to prevent firmware throttling based on time estimate
            # The merged geometry has different duration than reference, and some printer firmware
            # may throttle print speed to match the estimated time. Keeping original M73 values
            # ensures the print runs at the speed/pause times specified in the custom gcode.
            # M73 is cosmetic (progress display) and shouldn't affect actual print behavior.

            self._write_output(merged_gcode)

            self.log("Generating merge report...")
            self._generate_report()

            if self.dry_run:
                print(f"\n[DRY RUN] Merge completed in memory. "
                      f"Would have written: {self.output_path}")
            else:
                print(f"\n[SUCCESS] SUCCESS! Output: {self.output_path}")
            est = self.report.get("estimates") or {}
            if est:
                tag = "[DRY RUN]" if self.dry_run else "[SUCCESS]"
                print(f"{tag} Estimated print time: {est.get('print_time_text', 'n/a')} | "
                      f"estimated material: {est.get('total_grams', 0):.2f} g "
                      f"({est.get('filament_length_mm', 0) / 1000.0:.2f} m of filament)")

        except PrintVolumeError as e:
            # Already a fully-formed, user-facing message; main() prints it.
            self.report["errors"].append(str(e))
            raise
        except Exception as e:
            self.log(f"ERROR: {str(e)}", "ERROR")
            self.report["errors"].append(str(e))
            raise
        finally:
            if self.temp_dir and self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)

    def _extract_3mf(self):
        """Extract 3mf file (which is a zip)"""
        with zipfile.ZipFile(self.reference_3mf, 'r') as zip_ref:
            zip_ref.extractall(self.temp_dir)

    def _parse_reference_gcode(self):
        """
        Split the reference into the sections the merge needs.

        Only 'executable_start' (everything up to the object's first layer) and
        'executable_end' (the teardown) are actually reused -- the reference's
        own geometry is thrown away and replaced by yours. Both formats produce
        the same dict, so _merge_and_recalculate never branches on format.
        """
        if self.ref_format == "3mf":
            return self._parse_reference_bambu(self.ref_lines)
        return self._parse_reference_marlin(self.ref_lines)

    def _parse_reference_marlin(self, lines):
        """
        Split a plain sliced .gcode into start / print / end.

        A Marlin-flavour file has no block markers, so the boundaries are found
        structurally:

        START ends at the first line of the object's own geometry. Slicers all
        mark that, just not the same way, so a priority list is used and the
        one that fired is logged. Getting this boundary right is what keeps the
        machine's real start sequence -- heat-up, homing, bed probe, purge line
        -- byte-for-byte intact, which is the whole point of merging against a
        reference rather than inventing a preamble.

        END begins after the LAST extruding move. Scanning backwards for that
        is more reliable than looking for an end marker, because the custom end
        gcode a user pasted into their slicer profile is arbitrary text: it may
        contain any comment, or none. What it cannot contain is a normal
        extruding print move.
        """
        markers = (
            (";LAYER:", "Cura ';LAYER:'"),
            (";LAYER_CHANGE", "PrusaSlicer/Orca ';LAYER_CHANGE'"),
            (";TYPE:", "';TYPE:' feature marker"),
            ("; feature", "Orca '; feature' marker"),
        )
        start_end = None
        rule = None
        for prefix, name in markers:
            for i, line in enumerate(lines):
                if line.strip().startswith(prefix):
                    start_end, rule = i, name
                    break
            if start_end is not None:
                break

        if start_end is None:
            # No layer/feature markers at all. Fall back to the last extruder
            # reset in the head of the file: every slicer zeroes E at the end
            # of its start gcode, right before the object begins.
            head = min(len(lines), 600)
            for i in range(head - 1, -1, -1):
                if lines[i].split(";", 1)[0].strip().upper().startswith("G92 E"):
                    start_end, rule = i + 1, "last 'G92 E0' in the file head"
                    break
        if start_end is None:
            raise ReferenceFormatError(
                f"Could not find where the object geometry starts in "
                f"{self.reference_file.name}. Expected a ';LAYER:', ';LAYER_CHANGE' "
                f"or ';TYPE:' marker, or a 'G92 E0' ending the start gcode. "
                f"Re-slice with comments enabled, or use a .gcode.3mf reference.")

        # Walk back from the end to the last real extruding move.
        end_start = None
        for i in range(len(lines) - 1, start_end - 1, -1):
            code = lines[i].split(";", 1)[0].strip()
            if not (code.startswith("G1 ") or code.startswith("G0 ")):
                continue
            tokens = code.split()[1:]
            letters = {t[:1].upper() for t in tokens if t}
            if "E" in letters and ("X" in letters or "Y" in letters):
                end_start = i + 1
                break
        if end_start is None:
            raise ReferenceFormatError(
                f"{self.reference_file.name} contains no extruding moves after its "
                f"start gcode, so the end sequence could not be located. Is it a "
                f"sliced object file?")

        data = {
            "header": [],
            "config": [],
            "executable_start": lines[:start_end],
            "executable_print": lines[start_end:end_start],
            "executable_end": lines[end_start:],
        }
        self.log(f"Reference sections: start = lines 1-{start_end} [{rule}], "
                 f"geometry = {end_start - start_end} lines (discarded), "
                 f"end = {len(lines) - end_start} lines", "INFO")
        self.report["reference_sections"] = {
            "start_lines": start_end,
            "start_rule": rule,
            "reference_geometry_lines": end_start - start_end,
            "end_lines": len(lines) - end_start,
        }
        return data

    def _parse_reference_bambu(self, lines):
        """Parse a BambuStudio plate gcode into its declared blocks."""
        data = {
            "header": [],
            "config": [],
            "executable_start": [],
            "executable_print": [],
            "executable_end": []
        }

        section = None
        in_executable = False
        executable_phase = "start"
        pending_blank_lines = []  # Preserve blank lines between sections

        for line in lines:
            stripped = line.strip()

            if "HEADER_BLOCK_START" in line:
                data["header"].append(line)
                section = "header"
                continue
            elif "HEADER_BLOCK_END" in line:
                data["header"].append(line)
                section = None
                pending_blank_lines = []  # Reset pending lines
                continue
            elif "CONFIG_BLOCK_START" in line:
                # Add any pending blank lines before this section starts
                data["config"].extend(pending_blank_lines)
                pending_blank_lines = []
                data["config"].append(line)
                section = "config"
                continue
            elif "CONFIG_BLOCK_END" in line:
                data["config"].append(line)
                section = None
                pending_blank_lines = []
                continue
            elif "EXECUTABLE_BLOCK_START" in line:
                # Add any pending blank lines before executable section
                data["executable_start"].extend(pending_blank_lines)
                pending_blank_lines = []
                data["executable_start"].append(line)
                in_executable = True
                executable_phase = "start"
                continue

            # Preserve blank lines between sections
            if section is None and not in_executable and stripped == "":
                pending_blank_lines.append(line)
                continue

            if section == "header":
                data["header"].append(line)
            elif section == "config":
                data["config"].append(line)
            elif in_executable:
                if "EXECUTABLE_BLOCK_END" in line:
                    data["executable_end"].append(line)
                elif "machine_end_gcode" in line.lower() or "MACHINE_END_GCODE_START" in line:
                    executable_phase = "end"
                    data["executable_end"].append(line)
                elif executable_phase == "start":
                    # Only transition to print when we see actual geometry (XYZ + extrusion)
                    if stripped.startswith("G1") or stripped.startswith("G0"):
                        if (" X" in line or " Y" in line) and " E" in line:
                            executable_phase = "print"
                            data["executable_print"].append(line)
                        else:
                            data["executable_start"].append(line)
                    else:
                        data["executable_start"].append(line)
                elif executable_phase == "end":
                    data["executable_end"].append(line)
                elif executable_phase == "print":
                    data["executable_print"].append(line)

        return data

    def _parse_custom_gcode(self):
        """
        Parse the custom gcode into geometry moves.

        The custom file is produced by the Grasshopper generator and contains
        geometry ONLY -- no preamble, no setup, no teardown. So EVERY G0/G1 with
        coordinates is print geometry and is kept, at whatever Z Grasshopper
        produced. There is deliberately no Z-window heuristic: a path may
        legitimately start at Z=0.0 on a bare plate or at Z=2.0 (or Z=200) when
        printing on top of an existing object. The previous "geometry starts
        between Z=0.1 and Z=1.0" guess silently discarded the leading points of
        low-start paths and discarded the *entire* path for high-start ones.

        Absolute position is carried across moves (x_eff/y_eff/z_eff) so that a
        move which omits an axis still resolves to a full XYZ position for
        build-volume validation.
        """
        with open(self.custom_gcode, 'r') as f:
            lines = f.readlines()

        moves = []
        cur_x, cur_y, cur_z = None, None, None
        skipped_non_motion = 0

        for line in lines:
            stripped = line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith(";"):
                continue

            # Only process G0/G1 movement commands; anything else in a
            # geometry-only file (G28, G92, M-codes, ...) is not our geometry.
            if not (stripped.startswith("G1 ") or stripped.startswith("G0 ")):
                skipped_non_motion += 1
                continue

            move = self._parse_gcode_move(stripped, line)
            if not move:
                continue

            # Carry absolute position forward across moves that omit an axis
            if move["x"] is not None:
                cur_x = move["x"]
            if move["y"] is not None:
                cur_y = move["y"]
            if move["z"] is not None:
                cur_z = move["z"]
            move["x_eff"], move["y_eff"], move["z_eff"] = cur_x, cur_y, cur_z

            moves.append(move)

        self.log(f"Extracted {len(moves)} geometry moves from custom file", "INFO")
        if skipped_non_motion:
            self.log(f"Ignored {skipped_non_motion} non-motion command(s) in the custom file", "INFO")
        if moves:
            zs = [m["z_eff"] for m in moves if m["z_eff"] is not None]
            if zs:
                self.log(f"Geometry Z range: {min(zs):.3f} - {max(zs):.3f} mm", "INFO")
        self.report["moves_processed"] = len(moves)
        return moves

    def _parse_step_tags(self, comment):
        """
        Parse a per-step data block from a gcode comment.

        Grasshopper writes structured per-point data after a ';GH' marker, e.g.
            G1 X10 Y20 Z0.4 ;GH FLOW=1.05
        and (in future) more channels on the same line:
            G1 X10 Y20 Z0.4 ;GH FLOW=1.05 SPEED=60 TRAVEL=0

        Returns a dict of UPPERCASE string keys -> string values (e.g.
        {"FLOW": "1.05"}). Returns {} when the comment has no ';GH' block, so
        plain gcode and older files parse to no tags and fall back to defaults.
        """
        tags = {}
        if not comment:
            return tags
        tokens = comment.split()
        if not tokens or tokens[0].upper() != "GH":
            return tags
        for tok in tokens[1:]:
            if "=" in tok:
                key, value = tok.split("=", 1)
                tags[key.upper()] = value
        return tags

    def _parse_gcode_move(self, line, original_line):
        """Parse a single G-code movement line (and any ';GH' per-step tags)."""
        move = {
            "original": original_line,
            "x": None,
            "y": None,
            "z": None,
            "e": None,
            "f": None,
            # Absolute position after this move, filled in by _parse_custom_gcode
            # (a move may omit an axis and inherit it from the previous move).
            "x_eff": None,
            "y_eff": None,
            "z_eff": None,
            "is_extrusion": False,
            "tags": {}
        }

        # Split off the comment so coordinate parsing never sees tag tokens
        # (e.g. 'FLOW=1.05' must not be mistaken for an F word).
        code_part, _, comment_part = line.partition(";")

        # Parse coordinate/parameter words from the code part only
        parts = code_part.split()
        for part in parts:
            if part.startswith("X"):
                try:
                    move["x"] = float(part[1:])
                except:
                    pass
            elif part.startswith("Y"):
                try:
                    move["y"] = float(part[1:])
                except:
                    pass
            elif part.startswith("Z"):
                try:
                    move["z"] = float(part[1:])
                except:
                    pass
            elif part.startswith("E"):
                try:
                    move["e"] = float(part[1:])
                    move["is_extrusion"] = True
                except:
                    pass
            elif part.startswith("F"):
                try:
                    move["f"] = float(part[1:])
                except:
                    pass

        # Parse per-step tags from the comment (FLOW now; SPEED/TRAVEL later)
        move["tags"] = self._parse_step_tags(comment_part)

        # Only include moves with actual coordinates
        if move["x"] is not None or move["y"] is not None or move["z"] is not None:
            return move
        return None

    def _extract_test_extrusion_reference(self, reference_data=None):
        """
        Reverse-engineer E-per-mm from the reference, however that reference is
        built. An explicit ';GH_CONFIG E_PER_MM=' / config override wins over
        both derivations.

        The two formats need genuinely different sources:

        * BambuStudio declares a 'nozzle load line' -- a purge whose geometry
          and E are both spelled out, which is what the Bambu path has always
          calibrated against. Unchanged.
        * A Cura/Marlin file has no such section, and its prime line is useless
          for the job: 'G1 X265 E30' is 0.30 E/mm, a deliberately fat bead
          roughly 4x a real print line. Calibrating on it would over-extrude
          everything by 4x. The reference's own PRINTING moves are used
          instead -- see _calibrate_from_print_moves.
        """
        override = self.config.get("e_per_mm")
        if override:
            result = {"total_e": None, "total_distance": None,
                      "e_per_mm": float(override), "moves": 0,
                      "source": "explicit override"}
            self.log(f"E-per-mm: {float(override):.6f} [explicit override]", "INFO")
            self.report["test_extrusion_reference"] = result
            return result

        if self.ref_format != "3mf":
            return self._calibrate_from_print_moves(reference_data)

        try:
            content = "".join(self.ref_lines)

            # Find test extrusion section (nozzle load line)
            # Look for the actual gcode commands in the test line section, not the config comments
            test_start_marker = "nozzle load line"

            start_idx = content.find(test_start_marker)
            if start_idx == -1:
                self.log("Could not find test extrusion start marker", "WARNING")
                return None

            # The test extrusion section ends when we hit the next major section
            # Look for the next comment marker that indicates a new section
            end_idx = content.find(";===== for Textured", start_idx)
            if end_idx == -1:
                # If that doesn't exist, look for other possible end markers
                end_idx = content.find(";========turn off", start_idx)

            if end_idx == -1:
                self.log("Could not find test extrusion end marker", "WARNING")
                return None

            test_section = content[start_idx:end_idx]
            # Handle both actual newlines and escaped newlines
            test_lines = test_section.replace('\\n', '\n').split('\n')

            # Parse test extrusion moves
            total_e = 0
            total_distance = 0
            prev_x, prev_y = 0, 0
            move_count = 0

            for line in test_lines:
                stripped = line.strip()
                if not (stripped.startswith("G0 ") or stripped.startswith("G1 ")):
                    continue

                # Parse coordinates and extrusion
                x, y, e = None, None, None
                try:
                    # Split by spaces, but be careful with formulas that contain spaces
                    parts = []
                    current_part = ""
                    for char in stripped:
                        if char == " " and current_part and not any(c in current_part for c in ["{", "}"]):
                            parts.append(current_part)
                            current_part = ""
                        else:
                            current_part += char
                    if current_part:
                        parts.append(current_part)

                    for part in parts:
                        try:
                            if part.startswith("X"):
                                x = float(part[1:].split("{")[0].split("}")[0])
                            elif part.startswith("Y"):
                                y = float(part[1:].split("{")[0].split("}")[0])
                            elif part.startswith("E"):
                                e_str = part[1:].split("{")[0].split("}")[0]
                                if e_str:  # Make sure it's not empty
                                    e = float(e_str)
                        except:
                            continue

                    # Only process moves with extrusion values
                    if e is not None and e > 0:
                        # Calculate distance if we have X or Y
                        distance = 0
                        if x is not None or y is not None:
                            if x is None:
                                x = prev_x
                            if y is None:
                                y = prev_y
                            distance = math.sqrt((x - prev_x) ** 2 + (y - prev_y) ** 2)
                            prev_x, prev_y = x, y

                        # Accumulate E and distance
                        total_e += e
                        if distance > 0:
                            total_distance += distance
                            move_count += 1

                except (ValueError, IndexError):
                    continue

            if total_distance > 0.01:
                e_per_mm = total_e / total_distance
                result = {
                    "total_e": total_e,
                    "total_distance": total_distance,
                    "e_per_mm": e_per_mm,
                    "moves": move_count,
                    "source": "BambuStudio 'nozzle load line' purge",
                }
                self.log(f"Test extrusion: {total_e:.2f}mm E over {total_distance:.2f}mm distance = {e_per_mm:.6f} E/mm", "INFO")
                self.report["test_extrusion_reference"] = result
                return result

        except Exception as e:
            self.log(f"Error extracting test extrusion reference: {str(e)}", "WARNING")
            return None

    def _calibrate_from_print_moves(self, reference_data):
        """
        Derive E-per-mm from the reference's own printing moves.

        For every extruding move, E-per-mm is (E laid down) / (distance moved).
        Across a whole object that ratio varies -- walls, solid infill, sparse
        infill and bridges all use different widths -- so the MEDIAN is taken
        rather than a total-over-total sum. The median is the ratio of a
        *typical* line in this file, and it is immune to the handful of extreme
        values that a sum would quietly absorb: a fat prime bead, a bridge at
        reduced flow, or a coast/wipe move.

        Segments below CAL_MIN_SEGMENT_MM are dropped because the file's
        3-5 decimal rounding dominates their ratio, not the flow.

        Handles absolute (M82) and relative (M83) E, since which one is in
        force decides whether an E word is a total or an increment.
        """
        lines = (reference_data or {}).get("executable_print") or self.ref_lines
        absolute_e = self._reference_e_is_absolute()

        ratios = []
        total_e = 0.0
        total_dist = 0.0
        x = y = z = None
        e_prev = 0.0
        rel_mode = False  # G91

        for line in lines:
            code = line.split(";", 1)[0].strip()
            if not code:
                continue
            head = code.split()[0].upper()
            if head == "M82":
                absolute_e = True
                continue
            if head == "M83":
                absolute_e = False
                continue
            if head == "G90":
                rel_mode = False
                continue
            if head == "G91":
                rel_mode = True
                continue
            if head == "G92":
                for tok in code.split()[1:]:
                    if tok[:1].upper() == "E":
                        try:
                            e_prev = float(tok[1:])
                        except ValueError:
                            pass
                continue
            if head not in ("G0", "G1") or rel_mode:
                continue

            nx = ny = nz = ne = None
            for tok in code.split()[1:]:
                letter, val = tok[:1].upper(), tok[1:]
                try:
                    num = float(val)
                except ValueError:
                    continue
                if letter == "X":
                    nx = num
                elif letter == "Y":
                    ny = num
                elif letter == "Z":
                    nz = num
                elif letter == "E":
                    ne = num

            de = 0.0
            if ne is not None:
                de = (ne - e_prev) if absolute_e else ne
                e_prev = ne if absolute_e else e_prev + ne

            if nx is None and ny is None and nz is None:
                continue
            fx = nx if nx is not None else x
            fy = ny if ny is not None else y
            fz = nz if nz is not None else z
            if x is not None and y is not None:
                dist = math.sqrt((fx - x) ** 2 + (fy - y) ** 2 +
                                 ((fz - z) ** 2 if z is not None and fz is not None else 0.0))
            else:
                dist = 0.0
            x, y, z = fx, fy, fz

            if de > 0 and dist >= CAL_MIN_SEGMENT_MM:
                ratios.append(de / dist)
                total_e += de
                total_dist += dist
                if len(ratios) >= CAL_MAX_SAMPLES:
                    break

        if not ratios:
            msg = (f"No extruding moves could be measured in "
                   f"{self.reference_file.name}, so extrusion could not be "
                   f"calibrated. Set E_PER_MM explicitly (--e-per-mm) or use a "
                   f"different reference.")
            self.log(f"ERROR: {msg}", "ERROR")
            self.report["warnings"].append(msg)
            return None

        ratios.sort()
        n = len(ratios)
        median = ratios[n // 2] if n % 2 else (ratios[n // 2 - 1] + ratios[n // 2]) / 2.0
        mean_weighted = total_e / total_dist

        result = {
            "total_e": total_e,
            "total_distance": total_dist,
            "e_per_mm": median,
            "moves": n,
            "source": "median of the reference's own printing moves",
            "e_per_mm_weighted_mean": mean_weighted,
            "e_per_mm_min": ratios[0],
            "e_per_mm_max": ratios[-1],
        }
        self.log(f"Extrusion calibrated from {n} printing moves in the reference: "
                 f"median {median:.6f} E/mm "
                 f"(weighted mean {mean_weighted:.6f}, range "
                 f"{ratios[0]:.6f}-{ratios[-1]:.6f})", "INFO")
        self.report["test_extrusion_reference"] = result
        return result

    def _reference_e_is_absolute(self):
        """
        Is the reference's extruder in absolute (M82) or relative (M83) mode
        when its geometry starts?

        This decides two things: how the reference's own E words are read when
        calibrating, and whether the merged output has to switch modes around
        its geometry. BambuStudio slices M83; Cura's Marlin flavour slices M82.
        The last mode command before the geometry wins, so the file is scanned
        rather than assumed from the container.
        """
        absolute = self.ref_format != "3mf"  # Marlin convention unless told otherwise
        for line in self.ref_lines:
            stripped = line.strip()
            # Stop at the object's first layer: mode changes after that belong
            # to the geometry we are replacing, not to the machine's setup.
            if stripped.startswith(";LAYER:") or stripped.startswith(";LAYER_CHANGE"):
                break
            head = stripped.split(";", 1)[0].strip().upper()
            if head.startswith("M82"):
                absolute = True
            elif head.startswith("M83"):
                absolute = False
        return absolute

    def _plan_travel_runs(self, custom_moves, travel_prof):
        """
        Decide, ahead of the emit loop, which SEGMENTS are travel and which
        travel RUNS get a retraction.

        Rule (b): segment i (point i-1 -> point i) is a travel move only when
        BOTH of its endpoints carry TRAVEL=1. Flagging the points of a travel
        sub-curve therefore makes the segments strictly between them dry, while
        the segments leading into and out of the region still print. A single
        isolated flagged point produces no travel segment at all.

        Retraction is decided per contiguous RUN, never per segment: retracting
        and priming on every segment of a run would grind the filament and cost
        far more time than it saves. A run whose total 3D length is below
        min_dist is left un-retracted, exactly as a normal slicer would.

        Returns (seg_travel, run_id_of_seg, runs) where runs maps
        run_id -> {"first","last","length","retract"}.
        """
        n = len(custom_moves)
        travel_pt = []
        for m in custom_moves:
            raw = m["tags"].get("TRAVEL")
            flag = False
            if raw is not None:
                try:
                    flag = float(raw) != 0
                except (ValueError, TypeError):
                    flag = False
            travel_pt.append(flag)

        # Segment i arrives at point i. Segment 0 is the seeded zero-length move.
        seg_travel = [False] * n
        for i in range(1, n):
            seg_travel[i] = travel_pt[i - 1] and travel_pt[i]

        # Group contiguous travel segments into runs and measure each in 3D.
        run_id_of_seg = {}
        runs = {}
        rid = -1
        prev = None
        for i in range(n):
            m = custom_moves[i]
            pos = (m["x_eff"], m["y_eff"], m["z_eff"])
            if seg_travel[i]:
                starts_run = (i == 0) or (not seg_travel[i - 1])
                if starts_run:
                    rid += 1
                    runs[rid] = {"first": i, "last": i, "length": 0.0, "retract": False}
                run_id_of_seg[i] = rid
                runs[rid]["last"] = i
                if prev is not None and None not in pos and None not in prev:
                    runs[rid]["length"] += math.sqrt(
                        (pos[0] - prev[0]) ** 2 +
                        (pos[1] - prev[1]) ** 2 +
                        (pos[2] - prev[2]) ** 2)
            if None not in pos:
                prev = pos

        if travel_prof is not None:
            min_dist = travel_prof.get("min_dist", FALLBACK_MIN_TRAVEL_DIST)
            for r in runs.values():
                r["retract"] = r["length"] >= min_dist

        return seg_travel, run_id_of_seg, runs

    def _merge_and_recalculate(self, reference_data, custom_moves, origin=None):
        """Merge reference and custom gcode, recalculating E and F values"""
        merged = []

        # Add header and config from reference
        merged.extend(reference_data["header"])
        merged.extend(reference_data["config"])

        # Add executable start from reference (includes preheat, bed leveling, nozzle wipe, etc.)
        start_section = reference_data["executable_start"]
        start_section = self._process_bed_leveling(start_section)
        merged.extend(start_section)

        # ---- Extruder mode ----
        # All of the extrusion, retraction and prime maths below is written for
        # RELATIVE E (M83), which is what BambuStudio slices. A Cura/Marlin
        # reference is absolute (M82), so the geometry is wrapped: switch to
        # M83 for our moves, then hand the machine back in the mode its own end
        # gcode was written for. Emitting incremental values into an absolute
        # extruder would read "E0.07" as "wind back to 0.07 mm total" and grind
        # the filament backwards on every single move.
        ref_e_absolute = self._reference_e_is_absolute()
        self.report["reference_e_mode"] = "absolute (M82)" if ref_e_absolute else "relative (M83)"
        if ref_e_absolute:
            self.log("Reference extruder mode: absolute (M82) -> geometry emitted "
                     "in relative mode (M83), restored before the end gcode", "INFO")
            residual = self._start_block_residual_e(start_section)
            merged.append("M83 ; [MODE] relative extrusion for the merged geometry\n")
            merged.append("G92 E0 ; [MODE] reset the extruder counter\n")
            if residual < -0.0001:
                # The reference's start gcode ends retracted (Cura signs off with
                # 'G1 F2700 E-0.5'). Left unprimed, the first millimetres of the
                # path run dry. Give back exactly what it took.
                merged.append(f"G1 E{abs(residual):.5f} F{FALLBACK_RETRACT_SPEED * 60:.0f}"
                              f" ; [MODE] re-prime the retraction the start gcode ended on\n")
                self.log(f"Start gcode ends retracted by {abs(residual):.3f} mm; "
                         f"primed back before the geometry", "INFO")

        # Extract test extrusion reference to determine E-value per mm
        test_ref = self._extract_test_extrusion_reference(reference_data)
        if test_ref is None:
            self.log("WARNING: Could not extract test extrusion reference.", "WARNING")
            e_per_mm = None
        else:
            e_per_mm = test_ref["e_per_mm"]
            self.log(f"Using E-per-mm from test extrusion: {e_per_mm:.6f} "
                     f"[{test_ref.get('source', 'reference')}]", "INFO")

        # Global settings resolved from ;GH_CONFIG header (or config.yaml / defaults)
        speed_mm_s = self.settings["speed_mm_s"]      # base print speed (mm/s)
        flow_multiplier = self.settings["flow"]       # global flow multiplier
        filament_d = self._get_filament_diameter()    # from reference, for volumetric check
        filament_area = math.pi * (filament_d / 2.0) ** 2  # mm^2

        self.log(f"Base print speed: {speed_mm_s} mm/s | global flow: {flow_multiplier}", "INFO")
        self.log(f"Speed clamp: {SPEED_MIN_MMS}-{SPEED_MAX_MMS} mm/s | filament dia: {filament_d} mm", "INFO")

        # Process custom geometry moves
        prev_x, prev_y, prev_z = 0, 0, 0
        prev_flow = None   # per-step FLOW of the previous point (for averaging)
        prev_speed = None  # per-step SPEED of the previous point (for averaging)
        e_cumulative = 0
        max_z_geometry = 0  # Track maximum Z height (informational / sanity check)
        extrusion_stats = {"total_distance": 0, "total_e": 0, "moves": 0}
        flow_stats = {"tagged_moves": 0, "min": None, "max": None, "sum": 0.0, "count": 0}
        speed_stats = {"tagged_moves": 0, "min": None, "max": None, "sum": 0.0, "count": 0, "clamped": 0}
        vol_stats = {"max_rate": 0.0}

        # ---- Optional origin point ("0-Point") ----
        # A physical registration position in machine coordinates. Emitted right
        # after the reference preamble, so every probe/purge/wipe in that preamble
        # still runs on an EMPTY plate. The nozzle then drives to the origin and
        # waits for confirmation, which is when you place the object you want to
        # print onto. Moving there is a direct G0 with no lift: the point of the
        # origin is where the nozzle tip physically ends up.
        pause_gcode, pause_gcode_source = self._get_pause_gcode()
        parks_on_pause = bool((self.firmware or {}).get("parks_on_pause"))
        if pause_gcode:
            self.log(f"Confirmation pause command: '{pause_gcode}' "
                     f"[{pause_gcode_source}]", "INFO")
        pause_stats = {"timed": 0, "confirm": 0, "total_ms": 0.0, "max_ms": 0.0,
                       "invalid": 0, "origin_confirm": False, "retractions": 0,
                       "unsupported": 0}

        # ---- Retraction profiles (;GH_SETTINGS > reference config > fallback) ----
        gh_settings = self._parse_gh_settings()
        ref_retract = self._get_reference_retraction()
        travel_enabled = bool(gh_settings.get("TRAVEL_RETRACTION", 1))
        travel_prof = self._resolve_retraction("TRAVEL", gh_settings, ref_retract, travel_enabled)
        pause_prof = self._resolve_retraction("PAUSE", gh_settings, ref_retract, True)

        if travel_prof:
            self.log(f"Travel retraction: {travel_prof['length']:.2f} mm @ "
                     f"{travel_prof['speed']:.0f} mm/s, min run {travel_prof['min_dist']:.2f} mm"
                     + (f", z-hop {travel_prof['zhop']:.2f} mm" if travel_prof['zhop'] else "")
                     + (" [from ;GH_SETTINGS]" if travel_prof["from_settings"] else " [reference defaults]"),
                     "INFO")
        else:
            self.log("Travel retraction: disabled", "INFO")
        if pause_prof:
            self.log(f"Pause retraction: {pause_prof['length']:.2f} mm @ "
                     f"{pause_prof['speed']:.0f} mm/s, dwells >= {pause_prof['min_ms']:.0f} ms"
                     + (f", z-hop {pause_prof['zhop']:.2f} mm" if pause_prof['zhop'] else ""), "INFO")
        else:
            self.log("Pause retraction: disabled (no PAUSE_* settings supplied)", "INFO")

        # Decide travel segments and per-run retraction before emitting anything.
        seg_travel, run_id_of_seg, travel_runs = self._plan_travel_runs(custom_moves, travel_prof)
        travel_stats = {"segments": sum(1 for t in seg_travel if t),
                        "runs": len(travel_runs),
                        "retracted_runs": sum(1 for r in travel_runs.values() if r["retract"]),
                        "distance": sum(r["length"] for r in travel_runs.values()),
                        "retractions": 0}
        if travel_stats["segments"]:
            self.log(f"Travel: {travel_stats['segments']} segment(s) in "
                     f"{travel_stats['runs']} run(s), {travel_stats['distance']:.1f} mm total; "
                     f"{travel_stats['retracted_runs']} run(s) long enough to retract", "INFO")
            if travel_stats["segments"] >= len(custom_moves) - 1:
                msg = "EVERY segment is flagged as travel - the merged file will extrude nothing"
                self.log(f"WARNING: {msg}", "WARNING")
                self.report["warnings"].append(msg)

        if origin is not None:
            merged.append(f"; [ORIGIN] 0-Point - place the object to print on, then confirm\n")
            merged.append(f"G0 X{origin['x']:.3f} Y{origin['y']:.3f} Z{origin['z']:.3f} F20000"
                          f" ; [ORIGIN] Move to 0-Point (no extrusion)\n")
            merged.append("M400 ; [ORIGIN] ensure the nozzle has arrived before pausing\n")
            if origin["confirm"] and not pause_gcode:
                # Refusing beats guessing. A wrong pause command is silently
                # ignored by the firmware, and the printer then drives straight
                # into the object the operator was supposed to place.
                raise ReferenceFormatError(
                    "The 0-Point asks for a confirmation pause, but no pause command "
                    f"is known for this printer ({(self.firmware or {}).get('label')}). "
                    "The reference declares no '; machine_pause_gcode' and the printer "
                    "was not identified.\nSupply one explicitly -- e.g. "
                    "--pause-gcode M0 (Marlin), --pause-gcode PAUSE (Klipper), "
                    "--pause-gcode M601 (Prusa) -- or use a timed pause instead "
                    "(;GH_ORIGIN ... PAUSE=<ms>).")
            if origin["confirm"]:
                merged.append(f"{pause_gcode} ; [ORIGIN] wait for confirmation on the printer display\n")
                pause_stats["origin_confirm"] = True
                self.log(f"[ORIGIN] 0-Point X{origin['x']:.2f} Y{origin['y']:.2f} "
                         f"Z{origin['z']:.2f} -> wait for display confirmation "
                         f"('{pause_gcode}')", "INFO")
            elif origin["pause_ms"] > 0:
                merged.append(f"G4 P{int(round(origin['pause_ms']))} ; [ORIGIN] timed pause at 0-Point\n")
                self.log(f"[ORIGIN] 0-Point X{origin['x']:.2f} Y{origin['y']:.2f} "
                         f"Z{origin['z']:.2f} -> pause {origin['pause_ms']:.0f} ms", "INFO")
            else:
                self.log(f"[ORIGIN] 0-Point X{origin['x']:.2f} Y{origin['y']:.2f} "
                         f"Z{origin['z']:.2f} (no pause)", "INFO")
            # The nozzle is now physically at the origin.
            prev_x, prev_y, prev_z = origin["x"], origin["y"], origin["z"]

        # ---- Non-extruding approach to the first geometry point ----
        # The nozzle must reach the start of the path WITHOUT laying material,
        # whatever Z the geometry starts at: Z=0.2 on a bare plate, or Z=20 when
        # the path is printed on top of an existing object.
        #
        # WITHOUT an origin point: lift -> travel XY -> descend, so the nozzle
        # never drags across the build plate on its way in.
        # WITH an origin point: a single direct move. The nozzle is already at a
        # position you chose, so a lift would be pointless (and would break the
        # registration you just set up by hand).
        printable_height, printer_model = self._get_printer_z_limit()
        if custom_moves:
            first_move = custom_moves[0]
            fx, fy, fz = first_move["x_eff"], first_move["y_eff"], first_move["z_eff"]
            if fx is not None and fy is not None and origin is not None:
                parts = f"G0 X{fx:.3f} Y{fy:.3f}"
                if fz is not None:
                    parts += f" Z{fz:.3f}"
                merged.append(parts + " F20000 ; [SAFETY] Direct move from 0-Point to first print position (no extrusion)\n")
                self.log(f"[SAFETY] Approach from 0-Point: direct move to X{fx:.2f} Y{fy:.2f} "
                         f"Z{fz if fz is not None else 0:.2f} (no lift, no extrusion)", "INFO")
                prev_x, prev_y = fx, fy
                if fz is not None:
                    prev_z = fz
                    max_z_geometry = max(max_z_geometry, fz)
                    first_move["z"] = None
            elif fx is not None and fy is not None:
                travel_z = MIN_TRAVEL_Z_MM
                if fz is not None:
                    travel_z = max(travel_z, fz + APPROACH_CLEARANCE_MM)
                if printable_height is not None:
                    travel_z = min(travel_z, printable_height)

                self.log(f"[SAFETY] Approach: lift to Z{travel_z:.2f} -> travel to "
                         f"X{fx:.2f} Y{fy:.2f} -> descend to Z{fz if fz is not None else 0:.2f} "
                         f"(no extrusion)", "INFO")
                merged.append(f"G0 Z{travel_z:.3f} F20000 ; [SAFETY] Lift clear before travel (no extrusion)\n")
                merged.append(f"G0 X{fx:.3f} Y{fy:.3f} F20000 ; [SAFETY] Travel to first print position (no extrusion)\n")
                if fz is not None:
                    merged.append(f"G1 Z{fz:.3f} F3600 ; [SAFETY] Descend to first print Z (no extrusion)\n")

                # Seed position tracking from the start point so the first
                # geometry move covers zero distance -> no E is emitted for it.
                prev_x, prev_y = fx, fy
                if fz is not None:
                    prev_z = fz
                    max_z_geometry = max(max_z_geometry, fz)
                    # Z is already commanded above; don't repeat it on the first move.
                    first_move["z"] = None

        # Turn on fans for printing (must be on during geometry to cool the print).
        # The part fan is universal. The auxiliary fan is addressed as 'M106 P2',
        # which is a Bambu/multi-fan extension: on a stock Marlin or Klipper
        # machine P is either ignored or rejected as an unknown parameter, so it
        # is only emitted for a reference that is known to have one.
        merged.append("M106 S255 ; Turn on part cooling fan for print\n")
        if self.ref_format == "3mf":
            merged.append("M106 P2 S100 ; Turn on auxiliary cooling fan\n")

        def _retract(prof, tag):
            """Emit a retraction (M83 relative, so this is self-contained)."""
            return f"G1 E-{prof['length']:.5f} F{prof['speed'] * 60:.0f} ; [{tag}] retract\n"

        def _prime(prof, tag):
            return f"G1 E{prof['length']:.5f} F{prof['deretract_speed'] * 60:.0f} ; [{tag}] prime\n"

        # Process all custom geometry moves
        for idx, move in enumerate(custom_moves):
            is_travel = seg_travel[idx]
            run = travel_runs.get(run_id_of_seg.get(idx, -1))
            starts_run = bool(run) and run["first"] == idx
            ends_run = bool(run) and run["last"] == idx
            # True 3D path length for the extrusion calculation. XY-only distance
            # would emit NO extrusion at all for a vertical strut (dx=dy=0) and
            # under-extrude any inclined one by cos(angle) -- fatal for wireframe
            # / spatial printing, negligible for near-flat layer paths.
            nx = move["x_eff"] if move["x_eff"] is not None else prev_x
            ny = move["y_eff"] if move["y_eff"] is not None else prev_y
            nz = move["z_eff"] if move["z_eff"] is not None else prev_z
            distance_3d = math.sqrt(
                (nx - prev_x) ** 2 +
                (ny - prev_y) ** 2 +
                (nz - prev_z) ** 2
            )

            # ---- Per-step FLOW (extrusion multiplier), averaged over the segment ----
            # Missing tags default to 1.0, so untagged files behave unchanged.
            cur_flow = 1.0
            if move["tags"].get("FLOW") is not None:
                try:
                    cur_flow = float(move["tags"]["FLOW"])
                    flow_stats["tagged_moves"] += 1
                except (ValueError, TypeError):
                    cur_flow = 1.0
            seg_flow = cur_flow if prev_flow is None else (prev_flow + cur_flow) / 2.0

            # ---- Per-step SPEED (multiplier on base speed), averaged over the segment ----
            cur_speed = 1.0
            if move["tags"].get("SPEED") is not None:
                try:
                    cur_speed = float(move["tags"]["SPEED"])
                    speed_stats["tagged_moves"] += 1
                except (ValueError, TypeError):
                    cur_speed = 1.0
            seg_speed = cur_speed if prev_speed is None else (prev_speed + cur_speed) / 2.0

            # Effective feedrate for this move, clamped to a safe print-speed window.
            eff_speed = speed_mm_s * seg_speed
            clamped_speed = min(max(eff_speed, SPEED_MIN_MMS), SPEED_MAX_MMS)
            if abs(clamped_speed - eff_speed) > 1e-9:
                speed_stats["clamped"] += 1
            f_move = clamped_speed * 60  # mm/s -> mm/min

            # Calculate E value based on test extrusion reference
            # NOTE: BambuStudio files use M83 (relative extrusion), so output incremental E.
            # Speed does NOT affect E: extrusion is per-mm, so it's independent of feedrate.
            e_new = None
            if is_travel:
                # Travel segment: follow the polyline exactly, but lay no material.
                # No E word at all -- on Marlin-derived firmware that is also what
                # makes the planner treat the move as a travel for acceleration.
                # The per-point SPEED channel still sets F, so travel speed stays
                # under your control rather than being forced to some global.
                if starts_run and run["retract"] and travel_prof:
                    merged.append(_retract(travel_prof, "TRAVEL"))
                    travel_stats["retractions"] += 1
            elif distance_3d > 0.001:  # Any movement > threshold gets E calculation
                if e_per_mm is not None:
                    # Reference-based rate x global flow x per-step (averaged) flow
                    e_increment = distance_3d * e_per_mm * flow_multiplier * seg_flow
                else:
                    # Fallback: assume no extrusion if we can't reference
                    e_increment = 0

                # Track cumulative for statistics, but output incremental (M83 mode)
                e_cumulative += e_increment
                e_new = e_increment  # Output the increment, not cumulative total

                self.report["e_values_recalculated"] += 1
                extrusion_stats["total_distance"] += distance_3d
                extrusion_stats["total_e"] += e_increment
                extrusion_stats["moves"] += 1

                # Track the range of per-step flow actually applied to extrusion
                flow_stats["min"] = seg_flow if flow_stats["min"] is None else min(flow_stats["min"], seg_flow)
                flow_stats["max"] = seg_flow if flow_stats["max"] is None else max(flow_stats["max"], seg_flow)
                flow_stats["sum"] += seg_flow
                flow_stats["count"] += 1

                # Track the range of (clamped) print speed applied
                speed_stats["min"] = clamped_speed if speed_stats["min"] is None else min(speed_stats["min"], clamped_speed)
                speed_stats["max"] = clamped_speed if speed_stats["max"] is None else max(speed_stats["max"], clamped_speed)
                speed_stats["sum"] += clamped_speed
                speed_stats["count"] += 1

                # Volumetric flow rate (mm^3/s) = extrudate cross-section area x speed.
                # extrudate area (mm^2) = e_per_mm x flow x filament_area.
                if e_per_mm is not None:
                    vol_rate = e_per_mm * flow_multiplier * seg_flow * filament_area * clamped_speed
                    if vol_rate > vol_stats["max_rate"]:
                        vol_stats["max_rate"] = vol_rate

            # ---- Optional Z-hop across a travel run ----
            # A normal slicer invents the travel trajectory, so it can hop freely.
            # Here the travel path is drawn by you, so the hop is applied as an
            # OFFSET: every Z in the run is raised by 'zhop', preserving the shape
            # you drew while clearing it, then the true Z is restored at the end.
            # Unset (0) means the polyline is trusted exactly as-is.
            hop = 0.0
            if is_travel and travel_prof and run and run["retract"]:
                hop = travel_prof.get("zhop", 0.0) or 0.0
            z_override = None
            if hop and move["z_eff"] is not None:
                z_override = move["z_eff"] + hop
                if printable_height is not None and z_override > printable_height:
                    z_override = printable_height

            # Build new gcode line with recalculated E and per-step F values
            new_line = self._build_gcode_line(move, e_new, f_move, z_override=z_override)
            merged.append(new_line)

            # Land back on the drawn path, then restore pressure for printing.
            if is_travel and ends_run:
                if hop and move["z_eff"] is not None:
                    merged.append(f"G1 Z{move['z_eff']:.3f} F{f_move:.0f}"
                                  f" ; [TRAVEL] drop back to path Z after z-hop\n")
                if run["retract"] and travel_prof:
                    merged.append(_prime(travel_prof, "TRAVEL"))

            # ---- Per-point PAUSE, emitted AT the point (never averaged) ----
            # FLOW and SPEED are segment properties, so they are averaged across
            # each segment's two endpoints. A pause is a point EVENT: averaging it
            # would smear one dwell across two segments and halve it. So the value
            # is used verbatim, on the point it was tagged to, immediately after
            # the move that arrives there.
            #
            # For wireframe / spatial printing this dwell holds the nozzle at the
            # apex so the strand can freeze before the head moves on. That is why
            # nothing is lifted, parked, retracted or cooled here: the nozzle must
            # stay exactly where it is. Both fans are already at 100% from above.
            #
            # M400 first so the motion buffer has drained and the head is really
            # stationary at the point, rather than still decelerating into it.
            pause_raw = move["tags"].get("PAUSE")
            if pause_raw is not None:
                try:
                    pause_val = float(pause_raw)
                except (ValueError, TypeError):
                    pause_val = 0.0
                    pause_stats["invalid"] += 1

                # Pause retraction is opt-in and OFF unless PAUSE_* settings were
                # supplied: at a wireframe apex the melt pressure the dwell holds
                # is part of what freezes the strand, so retracting there works
                # against the purpose. When enabled, the retract wraps the dwell.
                # A retraction that would immediately be followed by the travel
                # retraction of a run starting here is skipped, so a pause landing
                # on the start of a travel run does not cycle the filament twice.
                def _pause_wrap(ms_for_threshold):
                    if pause_prof is None:
                        return False
                    if ms_for_threshold is not None and ms_for_threshold < pause_prof["min_ms"]:
                        return False
                    next_starts_retracted_run = (
                        idx + 1 < len(custom_moves)
                        and seg_travel[idx + 1]
                        and travel_runs.get(run_id_of_seg.get(idx + 1, -1), {}).get("retract")
                        and travel_prof is not None
                    )
                    return not next_starts_retracted_run

                if pause_val == PAUSE_CONFIRM and not pause_gcode:
                    # No known command: emit nothing rather than a line the
                    # firmware ignores, and say so loudly. Silently printing
                    # straight through a pause the operator planned around is
                    # the worst of the available outcomes.
                    pause_stats["unsupported"] += 1
                elif pause_val == PAUSE_CONFIRM:
                    wrap = _pause_wrap(None)
                    merged.append("M400 ; [PAUSE] wait for motion to finish\n")
                    if wrap:
                        merged.append(_retract(pause_prof, "PAUSE"))
                        pause_stats["retractions"] += 1
                    merged.append(f"{pause_gcode} ; [PAUSE] wait for confirmation on the printer display\n")
                    if wrap:
                        merged.append(_prime(pause_prof, "PAUSE"))
                    if parks_on_pause:
                        # Klipper's PAUSE (and Prusa's M601) move the toolhead to
                        # a park position and RESUME returns it -- but only to
                        # where the firmware thinks it should be. Re-commanding
                        # the point makes the path pick up exactly where it
                        # stopped no matter what the firmware did in between.
                        rx = move["x_eff"] if move["x_eff"] is not None else prev_x
                        ry = move["y_eff"] if move["y_eff"] is not None else prev_y
                        rz = move["z_eff"] if move["z_eff"] is not None else prev_z
                        merged.append(
                            f"G1 X{rx:.3f} Y{ry:.3f} Z{rz:.3f} F{f_move:.0f}"
                            f" ; [PAUSE] return to the pause point after resume\n")
                    pause_stats["confirm"] += 1
                elif pause_val < 0:
                    # Any other negative value is a mistake, not a sentinel.
                    pause_stats["invalid"] += 1
                elif pause_val >= 1:
                    ms = int(round(pause_val))
                    wrap = _pause_wrap(ms)
                    merged.append("M400 ; [PAUSE] wait for motion to finish\n")
                    if wrap:
                        merged.append(_retract(pause_prof, "PAUSE"))
                        pause_stats["retractions"] += 1
                    merged.append(f"G4 P{ms} ; [PAUSE] dwell {ms} ms at this point\n")
                    if wrap:
                        merged.append(_prime(pause_prof, "PAUSE"))
                    pause_stats["timed"] += 1
                    pause_stats["total_ms"] += ms
                    pause_stats["max_ms"] = max(pause_stats["max_ms"], ms)
                # 0 (or sub-millisecond) emits nothing at all: with tens of
                # thousands of points, 'G4 P0' everywhere would be dead weight.

            # Update position tracking
            if move["x"] is not None:
                prev_x = move["x"]
            if move["y"] is not None:
                prev_y = move["y"]
            if move["z"] is not None:
                prev_z = move["z"]
            # Track maximum Z from the resolved position, not the emitted word:
            # the first move's Z is commanded by the approach block above.
            if move["z_eff"] is not None and move["z_eff"] > max_z_geometry:
                max_z_geometry = move["z_eff"]
            prev_flow = cur_flow    # carry current point's flow to the next segment
            prev_speed = cur_speed  # carry current point's speed to the next segment

        # Log extrusion statistics
        self.log(f"Extrusion Statistics:", "INFO")
        self.log(f"  Total 3D path length: {extrusion_stats['total_distance']:.2f} mm", "INFO")
        self.log(f"  Total E value: {extrusion_stats['total_e']:.2f} mm", "INFO")
        self.log(f"  Extrusion moves: {extrusion_stats['moves']}", "INFO")
        if extrusion_stats["moves"] > 0:
            avg_e_per_move = extrusion_stats["total_e"] / extrusion_stats["moves"]
            self.log(f"  Average E per move: {avg_e_per_move:.6f} mm", "INFO")

        # Per-step flow summary (from ';GH FLOW=' tags in the custom file)
        if flow_stats["tagged_moves"] > 0:
            avg_flow = flow_stats["sum"] / flow_stats["count"] if flow_stats["count"] else 1.0
            self.log("Per-step flow (from ;GH FLOW tags):", "INFO")
            self.log(f"  Tagged moves: {flow_stats['tagged_moves']} / {len(custom_moves)}", "INFO")
            self.log(f"  Applied flow (averaged) range: {flow_stats['min']:.4f} - {flow_stats['max']:.4f} "
                     f"(avg {avg_flow:.4f})", "INFO")
        else:
            self.log("Per-step flow: no ;GH FLOW tags found (global flow only)", "INFO")

        self.report["per_step_flow"] = {
            "tagged_moves": flow_stats["tagged_moves"],
            "total_moves": len(custom_moves),
            "min": flow_stats["min"],
            "max": flow_stats["max"],
            "avg": (flow_stats["sum"] / flow_stats["count"]) if flow_stats["count"] else None,
        }

        # Per-step speed summary (from ';GH SPEED=' tags)
        if speed_stats["count"] > 0:
            avg_speed = speed_stats["sum"] / speed_stats["count"]
            self.log("Per-step speed:", "INFO")
            self.log(f"  Tagged moves: {speed_stats['tagged_moves']} / {len(custom_moves)}", "INFO")
            self.log(f"  Applied speed (clamped) range: {speed_stats['min']:.1f} - {speed_stats['max']:.1f} mm/s "
                     f"(avg {avg_speed:.1f})", "INFO")
            if speed_stats["clamped"] > 0:
                msg = (f"{speed_stats['clamped']} move(s) had speed clamped to the "
                       f"{SPEED_MIN_MMS:.0f}-{SPEED_MAX_MMS:.0f} mm/s window")
                self.log(f"  WARNING: {msg}", "WARNING")
                self.report["warnings"].append(msg)

        # Volumetric flow-rate warning (speed does not change E, but it changes mm^3/s demand)
        self.log(f"Peak volumetric flow: {vol_stats['max_rate']:.2f} mm^3/s "
                 f"(warn > {MAX_VOL_RATE:.0f})", "INFO")
        if vol_stats["max_rate"] > MAX_VOL_RATE:
            msg = (f"Peak volumetric flow {vol_stats['max_rate']:.1f} mm^3/s exceeds "
                   f"{MAX_VOL_RATE:.0f} mm^3/s - risk of under-extrusion; lower speed or flow")
            self.log(f"WARNING: {msg}", "WARNING")
            self.report["warnings"].append(msg)

        self.report["per_step_speed"] = {
            "tagged_moves": speed_stats["tagged_moves"],
            "total_moves": len(custom_moves),
            "base_speed_mm_s": speed_mm_s,
            "min": speed_stats["min"],
            "max": speed_stats["max"],
            "avg": (speed_stats["sum"] / speed_stats["count"]) if speed_stats["count"] else None,
            "clamped": speed_stats["clamped"],
            "peak_vol_rate": vol_stats["max_rate"],
        }

        # Per-point pause summary (from ';GH PAUSE=' tags)
        total_pauses = pause_stats["timed"] + pause_stats["confirm"]
        if total_pauses or pause_stats["origin_confirm"] or pause_stats["invalid"]:
            self.log("Per-point pauses:", "INFO")
            self.log(f"  Timed dwells: {pause_stats['timed']} "
                     f"(total {pause_stats['total_ms'] / 1000.0:.1f} s, "
                     f"longest {pause_stats['max_ms'] / 1000.0:.1f} s)", "INFO")
            self.log(f"  Confirmation pauses: {pause_stats['confirm']}"
                     + (" (+1 at the 0-Point)" if pause_stats["origin_confirm"] else ""), "INFO")
            if pause_stats["invalid"]:
                msg = (f"{pause_stats['invalid']} PAUSE tag(s) were negative or non-numeric "
                       f"and were ignored (use {PAUSE_CONFIRM} for a confirmation pause)")
                self.log(f"  WARNING: {msg}", "WARNING")
                self.report["warnings"].append(msg)
            if pause_stats["max_ms"] > LONG_PAUSE_WARN_MS:
                msg = (f"Longest dwell is {pause_stats['max_ms'] / 1000.0:.1f} s - the nozzle sits "
                       f"in contact with the print and does not retract; expect ooze/heat marks")
                self.log(f"  WARNING: {msg}", "WARNING")
                self.report["warnings"].append(msg)
            if pause_stats["unsupported"]:
                msg = (f"{pause_stats['unsupported']} confirmation pause(s) (PAUSE=-1) were "
                       f"DROPPED: no pause command is known for this printer "
                       f"({(self.firmware or {}).get('label')}), and emitting one the "
                       f"firmware ignores would print straight through the pause. "
                       f"Re-run with --pause-gcode (M0 for Marlin, PAUSE for Klipper, "
                       f"M601 for Prusa) to enable them")
                self.log(f"  WARNING: {msg}", "WARNING")
                self.report["warnings"].append(msg)
            if pause_stats["confirm"] or pause_stats["origin_confirm"]:
                msg = ("Confirmation pauses are indefinite - the nozzle stays hot and in place "
                       "until you resume from the display")
                self.log(f"  NOTE: {msg}", "WARNING")
                self.report["warnings"].append(msg)
                if parks_on_pause:
                    msg = (f"{(self.firmware or {}).get('label')} parks the toolhead when it "
                           f"pauses, unlike a Bambu 'M400 U1' which holds position. The merged "
                           f"file re-commands each pause point after the resume, so the path "
                           f"picks up correctly - but the nozzle will NOT stay at the apex for "
                           f"the duration of the pause")
                    self.log(f"  NOTE: {msg}", "WARNING")
                    self.report["warnings"].append(msg)

        self.report["travel"] = {
            "segments": travel_stats["segments"],
            "total_moves": len(custom_moves),
            "runs": travel_stats["runs"],
            "retracted_runs": travel_stats["retracted_runs"],
            "retractions": travel_stats["retractions"],
            "distance": travel_stats["distance"],
            "profile": travel_prof,
        }

        self.report["pauses"] = {
            "timed": pause_stats["timed"],
            "confirm": pause_stats["confirm"],
            "total_ms": pause_stats["total_ms"],
            "max_ms": pause_stats["max_ms"],
            "invalid": pause_stats["invalid"],
            "retractions": pause_stats["retractions"],
            "profile": pause_prof,
            "pause_gcode": pause_gcode,
            "pause_gcode_source": pause_gcode_source,
            "unsupported": pause_stats["unsupported"],
            "origin": ({"x": origin["x"], "y": origin["y"], "z": origin["z"],
                        "confirm": origin["confirm"], "pause_ms": origin["pause_ms"]}
                       if origin else None),
        }

        self.report["extrusion_stats"] = extrusion_stats
        self.report["f_values_updated"] = len(custom_moves)

        # ---- End-of-print Z safety ----
        # Two-part fix that works for ANY reference file, print height, and printer:
        #  1) Lift clear of the finished print before the teardown -- far enough
        #     that nothing in the end gcode can touch it. See _resolve_end_park_z
        #     for how far, and why that distance is cross-checked before it is
        #     trusted.
        #  2) Strip every Z move baked into the reference end gcode. Those embed the
        #     *reference* object's height (e.g. "G1 Z{max_layer_z + 0.5} ; lower z a
        #     little") and would drive the nozzle down into a taller custom print.
        # (2) is what makes (1) sufficient: once the reference's own Z moves are
        # gone, the height reached here is the height the teardown runs at.
        # (printable_height / printer_model were read above for the approach block.)
        override = self.config.get("end_park_z")
        if override is not None:
            park_z, z_source = float(override), "config end_park_z override"
        else:
            park_z, z_source = self._resolve_end_park_z(max_z_geometry, printable_height)

        self.log("End-of-Print Safety:", "INFO")
        if printer_model:
            self.log(f"  Printer: {printer_model}", "INFO")
        self.log(f"  Max geometry Z: {max_z_geometry:.2f} mm", "INFO")
        self.log(f"  Park Z (clear of the print): {park_z:.2f} mm  [{z_source}]", "INFO")
        if max_z_geometry > park_z:
            msg = (f"Custom geometry max Z {max_z_geometry:.1f}mm exceeds the park "
                   f"height {park_z:.1f}mm - check the custom file for stray moves")
            self.log(f"  WARNING: {msg}", "WARNING")
            self.report["warnings"].append(msg)

        # Lift clear of the print before the reference teardown runs
        merged.append(f"G1 Z{park_z:.3f} F900 ; [SAFETY] Lift clear of the print before end gcode\n")

        # Hand the extruder back in the mode the reference's end gcode expects.
        if ref_e_absolute:
            merged.append("M82 ; [MODE] back to absolute extrusion for the end gcode\n")
            merged.append("G92 E0 ; [MODE] define the absolute extruder position\n")

        # Neutralize all Z motion in the reference end gcode, then append it
        end_section = self._neutralize_end_gcode_z(reference_data["executable_end"])
        if ref_e_absolute:
            end_section = self._neutralize_end_gcode_absolute_e(end_section)
        merged.extend(end_section)

        # Store Z safety info in report
        self.report["z_safety_info"] = {
            "printer_model": printer_model or "unknown",
            "printable_height": printable_height,
            "max_geometry_z": max_z_geometry,
            "park_z": park_z,
            "park_z_source": z_source,
            "end_gcode_z_removed": self.report.get("end_gcode_z_removed", 0)
        }

        return merged

    def _build_gcode_line(self, move, e_new, f_new, z_override=None):
        """
        Build new gcode line with recalculated E and F values.

        z_override forces an explicit Z (used by the travel z-hop, which must
        emit a Z even on moves that would otherwise inherit it).
        """
        parts = []

        # Movement command (G1 or G0)
        if move["original"].strip().startswith("G0"):
            parts.append("G0")
        else:
            parts.append("G1")

        # Coordinates
        if move["x"] is not None:
            parts.append(f"X{move['x']:.3f}")
        if move["y"] is not None:
            parts.append(f"Y{move['y']:.3f}")
        if z_override is not None:
            parts.append(f"Z{z_override:.3f}")
        elif move["z"] is not None:
            parts.append(f"Z{move['z']:.3f}")

        # Extrusion
        if e_new is not None:
            parts.append(f"E{e_new:.6f}")

        # Feed rate
        if f_new is not None:
            parts.append(f"F{f_new:.0f}")

        return " ".join(parts) + "\n"

    def _get_printer_z_limit(self):
        """
        Resolve the printer's usable Z height and a display name for it.

        Same precedence as _get_printable_area: explicit override > declared by
        the file > printer registry. The height is what the bed is parked at
        before the teardown runs, so it has to be the MACHINE's limit and not
        the reference object's height.

        Returns (printable_height_or_None, printer_name_or_None).
        """
        gh = self._parse_gh_config()
        prof = (self.printer or {}).get("profile")
        model = (self._ref_config_value("printer_model")
                 or (prof["label"] if prof else None)
                 or (self.printer or {}).get("declared_name"))

        override = self.config.get("printable_height", gh.get("printable_height"))
        if override:
            self._height_source = "explicit override"
            return float(override), model

        for key in ("printable_height", "max_print_height"):
            val = self._ref_config_float(key)
            if val:
                self._height_source = f"reference '; {key}'"
                return val, model

        if prof:
            self._height_source = f"printer registry ({prof['label']})"
            return float(prof["height"]), model

        self._height_source = None
        return None, model

    def _get_height_source(self):
        """Where _get_printer_z_limit last got its answer (for the report)."""
        return getattr(self, "_height_source", None)

    def _neutralize_end_gcode_z(self, end_lines):
        """
        Remove every Z motion from the reference end-gcode block.

        Reference end sequences bake in the *reference* object's height — e.g.
        'G1 Z{max_layer_z + 0.5} ; lower z a little' resolves to a low absolute Z
        based on whatever the reference was sliced for. Copied verbatim into a
        taller custom print, those moves drive the nozzle down into the finished
        part. We already raise to full clearance before this block, so here we
        strip Z from every G0/G1 move: the entire teardown then runs at the safe
        park height, independent of the reference file.

        - Moves that still have X/Y/E/F after Z removal are kept (they run at park Z).
        - Z-only moves are commented out entirely (marked [Z-REMOVED]).
        Only G0/G1 are touched; comments and all non-motion commands pass through.
        """
        cleaned = []
        removed = 0
        for line in end_lines:
            s = line.strip()
            if s.startswith("G0 ") or s.startswith("G1 ") or s in ("G0", "G1"):
                code_part, sep, comment = line.partition(";")
                tokens = code_part.split()
                has_z = any(t[:1] == "Z" for t in tokens[1:])
                if has_z:
                    removed += 1
                    kept = [tokens[0]] + [t for t in tokens[1:] if t[:1] != "Z"]
                    actionable = [t for t in kept[1:] if t[:1] in ("X", "Y", "E", "F")]
                    if actionable:
                        new_line = " ".join(kept)
                        if sep:
                            new_line += " ;" + comment.rstrip("\n")
                        cleaned.append(new_line.rstrip() + "\n")
                    else:
                        note = (" (" + comment.strip() + ")") if sep and comment.strip() else ""
                        cleaned.append(f"; [Z-REMOVED] {s}{note}\n")
                    continue
            cleaned.append(line)
        if removed:
            self.log(f"Neutralized {removed} Z move(s) in reference end gcode", "INFO")
        self.report["end_gcode_z_removed"] = removed
        return cleaned

    def _neutralize_end_gcode_absolute_e(self, end_lines):
        """
        Strip absolute-E words from the reference end gcode.

        Exactly the same class of bug as the Z problem this file already fixes,
        on the other axis. Cura signs a print off with

            G1 F2700 E1502.74397

        which is "retract by 0.5 mm" ONLY if the extruder counter is already at
        1503.24. In the merged file it is not -- our geometry extruded a
        different amount -- so that line would command 1.5 METRES of filament.

        Relative-E moves are safe and are kept: they say "give back 2 mm" no
        matter what the counter reads, which is what the wipe/retract lines in
        a typical end sequence are for. So the block is walked with the mode
        tracked (M82/M83, and G91 which makes every axis relative in Marlin),
        and only genuinely absolute E words are removed.

        Only G0/G1 are touched. A move left with no actionable words is
        commented out rather than emitted as a bare 'G1'.
        """
        cleaned = []
        removed = 0
        absolute = True   # we hand over in M82; see _merge_and_recalculate
        rel_all = False   # G91
        for line in end_lines:
            code, sep, comment = line.partition(";")
            s = code.strip()
            head = s.split()[0].upper() if s else ""
            if head == "M82":
                absolute = True
            elif head == "M83":
                absolute = False
            elif head == "G91":
                rel_all = True
            elif head == "G90":
                rel_all = False
            elif head in ("G0", "G1") and absolute and not rel_all:
                tokens = s.split()
                if any(t[:1].upper() == "E" for t in tokens[1:]):
                    removed += 1
                    kept = [tokens[0]] + [t for t in tokens[1:] if t[:1].upper() != "E"]
                    actionable = [t for t in kept[1:] if t[:1].upper() in ("X", "Y", "Z")]
                    if actionable:
                        new_line = " ".join(kept)
                        if sep:
                            new_line += " ;" + comment.rstrip("\n")
                        cleaned.append(new_line.rstrip() + "\n")
                    else:
                        note = (" (" + comment.strip() + ")") if sep and comment.strip() else ""
                        cleaned.append(f"; [E-REMOVED] {s}{note}\n")
                    continue
            cleaned.append(line)
        if removed:
            self.log(f"Neutralized {removed} absolute-E move(s) in reference end gcode", "INFO")
        self.report["end_gcode_e_removed"] = removed
        return cleaned

    def _start_block_residual_e(self, start_lines):
        """
        Net filament left extruded (+) or retracted (-) by the reference's
        start gcode, measured from its last 'G92 E' reset.

        Cura's start block ends on 'G1 F2700 E-0.5' -- the nozzle is parked
        retracted, ready for a travel. Zeroing the counter there without giving
        that 0.5 mm back would leave the first few millimetres of the path
        running dry.
        """
        absolute = True
        rel_all = False
        e_abs = 0.0
        residual = 0.0
        for line in start_lines:
            s = line.split(";", 1)[0].strip()
            if not s:
                continue
            head = s.split()[0].upper()
            if head == "M82":
                absolute = True
                continue
            if head == "M83":
                absolute = False
                continue
            if head == "G91":
                rel_all = True
                continue
            if head == "G90":
                rel_all = False
                continue
            if head == "G92":
                for tok in s.split()[1:]:
                    if tok[:1].upper() == "E":
                        try:
                            e_abs = float(tok[1:])
                        except ValueError:
                            e_abs = 0.0
                        residual = 0.0
                continue
            if head not in ("G0", "G1"):
                continue
            for tok in s.split()[1:]:
                if tok[:1].upper() != "E":
                    continue
                try:
                    val = float(tok[1:])
                except ValueError:
                    continue
                if absolute and not rel_all:
                    residual += val - e_abs
                    e_abs = val
                else:
                    residual += val
        return residual

    def _process_bed_leveling(self, section):
        """
        Handle bed leveling commands based on firmware type.

        When disabled: strips firmware-specific bed leveling commands.
        When enabled: keeps existing commands (doesn't inject new ones for Bambu safety).
        """
        bed_leveling_enabled = self.settings.get("bed_leveling", True)
        firmware_key = self.firmware_key if hasattr(self, "firmware_key") else "marlin"

        if bed_leveling_enabled:
            # Bed leveling enabled: keep reference's existing commands
            # Don't inject new ones to avoid breaking Bambu files
            return section

        # Bed leveling disabled: strip firmware-specific commands
        cleaned = []
        for line in section:
            s = line.strip()
            should_keep = True

            # Check firmware-specific bed leveling patterns to remove
            if firmware_key == "bambu":
                # Bambu: strip G29 with any parameters (bed leveling probe)
                if s.startswith("G29") or (s.startswith("G0 ") and "G29" in s):
                    should_keep = False
            elif firmware_key == "klipper":
                # Klipper: strip probe/tilt commands
                if any(cmd in s.upper() for cmd in ["QUAD_GANTRY_LEVEL", "Z_TILT_ADJUST",
                                                      "PROBE_CALIBRATE", "G29"]):
                    should_keep = False
            elif firmware_key in ("prusa_buddy", "prusa_einsy"):
                # Prusa: strip G29 (mesh bed leveling)
                if s.startswith("G29"):
                    should_keep = False
            else:  # marlin, rrf, unknown
                # Marlin/others: strip G29 (bilinear bed leveling)
                if s.startswith("G29"):
                    should_keep = False

            if should_keep:
                cleaned.append(line)
            else:
                # Comment out stripped lines for debugging
                if s.startswith(";"):
                    cleaned.append(line)
                else:
                    cleaned.append(f"; [BED_LEVEL_DISABLED] {s}\n")

        return cleaned

    def _update_m73_commands(self, merged_gcode, total_seconds):
        """
        Recalculate M73 progress commands AFTER geometry starts.

        M73 commands report progress to the printer display:
        - M73 P<percent> R<remaining_minutes>
        - M73 L<layer_number>

        CRITICAL: Only update M73 commands AFTER the geometry starts, not in
        the preamble. The preamble's M73 commands use the reference's time
        estimates, which the printer firmware expects. Changing them to 3
        minutes (our merged geometry time) causes the firmware to hang during
        filament loading, thinking the print is very short.

        This function:
        1. Finds the first actual geometry move (G0/G1 with X/E)
        2. Preserves all M73 commands in the preamble unchanged
        3. Updates M73 commands after geometry starts to reflect actual progress

        Returns updated merged_gcode with new M73 values after geometry.
        """
        if total_seconds <= 0:
            return merged_gcode

        # Find where geometry starts (first G0/G1 with coordinates and extrusion)
        geometry_start = None
        for i, line in enumerate(merged_gcode):
            s = line.strip()
            if (s.startswith("G0 ") or s.startswith("G1 ")) and " X" in s and " E" in s:
                geometry_start = i
                break

        if geometry_start is None:
            # No geometry found, don't modify anything
            return merged_gcode

        # Keep preamble (lines before geometry) unchanged
        updated = merged_gcode[:geometry_start]

        # Update M73 commands only from geometry onward
        elapsed_time = 0.0  # seconds so far
        prev_x, prev_y, prev_z = 0.0, 0.0, 0.0

        for idx in range(geometry_start, len(merged_gcode)):
            line = merged_gcode[idx]
            s = line.strip()

            # Track time for G0/G1 moves (same logic as _calculate_print_time)
            if s.startswith("G0 ") or s.startswith("G1 "):
                code_part, _, comment_part = s.partition(";")
                parts = code_part.split()

                x, y, z, f = prev_x, prev_y, prev_z, None
                for part in parts[1:]:
                    try:
                        if part.startswith("X"):
                            x = float(part[1:])
                        elif part.startswith("Y"):
                            y = float(part[1:])
                        elif part.startswith("Z"):
                            z = float(part[1:])
                        elif part.startswith("F"):
                            f = float(part[1:])
                    except ValueError:
                        continue

                distance = math.sqrt(
                    (x - prev_x) ** 2 +
                    (y - prev_y) ** 2 +
                    (z - prev_z) ** 2
                )

                if f is not None and distance > 0.001:
                    speed_mm_s = f / 60.0
                    move_time = distance / speed_mm_s
                    elapsed_time += move_time

                prev_x, prev_y, prev_z = x, y, z

            # Track time for G4 dwell commands
            elif s.startswith("G4 "):
                parts = s.split()
                for part in parts[1:]:
                    if part.startswith("P"):
                        try:
                            dwell_ms = float(part[1:])
                            elapsed_time += dwell_ms / 1000.0
                        except ValueError:
                            pass

            # Update M73 P/R commands with progress based on actual print time
            if s.startswith("M73 P"):
                if "R" in s:
                    remaining_seconds = max(0, total_seconds - elapsed_time)
                    remaining_minutes = max(0, remaining_seconds / 60.0)
                    percent = min(100, int(round((elapsed_time / total_seconds) * 100)))
                    updated.append(f"M73 P{percent} R{int(remaining_minutes)}\n")
                    continue
            elif s.startswith("M73 L"):
                # Keep layer commands as-is; they don't need time updates
                pass

            updated.append(line)

        return updated

    def _calculate_print_time(self, merged_gcode):
        """
        Calculate total estimated print time from merged gcode.

        Walks through all moves and dwells:
        - G0/G1: distance / (F / 60) where F is mm/min
        - G4 P<ms>: adds dwell time in milliseconds

        Returns dict with total_seconds, move_time, dwell_time, move_count, dwell_count.
        """
        total_move_time = 0.0  # seconds
        total_dwell_time = 0.0  # seconds (converted from ms)
        move_count = 0
        dwell_count = 0
        print_time = 0.0     # moves that lay material
        travel_time = 0.0    # moves that do not
        travel_distance = 0.0

        prev_x, prev_y, prev_z = 0.0, 0.0, 0.0

        for line in merged_gcode:
            s = line.strip()

            # Parse G0/G1 moves
            if s.startswith("G0 ") or s.startswith("G1 "):
                # Split code from comment
                code_part, _, _ = s.partition(";")
                parts = code_part.split()

                x, y, z, f = prev_x, prev_y, prev_z, None
                extruding = False

                # Extract coordinates and feedrate
                for part in parts[1:]:
                    if part[:1] == "E":
                        extruding = True
                    try:
                        if part.startswith("X"):
                            x = float(part[1:])
                        elif part.startswith("Y"):
                            y = float(part[1:])
                        elif part.startswith("Z"):
                            z = float(part[1:])
                        elif part.startswith("F"):
                            f = float(part[1:])  # mm/min
                    except ValueError:
                        continue

                # Calculate 3D distance
                distance = math.sqrt(
                    (x - prev_x) ** 2 +
                    (y - prev_y) ** 2 +
                    (z - prev_z) ** 2
                )

                # If we have a feedrate and distance, calculate time
                if f is not None and distance > 0.001:
                    speed_mm_s = f / 60.0
                    move_time = distance / speed_mm_s
                    total_move_time += move_time
                    move_count += 1
                    if extruding:
                        print_time += move_time
                    else:
                        travel_time += move_time
                        travel_distance += distance

                prev_x, prev_y, prev_z = x, y, z

            # Parse G4 dwell commands
            elif s.startswith("G4 "):
                parts = s.split()
                for part in parts[1:]:
                    if part.startswith("P"):
                        try:
                            dwell_ms = float(part[1:])
                            total_dwell_time += dwell_ms / 1000.0  # convert to seconds
                            dwell_count += 1
                        except ValueError:
                            pass

        total_seconds = total_move_time + total_dwell_time

        result = {
            "total_seconds": total_seconds,
            "move_time_seconds": total_move_time,
            "dwell_time_seconds": total_dwell_time,
            "move_count": move_count,
            "dwell_count": dwell_count,
            "printing_time_s": print_time,
            "travel_time_s": travel_time,
            "travel_distance_mm": travel_distance,
        }

        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = int(total_seconds % 60)
        time_str = f"{hours}h {minutes}m {seconds}s"

        self.log(f"Estimated print time: {time_str} ({total_seconds:.0f} seconds total)", "INFO")
        if move_count > 0:
            self.log(f"  - Print moves: {move_count} moves, {total_move_time:.0f}s", "INFO")
        if dwell_count > 0:
            self.log(f"  - Dwells (G4): {dwell_count} pauses, {total_dwell_time:.0f}s", "INFO")

        return result

    def _write_output(self, merged_gcode):
        """
        Write the merged gcode in whatever container the reference came in.

        .3mf -- the merged plate gcode goes back into the extracted tree, its
        md5 sidecar is recomputed, and the archive is rebuilt.
        .gcode -- one text file, next to the inputs, named '<custom>_merged.gcode'
        so a re-run can never mistake it for either input.

        LF line endings are pinned in both cases: that is what the slicers
        themselves emit, and letting Windows text mode rewrite them as CRLF
        made the CLI and the browser build produce different bytes for
        identical inputs.

        Under --dry-run the whole merge still runs -- so the volume check, the
        calibration and the estimates are all real -- and only this last step is
        skipped. A dry run that stopped earlier would not be checking the thing
        it claims to check.
        """
        if self.dry_run:
            self.log(f"DRY RUN: would write {self.output_path.name} "
                     f"({len(merged_gcode)} lines). Nothing written.", "INFO")
            return

        if self.ref_format == "3mf":
            plate_gcode = self.temp_dir / "Metadata" / "plate_1.gcode"
            with open(plate_gcode, 'w', newline='\n') as f:
                f.writelines(merged_gcode)
            self.log("Updating metadata...")
            self._update_metadata()
            self.log("Recompressing to 3mf format...")
            self._recompress_3mf()
        else:
            merged_gcode = self._update_marlin_header(merged_gcode)
            with open(self.output_path, 'w', newline='\n') as f:
                f.writelines(merged_gcode)
            self.log(f"Wrote merged gcode: {self.output_path.name} "
                     f"({len(merged_gcode)} lines)", "INFO")

    def _update_marlin_header(self, merged_gcode):
        """
        Refresh the reference's own summary comments so the printer's display
        does not advertise the reference object's numbers.

        Cura writes ';TIME:', ';Filament used:', ';LAYER_COUNT:' and a
        ';MINX/;MAXZ' bounding box at the top of the file, and firmware and
        host software read them for the progress/ETA screen and the preview.
        Every one of them describes the object we just threw away, so a stale
        ';LAYER_COUNT:500' or a bounding box from the reference cube is worse
        than no value at all. They are rewritten from this merge's own figures,
        or dropped where there is no longer a meaningful value.

        Purely cosmetic -- nothing about the motion depends on any of it -- and
        keys the reference never wrote are never invented.
        """
        ept = self.report.get("estimated_print_time") or {}
        seconds = ept.get("total_seconds") or 0
        total_e_mm = (self.report.get("extrusion_stats") or {}).get("total_e") or 0.0
        bv = self.report.get("build_volume") or {}
        gx, gy, gz = bv.get("geometry_x"), bv.get("geometry_y"), bv.get("geometry_z")

        bbox = {}
        for key, span in (("X", gx), ("Y", gy), ("Z", gz)):
            if span and span[0] is not None and span[1] is not None:
                bbox[f";MIN{key}:"] = f";MIN{key}:{span[0]:.3f}\n"
                bbox[f";MAX{key}:"] = f";MAX{key}:{span[1]:.3f}\n"

        updated = []
        changed = 0
        for line in merged_gcode:
            s = line.strip()
            if s.startswith(";TIME:") and seconds:
                updated.append(f";TIME:{int(round(seconds))}\n")
                changed += 1
                continue
            if s.startswith(";Filament used:") and total_e_mm:
                updated.append(f";Filament used: {total_e_mm / 1000.0:.4f}m\n")
                changed += 1
                continue
            if s.startswith(";TIME_ELAPSED:") or s.startswith(";LAYER_COUNT:"):
                # Per-layer bookkeeping for geometry that is no longer here.
                # A Grasshopper path is one continuous polyline, not layers.
                changed += 1
                continue
            replaced = next((v for k, v in bbox.items() if s.startswith(k)), None)
            if replaced:
                updated.append(replaced)
                changed += 1
                continue
            updated.append(line)
        if changed:
            self.log(f"Refreshed {changed} slicer summary comment(s) in the output header",
                     "INFO")
        return updated

    def _update_metadata(self):
        """Recalculate MD5 hash for merged gcode"""
        # Read back the merged gcode file
        plate_gcode = self.temp_dir / "Metadata" / "plate_1.gcode"
        with open(plate_gcode, 'rb') as f:
            gcode_content = f.read()

        md5_hash = hashlib.md5(gcode_content).hexdigest()

        # Update MD5 file
        md5_file = self.temp_dir / "Metadata" / "plate_1.gcode.md5"
        with open(md5_file, 'w') as f:
            f.write(md5_hash)

        self.log(f"MD5 Hash (new gcode): {md5_hash}", "INFO")

    def _recompress_3mf(self):
        """
        Recompress temp directory back to 3mf file, preserving original structure.

        .3mf is a ZIP archive with strict requirements:
        - [Content_Types].xml MUST be stored uncompressed and FIRST
        - Other files use deflate compression
        - File order and timestamps matter for BambuStudio compatibility

        Strategy: Read the original reference .3mf to understand its structure,
        then recreate it with updated files from the merged temp directory.
        This ensures BambuStudio and the printer recognize the file correctly.
        """
        output_path = self.output_3mf

        # Map of which files should be stored uncompressed vs. deflated
        # (Based on .3mf spec and BambuStudio requirements)
        stored_files = {"[Content_Types].xml", "_rels/.rels"}

        # Build a dict of all updated files in the temp directory
        updated_files = {}
        for root, dirs, files in os.walk(self.temp_dir):
            for file in files:
                file_path = Path(root) / file
                arcname = str(file_path.relative_to(self.temp_dir)).replace("\\", "/")
                updated_files[arcname] = file_path

        # Create new zip with [Content_Types].xml first and uncompressed
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Write [Content_Types].xml first, uncompressed
            if "[Content_Types].xml" in updated_files:
                content_types_file = updated_files["[Content_Types].xml"]
                zipf.write(content_types_file, "[Content_Types].xml",
                          compress_type=zipfile.ZIP_STORED)

            # Write all other files in sorted order (deterministic)
            for arcname in sorted(updated_files.keys()):
                if arcname == "[Content_Types].xml":
                    continue  # Already written

                file_path = updated_files[arcname]

                # Determine compression type based on file extension and name
                if arcname in stored_files or arcname.endswith(".xml"):
                    compress_type = zipfile.ZIP_STORED
                else:
                    compress_type = zipfile.ZIP_DEFLATED

                # Preserve file timestamp from original for consistency
                # (zipfile writes current time by default)
                zinfo = zipfile.ZipInfo(filename=arcname)
                zinfo.compress_type = compress_type

                with open(file_path, 'rb') as f:
                    zipf.writestr(zinfo, f.read())

        self.log(f"Recompressed to .3mf (preserving structure for BambuStudio compatibility)", "INFO")

    def _generate_report(self):
        """Generate merge report"""
        # self.custom_gcode.stem includes the full name without extension(s)
        # For "testVase2.gcode", stem is "testVase2"
        output_file = self.project_dir / f"{self.custom_gcode.stem}_merge_report.txt"
        self.report_path = output_file

        pinfo = self.report.get("printer", {}) or {}
        bv = self.report.get("build_volume", {}) or {}
        area = bv.get("printable_area")
        cal = self.report.get("test_extrusion_reference", {}) or {}
        est = self.report.get("estimates", {}) or {}

        report_text = f"""
================================================================================
                    GCode Merger Report
================================================================================
Timestamp: {self.report['timestamp']}
Project: {self.report['project']}

FILES
================================================================================
Reference File: {self.report['reference_file']}
Reference Format: {'BambuStudio .gcode.3mf' if self.ref_format == '3mf' else 'sliced Marlin-flavour .gcode'}
Custom File: {self.report['custom_file']}
Output File: {self.output_path.name}

PRINTER
================================================================================
Detected: {pinfo.get('detected') or 'not identified'}
Declared in reference as: {pinfo.get('declared_name') or 'nothing'}
Firmware: {pinfo.get('firmware_label') or 'unknown'} [{pinfo.get('firmware_source') or 'n/a'}]
Extruder mode in reference: {self.report.get('reference_e_mode', 'relative (M83)')}
Build volume: {(f"X {area[0]:.0f}-{area[2]:.0f}, Y {area[1]:.0f}-{area[3]:.0f}" if area else 'X/Y unknown')}, {(f"Z 0-{bv.get('printable_height'):.0f}" if bv.get('printable_height') else 'Z unknown')} mm
  X/Y source: {self._get_bed_source() or 'none - X/Y check was skipped'}
  Z source:   {self._get_height_source() or 'none - Z check was skipped'}
Confirmation pause command: {self.report.get('pauses', {}).get('pause_gcode') or 'none available'}

EXTRUSION CALIBRATION
================================================================================
E per mm: {cal.get('e_per_mm', 0):.6f}
Source: {cal.get('source', 'unknown')}
""" + (f"""Samples: {cal.get('moves')} printing moves | weighted mean {cal.get('e_per_mm_weighted_mean', 0):.6f} \
| range {cal.get('e_per_mm_min', 0):.6f}-{cal.get('e_per_mm_max', 0):.6f}
""" if cal.get("e_per_mm_weighted_mean") else "") + (f"""
ESTIMATES
================================================================================
Print time:         {est.get('print_time_text', 'n/a')}
  Printing motion:  {format_duration(est.get('printing_time_s', 0))}
  Travel motion:    {format_duration(est.get('travel_time_s', 0))} ({est.get('travel_distance_mm', 0) / 1000.0:.2f} m)
  Dwells (G4):      {format_duration(est.get('dwell_time_s', 0))}
Material:           {est.get('total_grams', 0):.2f} g
  Geometry:         {est.get('filament_grams', 0):.2f} g ({est.get('filament_length_mm', 0) / 1000.0:.2f} m, {est.get('filament_volume_mm3', 0) / 1000.0:.2f} cm3)
  Priming line:     {est.get('priming_grams', 0):.2f} g ({est.get('priming_length_mm', 0) / 1000.0:.2f} m, from the reference start gcode)
Filament:           dia {est.get('filament_diameter_mm', 0)} mm, density {est.get('filament_density_g_cm3', 0)} g/cm3
Note: acceleration and junction deviation are NOT modelled, so the time is an
      optimistic floor - worst on paths made of many short segments. Heat-up,
      bed levelling and the teardown sequence are not counted either. A real
      print takes somewhat longer.
""" if est else "") + f"""
CONFIGURATION
================================================================================
Source: {'GH_CONFIG header' if self.settings.get('from_header') else 'config.yaml / defaults'}

Base Print Speed:
  Original: {self.report.get('original_speed_mm_s', self.settings.get('speed_mm_s', 100.0))} mm/s
  Applied: {self.settings.get('speed_mm_s')} mm/s """ + (f"(multiplied by {self.report.get('speed_multiplier_applied', 1.0)}x)" if self.report.get('speed_multiplier_applied', 1.0) != 1.0 else "") + f"""

Global Flow Multiplier:
  Original: {self.report.get('original_flow', self.settings.get('flow', 1.0))}x
  Applied: {self.settings.get('flow')}x """ + (f"(multiplied by {self.report.get('flow_multiplier_applied', 1.0)}x)" if self.report.get('flow_multiplier_applied', 1.0) != 1.0 else "") + f"""

Bed Leveling (via firmware {self.firmware_key}):
  Original: {('ENABLED' if self.report.get('original_bed_leveling', self.settings.get('bed_leveling')) else 'DISABLED')}
  Applied: {('ENABLED' if self.settings.get('bed_leveling') else 'DISABLED')} """ + (f"(user override)" if self.report.get('bed_leveling_override') is not None else "") + f"""

Per-Step Variations (from ;GH tags):
  Speed tags in geometry: {self.report.get('per_step_speed', {}).get('tagged_moves', 0)} moves
  Flow tags in geometry: {self.report.get('per_step_flow', {}).get('tagged_moves', 0)} moves

PROCESSING RESULTS
================================================================================
Total Moves Processed: {self.report['moves_processed']}
E-Values Recalculated: {self.report['e_values_recalculated']}
F-Values Updated: {self.report['f_values_updated']}
"""

        if self.report.get("estimated_print_time"):
            ept = self.report["estimated_print_time"]
            hours = int(ept["total_seconds"] // 3600)
            minutes = int((ept["total_seconds"] % 3600) // 60)
            seconds = int(ept["total_seconds"] % 60)
            report_text += f"""
ESTIMATED PRINT TIME
================================================================================
Total: {hours}h {minutes}m {seconds}s ({ept["total_seconds"]:.0f} seconds)
Print Moves: {ept["move_count"]} moves ({ept["move_time_seconds"]:.0f}s)
Pauses (G4 dwells): {ept["dwell_count"]} dwells ({ept["dwell_time_seconds"]:.0f}s)
Note: calculated from gcode moves (G0/G1 distance/speed) and dwell times (G4 P values).
"""

        report_text += f"""
EXTRUSION REFERENCE SAMPLE
================================================================================
"""
        if self.report.get("test_extrusion_reference"):
            ref = self.report["test_extrusion_reference"]
            report_text += f"""Total E: {ref.get('total_e', 0):.2f} mm
Total Distance: {ref.get('total_distance', 0):.2f} mm
E-per-mm: {ref.get('e_per_mm', 0):.6f}
Moves analyzed: {ref.get('moves', 0)}
"""
        else:
            report_text += "Could not extract test extrusion reference\n"

        report_text += f"""
EXTRUSION CALCULATIONS
================================================================================
"""
        if self.report.get("extrusion_stats"):
            stats = self.report["extrusion_stats"]
            report_text += f"""Total 3D Path Length: {stats.get('total_distance', 0):.2f} mm
Total Filament Extruded: {stats.get('total_e', 0):.2f} mm
Extrusion Moves: {stats.get('moves', 0)}
"""

        psf = self.report.get("per_step_flow")
        if psf:
            report_text += f"""
PER-STEP FLOW (from ;GH FLOW= tags)
================================================================================
Tagged Moves: {psf.get('tagged_moves', 0)} / {psf.get('total_moves', 0)}
"""
            if psf.get("tagged_moves"):
                report_text += f"""Applied Flow (averaged) Min: {psf.get('min'):.4f}
Applied Flow (averaged) Max: {psf.get('max'):.4f}
Applied Flow (averaged) Avg: {psf.get('avg'):.4f}
Note: per-step flow is averaged across each segment's two endpoints and
      multiplied onto the reference-based E (x global flow multiplier).
"""
            else:
                report_text += "No per-step flow tags found; global flow multiplier only.\n"

        pss = self.report.get("per_step_speed")
        if pss:
            report_text += f"""
PER-STEP SPEED (from ;GH SPEED= tags)
================================================================================
Tagged Moves: {pss.get('tagged_moves', 0)} / {pss.get('total_moves', 0)}
Base Speed: {pss.get('base_speed_mm_s')} mm/s
"""
            if pss.get("min") is not None:
                report_text += f"""Applied Speed (clamped) Min: {pss.get('min'):.1f} mm/s
Applied Speed (clamped) Max: {pss.get('max'):.1f} mm/s
Applied Speed (clamped) Avg: {pss.get('avg'):.1f} mm/s
Moves Clamped to {SPEED_MIN_MMS:.0f}-{SPEED_MAX_MMS:.0f} mm/s: {pss.get('clamped', 0)}
Peak Volumetric Flow: {pss.get('peak_vol_rate', 0):.2f} mm^3/s (warn > {MAX_VOL_RATE:.0f})
Note: per-step speed is a multiplier on the base speed, averaged across each
      segment's endpoints; it changes F only, never the extruded amount per mm.
"""

        tv = self.report.get("travel")
        if tv and tv.get("segments"):
            prof = tv.get("profile")
            report_text += f"""
TRAVEL (from ;GH TRAVEL= tags)
================================================================================
Travel Segments: {tv.get('segments', 0)} / {max(tv.get('total_moves', 1) - 1, 0)}
Travel Runs: {tv.get('runs', 0)}
Total Travel Distance: {tv.get('distance', 0):.2f} mm
Runs Long Enough to Retract: {tv.get('retracted_runs', 0)}
Retractions Emitted: {tv.get('retractions', 0)}
"""
            if prof:
                report_text += f"""Retract Length: {prof['length']:.3f} mm @ {prof['speed']:.0f} mm/s
Prime Speed: {prof['deretract_speed']:.0f} mm/s
Minimum Run Length to Retract: {prof['min_dist']:.2f} mm
Z-Hop: {('%.2f mm' % prof['zhop']) if prof['zhop'] else 'none (polyline trusted as drawn)'}
Source: {'GH_SETTINGS header' if prof['from_settings'] else 'reference config defaults'}
"""
            else:
                report_text += "Retraction: DISABLED\n"
            report_text += ("Note: a segment is travel only when BOTH its endpoints carry TRAVEL=1.\n"
                            "      Retraction is decided per contiguous run, not per segment, and\n"
                            "      runs shorter than the minimum are left un-retracted.\n"
                            "      Travel moves follow the polyline exactly and carry no E word;\n"
                            "      feedrate still comes from the per-point SPEED channel.\n")

        pz = self.report.get("pauses")
        if pz and (pz.get("timed") or pz.get("confirm") or pz.get("origin")):
            report_text += f"""
PAUSES & 0-POINT
================================================================================
Pause command: {pz.get('pause_gcode') or 'none available'} [{pz.get('pause_gcode_source') or 'n/a'}]
Timed Dwells: {pz.get('timed', 0)}
Total Added Dwell Time: {pz.get('total_ms', 0) / 1000.0:.1f} s
Longest Single Dwell: {pz.get('max_ms', 0) / 1000.0:.1f} s
Confirmation Pauses (in geometry): {pz.get('confirm', 0)}
Ignored/Invalid PAUSE Tags: {pz.get('invalid', 0)}
"""
            pprof = pz.get("profile")
            if pprof:
                report_text += f"""Pause Retraction: {pprof['length']:.3f} mm @ {pprof['speed']:.0f} mm/s (prime {pprof['deretract_speed']:.0f} mm/s)
Applied to Dwells >= {pprof['min_ms']:.0f} ms | Retractions Emitted: {pz.get('retractions', 0)}
Pause Z-Hop: {('%.2f mm' % pprof['zhop']) if pprof['zhop'] else 'none'}
"""
            else:
                report_text += ("Pause Retraction: DISABLED (no PAUSE_* settings supplied - the "
                                "nozzle holds\n                  melt pressure so the strand can freeze in place)\n")
            o = pz.get("origin")
            if o:
                if o.get("confirm"):
                    o_mode = "wait for confirmation on the display"
                elif o.get("pause_ms"):
                    o_mode = f"timed pause {o['pause_ms']:.0f} ms"
                else:
                    o_mode = "no pause"
                report_text += f"""0-Point: X{o['x']:.3f} Y{o['y']:.3f} Z{o['z']:.3f}  [{o_mode}]
Note: the 0-Point is reached AFTER the reference preamble, so all probing,
      purging and wiping still happens on an empty plate. Place the object to
      print on during that pause. The move from the 0-Point to the first print
      position is direct (no lift), preserving the registration you set by hand.
"""
            else:
                report_text += ("0-Point: not defined (standard lift -> travel -> descend "
                                "approach used)\n")
            report_text += ("Note: pauses are emitted at the tagged point itself and are never\n"
                            "      averaged. The nozzle holds position - it is not lifted,\n"
                            "      parked, retracted or cooled - so a strand can freeze in place.\n")

        bv = self.report.get("build_volume")
        if bv:
            def _rng(pair, unit=" mm"):
                lo, hi = pair
                if lo is None or hi is None:
                    return "n/a"
                return f"{lo:.3f} to {hi:.3f}{unit}"

            area = bv.get("printable_area")
            area_txt = (f"X {area[0]:.1f}-{area[2]:.1f}, Y {area[1]:.1f}-{area[3]:.1f} mm"
                        if area else "not declared by reference")
            height = bv.get("printable_height")
            height_txt = f"{height:.1f} mm" if height is not None else "not declared by reference"
            report_text += f"""
BUILD VOLUME CHECK (passed)
================================================================================
Printer: {bv.get('printer_model', 'unknown')}
Printable Area: {area_txt}
Printable Height: {height_txt}
Geometry X: {_rng(bv.get('geometry_x', [None, None]))}
Geometry Y: {_rng(bv.get('geometry_y', [None, None]))}
Geometry Z: {_rng(bv.get('geometry_z', [None, None]))}
Note: geometry outside the build volume (Z below 0, above the printer's Z limit,
      or beyond the printable area) is a fatal error - no file is written.
"""

        if self.report.get("z_safety_info"):
            z_info = self.report["z_safety_info"]
            report_text += f"""
END-OF-PRINT SAFETY
================================================================================
Printer: {z_info.get('printer_model', 'unknown')}
Printer Z Limit (printable_height): {z_info.get('printable_height')} mm
Max Geometry Z Height: {z_info.get('max_geometry_z', 0):.2f} mm
Park Z (Clear of the Print): {z_info.get('park_z', 0):.2f} mm  [{z_info.get('park_z_source', '')}]
Clearance Above Print: {z_info.get('park_z', 0) - z_info.get('max_geometry_z', 0):.2f} mm
Reference End-GCode Z Moves Removed: {z_info.get('end_gcode_z_removed', 0)}
Note: The toolhead is lifted clear of the finished print before the end sequence,
      and every Z move baked into the reference end gcode is stripped, so the
      teardown runs safely above the print regardless of reference file, print
      height, or printer model. The lift is {END_PARK_CLEARANCE_MM:.0f} mm above the
      highest geometry Z, capped at the printer's own Z limit. The print height is
      measured twice from the custom moves and the two must agree; if they ever do
      not, this falls back to parking at the printer's Z limit and says so above.

"""

        report_text += f"""
NOTES
================================================================================
- All startup and teardown sequences taken verbatim from the reference file
- Custom geometry extracted (preamble removed); the reference's own geometry is discarded
- Extrusion reverse-engineered from the reference (see EXTRUSION CALIBRATION above)
- Global speed/flow/bed-leveling read from the ;GH_CONFIG header (config.yaml optional)
- Per-step FLOW and SPEED applied from ;GH tags (averaged across each segment)
- Bed parked at the printer's Z limit; reference end-gcode Z moves stripped
- Output file is ready to load on your printer

================================================================================
"""

        if self.dry_run:
            self.report_path = None
            report_text += ("\nDRY RUN: this report was not saved, and no merged "
                            "file was written.\n")
        else:
            with open(output_file, 'w') as f:
                f.write(report_text)

        print(report_text)


def format_analysis(data):
    """
    Render an :meth:`GCodeMerger.analyze` dict as text for the terminal.

    The browser build draws the same dict as the Analyze card. Only the drawing
    differs -- every number here comes from that one analysis, so the two front
    ends cannot disagree about what is in the files.
    """
    if not data.get("ok"):
        return f"ANALYSIS FAILED: {data.get('error', 'unknown error')}"

    f, g = data["files"], data["geometry"]
    c, v = data["current_settings"], data["per_step_variations"]
    pz, ri = data["pauses"], data["reference_info"]

    def rng(axis):
        a = g[axis]
        if a["min"] is None:
            return "n/a"
        return f"{a['min']:.2f} .. {a['max']:.2f} mm  (span {a['max'] - a['min']:.2f})"

    lines = [
        "=" * 78,
        "PRE-FLIGHT ANALYSIS  (nothing has been written)",
        "=" * 78,
        "",
        "FILES",
        f"  Reference : {f['reference']}  [{f['reference_format']}]",
        f"  Custom    : {f['custom']}",
        "",
        "MACHINE (from the reference)",
        f"  Printer         : {ri['printer_label'] or 'not recognised'}"
        + (f"  [{ri['printer_slug']}]" if ri["printer_slug"] else ""),
        f"  Firmware        : {ri['firmware']}",
        f"  Nozzle          : {c['nozzle_diameter_mm']} mm",
        f"  Filament        : {c['filament_diameter_mm']} mm"
        + (f"  {c['material_type']}" if c["material_type"] else ""),
        f"  Bed levelling   : {'present in start block' if ri['has_bed_leveling'] else 'none found'}",
        "",
        "GLOBAL SETTINGS THAT WILL BE USED",
        f"  Base speed      : {c['base_speed_mm_s']} mm/s",
        f"  Global flow     : {c['global_flow']}x",
        f"  Bed levelling   : {'ENABLED (kept)' if c['bed_leveling_enabled'] else 'DISABLED (stripped)'}",
        f"  ;GH_CONFIG      : {data['gh_config_line'] or 'not present -- defaults in use'}",
        "",
        "GEOMETRY",
        f"  Moves           : {g['move_count']}",
        f"  X               : {rng('x')}",
        f"  Y               : {rng('y')}",
        f"  Z               : {rng('z')}",
        "",
        "PER-STEP ;GH TAGS",
    ]

    if v["speed_tags_found"]:
        lines.append(f"  SPEED tags      : {v['speed_tags_found']} moves, "
                     f"{v['speed_min_mult']}x .. {v['speed_max_mult']}x"
                     + ("  (varied)" if v["speed_varied"] else "  (uniform)"))
        eff_lo = c["base_speed_mm_s"] * v["speed_min_mult"]
        eff_hi = c["base_speed_mm_s"] * v["speed_max_mult"]
        lines.append(f"                    -> {eff_lo:.1f} .. {eff_hi:.1f} mm/s effective")
        if eff_lo < SPEED_MIN_MMS or eff_hi > SPEED_MAX_MMS:
            lines.append(f"                    !! outside the clamp range "
                         f"[{SPEED_MIN_MMS} .. {SPEED_MAX_MMS}] -- values will be clamped")
    else:
        lines.append("  SPEED tags      : none -- every move uses the base speed")

    if v["flow_tags_found"]:
        lines.append(f"  FLOW tags       : {v['flow_tags_found']} moves, "
                     f"{v['flow_min_mult']}x .. {v['flow_max_mult']}x"
                     + ("  (varied)" if v["flow_varied"] else "  (uniform)"))
    else:
        lines.append("  FLOW tags       : none -- every move uses the global flow")

    lines += [
        "",
        "PAUSES",
        f"  Confirmation    : {pz['confirm_count']}"
        + ("  (the printer waits for you at each one)" if pz["confirm_count"] else ""),
        f"  Timed           : {pz['timed_count']}"
        + (f"  totalling {format_duration(pz['total_pause_ms'] / 1000.0)}"
           if pz["timed_count"] else ""),
    ]
    if pz["total_pause_ms"] and pz["timed_count"]:
        longest = pz["total_pause_ms"] / max(pz["timed_count"], 1)
        if longest > LONG_PAUSE_WARN_MS:
            lines.append(f"                    !! averaging {longest / 1000.0:.1f} s per pause -- "
                         "watch for ooze and heat soak")

    lines += ["", "=" * 78,
              "Run again without --analyze to merge.", "=" * 78]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='Merge Grasshopper geometry with a sliced reference file. The '
                    'reference may be a BambuStudio .gcode.3mf or a plain '
                    'Marlin-flavour .gcode (Cura / PrusaSlicer / Orca). Per-step '
                    'FLOW + SPEED; globals from the ;GH_CONFIG header.')
    # Optional so --list-printers works on its own; checked by hand below.
    parser.add_argument('project_dir', nargs='?',
                        help='Project directory with input files')
    parser.add_argument('--config', default='gcode_merge_config.yaml',
                        help='Optional YAML fallback if the custom file has no ;GH_CONFIG header')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing files')

    tweaks = parser.add_argument_group(
        'print tweaks',
        'Scale the globals resolved from ;GH_CONFIG / config.yaml. These are the '
        "CLI half of the browser build's Tweak Settings card and go through the "
        'same GCodeMerger.apply_tweaks(), so identical flags give identical output. '
        'Omit them all and the merge runs exactly as it always has.')
    tweaks.add_argument('--speed-multiplier', type=float, default=1.0, metavar='X',
                        help='Scale the base print speed, e.g. 0.8 for 80%% '
                             '(default: 1.0, no change)')
    tweaks.add_argument('--flow-multiplier', type=float, default=1.0, metavar='X',
                        help='Scale the global flow multiplier, e.g. 1.05 '
                             '(default: 1.0, no change)')
    tweaks.add_argument('--bed-leveling', dest='bed_leveling', action='store_true',
                        default=None,
                        help="Keep the reference's bed-levelling commands "
                             '(overrides ;GH_CONFIG / config.yaml)')
    tweaks.add_argument('--no-bed-leveling', dest='bed_leveling', action='store_false',
                        help='Strip the bed-levelling commands from the start block')
    tweaks.add_argument('--analyze', action='store_true',
                        help='Print the pre-flight analysis and exit without merging '
                             "(the CLI form of the page's Analyze step)")

    machine = parser.add_argument_group(
        'machine overrides',
        'Needed when the reference declares no machine data (Cura writes none) '
        'and the printer is not in the registry. These always win over both the '
        'file and the registry.')
    machine.add_argument('--printer', help='Registry slug to force, e.g. elegoo_n4_max')
    machine.add_argument('--bed', metavar='XxY',
                         help='Bed size in mm, e.g. 420x420')
    machine.add_argument('--height', type=float, metavar='MM',
                         help='Usable Z height in mm, e.g. 480')
    machine.add_argument('--firmware', help='marlin | klipper | prusa_buddy | rrf | bambu')
    machine.add_argument('--pause-gcode', metavar='CMD',
                         help="Confirmation-pause command, e.g. 'M0', 'PAUSE', 'M601'")
    machine.add_argument('--e-per-mm', type=float, metavar='VAL',
                         help='Force the extrusion rate instead of deriving it '
                              'from the reference')
    machine.add_argument('--list-printers', action='store_true',
                         help='Print the printer registry and exit')

    args = parser.parse_args()

    if args.list_printers:
        if printer_profiles is None:
            print("printer_profiles.py not found next to gcode_merger.py", file=sys.stderr)
            sys.exit(1)
        reg = printer_profiles.as_dict()
        for p in reg["printers"]:
            print(f"{p['slug']:<28} {p['label']:<34} "
                  f"{p['bed_x']:.0f}x{p['bed_y']:.0f}x{p['height']:.0f} mm  {p['firmware']}")
        print(f"\n{len(reg['printers'])} printers. "
              f"Firmware families: {', '.join(f['key'] for f in reg['firmwares'])}")
        sys.exit(0)

    if not args.project_dir:
        parser.error("project_dir is required (omit it only with --list-printers)")

    # Config file is optional; pass whichever path exists (project first, then cwd).
    config_path = Path(args.project_dir) / args.config
    if not config_path.exists():
        config_path = Path(args.config)

    overrides = {}
    if args.bed:
        try:
            bx, by = args.bed.lower().split("x", 1)
            overrides["bed_x"], overrides["bed_y"] = float(bx), float(by)
        except ValueError:
            print(f"ERROR: --bed expects WIDTHxDEPTH in mm, e.g. 420x420 "
                  f"(got '{args.bed}')", file=sys.stderr)
            sys.exit(1)
    if args.height:
        overrides["printable_height"] = args.height
    if args.firmware:
        overrides["firmware"] = args.firmware
    if args.pause_gcode:
        overrides["pause_gcode"] = args.pause_gcode
    if args.e_per_mm:
        overrides["e_per_mm"] = args.e_per_mm
    if args.printer:
        if printer_profiles is None or args.printer not in printer_profiles.PRINTER_PROFILES:
            print(f"ERROR: unknown printer '{args.printer}'. "
                  f"Run --list-printers to see the registry.", file=sys.stderr)
            sys.exit(1)
        prof = printer_profiles.PRINTER_PROFILES[args.printer]
        overrides.setdefault("bed_x", float(prof["bed"][0]))
        overrides.setdefault("bed_y", float(prof["bed"][1]))
        overrides.setdefault("printable_height", float(prof["height"]))
        overrides.setdefault("firmware", prof["firmware"])

    for name, value in (('--speed-multiplier', args.speed_multiplier),
                        ('--flow-multiplier', args.flow_multiplier)):
        if value <= 0:
            print(f"ERROR: {name} must be greater than 0 (got {value})", file=sys.stderr)
            sys.exit(1)

    try:
        merger = GCodeMerger(args.project_dir, config_path, args.dry_run,
                             config=overrides or None)
        # Identity by default, so a bare `gcode_merger.py <dir>` is untouched.
        merger.apply_tweaks(speed_multiplier=args.speed_multiplier,
                            flow_multiplier=args.flow_multiplier,
                            bed_leveling=args.bed_leveling)
        if args.analyze:
            print(merger.analysis_text())
            sys.exit(0)
        merger.run()
    except PrintVolumeError as e:
        print("\n" + "=" * 80, file=sys.stderr)
        print("FATAL: BUILD VOLUME CHECK FAILED", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        print(str(e), file=sys.stderr)
        sys.exit(2)
    except ReferenceFormatError as e:
        print("\n" + "=" * 80, file=sys.stderr)
        print("FATAL: REFERENCE FILE NOT USABLE", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        print(str(e), file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"ERROR: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
