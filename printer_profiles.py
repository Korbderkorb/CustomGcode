#!/usr/bin/env python3
"""
Printer + firmware registry, shared by the CLI merger and the browser build.

WHY THIS EXISTS
---------------
A BambuStudio ``.gcode.3mf`` declares everything the merger needs in its CONFIG
block: ``; printable_area``, ``; printable_height``, ``; machine_pause_gcode``,
``; retraction_length`` and friends. A Cura/Marlin ``.gcode`` declares almost
none of that -- it has an object bounding box (``;MINX``/``;MAXX``) but nothing
about the *machine*. The fatal build-volume check and the confirmation-pause
command both need machine facts, so they have to come from somewhere else.

This module is that somewhere else. It is deliberately **data, not logic**:
adding a printer is a one-line edit and needs no change to the merge code.

RESOLUTION ORDER (enforced by the merger, not here)
---------------------------------------------------
    1. explicit override  (CLI flag, web dialog, ;GH_CONFIG, config dict)
    2. value declared IN the reference file
    3. this table, matched on the machine name the slicer wrote
    4. nothing -- the merger warns and skips the check it cannot make

Step 2 outranks step 3 on purpose: a modified machine (bigger bed, raised
gantry, custom firmware) declares the truth in its own profile, and a
spec-sheet number from this table must never silently overrule it.

ACCURACY
--------
Build volumes are manufacturer spec-sheet values and are advertised
inconsistently across the industry (usable vs. nominal, with/without the
clip/purge zones). They are a safety net, not gospel -- every one of them is
overridable, and the merger reports which source it used. If a number here is
wrong for your machine, fix the entry: it is one line.
"""

# --------------------------------------------------------------------------
# Firmware families
# --------------------------------------------------------------------------
# 'pause_gcode' is the command used for a *confirmation* pause -- PAUSE=-1 on a
# point, or CONFIRM=1 on the 0-Point. It must stop and wait for the operator,
# NOT resume on a timer. Timed pauses always use G4 and never touch this.
#
# 'parks_on_pause' says the firmware moves the toolhead away when it pauses.
# That matters a lot here: this merger's pauses exist to hold a nozzle at a
# wireframe apex while the strand freezes. When the firmware parks, the merger
# re-commands the point after the pause so the path resumes where it left off.
#
# Klipper deliberately gets 'PAUSE' rather than 'M0'. Klipper implements M0
# only if the vendor happens to define a [gcode_macro M0]; PAUSE is provided by
# [pause_resume], which every stock vendor Klipper build configures.

FIRMWARE_PROFILES = {
    "bambu": {
        "label": "Bambu Lab (Marlin-derived)",
        "pause_gcode": "M400 U1",
        "parks_on_pause": False,
        "note": "Read from the reference's '; machine_pause_gcode' when present.",
    },
    "marlin": {
        "label": "Marlin",
        "pause_gcode": "M0",
        "parks_on_pause": False,
        "note": "M0 = pause and wait for a click. Needs an LCD/host that can resume.",
    },
    "klipper": {
        "label": "Klipper",
        "pause_gcode": "PAUSE",
        "parks_on_pause": True,
        "note": "PAUSE comes from [pause_resume]; resume from the web UI. "
                "M0 is NOT standard Klipper.",
    },
    "prusa_buddy": {
        "label": "Prusa Buddy (MINI/MK4/XL/CORE One)",
        "pause_gcode": "M601",
        "parks_on_pause": True,
        "note": "M601 = print pause, resumed from the printer menu.",
    },
    "prusa_einsy": {
        "label": "Prusa Einsy (MK3 family, Prusa-Firmware)",
        "pause_gcode": "M601",
        "parks_on_pause": True,
        "note": "M601 = print pause, resumed from the printer menu.",
    },
    "rrf": {
        "label": "RepRapFirmware (Duet)",
        "pause_gcode": "M25",
        "parks_on_pause": True,
        "note": "M25 pauses; resume with M24.",
    },
    "unknown": {
        "label": "Unknown / other",
        "pause_gcode": None,
        "parks_on_pause": True,
        "note": "No confirmation-pause command known. Supply one explicitly to "
                "use PAUSE=-1 or a 0-Point confirmation.",
    },
}

DEFAULT_FIRMWARE = "marlin"


