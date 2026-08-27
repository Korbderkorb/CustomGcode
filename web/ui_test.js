/*
 * Headless smoke test for gcode_merger_web.html.
 *
 *     npm install jsdom
 *     node web/ui_test.js
 *
 * Loads the GENERATED page into jsdom with the Pyodide CDN <script> swapped for
 * a stub. The stub's data is not invented: it is captured by running the page's
 * own py-driver block under real CPython against the real reference files, so
 * what the UI is tested against is what Pyodide would actually return.
 *
 * What this covers is the layer that Python cannot: the printer info box, which
 * values are treated as file-declared vs. table-supplied, and the rule that an
 * incomplete build volume blocks Process. Getting that last one wrong would
 * ship files with the fatal volume check silently switched off.
 *
 * Note: top-level `const`/`let` in the page live in the global LEXICAL scope,
 * reachable through win.eval() but NOT as window properties. Hence ev().
 */
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { JSDOM } = require("jsdom");

const WEB_DIR = __dirname;
const PROJECT_DIR = path.dirname(WEB_DIR);
const PAGE = path.join(PROJECT_DIR, "gcode_merger_web.html");

// Real sliced files to run the detection against. They are not in the repo --
// they are megabytes of someone's print job, and the point of the test is that
// it runs against genuine slicer output rather than a hand-written fixture.
// Drop your own into testdata/ (or point GCODE_TESTDATA at a folder holding
// them) and the groups that need them light up; without them those groups skip
// and the rest of the suite still runs, which is what a fresh clone gets.
const TESTDATA = process.env.GCODE_TESTDATA || path.join(PROJECT_DIR, "testdata");
const REFS = {
  elegoo: path.join(TESTDATA, "EN4MAX_Cube.gcode"),
  bambu: path.join(TESTDATA, "PETG.gcode.3mf"),
};

function buildFixtures() {
  const py = `
import contextlib, io, re, json, sys
from pathlib import Path
page = Path(${JSON.stringify(PAGE)}).read_text(encoding="utf-8")
driver = re.search(r'<script type="text/x-python" id="py-driver">(.*?)</script>', page, re.S).group(1)
sys.path.insert(0, ${JSON.stringify(PROJECT_DIR)})
ns = {}

# The merger logs to stdout; in the browser Pyodide captures that. Here it would
# land in the middle of the JSON, so swallow it and print only the payload.
noise = io.StringIO()
with contextlib.redirect_stdout(noise):
    exec(compile(driver, "driver", "exec"), ns)
    out = {"registry": json.loads(ns["registry_json"]())}
    for key, p in ${JSON.stringify(REFS)}.items():
        if Path(p).exists():
            out[key] = json.loads(ns["inspect_reference"](p))

    # The Analyze card is driven by GCodeMerger.analyze(), which needs a project
    # holding BOTH files. Point the driver's PROJECT at testdata/ and run the
    # real thing, so the Tweak group is tested against genuine numbers too.
    testdata = Path(${JSON.stringify(TESTDATA)})
    if (testdata / "EN4MAX_Cube.gcode").exists() and (testdata / "geometry.gcode").exists():
        ns["PROJECT"] = testdata
        analysis = json.loads(ns["analyze_files"]())
        if analysis.get("ok"):
            out["analysis"] = analysis
print(json.dumps(out))
`;
  return JSON.parse(execFileSync("python", ["-c", py], { encoding: "utf8", maxBuffer: 1 << 24 }));
}

const fixtures = buildFixtures();
fixtures.unknown = {
  ok: true, format: "gcode", slug: null, label: null, vendor: null,
  declared_name: "Wombat Printworks 9000", registry: null, declared: {},
};

