#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
'''
conformal.py
Conformal map renderer for GIMP 3.2+
Copyright (C) 2006-2011  Michael J. Gruber <conformal@drmicha.warpmail.net>
Updated for version 3.2 by DeeFeeCee

This plug-in ports the original GIMP 2 plug-in to the GIMP 3 API

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, version 2 of the License.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
'''

import cmath
import math
import ast
import re
import sys
from gettext import gettext as _

import gi

gi.require_version("Gegl", "0.4")
gi.require_version("Gimp", "3.0")
from gi.repository import Gegl
from gi.repository import Gimp
from gi.repository import GLib
from gi.repository import GObject

CONF_VERSION = "0.3.7"
PROC_RENDER = "plug-in-conformal-render"
_UI_INITIALIZED = False
GRADIENT_ID_MAP = {0: "HSV", 1: "grayscale", 2: "red-blue", 3: "white-black", 4: "custom"}
ABYSS_ID_MAP = {0: "transparent", 1: "black", 2: "white", 3: "clamp", 4: "loop", 5: "reflect"}

# expose math functions to user equations in a controlled namespace
MATH_NAMESPACE = {
    "math": math,
    "cmath": cmath,
    "complex": complex,
    "abs": abs,
    "min": min,
    "max": max,
    "pow": pow,
}
for _name in dir(math):
    if not _name.startswith("_"):
        MATH_NAMESPACE[_name] = getattr(math, _name)
for _name in dir(cmath):
    if not _name.startswith("_"):
        MATH_NAMESPACE[_name] = getattr(cmath, _name)