# --------------------------------------------------------------------------
# Printer registry
# --------------------------------------------------------------------------
# bed    -- (size_x, size_y) in mm, origin at the front-left corner (0,0)
# height -- usable Z in mm
# alias  -- strings to look for in whatever machine name the slicer wrote.
#           Matching is case/punctuation-insensitive and picks the LONGEST
#           matching alias, so "NEPTUNE 4 MAX" wins over "NEPTUNE 4".

PRINTER_PROFILES = {
    # ---------------- Bambu Lab (references normally declare their own) -----
    "bambu_a1_mini": {"label": "Bambu Lab A1 mini", "vendor": "Bambu Lab",
                      "bed": (180, 180), "height": 180, "firmware": "bambu",
                      "aliases": ["Bambu Lab A1 mini", "A1 mini", "A1M"]},
    "bambu_a1": {"label": "Bambu Lab A1", "vendor": "Bambu Lab",
                 "bed": (256, 256), "height": 256, "firmware": "bambu",
                 "aliases": ["Bambu Lab A1"]},
    "bambu_p1p": {"label": "Bambu Lab P1P", "vendor": "Bambu Lab",
                  "bed": (256, 256), "height": 256, "firmware": "bambu",
                  "aliases": ["Bambu Lab P1P", "P1P"]},
    "bambu_p1s": {"label": "Bambu Lab P1S", "vendor": "Bambu Lab",
                  "bed": (256, 256), "height": 256, "firmware": "bambu",
                  "aliases": ["Bambu Lab P1S", "P1S"]},
    "bambu_x1c": {"label": "Bambu Lab X1 Carbon", "vendor": "Bambu Lab",
                  "bed": (256, 256), "height": 256, "firmware": "bambu",
                  "aliases": ["Bambu Lab X1 Carbon", "X1 Carbon", "X1C"]},
    "bambu_x1e": {"label": "Bambu Lab X1E", "vendor": "Bambu Lab",
                  "bed": (256, 256), "height": 256, "firmware": "bambu",
                  "aliases": ["Bambu Lab X1E", "X1E"]},
    "bambu_x1": {"label": "Bambu Lab X1", "vendor": "Bambu Lab",
                 "bed": (256, 256), "height": 256, "firmware": "bambu",
                 "aliases": ["Bambu Lab X1"]},
    "bambu_h2d": {"label": "Bambu Lab H2D", "vendor": "Bambu Lab",
                  "bed": (325, 320), "height": 325, "firmware": "bambu",
                  "aliases": ["Bambu Lab H2D", "H2D"]},
    "bambu_h2s": {"label": "Bambu Lab H2S", "vendor": "Bambu Lab",
                  "bed": (325, 320), "height": 325, "firmware": "bambu",
                  "aliases": ["Bambu Lab H2S", "H2S"]},

    # ---------------- Elegoo ------------------------------------------------
    "elegoo_n4_max": {"label": "ELEGOO Neptune 4 Max", "vendor": "Elegoo",
                      "bed": (420, 420), "height": 480, "firmware": "klipper",
                      "aliases": ["ELEGOO Neptune 4 Max", "Neptune 4 Max", "N4MAX"]},
    "elegoo_n4_plus": {"label": "ELEGOO Neptune 4 Plus", "vendor": "Elegoo",
                       "bed": (320, 320), "height": 385, "firmware": "klipper",
                       "aliases": ["ELEGOO Neptune 4 Plus", "Neptune 4 Plus", "N4PLUS"]},
    "elegoo_n4_pro": {"label": "ELEGOO Neptune 4 Pro", "vendor": "Elegoo",
                      "bed": (225, 225), "height": 265, "firmware": "klipper",
                      "aliases": ["ELEGOO Neptune 4 Pro", "Neptune 4 Pro", "N4PRO"]},
    "elegoo_n4": {"label": "ELEGOO Neptune 4", "vendor": "Elegoo",
                  "bed": (225, 225), "height": 265, "firmware": "klipper",
                  "aliases": ["ELEGOO Neptune 4", "Neptune 4"]},
    "elegoo_n3_max": {"label": "ELEGOO Neptune 3 Max", "vendor": "Elegoo",
                      "bed": (420, 420), "height": 500, "firmware": "marlin",
                      "aliases": ["ELEGOO Neptune 3 Max", "Neptune 3 Max"]},
    "elegoo_n3_plus": {"label": "ELEGOO Neptune 3 Plus", "vendor": "Elegoo",
                       "bed": (320, 320), "height": 400, "firmware": "marlin",
                       "aliases": ["ELEGOO Neptune 3 Plus", "Neptune 3 Plus"]},
    "elegoo_n3_pro": {"label": "ELEGOO Neptune 3 Pro", "vendor": "Elegoo",
                      "bed": (225, 225), "height": 280, "firmware": "marlin",
                      "aliases": ["ELEGOO Neptune 3 Pro", "Neptune 3 Pro"]},
    "elegoo_n3": {"label": "ELEGOO Neptune 3", "vendor": "Elegoo",
                  "bed": (220, 220), "height": 280, "firmware": "marlin",
                  "aliases": ["ELEGOO Neptune 3", "Neptune 3"]},
    "elegoo_n2s": {"label": "ELEGOO Neptune 2S", "vendor": "Elegoo",
                   "bed": (220, 220), "height": 250, "firmware": "marlin",
                   "aliases": ["ELEGOO Neptune 2S", "Neptune 2S"]},
    "elegoo_n2": {"label": "ELEGOO Neptune 2", "vendor": "Elegoo",
                  "bed": (220, 220), "height": 250, "firmware": "marlin",
                  "aliases": ["ELEGOO Neptune 2", "Neptune 2"]},
    "elegoo_centauri_carbon": {"label": "ELEGOO Centauri Carbon", "vendor": "Elegoo",
                               "bed": (256, 256), "height": 256, "firmware": "klipper",
                               "aliases": ["ELEGOO Centauri Carbon", "Centauri Carbon"]},
    "elegoo_centauri": {"label": "ELEGOO Centauri", "vendor": "Elegoo",
                        "bed": (256, 256), "height": 256, "firmware": "klipper",
                        "aliases": ["ELEGOO Centauri"]},

    # ---------------- Creality ---------------------------------------------
    "creality_k2_plus": {"label": "Creality K2 Plus", "vendor": "Creality",
                         "bed": (350, 350), "height": 350, "firmware": "klipper",
                         "aliases": ["Creality K2 Plus", "K2 Plus"]},
    "creality_k1_max": {"label": "Creality K1 Max", "vendor": "Creality",
                        "bed": (300, 300), "height": 300, "firmware": "klipper",
                        "aliases": ["Creality K1 Max", "K1 Max"]},
    "creality_k1c": {"label": "Creality K1C", "vendor": "Creality",
                     "bed": (220, 220), "height": 250, "firmware": "klipper",
                     "aliases": ["Creality K1C", "K1C"]},
    "creality_k1_se": {"label": "Creality K1 SE", "vendor": "Creality",
                       "bed": (220, 220), "height": 250, "firmware": "klipper",
                       "aliases": ["Creality K1 SE", "K1 SE"]},
    "creality_k1": {"label": "Creality K1", "vendor": "Creality",
                    "bed": (220, 220), "height": 250, "firmware": "klipper",
                    "aliases": ["Creality K1"]},
    "creality_ender3_v3_ke": {"label": "Creality Ender-3 V3 KE", "vendor": "Creality",
                              "bed": (220, 220), "height": 240, "firmware": "klipper",
                              "aliases": ["Ender-3 V3 KE", "Ender 3 V3 KE"]},
    "creality_ender3_v3_plus": {"label": "Creality Ender-3 V3 Plus", "vendor": "Creality",
                                "bed": (300, 300), "height": 330, "firmware": "marlin",
                                "aliases": ["Ender-3 V3 Plus", "Ender 3 V3 Plus"]},
    "creality_ender3_v3_se": {"label": "Creality Ender-3 V3 SE", "vendor": "Creality",
                              "bed": (220, 220), "height": 250, "firmware": "marlin",
                              "aliases": ["Ender-3 V3 SE", "Ender 3 V3 SE"]},
    "creality_ender3_v3": {"label": "Creality Ender-3 V3", "vendor": "Creality",
                           "bed": (220, 220), "height": 250, "firmware": "marlin",
                           "aliases": ["Ender-3 V3", "Ender 3 V3"]},
    "creality_ender3_s1_pro": {"label": "Creality Ender-3 S1 Pro", "vendor": "Creality",
                               "bed": (220, 220), "height": 270, "firmware": "marlin",
                               "aliases": ["Ender-3 S1 Pro", "Ender 3 S1 Pro"]},
    "creality_ender3_s1": {"label": "Creality Ender-3 S1", "vendor": "Creality",
                           "bed": (220, 220), "height": 270, "firmware": "marlin",
                           "aliases": ["Ender-3 S1", "Ender 3 S1"]},
    "creality_ender3_v2": {"label": "Creality Ender-3 V2", "vendor": "Creality",
                           "bed": (220, 220), "height": 250, "firmware": "marlin",
                           "aliases": ["Ender-3 V2", "Ender 3 V2"]},
    "creality_ender3_pro": {"label": "Creality Ender-3 Pro", "vendor": "Creality",
                            "bed": (220, 220), "height": 250, "firmware": "marlin",
                            "aliases": ["Ender-3 Pro", "Ender 3 Pro"]},
    "creality_ender3": {"label": "Creality Ender-3", "vendor": "Creality",
                        "bed": (220, 220), "height": 250, "firmware": "marlin",
                        "aliases": ["Ender-3", "Ender 3"]},
    "creality_ender5_plus": {"label": "Creality Ender-5 Plus", "vendor": "Creality",
                             "bed": (350, 350), "height": 400, "firmware": "marlin",
                             "aliases": ["Ender-5 Plus", "Ender 5 Plus"]},
    "creality_ender5_s1": {"label": "Creality Ender-5 S1", "vendor": "Creality",
                           "bed": (220, 220), "height": 280, "firmware": "marlin",
                           "aliases": ["Ender-5 S1", "Ender 5 S1"]},
    "creality_ender5": {"label": "Creality Ender-5", "vendor": "Creality",
                        "bed": (220, 220), "height": 300, "firmware": "marlin",
                        "aliases": ["Ender-5", "Ender 5"]},
    "creality_ender6": {"label": "Creality Ender-6", "vendor": "Creality",
                        "bed": (250, 250), "height": 400, "firmware": "marlin",
                        "aliases": ["Ender-6", "Ender 6"]},
    "creality_cr10_smart_pro": {"label": "Creality CR-10 Smart Pro", "vendor": "Creality",
                                "bed": (300, 300), "height": 400, "firmware": "marlin",
                                "aliases": ["CR-10 Smart Pro"]},
    "creality_cr10_max": {"label": "Creality CR-10 Max", "vendor": "Creality",
                          "bed": (450, 450), "height": 470, "firmware": "marlin",
                          "aliases": ["CR-10 Max"]},
    "creality_cr10s_pro": {"label": "Creality CR-10S Pro", "vendor": "Creality",
                           "bed": (300, 300), "height": 400, "firmware": "marlin",
                           "aliases": ["CR-10S Pro"]},
    "creality_cr10_v3": {"label": "Creality CR-10 V3", "vendor": "Creality",
                         "bed": (300, 300), "height": 400, "firmware": "marlin",
                         "aliases": ["CR-10 V3"]},
    "creality_cr10": {"label": "Creality CR-10", "vendor": "Creality",
                      "bed": (300, 300), "height": 400, "firmware": "marlin",
                      "aliases": ["CR-10"]},
    "creality_cr6_se": {"label": "Creality CR-6 SE", "vendor": "Creality",
                        "bed": (235, 235), "height": 250, "firmware": "marlin",
                        "aliases": ["CR-6 SE"]},

    # ---------------- Prusa Research ---------------------------------------
    "prusa_core_one": {"label": "Original Prusa CORE One", "vendor": "Prusa",
                       "bed": (250, 220), "height": 270, "firmware": "prusa_buddy",
                       "aliases": ["Original Prusa CORE One", "CORE One", "COREONE"]},
    "prusa_xl": {"label": "Original Prusa XL", "vendor": "Prusa",
                 "bed": (360, 360), "height": 360, "firmware": "prusa_buddy",
                 "aliases": ["Original Prusa XL", "Prusa XL"]},
    "prusa_mk4s": {"label": "Original Prusa MK4S", "vendor": "Prusa",
                   "bed": (250, 210), "height": 220, "firmware": "prusa_buddy",
                   "aliases": ["Original Prusa MK4S", "MK4S", "MK4IS"]},
    "prusa_mk4": {"label": "Original Prusa MK4", "vendor": "Prusa",
                  "bed": (250, 210), "height": 220, "firmware": "prusa_buddy",
                  "aliases": ["Original Prusa MK4", "MK4"]},
    "prusa_mk39": {"label": "Original Prusa MK3.9", "vendor": "Prusa",
                   "bed": (250, 210), "height": 210, "firmware": "prusa_buddy",
                   "aliases": ["Original Prusa MK3.9", "MK3.9"]},
    "prusa_mk3s": {"label": "Original Prusa i3 MK3S+", "vendor": "Prusa",
                   "bed": (250, 210), "height": 210, "firmware": "prusa_einsy",
                   "aliases": ["Original Prusa i3 MK3S", "MK3S"]},
    "prusa_mk3": {"label": "Original Prusa i3 MK3", "vendor": "Prusa",
                  "bed": (250, 210), "height": 210, "firmware": "prusa_einsy",
                  "aliases": ["Original Prusa i3 MK3", "MK3"]},
    "prusa_mini": {"label": "Original Prusa MINI+", "vendor": "Prusa",
                   "bed": (180, 180), "height": 180, "firmware": "prusa_buddy",
                   "aliases": ["Original Prusa MINI", "Prusa MINI", "MINIIS"]},

    # ---------------- Anycubic ---------------------------------------------
    "anycubic_kobra3_max": {"label": "Anycubic Kobra 3 Max", "vendor": "Anycubic",
                            "bed": (420, 420), "height": 500, "firmware": "klipper",
                            "aliases": ["Anycubic Kobra 3 Max", "Kobra 3 Max"]},
    "anycubic_kobra3": {"label": "Anycubic Kobra 3", "vendor": "Anycubic",
                        "bed": (250, 250), "height": 260, "firmware": "klipper",
                        "aliases": ["Anycubic Kobra 3", "Kobra 3"]},
    "anycubic_kobra2_max": {"label": "Anycubic Kobra 2 Max", "vendor": "Anycubic",
                            "bed": (420, 420), "height": 500, "firmware": "marlin",
                            "aliases": ["Anycubic Kobra 2 Max", "Kobra 2 Max"]},
    "anycubic_kobra2_plus": {"label": "Anycubic Kobra 2 Plus", "vendor": "Anycubic",
                             "bed": (320, 320), "height": 400, "firmware": "marlin",
                             "aliases": ["Anycubic Kobra 2 Plus", "Kobra 2 Plus"]},
    "anycubic_kobra2_pro": {"label": "Anycubic Kobra 2 Pro", "vendor": "Anycubic",
                            "bed": (220, 220), "height": 250, "firmware": "marlin",
                            "aliases": ["Anycubic Kobra 2 Pro", "Kobra 2 Pro"]},
    "anycubic_kobra2_neo": {"label": "Anycubic Kobra 2 Neo", "vendor": "Anycubic",
                            "bed": (220, 220), "height": 250, "firmware": "marlin",
                            "aliases": ["Anycubic Kobra 2 Neo", "Kobra 2 Neo"]},
    "anycubic_kobra2": {"label": "Anycubic Kobra 2", "vendor": "Anycubic",
                        "bed": (220, 220), "height": 250, "firmware": "marlin",
                        "aliases": ["Anycubic Kobra 2", "Kobra 2"]},
    "anycubic_kobra_max": {"label": "Anycubic Kobra Max", "vendor": "Anycubic",
                           "bed": (400, 400), "height": 450, "firmware": "marlin",
                           "aliases": ["Anycubic Kobra Max", "Kobra Max"]},
    "anycubic_kobra_plus": {"label": "Anycubic Kobra Plus", "vendor": "Anycubic",
                            "bed": (300, 300), "height": 350, "firmware": "marlin",
                            "aliases": ["Anycubic Kobra Plus", "Kobra Plus"]},
    "anycubic_kobra": {"label": "Anycubic Kobra", "vendor": "Anycubic",
                       "bed": (220, 220), "height": 250, "firmware": "marlin",
                       "aliases": ["Anycubic Kobra", "Kobra"]},
    "anycubic_vyper": {"label": "Anycubic Vyper", "vendor": "Anycubic",
                       "bed": (245, 245), "height": 260, "firmware": "marlin",
                       "aliases": ["Anycubic Vyper", "Vyper"]},
    "anycubic_chiron": {"label": "Anycubic Chiron", "vendor": "Anycubic",
                        "bed": (400, 400), "height": 450, "firmware": "marlin",
                        "aliases": ["Anycubic Chiron", "Chiron"]},
    "anycubic_mega_s": {"label": "Anycubic i3 Mega S", "vendor": "Anycubic",
                        "bed": (210, 210), "height": 205, "firmware": "marlin",
                        "aliases": ["Anycubic i3 Mega S", "i3 Mega S"]},

    # ---------------- Sovol ------------------------------------------------
    "sovol_sv08_max": {"label": "Sovol SV08 Max", "vendor": "Sovol",
                       "bed": (500, 500), "height": 500, "firmware": "klipper",
                       "aliases": ["Sovol SV08 Max", "SV08 Max"]},
    "sovol_sv08": {"label": "Sovol SV08", "vendor": "Sovol",
                   "bed": (350, 350), "height": 345, "firmware": "klipper",
                   "aliases": ["Sovol SV08", "SV08"]},
    "sovol_sv07_plus": {"label": "Sovol SV07 Plus", "vendor": "Sovol",
                        "bed": (300, 300), "height": 340, "firmware": "klipper",
                        "aliases": ["Sovol SV07 Plus", "SV07 Plus"]},
    "sovol_sv07": {"label": "Sovol SV07", "vendor": "Sovol",
                   "bed": (220, 220), "height": 240, "firmware": "klipper",
                   "aliases": ["Sovol SV07", "SV07"]},
    "sovol_sv06_plus": {"label": "Sovol SV06 Plus", "vendor": "Sovol",
                        "bed": (300, 300), "height": 340, "firmware": "marlin",
                        "aliases": ["Sovol SV06 Plus", "SV06 Plus"]},
    "sovol_sv06": {"label": "Sovol SV06", "vendor": "Sovol",
                   "bed": (220, 220), "height": 250, "firmware": "marlin",
                   "aliases": ["Sovol SV06", "SV06"]},

    # ---------------- Qidi Tech --------------------------------------------
    "qidi_plus4": {"label": "QIDI Plus4", "vendor": "QIDI",
                   "bed": (305, 305), "height": 280, "firmware": "klipper",
                   "aliases": ["QIDI Plus4", "Plus4"]},
    "qidi_q1_pro": {"label": "QIDI Q1 Pro", "vendor": "QIDI",
                    "bed": (245, 245), "height": 240, "firmware": "klipper",
                    "aliases": ["QIDI Q1 Pro", "Q1 Pro"]},
    "qidi_x_max3": {"label": "QIDI X-Max 3", "vendor": "QIDI",
                    "bed": (325, 325), "height": 315, "firmware": "klipper",
                    "aliases": ["QIDI X-Max 3", "X-Max 3", "XMAX3"]},
    "qidi_x_plus3": {"label": "QIDI X-Plus 3", "vendor": "QIDI",
                     "bed": (280, 280), "height": 270, "firmware": "klipper",
                     "aliases": ["QIDI X-Plus 3", "X-Plus 3", "XPLUS3"]},
    "qidi_x_smart3": {"label": "QIDI X-Smart 3", "vendor": "QIDI",
                      "bed": (175, 180), "height": 170, "firmware": "klipper",
                      "aliases": ["QIDI X-Smart 3", "X-Smart 3", "XSMART3"]},

    # ---------------- Artillery --------------------------------------------
    "artillery_x4_plus": {"label": "Artillery Sidewinder X4 Plus", "vendor": "Artillery",
                          "bed": (300, 300), "height": 400, "firmware": "klipper",
                          "aliases": ["Sidewinder X4 Plus", "X4 Plus"]},
    "artillery_x4_pro": {"label": "Artillery Sidewinder X4 Pro", "vendor": "Artillery",
                         "bed": (220, 220), "height": 260, "firmware": "klipper",
                         "aliases": ["Sidewinder X4 Pro", "X4 Pro"]},
    "artillery_x3_plus": {"label": "Artillery Sidewinder X3 Plus", "vendor": "Artillery",
                          "bed": (300, 300), "height": 400, "firmware": "marlin",
                          "aliases": ["Sidewinder X3 Plus", "X3 Plus"]},
    "artillery_x2": {"label": "Artillery Sidewinder X2", "vendor": "Artillery",
                     "bed": (300, 300), "height": 400, "firmware": "marlin",
                     "aliases": ["Sidewinder X2"]},
    "artillery_x1": {"label": "Artillery Sidewinder X1", "vendor": "Artillery",
                     "bed": (300, 300), "height": 400, "firmware": "marlin",
                     "aliases": ["Sidewinder X1"]},
    "artillery_genius": {"label": "Artillery Genius", "vendor": "Artillery",
                         "bed": (220, 220), "height": 250, "firmware": "marlin",
                         "aliases": ["Artillery Genius", "Genius Pro"]},

    # ---------------- FlashForge -------------------------------------------
    "flashforge_ad5m_pro": {"label": "FlashForge Adventurer 5M Pro", "vendor": "FlashForge",
                            "bed": (220, 220), "height": 220, "firmware": "klipper",
                            "aliases": ["Adventurer 5M Pro", "AD5M Pro"]},
    "flashforge_ad5m": {"label": "FlashForge Adventurer 5M", "vendor": "FlashForge",
                        "bed": (220, 220), "height": 220, "firmware": "klipper",
                        "aliases": ["Adventurer 5M", "AD5M"]},
    "flashforge_ad4": {"label": "FlashForge Adventurer 4", "vendor": "FlashForge",
                       "bed": (220, 200), "height": 250, "firmware": "marlin",
                       "aliases": ["Adventurer 4"]},

    # ---------------- Voron / open hardware --------------------------------
    "voron_24_350": {"label": "Voron 2.4 (350)", "vendor": "Voron",
                     "bed": (350, 350), "height": 350, "firmware": "klipper",
                     "aliases": ["Voron 2.4 350", "Voron2.4 350"]},
    "voron_24_300": {"label": "Voron 2.4 (300)", "vendor": "Voron",
                     "bed": (300, 300), "height": 300, "firmware": "klipper",
                     "aliases": ["Voron 2.4 300", "Voron2.4 300"]},
    "voron_24_250": {"label": "Voron 2.4 (250)", "vendor": "Voron",
                     "bed": (250, 250), "height": 250, "firmware": "klipper",
                     "aliases": ["Voron 2.4", "Voron2.4"]},
    "voron_trident_300": {"label": "Voron Trident (300)", "vendor": "Voron",
                          "bed": (300, 300), "height": 250, "firmware": "klipper",
                          "aliases": ["Voron Trident 300"]},
    "voron_trident": {"label": "Voron Trident (250)", "vendor": "Voron",
                      "bed": (250, 250), "height": 250, "firmware": "klipper",
                      "aliases": ["Voron Trident"]},
    "voron_v0": {"label": "Voron V0.2", "vendor": "Voron",
                 "bed": (120, 120), "height": 120, "firmware": "klipper",
                 "aliases": ["Voron V0", "Voron 0.2", "Voron0"]},

    # ---------------- Others -----------------------------------------------
    "twotrees_sk1": {"label": "Two Trees SK1", "vendor": "Two Trees",
                     "bed": (256, 256), "height": 256, "firmware": "klipper",
                     "aliases": ["Two Trees SK1", "SK1"]},
    "snapmaker_artisan": {"label": "Snapmaker Artisan", "vendor": "Snapmaker",
                          "bed": (400, 400), "height": 400, "firmware": "marlin",
                          "aliases": ["Snapmaker Artisan", "Artisan"]},
    "snapmaker_j1": {"label": "Snapmaker J1", "vendor": "Snapmaker",
                     "bed": (324, 200), "height": 200, "firmware": "marlin",
                     "aliases": ["Snapmaker J1"]},
    "snapmaker_a350": {"label": "Snapmaker A350", "vendor": "Snapmaker",
                       "bed": (320, 350), "height": 330, "firmware": "marlin",
                       "aliases": ["Snapmaker A350"]},
    "ultimaker_s5": {"label": "Ultimaker S5", "vendor": "UltiMaker",
                     "bed": (330, 240), "height": 300, "firmware": "marlin",
                     "aliases": ["Ultimaker S5"]},
    "ultimaker_s3": {"label": "Ultimaker S3", "vendor": "UltiMaker",
                     "bed": (230, 190), "height": 200, "firmware": "marlin",
                     "aliases": ["Ultimaker S3"]},
    "ultimaker_2plus": {"label": "Ultimaker 2+", "vendor": "UltiMaker",
                        "bed": (223, 223), "height": 205, "firmware": "marlin",
                        "aliases": ["Ultimaker 2+", "Ultimaker 2"]},
    "kingroon_kp3s": {"label": "Kingroon KP3S", "vendor": "Kingroon",
                      "bed": (180, 180), "height": 180, "firmware": "marlin",
                      "aliases": ["Kingroon KP3S", "KP3S"]},
    "biqu_hurakan": {"label": "BIQU Hurakan", "vendor": "BIQU",
                     "bed": (235, 235), "height": 270, "firmware": "klipper",
                     "aliases": ["BIQU Hurakan", "Hurakan"]},
}