const STUB = `<script>
window.__fixtures = ${JSON.stringify(fixtures)};
window.__inspectTarget = "elegoo";
window.__lastConfig = null;
const __py = {
  registry_json: () => JSON.stringify(window.__fixtures.registry),
  inspect_reference: () => JSON.stringify(window.__fixtures[window.__inspectTarget]),
  reset_project: () => "/project",
  analyze_files: () => JSON.stringify(window.__fixtures.analysis),
  run_merge: (cfgJson) => {
    window.__lastConfig = JSON.parse(cfgJson);
    return JSON.stringify({ ok: true, output_path: "/project/out.gcode", output_name: "out.gcode",
      report_path: "/project/out_merge_report.txt",
      report_name: "out_merge_report.txt", warnings: [], moves: 10,
      estimates: { print_time_text: "19m 05s", total_grams: 5.82, filament_length_mm: 1921,
                   printing_time_s: 1108, travel_time_s: 37, filament_grams: 5.73 },
      settings: {} });
  },
};
window.loadPyodide = async () => ({
  setStdout() {}, setStderr() {},
  FS: { mkdirTree() {}, writeFile() {}, readFile: () => new Uint8Array([1,2,3]) },
  runPython() {},
  globals: { get: (k) => __py[k] },
});
window.__file = (name, size) => ({ name, size, arrayBuffer: async () => new ArrayBuffer(size || 1) });
window.__downloads = [];
window.URL.createObjectURL = () => "blob:stub";
window.URL.revokeObjectURL = () => {};
</script>`;

let html = fs.readFileSync(PAGE, "utf8");
const cdn = html.match(/<script src="https:\/\/cdn[^"]*"><\/script>/);
if (!cdn) { console.error("could not find the Pyodide CDN script tag in " + PAGE); process.exit(2); }
html = html.replace(cdn[0], STUB);

const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true });
const win = dom.window;
const ev = (code) => win.eval(code);

let failures = 0;
let skipped = 0;
function check(name, cond, extra) {
  if (cond) console.log("  PASS  " + name);
  else { failures++; console.log("  FAIL  " + name + (extra !== undefined ? "  -> " + extra : "")); }
}
const idle = () => new Promise((r) => setTimeout(r, 0));

// processWithSettings() kicks doProcess() off without awaiting it -- it is a
// click handler, not a pipeline -- so wait for the config to land rather than
// for the call to return.
async function settled(win, ms = 3000) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    // Wait for busy to clear too: run_merge lands the config, but doProcess
    // then holds the busy state for MIN_BUSY_MS, and a second click during
    // that window is silently dropped by the handler's own guard.
    if (win.eval("window.__lastConfig") !== null && win.eval("busy") === false) return true;
    await new Promise((r) => setTimeout(r, 10));
  }
  return false;
}

