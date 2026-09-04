# CustomGcode

Design a toolpath in Grasshopper, then print it on a normal FDM printer.

Slicers will not do this: they take a solid and decide the path themselves. These
Grasshopper definitions let you *author* the path — non-planar, spiralised, wireframe,
from a point cloud — and the merger then wraps it in a real sliced file so the machine
runs your geometry with its own startup, extrusion calibration and teardown.

```
Grasshopper definition  ──►  geometry .gcode  ──┐
                                                ├──►  merger  ──►  printable file
reference file sliced for your printer  ────────┘
```

Two halves, and you need both:

| | |
|---|---|
| **`Grasshopper/`** | Builds the toolpath and exports a geometry-only `.gcode`. |
| **The merger** | Wraps that geometry in a file your printer will actually accept. |

The merger comes in two forms, and they are **one implementation**:

| | |
|---|---|
| `gcode_merger_web.html` | Double-click it. Runs in the browser, nothing to install. |
| `gcode_merger.py` | `python gcode_merger.py <folder>`. Same merge, same output. |

The page is *generated* from the `.py` files — the Python that runs in your browser is
the same source, injected verbatim. Identical inputs and identical settings give
**byte-identical output** from either one. That is enforced, not hoped for; see
[Keeping the two identical](#keeping-the-two-identical).

Everything is client-side. No file is ever uploaded anywhere.

> **Print at your own risk.** These are authored toolpaths, not slicer output. The
> merger's build-volume check is fatal by design, but it cannot know whether your path
> is physically printable. Watch the first layer.

---

## Requirements

| For | You need |
|---|---|
| The Grasshopper definitions | Rhino 7 or later, with Grasshopper |
| `gcode_merger_web.html` | Any modern browser. Internet on first run only (see below). |
| `gcode_merger.py` | Python 3.8+ and nothing else |

---

## Quick start

1. Slice *anything* for your printer in your normal slicer and keep that file. This is
   the **reference** — it is where the merger learns your machine's startup, extrusion
   rate and teardown. A small cube is fine.
2. In Rhino, open `1_Print_Volume.gh` to see your build limits, then pick the generator
   that matches what you are printing — `2_1` for a vase-mode spiral, `2_3` for a
   wireframe, `2_4` for a point cloud.
3. Feed the resulting polyline into `4_GCode_Generator.gh` and export the geometry
   `.gcode`.
4. Open `gcode_merger_web.html`, drop the reference in the first area and your geometry
   in the second, press **Analyze**, then **Process**.
5. Print the file it gives you.

---

## The Grasshopper pipeline

Every route through the pipeline ends the same way: a **polyline**, handed to
`4_GCode_Generator`, which writes the geometry `.gcode`. What differs is how you get
that polyline.

```
1_Print_Volume            visual check — does it fit on the bed?
        │
        ├── 2_1_Spiralize_Geometry ──────────┬── (3_1_PathManipulator) ──┐
        │                                    │                          │
        ├── 2_2_Spiralize_for_Wireframe ─► 2_3_Wireframe_Printing ───────┤
        │                                                               │
        └── 2_4_Pointcloud_Path_Generator ──────────────────────────────┤
                                                                        ▼
                                                          4_GCode_Generator
                                                                        │
                                                                   geometry .gcode
```

| Stage | Definition | Input | Produces |
|---|---|---|---|
| 1 | `1_Print_Volume.gh` | — | a visual build-limit check |
| 2 | `2_1_Spiralize_Geometry.gh` | a Brep | a continuous "vase-mode" spiral |
| 2 | `2_2_Spiralize_Geometry_forWireframe.gh` | a Brep | a simplified spiral, for `2_3` only |
| 2 | `2_3_Wireframe_Printing.gh` | output of `2_2` | a wireframe print path |
| 2 | `2_4_Pointcloud_Path_Generator.gh` | a list of points | a space frame through the cloud |
| 3 | `3_1_PathManipulator.gh` | any path | an attractor-deformed path (optional) |
| 4 | `4_GCode_Generator.gh` | a polyline | the geometry `.gcode` |

Stage 2 is a **choice** — those four are alternatives, not steps.

<details>
<summary><b>1 — Print volume</b> · what it does and does not enforce</summary>

**`1_Print_Volume.gh`** draws your printer's build limits so you can see whether the
geometry actually sits inside the printable area. It is a **visual reference frame only**
and has no effect on the generated gcode.

The real enforcement happens later: the merger's build-volume check is fatal, and refuses
to write a file for geometry that leaves the volume. This definition is how you catch that
in Rhino instead of at the end.

</details>

<details>
<summary><b>2_1 / 2_2 / 2_3 — spiral and wireframe</b> · why there are two spiralisers</summary>

**`2_1_Spiralize_Geometry`** turns a Brep into one continuous vase-mode path, with layer
height, subdivisions and similar under your control. This is the one to use for a normal
spiralised print — its output is the cleanest, with **no seams**.

**`2_2_Spiralize_Geometry_forWireframe`** is a simplified version of the same thing. Use
it *only* as the input to `2_3`. The trade-off is deliberate: `2_1` gives the cleaner,
seamless result but its output will not drive the wireframe script, so `2_2` exists to
produce something `2_3` can consume.

**`2_3_Wireframe_Printing`** takes that simplified spiral and builds a path with constant
Z-changes, printing a light **wireframe** of the Brep rather than solid walls. It exposes
pause values and the other inputs wireframe printing needs — those pauses become
`;GH PAUSE=` tags and let each strand set before the next move.

</details>

<details>
<summary><b>2_4 — point cloud path generator</b> · modes, overhangs, sag compensation, inputs</summary>

**`2_4_Pointcloud_Path_Generator`** works on the same principle as the wireframe script
but drives from a **point cloud** instead of a surface. It groups the points into Z bands
and joins each pair of consecutive bands with a repeating strut motif, so the result is a
self-supporting **space frame** whose nodes are the points you gave it.

Nothing about the cloud has to be declared. The per-axis pitch is measured from the data
(as a median, so a cloud that is irregular in plan does not throw it off), and the
neighbourhood radius follows from that.

#### The printing rhythm

Spatial printing lives or dies on the order of moves, and the whole motif is built around
one rule: **the strut that rises is vertical, the strut that descends leans.**

```
    apex over A  o                        o  apex over B
                 |\                      /|
                 | \    <- correct      / |   <- wrong
                 |  \                  /  |
                 A   B                A   B
```

Two reasons, both of which show up on the machine:

- a vertical **descent** ends on an apex held by nothing but the diagonal it just arrived
  on, and pushes that diagonal back down;
- after a vertical descent onto a node there is a strand standing directly over it, so the
  next move — in any direction — walks the hot end straight into it.

So the path is **post up → dwell → diagonal down**, repeated. It never descends
vertically, every anchor gets its post (including the last one in each band, which is a
corner of the cloud), and no member is ever printed twice — an existing member is
travelled over instead of retraced.

#### Modes

| `mode` | Use it for |
|---|---|
| **`TRUSS_OVERHANG`** | Any cloud that overhangs. Everything `TRUSS_OPT` does, plus corbels. |
| **`TRUSS_OPT`** | Clouds with no overhang. Identical output to `TRUSS_OVERHANG` there. |
| `TRUSS` | The original motif, kept unchanged for comparison. |

`ZIGZAG`, `SPIKE`, `SPIKE_RETRACE` and `LATTICE` are **disabled** — none of them held up
in a real print. The code is still in the component and the mode names are rejected with
a message saying so.

`TRUSS` is worth understanding as a contrast: it scores each apex by distance to both
lower points, and on a regular raster the point over A and the point over B cost exactly
the same. The tie breaks arbitrarily, so the up/down rhythm flips at random, and it stops
one anchor short of the end of every band — which is why corners came out with no vertical
under them and sagged.

#### Overhangs

A point with nothing plumb beneath it never has anything rise to it, so it used to get
picked up sideways by the horizontal pass, in open air and with no pause. `TRUSS_OVERHANG`
adds the **corbel**: a diagonal-up strut from a supported anchor below, with the dwell at
the top.

```
    O  <- unsupported: reached by a diagonal UP, then a dwell
   /:
  / :
 L..:  <- launch anchor, one band down
```

The corbel pass runs **before** the posts of its band. That order is forced, not
preferred: put the post up first and the corbel has to set off from an anchor with a
strand standing directly over it. The pass is itself a zigzag — up to an apex, diagonally
back down to the next anchor, up again — so it costs one reposition per band rather than
one per strut.

Only the **first ring** of an overhang can be corbelled. A point two or more cells past
the edge has nothing below it within reach of any member, so it is cantilevered
horizontally — but now from a properly bracketed root, and with a dwell on arrival. A
cloud that steps out one cell per band is corbelled throughout; a sudden shelf is not, and
`status` counts both.

Corbels extrude upward into free air, so they get their own `overhang_speed`,
`overhang_flow` and `overhang_dwell`.

#### Sag compensation

A free apex is pushed down as the nozzle leaves it, which is what weakens its bond to
whatever is built on it next. `post_lift` and `overhang_lift` pay for that in advance: the
apex is **deposited** that many mm high and settles onto the nominal point.

Two values rather than one, because the two apexes fail by different mechanics — a post
apex sits on an axially loaded column and barely moves, while a corbel apex is a
cantilever tip loaded in bending. A model also runs roughly four posts per corbel, so one
shared number would spread a corbel-sized correction across four times as many points that
never needed it.

`overhang_lift` is applied **per ring** — how many cells of open air separate a point from
the nearest supported one. Each ring hangs off an already-drooping one, so the droop
compounds outward:

```
ring:      0        1        2        3
        support | corbel | chord  | chord      <- how it is reached
        +0.00   | +0.40  | +0.80  | +1.20      <- at overhang_lift = 0.4
```

The compensation works **only because it is asymmetric**. Each point appears in the path
several times — once as the top of an upward strut, later as the anchor the next strut
rises from. The lift applies to the first of those only. Apply it everywhere instead and
you raise every band by the same amount, which is a global Z offset that does nothing;
worse, starting the next strut from the lifted height after the node has settled leaves
the nozzle extruding into a gap.

Setting a lift far too high is not silent: raising it steepens every descent off that
apex, and any member dropped because its descent would have been plumb is counted in
`status`.

#### Inputs

The first eleven are the working set; the `overhang_*` and `post_lift` five are optional
and can be left unwired.

| Input | Access | Default | |
|---|---|---|---|
| `points` | List | — | the cloud |
| `mode` | Item | `TRUSS_OPT` | `TRUSS_OVERHANG` / `TRUSS_OPT` / `TRUSS` |
| `neighbors` | Item | 6 | how many of the 26 neighbours a point may connect to, nearest first |
| `triangulate` | Item | off | add the in-band horizontal members that close each triangle |
| `nozzle_profile` | List | a guess | flat `height, radius` pairs describing the hot end — **measure your own** |
| `tol` | Item | 0.5 | Z-banding tolerance, mm |
| `dwell_ms` | Item | 800 | pause at a post apex |
| `clearance` | Item | 5 | travel height above the print |
| `flow` / `speed` | Item | 1.0 | base per-point multipliers |
| `check` | Item | on | run the collision check |
| `overhang_speed` | Item | 0.5 | speed multiplier on corbels |
| `overhang_flow` | Item | 1.0 | flow multiplier on corbels |
| `overhang_dwell` | Item | 2 × `dwell_ms` | pause at a corbel apex |
| `overhang_lift` | Item | 0.0 | sag allowance at an overhang tip, **per ring**, mm |
| `post_lift` | Item | 0.0 | sag allowance at a post apex, mm |

Both ends of a corbel carry `overhang_speed` and `overhang_flow`, because the merger
averages flow and speed across a segment's two endpoints — tagging only the apex would
deliver half the reduction asked for.

#### Outputs

`polyline` and the four channel lists (`flow_values`, `speed_values`, `pause_values`,
`travel_values`) go straight to `4_GCode_Generator`; the channels are index-aligned with
the polyline's points, which is the contract it relies on. `preview_curves` and
`preview_colors` drive a Custom Preview — blue where it extrudes, grey where it travels.
`lattice_edges` shows the connectivity graph before routing, and `collisions` marks points
where the hot end would strike printed material.

#### The collision check and `status`

`nozzle_profile` describes the envelope of everything that can hit the print, as
`height above the tip, radius` pairs — the tip, the nozzle cone, the heater block, the fan
shroud. The check walks the finished path against that cone and reports where it would
strike. A cone is narrower than a cylinder low down but **wider up high**, so a shroud
that hangs out at 18 mm reaches further than a flat 8 mm ever did; the widest *low* part
is what decides things. The built-in profile is a plausible guess, not your printer.

`status` is worth wiring to a panel. Besides the measured pitch, the graph size and the
collision verdict, it prints a histogram of every printed move — so the "never descend
vertically" rule is something you can confirm on the real path rather than take on trust —
along with the overhang and corbel counts and the sag allowance in force.

</details>

<details>
<summary><b>3 — path manipulation (optional)</b> · attractor deformation</summary>

**`3_1_PathManipulator.gh`** deforms a path produced by the spiralize script, driven by
**attractor points**. Skip it if you don't need it.

It is deliberately an **open script**: it is meant to be opened up, modified and rebuilt
around whatever logic you actually want. Treat the attractor setup as a worked starting
point, not a finished tool.

</details>

<details>
<summary><b>4 — writing the gcode</b> · the tags the merger reads</summary>

**`4_GCode_Generator.gh`** takes a previously generated polyline plus your parameters and
writes a simple `.gcode`. Its output goes **straight into the merger**, together with a
reference file from a conventional slicer.

The export is deliberately geometry-only — no temperatures, no homing, no calibration —
because all of that comes from the reference instead. Alongside the coordinates it writes
the tags the merger reads:

- a **`;GH_CONFIG`** header line carrying the globals (base speed, flow, bed levelling);
- per-point **`;GH SPEED=`**, **`;GH FLOW=`**, **`;GH PAUSE=`** and **`;GH TRAVEL`** tags
  that modulate individual moves.

See [Settings, and where they come from](#settings-and-where-they-come-from) for how those
are resolved and how a tweak overrides them.

</details>

**Example.** `Grasshopper/Examples/E1_Pointcloud_Printing_Example.gh` — a worked
point-cloud print, showing `2_4` wired up end to end. The quickest way to see what the
parameters do.

---

## What the merger does

You have geometry from Grasshopper — a `.gcode` of nothing but moves, with no
temperatures, no homing, no extrusion calibration and no idea what machine it is for. You
also have a **reference**: a real file sliced for your actual printer, which has all of
that and is known to work. The merger keeps the reference's startup and teardown
*verbatim*, throws away its geometry, and substitutes yours — recalculating every
extrusion value against the reference's own measured E-per-mm rather than guessing.

<details>
<summary><b>Reference formats, and the end-of-print Z safety</b></summary>

Two reference formats take the same path through the merge:

- **BambuStudio `.gcode.3mf`** — a zip whose `Metadata/plate_1.gcode` carries block
  markers and a full `; key = value` config. Output is a `.gcode.3mf`.
- **Plain Marlin-flavour `.gcode`** (Cura, PrusaSlicer, OrcaSlicer) — one text file,
  no block markers, and from Cura no machine config at all. Output is
  `<name>_merged.gcode`.

Everything downstream of parsing is format-independent: geometry extraction, per-point
`FLOW` / `SPEED` / `PAUSE` / `TRAVEL`, retraction planning, the fatal build-volume
check, and the end-of-print Z safety all run identically.

### End-of-print Z safety

Your print is almost never the height the reference was. A slicer bakes its own object
height into the end gcode (`G1 Z{max_layer_z + 0.5} ; lower z a little`), which on a
taller custom print would drive the nozzle straight down into it. So the merger does two
things:

1. **Strips every Z move from the reference teardown**, leaving the rest verbatim. Removed
   lines stay visible as `; [Z-REMOVED]` comments.
2. **Lifts 20 mm clear of the finished print** first, capped at the printer's own Z limit
   and floored at 30 mm.

Because (1) removes the teardown's Z moves, the height reached in (2) is the height the
whole teardown runs at.

The lift needs the print's true height, so that height is measured **twice** — once by the
build-volume check before anything is written, once by the merge loop — and the two must
agree before the clearance lift is used. If they ever disagree the merger falls back to
parking at the printer's Z limit and says so in the report. The failure mode is a slow
teardown, never a nozzle dragged through the print.

`end_park_z` in a printer's config entry overrides the height outright.

</details>

---

## Browser use

Double-click `gcode_merger_web.html`, drop the reference then the Grasshopper `.gcode`,
press **Analyze**, then **Process**.

<details>
<summary><b>Step by step, and the one time it needs the internet</b></summary>

1. Drop the **reference** in the first area.
2. The **Printer** card appears, naming the machine it recognised and filling in the
   build volume, firmware and pause command. Correct anything that is wrong.
3. Drop the Grasshopper `.gcode` in the second area and press **Analyze**.
4. Read the analysis. Then either **Process Now**, or **Tweak Settings** first.

The merged file and the text report download from the page.

First load pulls the Pyodide runtime (~10 MB, CPython compiled to WebAssembly) from
jsDelivr; after that the browser caches it. That first load is the only time the page
needs the internet.

</details>

---

## Command-line use

Put the reference and the Grasshopper file in one folder and point at it:

```bash
python gcode_merger.py path/to/folder
```

No arguments beyond the folder means no tweaks — the plain merge, exactly as it has
always behaved.

<details>
<summary><b>Which file is which, where output lands, and every option</b></summary>

**Which file is which** is worked out for you. The custom file is identified by the
generator's own `;GH` markers, or failing that by the *absence* of a slicer preamble
(no temperatures, no homing, no slicer banner). A `.3mf` always wins as the reference,
and a previous run's `_merged.gcode` is never treated as an input.

Outputs land next to the inputs:

| Reference | Output | Report |
|---|---|---|
| `ref.gcode.3mf` | `<custom>.gcode.3mf` | `<custom>_merge_report.txt` |
| `ref.gcode` | `<custom>_merged.gcode` | `<custom>_merge_report.txt` |

### Options

`python gcode_merger.py --help` prints the full list. In brief:

```
--analyze                Print the pre-flight analysis and exit, reading only
--dry-run                Run the whole merge, print the report, write nothing
--config FILE            Optional YAML fallback if there is no ;GH_CONFIG header
```

`--analyze` inspects the two files. `--dry-run` goes further: the merge really runs, so
the build-volume check, the extrusion calibration and the estimates are all genuine — only
the final write is skipped.

**Print tweaks** — the CLI half of the page's *Tweak Settings* card. Omit them all and
nothing changes:

```
--speed-multiplier X     Scale the base print speed, e.g. 0.8
--flow-multiplier X      Scale the global flow multiplier, e.g. 1.05
--bed-leveling           Keep the reference's bed-levelling commands
--no-bed-leveling        Strip them from the start block
```

**Machine overrides** — needed when the reference declares no machine data (Cura writes
none) and the printer is not in the registry. These win over both the file and the
registry:

```
--printer SLUG           e.g. elegoo_n4_max
--bed XxY                e.g. 420x420
--height MM              e.g. 480
--firmware NAME          marlin | klipper | prusa_buddy | prusa_einsy | rrf | bambu
--pause-gcode CMD        e.g. 'M0', 'PAUSE', 'M601'
--e-per-mm VAL           Force the extrusion rate instead of deriving it
--list-printers          Print the printer registry and exit
```

Python 3.8+, standard library only. PyYAML is optional and used only for the legacy
`config.yaml` fallback.

</details>

---

## Settings, and where they come from

Resolution order is `;GH_CONFIG` → `config.yaml` (optional) → built-in defaults, and a
tweak or a CLI flag is applied on top of whatever that produced.

<details>
<summary><b>The <code>;GH_CONFIG</code> header, per-point tags, and bed levelling</b></summary>

Global settings are read from a `;GH_CONFIG` header line in the custom file, which the
Grasshopper definition writes:

```
;GH_CONFIG SPEED_MMS=30.0000 FLOW=1.5000 BED_LEVELING=0
```

Per-point `;GH SPEED=`, `;GH FLOW=`, `;GH PAUSE=` and `;GH TRAVEL` tags in the geometry
modulate individual moves.

`BED_LEVELING` defaults to **on**, meaning the reference's start block is copied
untouched. Turning it off strips the levelling commands for the detected firmware —
`G29` on Marlin and Prusa, `QUAD_GANTRY_LEVEL` / `Z_TILT_ADJUST` / `PROBE_CALIBRATE` on
Klipper — and leaves each stripped line in place as a `; [BED_LEVEL_DISABLED]` comment
so the output is still readable.

</details>

---

## The Printer card

The browser build inspects the reference the moment you drop it and shows you what it
found, before anything is merged — something the CLI cannot do.

<details>
<summary><b>Why it exists, the resolution order, and the unknown-printer case</b></summary>

**Why it exists.** A BambuStudio `.gcode.3mf` declares its own `printable_area`,
`printable_height` and `machine_pause_gcode`, so the merger has always known the machine.
A Cura `.gcode` declares *none* of that — it has an object bounding box and nothing about
the printer. Without that data the fatal build-volume check has nothing to check against,
and a confirmation pause has no command to emit.

**Resolution order**, mirrored exactly from `printer_profiles.py`:

| Priority | Source | Shown as |
|---|---|---|
| 1 | you typed it | "entered by you" |
| 2 | declared in the reference file | "declared by the reference file" |
| 3 | the printer registry, matched on the machine name the slicer wrote | "from the printer table" |
| 4 | nothing | fields blank; **Analyze is disabled** |

Priority 2 beating 3 is load-bearing. A P1S reference declares `printable_height = 250`
while the spec sheet says 256 — the file is right, because it describes the machine as
configured. The same protects a modified printer from being overruled by a spec number.

**Fields whose value came from the file are deliberately not sent back to Python.** The
merger reads those itself, and the report then records them as file-declared rather than
as an override. Table values and anything you typed *are* sent, because the merger has no
other way to know them.

**Unknown printer.** Not an error — the card turns amber, the volume fields are left empty
and **Analyze stays disabled until you fill them in**. That is intentional: the
build-volume check is fatal-by-design, and quietly merging with it switched off is the one
outcome worse than making you type three numbers. Picking the closest model from the
dropdown fills them for you.

</details>

---

## Analyze, then Tweak

**Analyze** is a pre-flight read of both files that writes nothing. **Tweak Settings**
scales the globals — speed, flow, bed levelling — with a live preview.

<details>
<summary><b>What Analyze reports, and why the two front ends cannot drift</b></summary>

**Analyze** reports move count and bounding box, the machine the reference describes, the
globals that *will* be used, the spread of per-step `;GH` tags, and the pauses. If a speed
multiplier would push moves outside the clamp range it says so before you commit.

**Tweak Settings** untouched is the identity: `1.0` / `1.0` and the bed-levelling value the
merge already resolved, so *Process Now* does exactly what a bare CLI run does.

Both are one implementation shared with the CLI: `GCodeMerger.analyze()` and
`GCodeMerger.apply_tweaks()`. The page draws the dict; `--analyze` prints it. The Tweak
card and `--speed-multiplier` call the same method. Neither front end can report or apply
something the other would not.

</details>

---

## Estimates (print time / material)

`report["estimates"]` is filled during the merge and shown in three places: the CLI
`[SUCCESS]` line, an **ESTIMATES** block in the text report, and the stat tiles on the
page's Result card.

<details>
<summary><b>How each number is derived, and what is deliberately not modelled</b></summary>

| Number | Derivation |
|---|---|
| Print time | `sum(3D move length / that move's feedrate)` over the merged gcode, plus `G4` dwells. Every merged move carries an explicit `F`, so no feedrate is guessed. Printing vs. travel is split by whether the move has an `E` word. |
| Material (g) | `E_total (mm) x filament cross-section (mm^2) x density (g/cm^3) / 1000`. Diameter and density come from the reference config, defaulting to 1.75 mm / 1.24 g/cm^3. |
| Priming line | Measured as the total positive E in the reference's start block, which is copied into the output verbatim. Measured from the gcode rather than from the calibration result, because on a Marlin reference the calibration samples the *object's* print moves and has nothing to do with priming. |

Deliberately **not** modelled, and stated as such in both the report and the page:
acceleration / junction deviation (so the time is an optimistic floor, worst on short
segments), and the heat-up, bed-levelling and teardown sequences. A real print takes
somewhat longer.

</details>

---

## The printer registry

`printer_profiles.py` holds 101 printers and 7 firmware families, plus the fuzzy matching
that turns whatever name a slicer wrote into a registry entry. **Adding a printer is a
one-line edit** and needs no change to the merge code.

<details>
<summary><b>Why the page cannot drift from the table, and the extras seam</b></summary>

It is deliberately data plus one matching function. The page does not carry its own copy
of the table — it calls `printer_profiles.as_dict()` through Pyodide. The machine the page
shows you and the machine the merge validates against are therefore the same object.

```bash
python gcode_merger.py --list-printers
```

The `<script type="text/x-printers">` block in the page is a seam for per-printer *extras*
that are not machine geometry (e.g. `end_park_z`). Entries there are keyed by registry
slug and merged into the config last.

</details>

---

## Files

<details>
<summary><b>Repository layout</b></summary>

```
.
├── Grasshopper/             the toolpath definitions
│   ├── 1_Print_Volume.gh                       visual build-limit check
│   ├── 2_1_Spiralize_Geometry.gh               vase-mode spiral, seamless
│   ├── 2_2_Spiralize_Geometry_forWireframe.gh  simplified spiral, feeds 2_3
│   ├── 2_3_Wireframe_Printing.gh               wireframe path from 2_2
│   ├── 2_4_Pointcloud_Path_Generator.gh        space frame from a point cloud
│   ├── 3_1_PathManipulator.gh                  optional attractor deformation
│   ├── 4_GCode_Generator.gh                    polyline -> geometry .gcode
│   └── Examples/
│       └── E1_Pointcloud_Printing_Example.gh   worked point-cloud print
│
├── gcode_merger_web.html    GENERATED standalone page — do not edit by hand
├── gcode_merger.py          the merger; also the CLI
├── printer_profiles.py      printer + firmware registry
│
├── README.md
├── CONTRIBUTING.md          build + test workflow, if you want to change the merger
│
├── tests/                   development only — users never need this
│   └── test_end_park_z.py   end-of-print park height, across the whole registry
└── web/                     development only — users never need this
    ├── template.html        UI + Pyodide glue — edit this
    ├── build_web.py         injects both .py files into the template
    ├── ui_test.js           headless jsdom test of the generated page
    └── equivalence_test.py  proves the page and the CLI emit the same bytes
```

`gcode_merger.py` imports `printer_profiles.py` directly, so those two must stay side by
side. `gcode_merger_web.html` needs neither — the Python is embedded in it.

The Python behind each Grasshopper component lives inside its `.gh` file, not as a loose
`.py` in the repo.

</details>

---

## Keeping the two identical

After **any** change to `gcode_merger.py`, `printer_profiles.py` or `web/template.html`:

```bash
python web/build_web.py
```

<details>
<summary><b>Why editing the page by hand is pointless</b></summary>

Editing `gcode_merger_web.html` directly is pointless — the next build overwrites it, and
`build_web.py` warns when the committed page is older than its sources. The Python is
*embedded* in the page rather than fetched, which is why the page works from a plain
`file://` double-click; both modules are written into the Pyodide filesystem under `/lib`
at boot and imported normally.

The page cannot drift from the CLI as long as it is only ever produced by that script.
Everything the page adds on top — the Printer card, Analyze, Tweak — is glue: the merge
logic, the analysis and the tweak arithmetic all live in `gcode_merger.py`.

</details>

---

## Testing

```bash
python tests/test_end_park_z.py   # end-of-print park height, every printer
python web/equivalence_test.py    # the two front ends emit the same bytes
npm install jsdom                 # once; node_modules/ is gitignored
node web/ui_test.js               # the page's own behaviour
```

<details>
<summary><b>What each suite covers, and the test data it needs</b></summary>

### `tests/test_end_park_z.py`

Pins down the end-of-print lift: the arithmetic, the clamp to the machine's Z limit, the
floor, the `end_park_z` override, and that a disagreement between the two height
measurements falls back to the safe behaviour. The last group runs the rule against
**every printer in the registry at seven print heights**, so a printer added later cannot
produce a park height below the print or above the machine. Needs no test data.

### `web/equivalence_test.py`

The test for the promise at the top of this file. It pulls the `py-driver` block out of
the generated page, runs the page's own `run_merge()` under CPython, runs the CLI over an
identical copy of the same folder, and compares the output byte for byte — plain and
tweaked, on both reference formats. A `.3mf` is compared by its inner
`Metadata/plate_1.gcode`, since zip archives embed timestamps and never match as files.

### `web/ui_test.js`

Loads the **generated page** into jsdom with the Pyodide CDN script swapped for a stub.
The stub's data is not invented — `ui_test.js` shells out to real CPython, runs the page's
own `py-driver` block against real reference files, and feeds the genuine JSON back in.
So the UI is tested against what Pyodide would actually return.

It covers the layer Python cannot: the info box, the file-vs-table provenance rule, the
"incomplete volume blocks Analyze" gate, and that the Tweak card's values reach Python
unchanged.

### Test data

Groups needing real sliced files skip cleanly when those files are absent, so a fresh
clone still runs the suite. To light them up, put your own files in `testdata/` (or point
`GCODE_TESTDATA` at a folder holding them):

| File | What it is |
|---|---|
| `EN4MAX_Cube.gcode` | a Cura/Marlin reference |
| `geometry.gcode` | a Grasshopper export to merge into it |
| `PETG.gcode.3mf` | a BambuStudio reference |
| `wire6.gcode` | a Grasshopper export to merge into that |

They are gitignored: they are megabytes of somebody's print job, and machine-specific.
`equivalence_test.py` skips loudly without them rather than passing on nothing.

</details>

---

## Known limitations

<details>
<summary><b>Five things this does not do</b></summary>

1. **Needs internet on first run** for the Pyodide CDN. Vendoring Pyodide into this folder
   would make it fully offline.
2. The Pyodide version is pinned in `web/build_web.py` (`PYODIDE_VERSION`, currently
   `314.0.3`). Its scheme tracks CPython: `314.x.y` == CPython 3.14.
3. **Reference inspection re-reads the file at merge time.** The file is written to the
   Pyodide FS twice (once into `/scratch` on drop, once into `/project` on Process). Fine
   at these sizes; a 100 MB reference would be held twice.
4. Pyodide runs **synchronously on the main thread**, so the page is frozen while a merge
   runs. The spinner and progress bar animate only `transform`, which Chrome runs on the
   compositor thread and keeps moving through main-thread blocking — animating `width` or
   `background` instead would visibly stall.
5. Print-time estimates ignore acceleration, so they are an optimistic floor.

</details>

---

## Changing the merger

`gcode_merger_web.html` is generated — never edit it by hand. Edit the sources and
rebuild. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the loop and the tests.
