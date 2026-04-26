# Conformal Mapping Plugin for Gimp 3

This is a plugin-in for [Gimp 3](http://gimp.org) which uses complex functions to transform an image while preserving local angles. Its primary workflow is to transform an existing image through a [conformal map](https://en.wikipedia.org/wiki/Conformal_map), while also including optional analysis layers such as a grid & customizable domain coloring. This is an updated version of the [GIMP 2 plugin](https://github.com/mjg/conformal) by Michael J. Gruber. This would not be possible without his code, untouched for nearly 14 years. Also thanks to Codex because I don't know Python.

## Installation

Copy `conformal.py` into your GIMP 3 plug-ins directory and make it executable.

## Usage

Open an image, select a layer, then run **Filters → Distorts → Conformal Mapping**.

Main controls:

- **Formula**: Python code assigning `w` from `z`.
- **X left / X right / Y top / Y bottom / Grid step**: Numeric map controls via sliders.
- **Palette / Custom palette**: Domain coloring options.
- **Abyss mode / Wrap iterations**: Out-of-bounds sampling behavior.
- **Transform active layer**: Creates transformed output layer.
- **Add analysis layers**: Adds `Argument`, `Log Modulus`, and `Grid`/`Checkerboard` layers.
- **Checkerboard**: Uses checkerboard analysis layer instead of line grid.

## Formula notes

- `^` is accepted as exponentiation and converted to `**`.
- `i` is interpreted as the imaginary unit (`j`).
- `2z` style shorthand is normalized to `2*z`.
- Helper functions/recursion are supported as long as final code assigns `w`.

## License

GPL-2.0-only.
