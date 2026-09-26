"""Render a pyte terminal screen, including cell styling, to a standalone SVG."""

from __future__ import annotations

from html import escape

PALETTES = {
    "pink": ("#ff5c8a", "#1e1e2e"),
    "dark": ("#d8dee9", "#20242c"),
    "light": ("#30343b", "#f6f5f1"),
    "nocolor": ("#d8dee9", "#20242c"),
}
NAMED = {"black": "#000000", "red": "#cd3131", "green": "#0dbc79",
         "brown": "#e5e510", "blue": "#2472c8", "magenta": "#bc3fbc",
         "cyan": "#11a8cd", "white": "#e5e5e5"}


def _color(value: str, default: str) -> str:
    if value == "default":
        return default
    if len(value) == 6 and all(character in "0123456789abcdefABCDEF" for character in value):
        return "#" + value.lower()
    return NAMED.get(value, default)


def render(screen, palette: str = "pink") -> str:
    """Return an SVG snapshot of a pyte screen under a named terminal palette."""
    if palette not in PALETTES:
        raise ValueError("unknown palette")
    default_fg, default_bg = PALETTES[palette]
    cell_w, cell_h = 10, 19
    width, height = screen.columns * cell_w, screen.lines * cell_h
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             f'<rect width="{width}" height="{height}" fill="{default_bg}"/>']
    for row in range(screen.lines):
        for column in range(screen.columns):
            cell = screen.buffer[row][column]
            x, y = column * cell_w, row * cell_h
            fg = _color(cell.fg, default_fg)
            bg = _color(cell.bg, default_bg)
            if cell.reverse:
                fg, bg = bg, fg
            if bg != default_bg:
                parts.append(f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" fill="{bg}"/>')
            if cell.data and cell.data != " ":
                weight = ' font-weight="bold"' if cell.bold else ""
                parts.append(f'<text x="{x}" y="{y+15}" fill="{fg}"{weight}>{escape(cell.data)}</text>')
    parts.insert(2, '<style>text{font-family:monospace;font-size:15px;white-space:pre}</style>')
    parts.append('</svg>')
    return "\n".join(parts) + "\n"
