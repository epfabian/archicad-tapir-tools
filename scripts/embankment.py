"""Embankment hatch for Archicad (via the Tapir JSON API).

Workflow: select the top and bottom edge, run the script (Tapir palette),
confirm the spacing, click the top edge.
"""

import argparse
import json
import math
import os
import sys
import time
import traceback
import urllib.request
from pathlib import Path

PORT_RANGE = range(19723, 19745)


def _user_dir():
    """Per-user folder for settings and logs. Not next to the script: Tapir keeps
    scripts loaded from GitHub in a temp folder that is replaced on updates."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "archicad-tapir-tools"


SETTINGS_FILE = _user_dir() / "embankment.json"
LOG_FILE = _user_dir() / "embankment.log"
DEFAULT_SETTINGS = {"spacing": 0.5, "clean": True, "language": "auto"}
UI_TIMEOUT = 300.0      # seconds, in case the palette is closed via its X button
UI_POLL = 0.05
TAU = 2.0 * math.pi
EPS = 1e-9
CORNER_MIN_TURN = math.radians(5)   # smaller kinks don't count as corners
GERMAN_ARCHICAD = {"GER", "AUT", "CHE"}

# --------------------------------------------------------------------------- Texts

TEXTS = {
    "de": {
        "title": "Böschungsschraffur",
        "layer": "Böschungsschraffur",
        "spacing": "Abstand der Striche",
        "miter": "Ecken auf Gehrung",
        "hint": "Nach &bdquo;Weiter&ldquo; auf die <b>Oberkante</b> klicken.",
        "cancel": "Abbrechen",
        "next": "Weiter",
        "invalid": "Bitte eine Zahl größer als 0 eingeben.",
        # Tapir shows the prompt as single-byte text, so no umlauts here.
        "pick": "Boeschung: Bitte auf die OBERKANTE klicken (Esc = Abbrechen)",
        "select_two": "Bitte genau 2 Kanten markieren: die Oberkante und die Unterkante "
                      "der Böschung. Markiert sind gerade {n} Elemente.",
        "zero_length": "Eine der Polylinien hat keine Länge.",
        "no_ellipse": "Ellipsen werden noch nicht unterstützt.",
        "no_spline": "Splines werden noch nicht unterstützt. Bitte vorher in eine Polylinie umwandeln.",
        "bad_type": "Bitte nur Linien, Polylinien, Bögen oder Kreise als Kanten markieren.",
        "done": "{n} Striche als Gruppe auf Ebene '{layer}' angelegt (Abstand {spacing} m, Ecken {corners}).",
        "corners_miter": "auf Gehrung",
        "corners_fan": "als Fächer",
        "cancelled": "Abgebrochen.",
        "decimal": ",",
    },
    "en": {
        "title": "Embankment Hatch",
        "layer": "Embankment Hatch",
        "spacing": "Stroke spacing",
        "miter": "Mitered corners",
        "hint": "After &ldquo;Next&rdquo;, click the <b>top edge</b>.",
        "cancel": "Cancel",
        "next": "Next",
        "invalid": "Please enter a number greater than 0.",
        "pick": "Embankment: Click the TOP EDGE (Esc = cancel)",
        "select_two": "Please select exactly 2 edges: the top and the bottom edge "
                      "of the embankment. Currently selected: {n} elements.",
        "zero_length": "One of the polylines has zero length.",
        "no_ellipse": "Ellipses are not supported yet.",
        "no_spline": "Splines are not supported yet. Please convert them to polylines first.",
        "bad_type": "Please select only lines, polylines, arcs or circles as edges.",
        "done": "{n} strokes created as a group on layer '{layer}' (spacing {spacing} m, {corners} corners).",
        "corners_miter": "mitered",
        "corners_fan": "fanned",
        "cancelled": "Cancelled.",
        "decimal": ".",
    },
}
T = TEXTS["en"]     # set in main() once the language is known


class Cancelled(Exception):
    """The user cancelled."""


class UserError(Exception):
    """A problem the user can fix; shown as an Archicad alert in the user's language."""

    def __init__(self, key, **kwargs):
        super().__init__(key)
        self.key, self.kwargs = key, kwargs

    def message(self):
        return T[self.key].format(**self.kwargs)


