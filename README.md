# Conformal Mapping Plugin for Gimp 3

This is a plugin-in for [Gimp 3](http://gimp.org) which uses complex functions to transform an image while preserving local angles. Its primary workflow is to transform an existing image through a [conformal map](https://en.wikipedia.org/wiki/Conformal_map), while also including optional analysis layers such as a grid & customizable domain coloring. This is an updated version of the [GIMP 2 plugin](https://github.com/mjg/conformal) by Michael J. Gruber. This would not be possible without his code, untouched for nearly 14 years. Also thanks to Codex because I don't know Python.

## Installation

Copy `conformal.py` and the `third_party/` directory into a folder named `conformal`. Then place this folder into your GIMP 3 plug-ins directory, which for Windows is usually `C:\Users\USER\AppData\Roaming\GIMP\3.2\plug-ins`. The plugin uses the bundled SymPy 1.14.0 path under `third_party/sympy-1.14.0` and its bundled mpmath 1.4.0 dependency under `third_party/mpmath-1.4.0` for symbolic inverse rendering.

## Usage

Open an image, select a layer, then run **Filters → Distorts → Conformal Mapping**.

Main controls:

- **Formula**: A simple expression `w = f(z)` (the leading `w =` is optional) or Python code assigning `w` from `z`.
- **Forward precision**: Applies only when the formula is Python code or when the function's inverse cannot be determined. Setting the value to `n` adds 1 additional ring of subpixel sampling, with (2n+1)² samples total.
- **Coordinate system**: By default, relative coordinates are defined as (0,0) at the image's center & (1,y) or (x,1) at whichever is the shortest edge. `Pixels` uses GIMP's coordinate system, expanded outside the canvas. `Scale uses long side` bases the coordinate system on the long side instead.
- **Input scale**: Defines what the coordinate value is for the short/long side. For example, when a square image's scale is set to 2, the image spans from (-2,-2) to (2,2).
- **Input center X/Y / Output center X/Y**: Determine location of the origin in the input/output.
- **Output zoom**: Higher values zoom inwards. Zoom takes scale into account, so for the simple `w = z`, no center changing, a scale of 4 & zoom of 4 result in the same output as doing nothing. However, this is useful for more complex functions.
- **Transform active layer**: Creates transformed output layer from active layer.
- **Abyss mode / Tile iterations**: Out-of-bounds sampling behavior. `Loop tiling` creates identical copies of the image, tiling in all directions. `Reflect tiling` does the same but reflects along the proper axis to prevent harsh boundaries. `Clamp` uses the outermost pixels as out-of-bounds input. The rest are single colors.
- **Add analysis layers**: Adds `Argument`, `Log Modulus`, and `Grid`/`Checkerboard` layers.
- **Checkerboard**: Uses checkerboard analysis layer instead of line grid.
- **Group analysis layers**: Puts the analysis layers into a group. For some reason, the transparent elements initially don't appear properly. This is resolved if each layers' visibility is toggled off & on.
- **Palette / Custom palette**: Domain coloring options for the `Argument` analysis layer. Custom palettes apply from 0° to 360° in the counter-clockwise direction.
- **Log modulus base**: The base used by the `Log Modulus` analysis layer.
- **Grid density**: Determines the density of the grid or checkerboard. `Grid density uses long side` applies the density to the longer side. `Grid density (from center to side)` applies `n` grid lines or checkers from the center to the selected side.

## Formula notes
- `^` is accepted as exponentiation and converted to `**`.
- `i` is interpreted as the imaginary unit (`j`).
- Helper functions/recursion are supported as long as final code assigns `w`.
- For a single expression of the form `w = f(z)` (or just `f(z)`), the transform uses SymPy to solve for the symbolic inverse and samples the source image with that inverse.
- Python code and formulas that cannot be inverted fall back to forward mapping with the **Forward precision** slider.

## License

GPL-2.0-only.