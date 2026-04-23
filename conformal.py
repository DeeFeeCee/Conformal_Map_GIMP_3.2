#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

"""Conformal map renderer for GIMP 3.2.

This plug-in ports the original GIMP 2 plug-in to the GIMP 3 API and keeps
rendering work in pure Python bytearrays before committing a full buffer at
once, which is significantly faster than per-pixel GIMP API calls.
"""

import cmath
import math
import sys

import gi

gi.require_version("Gegl", "0.4")
gi.require_version("Gimp", "3.0")
from gi.repository import Gegl
from gi.repository import Gimp
from gi.repository import GLib
from gi.repository import GObject

CONF_VERSION = "1.0-gimp3"
PROC_RENDER = "plug-in-conformal-render"

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
    """Pixel renderer independent from GIMP glue code."""

    QUANT = 4096

    def __init__(self, width, height, code, constraint, xl, xr, yt, yb, grid, checkerboard):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.code = code
        self.constraint = constraint
        self.xl = float(xl)
        self.xr = float(xr)
        self.yt = float(yt)
        self.yb = float(yb)
        self.grid = max(float(grid), 1e-9)
        self.checkerboard = bool(checkerboard)

        self._sx = (self.width - 1.0) / (self.xr - self.xl)
        self._sy = (self.height - 1.0) / (self.yt - self.yb)
        self._two_pi = 2.0 * math.pi
        self._log2 = math.log(2.0)

        self._compiled_code = compile(self.code, "conformal-code", "exec")
        self._compiled_constraint = compile(self.constraint, "conformal-constraint", "exec")

    @staticmethod
    def _clamp_u8(x):
        return max(0, min(255, int(x)))

    def _arg_color(self, arg_norm):
        """Fast HSV wheel (s=1,v=1) for argument coloring."""
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

    def _evaluate_point(self, z):
        env = {"z": z, "w": 0j, "p": True}
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
                mod = (logw.real / self._log2) % 1.0
                sqr = int((w.imag / self.grid) % 2.0) + int((w.real / self.grid) % 2.0)
                sqr = sqr % 2
            except Exception:
                valid = False

        if not valid:
            return False, 0j, 0.0, 0.0, 0
        return True, w, arg_norm, mod, sqr

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
                valid, w, arg_norm, mod, sqr = self._evaluate_point(z)

                if not valid:
                    arg_px = (0, 0, 0, 255)
                    mod_px = (0, 0, 0, 0)
                    grid_px = (0, 0, 0, 0)
                    mapped_px = (0, 0, 0, 0)
                else:
                    arg_px = self._arg_color(arg_norm)
                    mod_px = self._mod_shade(mod)
                    grid_px = self._grid_pixel(sqr)
                    if source_pixels is not None:
                        sx = int(round((w.real - self.xl) * self._sx))
                        sy = int(round((self.yt - w.imag) * self._sy))
                        if 0 <= sx < self.width and 0 <= sy < self.height:
                            sidx = (sy * self.width + sx) * 4
                            mapped_px = tuple(source_pixels[sidx:sidx + 4])
                        else:
                            mapped_px = (0, 0, 0, 0)

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