class ConformalRenderer:
    #Pixel renderer independent from GIMP glue code.

    QUANT = 4096

    def __init__(
        self,
        width,
        height,
        code,
        constraint,
        xl,
        xr,
        yt,
        yb,
        grid,
        checkerboard,
        gradient,
        abyss_mode,
        abyss_loop_iterations,
        log_base,
    ):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.code = self._normalize_code(code)
        self.constraint = constraint
        self.xl = float(xl)
        self.xr = float(xr)
        self.yt = float(yt)
        self.yb = float(yb)
        self.grid_lines = max(float(grid), 1.0)
        shorter_span = max(min(abs(self.xr - self.xl), abs(self.yt - self.yb)), 1e-9)
        self.grid = max(shorter_span / self.grid_lines, 1e-9)
        self.checkerboard = bool(checkerboard)
        self.gradient = gradient or "HSV"
        self.abyss_mode = (abyss_mode or "transparent").strip().lower()
        self.abyss_loop_iterations = max(1, int(abyss_loop_iterations))
        self.log_base = str(log_base or "2")
        self._validate_gradient_setting()

        self._sx = (self.width - 1.0) / (self.xr - self.xl)
        self._sy = (self.height - 1.0) / (self.yt - self.yb)
        self._two_pi = 2.0 * math.pi
        if self.log_base == "e":
            self._log = 1.0
        elif self.log_base == "10":
            self._log = math.log(10.0)
        else:
            self._log = math.log(2.0)

        self._compiled_code = compile(self.code, "conformal-code", "exec")
        self._compiled_constraint = compile(self.constraint, "conformal-constraint", "exec")

    @staticmethod
    def _normalize_code(code):
        snippet = (code or "").strip()
        if not snippet:
            return "w = z"
        # Map common math notation to Python.
        snippet = snippet.replace("^", "**")
        # Allow shorthand multiplication like 2z -> 2*z.
        snippet = re.sub(r"(\d+(?:\.\d+)?)(\s*)(z)\b", r"\1*\3", snippet)
        # Interpret "i" as the imaginary unit, including forms like 0.2i.
        snippet = re.sub(r"(?<=\d)i\b", "j", snippet)
        snippet = re.sub(r"\bi\b", "(1j)", snippet)
        ConformalRenderer._validate_code_ast(snippet)
        parsed = ast.parse(snippet, mode="exec")
        has_w_assignment = False
        for node in ast.walk(parsed):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "w":
                        has_w_assignment = True
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == "w":
                    has_w_assignment = True
        if not has_w_assignment:
            snippet = f"w = ({snippet})"
        return snippet

    @staticmethod
    def _validate_code_ast(snippet):
        tree = ast.parse(snippet, mode="exec")
        blocked = (
            ast.Import,
            ast.ImportFrom,
            ast.With,
            ast.AsyncWith,
            ast.Try,
            ast.Raise,
            ast.Global,
            ast.Nonlocal,
            ast.Delete,
            ast.ClassDef,
            ast.Lambda,
            ast.Await,
            ast.Yield,
            ast.YieldFrom,
        )
        for node in ast.walk(tree):
            if isinstance(node, blocked):
                raise ValueError(f"Unsupported code construct: {type(node).__name__}")

    @staticmethod
    def _clamp_u8(x):
        return max(0, min(255, int(x)))

    def _validate_gradient_setting(self):
        gradient_name = str(self.gradient).strip().lower()
        if gradient_name in ("hsv", "grayscale", "red-blue", "white-black"):
            return
        if "," in str(self.gradient):
            stops = [token for token in str(self.gradient).split(",") if token.strip()]
            if len(stops) < 2:
                raise ValueError("Custom palette needs at least 2 hex stops.")
            for token in stops:
                self._parse_hex_color(token)
            return
        raise ValueError(f"Unknown gradient preset '{self.gradient}'.")

    @staticmethod
    def _parse_hex_color(token):
        value = token.strip().lstrip("#")
        if len(value) != 6:
            raise ValueError("Expected 6 hex digits")
        return (
            int(value[0:2], 16),
            int(value[2:4], 16),
            int(value[4:6], 16),
            255,
        )

    @staticmethod
    def _lerp_color(a, b, t):
        return (
            int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t),
            255,
        )

    def _arg_color(self, arg_norm):
        # Built-in gradients or user-defined list (#RRGGBB,#RRGGBB,...)
        gradient_name = self.gradient.strip().lower()
        if gradient_name == "grayscale":
            v = self._clamp_u8((arg_norm % 1.0) * 255.0)
            return (v, v, v, 255)
        if gradient_name == "white-black":
            v = self._clamp_u8((1.0 - (arg_norm % 1.0)) * 255.0)
            return (v, v, v, 255)
        if gradient_name == "red-blue":
            return self._lerp_color((255, 0, 0, 255), (0, 0, 255, 255), arg_norm % 1.0)
        if "," in self.gradient:
            try:
                stops = [self._parse_hex_color(token) for token in self.gradient.split(",") if token.strip()]
                if len(stops) >= 2:
                    pos = (arg_norm % 1.0) * (len(stops) - 1)
                    idx = int(pos)
                    t = pos - idx
                    if idx >= len(stops) - 1:
                        return stops[-1]
                    return self._lerp_color(stops[idx], stops[idx + 1], t)
            except Exception:
                pass

        # Default: Fast HSV wheel (s=1,v=1) for argument coloring.
        h = (arg_norm % 1.0) * 6.0
        i = int(h)
        f = h - i
        p = 0.0
        q = 1.0 - f
        t = f
        if i == 0:
            r, g, b = 1.0, t, p
        elif i == 1:
            r, g, b = q, 1.0, p
        elif i == 2:
            r, g, b = p, 1.0, t
        elif i == 3:
            r, g, b = p, q, 1.0
        elif i == 4:
            r, g, b = t, p, 1.0
        else:
            r, g, b = 1.0, p, q
        return (
            self._clamp_u8(r * 255.0),
            self._clamp_u8(g * 255.0),
            self._clamp_u8(b * 255.0),
            255,
        )

    def _mod_shade(self, mod):
        shade = self._clamp_u8(mod * 255.0)
        return (shade, shade, shade, 96)

    def _grid_pixel(self, sqr):
        if self.checkerboard:
            v = 255 if sqr else 0
            return (v, v, v, 80)
        return (0, 0, 0, 80 if sqr else 0)

    def _mirror_coord(self, value, size):
        if size <= 1:
            return 0
        period = 2 * size
        m = value % period
        return m if m < size else (period - 1) - m

    def _evaluate_point(self, z):
        env = {"z": z, "zz": z * z, "w": 0j, "p": True}
        env.update(MATH_NAMESPACE)
        try:
            exec(self._compiled_constraint, {"__builtins__": {}}, env)
        except Exception:
            env["p"] = False

        if env.get("p", False):
            try:
                exec(self._compiled_code, {"__builtins__": {}}, env)
            except Exception:
                env["p"] = False

        w = env.get("w", 0j)
        valid = env.get("p", False)

        try:
            valid = valid and not (math.isnan(w.real) or math.isnan(w.imag))
            valid = valid and not (math.isinf(w.real) or math.isinf(w.imag))
        except Exception:
            valid = False

        if valid:
            try:
                logw = cmath.log(w)
                arg = logw.imag
                if arg < 0.0:
                    arg += self._two_pi
                arg_norm = arg / self._two_pi
                mod = (logw.real / self._log) % 1.0
                sqr = int(math.floor(w.imag / self.grid)) + int(math.floor(w.real / self.grid))
                sqr = sqr % 2
                x_mod = abs((w.real / self.grid) - round(w.real / self.grid))
                y_mod = abs((w.imag / self.grid) - round(w.imag / self.grid))
                grid_line = (x_mod < 0.03) or (y_mod < 0.03)
            except Exception:
                valid = False

        if not valid:
            return False, 0j, 0.0, 0.0, 0, False
        return True, w, arg_norm, mod, sqr, grid_line

    def _sample_mapped_pixel(self, source_pixels, sx, sy):
        tile_x = 0 if 0 <= sx < self.width else (abs(sx) // self.width + 1 if sx < 0 else sx // self.width)
        tile_y = 0 if 0 <= sy < self.height else (abs(sy) // self.height + 1 if sy < 0 else sy // self.height)
        if max(tile_x, tile_y) > self.abyss_loop_iterations:
            return (0, 0, 0, 0)

        if self.abyss_mode == "clamp":
            sx = min(max(0, sx), self.width - 1)
            sy = min(max(0, sy), self.height - 1)
            sidx = (sy * self.width + sx) * 4
            return tuple(source_pixels[sidx:sidx + 4])
        if self.abyss_mode == "loop":
            sx %= self.width
            sy %= self.height
            sidx = (sy * self.width + sx) * 4
            return tuple(source_pixels[sidx:sidx + 4])
        if self.abyss_mode == "reflect":
            sx = self._mirror_coord(sx, self.width)
            sy = self._mirror_coord(sy, self.height)
            sidx = (sy * self.width + sx) * 4
            return tuple(source_pixels[sidx:sidx + 4])
        if self.abyss_mode == "black":
            return (0, 0, 0, 255)
        if self.abyss_mode == "white":
            return (255, 255, 255, 255)
        return (0, 0, 0, 0)

    def render(self, source_pixels=None, progress_cb=None):
        arg_data = bytearray(self.width * self.height * 4)
        mod_data = bytearray(self.width * self.height * 4)
        grid_data = bytearray(self.width * self.height * 4)
        mapped_data = bytearray(self.width * self.height * 4) if source_pixels is not None else None

        max_progress = float(self.width * self.height)
        progress = 0.0

        for row in range(self.height):
            base = row * self.width * 4
            imag = self.yt - (row / self._sy)
            for col in range(self.width):
                z = col / self._sx + self.xl + 1j * imag
                valid, w, arg_norm, mod, sqr, grid_line = self._evaluate_point(z)

                if not valid:
                    arg_px = (0, 0, 0, 255)
                    mod_px = (0, 0, 0, 0)
                    grid_px = (0, 0, 0, 0)
                    mapped_px = (0, 0, 0, 0)
                else:
                    arg_px = self._arg_color(arg_norm)
                    mod_px = self._mod_shade(mod)
                    grid_px = self._grid_pixel(sqr if self.checkerboard else grid_line)
                    if source_pixels is not None:
                        sx = int(round((w.real - self.xl) * self._sx))
                        sy = int(round((self.yt - w.imag) * self._sy))
                        if 0 <= sx < self.width and 0 <= sy < self.height:
                            sidx = (sy * self.width + sx) * 4
                            mapped_px = tuple(source_pixels[sidx:sidx + 4])
                        else:
                            mapped_px = self._sample_mapped_pixel(source_pixels, sx, sy)

                idx = base + (col * 4)
                arg_data[idx:idx + 4] = bytes(arg_px)
                mod_data[idx:idx + 4] = bytes(mod_px)
                grid_data[idx:idx + 4] = bytes(grid_px)
                if mapped_data is not None:
                    mapped_data[idx:idx + 4] = bytes(mapped_px)

                progress += 1.0

            if progress_cb is not None:
                progress_cb(progress / max_progress)

        mapped_bytes = bytes(mapped_data) if mapped_data is not None else None
        return bytes(arg_data), bytes(mod_data), bytes(grid_data), mapped_bytes


def _layer_mode(*names):
    for name in names:
        if hasattr(Gimp.LayerMode, name):
            return getattr(Gimp.LayerMode, name)
    raise AttributeError(f"No supported layer mode found in candidates: {names}")


def _push_bytes_to_layer(layer, width, height, rgba_bytes):
    buffer = layer.get_buffer()
    rect = Gegl.Rectangle.new(0, 0, width, height)

    # GIMP 3 builds may expose different introspection overloads for buffer.set().
    try:
        buffer.set(rect, "R'G'B'A u8", rgba_bytes)
    except TypeError:
        # Fallback overload: set(rect, rowstride, format, bytes)
        buffer.set(rect, width * 4, "R'G'B'A u8", rgba_bytes)

    if hasattr(layer, "update"):
        layer.update(0, 0, width, height)
    if hasattr(layer, "flush"):
        layer.flush()


def _gegl_to_u8(color):
    r, g, b, a = color.get_rgba()
    return (
        max(0, min(255, int(r * 255.0 + 0.5))),
        max(0, min(255, int(g * 255.0 + 0.5))),
        max(0, min(255, int(b * 255.0 + 0.5))),
        max(0, min(255, int(a * 255.0 + 0.5))),
    )


def _drawable_pixels_rgba(drawable, width, height):
    buffer = drawable.get_buffer()
    rect = Gegl.Rectangle.new(0, 0, width, height)
    try:
        raw = buffer.get(rect, 1.0, "R'G'B'A u8", Gegl.AbyssPolicy.NONE)
        if hasattr(raw, "get_data"):
            raw = raw.get_data()
        if raw is not None:
            raw_bytes = bytes(raw)
            if len(raw_bytes) == width * height * 4:
                return raw_bytes
    except Exception:
        pass

    data = bytearray(width * height * 4)
    for y in range(height):
        row = y * width * 4
        for x in range(width):
            try:
                px = _gegl_to_u8(drawable.get_pixel(x, y))
            except Exception:
                px = (0, 0, 0, 255)
            idx = row + x * 4
            data[idx:idx + 4] = bytes(px)
    return bytes(data)


def _show_dialog(procedure, config, width, height):
    gi.require_version("GimpUi", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import GimpUi
    from gi.repository import Gtk

    global _UI_INITIALIZED
    if not _UI_INITIALIZED:
        GimpUi.init(PROC_RENDER)
        _UI_INITIALIZED = True

    dialog = Gtk.Dialog(title="Conformal Map Transform", modal=True)
    RESPONSE_RESET_DEFAULTS = 1
    RESPONSE_RESET_LAST = 2
    dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("Reset _Defaults", RESPONSE_RESET_DEFAULTS)
    dialog.add_button("Reset _Last Used", RESPONSE_RESET_LAST)
    dialog.add_button("_OK", Gtk.ResponseType.OK)
    dialog.set_default_size(760, 560)
    area = dialog.get_content_area()
    grid = Gtk.Grid(column_spacing=8, row_spacing=8, margin=8)
    area.add(grid)

    row = 0
    code_label = Gtk.Label(label="Formula")
    code_label.set_xalign(0.0)
    code_label.set_tooltip_text("Python code assigning w from z.")
    grid.attach(code_label, 0, row, 1, 1)
    code_view = Gtk.TextView()
    code_view.set_monospace(True)
    code_view.set_tooltip_text("Supports shorthand like 2z, i, and ^.")
    code_buffer = code_view.get_buffer()
    code_buffer.set_text(config.get_property("code"))
    sw = Gtk.ScrolledWindow()
    sw.set_min_content_height(120)
    sw.add(code_view)
    grid.attach(sw, 1, row, 3, 1)
    row += 1

    scale_widgets = {}

    def _make_scale(name, label_text, lower, upper, value, step, page, digits=5):
        nonlocal row
        label = Gtk.Label(label=label_text)
        label.set_xalign(0.0)
        grid.attach(label, 0, row, 1, 1)
        adj = Gtk.Adjustment(value=float(value), lower=float(lower), upper=float(upper), step_increment=float(step), page_increment=float(page), page_size=0.0)
        scale = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, adj)
        scale.set_digits(digits)
        scale.set_draw_value(True)
        scale.set_hexpand(True)
        grid.attach(scale, 1, row, 3, 1)
        spin = Gtk.SpinButton.new(adj, climb_rate=0.5, digits=digits)
        spin.set_numeric(True)
        spin.set_width_chars(8)
        grid.attach(spin, 4, row, 1, 1)
        scale_widgets[name] = (scale, spin)
        row += 1

    coord_combo = Gtk.ComboBoxText()
    coord_combo.append("relative", "Relative coordinates")
    coord_combo.append("pixels", "Pixels")
    coord_combo.set_active_id(str(config.get_property("coord-system")) if hasattr(config, "get_property") else "relative")
    if coord_combo.get_active_id() is None:
        coord_combo.set_active_id("relative")
    coord_label = Gtk.Label(label="Coordinate system")
    coord_label.set_xalign(0.0)
    coord_label.set_tooltip_text("Relative coordinates: 1 equals the distance to the short edge.")
    grid.attach(coord_label, 0, row, 1, 1)
    coord_combo.set_tooltip_text("Select center coordinate units.")
    grid.attach(coord_combo, 1, row, 1, 1)
    row += 1

    _make_scale("center-x", "Center X", -1.0e3, 1.0e3, config.get_property("center-x"), 0.01, 0.1, digits=5)
    _make_scale("center-y", "Center Y", -1.0e3, 1.0e3, config.get_property("center-y"), 0.01, 0.1, digits=5)
    _make_scale("zoom", "Zoom", -1.0e3, 1.0e3, config.get_property("zoom"), 0.01, 0.1, digits=5)

    def _convert_units(_widget):
        old = getattr(_convert_units, "last", "relative")
        new = coord_combo.get_active_id() or "relative"
        if old != new:
            cx = scale_widgets["center-x"][0].get_value()
            cy = scale_widgets["center-y"][0].get_value()
            short_half = min(width, height) / 2.0
            img_cx = (width - 1) / 2.0
            img_cy = (height - 1) / 2.0
            if old == "relative" and new == "pixels":
                scale_widgets["center-x"][0].set_value(img_cx + cx * short_half)
                scale_widgets["center-y"][0].set_value(img_cy - cy * short_half)
            elif old == "pixels" and new == "relative":
                scale_widgets["center-x"][0].set_value((cx - img_cx) / max(short_half, 1e-9))
                scale_widgets["center-y"][0].set_value((img_cy - cy) / max(short_half, 1e-9))

        if new == "pixels":
            lower, upper, step, page, digits = -1.0e4, 1.0e4, 0.5, 10.0, 4
        else:
            lower, upper, step, page, digits = -1.0e3, 1.0e3, 0.01, 0.1, 5
        for key in ("center-x", "center-y"):
            scale, spin = scale_widgets[key]
            adj = scale.get_adjustment()
            adj.set_lower(lower)
            adj.set_upper(upper)
            adj.set_step_increment(step)
            adj.set_page_increment(page)
            scale.set_digits(digits)
            spin.set_digits(digits)
        _convert_units.last = new

    _convert_units.last = coord_combo.get_active_id() or "relative"
    coord_combo.connect("changed", _convert_units)
    _convert_units(None)

    gradient_combo = Gtk.ComboBoxText()
    for key, label in [("HSV", "HSV"), ("grayscale", "Grayscale"), ("red-blue", "Red-Blue"), ("white-black", "White-Black"), ("custom", "Custom")]:
        gradient_combo.append(key, label)
    gradient_value = config.get_property("gradient-preset")
    gradient_value = GRADIENT_ID_MAP.get(gradient_value, "HSV") if isinstance(gradient_value, int) else str(gradient_value)
    gradient_combo.set_active_id(gradient_value)
    grid.attach(Gtk.Label(label="Palette", xalign=0.0), 0, row, 1, 1)
    grid.attach(gradient_combo, 1, row, 1, 1)

    gradient_entry = Gtk.Entry()
    gradient_entry.set_text(config.get_property("gradient-custom"))
    grid.attach(Gtk.Label(label="Custom palette", xalign=0.0), 2, row, 1, 1)
    grid.attach(gradient_entry, 3, row, 1, 1)
    row += 1

    def _pick_color(_button):
        chooser = Gtk.ColorChooserDialog(title="Pick color", transient_for=dialog, modal=True)
        chooser.set_use_alpha(False)
        if chooser.run() == Gtk.ResponseType.OK:
            rgba = chooser.get_rgba()
            hex_value = "#{:02x}{:02x}{:02x}".format(int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255))
            current = gradient_entry.get_text().strip()
            gradient_entry.set_text(f"{current},{hex_value}" if current else hex_value)
        chooser.destroy()

    pick_btn = Gtk.Button(label="Pick color…")
    pick_btn.connect("clicked", _pick_color)
    grid.attach(Gtk.Label(), 2, row, 1, 1)
    grid.attach(pick_btn, 3, row, 1, 1)
    row += 1

    abyss_combo = Gtk.ComboBoxText()
    for key, label in [("transparent", "Transparent"), ("black", "Black"), ("white", "White"), ("clamp", "Clamp"), ("loop", "Loop"), ("reflect", "Reflect")]:
        abyss_combo.append(key, label)
    abyss_value = config.get_property("abyss-mode")
    abyss_value = ABYSS_ID_MAP.get(abyss_value, "transparent") if isinstance(abyss_value, int) else str(abyss_value)
    abyss_combo.set_active_id(abyss_value)
    grid.attach(Gtk.Label(label="Abyss mode", xalign=0.0), 0, row, 1, 1)
    grid.attach(abyss_combo, 1, row, 1, 1)

    abyss_spin = Gtk.SpinButton()
    abyss_spin.set_adjustment(Gtk.Adjustment(value=float(config.get_property("abyss-loop-iterations")), lower=1.0, upper=1024.0, step_increment=1.0, page_increment=10.0, page_size=0.0))
    grid.attach(Gtk.Label(label="Wrap iterations", xalign=0.0), 2, row, 1, 1)
    grid.attach(abyss_spin, 3, row, 1, 1)
    row += 1

    _make_scale("grid-spacing", "Grid length (shorter side)", 1.0, 1000.0, config.get_property("grid-spacing"), 1.0, 10.0, digits=2)

    log_combo = Gtk.ComboBoxText()
    log_combo.append("2", "2")
    log_combo.append("e", "e")
    log_combo.append("10", "10")
    log_combo.set_active_id(str(config.get_property("log-base")))
    if log_combo.get_active_id() is None:
        log_combo.set_active_id("2")
    grid.attach(Gtk.Label(label="Logarithm", xalign=0.0), 0, row, 1, 1)
    grid.attach(log_combo, 1, row, 1, 1)
    row += 1

    transform_check = Gtk.CheckButton(label="Transform active layer")
    transform_check.set_active(bool(config.get_property("transform-active-layer")))
    analysis_check = Gtk.CheckButton(label="Add analysis layers")
    analysis_check.set_active(bool(config.get_property("create-analysis-layers")))
    checker_check = Gtk.CheckButton(label="Checkerboard")
    checker_check.set_active(bool(config.get_property("checkerboard")))
    grid.attach(transform_check, 0, row, 2, 1)
    grid.attach(analysis_check, 2, row, 2, 1)
    row += 1
    grid.attach(checker_check, 2, row, 2, 1)

    def _sync():
        checker_check.set_sensitive(analysis_check.get_active())
        gradient_entry.set_sensitive(gradient_combo.get_active_id() == "custom")
        pick_btn.set_sensitive(gradient_combo.get_active_id() == "custom")
        abyss_spin.set_sensitive(abyss_combo.get_active_id() in ("loop", "reflect"))

    analysis_check.connect("toggled", lambda *_a: _sync())
    gradient_combo.connect("changed", lambda *_a: _sync())
    abyss_combo.connect("changed", lambda *_a: _sync())
    _sync()

    def _reset_defaults():
        code_buffer.set_text("w = z")
        scale_widgets["center-x"][0].set_value(0.0)
        scale_widgets["center-y"][0].set_value(0.0)
        scale_widgets["zoom"][0].set_value(1.0)
        scale_widgets["grid-spacing"][0].set_value(4.0)
        coord_combo.set_active_id("relative")
        gradient_combo.set_active_id("HSV")
        gradient_entry.set_text("#ff0000,#ffff00,#00ff00,#00ffff,#0000ff")
        abyss_combo.set_active_id("transparent")
        abyss_spin.set_value(2)
        log_combo.set_active_id("2")
        transform_check.set_active(True)
        analysis_check.set_active(True)
        checker_check.set_active(False)
        _sync()

    last_used = {
        "code": config.get_property("code"),
        "center-x": config.get_property("center-x"),
        "center-y": config.get_property("center-y"),
        "zoom": config.get_property("zoom"),
        "grid-spacing": config.get_property("grid-spacing"),
        "coord-system": coord_combo.get_active_id() or "relative",
        "gradient-preset": gradient_combo.get_active_id() or "HSV",
        "gradient-custom": gradient_entry.get_text(),
        "abyss-mode": abyss_combo.get_active_id() or "transparent",
        "abyss-loop-iterations": int(abyss_spin.get_value_as_int()),
        "log-base": log_combo.get_active_id() or "2",
        "transform-active-layer": transform_check.get_active(),
        "create-analysis-layers": analysis_check.get_active(),
        "checkerboard": checker_check.get_active(),
    }

    def _reset_last():
        code_buffer.set_text(last_used["code"])
        scale_widgets["center-x"][0].set_value(float(last_used["center-x"]))
        scale_widgets["center-y"][0].set_value(float(last_used["center-y"]))
        scale_widgets["zoom"][0].set_value(float(last_used["zoom"]))
        scale_widgets["grid-spacing"][0].set_value(float(last_used["grid-spacing"]))
        coord_combo.set_active_id(last_used["coord-system"])
        gradient_combo.set_active_id(last_used["gradient-preset"])
        gradient_entry.set_text(last_used["gradient-custom"])
        abyss_combo.set_active_id(last_used["abyss-mode"])
        abyss_spin.set_value(last_used["abyss-loop-iterations"])
        log_combo.set_active_id(last_used["log-base"])
        transform_check.set_active(bool(last_used["transform-active-layer"]))
        analysis_check.set_active(bool(last_used["create-analysis-layers"]))
        checker_check.set_active(bool(last_used["checkerboard"]))
        _sync()

    dialog.show_all()
    accepted = False
    while True:
        response = dialog.run()
        if response == RESPONSE_RESET_DEFAULTS:
            _reset_defaults()
            continue
        if response == RESPONSE_RESET_LAST:
            _reset_last()
            continue
        if response == Gtk.ResponseType.OK:
            accepted = True
            break
        break

    if accepted:
        start = code_buffer.get_start_iter()
        end = code_buffer.get_end_iter()
        config.set_property("code", code_buffer.get_text(start, end, True))
        for name, pair in scale_widgets.items():
            config.set_property(name, float(pair[0].get_value()))
        config.set_property("coord-system", coord_combo.get_active_id() or "relative")
        config.set_property("gradient-preset", gradient_combo.get_active_id() or "HSV")
        config.set_property("gradient-custom", gradient_entry.get_text().strip())
        config.set_property("abyss-mode", abyss_combo.get_active_id() or "transparent")
        config.set_property("abyss-loop-iterations", int(abyss_spin.get_value_as_int()))
        config.set_property("log-base", log_combo.get_active_id() or "2")
        config.set_property("transform-active-layer", bool(transform_check.get_active()))
        config.set_property("create-analysis-layers", bool(analysis_check.get_active()))
        config.set_property("checkerboard", bool(checker_check.get_active()))
    dialog.destroy()
    return accepted


def conformal_run(procedure, run_mode, image, drawables, config, data):
    width = image.get_width()
    height = image.get_height()

    code = config.get_property("code")
    constraint = "p = True"
    xl = config.get_property("x-left")
    xr = config.get_property("x-right")
    yt = config.get_property("y-top")
    yb = config.get_property("y-bottom")
    grid = config.get_property("grid-spacing")
    xy_units = config.get_property("x-y-units")
    checkerboard = config.get_property("checkerboard")
    gradient_preset = config.get_property("gradient-preset")
    gradient_custom = config.get_property("gradient-custom")
    if isinstance(gradient_preset, int):
        gradient_preset = GRADIENT_ID_MAP.get(gradient_preset, "HSV")
    gradient = gradient_custom if str(gradient_preset).strip().lower() == "custom" else gradient_preset
    abyss_mode = config.get_property("abyss-mode")
    if isinstance(abyss_mode, int):
        abyss_mode = ABYSS_ID_MAP.get(abyss_mode, "transparent")
    abyss_loop_iterations = config.get_property("abyss-loop-iterations")
    transform_layer = config.get_property("transform-active-layer")
    create_analysis = config.get_property("create-analysis-layers")

    if run_mode == Gimp.RunMode.INTERACTIVE:
        if not _show_dialog(procedure, config, width, height):
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
        code = config.get_property("code")
        constraint = "p = True"
        xl = config.get_property("x-left")
        xr = config.get_property("x-right")
        yt = config.get_property("y-top")
        yb = config.get_property("y-bottom")
        grid = config.get_property("grid-spacing")
        xy_units = config.get_property("x-y-units")
        checkerboard = config.get_property("checkerboard")
        gradient_preset = config.get_property("gradient-preset")
        gradient_custom = config.get_property("gradient-custom")
        if isinstance(gradient_preset, int):
            gradient_preset = GRADIENT_ID_MAP.get(gradient_preset, "HSV")
        gradient = gradient_custom if str(gradient_preset).strip().lower() == "custom" else gradient_preset
        abyss_mode = config.get_property("abyss-mode")
        if isinstance(abyss_mode, int):
            abyss_mode = ABYSS_ID_MAP.get(abyss_mode, "transparent")
        abyss_loop_iterations = config.get_property("abyss-loop-iterations")
        transform_layer = config.get_property("transform-active-layer")
        create_analysis = config.get_property("create-analysis-layers")

    short_side = min(width, height)
    x_mid = (xl + xr) / 2.0
    y_mid = (yt + yb) / 2.0
    if width >= height:
        y_span = abs(yt - yb)
        unit = y_span / max(1.0, float(short_side - 1))
        x_span = unit * max(1.0, float(width - 1))
    else:
        x_span = abs(xr - xl)
        unit = x_span / max(1.0, float(short_side - 1))
        y_span = unit * max(1.0, float(height - 1))
    axl, axr = x_mid - x_span / 2.0, x_mid + x_span / 2.0
    ayt, ayb = y_mid + y_span / 2.0, y_mid - y_span / 2.0

    try:
        renderer_full = ConformalRenderer(
            width,
            height,
            code,
            constraint,
            xl,
            xr,
            yt,
            yb,
            grid,
            checkerboard,
            gradient,
            abyss_mode,
            abyss_loop_iterations,
        )
        renderer_analysis = ConformalRenderer(
            width,
            height,
            code,
            constraint,
            axl,
            axr,
            ayt,
            ayb,
            grid,
            checkerboard,
            gradient,
            abyss_mode,
            abyss_loop_iterations,
        )
    except Exception as exc:
        Gimp.message(f"Conformal Mapping input error: {exc}")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))
    source = drawables[0] if drawables else image.get_active_layer()
    source_pixels = _drawable_pixels_rgba(source, width, height) if (transform_layer and source is not None) else None

    if run_mode == Gimp.RunMode.INTERACTIVE:
        Gimp.progress_init("Rendering conformal map…")

    try:
        _, _, _, mapped_pixels = renderer_full.render(
            source_pixels=source_pixels,
            progress_cb=(lambda value: Gimp.progress_update(value)) if run_mode == Gimp.RunMode.INTERACTIVE else None
        )
        arg_pixels, mod_pixels, grid_pixels, _ = renderer_analysis.render(
            source_pixels=None,
            progress_cb=None,
        )
    except Exception as exc:
        Gimp.message(f"Conformal Mapping render error: {exc}")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))

    image.undo_group_start()
    try:
        if transform_layer and mapped_pixels is not None:
            mapped_layer = Gimp.Layer.new(
                image,
                "Conformal Transform",
                width,
                height,
                Gimp.ImageType.RGBA_IMAGE,
                100.0,
                _layer_mode("NORMAL", "NORMAL_LEGACY"),
            )
            image.insert_layer(mapped_layer, None, -1)
            _push_bytes_to_layer(mapped_layer, width, height, mapped_pixels)

        if create_analysis:
            arg_layer = Gimp.Layer.new(
                image,
                "Argument",
                width,
                height,
                Gimp.ImageType.RGBA_IMAGE,
                100.0,
                _layer_mode("NORMAL", "NORMAL_LEGACY"),
            )
            mod_layer = Gimp.Layer.new(
                image,
                "Log Modulus",
                width,
                height,
                Gimp.ImageType.RGBA_IMAGE,
                33.3,
                _layer_mode("LCH_VALUE", "HSV_VALUE", "VALUE", "VALUE_LEGACY"),
            )
            grid_layer = Gimp.Layer.new(
                image,
                "Checkerboard" if checkerboard else "Grid",
                width,
                height,
                Gimp.ImageType.RGBA_IMAGE,
                33.3,
                _layer_mode("DARKEN_ONLY", "DARKEN_ONLY_LEGACY", "DARKEN", "DARKEN_LEGACY"),
            )
            image.insert_layer(arg_layer, None, -1)
            image.insert_layer(mod_layer, None, -1)
            image.insert_layer(grid_layer, None, -1)
            _push_bytes_to_layer(arg_layer, width, height, arg_pixels)
            _push_bytes_to_layer(mod_layer, width, height, mod_pixels)
            _push_bytes_to_layer(grid_layer, width, height, grid_pixels)

        comment = (
            f"# conformal {CONF_VERSION}\n"
            f"code = \"\"\"\n{code}\n\"\"\"\n"
            f"constraint = \"\"\"\n{constraint}\n\"\"\"\n"
            f"xl = {xl}\nxr = {xr}\nyt = {yt}\nyb = {yb}\n"
            f"grid = {grid}\ncheckerboard = {int(checkerboard)}\n"
            f"gradient = {gradient}\n"
            f"abyss_mode = {abyss_mode}\nabyss_loop_iterations = {abyss_loop_iterations}\n"
            f"width = {width}\nheight = {height}\n"
        )
        parasite = Gimp.Parasite.new("gimp-comment", Gimp.PARASITE_PERSISTENT, comment.encode("utf-8"))
        image.attach_parasite(parasite)
    finally:
        image.undo_group_end()

    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