# --------------------------------------------------------------------------
# Matching helpers
# --------------------------------------------------------------------------

def _normalize(text):
    """Uppercase and collapse every non-alphanumeric run to a single space.

    'Ender-3 V3 SE', 'ENDER_3_V3_SE' and 'ender 3 v3 se' all normalize to the
    same string, so an alias written one way still matches a slicer that wrote
    it another way.
    """
    out = []
    prev_space = True
    for ch in str(text).upper():
        if ch.isalnum():
            out.append(ch)
            prev_space = False
        elif not prev_space:
            out.append(" ")
            prev_space = True
    return "".join(out).strip()


# Comment keys a slicer may use to name the machine. Order is preference order.
MACHINE_NAME_KEYS = (
    ";TARGET_MACHINE.NAME:",      # Cura
    "; printer_model =",          # BambuStudio / OrcaSlicer / PrusaSlicer
    "; printer_settings_id =",    # PrusaSlicer / OrcaSlicer profile name
    "; machine_name =",           # some Orca forks
    ";PRINTER_MODEL:",            # misc
)


def iter_machine_names(lines):
    """Yield every machine-name string a slicer declared, best key first."""
    seen = []
    for key in MACHINE_NAME_KEYS:
        low_key = key.lower()
        for line in lines:
            s = line.strip()
            if s.lower().startswith(low_key):
                val = s[len(key):].strip()
                if val and val not in seen:
                    seen.append(val)
                    yield val