def _show_dialog(procedure, config):
    gi.require_version("GimpUi", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import GimpUi

    GimpUi.init(PROC_RENDER)
    dialog = GimpUi.ProcedureDialog.new(procedure, config, "Conformal Map")
    dialog.fill(
        [
            "code",
            "constraint",
            "x-left",
            "x-right",
            "y-top",
            "y-bottom",
            "grid-spacing",
            "checkerboard",
            "transform-active-layer",
            "create-analysis-layers",
        ]
    )
    accepted = dialog.run()
    dialog.destroy()
    return accepted


def conformal_run(procedure, run_mode, image, drawables, config, data):
    width = image.get_width()
    height = image.get_height()

    code = config.get_property("code")
    constraint = config.get_property("constraint")
    xl = config.get_property("x-left")
    xr = config.get_property("x-right")
    yt = config.get_property("y-top")
    yb = config.get_property("y-bottom")
    grid = config.get_property("grid-spacing")
    checkerboard = config.get_property("checkerboard")
    transform_layer = config.get_property("transform-active-layer")
    create_analysis = config.get_property("create-analysis-layers")

    if run_mode == Gimp.RunMode.INTERACTIVE:
        if not _show_dialog(procedure, config):
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
        code = config.get_property("code")
        constraint = config.get_property("constraint")
        xl = config.get_property("x-left")
        xr = config.get_property("x-right")
        yt = config.get_property("y-top")
        yb = config.get_property("y-bottom")
        grid = config.get_property("grid-spacing")
        checkerboard = config.get_property("checkerboard")
        transform_layer = config.get_property("transform-active-layer")
        create_analysis = config.get_property("create-analysis-layers")

    renderer = ConformalRenderer(width, height, code, constraint, xl, xr, yt, yb, grid, checkerboard)
    source = drawables[0] if drawables else image.get_active_layer()
    source_pixels = _drawable_pixels_rgba(source, width, height) if (transform_layer and source is not None) else None

    if run_mode == Gimp.RunMode.INTERACTIVE:
        Gimp.progress_init("Rendering conformal map…")

    arg_pixels, mod_pixels, grid_pixels, mapped_pixels = renderer.render(
        source_pixels=source_pixels,
        progress_cb=(lambda value: Gimp.progress_update(value)) if run_mode == Gimp.RunMode.INTERACTIVE else None
    )

    image.undo_group_start()
    try:
        if transform_layer and mapped_pixels is not None:
            mapped_layer = Gimp.Layer.new(
                image,
                "Conformal transform",
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
                "Log. modulus",
                width,
                height,
                Gimp.ImageType.RGBA_IMAGE,
                35.0,
                _layer_mode("LCH_VALUE", "HSV_VALUE", "VALUE", "VALUE_LEGACY"),
            )
            grid_layer = Gimp.Layer.new(
                image,
                "Grid",
                width,
                height,
                Gimp.ImageType.RGBA_IMAGE,
                10.0,
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
        procedure.set_menu_label("_Conformal Map (GIMP 3)")
        procedure.add_menu_path("<Image>/Filters/Render")
        procedure.set_documentation(
            "Colour representation of a conformal map",
            "Renders argument, logarithmic modulus and grid into three layers using the GIMP 3.2 API.",
            name,
        )
        procedure.set_attribution("Michael J Gruber", "Ported for GIMP 3.2", "2026")

        procedure.add_string_argument("code", "Code", "Python expression block assigning w", "w = z", GObject.ParamFlags.READWRITE)
        procedure.add_string_argument(
            "constraint",
            "Constraint",
            "Python expression block assigning boolean p",
            "p = True",
            GObject.ParamFlags.READWRITE,
        )
        procedure.add_double_argument("x-left", "X left", "Left bound of source plane", -1.0e9, 1.0e9, -1.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("x-right", "X right", "Right bound of source plane", -1.0e9, 1.0e9, 1.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("y-top", "Y top", "Top bound of source plane", -1.0e9, 1.0e9, 1.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("y-bottom", "Y bottom", "Bottom bound of source plane", -1.0e9, 1.0e9, -1.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("grid-spacing", "Grid spacing", "Grid spacing in mapped complex plane", 1.0e-12, 1.0e9, 1.0, GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("checkerboard", "Checkerboard", "Use checkerboard instead of line grid", False, GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("transform-active-layer", "Transform active layer", "Render transformed active layer", True, GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("create-analysis-layers", "Create analysis layers", "Create argument/modulus/grid helper layers", True, GObject.ParamFlags.READWRITE)

        return procedure


Gimp.main(ConformalPlugin.__gtype__, sys.argv)
