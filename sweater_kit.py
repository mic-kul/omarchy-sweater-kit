#!/usr/bin/python3
"""Sweater Kit: knitted borders around every Hyprland window.

A click-through layer-shell overlay per monitor. It polls Hyprland's IPC for
window geometry and paints a knitted frame (stockinette stitches) around each
window, with yarn colours picked from the app's icon.
"""

import colorsys
import hashlib
import json
import math
import os
import signal
import socket
import sys
import tomllib
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Gtk4LayerShell  # noqa: E402

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sweater-kit.toml"
DEFAULTS = {
    "thickness": 12.0,   # total knit width in px
    "outset": 6.0,       # how much of it sits outside the window edge; the rest overlaps the window
    "stitch": 12.0,      # stitch width in px
    "yarn": "theme",     # theme: Omarchy theme colours, icon: raw app icon colours
    "design": "",        # force one design for every window, or "" to pick per app
    "poll_ms": 33,
}
DESIGNS = ("solid", "stripes", "checker", "fairisle")


class Config:
    """Settings from ~/.config/sweater-kit.toml, overridable by SWEATER_* env vars, hot-reloaded."""

    def __init__(self):
        self.stamp = None
        self.values = dict(DEFAULTS)
        self.reload()

    def __getattr__(self, name):
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name)

    @property
    def inset(self):
        return max(0.0, self.thickness - self.outset)

    def reload(self):
        """Re-read the file if it changed. Returns True when the settings changed."""
        try:
            stamp = CONFIG_PATH.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp == self.stamp:
            return False
        self.stamp = stamp
        fresh = dict(DEFAULTS)
        if stamp is not None:
            try:
                with open(CONFIG_PATH, "rb") as f:
                    fresh.update({k: v for k, v in tomllib.load(f).items() if k in DEFAULTS})
            except (OSError, tomllib.TOMLDecodeError) as e:
                print(f"sweater-kit: ignoring {CONFIG_PATH}: {e}", file=sys.stderr)
        for key in DEFAULTS:
            if (env := os.environ.get(f"SWEATER_{key.upper()}")) is not None:
                fresh[key] = env
        for key, default in DEFAULTS.items():
            fresh[key] = type(default)(fresh[key])
        changed = fresh != self.values
        self.values = fresh
        return changed


CONFIG = Config()
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
PID_FILE = RUNTIME_DIR / "sweater-kit.pid"
STATE_FILE = RUNTIME_DIR / "sweater-kit.state"  # "on" or "off"; the bar widget watches it


def read_state():
    try:
        return STATE_FILE.read_text().strip() or "on"
    except OSError:
        return "on"


def write_state(state):
    STATE_FILE.write_text(state + "\n")


def control(action):
    """CLI: flip the state file and poke the running overlay."""
    if action == "status":
        print(read_state())
        return
    write_state({"toggle": "off" if read_state() == "on" else "on", "on": "on", "off": "off"}[action])
    try:
        os.kill(int(PID_FILE.read_text()), signal.SIGHUP)
    except (OSError, ValueError):
        sys.exit("sweater-kit: overlay is not running")
THEME_DIR = Path.home() / ".local/state/omarchy/current/theme"
THEME_YARNS = ("red", "yellow", "orange", "green", "cyan", "blue", "magenta",
               "bright_red", "bright_yellow", "bright_green", "bright_cyan", "bright_blue", "bright_magenta")

YARN_BASKET = [
    (0.86, 0.36, 0.30), (0.93, 0.65, 0.25), (0.42, 0.62, 0.36), (0.30, 0.55, 0.75),
    (0.58, 0.42, 0.70), (0.85, 0.50, 0.60), (0.35, 0.65, 0.65), (0.75, 0.60, 0.40),
]
CREAM = (0.96, 0.93, 0.85)


# --- Hyprland IPC -----------------------------------------------------------