def match_printer(name):
    """
    Match one machine-name string against the registry.

    Returns (slug, profile, matched_alias) or (None, None, None).

    The LONGEST matching alias wins, so 'ELEGOO NEPTUNE 4 MAX' resolves to the
    Max and not to the plain Neptune 4 whose alias is also a substring of it.
    """
    norm = _normalize(name)
    if not norm:
        return (None, None, None)
    best = (None, None, None)
    best_len = 0
    for slug, prof in PRINTER_PROFILES.items():
        for alias in prof["aliases"]:
            na = _normalize(alias)
            if na and na in norm and len(na) > best_len:
                best = (slug, prof, alias)
                best_len = len(na)
    return best


def detect_printer(lines):
    """
    Identify the printer from a reference gcode's comment lines.

    Strategy, in order:
      1. explicit machine-name keys (';TARGET_MACHINE.NAME:', '; printer_model =', ...)
      2. any comment in the first 60 lines -- Cura's machine start gcode often
         opens with a bare banner like ';ELEGOO NEPTUNE 4 MAX', which is the
         only name some profiles ever write.

    Returns a dict describing the match; 'slug' is None when nothing matched.
    """
    for name in iter_machine_names(lines):
        slug, prof, alias = match_printer(name)
        if slug:
            return {"slug": slug, "profile": prof, "declared_name": name,
                    "matched_alias": alias, "source": "machine name comment"}

    for line in lines[:60]:
        s = line.strip()
        if not s.startswith(";") or len(s) < 5:
            continue
        slug, prof, alias = match_printer(s[1:])
        if slug:
            return {"slug": slug, "profile": prof, "declared_name": s[1:].strip(),
                    "matched_alias": alias, "source": "header comment"}

    declared = next(iter_machine_names(lines), None)
    return {"slug": None, "profile": None, "declared_name": declared,
            "matched_alias": None, "source": None}


def firmware_profile(key):
    """Look up a firmware family, falling back to the 'unknown' entry."""
    return FIRMWARE_PROFILES.get(key or "", FIRMWARE_PROFILES["unknown"])


def as_dict():
    """
    The whole registry in a JSON-friendly shape.

    The browser build calls this through Pyodide instead of keeping its own
    copy of the table in JavaScript, so the dropdown, the detection info box
    and the merge itself can never disagree about a printer.
    """
    printers = []
    for slug, p in sorted(PRINTER_PROFILES.items(),
                          key=lambda kv: (kv[1]["vendor"], kv[1]["label"])):
        printers.append({
            "slug": slug,
            "label": p["label"],
            "vendor": p["vendor"],
            "bed_x": p["bed"][0],
            "bed_y": p["bed"][1],
            "height": p["height"],
            "firmware": p["firmware"],
        })
    firmwares = [{"key": k, "label": v["label"], "pause_gcode": v["pause_gcode"],
                  "parks_on_pause": v["parks_on_pause"], "note": v["note"]}
                 for k, v in FIRMWARE_PROFILES.items()]
    return {"printers": printers, "firmwares": firmwares}
