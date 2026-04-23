conformal.py
============
:Author: Michael J. Gruber
:Email:  conformal@drmicha.warpmail.net
:Edited by: DeeFeeCee
:Revision: 0.4

== Introduction
`conformal.py` is a plugin-in for http://gimp.org[Gimp] which allows
conformal image distortion and conformal-map visualisation.
Its primary workflow is to transform an existing image (usually the
active layer) through a complex map; the analysis layers are optional
helpers.

== Requirements
You need `gimp` version 3.0 or above.

== Installation
You can install `conformal.py` as a local user or system wide:

=== Local User
Copy `conformal.py` to the `plug-ins` subdirectory of your Gimp
directory, usually `$HOME/.gimp-3.*/plug-ins/` on Linux, and make sure
that it is executable (`chmod +x conformal.py`).

=== System wide
Copy `conformal.py` to the `plug-ins` subdirectory of your system Gimp
directory, usually `/usr/lib/gimp/3.*/plug-ins/` or (similar) on Linux,
and make sure that it is executable (`chmod a+rx conformal.py`).

=== Usage
After starting Gimp, you find the conformal plug-in in
`Filters -> Distorts`. The default workflow is:

1. Open or create an image.
2. Choose the layer you want to distort.
3. Run Conformal Map with `Transform active layer` enabled.
4. Optionally keep `Create analysis layers` enabled to also generate
   the argument/modulus/grid helper layers.

From the dialogue, you can adjust these parameters:

`width`, `height`::
	The dimensions of the new image.
`code`::
	Python code executed for every pixel.
	The variable `z` is provided as the current complex coordinate.
	Assign the mapped value to `w`.
	You can use multi-line code, helper functions, and recursive calls.
	Use `^` or `**` for exponentiation (`z^2` and `z**2` both work).
	The symbol `i` is interpreted as Python's imaginary unit `j`.
`x left`, `x-right`::
	The range of x-values (real parts) which is mapped to the horizontal image axis.
`y top`, `y bottom`::
	The range of y-values (imaginary parts) which is mapped to the vertical image axis.
`grid spacing`::
	The spacing of the generated coordinate grid.
`checker board`::
	Use a checker board instead of a grid.
`palette`::
	Gradient preset used for the `Argument` layer.
	Supported options are `HSV`, `grayscale`, `red-blue`, `white-black`,
	or `custom`.
`custom palette`::
	When `palette` is `custom`, the plug-in opens a dialog where you can
	enter comma-separated hex stops like
	`#ff0000,#ffff00,#00ff00,#00ffff,#0000ff`.
`abyss mode`::
	Controls out-of-bounds sampling for transformed pixels:
	`transparent`, `black`, `white`, `clamp`, or `loop`.
`wrap iterations`::
	Safety cap for `loop` abyss mode wrap attempts.
NOTE: The old `constraint` parameter has been removed from the UI.
Invalid points should be handled directly in your `code` expression.

With `Overwrite active layer` enabled, the plugin writes the transformed
pixels directly back to the active layer (no extra transform layer is created).
If `Create analysis layers` is enabled, it additionally creates:

`Grid`::
	This layer paints the conformally transformed coordinate grid.
	
`Log. modulus`::
	This layer adds a shading corresponding to the absolute value on
	a logarithmic scale, i.e. one cycle (from white to black) of the
	shading means doubling the modulus: the fractional part of `ln |w|`
	is used as an index into the default white-black gradient.
	 
`Argument`::
	This layer is coloured by the selected `palette`
	at an index corresponding to `arg w`.

The two topmost layers have transparency and layer mode set
appropriately, but feel free to experiment with these, as well as
turning some layers off, depending on your goal: produce instructive
illustrations, or simply beautiful pictures!

== Troubleshooting
If your formula fails:

* Use valid Python syntax. For example, `2z` is invalid Python; use `2*z`.
* Mathematical notation like `2z` is not valid Python; write `2*z`.
* `^` is accepted for exponentiation and automatically converted to `**`.
* `i` is accepted as the imaginary unit and converted to `j`.
* Helper functions and recursive expressions are supported as long as
  the final code assigns `w`.
* If you get transparent/black output, start with a simple mapping
  (`z`, `z+1`, `z*z`) and increase complexity incrementally.
* If transformed pixels fall outside the source bounds, set `abyss mode`
  to `clamp` or `loop` to reduce transparent regions.
* The warning about `GLibWin32` typelibs is environment-specific and is
  unrelated to conformal expression parsing.

== License
`conformal.py` is copyrighted by Michael J. Gruber and is available
under the GNU General Public License Version 2.
