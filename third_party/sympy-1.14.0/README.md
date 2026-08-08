# Bundled SymPy 1.14.0

This directory is reserved for the bundled SymPy 1.14.0 distribution used by
`conformal.py` for symbolic inverse rendering.

The plugin prepends this directory to `sys.path` before importing SymPy, so a
complete SymPy 1.14.0 source or wheel extraction can be placed here with its
`sympy/` package directory at this level.

Source release requested for vendoring:
https://github.com/sympy/sympy/releases/tag/1.14.0

Expected package layout after vendoring:

```text
third_party/sympy-1.14.0/
  LICENSE
  sympy/
    __init__.py
    ...
```

SymPy is redistributed under the BSD license. Keep the full upstream license
file in this directory when bundling or updating the vendored copy.