class ConformalPlugin(Gimp.PlugIn):
    def do_set_i18n(self, procname):
        return False

    def do_query_procedures(self):
        return [PROC_RENDER]

    def do_create_procedure(self, name):
        if name != PROC_RENDER:
            return None

        procedure = Gimp.ImageProcedure.new(
            self,
            name,
            Gimp.PDBProcType.PLUGIN,
            conformal_run,
            None,
        )
        procedure.set_image_types("*")
        procedure.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE | Gimp.ProcedureSensitivityMask.NO_DRAWABLES)
        procedure.set_menu_label("_Conformal Mapping")
        procedure.add_menu_path("<Image>/Filters/Distorts")
        procedure.set_documentation(
            "Distort an existing layer with a conformal map",
            "Transforms the active layer through a conformal map and can optionally create argument/modulus/grid analysis layers.",
            name,
        )
        procedure.set_attribution("Michael J Gruber", "Ported for GIMP 3.2", "2026")

        procedure.add_string_argument(
            "code",
            "_Formula",
            "Python code assigning w; supports helper functions/recursion, '^' exponentiation, and 'i' as imaginary unit",
            "w = z",
            GObject.ParamFlags.READWRITE,
        )
        procedure.add_double_argument("x-left", "X _left", "Left bound of source plane", -1.0e9, 1.0e9, -1.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("x-right", "X r_ight", "Right bound of source plane", -1.0e9, 1.0e9, 1.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("y-top", "Y _top", "Top bound of source plane", -1.0e9, 1.0e9, 1.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("y-bottom", "Y bo_ttom", "Bottom bound of source plane", -1.0e9, 1.0e9, -1.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("grid-spacing", "Grid _length (shorter side)", "Number of grid lines on the shorter axis", 1.0, 1000.0, 4.0, GObject.ParamFlags.READWRITE)
        units_choice = Gimp.Choice.new()
        units_choice.add("portion", 0, _("Portion"), "Normalized image portion units")
        units_choice.add("pixels", 1, _("Pixels"), "Absolute pixel units")
        procedure.add_choice_argument("x-y-units", "_X/Y units", "Coordinate unit system for x/y bounds", units_choice, "portion", GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("checkerboard", "_Checkerboard", "Use checkerboard instead of line grid", False, GObject.ParamFlags.READWRITE)
        choices_gradient = Gimp.Choice.new()
        choices_gradient.add("HSV", 0, _("HSV"), "HSV wheel")
        choices_gradient.add("grayscale", 1, _("Grayscale"), "Black to white")
        choices_gradient.add("red-blue", 2, _("Red-Blue"), "Red to blue")
        choices_gradient.add("white-black", 3, _("White-Black"), "White to black")
        choices_gradient.add("custom", 4, _("Custom…"), "Custom hex-stop palette")
        procedure.add_choice_argument(
            "gradient-preset",
            "_Palette",
            "Gradient preset",
            choices_gradient,
            "HSV",
            GObject.ParamFlags.READWRITE,
        )
        procedure.add_string_argument(
            "gradient-custom",
            "Custom p_alette",
            "Custom gradient stops (#RRGGBB,#RRGGBB,...) used when preset is 'custom'",
            "#ff0000,#ffff00,#00ff00,#00ffff,#0000ff",
            GObject.ParamFlags.READWRITE,
        )
        choices_abyss = Gimp.Choice.new()
        choices_abyss.add("transparent", 0, _("Transparent"), "Transparent outside area")
        choices_abyss.add("black", 1, _("Black"), "Black outside area")
        choices_abyss.add("white", 2, _("White"), "White outside area")
        choices_abyss.add("clamp", 3, _("Clamp"), "Clamp to nearest edge pixel")
        choices_abyss.add("loop", 4, _("Loop"), "Repeat image in tiles")
        choices_abyss.add("reflect", 5, _("Reflect"), "Mirror-repeat image in tiles")
        procedure.add_choice_argument(
            "abyss-mode",
            "Abyss _mode",
            "Outside-sample mode",
            choices_abyss,
            "transparent",
            GObject.ParamFlags.READWRITE,
        )
        procedure.add_int_argument(
            "abyss-loop-iterations",
            "_Wrap iterations",
            "Maximum wrap iterations in loop abyss mode",
            1,
            1024,
            4,
            GObject.ParamFlags.READWRITE,
        )
        procedure.add_boolean_argument("transform-active-layer", "_Transform active layer", "Transform pixels in the active layer directly", True, GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("create-analysis-layers", "Add _analysis layers", "Create argument/modulus/grid helper layers", True, GObject.ParamFlags.READWRITE)

        return procedure


Gimp.main(ConformalPlugin.__gtype__, sys.argv)
