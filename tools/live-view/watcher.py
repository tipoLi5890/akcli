"""Watch a .kicad_sch; on change: export SVG + metrics, append a step to state.json.

Usage: python3 watcher.py <schematic.kicad_sch>
- state.json / step-N.svg land next to this script (served by http.server).
- An optional note for the NEXT step can be written to note.txt (consumed once).
- Per step, records KiCad ERC error/warning counts and akcli part/net counts.
- AUTO_REVERT=1 additionally asks the open KiCad Schematic Editor to File>Revert
  via AppleScript (needs macOS Accessibility permission; failure is non-fatal).
"""
import json, os, pathlib, shutil, subprocess, sys, tempfile, time
from datetime import datetime

HERE = pathlib.Path(__file__).parent
TARGET = pathlib.Path(sys.argv[1]).resolve()


def _find_kicad_cli() -> str:
    env = os.environ.get("KICAD_CLI")
    if env:
        return env
    found = shutil.which("kicad-cli")
    if found:
        return found
    mac = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    return mac if os.path.exists(mac) else "kicad-cli"


def _find_akcli() -> str:
    env = os.environ.get("AKCLI")
    if env:
        return env
    repo = HERE.parent.parent / "bin" / "akcli"   # tools/live-view/ -> repo root
    if repo.exists():
        return str(repo)
    return shutil.which("akcli") or "akcli"


KICAD_CLI = _find_kicad_cli()
AKCLI = _find_akcli()
AUTO_REVERT = os.environ.get("AUTO_REVERT") == "1"
STATE = HERE / "state.json"


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except ValueError:
            pass
    return {"version": 0, "file": TARGET.name, "steps": []}


def export_svg(dest: pathlib.Path) -> bool:
    # --no-background-color matters: the theme background is a page-sized rect,
    # which would defeat the dashboard's getBBox() content crop.
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(
            [KICAD_CLI, "sch", "export", "svg", "--exclude-drawing-sheet",
             "--no-background-color", "-o", td, str(TARGET)],
            capture_output=True, timeout=60,
        )
        svgs = sorted(pathlib.Path(td).glob("*.svg"))
        if r.returncode != 0 or not svgs:
            return False
        shutil.copy(svgs[0], dest)
        return True


def wait_stable(path: pathlib.Path, quiet=1.2, max_wait=8.0) -> float:
    """Wait until ``path``'s mtime stops changing (collapses seed+apply bursts)."""
    deadline = time.time() + max_wait
    m = path.stat().st_mtime
    while time.time() < deadline:
        time.sleep(quiet)
        m2 = path.stat().st_mtime
        if m2 == m:
            return m
        m = m2
    return m


def erc_counts():
    """(errors, warnings) from KiCad's JSON ERC, or (None, None) on failure."""
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "erc.json"
        try:
            subprocess.run(
                [KICAD_CLI, "sch", "erc", "--format", "json", "--output",
                 str(out), str(TARGET)],
                capture_output=True, timeout=90,
            )
            rep = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            return None, None
    err = warn = 0
    for sheet in rep.get("sheets", []):
        for v in sheet.get("violations", []):
            sev = v.get("severity", "")
            if sev == "error":
                err += 1
            elif sev == "warning":
                warn += 1
    return err, warn


def akcli_counts():
    """(components, nets) via akcli read --json, or (None, None)."""
    try:
        r = subprocess.run([AKCLI, "read", str(TARGET), "--json"],
                           capture_output=True, timeout=30)
        d = json.loads(r.stdout.decode("utf-8"))
        return len(d.get("components", [])), len(d.get("nets", []))
    except Exception:
        return None, None


def take_note() -> str:
    note = HERE / "note.txt"
    if note.exists():
        text = note.read_text(encoding="utf-8").strip()
        note.unlink()
        return text
    return ""


def revert_kicad() -> None:
    """Best-effort File>Revert in the open KiCad Schematic Editor (en/zh menus)."""
    script = '''
    tell application "Schematic Editor" to activate
    delay 0.3
    tell application "System Events"
      tell process "eeschema"
        repeat with pair in {{"File", "Revert"}, {"檔案", "還原"}, {"檔案", "回復"}}
          try
            click menu item (item 2 of pair) of menu (item 1 of pair) of menu bar item (item 1 of pair) of menu bar 1
            delay 0.4
            try
              click button 1 of window 1
            end try
            return
          end try
        end repeat
      end tell
    end tell
    '''
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass  # advisory only


def main():
    state = load_state()
    last = TARGET.stat().st_mtime if TARGET.exists() else 0
    pending = TARGET.exists()  # emit an initial baseline step immediately
    print(f"watching {TARGET} -> {HERE}", flush=True)
    while True:
        if TARGET.exists():
            m = TARGET.stat().st_mtime
            if m != last or pending:
                m = wait_stable(TARGET)  # collapse seed+apply write bursts into one step
                n = len(state["steps"]) + 1
                svg = f"step-{n}.svg"
                if export_svg(HERE / svg):
                    err, warn = erc_counts()
                    parts, nets = akcli_counts()
                    # Hold the pending note for a step that actually has content:
                    # an intermediate empty write must not consume it.
                    note = take_note() if parts else ""
                    state["steps"].append({
                        "n": n, "svg": svg,
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "note": note or ("baseline" if pending else ""),
                        "erc_err": err, "erc_warn": warn,
                        "parts": parts, "nets": nets,
                    })
                    state["version"] += 1
                    tmp = STATE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(state, ensure_ascii=False))
                    tmp.replace(STATE)
                    print(f"step {n} @ {state['steps'][-1]['time']} "
                          f"erc={err}E/{warn}W parts={parts} nets={nets}", flush=True)
                    if AUTO_REVERT and not pending:
                        revert_kicad()
                last, pending = m, False
        time.sleep(1.0)


if __name__ == "__main__":
    main()
