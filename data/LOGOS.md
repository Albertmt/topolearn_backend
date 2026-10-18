# Tool logos

`data/logos.json` holds, per tool id, the path data of a 24x24 single-path SVG. The app draws it
in white on the tool's colour on tiles and headers. `scripts/build_db.py` wraps it into an SVG and
loads it into the catalog (`tools.logo_svg`); tools without an entry get a monogram (`tool.icon`).

Source: [Simple Icons](https://simpleicons.org) v16.35 (CC0 1.0, public domain), reduced to a
single 24x24 path. Brands whose icons Simple Icons does not carry (Microsoft and Amazon products,
for example) use the monogram. Every logo is the trademark of its owner; Topolearn is not
affiliated with or endorsed by any of them (see the terms of use).

To add one: add `"<tool id>": {"source": "...", "path": "<the d attribute of a 24x24 single-path SVG>"}`
to `data/logos.json` and reload the catalog.
