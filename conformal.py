#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
'''
conformal.py
Conformal map renderer for GIMP 3.2+
Copyright (C) 2006-2011  Michael J. Gruber <conformal@drmicha.warpmail.net>
Updated for GIMP 3.2 by DeeFeeCee

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
import signal
from contextlib import contextmanager
from pathlib import Path
from collections import defaultdict
from gettext import gettext as _

import gi

gi.require_version("Gegl", "0.4")
gi.require_version("Gimp", "3.0")
from gi.repository import Gegl
from gi.repository import Gimp
from gi.repository import GLib
from gi.repository import GObject

CONF_VERSION = "0.3.10"
PROC_RENDER = "plug-in-conformal-render"
_UI_INITIALIZED = False
GRADIENT_ID_MAP = {0: "HSV", 1: "grayscale", 2: "red-blue", 3: "white-black", 4: "custom"}
ABYSS_ID_MAP = {0: "loop", 1: "reflect", 2: "clamp", 3: "transparent", 4: "foreground", 5: "background", 6: "black", 7: "white"}
THIRD_PARTY_PATH = Path(__file__).resolve().parent / "third_party"
VENDORED_SYMPY_PATH = THIRD_PARTY_PATH / "sympy"
VENDORED_SYMPY_PACKAGE = VENDORED_SYMPY_PATH / "sympy"
VENDORED_MPMATH_PATH = THIRD_PARTY_PATH / "mpmath"
VENDORED_MPMATH_PACKAGE = VENDORED_MPMATH_PATH / "mpmath"


@contextmanager
def _time_limit(seconds):
    """Temporarily bound operations that may hang, such as symbolic solving."""
    if seconds is None or seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handler(_signum, _frame):
        raise TimeoutError("operation timed out")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    signal.signal(signal.SIGALRM, _handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _find_vendored_package_path(package_name, preferred_path):
    """Find the sys.path entry that contains a bundled package directory."""
    preferred_path = Path(preferred_path).resolve()
    preferred_module = preferred_path / package_name
    if (preferred_module / "__init__.py").exists():
        return preferred_path, preferred_module

    if not THIRD_PARTY_PATH.exists():
        raise ImportError(f"Bundled {package_name} was not found; missing {THIRD_PARTY_PATH}")

    candidates = []
    direct_module = THIRD_PARTY_PATH / package_name
    if (direct_module / "__init__.py").exists():
        candidates.append((THIRD_PARTY_PATH, direct_module))
    for child in THIRD_PARTY_PATH.iterdir():
        module_path = child / package_name
        if child.is_dir() and (module_path / "__init__.py").exists():
            candidates.append((child, module_path))

    if candidates:
        return candidates[0][0].resolve(), candidates[0][1].resolve()
    raise ImportError(f"Bundled {package_name} was not found under {THIRD_PARTY_PATH}")


def _ensure_vendored_package_path(package_name, package_path, module_path=None):
    """Put a bundled package ahead of any system installation."""
    package_path, module_path = _find_vendored_package_path(package_name, package_path)
    vendored = str(package_path)
    if vendored in sys.path:
        sys.path.remove(vendored)
    if sys.modules.get(package_name) is None:
        sys.path.insert(0, vendored)
        return

    loaded_from = Path(getattr(sys.modules[package_name], "__file__", "")).resolve()
    if module_path not in (loaded_from, *loaded_from.parents):
        raise ImportError(
            f"A non-bundled {package_name} module is already loaded; restart GIMP so "
            f"the bundled {package_name} at {module_path} can be used."
        )
    sys.path.insert(0, vendored)


def _ensure_vendored_sympy_path():
    """Put bundled mpmath and SymPy packages ahead of system installations."""
    _ensure_vendored_package_path("mpmath", VENDORED_MPMATH_PATH, VENDORED_MPMATH_PACKAGE)
    _ensure_vendored_package_path("sympy", VENDORED_SYMPY_PATH, VENDORED_SYMPY_PACKAGE)

def _complex_csc(z):
    return 1 / cmath.sin(z)


def _complex_sec(z):
    return 1 / cmath.cos(z)


def _complex_cot(z):
    return 1 / cmath.tan(z)


def _complex_acsc(z):
    return cmath.asin(1 / z)


def _complex_asec(z):
    return cmath.acos(1 / z)


def _complex_acot(z):
    return cmath.atan(1 / z)


def _math_real(x):
    return x.real if isinstance(x, complex) else x


def _math_imag(x):
    return x.imag if isinstance(x, complex) else 0


def _math_conj(x):
    return x.conjugate() if isinstance(x, complex) else x


def _math_abs(x):
    return math.sqrt((_math_real(x) ** 2) + (_math_imag(x) ** 2))


def _math_abz(x):
    return cmath.sqrt(x ** 2)


def _math_sqr(x):
    return x ** 2


def _real_value(x):
    return x.real if isinstance(x, complex) else x


def _math_floor(x):
    return math.floor(_real_value(x))


def _math_ceil(x):
    return math.ceil(_real_value(x))


def _math_round(x):
    return round(_real_value(x))


def _math_mod(x, y):
    return x % y


def _math_sign(x):
    if isinstance(x, complex):
        magnitude = _math_abs(x)
        return 0 if magnitude == 0 else x / magnitude
    return 0 if x == 0 else (1 if x > 0 else -1)


# expose math functions to user equations in a controlled namespace
MATH_NAMESPACE = {
    "math": math,
    "cmath": cmath,
    "complex": complex,
    "abs": abs,
    "min": min,
    "max": max,
    "pow": pow,
    "range": range,
    "phi": (1.0 + math.sqrt(5.0)) / 2.0,
}
for _name in dir(math):
    if not _name.startswith("_"):
        MATH_NAMESPACE[_name] = getattr(math, _name)
for _name in dir(cmath):
    if not _name.startswith("_"):
        MATH_NAMESPACE[_name] = getattr(cmath, _name)
MATH_NAMESPACE.update({
    "abs": _math_abs,
    "abz": _math_abz,
    "real": _math_real,
    "imag": _math_imag,
    "conj": _math_conj,
    "sqr": _math_sqr,
    "ceil": _math_ceil,
    "floor": _math_floor,
    "round": _math_round,
    "mod": _math_mod,
    "sign": _math_sign,
    "csc": _complex_csc,
    "sec": _complex_sec,
    "cot": _complex_cot,
    "acsc": _complex_acsc,
    "asec": _complex_asec,
    "acot": _complex_acot,
})
for _arc_name in ("sin", "cos", "tan", "csc", "sec", "cot", "sinh", "cosh", "tanh"):
    _inverse_name = f"a{_arc_name}"
    if _inverse_name in MATH_NAMESPACE:
        MATH_NAMESPACE[f"arc{_arc_name}"] = MATH_NAMESPACE[_inverse_name]


class ConformalRenderer:
    # Pixel renderer independent from GIMP glue code.

    QUANT = 4096

    def __init__(
        self,
        width,
        height,
        code,
        constraint,
        domain_xl,
        domain_xr,
        domain_yt,
        domain_yb,
        source_xl,
        source_xr,
        source_yt,
        source_yb,
        grid,
        grid_long_side,
        checkerboard,
        gradient,
        abyss_mode,
        abyss_loop_iterations,
        log_base,
        inverse_code=None,
        transform_precision=0,
        abyss_foreground_color=(0, 0, 0, 255),
        abyss_background_color=(255, 255, 255, 255),
    ):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.code = self._normalize_code(code)
        self.constraint = constraint
        # Store the zoomed output/domain viewport used to compute z.
        self.domain_xl = float(domain_xl)
        self.domain_xr = float(domain_xr)
        self.domain_yt = float(domain_yt)
        self.domain_yb = float(domain_yb)
        # Store the unzoomed source/image viewport used to sample evaluated w.
        self.source_xl = float(source_xl)
        self.source_xr = float(source_xr)
        self.source_yt = float(source_yt)
        self.source_yb = float(source_yb)
        self.grid_lines = max(float(grid), 1.0)
        grid_x_span = abs(self.source_xr - self.source_xl)
        grid_y_span = abs(self.source_yt - self.source_yb)
        grid_side_span = max(grid_x_span, grid_y_span) if grid_long_side else min(grid_x_span, grid_y_span)
        self.grid = max((grid_side_span / 2.0) / self.grid_lines, 1e-9)
        self.checkerboard = bool(checkerboard)
        self.gradient = gradient or "HSV"
        self.abyss_mode = (abyss_mode or "transparent").strip().lower()
        self.abyss_loop_iterations = max(0, abyss_loop_iterations)
        self.log_base = str(log_base or "2")
        self.inverse_code = inverse_code
        self.transform_precision = max(0, min(100, int(transform_precision)))
        self.abyss_foreground_color = tuple(abyss_foreground_color or (0, 0, 0, 255))
        self.abyss_background_color = tuple(abyss_background_color or (255, 255, 255, 255))
        self._validate_gradient_setting()

        # Build separate output/domain scales for pixel-to-z conversion.
        self._domain_sx = (self.width - 1.0) / (self.domain_xr - self.domain_xl)
        self._domain_sy = (self.height - 1.0) / (self.domain_yt - self.domain_yb)
        # Build separate source/image scales for w-to-source-pixel conversion.
        self._source_sx = (self.width - 1.0) / (self.source_xr - self.source_xl)
        self._source_sy = (self.height - 1.0) / (self.source_yt - self.source_yb)
        self._two_pi = 2.0 * math.pi
        if self.log_base == "e":
            self._log = 1.0
        elif self.log_base == "10":
            self._log = math.log(10.0)
        else:
            self._log = math.log(2.0)

        self._compiled_code = compile(self.code, "conformal-code", "exec")
        self._compiled_inverse_code = compile(inverse_code, "conformal-inverse", "exec") if inverse_code else None
        self._compiled_constraint = compile(self.constraint, "conformal-constraint", "exec")

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
        assigned_names = set()
        for node in ast.walk(tree):
            if isinstance(node, blocked):
                raise ValueError(f"Unsupported code construct: {type(node).__name__}")
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param)):
                assigned_names.add(node.id)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assigned_names.add(node.name)
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    assigned_names.add(arg.arg)
                if node.args.vararg is not None:
                    assigned_names.add(node.args.vararg.arg)
                if node.args.kwarg is not None:
                    assigned_names.add(node.args.kwarg.arg)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if not isinstance(node.value, ast.Name) or node.value.id not in {"math", "cmath"}:
                    raise ValueError(f"Unsupported attribute access: {ast.unparse(node)}")
                module = math if node.value.id == "math" else cmath
                if not hasattr(module, node.attr) or node.attr.startswith("_"):
                    raise ValueError(f"Unsupported function: {ast.unparse(node)}")

    @staticmethod
    def _sympy_expression_to_python(expression):
        _ensure_vendored_sympy_path()
        import sympy as sp
        from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application, convert_xor

        transformations = standard_transformations + (implicit_multiplication_application, convert_xor)
        z, w = sp.symbols("z w")
        def _sympy_real(x):
            return sp.re(x)

        def _sympy_imag(x):
            return sp.im(x)

        def _sympy_conj(x):
            return sp.conjugate(x)

        def _sympy_abs(x):
            return sp.sqrt((_sympy_real(x) ** 2) + (_sympy_imag(x) ** 2))

        def _sympy_abz(x):
            return sp.sqrt(x ** 2)

        def _sympy_sqr(x):
            return x ** 2

        def _sympy_max(x, y):
            return (x + y + _sympy_abs(x - y)) / 2

        def _sympy_min(x, y):
            return (x + y - _sympy_abs(x - y)) / 2

        local_dict = {"z": z, "w": w, "i": sp.I, "I": sp.I, "phi": sp.GoldenRatio}
        for _name in ("sin", "cos", "tan", "csc", "sec", "cot", "sinh", "cosh", "tanh", "sqrt", "log", "exp", "sign", "floor", "ceiling"):
            if hasattr(sp, _name):
                local_dict[_name] = getattr(sp, _name)
        local_dict.update({
            "abs": _sympy_abs,
            "Abs": _sympy_abs,
            "abz": _sympy_abz,
            "real": _sympy_real,
            "imag": _sympy_imag,
            "conj": _sympy_conj,
            "sqr": _sympy_sqr,
            "max": _sympy_max,
            "min": _sympy_min,
            "ceil": sp.ceiling,
            "round": sp.floor,
            "mod": sp.Mod,
        })
        for _arc_name in ("sin", "cos", "tan", "csc", "sec", "cot", "sinh", "cosh", "tanh"):
            _inverse_name = f"a{_arc_name}"
            if hasattr(sp, _inverse_name):
                local_dict[f"arc{_arc_name}"] = getattr(sp, _inverse_name)
        expr = parse_expr(expression, local_dict=local_dict, transformations=transformations, evaluate=False)
        unknown_symbols = expr.free_symbols - {z, w}
        if unknown_symbols:
            names = ", ".join(sorted(str(symbol) for symbol in unknown_symbols))
            raise ValueError(f"Unsupported symbol(s): {names}")
        return sp.sstr(expr).replace("I", "1j").replace("GoldenRatio", "phi")

    @staticmethod
    def _normalize_code(code):
        snippet = (code or "").strip()
        if not snippet:
            return "w = z"
        # Map common exponent notation to Python.
        snippet = snippet.replace("^", "**")
        # Interpret standalone "i" as the imaginary unit; coefficients must use explicit multiplication, e.g. 0.2*i.
        snippet = re.sub(r"\bi\b", "(1j)", snippet)
        try:
            ConformalRenderer._validate_code_ast(snippet)
        except SyntaxError:
            expression = ConformalRenderer._strip_w_assignment(snippet)
            if expression is None:
                raise
            snippet = f"w = ({ConformalRenderer._sympy_expression_to_python(expression)})"
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
    def _strip_w_assignment(code):
        snippet = (code or "").strip()
        if not snippet or "\n" in snippet or ";" in snippet:
            return None
        snippet = snippet.replace("^", "**")
        assignment = re.match(r"^w\s*=\s*(.+)$", snippet, flags=re.DOTALL)
        if assignment:
            return assignment.group(1).strip()
        # A single-line expression can omit the leading "w =".
        if re.match(r"^[A-Za-z_]\w*\s*=", snippet):
            return None
        return snippet

    @staticmethod
    def _uses_branch_helper(expression):
        try:
            tree = ast.parse((expression or "").strip().replace("^", "**"), mode="eval")
        except SyntaxError:
            return False
        branch_helpers = {"abs", "Abs", "abz", "real", "imag", "max", "min", "floor", "ceil", "round", "mod", "sign"}
        return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in branch_helpers for node in ast.walk(tree))

    @staticmethod
    def _simple_call_inverse_code(expression):
        try:
            tree = ast.parse((expression or "").strip().replace("^", "**"), mode="eval")
        except SyntaxError:
            return None
        call = tree.body
        if not isinstance(call, ast.Call) or len(call.args) != 1 or call.keywords:
            return None
        if not isinstance(call.func, ast.Name):
            return None
        argument = call.args[0]
        if not isinstance(argument, ast.Name) or argument.id != "z":
            return None
        if call.func.id == "sqr":
            return "z = (w ** 0.5)"
        if call.func.id == "conj":
            return "z = (conj(w))"
        return None

    @staticmethod
    def _simple_power_inverse_code(expression):
        normalized = (expression or "").strip().replace("^", "**")
        try:
            tree = ast.parse(normalized, mode="eval")
        except SyntaxError:
            return None
        body = tree.body
        if not isinstance(body, ast.BinOp) or not isinstance(body.op, ast.Pow):
            return None
        if not isinstance(body.left, ast.Name) or body.left.id != "z":
            return None
        exponent_node = body.right
        if isinstance(exponent_node, ast.UnaryOp) and isinstance(exponent_node.op, (ast.UAdd, ast.USub)) and isinstance(exponent_node.operand, ast.Constant):
            exponent = float(exponent_node.operand.value)
            if isinstance(exponent_node.op, ast.USub):
                exponent = -exponent
        elif isinstance(exponent_node, ast.Constant) and isinstance(exponent_node.value, (int, float)):
            exponent = float(exponent_node.value)
        else:
            return None
        if not math.isfinite(exponent) or abs(exponent) < 1e-12:
            return None
        return f"z = (w ** ({1.0 / exponent!r}))"

    @staticmethod
    def symbolic_inverse_code(code):
        expression = ConformalRenderer._strip_w_assignment(code)
        if expression is None:
            return None
        if ConformalRenderer._uses_branch_helper(expression):
            return None

        simple_call_inverse = ConformalRenderer._simple_call_inverse_code(expression)
        if simple_call_inverse is not None:
            return simple_call_inverse

        simple_inverse = ConformalRenderer._simple_power_inverse_code(expression)
        if simple_inverse is not None:
            return simple_inverse

        _ensure_vendored_sympy_path()
        import sympy as sp

        z, w = sp.symbols("z w")
        expr = sp.sympify(ConformalRenderer._sympy_expression_to_python(expression), locals={"z": z, "w": w})
        with _time_limit(10):
            solutions = sp.solve(sp.Eq(w, expr), z)
        if not solutions:
            return None
        inverse_expr = sp.sstr(solutions[0]).replace("I", "1j")
        return f"z = ({inverse_expr})"

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
        return (shade, shade, shade, 255)

    def _grid_pixel(self, sqr):
        if self.checkerboard:
            v = 255 if sqr else 0
            return (v, v, v, 255)
        return (0, 0, 0, 255 if sqr else 0)

    def _mirror_coord(self, value, size):
        if size <= 1:
            return 0
        period = 2 * size
        m = value % period
        return m if m < size else (period - 1) - m

    def _evaluate_mapping(self, z):
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
        return valid, w

    def _evaluate_point(self, z):
        valid, w = self._evaluate_mapping(z)

        if valid:
            try:
                # Use source coordinates where the center-to-short-edge distance is 1 unit.
                logw = cmath.log(w)
                arg = logw.imag
                if arg < 0.0:
                    arg += self._two_pi
                arg_norm = arg / self._two_pi
                mod = (logw.real / self._log) % 1.0
                sqr = int(math.floor(w.real / self.grid)) + int(math.floor(w.imag / self.grid))
                sqr = sqr % 2
                x_mod = abs((w.real / self.grid) - round(w.real / self.grid))
                y_mod = abs((w.imag / self.grid) - round(w.imag / self.grid))
                grid_line = (x_mod < 0.03) or (y_mod < 0.03)
            except Exception:
                valid = False

        if not valid:
            return False, 0j, 0.0, 0.0, 0, False
        return True, w, arg_norm, mod, sqr, grid_line

    def _sample_mapped_pixel(self, source_pixels, sx, sy, use_abyss=True):
        if sx is None or sy is None:
            return (0, 0, 0, 0)

        if 0 <= sx < self.width:
            tile_x = 0
        elif sx < 0:
            tile_x = ((-sx - 1) // self.width) + 1
        else:
            tile_x = ((sx - self.width) // self.width) + 1

        if 0 <= sy < self.height:
            tile_y = 0
        elif sy < 0:
            tile_y = ((-sy - 1) // self.height) + 1
        else:
            tile_y = ((sy - self.height) // self.height) + 1

        if not use_abyss:
            if tile_x == 0 and tile_y == 0:
                sidx = (sy * self.width + sx) * 4
                return tuple(source_pixels[sidx:sidx + 4])
            return (0, 0, 0, 0)

        if self.abyss_mode == "clamp":
            sx = min(max(0, sx), self.width - 1)
            sy = min(max(0, sy), self.height - 1)
            sidx = (sy * self.width + sx) * 4
            return tuple(source_pixels[sidx:sidx + 4])
        if self.abyss_mode == "loop":
            if (tile_x + tile_y) > self.abyss_loop_iterations:
                return (0, 0, 0, 0)
            sx %= self.width
            sy %= self.height
            sidx = (sy * self.width + sx) * 4
            return tuple(source_pixels[sidx:sidx + 4])
        if self.abyss_mode == "reflect":
            if (tile_x + tile_y) > self.abyss_loop_iterations:
                return (0, 0, 0, 0)
            sx = self._mirror_coord(sx, self.width)
            sy = self._mirror_coord(sy, self.height)
            sidx = (sy * self.width + sx) * 4
            return tuple(source_pixels[sidx:sidx + 4])
        if self.abyss_mode == "foreground":
            return self.abyss_foreground_color
        if self.abyss_mode == "background":
            return self.abyss_background_color
        if self.abyss_mode == "black":
            return (0, 0, 0, 255)
        if self.abyss_mode == "white":
            return (255, 255, 255, 255)
        return (0, 0, 0, 0)

    def _evaluate_inverse_point(self, w):
        if self._compiled_inverse_code is None:
            return False, 0j
        env = {"w": w, "z": 0j}
        env.update(MATH_NAMESPACE)
        try:
            exec(self._compiled_inverse_code, {"__builtins__": {}}, env)
            z = env.get("z", 0j)
            valid = not (math.isnan(z.real) or math.isnan(z.imag) or math.isinf(z.real) or math.isinf(z.imag))
        except Exception:
            return False, 0j
        return valid, z

    def _safe_round_to_int(self, value):
        try:
            if not math.isfinite(value):
                return None
            return int(round(value))
        except Exception:
            return None

    def _source_coord_to_pixel(self, z):
        sx = self._safe_round_to_int((z.real - self.source_xl) * self._source_sx)
        sy = self._safe_round_to_int((self.source_yt - z.imag) * self._source_sy)
        return sx, sy

    def _domain_coord_to_pixel(self, w):
        ox = self._safe_round_to_int((w.real - self.domain_xl) * self._domain_sx)
        oy = self._safe_round_to_int((self.domain_yt - w.imag) * self._domain_sy)
        return ox, oy

    def _forward_output_bounds(self):
        min_x = min_y = math.inf
        max_x = max_y = -math.inf
        for sy in range(self.height):
            z_imag = self.source_yt - (sy / self._source_sy)
            for sx in range(self.width):
                z = (sx / self._source_sx + self.source_xl) + 1j * z_imag
                valid, w = self._evaluate_mapping(z)
                if not valid:
                    continue
                min_x = min(min_x, w.real)
                max_x = max(max_x, w.real)
                min_y = min(min_y, w.imag)
                max_y = max(max_y, w.imag)

        if not all(math.isfinite(value) for value in (min_x, max_x, min_y, max_y)):
            return self.domain_xl, self.domain_xr, self.domain_yt, self.domain_yb

        x_span = max(max_x - min_x, 1e-9)
        y_span = max(max_y - min_y, 1e-9)
        x_pad = x_span / max(self.width - 1, 1) * 0.5
        y_pad = y_span / max(self.height - 1, 1) * 0.5
        return min_x - x_pad, max_x + x_pad, max_y + y_pad, min_y - y_pad

    def _forward_coord_to_pixel(self, w, bounds):
        xl, xr, yt, yb = bounds
        sx = (self.width - 1.0) / max(xr - xl, 1e-9)
        sy = (self.height - 1.0) / max(yt - yb, 1e-9)
        ox = int(round((w.real - xl) * sx))
        oy = int(round((yt - w.imag) * sy))
        return ox, oy

    def _pixel_in_source_bounds(self, sx, sy):
        return sx is not None and sy is not None and 0 <= sx < self.width and 0 <= sy < self.height

    def _render_inverse_mapped(self, source_pixels, progress_cb=None):
        mapped_data = bytearray(self.width * self.height * 4)
        forward_mapped = self._render_forward_mapped(source_pixels) if self.abyss_mode != "transparent" else None
        max_progress = float(self.width * self.height)
        progress = 0.0
        for row in range(self.height):
            base = row * self.width * 4
            imag = self.domain_yt - (row / self._domain_sy)
            for col in range(self.width):
                w = col / self._domain_sx + self.domain_xl + 1j * imag
                valid, z = self._evaluate_inverse_point(w)
                idx = base + (col * 4)
                forward_px = None
                if forward_mapped is not None:
                    forward_px = tuple(forward_mapped[idx:idx + 4])

                if valid:
                    sx, sy = self._source_coord_to_pixel(z)
                    mapped_px = self._sample_mapped_pixel(source_pixels, sx, sy)
                    if not self._pixel_in_source_bounds(sx, sy) and forward_px is not None and forward_px[3] > 0:
                        mapped_px = forward_px
                elif forward_px is not None and forward_px[3] > 0:
                    mapped_px = forward_px
                else:
                    mapped_px = (0, 0, 0, 0)
                mapped_data[idx:idx + 4] = bytes(mapped_px)
                progress += 1.0
            if progress_cb is not None:
                progress_cb((progress / max_progress) * 0.5)

        # Paint the actual transformed image over the abyss fill, so abyss pixels
        # cannot cover source pixels even when wrap iterations are zero.
        forward_mapped = self._render_forward_mapped(
            source_pixels,
            (lambda value: progress_cb(0.5 + value * 0.45)) if progress_cb is not None else None,
        )
        overlay_progress = 0.0
        overlay_max = float(max(1, len(mapped_data) // 4))
        for idx in range(0, len(mapped_data), 4):
            if forward_mapped[idx + 3] > 0:
                mapped_data[idx:idx + 4] = forward_mapped[idx:idx + 4]
            overlay_progress += 1.0
            if progress_cb is not None and idx % (self.width * 64) == 0:
                progress_cb(0.95 + (overlay_progress / overlay_max) * 0.05)
        if progress_cb is not None:
            progress_cb(1.0)
        return bytes(mapped_data)

    def _accumulate_forward_pixel(self, accum, source_pixels, sx, sy, z):
        valid, w, *_rest = self._evaluate_point(z)
        if not valid:
            return None
        ox, oy = self._domain_coord_to_pixel(w)
        if ox is None or oy is None or not (0 <= ox < self.width and 0 <= oy < self.height):
            return None
        sidx = (sy * self.width + sx) * 4
        px = source_pixels[sidx:sidx + 4]
        bucket = accum[(ox, oy)]
        bucket[0] += px[0]; bucket[1] += px[1]; bucket[2] += px[2]; bucket[3] += px[3]; bucket[4] += 1
        return ox, oy

    def _forward_center_outputs(self, accum, source_pixels, progress_cb=None):
        outputs = [[None for _x in range(self.width)] for _y in range(self.height)]
        for sy in range(self.height):
            imag = self.source_yt - (sy / self._source_sy)
            for sx in range(self.width):
                z = (sx / self._source_sx + self.source_xl) + 1j * imag
                outputs[sy][sx] = self._accumulate_forward_pixel(accum, source_pixels, sx, sy, z)
            if progress_cb is not None:
                progress_cb((sy + 1) / float(max(1, self.height)))
        return outputs

    @staticmethod
    def _adjacent_or_overlapping(a, b):
        return a is not None and b is not None and abs(a[0] - b[0]) <= 1 and abs(a[1] - b[1]) <= 1

    def _forward_pixel_is_surrounded(self, outputs, sx, sy):
        center = outputs[sy][sx]
        if center is None:
            return False
        neighbors = []
        if sy > 0:
            neighbors.append(outputs[sy - 1][sx])
        if sx + 1 < self.width:
            neighbors.append(outputs[sy][sx + 1])
        if sy + 1 < self.height:
            neighbors.append(outputs[sy + 1][sx])
        if sx > 0:
            neighbors.append(outputs[sy][sx - 1])
        return bool(neighbors) and all(self._adjacent_or_overlapping(center, neighbor) for neighbor in neighbors)

    def _render_forward_mapped(self, source_pixels, progress_cb=None):
        accum = defaultdict(lambda: [0, 0, 0, 0, 0])
        center_progress_scale = 1.0 if self.transform_precision <= 0 else 0.5
        center_outputs = self._forward_center_outputs(
            accum,
            source_pixels,
            (lambda value: progress_cb(value * center_progress_scale)) if progress_cb is not None else None,
        )
        if self.transform_precision <= 0:
            progress = 0.0
        else:
            samples = self.transform_precision + 1
            offsets = [(i + 0.5) / samples - 0.5 for i in range(samples)]
            offsets = [(ox, oy) for oy in offsets for ox in offsets if abs(ox) > 1e-12 or abs(oy) > 1e-12]
            max_progress = float(max(1, self.width * self.height * max(1, len(offsets))))
            progress = 0.0
            for sy in range(self.height):
                imag_base = self.source_yt - (sy / self._source_sy)
                for sx in range(self.width):
                    if self._forward_pixel_is_surrounded(center_outputs, sx, sy):
                        progress += len(offsets)
                        continue
                    for ox_off, oy_off in offsets:
                        z = ((sx + ox_off) / self._source_sx + self.source_xl) + 1j * (imag_base - (oy_off / self._source_sy))
                        self._accumulate_forward_pixel(accum, source_pixels, sx, sy, z)
                        progress += 1.0
                    if progress_cb is not None and sx % 16 == 0:
                        progress_cb(0.5 + min(progress / max_progress, 1.0) * 0.5)

        mapped_data = bytearray(self.width * self.height * 4)
        for (ox, oy), bucket in accum.items():
            count = max(1, bucket[4])
            idx = (oy * self.width + ox) * 4
            mapped_data[idx:idx + 4] = bytes((bucket[0] // count, bucket[1] // count, bucket[2] // count, bucket[3] // count))
        if progress_cb is not None:
            progress_cb(1.0)
        return bytes(mapped_data)

    def render_mapped(self, source_pixels, progress_cb=None):
        if self._compiled_inverse_code is not None:
            return self._render_inverse_mapped(source_pixels, progress_cb)
        return self._render_forward_mapped(source_pixels, progress_cb)

    def _analysis_value_at_output_point(self, output_point):
        if self._compiled_inverse_code is not None:
            valid, z = self._evaluate_inverse_point(output_point)
            return valid, z
        valid, w, *_rest = self._evaluate_point(output_point)
        return valid, w

    def _analysis_components(self, value):
        try:
            log_value = cmath.log(value)
            arg = log_value.imag
            if arg < 0.0:
                arg += self._two_pi
            arg_norm = arg / self._two_pi
            mod = (log_value.real / self._log) % 1.0
            sqr = int(math.floor(value.real / self.grid)) + int(math.floor(value.imag / self.grid))
            sqr = sqr % 2
            x_mod = abs((value.real / self.grid) - round(value.real / self.grid))
            y_mod = abs((value.imag / self.grid) - round(value.imag / self.grid))
            grid_line = (x_mod < 0.03) or (y_mod < 0.03)
        except Exception:
            return False, 0.0, 0.0, 0, False
        return True, arg_norm, mod, sqr, grid_line

    def render(self, source_pixels=None, progress_cb=None):
        arg_data = bytearray(self.width * self.height * 4)
        mod_data = bytearray(self.width * self.height * 4)
        grid_data = bytearray(self.width * self.height * 4)
        mapped_data = bytearray(self.width * self.height * 4) if source_pixels is not None else None

        max_progress = float(self.width * self.height)
        progress = 0.0

        for row in range(self.height):
            base = row * self.width * 4
            # Convert the output row through the zoomed domain viewport.
            imag = self.domain_yt - (row / self._domain_sy)
            for col in range(self.width):
                # Convert the output column through the zoomed domain viewport.
                z = col / self._domain_sx + self.domain_xl + 1j * imag
                valid, w = self._analysis_value_at_output_point(z)
                if valid:
                    valid, arg_norm, mod, sqr, grid_line = self._analysis_components(w)

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
                        # Convert evaluated w through the unzoomed source/image viewport.
                        sx, sy = self._source_coord_to_pixel(w)
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
    dialog.add_button("Reset to _Defaults", RESPONSE_RESET_DEFAULTS)
    dialog.add_button("Reset _Last Used", RESPONSE_RESET_LAST)
    dialog.add_button("_OK", Gtk.ResponseType.OK)
    dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
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
    code_view.set_tooltip_text("Use explicit multiplication, for example 2*z. Use Python syntax for iterative functions.")
    code_buffer = code_view.get_buffer()
    code_buffer.set_text(config.get_property("code"))
    sw = Gtk.ScrolledWindow()
    sw.set_min_content_height(120)
    sw.add(code_view)
    grid.attach(sw, 1, row, 3, 1)

    syntax_help = Gtk.Expander(label="Syntax help")
    syntax_help.set_expanded(False)
    syntax_label = Gtk.Label(
        label=(
            "Multiplication must be explicit: type 2*z, not 2z.\n"
            "Use Python operators such as z**2 or z^2 for powers.\n"
            "Use 0.2*i for imaginary coefficients, not 0.2i.\n"
            "Iterative functions can use helper code, for example:\n"
            "w = z\n"
            "for _ in range(8):\n"
            "    w = w*w + z"
        )
    )
    syntax_label.set_xalign(0.0)
    syntax_label.set_yalign(0.0)
    syntax_label.set_line_wrap(True)
    syntax_help.add(syntax_label)
    grid.attach(syntax_help, 4, row, 1, 1)
    row += 1

    scale_widgets = {}
    scale_labels = {}

    def _make_scale(name, label_text, lower, upper, value, step, page, digits=5, tooltip=None):
        nonlocal row
        label = Gtk.Label(label=label_text)
        label.set_xalign(0.0)
        if tooltip:
            label.set_tooltip_text(tooltip)
        grid.attach(label, 0, row, 1, 1)
        adj = Gtk.Adjustment(value=float(value), lower=float(lower), upper=float(upper), step_increment=float(step), page_increment=float(page), page_size=0.0)
        scale = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, adj)
        scale.set_digits(digits)
        scale.set_draw_value(True)
        scale.set_hexpand(True)
        if tooltip:
            scale.set_tooltip_text(tooltip)
        grid.attach(scale, 1, row, 3, 1)
        spin = Gtk.SpinButton.new(adj, climb_rate=0.5, digits=digits)
        spin.set_numeric(True)
        spin.set_width_chars(8)
        if tooltip:
            spin.set_tooltip_text(tooltip)
        grid.attach(spin, 4, row, 1, 1)
        scale_widgets[name] = (scale, spin)
        scale_labels[name] = label
        row += 1

    _make_scale(
        "transform-precision",
        "Forward precision",
        0,
        100,
        config.get_property("transform-precision"),
        1,
        10,
        digits=0,
        tooltip="Higher values add subpixel samples and increase transform work by roughly n². Does not apply to functions of form w = f(z) which use elementary functions.",
    )

    coord_expander = Gtk.Expander(label="Coordinate/Scale settings")
    coord_expander.set_expanded(bool(config.get_property("coordinate-settings-expanded")))
    coord_grid = Gtk.Grid(column_spacing=8, row_spacing=8, margin=8)
    coord_expander.add(coord_grid)
    grid.attach(coord_expander, 0, row, 5, 1)
    row += 1
    coord_row = 0

    def _make_coord_scale(name, label_text, lower, upper, value, step, page, digits=5, tooltip=None):
        nonlocal coord_row
        label = Gtk.Label(label=label_text)
        label.set_xalign(0.0)
        if tooltip:
            label.set_tooltip_text(tooltip)
        coord_grid.attach(label, 0, coord_row, 1, 1)
        adj = Gtk.Adjustment(value=float(value), lower=float(lower), upper=float(upper), step_increment=float(step), page_increment=float(page), page_size=0.0)
        scale = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, adj)
        scale.set_digits(digits)
        scale.set_draw_value(True)
        scale.set_hexpand(True)
        if tooltip:
            scale.set_tooltip_text(tooltip)
        coord_grid.attach(scale, 1, coord_row, 3, 1)
        spin = Gtk.SpinButton.new(adj, climb_rate=0.5, digits=digits)
        spin.set_numeric(True)
        spin.set_width_chars(8)
        if tooltip:
            spin.set_tooltip_text(tooltip)
        coord_grid.attach(spin, 4, coord_row, 1, 1)
        scale_widgets[name] = (scale, spin)
        scale_labels[name] = label
        coord_row += 1

    coord_combo = Gtk.ComboBoxText()
    coord_combo.append("relative", "Relative coordinates")
    coord_combo.append("pixels", "Pixels")
    coord_combo.set_active_id(str(config.get_property("coord-system")) if hasattr(config, "get_property") else "relative")
    if coord_combo.get_active_id() is None:
        coord_combo.set_active_id("relative")
    coord_label = Gtk.Label(label="Coordinate system")
    coord_label.set_xalign(0.0)
    coord_label.set_tooltip_text("Relative coordinates use the selected image side and scale below.")
    coord_grid.attach(coord_label, 0, coord_row, 1, 1)
    coord_combo.set_tooltip_text("Select center coordinate units.")
    coord_grid.attach(coord_combo, 1, coord_row, 1, 1)

    scale_basis_check = Gtk.CheckButton(label="Scale uses long side")
    scale_basis_check.set_active(bool(config.get_property("scale-long-side")))
    scale_basis_check.set_tooltip_text("When enabled, Scale applies to the long image side instead of the short side.")
    coord_grid.attach(scale_basis_check, 2, coord_row, 3, 1)
    coord_row += 1

    _make_coord_scale("scale", "Input scale", 1.0e-5, 1.0e3, config.get_property("scale"), 0.01, 0.1, digits=5, tooltip="Coordinate assigned to opposite sides of input image (half of short/long side). Applied before zoom.")
    _make_coord_scale("center-x", "Input center X", -1.0e3, 1.0e3, config.get_property("center-x"), 0.01, 0.1, digits=5, tooltip="Input center X coordinate used for sampling the source image.")
    _make_coord_scale("center-y", "Input center Y", -1.0e3, 1.0e3, config.get_property("center-y"), 0.01, 0.1, digits=5, tooltip="Input center Y coordinate used for sampling the source image.")
    _make_coord_scale("zoom", "Output zoom", 1.0e-5, 1.0e3, config.get_property("zoom"), 0.01, 0.1, digits=5, tooltip="Zoom factor: Higher values zoom in. Based on input scale.")
    _make_coord_scale("output-center-x", "Output center X", -1.0e3, 1.0e3, config.get_property("output-center-x"), 0.01, 0.1, digits=5, tooltip="Output center X coordinate for the rendered image viewport.")
    _make_coord_scale("output-center-y", "Output center Y", -1.0e3, 1.0e3, config.get_property("output-center-y"), 0.01, 0.1, digits=5, tooltip="Output center Y coordinate for the rendered image viewport.")
    def _convert_units(_widget):
        old = getattr(_convert_units, "last", "relative")
        new = coord_combo.get_active_id() or "relative"
        if old != new:
            cx = scale_widgets["center-x"][0].get_value()
            cy = scale_widgets["center-y"][0].get_value()
            ocx = scale_widgets["output-center-x"][0].get_value()
            ocy = scale_widgets["output-center-y"][0].get_value()
            selected_side_px = max(width, height) if scale_basis_check.get_active() else min(width, height)
            selected_half_px = selected_side_px / 2.0
            safe_scale = max(abs(scale_widgets["scale"][0].get_value()), 1e-9)
            img_cx = (width - 1) / 2.0
            img_cy = (height - 1) / 2.0
            if old == "relative" and new == "pixels":
                scale_widgets["center-x"][0].set_value(img_cx + (cx / safe_scale) * selected_half_px)
                scale_widgets["center-y"][0].set_value(img_cy - (cy / safe_scale) * selected_half_px)
                scale_widgets["output-center-x"][0].set_value(img_cx + (ocx / safe_scale) * selected_half_px)
                scale_widgets["output-center-y"][0].set_value(img_cy - (ocy / safe_scale) * selected_half_px)
            elif old == "pixels" and new == "relative":
                scale_widgets["center-x"][0].set_value(((cx - img_cx) / max(selected_half_px, 1e-9)) * safe_scale)
                scale_widgets["center-y"][0].set_value(((img_cy - cy) / max(selected_half_px, 1e-9)) * safe_scale)
                scale_widgets["output-center-x"][0].set_value(((ocx - img_cx) / max(selected_half_px, 1e-9)) * safe_scale)
                scale_widgets["output-center-y"][0].set_value(((img_cy - ocy) / max(selected_half_px, 1e-9)) * safe_scale)

        if new == "pixels":
            lower, upper, step, page, digits = -1.0e4, 1.0e4, 0.5, 10.0, 4
        else:
            lower, upper, step, page, digits = -1.0e3, 1.0e3, 0.01, 0.1, 5
        for key in ("center-x", "center-y", "output-center-x", "output-center-y"):
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

    abyss_combo = Gtk.ComboBoxText()
    for key, label in [
        ("loop", "Loop tiling"),
        ("reflect", "Reflect tiling"),
        ("clamp", "Clamp"),
        ("transparent", "Transparent"),
        ("foreground", "Foreground color"),
        ("background", "Background color"),
        ("black", "Black"),
        ("white", "White"),
    ]:
        abyss_combo.append(key, label)
    abyss_value = config.get_property("abyss-mode")
    abyss_value = ABYSS_ID_MAP.get(abyss_value, "transparent") if isinstance(abyss_value, int) else str(abyss_value)
    abyss_combo.set_active_id(abyss_value)
    abyss_label = Gtk.Label(label="Abyss mode", xalign=0.0)
    abyss_spin = Gtk.SpinButton()
    abyss_spin.set_adjustment(Gtk.Adjustment(value=config.get_property("abyss-loop-iterations"), lower=0.0, upper=1024.0, step_increment=1.0, page_increment=10.0, page_size=0.0))
    abyss_spin.set_digits(0)
    abyss_spin.set_numeric(True)

    transform_check = Gtk.CheckButton(label="Transform active layer")
    transform_check.set_active(bool(config.get_property("transform-active-layer")))
    grid.attach(transform_check, 0, row, 1, 1)
    row += 1

    abyss_label.set_tooltip_text("How samples outside the image bounds are handled.")
    grid.attach(abyss_label, 0, row, 1, 1)
    abyss_combo.set_tooltip_text("Choose outside-image sampling behavior.")
    grid.attach(abyss_combo, 1, row, 1, 1)

    tile_label = Gtk.Label(label="Tile iterations", xalign=0.0)
    tile_label.set_tooltip_text("Maximum number of adjacent out-of-bounds wrap tiles to sample.")
    grid.attach(tile_label, 2, row, 1, 1)
    abyss_spin.set_tooltip_text("Effective for Loop and Reflect modes.")
    grid.attach(abyss_spin, 3, row, 1, 1)
    row += 1

    analysis_check = Gtk.CheckButton(label="Add analysis layers")
    analysis_check.set_active(bool(config.get_property("create-analysis-layers")))
    group_check = Gtk.CheckButton(label="Group analysis layers (has visual bug)")
    group_check.set_active(bool(config.get_property("analysis-group")))
    checker_check = Gtk.CheckButton(label="Checkerboard (grid if disabled)")
    checker_check.set_active(bool(config.get_property("checkerboard")))
    grid.attach(analysis_check, 0, row, 1, 1)
    row += 1
    grid.attach(checker_check, 0, row, 2, 1)
    grid.attach(group_check, 2, row, 2, 1)
    row += 1

    gradient_combo = Gtk.ComboBoxText()
    for key, label in [("HSV", "HSV"), ("red-blue", "Red-Blue"), ("grayscale", "Grayscale"), ("white-black", "White-Black"), ("custom", "Custom")]:
        gradient_combo.append(key, label)
    gradient_value = config.get_property("gradient-preset")
    gradient_value = GRADIENT_ID_MAP.get(gradient_value, "HSV") if isinstance(gradient_value, int) else str(gradient_value)
    gradient_combo.set_active_id(gradient_value)
    palette_label = Gtk.Label(label="Palette", xalign=0.0)
    palette_label.set_tooltip_text("Color mapping for the argument layer.")
    grid.attach(palette_label, 0, row, 1, 1)
    gradient_combo.set_tooltip_text("Select a built-in palette or choose Custom.")
    grid.attach(gradient_combo, 1, row, 1, 1)

    gradient_entry = Gtk.Entry()
    gradient_entry.set_text(config.get_property("gradient-custom") or "")
    custom_palette_label = Gtk.Label(label="Custom palette", xalign=0.0)
    custom_palette_label.set_tooltip_text("Comma-separated #RRGGBB values for custom colors. Used when Custom palette is selected.")

    def _pick_color(_button):
        chooser = Gtk.ColorChooserDialog(title="Pick color", transient_for=dialog, modal=True)
        chooser.set_use_alpha(False)
        if chooser.run() == Gtk.ResponseType.OK:
            rgba = chooser.get_rgba()
            hex_value = "#{:02x}{:02x}{:02x}".format(round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255))
            current = gradient_entry.get_text().strip()
            gradient_entry.set_text(f"{current},{hex_value}" if current else hex_value)
        chooser.destroy()

    grid.attach(custom_palette_label, 2, row, 1, 1)
    gradient_entry.set_tooltip_text("Comma-separated #RRGGBB values for custom colors. Example: #ff0000,#00ff00,#0000ff")
    grid.attach(gradient_entry, 3, row, 1, 1)
    pick_btn = Gtk.Button(label="Pick color…")
    pick_btn.connect("clicked", _pick_color)
    grid.attach(pick_btn, 4, row, 1, 1)
    row += 1

    log_combo = Gtk.ComboBoxText()
    log_combo.append("2", "2")
    log_combo.append("e", "e")
    log_combo.append("10", "10")
    log_combo.set_active_id(str(config.get_property("log-base")))
    if log_combo.get_active_id() is None:
        log_combo.set_active_id("2")
    log_label = Gtk.Label(label="Log modulus base", xalign=0.0)
    log_label.set_tooltip_text("Base used for logarithm modulus shading.")
    grid.attach(log_label, 0, row, 1, 1)
    log_combo.set_tooltip_text("Select modulus base.")
    grid.attach(log_combo, 1, row, 1, 1)
    row += 1

    grid_basis_check = Gtk.CheckButton(label="Grid density uses long side")
    grid_basis_check.set_active(bool(config.get_property("grid-long-side")))
    grid_basis_check.set_tooltip_text("When enabled, grid density is measured from the center to the long image side instead of the short side.")
    grid.attach(grid_basis_check, 0, row, 3, 1)
    row += 1

    _make_scale(
        "grid-density",
        "Grid density (from center to side)",
        1.0,
        100.0,
        config.get_property("grid-density"),
        1.0,
        10.0,
        digits=2,
        tooltip="Number of grid lines from the center to the selected image side.",
    )
    row += 1

    def _sync():
        analysis_enabled = analysis_check.get_active()
        custom_palette = gradient_combo.get_active_id() == "custom"
        abyss_label.set_sensitive(transform_check.get_active())
        abyss_combo.set_sensitive(transform_check.get_active())
        tile_label.set_sensitive(transform_check.get_active())
        abyss_spin.set_sensitive(transform_check.get_active())
        group_check.set_sensitive(analysis_enabled)
        checker_check.set_sensitive(analysis_enabled)
        palette_label.set_sensitive(analysis_enabled)
        gradient_combo.set_sensitive(analysis_enabled)
        custom_palette_label.set_sensitive(analysis_enabled and custom_palette)
        gradient_entry.set_sensitive(analysis_enabled and custom_palette)
        pick_btn.set_sensitive(analysis_enabled and custom_palette)
        grid_enabled = analysis_enabled
        grid_basis_check.set_sensitive(grid_enabled)
        scale_labels["grid-density"].set_sensitive(grid_enabled)
        scale_widgets["grid-density"][0].set_sensitive(grid_enabled)
        scale_widgets["grid-density"][1].set_sensitive(grid_enabled)
        log_label.set_sensitive(analysis_enabled)
        log_combo.set_sensitive(analysis_enabled)

    gradient_combo.connect("changed", lambda *_a: _sync())
    transform_check.connect("toggled", lambda *_a: _sync())
    abyss_combo.connect("changed", lambda *_a: _sync())
    analysis_check.connect("toggled", lambda *_a: _sync())
    _sync()

    def _reset_defaults():
        code_buffer.set_text("w = z")
        scale_widgets["center-x"][0].set_value(0.0)
        scale_widgets["center-y"][0].set_value(0.0)
        scale_widgets["output-center-x"][0].set_value(0.0)
        scale_widgets["output-center-y"][0].set_value(0.0)
        scale_widgets["zoom"][0].set_value(1.0)
        scale_widgets["scale"][0].set_value(1.0)
        scale_basis_check.set_active(False)
        grid_basis_check.set_active(False)
        scale_widgets["grid-density"][0].set_value(8.0)
        scale_widgets["transform-precision"][0].set_value(0.0)
        coord_combo.set_active_id("relative")
        gradient_combo.set_active_id("HSV")
        gradient_entry.set_text("#ff0000,#ffff00,#00ff00,#00ffff,#0000ff")
        abyss_combo.set_active_id("transparent")
        abyss_spin.set_value(2)
        log_combo.set_active_id("2")
        transform_check.set_active(True)
        analysis_check.set_active(True)
        checker_check.set_active(False)
        group_check.set_active(False)
        coord_expander.set_expanded(False)
        _sync()

    last_used = {
        "code": config.get_property("code"),
        "center-x": config.get_property("center-x"),
        "center-y": config.get_property("center-y"),
        "output-center-x": config.get_property("output-center-x"),
        "output-center-y": config.get_property("output-center-y"),
        "zoom": config.get_property("zoom"),
        "scale": config.get_property("scale"),
        "scale-long-side": scale_basis_check.get_active(),
        "grid-density": config.get_property("grid-density"),
        "transform-precision": config.get_property("transform-precision"),
        "grid-long-side": grid_basis_check.get_active(),
        "coord-system": coord_combo.get_active_id() or "relative",
        "gradient-preset": gradient_combo.get_active_id() or "HSV",
        "gradient-custom": gradient_entry.get_text(),
        "abyss-mode": abyss_combo.get_active_id() or "transparent",
        "abyss-loop-iterations": abyss_spin.get_value(),
        "log-base": log_combo.get_active_id() or "2",
        "transform-active-layer": transform_check.get_active(),
        "create-analysis-layers": analysis_check.get_active(),
        "checkerboard": checker_check.get_active(),
        "analysis-group": group_check.get_active(),
    }

    def _reset_last():
        code_buffer.set_text(last_used["code"])
        scale_widgets["center-x"][0].set_value(float(last_used["center-x"]))
        scale_widgets["center-y"][0].set_value(float(last_used["center-y"]))
        scale_widgets["output-center-x"][0].set_value(float(last_used["output-center-x"]))
        scale_widgets["output-center-y"][0].set_value(float(last_used["output-center-y"]))
        scale_widgets["zoom"][0].set_value(float(last_used["zoom"]))
        scale_widgets["scale"][0].set_value(float(last_used["scale"]))
        scale_basis_check.set_active(bool(last_used["scale-long-side"]))
        scale_widgets["grid-density"][0].set_value(float(last_used["grid-density"]))
        scale_widgets["transform-precision"][0].set_value(float(last_used["transform-precision"]))
        grid_basis_check.set_active(bool(last_used["grid-long-side"]))
        coord_combo.set_active_id(last_used["coord-system"])
        gradient_combo.set_active_id(last_used["gradient-preset"])
        gradient_entry.set_text(last_used["gradient-custom"])
        abyss_combo.set_active_id(last_used["abyss-mode"])
        abyss_spin.set_value(last_used["abyss-loop-iterations"])
        log_combo.set_active_id(last_used["log-base"])
        transform_check.set_active(bool(last_used["transform-active-layer"]))
        analysis_check.set_active(bool(last_used["create-analysis-layers"]))
        checker_check.set_active(bool(last_used["checkerboard"]))
        group_check.set_active(bool(last_used["analysis-group"]))
        coord_expander.set_expanded(False)
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
        config.set_property("scale", float(scale_widgets["scale"][0].get_value()))
        config.set_property("scale-long-side", bool(scale_basis_check.get_active()))
        config.set_property("grid-long-side", bool(grid_basis_check.get_active()))
        config.set_property("gradient-preset", gradient_combo.get_active_id() or "HSV")
        config.set_property("gradient-custom", gradient_entry.get_text().strip())
        config.set_property("abyss-mode", abyss_combo.get_active_id() or "transparent")
        config.set_property("abyss-loop-iterations", abyss_spin.get_value())
        config.set_property("log-base", log_combo.get_active_id() or "2")
        config.set_property("transform-active-layer", bool(transform_check.get_active()))
        config.set_property("create-analysis-layers", bool(analysis_check.get_active()))
        config.set_property("checkerboard", bool(checker_check.get_active()))
        config.set_property("analysis-group", bool(group_check.get_active()))
        config.set_property("coordinate-settings-expanded", bool(coord_expander.get_expanded()))
    dialog.destroy()
    return accepted


def conformal_run(procedure, run_mode, image, drawables, config, data):
    width = image.get_width()
    height = image.get_height()

    code = config.get_property("code")
    constraint = "p = True"
    center_x = float(config.get_property("center-x"))
    center_y = float(config.get_property("center-y"))
    output_center_x = float(config.get_property("output-center-x"))
    output_center_y = float(config.get_property("output-center-y"))
    zoom = float(config.get_property("zoom"))
    scale_value = float(config.get_property("scale"))
    scale_long_side = bool(config.get_property("scale-long-side"))
    coord_system = str(config.get_property("coord-system") or "relative")
    grid = config.get_property("grid-density")
    grid_long_side = bool(config.get_property("grid-long-side"))
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
    log_base = str(config.get_property("log-base") or "2")
    transform_layer = config.get_property("transform-active-layer")
    create_analysis = config.get_property("create-analysis-layers")
    group_analysis = config.get_property("analysis-group")
    transform_precision = int(config.get_property("transform-precision"))

    if run_mode == Gimp.RunMode.INTERACTIVE:
        if not _show_dialog(procedure, config, width, height):
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
        code = config.get_property("code")
        constraint = "p = True"
        center_x = float(config.get_property("center-x"))
        center_y = float(config.get_property("center-y"))
        output_center_x = float(config.get_property("output-center-x"))
        output_center_y = float(config.get_property("output-center-y"))
        zoom = float(config.get_property("zoom"))
        scale_value = float(config.get_property("scale"))
        scale_long_side = bool(config.get_property("scale-long-side"))
        coord_system = str(config.get_property("coord-system") or "relative")
        grid = config.get_property("grid-density")
        grid_long_side = bool(config.get_property("grid-long-side"))
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
        log_base = str(config.get_property("log-base") or "2")
        transform_layer = config.get_property("transform-active-layer")
        create_analysis = config.get_property("create-analysis-layers")
        group_analysis = config.get_property("analysis-group")
        transform_precision = int(config.get_property("transform-precision"))

    short_side = float(max(1, min(width, height)))
    long_side = float(max(1, max(width, height)))
    selected_side = long_side if scale_long_side else short_side
    selected_half_px = selected_side / 2.0
    safe_scale = max(abs(scale_value), 1e-9)
    img_cx = (width - 1) / 2.0
    img_cy = (height - 1) / 2.0

    if not transform_layer and not create_analysis:
        Gimp.message("Conformal Mapping: select Transform active layer and/or Add analysis layers to run.")
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())

    source = drawables[0] if drawables else image.get_active_layer()
    if transform_layer and source is None:
        Gimp.message("Conformal Mapping: no active layer is available to transform.")
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())

    if coord_system == "pixels":
        center_x = ((center_x - img_cx) / max(selected_half_px, 1e-9)) * safe_scale
        center_y = ((img_cy - center_y) / max(selected_half_px, 1e-9)) * safe_scale
        output_center_x = ((output_center_x - img_cx) / max(selected_half_px, 1e-9)) * safe_scale
        output_center_y = ((img_cy - output_center_y) / max(selected_half_px, 1e-9)) * safe_scale

    safe_zoom = max(abs(zoom), 1e-9)
    domain_selected_half_span = safe_scale / safe_zoom
    domain_x_half_span = domain_selected_half_span * (width / selected_side)
    domain_y_half_span = domain_selected_half_span * (height / selected_side)

    # Build the unzoomed source/image viewport for converting w to source pixels.
    source_selected_half_span = safe_scale
    source_x_half_span = source_selected_half_span * (width / selected_side)
    source_y_half_span = source_selected_half_span * (height / selected_side)
    source_xl = center_x - source_x_half_span
    source_xr = center_x + source_x_half_span
    source_yt = center_y + source_y_half_span
    source_yb = center_y - source_y_half_span

    # Build the zoomed output/domain viewport for converting output pixels to z.
    domain_xl = output_center_x - domain_x_half_span
    domain_xr = output_center_x + domain_x_half_span
    domain_yt = output_center_y + domain_y_half_span
    domain_yb = output_center_y - domain_y_half_span

    if run_mode == Gimp.RunMode.INTERACTIVE:
        Gimp.progress_init("Evaluating inverse function…")
        Gimp.progress_update(0.0)

    try:
        normalized_code = ConformalRenderer._normalize_code(code)
    except Exception as exc:
        Gimp.message(f"Conformal Mapping syntax error: {exc}")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))

    inverse_code = None
    symbolic_expression = ConformalRenderer._strip_w_assignment(code) is not None
    inverse_warning = None
    if symbolic_expression:
        try:
            inverse_code = ConformalRenderer.symbolic_inverse_code(code)
        except Exception as exc:
            inverse_warning = f"Conformal Mapping inverse warning: {exc}; using forward splatting instead."
        if inverse_code is None and inverse_warning is None:
            inverse_warning = "Conformal Mapping inverse warning: SymPy could not solve this expression; using forward splatting instead."
    if inverse_warning is not None:
        Gimp.message(inverse_warning)

    if run_mode == Gimp.RunMode.INTERACTIVE:
        Gimp.progress_update(0.10)

    print(f"Conformal Mapping interpreted function: {normalized_code}", flush=True)
    print(f"Conformal Mapping interpreted inverse: {inverse_code or 'none'}", flush=True)

    abyss_foreground_color = _gegl_to_u8(Gimp.context_get_foreground())
    abyss_background_color = _gegl_to_u8(Gimp.context_get_background())

    try:
        renderer_full = ConformalRenderer(
            width,
            height,
            normalized_code,
            constraint,
            domain_xl,
            domain_xr,
            domain_yt,
            domain_yb,
            source_xl,
            source_xr,
            source_yt,
            source_yb,
            grid,
            grid_long_side,
            checkerboard,
            gradient,
            abyss_mode,
            abyss_loop_iterations,
            log_base,
            inverse_code,
            transform_precision,
            abyss_foreground_color,
            abyss_background_color,
        )
        if run_mode == Gimp.RunMode.INTERACTIVE:
            Gimp.progress_update(0.15)
    except Exception as exc:
        Gimp.message(f"Conformal Mapping input error: {exc}")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))
    try:
        source_pixels = _drawable_pixels_rgba(source, width, height) if transform_layer else None
        if run_mode == Gimp.RunMode.INTERACTIVE:
            Gimp.progress_update(0.20)
    except Exception as exc:
        Gimp.message(f"Conformal Mapping source layer error: {exc}")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))

    try:
        if transform_layer and create_analysis:
            if run_mode == Gimp.RunMode.INTERACTIVE:
                Gimp.progress_init("Rendering transformed image…")
            mapped_pixels = renderer_full.render_mapped(
                source_pixels,
                progress_cb=(lambda value: Gimp.progress_update(value * 0.5)) if (run_mode == Gimp.RunMode.INTERACTIVE and source_pixels is not None) else None,
            ) if source_pixels is not None else None
            if run_mode == Gimp.RunMode.INTERACTIVE:
                Gimp.progress_init("Generating analysis layers…")
            arg_pixels, mod_pixels, grid_pixels, _ = renderer_full.render(
                source_pixels=None,
                progress_cb=(lambda value: Gimp.progress_update(0.5 + value * 0.5)) if run_mode == Gimp.RunMode.INTERACTIVE else None,
            )
        elif transform_layer:
            if run_mode == Gimp.RunMode.INTERACTIVE:
                Gimp.progress_init("Rendering transformed image…")
            mapped_pixels = renderer_full.render_mapped(
                source_pixels,
                progress_cb=(lambda value: Gimp.progress_update(value)) if (run_mode == Gimp.RunMode.INTERACTIVE and source_pixels is not None) else None,
            ) if source_pixels is not None else None
            arg_pixels = mod_pixels = grid_pixels = None
        elif create_analysis:
            mapped_pixels = None
            if run_mode == Gimp.RunMode.INTERACTIVE:
                Gimp.progress_init("Generating analysis layers…")
            arg_pixels, mod_pixels, grid_pixels, _ = renderer_full.render(
                source_pixels=None,
                progress_cb=(lambda value: Gimp.progress_update(value)) if run_mode == Gimp.RunMode.INTERACTIVE else None,
            )
        else:
            mapped_pixels = None
            arg_pixels = mod_pixels = grid_pixels = None
    except Exception as exc:
        Gimp.message(f"Conformal Mapping render error: {exc}")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))

    image.undo_group_start()
    # Gets active layer's name & appends space if name == Layer (default layer name in English)
    try:
        source_name = source.get_name() if source is not None else ""
    except Exception as exc:
        image.undo_group_end()
        Gimp.message(f"Conformal Mapping source layer error: {exc}")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))
    layer_name = "" if source_name in ("", "Layer") else source_name + " "
    try:
        if transform_layer and mapped_pixels is not None:
            mapped_layer = Gimp.Layer.new(
                image,
                layer_name + " Conformal Transform",
                width,
                height,
                Gimp.ImageType.RGBA_IMAGE,
                100.0,
                _layer_mode("NORMAL", "NORMAL_LEGACY"),
            )
            image.insert_layer(mapped_layer, None, -1)
            _push_bytes_to_layer(mapped_layer, width, height, mapped_pixels)

        if create_analysis:
            if group_analysis:
                analysis_group = Gimp.GroupLayer.new(image, layer_name + "Analysis")
                image.insert_layer(analysis_group, None, -1)
            else:
                analysis_group = None
            arg_layer = Gimp.Layer.new(
                image,
                layer_name + "Argument",
                width,
                height,
                Gimp.ImageType.RGBA_IMAGE,
                100.0,
                _layer_mode("NORMAL", "NORMAL_LEGACY"),
            )
            mod_layer = Gimp.Layer.new(
                image,
                layer_name + "Log Modulus",
                width,
                height,
                Gimp.ImageType.RGBA_IMAGE,
                20,
                _layer_mode("DARKEN_ONLY", "DARKEN_ONLY_LEGACY", "DARKEN", "DARKEN_LEGACY"),
            )
            grid_layer = Gimp.Layer.new(
                image,
                layer_name + "Checkerboard" if checkerboard else layer_name + "Grid",
                width,
                height,
                Gimp.ImageType.RGBA_IMAGE,
                20,
                _layer_mode("DARKEN_ONLY", "DARKEN_ONLY_LEGACY", "DARKEN", "DARKEN_LEGACY"),
            )
            image.insert_layer(arg_layer, analysis_group, -1)
            image.insert_layer(mod_layer, analysis_group, -1)
            image.insert_layer(grid_layer, analysis_group, -1)
            _push_bytes_to_layer(arg_layer, width, height, arg_pixels)
            _push_bytes_to_layer(mod_layer, width, height, mod_pixels)
            _push_bytes_to_layer(grid_layer, width, height, grid_pixels)

        comment = (
            f"# conformal {CONF_VERSION}\n"
            f"code = \"\"\"\n{code}\n\"\"\"\n"
            f"constraint = \"\"\"\n{constraint}\n\"\"\"\n"
            f"domain_xl = {domain_xl}\ndomain_xr = {domain_xr}\ndomain_yt = {domain_yt}\ndomain_yb = {domain_yb}\n"
            f"source_xl = {source_xl}\nsource_xr = {source_xr}\nsource_yt = {source_yt}\nsource_yb = {source_yb}\n"
            f"scale = {scale_value}\nscale_long_side = {int(scale_long_side)}\n"
            f"grid = {grid}\ngrid_long_side = {int(grid_long_side)}\ncheckerboard = {int(checkerboard)}\n"
            f"gradient = {gradient}\n"
            f"abyss_mode = {abyss_mode}\nabyss_loop_iterations = {abyss_loop_iterations}\n"
            f"width = {width}\nheight = {height}\n"
        )
        parasite = Gimp.Parasite.new("gimp-comment", Gimp.PARASITE_PERSISTENT, comment.encode("utf-8"))
        image.attach_parasite(parasite)
    except Exception as exc:
        Gimp.message(f"Conformal Mapping layer write error: {exc}")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))
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
            "Distort an image using complex functions",
            "Transforms the active layer through a conformal map and can optionally create argument/modulus/grid analysis layers.",
            name,
        )
        procedure.set_attribution("Michael J Gruber, DeeFeeCee", "Ported for GIMP 3.2", "2026")

        procedure.add_string_argument(
            "code",
            "_Formula",
            "Expression w = f(z) or Python code assigning w; w = may be omitted for simple expressions",
            "w = z",
            GObject.ParamFlags.READWRITE,
        )
        procedure.add_double_argument("center-x", "Input center _X", "Input center X coordinate", -1.0e9, 1.0e9, 0.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("center-y", "Input center _Y", "Input center Y coordinate", -1.0e9, 1.0e9, 0.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("zoom", "Output _zoom", "Zoom factor (higher values zoom in)", -1.0e9, 1.0e9, 1.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("output-center-x", "Output center X", "Output center X coordinate", -1.0e9, 1.0e9, 0.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("output-center-y", "Output center Y", "Output center Y coordinate", -1.0e9, 1.0e9, 0.0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("scale", "Input _scale", "Coordinate assigned to opposite sides of input image (half of short/long side). Applied before zoom.", 1.0e-5, 1.0e3, 1.0, GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("scale-long-side", "Scale uses _long side", "Apply Scale to the long image side instead of the short side", False, GObject.ParamFlags.READWRITE)
        procedure.add_int_argument("transform-precision", "Forward _precision", "Higher values add subpixel samples and increase work by roughly n². Does not apply to functions of form w = f(z) which use elementary functions.", 0, 100, 0, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("grid-density", "Grid _density (from center to side)", "Number of grid lines from the center to the selected image side", 1.0, 1000.0, 8.0, GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("grid-long-side", "Grid density uses l_ong side", "Measure Grid density from the center to the long image side instead of the short side", False, GObject.ParamFlags.READWRITE)
        units_choice = Gimp.Choice.new()
        units_choice.add("relative", 0, _("Relative coordinates"), "Coordinates relative to the shorter image side")
        units_choice.add("pixels", 1, _("Pixels"), "Absolute pixel units")
        procedure.add_choice_argument("coord-system", "_Coordinate system", "Coordinate unit system for center values", units_choice, "relative", GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("checkerboard", "_Checkerboard (grid if disabled)", "Use checkerboard instead of line grid", False, GObject.ParamFlags.READWRITE)
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
            "Comma-separated #RRGGBB values for custom colors. Used when preset is 'custom'",
            "#ff0000,#ffff00,#00ff00,#00ffff,#0000ff",
            GObject.ParamFlags.READWRITE,
        )
        choices_abyss = Gimp.Choice.new()
        choices_abyss.add("loop", 0, _("Loop"), "Repeat image in tiles")
        choices_abyss.add("reflect", 1, _("Reflect"), "Mirror-repeat image in tiles")
        choices_abyss.add("clamp", 2, _("Clamp"), "Clamp to nearest edge pixel")
        choices_abyss.add("transparent", 3, _("Transparent"), "Transparent outside area")
        choices_abyss.add("foreground", 4, _("Foreground color"), "Use the current foreground color outside area")
        choices_abyss.add("background", 5, _("Background color"), "Use the current background color outside area")
        choices_abyss.add("black", 6, _("Black"), "Black outside area")
        choices_abyss.add("white", 7, _("White"), "White outside area")
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
            "_Tile iterations",
            "Maximum tile iterations in loop abyss mode",
            1,
            1024,
            2,
            GObject.ParamFlags.READWRITE,
        )
        choices_log = Gimp.Choice.new()
        choices_log.add("2", 0, _("2"), "Base-2 logarithm")
        choices_log.add("e", 1, _("e"), "Natural logarithm")
        choices_log.add("10", 2, _("10"), "Base-10 logarithm")
        procedure.add_choice_argument("log-base", "_Logarithm", "Logarithm base for modulus layer", choices_log, "2", GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("transform-active-layer", "_Transform active layer", "Transform pixels in the active layer directly", True, GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("create-analysis-layers", "Add _analysis layers", "Create argument/modulus/grid helper layers", True, GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("analysis-group", "_Group analysis layers (has visual bug)", "You may need to toggle the layers' visibility for them to appear correctly", False, GObject.ParamFlags.READWRITE)
        procedure.add_boolean_argument("coordinate-settings-expanded", "Coordinate settings expanded", "Remember whether Coordinate/Scale settings were expanded in the dialog", False, GObject.ParamFlags.READWRITE)

        return procedure


Gimp.main(ConformalPlugin.__gtype__, sys.argv)