class Busy(RuntimeError):
    """Archicad is waiting for user input and rejects commands."""


ERR_ONGOING_USER_INPUT = 4001


# --------------------------------------------------------------------------- Archicad

class Archicad:
    """Minimal JSON client for the Archicad API including Tapir commands."""

    def __init__(self, port=None):
        self.url = f"http://127.0.0.1:{port}" if port else self._find_port()

    @staticmethod
    def _post(url, body, timeout):
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def _find_port(self):
        for port in PORT_RANGE:
            url = f"http://127.0.0.1:{port}"
            try:
                if self._post(url, {"command": "API.IsAlive"}, 0.3).get("succeeded"):
                    return url
            except OSError:
                pass
        raise RuntimeError("No running Archicad found.")

    def command(self, name, params=None):
        body = {"command": name}
        if params is not None:
            body["parameters"] = params
        res = self._post(self.url, body, 600)
        if not res.get("succeeded"):
            err = res.get("error") or {}
            if err.get("code") == ERR_ONGOING_USER_INPUT:
                raise Busy(f"{name}: Archicad is waiting for user input")
            raise RuntimeError(f"{name} failed: {err}")
        return res["result"]

    def tapir(self, name, params=None):
        out = self.command("API.ExecuteAddOnCommand", {
            "addOnCommandId": {"commandNamespace": "TapirCommand", "commandName": name},
            "addOnCommandParameters": params or {},
        })["addOnCommandResponse"]
        if "error" in out:
            raise RuntimeError(f"{name} failed: {out['error']}")
        return out

    def alert(self, message):
        self.tapir("ShowAlert", {"alertType": "warning", "title": T["title"], "message": message, "button1": "OK"})


# --------------------------------------------------------------------------- Geometry
#
# An edge is {"segs": [...], "closed": bool}, a segment is
#   ("L", ax, ay, dx, dy, length)                 line from (ax, ay) by (dx, dy)
#   ("A", cx, cy, r, start_angle, sweep, length)  arc, sweep > 0 = counter-clockwise