class Hypr:
    def __init__(self):
        sig = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        self.sock = f"{runtime}/hypr/{sig}/.socket.sock"

    def query(self, cmd):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self.sock)
            s.sendall(f"j/{cmd}".encode())
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
        return json.loads(b"".join(chunks))

    def option(self, name, key="int", default=0):
        try:
            return self.query(f"getoption {name}").get(key, default)
        except Exception:
            return default


# --- Yarn colours -----------------------------------------------------------

class YarnShop:
    """Turns a window class into a small palette, sampled from the app icon."""

    def __init__(self, display):
        self.icons = Gtk.IconTheme.get_for_display(display)
        self.cache = {}
        self.desktop_icons = self._index_desktop_files()
        self.theme = None
        self.theme_stamp = None
        self.reload_theme()

    def palette(self, cls):
        if cls not in self.cache:
            icon = self._sample_icon(cls) or self._from_basket(cls)
            self.cache[cls] = self._dye_to_theme(cls, icon) if self.theme else icon
        return self.cache[cls]

    def reload_theme(self):
        """Re-read the Omarchy theme. Returns True when it changed."""
        if CONFIG.yarn != "theme":
            changed = self.theme is not None
            self.theme = self.theme_stamp = None
            return changed
        try:
            stamp = (THEME_DIR / "colors.toml").stat().st_mtime_ns
            if stamp == self.theme_stamp:
                return False
            with open(THEME_DIR / "colors.toml", "rb") as f:
                colours = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return False
        self.theme_stamp = stamp
        self.theme = {k: self._hex(v) for k, v in colours.items() if isinstance(v, str) and v.startswith("#")}
        self.cache.clear()
        return True

    def _dye_to_theme(self, cls, icon_palette):
        """Keep the app's hue but pick the yarn from the theme's basket."""
        yarns = [self.theme[k] for k in THEME_YARNS if k in self.theme]
        if not yarns:
            return icon_palette
        main_h, main_l, main_s = colorsys.rgb_to_hls(*icon_palette[0])
        if main_s < 0.2:  # greyish icon: fall back to the theme accent
            main = self.theme.get("accent", yarns[0])
        else:
            main = min(yarns, key=lambda c: self._hue_distance(colorsys.rgb_to_hls(*c)[0], main_h))
        accent = self.theme.get("bright_foreground") or self.theme.get("light_foreground") or CREAM
        return [main, self._tint(main, 0.35), accent]

    @staticmethod
    def _hue_distance(a, b):
        d = abs(a - b)
        return min(d, 1 - d)

    @staticmethod
    def _hex(value):
        v = value.lstrip("#")
        return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def _index_desktop_files(self):
        icons = {}
        dirs = [Path("/usr/share/applications"), Path.home() / ".local/share/applications"]
        for d in dirs:
            for f in d.glob("*.desktop") if d.is_dir() else []:
                try:
                    text = f.read_text(errors="ignore")
                except OSError:
                    continue
                icon = wm_class = None
                for line in text.splitlines():
                    if line.startswith("Icon=") and icon is None:
                        icon = line[5:].strip()
                    elif line.startswith("StartupWMClass="):
                        wm_class = line[15:].strip()
                if icon:
                    icons.setdefault(f.stem.lower(), icon)
                    if wm_class:
                        icons.setdefault(wm_class.lower(), icon)
        return icons

    def _sample_icon(self, cls):
        candidates = [cls, cls.lower(), self.desktop_icons.get(cls.lower())]
        for name in filter(None, candidates):
            path = self._icon_path(name)
            if path:
                colours = self._dominant_colours(path)
                if colours:
                    return colours
        return None

    def _icon_path(self, name):
        if name.startswith("/"):
            return name if os.path.exists(name) else None
        if not self.icons.has_icon(name):
            return None
        paintable = self.icons.lookup_icon(name, None, 64, 1, Gtk.TextDirection.NONE, 0)
        f = paintable.get_file() if paintable else None
        return f.get_path() if f else None

    def _dominant_colours(self, path):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(path, 48, 48)
        except GLib.Error:
            return None
        pb = pb.add_alpha(False, 0, 0, 0) if not pb.get_has_alpha() else pb
        data, stride, w, h = pb.get_pixels(), pb.get_rowstride(), pb.get_width(), pb.get_height()
        buckets = {}
        for y in range(h):
            for x in range(w):
                i = y * stride + x * 4
                r, g, b, a = data[i] / 255, data[i + 1] / 255, data[i + 2] / 255, data[i + 3] / 255
                if a < 0.5:
                    continue
                hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
                if ss < 0.25 or ll < 0.12 or ll > 0.92:
                    continue
                key = int(hh * 12)
                acc = buckets.setdefault(key, [0.0, 0.0, 0.0, 0.0])
                weight = ss * a
                acc[0] += r * weight
                acc[1] += g * weight
                acc[2] += b * weight
                acc[3] += weight
        if not buckets:
            return None
        ranked = sorted(buckets.values(), key=lambda v: -v[3])
        main = tuple(c / ranked[0][3] for c in ranked[0][:3])
        if len(ranked) > 1 and ranked[1][3] > ranked[0][3] * 0.2:
            second = tuple(c / ranked[1][3] for c in ranked[1][:3])
        else:
            second = CREAM
        return [main, self._tint(main, 0.35), second]

    def _from_basket(self, cls):
        digest = hashlib.md5(cls.encode()).digest()
        main = YARN_BASKET[digest[0] % len(YARN_BASKET)]
        return [main, self._tint(main, 0.35), CREAM]

    @staticmethod
    def _tint(rgb, amount):
        return tuple(c + (1 - c) * amount for c in rgb)