(async () => {
  await new Promise((r) => win.addEventListener("load", r));
  for (let i = 0; i < 80; i++) await idle();

  const $ = (id) => win.document.getElementById(id);
  const val = (id) => $(id).value;

  console.log("\n[1] registry loaded into the dropdowns");
  check("model dropdown populated", $("printer").options.length > 50, $("printer").options.length + " options");
  check("firmware dropdown populated",
        $("firmware").options.length === fixtures.registry.firmwares.length, $("firmware").options.length);
  check("vendors grouped", $("printer").querySelectorAll("optgroup").length > 5);
  check("printer card hidden before a reference is dropped", $("printercard").classList.contains("hidden"));
  check("status is Ready", $("status").textContent === "Ready.", $("status").textContent);

  if (fixtures.elegoo) {
    console.log("\n[2] Cura/Elegoo reference -> registry supplies the volume");
    ev('window.__inspectTarget = "elegoo"');
    await ev('inspectReference(window.__file("EN4MAX_Cube.gcode", 2400826))');
    await idle();
    check("card revealed", !$("printercard").classList.contains("hidden"));
    check("detected as known", $("detected").classList.contains("known"), $("detected").className);
    check("names the printer", /Neptune 4 Max/.test($("detected").textContent));
    check("bed X = 420", val("bedx") === "420", val("bedx"));
    check("bed Y = 420", val("bedy") === "420", val("bedy"));
    check("max Z = 480", val("bedz") === "480", val("bedz"));
    check("firmware = klipper", val("firmware") === "klipper", val("firmware"));
    check("pause command = PAUSE", val("pausecmd") === "PAUSE", val("pausecmd"));
    check("source note credits the table", /Bed X\/Y: from the printer table/.test($("srcnote").textContent),
          $("srcnote").textContent.slice(0, 70));

    console.log("\n[3] Analyze gating on an incomplete build volume");
    ev('picked.ref = window.__file("EN4MAX_Cube.gcode", 1)');
    ev("refreshProcessButton()");
    check("disabled with no custom file", $("analyze").disabled);
    ev('picked.custom = window.__file("wire.gcode", 100)');
    ev("refreshProcessButton()");
    check("enabled once both files + a volume are present", !$("analyze").disabled);
    $("bedy").value = "";
    $("bedy").dispatchEvent(new win.Event("input"));
    check("disabled again when a dimension is cleared", $("analyze").disabled);
    check("cleared field flagged amber", $("bedy").parentElement.classList.contains("needed"));
    $("bedy").value = "420";
    $("bedy").dispatchEvent(new win.Event("input"));
    check("re-enabled when refilled", !$("analyze").disabled);

    console.log("\n[4] config sent to Python (table values ARE sent as overrides)");
    await ev("process()");
    const cfg = ev("window.__lastConfig");
    check("reference_file named", cfg.reference_file === "EN4MAX_Cube.gcode", cfg.reference_file);
    check("custom_file named", cfg.custom_file === "wire.gcode", cfg.custom_file);
    check("bed_x sent", cfg.bed_x === 420, JSON.stringify(cfg));
    check("bed_y sent", cfg.bed_y === 420);
    check("printable_height sent", cfg.printable_height === 480);
    check("pause_gcode sent", cfg.pause_gcode === "PAUSE");
    check("firmware sent", cfg.firmware === "klipper");
  } else { skipped++; console.log("\n[2-4] SKIPPED (Elgoo reference not present)"); }

  if (fixtures.bambu) {
    console.log("\n[5] Bambu reference -> file-declared values are NOT re-sent as overrides");
    ev('window.__inspectTarget = "bambu"; window.__lastConfig = null');
    await ev('inspectReference(window.__file("PETG.gcode.3mf", 1))');
    await idle();
    check("bed from the file (256)", val("bedx") === "256", val("bedx"));
    check("Z from the file (250, not the 256 spec sheet)", val("bedz") === "250", val("bedz"));
    check("pause from the file", val("pausecmd") === "M400 U1", val("pausecmd"));
    check("source note credits the file", /Bed X\/Y: declared by the reference file/.test($("srcnote").textContent));
    ev('picked.ref = window.__file("PETG.gcode.3mf", 1); picked.custom = window.__file("wire6.gcode", 1)');
    await ev("process()");
    let cfg = ev("window.__lastConfig");
    check("bed_x NOT overridden", !("bed_x" in cfg), JSON.stringify(cfg));
    check("printable_height NOT overridden", !("printable_height" in cfg));
    check("pause_gcode NOT overridden", !("pause_gcode" in cfg));

    console.log("\n[6] a manual edit overrides everything");
    $("bedz").value = "199";
    $("bedz").dispatchEvent(new win.Event("input"));
    await ev("process()");
    cfg = ev("window.__lastConfig");
    check("edited Z is sent", cfg.printable_height === 199, JSON.stringify(cfg));
    check("source note says manual", /Max Z: entered by you/.test($("srcnote").textContent));
  } else { skipped++; console.log("\n[5-6] SKIPPED (Bambu reference not present)"); }

  console.log("\n[7] unknown printer -> manual entry required");
  ev('window.__inspectTarget = "unknown"');
  await ev('inspectReference(window.__file("weird.gcode", 1))');
  await idle();
  check("flagged unknown", $("detected").classList.contains("unknown"), $("detected").className);
  check("asks for the volume", /type your build volume/.test($("detected").textContent));
  check("fields left empty", val("bedx") === "" && val("bedz") === "", val("bedx") + "/" + val("bedz"));
  ev('picked.ref = window.__file("weird.gcode", 1)');
  ev('picked.custom = window.__file("wire.gcode", 100)');  // group 3 may have been skipped
  ev("refreshProcessButton()");
  check("Analyze blocked until filled", $("analyze").disabled);
  $("printer").value = "sovol_sv08";
  $("printer").dispatchEvent(new win.Event("change"));
  check("choosing a model fills the volume", val("bedx") === "350" && val("bedz") === "345",
        val("bedx") + "/" + val("bedz"));
  check("and its firmware", val("firmware") === "klipper", val("firmware"));
  check("Analyze unblocked", !$("analyze").disabled);

  if (fixtures.elegoo) {
    console.log("\n[8] dropping a reference BEFORE Pyodide is ready");
    ev("pyReady = false; pendingInspect = null");
    ev('window.__inspectTarget = "elegoo"');
    await ev('inspectReference(window.__file("EN4MAX_Cube.gcode", 1))');
    check("file is remembered, not dropped", ev("pendingInspect !== null"));
    ev("pyReady = true");
    await ev("inspectReference(pendingInspect)");
    await idle();
    check("inspection replays once ready", val("bedx") === "420", val("bedx"));
  }

  console.log("\n[9] the reference slot accepts both container types");
  check("accept attribute", $("input-ref").getAttribute("accept") === ".3mf,.gcode",
        $("input-ref").getAttribute("accept"));
  if (fixtures.analysis) {
    console.log("\n[10] Analyze -> Tweak -> the multipliers reach Python");
    ev('window.__inspectTarget = "elegoo"; window.__lastConfig = null');
    await ev('inspectReference(window.__file("EN4MAX_Cube.gcode", 1))');
    await idle();
    ev('picked.ref = window.__file("EN4MAX_Cube.gcode", 1)');
    ev('picked.custom = window.__file("geometry.gcode", 1)');
    ev("refreshProcessButton()");
    await ev("analyze()");
    await idle();

    const a = fixtures.analysis;
    check("analysis card shown", !$("analysis-card").classList.contains("hidden"));
    check("move count rendered", $("analysis-content").textContent
          .includes(a.geometry.move_count.toLocaleString()), a.geometry.move_count);
    check("base speed rendered", $("analysis-content").textContent
          .includes(String(a.current_settings.base_speed_mm_s)));

    // Untouched, the Tweak card must be the identity: 1.0 / 1.0 and the bed
    // levelling the merge already resolved. Anything else and "Process Now"
    // would quietly change the print relative to the CLI.
    check("speed multiplier defaults to 1.0", val("speed-mult") === "1.0", val("speed-mult"));
    check("flow multiplier defaults to 1.0", val("flow-mult") === "1.0", val("flow-mult"));
    check("bed levelling mirrors the resolved setting",
          $("bed-leveling-toggle").checked === a.current_settings.bed_leveling_enabled,
          $("bed-leveling-toggle").checked + " vs " + a.current_settings.bed_leveling_enabled);

    ev("window.__lastConfig = null");
    await ev("processWithSettings()");
    check("config reached Python", await settled(win));
    let cfg = ev("window.__lastConfig");
    check("identity tweaks still sent explicitly", cfg.speed_multiplier === 1
          && cfg.flow_multiplier === 1, JSON.stringify(cfg));
    check("bed_leveling sent as the resolved value",
          cfg.bed_leveling === a.current_settings.bed_leveling_enabled, JSON.stringify(cfg));

    console.log("\n[11] edited tweaks are the ones that travel");
    $("speed-mult").value = "0.8";
    $("speed-mult").dispatchEvent(new win.Event("input"));
    $("flow-mult").value = "1.1";
    $("flow-mult").dispatchEvent(new win.Event("input"));
    $("bed-leveling-toggle").checked = true;
    $("bed-leveling-toggle").dispatchEvent(new win.Event("change"));
    ev("window.__lastConfig = null");
    await ev("processWithSettings()");
    check("config reached Python", await settled(win));
    cfg = ev("window.__lastConfig");
    check("speed_multiplier sent", cfg.speed_multiplier === 0.8, JSON.stringify(cfg));
    check("flow_multiplier sent", cfg.flow_multiplier === 1.1, JSON.stringify(cfg));
    check("bed_leveling sent", cfg.bed_leveling === true, JSON.stringify(cfg));
    check("machine facts still ride along", cfg.reference_file === "EN4MAX_Cube.gcode"
          && cfg.bed_x === 420, JSON.stringify(cfg));
  } else { skipped++; console.log("\n[10-11] SKIPPED (testdata reference + geometry not present)"); }


  if (fixtures.analysis) {
    console.log("\n[12] one click = one file");
    // Record downloads instead of letting jsdom try to navigate to the blob.
    win.HTMLAnchorElement.prototype.click = function () {
      win.__downloads.push(this.getAttribute("download"));
    };

    // Land a fresh result via the direct Process path.
    ev('window.__inspectTarget = "elegoo"');
    await ev('inspectReference(window.__file("EN4MAX_Cube.gcode", 1))');
    await idle();
    ev('picked.ref = window.__file("EN4MAX_Cube.gcode", 1)');
    ev('picked.custom = window.__file("geometry.gcode", 1)');
    ev("window.__lastConfig = null");
    await ev("process()");
    check("a result is armed", await settled(win));

    check("both buttons enabled once a result exists",
          !$("dl-output").disabled && !$("dl-report").disabled);

    // The bug this guards: the buttons were wired twice -- an onclick AND an
    // addEventListener -- so one click fired two downloads. Chrome then asks to
    // allow "multiple files", and once denied it silently blocks every later
    // download, which reads as a dead button rather than a blocked one.
    ev("window.__downloads = []");
    $("dl-output").dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
    let dl = ev("window.__downloads");
    check("merged file: exactly one download", dl.length === 1, JSON.stringify(dl));
    check("merged file: correct name", dl[0] === "out.gcode", JSON.stringify(dl));

    ev("window.__downloads = []");
    $("dl-report").dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
    dl = ev("window.__downloads");
    check("report: exactly one download", dl.length === 1, JSON.stringify(dl));
    check("report: correct name", dl[0] === "out_merge_report.txt", JSON.stringify(dl));

    console.log("\n[13] the Tweak path arms the buttons the same way");
    ev("window.__lastConfig = null");
    await ev("analyze()");
    await idle();
    await ev("processWithSettings()");
    check("a result is armed", await settled(win));
    ev("window.__downloads = []");
    $("dl-output").dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
    dl = ev("window.__downloads");
    check("still exactly one download after a tweaked merge", dl.length === 1,
          JSON.stringify(dl));
    ev("window.__downloads = []");
    $("dl-report").dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
    dl = ev("window.__downloads");
    check("report still exactly one", dl.length === 1, JSON.stringify(dl));

    console.log("\n[14] reset disarms the buttons");
    $("reset").dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
    await idle();
    check("download buttons disabled again",
          $("dl-output").disabled && $("dl-report").disabled);
    ev("window.__downloads = []");
    $("dl-output").dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
    check("a disarmed button downloads nothing", ev("window.__downloads").length === 0);
  } else { skipped++; console.log("\n[12-14] SKIPPED (testdata not present)"); }

  const tail = skipped ? ` (${skipped} group(s) skipped)` : "";
  console.log(failures === 0 ? `\nALL UI CHECKS PASSED${tail}` : `\n${failures} UI CHECK(S) FAILED${tail}`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => { console.error("TEST HARNESS ERROR:", e); process.exit(2); });