def _line(ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    return ("L", ax, ay, dx, dy, math.hypot(dx, dy))


def _arc(cx, cy, r, a0, sweep):
    return ("A", cx, cy, r, a0, sweep, abs(sweep) * r)


def _arc_from_chord(ax, ay, bx, by, angle):
    """Polyline arc from A to B with the given arc angle (> 0: counter-clockwise)."""
    dx, dy = bx - ax, by - ay
    c = math.hypot(dx, dy)
    k = 0.5 / math.tan(angle * 0.5)
    cx = (ax + bx) * 0.5 - dy * k
    cy = (ay + by) * 0.5 + dx * k
    r = c / (2.0 * abs(math.sin(angle * 0.5)))
    return _arc(cx, cy, r, math.atan2(ay - cy, ax - cx), angle)


def point_at(seg, s):
    """Point on the segment at arc length s from its start."""
    if seg[0] == "L":
        _, ax, ay, dx, dy, ln = seg
        t = s / ln if ln else 0.0
        return ax + t * dx, ay + t * dy
    _, cx, cy, r, a0, sweep, ln = seg
    a = a0 + (s / ln if ln else 0.0) * sweep
    return cx + r * math.cos(a), cy + r * math.sin(a)


def tangent_at(seg, s):
    """Unit tangent in the direction of travel."""
    if seg[0] == "L":
        _, _, _, dx, dy, ln = seg
        return dx / ln, dy / ln
    _, _, _, _, a0, sweep, ln = seg
    a = a0 + (s / ln) * sweep
    sg = 1.0 if sweep > 0 else -1.0
    return -math.sin(a) * sg, math.cos(a) * sg


def _on_arc(seg, x, y):
    _, cx, cy, _, a0, sweep, _ = seg
    rel = (math.atan2(y - cy, x - cx) - a0) * (1.0 if sweep > 0 else -1.0) % TAU
    return rel <= abs(sweep) + EPS


def _closest_on_seg(seg, px, py):
    if seg[0] == "L":
        _, ax, ay, dx, dy, ln = seg
        l2 = ln * ln
        t = ((px - ax) * dx + (py - ay) * dy) / l2 if l2 else 0.0
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        return ax + t * dx, ay + t * dy
    _, cx, cy, r, _, _, _ = seg
    if _on_arc(seg, px, py):
        a = math.atan2(py - cy, px - cx)
        return cx + r * math.cos(a), cy + r * math.sin(a)
    b = point_at(seg, 0.0)
    e = point_at(seg, seg[-1])
    if (b[0] - px) ** 2 + (b[1] - py) ** 2 <= (e[0] - px) ** 2 + (e[1] - py) ** 2:
        return b
    return e


def closest_point(segs, px, py):
    """Closest point on the segments to (px, py) and the squared distance."""
    best = (0.0, 0.0, float("inf"))
    for seg in segs:
        cx, cy = _closest_on_seg(seg, px, py)
        d2 = (px - cx) ** 2 + (py - cy) ** 2
        if d2 < best[2]:
            best = (cx, cy, d2)
    return best


def _ray_hit(seg, px, py, nx, ny):
    """Distance t > 0 at which the ray p + t·n hits the segment, otherwise None."""
    if seg[0] == "L":
        _, ax, ay, dx, dy, _ = seg
        denom = nx * dy - ny * dx
        if abs(denom) < 1e-12:
            return None
        wx, wy = ax - px, ay - py
        t = (wx * dy - wy * dx) / denom
        u = (wx * ny - wy * nx) / denom
        return t if t > EPS and -EPS <= u <= 1.0 + EPS else None
    _, cx, cy, r, _, _, _ = seg
    fx, fy = px - cx, py - cy
    b = nx * fx + ny * fy
    disc = b * b - (fx * fx + fy * fy - r * r)
    if disc < 0.0:
        return None
    sq = math.sqrt(disc)
    for t in (-b - sq, -b + sq):
        if t > EPS and _on_arc(seg, px + t * nx, py + t * ny):
            return t
    return None


def ray_cast(segs, px, py, nx, ny):
    best = None
    for seg in segs:
        t = _ray_hit(seg, px, py, nx, ny)
        if t is not None and (best is None or t < best):
            best = t
    return best


def stations(edge, spacing):
    """Points at the given spacing along the edge, in a single linear pass.

    Yields (x, y, segment, s within segment, s overall). On closed edges the
    last point is dropped if it coincides with the start.
    """
    segs = edge["segs"]
    out = [(*point_at(segs[0], 0.0), segs[0], 0.0, 0.0)]
    walked = 0.0
    next_at = spacing
    for seg in segs:
        ln = seg[-1]
        while next_at <= walked + ln + 1e-9:
            s = min(next_at - walked, ln)
            out.append((*point_at(seg, s), seg, s, next_at))
            next_at += spacing
        walked += ln
    if edge["closed"] and len(out) > 1:
        lx, ly = out[-1][:2]
        sx, sy = out[0][:2]
        if (lx - sx) ** 2 + (ly - sy) ** 2 < 1e-12:
            out.pop()
    return out


def _corners(top, bottom_segs):
    """Corners of the top edge: position, vertex, opposite point on the bottom edge, normals."""
    segs = top["segs"]
    pairs = [(i - 1, i) for i in range(1, len(segs))]
    if top["closed"]:
        pairs.append((len(segs) - 1, 0))
    starts = [0.0]
    for seg in segs[:-1]:
        starts.append(starts[-1] + seg[-1])
    out = []
    for a, b in pairs:
        t1 = tangent_at(segs[a], segs[a][-1])
        t2 = tangent_at(segs[b], 0.0)
        turn = math.atan2(t1[0] * t2[1] - t1[1] * t2[0], t1[0] * t2[0] + t1[1] * t2[1])
        if abs(turn) < CORNER_MIN_TURN:
            continue
        vx, vy = point_at(segs[b], 0.0)
        cx, cy, _ = closest_point(bottom_segs, vx, vy)
        n1 = _side_normal(segs[a], t1, bottom_segs)
        n2 = _side_normal(segs[b], t2, bottom_segs)
        # Outer corner: the top edge turns away from the bottom edge, so the
        # perpendiculars of both sides diverge and leave a gap.
        convex = t2[0] * n1[0] + t2[1] * n1[1] < 0.0
        out.append({"pos": starts[b], "v": (vx, vy), "c": (cx, cy), "n1": n1, "n2": n2, "convex": convex})
    return out


def _normal_towards(tx, ty, wx, wy):
    nx, ny = -ty, tx
    return (nx, ny) if nx * wx + ny * wy >= 0.0 else (-nx, -ny)


def _side_normal(seg, t, bottom_segs):
    """Normal to t pointing towards the bottom edge; side decided at the segment midpoint."""
    mx, my = point_at(seg, seg[-1] * 0.5)
    cx, cy, _ = closest_point(bottom_segs, mx, my)
    return _normal_towards(*t, cx - mx, cy - my)


def _clean_strokes(top, bottom, spacing):
    """Perpendicular to the top edge, clipped at the miter line in inner corners,
    filled with a fan in outer corners. Returns (position, px, py, ex, ey)."""
    bsegs = bottom["segs"]
    corners = _corners(top, bsegs)
    total = sum(seg[-1] for seg in top["segs"])
    miters = [_line(*k["v"], *k["c"]) for k in corners if not k["convex"] and k["v"] != k["c"]]
    blockers = bsegs + miters
    items = []

    def at_corner(s_glob):
        for k in corners:
            if abs(s_glob - k["pos"]) < 1e-6 or (top["closed"] and k["pos"] == 0.0 and abs(s_glob - total) < 1e-6):
                return k
        return None

    for px, py, seg, s, s_glob in stations(top, spacing):
        cx, cy, d2 = closest_point(bsegs, px, py)
        k = at_corner(s_glob)
        if k is not None:
            if not k["convex"]:
                items.append((s_glob, px, py, cx, cy))   # the miter line itself
            continue                                     # outer corner: handled by the fan
        nx, ny = _normal_towards(*tangent_at(seg, s), cx - px, cy - py)
        t = ray_cast(blockers, px, py, nx, ny)
        if t is None or t * t > 9.0 * d2 + spacing * spacing:
            items.append((s_glob, px, py, cx, cy))       # no sensible hit: fall back to closest point
        else:
            items.append((s_glob, px, py, px + t * nx, py + t * ny))

    for k in corners:
        if not k["convex"]:
            continue
        vx, vy = k["v"]
        n1, n2 = k["n1"], k["n2"]
        theta = math.atan2(n1[0] * n2[1] - n1[1] * n2[0], n1[0] * n2[0] + n1[1] * n2[1])
        # Size the fan density at its widest point (the bisector)
        bx, by = n1[0] + n2[0], n1[1] + n2[1]
        bl = math.hypot(bx, by) or 1.0
        reach = ray_cast(bsegs, vx, vy, bx / bl, by / bl) or math.sqrt(closest_point(bsegs, vx, vy)[2])
        count = max(1, round(abs(theta) * reach / spacing) - 1)
        for i in range(1, count + 1):
            a = theta * i / (count + 1)
            ca, sa = math.cos(a), math.sin(a)
            nx, ny = n1[0] * ca - n1[1] * sa, n1[0] * sa + n1[1] * ca
            t = ray_cast(bsegs, vx, vy, nx, ny)
            if t is not None:
                items.append((k["pos"] + i * 1e-7, vx, vy, vx + t * nx, vy + t * ny))
    items.sort(key=lambda it: it[0])
    return items


def compute_strokes(top, bottom, spacing, clean=True):
    """Strokes from the top to the bottom edge, alternating full and half length."""
    if clean:
        items = _clean_strokes(top, bottom, spacing)
    else:
        items = []
        for px, py, _, _, s_glob in stations(top, spacing):
            cx, cy, _ = closest_point(bottom["segs"], px, py)
            items.append((s_glob, px, py, cx, cy))
    strokes = []
    for i, (_, px, py, ex, ey) in enumerate(items):
        if i % 2:
            ex = (px + ex) * 0.5
            ey = (py + ey) * 0.5
        strokes.append((px, py, ex, ey))
    return strokes


# --------------------------------------------------------------------------- Reading edges

def make_edge(detail):
    """Archicad element (line, polyline, arc, circle) -> edge."""
    kind = detail["type"]
    d = detail["details"]
    if kind == "Line":
        b, e = d["begCoordinate"], d["endCoordinate"]
        return {"segs": [_line(b["x"], b["y"], e["x"], e["y"])], "closed": False}
    if kind == "PolyLine":
        pts = [(c["x"], c["y"]) for c in d["coordinates"]]
        arcs = {a["begIndex"]: a["arcAngle"] for a in d.get("arcs", [])}
        segs = []
        for i in range(len(pts) - 1):
            (ax, ay), (bx, by) = pts[i], pts[i + 1]
            angle = arcs.get(i)
            if angle:
                segs.append(_arc_from_chord(ax, ay, bx, by, angle))
            elif (ax, ay) != (bx, by):
                segs.append(_line(ax, ay, bx, by))
        if not segs:
            raise UserError("zero_length")
        return {"segs": segs, "closed": pts[0] == pts[-1]}
    if kind in ("Arc", "Circle"):
        if abs(d.get("ratio", 1.0) - 1.0) > 1e-9:
            raise UserError("no_ellipse")
        o, r = d["origin"], d["radius"]
        if "begAngle" in d:
            sweep = (d["endAngle"] - d["begAngle"]) % TAU or TAU
            return {"segs": [_arc(o["x"], o["y"], r, d["begAngle"], sweep)], "closed": False}
        return {"segs": [_arc(o["x"], o["y"], r, 0.0, TAU)], "closed": True}
    if kind == "Spline":
        raise UserError("no_spline")
    raise UserError("bad_type")


def read_edges(ac):
    selected = ac.tapir("GetSelectedElements")["elements"]
    if len(selected) != 2:
        raise UserError("select_two", n=len(selected))
    details = ac.tapir("GetDetailsOfElements", {
        "elements": selected,
        "fields": ["type", "floorIndex", "details"],
    })["detailsOfElements"]
    return [make_edge(d) for d in details], details[0]["floorIndex"]


# --------------------------------------------------------------------------- Settings and UI

def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings["spacing"] = float(saved.get("spacing", settings["spacing"]))
        settings["clean"] = bool(saved.get("clean", settings["clean"]))
        settings["language"] = str(saved.get("language", settings["language"]))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return settings


def save_settings(settings):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass


def detect_language(ac, setting):
    """'de'/'en' from the settings, otherwise follow Archicad's UI language."""
    if setting in TEXTS:
        return setting
    try:
        code = ac.command("API.GetProductInfo").get("languageCode", "")
    except RuntimeError:
        code = ""
    return "de" if code in GERMAN_ARCHICAD else "en"


def fmt_number(v):
    return f"{v:g}".replace(".", T["decimal"])


UI_WIDTH, UI_HEIGHT = 260, 150

# A single window styled like a Windows/Archicad dialog; "Next" closes it.
UI_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; overflow: hidden; }
  body { font: 12px "Segoe UI", Tahoma, sans-serif; color: #000; background: #f0f0f0;
         padding: 12px 12px 10px; display: flex; flex-direction: column; user-select: none; }
  .row { display: flex; align-items: center; gap: 6px; }
  .row label { flex: 1; }
  input[type=text] { width: 64px; height: 23px; padding: 0 5px; font: inherit; text-align: right;
                     border: 1px solid #7a7a7a; background: #fff; outline: none; }
  input[type=text]:focus { border-color: #0078d7; }
  .check { margin-top: 8px; display: flex; align-items: center; gap: 6px; }
  .check input { margin: 0; }
  .next { margin-top: 10px; display: flex; align-items: center; gap: 8px; }
  .next .icon { flex: none; width: 20px; height: 20px; border-radius: 50%; background: #0078d7;
                color: #fff; font-weight: bold; display: flex; align-items: center; justify-content: center; }
  .err { color: #c42b1c; }
  .buttons { margin-top: auto; display: flex; justify-content: flex-end; gap: 6px; }
  button { min-width: 75px; height: 23px; font: inherit; background: #e1e1e1;
           border: 1px solid #adadad; }
  button:hover { background: #e5f1fb; border-color: #0078d7; }
  button.default { border: 2px solid #0078d7; }
</style></head><body>
  <div class="row"><label for="s">{spacing}</label>
    <input id="s" type="text" inputmode="decimal" value="{value}" autofocus><span>m</span></div>
  <label class="check"><input id="k" type="checkbox" {checked}> {miter}</label>
  <div class="next" id="m"><span class="icon">i</span><span>{hint}</span></div>
  <div class="buttons"><button id="c">{cancel}</button><button id="ok" class="default">{next}</button></div>
<script>
  var s = document.getElementById("s"), k = document.getElementById("k"), m = document.getElementById("m");
  function send(v) { ACAPI.SubmitResult(v); ACAPI.ClosePalette(); }
  function ok() {
    var v = parseFloat(s.value.replace(",", "."));
    if (!(v > 0)) { m.innerHTML = '<span class="err">{invalid}</span>'; s.focus(); return; }
    send(JSON.stringify({ spacing: v, clean: k.checked }));
  }
  document.getElementById("ok").onclick = ok;
  document.getElementById("c").onclick = function () { send("cancel"); };
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") ok();
    if (ev.key === "Escape") send("cancel");
  });
  s.focus(); s.select();
</script></body></html>"""

UI_PARAMS = {
    # The size only applies with width AND height. Don't set navigationDisabled:
    # Tapir would then block loading this very page.
    "width": UI_WIDTH, "height": UI_HEIGHT,
    "resizable": False, "zoomEnabled": False, "contextMenuEnabled": False,
    "scrollBarsVisible": False,
}


def render_ui(settings):
    html = UI_HTML
    for key in ("spacing", "miter", "hint", "cancel", "next", "invalid"):
        html = html.replace("{" + key + "}", T[key])
    return (html.replace("{value}", fmt_number(settings["spacing"]))
                .replace("{checked}", "checked" if settings["clean"] else ""))


def ask_settings(ac, settings):
    """Shows the settings window in Archicad and waits for the input."""
    ac.tapir("GetScriptUIResult")  # discard any stale result
    params = dict(UI_PARAMS, title=T["title"], htmlContent=render_ui(settings))
    # Twice: on first show Archicad restores the palette's saved size; only the
    # second call (palette already visible) applies ours.
    ac.tapir("ShowScriptUI", params)
    ac.tapir("ShowScriptUI", params)
    deadline = time.monotonic() + UI_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(UI_POLL)
        try:
            res = ac.tapir("GetScriptUIResult")
        except Busy:            # the input field has focus: keep waiting
            continue
        if res.get("hasResult"):
            if res["result"] == "cancel":
                raise Cancelled()
            data = json.loads(res["result"])
            return dict(settings, spacing=float(data["spacing"]), clean=bool(data["clean"]))
    raise Cancelled()


def pick_point(ac, prompt):
    deadline = time.monotonic() + 3.0   # the palette may not release focus immediately
    while True:
        try:
            p = ac.tapir("GetPointFromUser", {"prompt": prompt})["position"]
            return p["x"], p["y"]
        except Busy:
            if time.monotonic() > deadline:
                raise Cancelled()
            time.sleep(UI_POLL)
        except RuntimeError:            # Esc
            raise Cancelled()


# --------------------------------------------------------------------------- Creating elements

def layer_index(ac):
    """Index of the embankment layer. Accepts the layer name of any language so
    mixed-language teams don't end up with duplicate layers; creates it if missing."""
    names = {t["layer"] for t in TEXTS.values()}
    for _ in range(2):
        for a in ac.tapir("GetAttributesByType", {"attributeType": "Layer"})["attributes"]:
            if a["name"] in names:
                return a["index"], a["name"]
        ac.tapir("CreateLayers", {"layerDataArray": [{"name": T["layer"]}]})
    raise RuntimeError(f"Layer '{T['layer']}' could not be created.")


def create_lines(ac, strokes, layer, floor):
    """Creates all strokes in a single call."""
    res = ac.tapir("CreateLineElements", {"linesData": [
        {"begCoordinate": {"x": x1, "y": y1}, "endCoordinate": {"x": x2, "y": y2},
         "layerIndex": layer, "floorInd": floor}
        for x1, y1, x2, y2 in strokes
    ]})["elements"]
    failed = [r for r in res if "elementId" not in r]
    if failed:
        raise RuntimeError(f"{len(failed)} strokes could not be created: {failed[0]}")
    return res


def group(ac, elements):
    """Groups the strokes so one click selects the whole embankment."""
    if len(elements) >= 2:
        ac.tapir("CreateGroups", {"elementGroups": [{"elements": elements}]})


# --------------------------------------------------------------------------- Main flow

def run(ac, settings, spacing=None):
    t0 = time.perf_counter()
    edges, floor = read_edges(ac)   # check first so errors appear before any input
    t1 = time.perf_counter()

    if spacing is None:
        settings = ask_settings(ac, settings)
        save_settings(settings)
    else:
        settings = dict(settings, spacing=spacing)

    cx, cy = pick_point(ac, T["pick"])
    t2 = time.perf_counter()

    d0 = closest_point(edges[0]["segs"], cx, cy)[2]
    d1 = closest_point(edges[1]["segs"], cx, cy)[2]
    top, bottom = (edges[0], edges[1]) if d0 <= d1 else (edges[1], edges[0])
    strokes = compute_strokes(top, bottom, settings["spacing"], settings["clean"])
    t3 = time.perf_counter()

    layer, layer_name = layer_index(ac)
    t4 = time.perf_counter()
    lines = create_lines(ac, strokes, layer, floor)
    t5 = time.perf_counter()
    group(ac, lines)
    t6 = time.perf_counter()

    print(T["done"].format(n=len(lines), layer=layer_name, spacing=fmt_number(settings["spacing"]),
                           corners=T["corners_miter" if settings["clean"] else "corners_fan"]))
    print(f"Time: read {1000 * (t1 - t0):.1f} ms, compute {1000 * (t3 - t2):.2f} ms, "
          f"layer {1000 * (t4 - t3):.1f} ms, create {1000 * (t5 - t4):.1f} ms, "
          f"group {1000 * (t6 - t5):.1f} ms (user input not included)")


def main():
    global T
    parser = argparse.ArgumentParser(description="Embankment hatch for Archicad")
    parser.add_argument("--port", type=int, help="Archicad port (passed automatically by Tapir)")
    parser.add_argument("--spacing", type=float, help="spacing in m, skips the settings window")
    parser.add_argument("--language", choices=["auto", *TEXTS], help="overrides the language setting")
    args, _ = parser.parse_known_args()

    ac = Archicad(args.port)
    settings = load_settings()
    T = TEXTS[detect_language(ac, args.language or settings["language"])]
    try:
        run(ac, settings, args.spacing)
    except Cancelled:
        print(T["cancelled"])
    except UserError as e:
        ac.alert(e.message())
        print(e.message())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text(" ".join(sys.argv) + "\n" + traceback.format_exc(), encoding="utf-8")
        print(f"Error, details in {LOG_FILE}", file=sys.stderr)
        raise
