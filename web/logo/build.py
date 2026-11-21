#!/usr/bin/env python3
"""OnlyVibes pixel-art logo: the ASCII grids below are the source of truth.

Everything sits on one 20-row grid (row 0 = top). Each row has a single gradient colour
(yellow at the top, red at the bottom), so the mark and "Vibes" band exactly like the
original raster logo. Run `python3 web/logo/build.py` to regenerate the SVGs next to it:

  mark.svg              OV mark with the cat (favicon, compact header)
  wordmark.svg          "OnlyVibes", dark "Only" for light backgrounds
  wordmark-dark.svg     "OnlyVibes", light "Only" for dark backgrounds (the board)
  onlyvibes.svg         mark + wordmark, light backgrounds
  onlyvibes-dark.svg    mark + wordmark, dark backgrounds

Legend: '#' gradient, 'o' ink ("Only"), 'c' cat, 'd' cat chin, 'k' cat eyes/nose, '.' empty.
"""

from pathlib import Path

ROWS = 20

# Gradient stops (row, hex); rows in between are interpolated and rounded per row.
STOPS = [(0, "ffe600"), (5, "ffaa00"), (10, "ff6e00"), (14, "ff3a00"), (19, "d9120f")]

FIXED = {"c": "#ff8c1a", "d": "#f0541a", "k": "#141414"}
INK = {"light": "#111111", "dark": "#d6dce2"}

MARK = """
......#######................#####
...#############.............#####
..###############............###..
.#####.......#####...........###..
.####.........####..........###...
####...........####.........###...
###..c.......c..###........###....
###..cc.....cc..###........###....
###..ccccccccc..#####.....###.....
###.ccckccckccc.#####.....###.....
###.ccccckccccc.######...###......
###.ccccccccccc.######...###......
####.ddddddddd.####.###.###.......
.####.........####..###.###.......
.#####.......#####...#####........
..###############....#####........
...#############......###.........
......#######.........###.........
.......................#..........
..................................
"""

# Cap height rows 2-15, x-height rows 6-15, descender rows 16-18. '#' here means "letter ink";
# it is recoloured to 'o' for "Only" and kept as gradient for "Vibes".
GLYPHS = {
    "O": [
        ".............",
        ".............",
        "...#######...",
        ".###########.",
        "####.....####",
        "###.......###",
        "###.......###",
        "###.......###",
        "###.......###",
        "###.......###",
        "###.......###",
        "###.......###",
        "###.......###",
        "####.....####",
        ".###########.",
        "...#######...",
    ],
    "n": [
        "###.####.",
        "#########",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
    ],
    "l": ["###"] * 14,
    "y": [
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "#########",
        ".########",
        "......###",
        ".########",
        ".#######.",
    ],
    "V": [
        "###.......###",
        "###.......###",
        "###.......###",
        "###.......###",
        ".###.....###.",
        ".###.....###.",
        "..###...###..",
        "..###...###..",
        "...###.###...",
        "...###.###...",
        "....#####....",
        "....#####....",
        ".....###.....",
        ".....###.....",
    ],
    "i": ["###", "###", "...", "...", "###", "###", "###", "###", "###", "###", "###", "###", "###", "###"],
    "b": [
        "###......",
        "###......",
        "###......",
        "###......",
        "###.####.",
        "#########",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "###...###",
        "#########",
        "########.",
    ],
    "e": [
        ".#######.",
        "#########",
        "###...###",
        "###...###",
        "#########",
        "#########",
        "###......",
        "###......",
        "#########",
        ".########",
    ],
    "s": [
        ".########",
        "#########",
        "###......",
        "###......",
        "########.",
        ".########",
        "......###",
        "......###",
        "#########",
        "########.",
    ],
}
# first row each glyph occupies
TOP = {"O": 0, "n": 6, "l": 2, "y": 6, "V": 2, "i": 2, "b": 2, "e": 6, "s": 6}
LETTER_GAP = 2
MARK_GAP = 5


def gradient(row: int) -> str:
    for (r0, c0), (r1, c1) in zip(STOPS, STOPS[1:]):
        if r0 <= row <= r1:
            t = (row - r0) / (r1 - r0)
            a, b = bytes.fromhex(c0), bytes.fromhex(c1)
            return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))
    raise ValueError(row)


def grid(text: str) -> list[str]:
    rows = [r for r in text.strip("\n").split("\n")]
    width = max(map(len, rows))
    rows = [r.ljust(width, ".") for r in rows]
    assert len(rows) == ROWS, f"{len(rows)} rows"
    return rows


def glyph(ch: str, ink: str) -> list[str]:
    g = GLYPHS[ch]
    w = len(g[0])
    assert all(len(r) == w for r in g), ch
    out = ["." * w] * ROWS
    for i, r in enumerate(g):
        out[TOP[ch] + i] = r.replace("#", ink)
    return out


def hcat(*parts: list[str], gap: int) -> list[str]:
    sep = "." * gap
    return [sep.join(p[r] for p in parts) for r in range(ROWS)]


def wordmark() -> list[str]:
    letters = [glyph(c, "o") for c in "Only"] + [glyph(c, "#") for c in "Vibes"]
    return hcat(*letters, gap=LETTER_GAP)


def svg(rows: list[str], ink: str, title: str) -> str:
    # trim fully empty trailing columns, keep the 20-row height so every asset aligns
    width = len(rows[0])
    while width and all(r[width - 1] == "." for r in rows):
        width -= 1
    rects = []
    for y, row in enumerate(rows):
        x = 0
        while x < width:
            ch = row[x]
            run = x
            while run < width and row[run] == ch:
                run += 1
            if ch != ".":
                fill = gradient(y) if ch == "#" else INK[ink] if ch == "o" else FIXED[ch]
                rects.append(f'<rect x="{x}" y="{y}" width="{run - x}" height="1" fill="{fill}"/>')
            x = run
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {ROWS}" '
        f'width="{width}" height="{ROWS}" shape-rendering="crispEdges" role="img">'
        f"<title>{title}</title>" + "".join(rects) + "</svg>\n"
    )


def main() -> None:
    here = Path(__file__).parent
    mark, word = grid(MARK), wordmark()
    mark_w = max(len(r.rstrip(".")) for r in mark)
    full = [m[:mark_w] + "." * MARK_GAP + w for m, w in zip(mark, word)]
    out = {
        "mark.svg": svg(mark, "light", "OnlyVibes"),
        "wordmark.svg": svg(word, "light", "OnlyVibes"),
        "wordmark-dark.svg": svg(word, "dark", "OnlyVibes"),
        "onlyvibes.svg": svg(full, "light", "OnlyVibes"),
        "onlyvibes-dark.svg": svg(full, "dark", "OnlyVibes"),
    }
    for name, body in out.items():
        (here / name).write_text(body)
    for r in full:
        print(r)


if __name__ == "__main__":
    main()