# --- Knitting ---------------------------------------------------------------

def shade(rgb, k):
    return tuple(max(0.0, min(1.0, c * k)) for c in rgb)


def draw_stitch(cr, x, y, sw, sh, rgb):
    """One stockinette 'V' stitch with its top-left corner at (x, y)."""
    leg = sw * 0.42
    cx, by = x + sw / 2, y + sh
    for colour, width, dy in ((shade(rgb, 0.55), leg + 1.2, 0.6), (rgb, leg, 0.0), (shade(rgb, 1.25), leg * 0.35, -0.4)):
        cr.set_source_rgb(*colour)
        cr.set_line_width(width)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(x + leg * 0.3, y + dy)
        cr.curve_to(x + sw * 0.35, y + sh * 0.55 + dy, cx - leg * 0.2, by - leg * 0.2 + dy, cx, by + dy)
        cr.stroke()
        cr.move_to(x + sw - leg * 0.3, y + dy)
        cr.curve_to(x + sw * 0.65, y + sh * 0.55 + dy, cx + leg * 0.2, by - leg * 0.2 + dy, cx, by + dy)
        cr.stroke()


def stitch_colour(design, palette, row, col):
    main, light, accent = palette
    if design == "stripes":
        return (main, accent, light)[row % 3]
    if design == "checker":
        return main if ((col // 3) + row) % 2 == 0 else accent
    if design == "fairisle":
        return accent if (col + row * 2) % 4 == 0 else main
    return main


def knit_tile(design, palette, thickness, scale, backing=None):
    """A repeating strip of knitting, `thickness` px tall, tiled horizontally."""
    sw = CONFIG.stitch
    rows = max(2, round(thickness / (sw * 0.85)))
    sh = thickness / rows
    period = 12
    width = sw * period
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, math.ceil(width * scale), math.ceil(thickness * scale))
    surf.set_device_scale(scale, scale)
    cr = cairo.Context(surf)
    cr.set_source_rgb(*(backing or shade(palette[0], 0.45)))
    cr.paint()
    for row in range(rows + 1):
        for col in range(-1, period + 1):
            draw_stitch(cr, col * sw, row * sh - sh * 0.35, sw, sh * 1.05, stitch_colour(design, palette, row, col))
    surf.flush()
    pattern = cairo.SurfacePattern(surf)
    pattern.set_extend(cairo.EXTEND_REPEAT)
    pattern.set_filter(cairo.FILTER_GOOD)
    return pattern


def rounded_rect(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def knit_frame(cr, rect, pattern, rounding):
    """Paint a knitted ring around `rect` = (x, y, w, h) in logical px."""
    x, y, w, h = rect
    t, outset = CONFIG.thickness, CONFIG.outset
    ox, oy, ow, oh = x - outset, y - outset, w + 2 * outset, h + 2 * outset
    outer_r = rounding + outset if rounding else 3
    cr.save()
    rounded_rect(cr, ox, oy, ow, oh, outer_r)
    rounded_rect(cr, ox + t, oy + t, ow - 2 * t, oh - 2 * t, max(outer_r - t, 0))
    cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
    cr.clip()

    # Four mitred sides so the stitches always run along the edge.
    cx, cy = ox + ow / 2, oy + oh / 2
    corners = [(ox, oy), (ox + ow, oy), (ox + ow, oy + oh), (ox, oy + oh)]
    sides = [  # (corner a, corner b, rotation, origin)
        (corners[0], corners[1], 0.0, (ox, oy)),
        (corners[1], corners[2], math.pi / 2, (ox + ow, oy)),
        (corners[2], corners[3], math.pi, (ox + ow, oy + oh)),
        (corners[3], corners[0], -math.pi / 2, (ox, oy + oh)),
    ]
    for (ax, ay), (bx, by), angle, origin in sides:
        cr.save()
        cr.move_to(ax, ay)
        cr.line_to(bx, by)
        cr.line_to(cx, cy)
        cr.close_path()
        cr.clip()
        m = cairo.Matrix()
        m.translate(*origin)
        m.rotate(angle)
        m.invert()
        pattern.set_matrix(m)
        cr.set_source(pattern)
        cr.paint()
        cr.restore()

    # Soft edge so the knit looks tucked against the window.
    cr.set_source_rgba(0, 0, 0, 0.35)
    cr.set_line_width(1.0)
    rounded_rect(cr, ox + t - 0.5, oy + t - 0.5, ow - 2 * t + 1, oh - 2 * t + 1, max(outer_r - t, 0))
    cr.stroke()
    cr.restore()


# --- Overlay windows --------------------------------------------------------

class Overlay:
    def __init__(self, app, monitor_info, gdk_monitor, yarn):
        self.info = monitor_info
        self.yarn = yarn
        self.scale = monitor_info.get("scale", 1.0) or 1.0
        self.windows = []
        self.tiles = {}
        self.win = Gtk.Window(application=app, decorated=False)
        self.win.add_css_class("sweater-kit")
        Gtk4LayerShell.init_for_window(self.win)
        Gtk4LayerShell.set_namespace(self.win, "sweater-kit")
        Gtk4LayerShell.set_layer(self.win, Gtk4LayerShell.Layer.TOP)
        Gtk4LayerShell.set_monitor(self.win, gdk_monitor)
        Gtk4LayerShell.set_exclusive_zone(self.win, -1)
        Gtk4LayerShell.set_keyboard_mode(self.win, Gtk4LayerShell.KeyboardMode.NONE)
        for edge in (Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM, Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT):
            Gtk4LayerShell.set_anchor(self.win, edge, True)
        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(self.draw)
        self.win.set_child(self.area)
        self.win.connect("realize", self._passthrough)
        self.win.present()

    def _passthrough(self, win):
        surface = win.get_surface()
        surface.set_input_region(cairo.Region())

    def update(self, windows):
        if windows != self.windows:
            self.windows = windows
            self.area.queue_draw()

    def set_knitting(self, knitting):
        self.win.set_visible(knitting)

    def draw(self, area, cr, width, height):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        mx, my = self.info["x"], self.info["y"]
        for i, w in enumerate(self.windows):
            cr.save()
            for above in self.windows[i + 1:]:
                ax, ay = above["at"]
                aw, ah = above["size"]
                cr.rectangle(0, 0, width, height)
                cr.rectangle(ax - mx, ay - my, aw, ah)
                cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
                cr.clip()
            x, y = w["at"]
            ww, wh = w["size"]
            knit_frame(cr, (x - mx, y - my, ww, wh), self.tile_for(w["class"]), w["rounding"])
            cr.restore()

    def tile_for(self, cls):
        if cls not in self.tiles:
            design = CONFIG.design or DESIGNS[hashlib.md5(cls.encode()).digest()[1] % len(DESIGNS)]
            backing = (self.yarn.theme or {}).get("dark_background")
            self.tiles[cls] = knit_tile(design, self.yarn.palette(cls), CONFIG.thickness, self.scale, backing)
        return self.tiles[cls]


class SweaterKit(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="club.tinkerer.sweater-kit")
        self.hypr = Hypr()
        self.overlays = {}
        self.yarn = None
        self.rounding = 0

    def do_activate(self):
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_string("window.sweater-kit { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.yarn = YarnShop(display)
        self.rounding = self.hypr.option("decoration:rounding")
        self.sync_monitors(display)
        self.hold()
        PID_FILE.write_text(str(os.getpid()))
        if not STATE_FILE.exists():
            write_state("on")
        self.apply_state()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, self.apply_state)
        GLib.timeout_add(CONFIG.poll_ms, self.tick)
        GLib.timeout_add_seconds(2, self.slow_tick)

    def sync_monitors(self, display):
        gdk_monitors = {m.get_connector(): m for m in display.get_monitors()}
        for mon in self.hypr.query("monitors"):
            gdk = gdk_monitors.get(mon["name"])
            if gdk and mon["name"] not in self.overlays:
                self.overlays[mon["name"]] = Overlay(self, mon, gdk, self.yarn)
                self.overlays[mon["name"]].set_knitting(getattr(self, "knitting", True))
            elif mon["name"] in self.overlays:
                self.overlays[mon["name"]].info = mon
        for name in list(self.overlays):
            if name not in gdk_monitors:
                self.overlays.pop(name).win.destroy()

    def apply_state(self):
        self.knitting = read_state() == "on"
        for overlay in self.overlays.values():
            overlay.set_knitting(self.knitting)
        return True

    def slow_tick(self):
        self.rounding = self.hypr.option("decoration:rounding")
        self.sync_monitors(Gdk.Display.get_default())
        if CONFIG.reload() | self.yarn.reload_theme():
            self.yarn.cache.clear()
            for overlay in self.overlays.values():
                overlay.tiles.clear()
                overlay.area.queue_draw()
        return True

    def tick(self):
        if not self.knitting:
            return True
        try:
            monitors = {m["id"]: m for m in self.hypr.query("monitors")}
            clients = self.hypr.query("clients")
        except (OSError, ValueError):
            return True
        for overlay in self.overlays.values():
            mon = next((m for m in monitors.values() if m["name"] == overlay.info["name"]), None)
            if mon is None:
                continue
            overlay.info = mon
            visible = [c for c in clients if self.visible_on(c, mon)]
            visible.sort(key=lambda c: (c["floating"], -c["focusHistoryID"]))
            overlay.update([{
                "at": tuple(c["at"]), "size": tuple(c["size"]), "class": c["class"] or c["initialClass"] or "?",
                "rounding": self.rounding,
            } for c in visible])
        return True

    @staticmethod
    def visible_on(c, mon):
        if not c["mapped"] or c["hidden"] or c["monitor"] != mon["id"] or c["fullscreen"] == 2:
            return False
        if c["pinned"]:
            return True
        ws = c["workspace"]["id"]
        return ws in (mon["activeWorkspace"]["id"], mon["specialWorkspace"]["id"])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("toggle", "on", "off", "status"):
        control(sys.argv[1])
    elif "HYPRLAND_INSTANCE_SIGNATURE" not in os.environ:
        sys.exit("sweater-kit: not running under Hyprland")
    else:
        SweaterKit().run(None)
