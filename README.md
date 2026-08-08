# Conformal Mapping Plugin for Gimp 3

This is a plugin-in for [Gimp 3](http://gimp.org) which uses complex functions to transform an image while preserving local angles. Its primary workflow is to transform an existing image through a [conformal map](https://en.wikipedia.org/wiki/Conformal_map), while also including optional analysis layers such as a grid & customizable domain coloring. This is an updated version of the [GIMP 2 plugin](https://github.com/mjg/conformal) by Michael J. Gruber. This would not be possible without his code, untouched for nearly 14 years. Also thanks to Codex because I don't know Python.

## Installation

Copy `conformal.py` and the `third_party/` directory into your GIMP 3 plug-ins directory and make `conformal.py` executable. The plugin prefers the bundled SymPy 1.14.0 path under `third_party/sympy-1.14.0` for symbolic inverse rendering.

## Usage

Open an image, select a layer, then run **Filters → Distorts → Conformal Mapping**.

Main controls:

- **Formula**: A simple expression `w = f(z)` (the leading `w =` is optional) or Python code assigning `w` from `z`.
- **Coordinate system**: Relative coordinates are defined as (0,0) at the image's center & (1,y) or (x,1) at whichever is the shortest edge.
- **Input center X/Y / Output center X/Y / Zoom / Grid length (shorter side)**: Numeric map controls via slider + text entry.
- **Palette / Custom palette**: Domain coloring options.
- **Abyss mode / Wrap iterations**: Out-of-bounds sampling behavior.
- **Transform active layer**: Creates transformed output layer from active layer.
- **Add analysis layers**: Adds `Argument`, `Log Modulus`, and `Grid`/`Checkerboard` layers.
- **Checkerboard**: Uses checkerboard analysis layer instead of line grid.
- **Grid length (shorter side)**: Creates a grid that is x-units wide.
- **Log modulus base**: The base used by the log modulus.
- **Forward precision**: Applies only when the formula is Python code or cannot be symbolically inverted as a simple `w = f(z)` expression; higher values add subpixel samples and increase transform time by roughly n².

## Formula notes
- `^` is accepted as exponentiation and converted to `**`.
- `i` is interpreted as the imaginary unit (`j`).
- `2z` style shorthand is normalized to `2*z`.
- Helper functions/recursion are supported as long as final code assigns `w`.
- For a single expression of the form `w = f(z)` (or just `f(z)`), the transform uses SymPy to solve for the symbolic inverse and samples the source image with that inverse.
- Python code and formulas that cannot be inverted fall back to forward mapping with the **Forward precision** slider.

## License

GPL-2.0-only.
