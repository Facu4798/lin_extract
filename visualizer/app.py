"""Cosmos Lineage Visualizer.

A small tkinter app for drawing lineage connections between fields of
CSV "table" definitions.

Usage:
    python app.py

It reads every ``*.csv`` file in ``./tables/`` (each file needs a
``field`` and a ``description`` column -- any other columns are
ignored) and draws one draggable box per table on a scrollable canvas.

Click a row, then click another row (in the same or a different table)
to draw a connection between them. Click a selected row again to
cancel the selection. Right-click a connection line for a menu to
recolor or delete it.

The toolbar's color swatch picks the color new connections are drawn
with, so you can group related connections by color as you go.

Table positions and connections (including each connection's color)
are persisted to ``connections.json`` next to this file, and reloaded
on the next run.
"""
from __future__ import annotations

import json
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import colorchooser

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
TABLES_DIR = BASE_DIR / "tables"
STATE_FILE = BASE_DIR / "connections.json"

ROW_HEIGHT = 24
HEADER_HEIGHT = 28
TABLE_WIDTH = 260
TABLE_GAP_X = 60
TABLE_GAP_Y = 50
MAX_ROW_WIDTH = 920  # wrap tables onto a new row past this canvas x

COLOR_HEADER_BG = "#3a5a78"
COLOR_HEADER_FG = "white"
COLOR_ROW_EVEN = "#ffffff"
COLOR_ROW_ODD = "#f2f5f8"
COLOR_ROW_SELECTED = "#ffe08a"
COLOR_ROW_OUTLINE = "#c7ccd1"
COLOR_TABLE_OUTLINE = "#8a94a0"
COLOR_FIELD_TEXT = "#1a1a1a"
COLOR_DESC_TEXT = "#6e6e6e"
COLOR_LINE = "#2f7dd1"
COLOR_LINE_HOVER = "#d1462f"

FONT_HEADER = ("Segoe UI", 10, "bold")
FONT_FIELD = ("Segoe UI", 9, "bold")
FONT_DESC = ("Segoe UI", 8)


field_column = "field"
description_column = "description"


def load_tables(tables_dir: Path) -> dict[str, list[dict[str, str]]]:
    """Read every CSV in ``tables_dir`` and keep the field/description rows.

    Returns a mapping of table name (the csv filename, no extension) to a
    list of {"field": ..., "description": ...} dicts. Files that don't
    have both required columns are skipped with a printed warning.
    """
    tables: dict[str, list[dict[str, str]]] = {}
    if not tables_dir.exists():
        print(f"warning: tables directory not found: {tables_dir}")
        return tables

    for csv_path in sorted(tables_dir.glob("*.csv")):
        try:
            df = pd.read_csv(csv_path, dtype=str)
        except Exception as exc:  # malformed csv shouldn't crash the app
            print(f"warning: could not read {csv_path.name}: {exc}")
            continue

        df.columns = [str(c).strip().lower() for c in df.columns]
        if field_column not in df.columns or description_column not in df.columns:
            print(
                f"warning: {csv_path.name} is missing 'field'/'description' "
                f"columns, skipping (found: {list(df.columns)})"
            )
            continue

        rows = []
        for _, row in df.iterrows():
            field = str(row[field_column]).strip() if pd.notna(row[field_column]) else ""
            desc = (
                str(row[description_column]).strip()
                if pd.notna(row[description_column])
                else ""
            )
            if not field:
                continue
            rows.append({field_column: field, description_column: desc})

        tables[csv_path.name] = rows

    return tables


def truncate_to_width(font: tkfont.Font, text: str, max_width: int) -> str:
    """Trim ``text`` with an ellipsis so it fits within ``max_width`` pixels."""
    if font.measure(text) <= max_width:
        return text
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + ellipsis) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis if lo > 0 else ellipsis


class Tooltip:
    """A tiny popup that shows the full field/description text on hover."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self._win: tk.Toplevel | None = None

    def show(self, x: int, y: int, text: str) -> None:
        self.hide()
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        label = tk.Label(
            win,
            text=text,
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            font=FONT_DESC,
            justify="left",
            wraplength=360,
            padx=4,
            pady=2,
        )
        label.pack()
        win.geometry(f"+{x + 12}+{y + 12}")
        self._win = win

    def hide(self) -> None:
        if self._win is not None:
            self._win.destroy()
            self._win = None


class Row:
    """One field/description row belonging to a TableBox."""

    def __init__(self, table: "TableBox", index: int, field: str, description: str):
        self.table = table
        self.index = index
        self.field = field
        self.description = description
        self.rect_id: int | None = None
        self.text_id: int | None = None
        self.selected = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.table.name, self.field)

    def local_y(self) -> int:
        return HEADER_HEIGHT + self.index * ROW_HEIGHT

    def center_y(self) -> int:
        return self.table.y + self.local_y() + ROW_HEIGHT // 2

    def left_x(self) -> int:
        return self.table.x

    def right_x(self) -> int:
        return self.table.x + self.table.width


class TableBox:
    """A draggable box rendering one csv's field/description rows."""

    def __init__(self, app: "App", name: str, rows: list[dict[str, str]], x: int, y: int):
        self.app = app
        self.canvas = app.canvas
        self.name = name
        self.x = x
        self.y = y
        self.width = TABLE_WIDTH
        self.rows: list[Row] = [
            Row(self, i, r[field_column], r[description_column]) for i, r in enumerate(rows)
        ]
        self.height = HEADER_HEIGHT + len(self.rows) * ROW_HEIGHT
        self._header_rect_id: int | None = None
        self._border_id: int | None = None
        self._drag_origin: tuple[int, int] | None = None
        self.tag = f"table_{self.name}"
        self.draw()

    # -- drawing -----------------------------------------------------
    def draw(self) -> None:
        c = self.canvas
        tag = self.tag

        self._border_id = c.create_rectangle(
            self.x, self.y, self.x + self.width, self.y + self.height,
            outline=COLOR_TABLE_OUTLINE, width=1, fill="", tags=(tag,),
        )

        self._header_rect_id = c.create_rectangle(
            self.x, self.y, self.x + self.width, self.y + HEADER_HEIGHT,
            fill=COLOR_HEADER_BG, outline=COLOR_TABLE_OUTLINE, tags=(tag, "header"),
        )
        header_text_id = c.create_text(
            self.x + self.width / 2, self.y + HEADER_HEIGHT / 2,
            text=self.name, fill=COLOR_HEADER_FG, font=FONT_HEADER,
            tags=(tag, "header"),
        )

        for row in self.rows:
            self._draw_row(row)

        # Bind only this table's own header items -- find_withtag("header")
        # would match every table's header (the tag isn't table-specific)
        # and rebind earlier tables' headers to drag this one instead.
        for item in (self._header_rect_id, header_text_id):
            c.tag_bind(item, "<ButtonPress-1>", self._on_drag_start)
            c.tag_bind(item, "<B1-Motion>", self._on_drag_move)
            c.tag_bind(item, "<ButtonRelease-1>", self._on_drag_end)

    def _draw_row(self, row: Row) -> None:
        c = self.canvas
        top = self.y + row.local_y()
        bottom = top + ROW_HEIGHT
        fill = COLOR_ROW_SELECTED if row.selected else (
            COLOR_ROW_EVEN if row.index % 2 == 0 else COLOR_ROW_ODD
        )
        row_tag = f"row_{self.name}_{row.index}"

        row.rect_id = c.create_rectangle(
            self.x, top, self.x + self.width, bottom,
            fill=fill, outline=COLOR_ROW_OUTLINE,
            tags=(self.tag, "row", row_tag),
        )

        field_font = tkfont.Font(font=FONT_FIELD)
        desc_font = tkfont.Font(font=FONT_DESC)
        field_w = field_font.measure(row.field + "  ")
        available_desc_w = max(10, self.width - 12 - field_w)
        desc_display = truncate_to_width(desc_font, row.description, available_desc_w)

        # field (bold) + description (grey) drawn as two adjoining text items
        field_id = c.create_text(
            self.x + 6, (top + bottom) / 2, anchor="w",
            text=row.field, font=FONT_FIELD, fill=COLOR_FIELD_TEXT,
            tags=(self.tag, "row", row_tag),
        )
        fx0, _, fx1, _ = c.bbox(field_id)
        c.create_text(
            fx1 + 6, (top + bottom) / 2, anchor="w",
            text=desc_display, font=FONT_DESC, fill=COLOR_DESC_TEXT,
            tags=(self.tag, "row", row_tag),
        )
        row.text_id = field_id

        for item in c.find_withtag(row_tag):
            c.tag_bind(item, "<Button-1>", lambda e, r=row: self.app.on_row_click(r))
            c.tag_bind(item, "<Enter>", lambda e, r=row: self.app.on_row_enter(e, r))
            c.tag_bind(item, "<Leave>", lambda e, r=row: self.app.on_row_leave())

    def redraw_row(self, row: Row) -> None:
        self.canvas.delete(f"row_{self.name}_{row.index}")
        self._draw_row(row)

    # -- dragging ------------------------------------------------------
    def _on_drag_start(self, event) -> None:
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        self._drag_origin = (cx - self.x, cy - self.y)

    def _on_drag_move(self, event) -> None:
        if self._drag_origin is None:
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        new_x = cx - self._drag_origin[0]
        new_y = cy - self._drag_origin[1]
        dx = new_x - self.x
        dy = new_y - self.y
        if dx == 0 and dy == 0:
            return
        self.canvas.move(f"table_{self.name}", dx, dy)
        self.x = new_x
        self.y = new_y
        self.app.redraw_connections_for_table(self.name)

    def _on_drag_end(self, event) -> None:
        self._drag_origin = None
        self.app.update_scrollregion()
        self.app.save_state()

    def find_row(self, field: str) -> Row | None:
        return next((r for r in self.rows if r.field == field), None)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Cosmos Lineage Visualizer")
        self.root.geometry("1100x700")

        self.current_line_color = COLOR_LINE

        self._build_menu()
        self._build_toolbar()

        container = tk.Frame(root)
        container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(container, bg="#eef1f4")
        hbar = tk.Scrollbar(container, orient="horizontal", command=self.canvas.xview)
        vbar = tk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.status = tk.Label(root, anchor="w", text="", bd=1, relief="sunken")
        self.status.pack(fill="x", side="bottom")

        self.tooltip = Tooltip(root)

        self.tables: dict[str, TableBox] = {}
        # connection key -> {"line": id, "a": Row, "b": Row}
        self.connections: dict[tuple, dict] = {}
        self.selected_row: Row | None = None

        self._bind_canvas_events()
        self.reload_tables()
        self.set_status(
            "Click a row, then click another row to connect them. "
            "Right-click a connection to recolor or delete it."
        )

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- menu ------------------------------------------------------
    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Reload tables", command=self.reload_tables)
        file_menu.add_command(label="Save layout", command=self.save_state)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=False)
        edit_menu.add_command(label="Clear all connections", command=self.clear_connections)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        self.root.config(menu=menubar)

    # -- toolbar -----------------------------------------------------
    def _build_toolbar(self) -> None:
        toolbar = tk.Frame(self.root, bd=1, relief="raised")
        toolbar.pack(fill="x", side="top")

        tk.Label(toolbar, text="New connection color:").pack(
            side="left", padx=(8, 4), pady=4
        )

        self.color_swatch = tk.Canvas(
            toolbar, width=22, height=22, highlightthickness=1,
            highlightbackground="#888888", cursor="hand2",
        )
        self.color_swatch.pack(side="left", pady=4)
        self._swatch_rect = self.color_swatch.create_rectangle(
            0, 0, 22, 22, fill=self.current_line_color, outline=""
        )
        self.color_swatch.bind("<Button-1>", lambda e: self._pick_new_connection_color())

        tk.Label(
            toolbar,
            text="Applies to new connections — right-click an existing line to recolor it.",
            fg="#555555",
        ).pack(side="left", padx=(8, 0))

    def _pick_new_connection_color(self) -> None:
        _, hex_color = colorchooser.askcolor(
            color=self.current_line_color, title="New connection color", parent=self.root
        )
        if not hex_color:
            return
        self.current_line_color = hex_color
        self.color_swatch.itemconfig(self._swatch_rect, fill=hex_color)
        self.set_status(f"New connections will use {hex_color}.")

    # -- canvas navigation ------------------------------------------
    def _bind_canvas_events(self) -> None:
        self.canvas.bind("<ButtonPress-2>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B2-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_mousewheel)

    def _on_mousewheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_shift_mousewheel(self, event) -> None:
        self.canvas.xview_scroll(int(-event.delta / 120), "units")

    # -- loading / state ------------------------------------------
    def reload_tables(self) -> None:
        self.canvas.delete("all")
        self.tables.clear()
        self.connections.clear()
        self.selected_row = None

        data = load_tables(TABLES_DIR)
        state = self._load_state_file()
        positions = state.get("positions", {})

        x, y = TABLE_GAP_X, TABLE_GAP_Y
        row_max_height = 0
        for name in sorted(data.keys()):
            rows = data[name]
            if name in positions:
                tx, ty = positions[name]
            else:
                if x + TABLE_WIDTH > MAX_ROW_WIDTH:
                    x = TABLE_GAP_X
                    y += row_max_height + TABLE_GAP_Y
                    row_max_height = 0
                tx, ty = x, y
                x += TABLE_WIDTH + TABLE_GAP_X

            table = TableBox(self, name, rows, tx, ty)
            self.tables[name] = table
            row_max_height = max(row_max_height, table.height)

        for entry in state.get("connections", []):
            a_table, a_field, b_table, b_field = entry[:4]
            color = entry[4] if len(entry) > 4 else COLOR_LINE
            self._create_connection_if_valid(a_table, a_field, b_table, b_field, color)

        self.update_scrollregion()
        self.set_status(f"Loaded {len(self.tables)} table(s) from {TABLES_DIR}")

    def _load_state_file(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"warning: could not read {STATE_FILE.name}: {exc}")
        return {}

    def save_state(self) -> None:
        state = {
            "positions": {name: [t.x, t.y] for name, t in self.tables.items()},
            "connections": [
                [a_t, a_f, b_t, b_f, conn["color"]]
                for (a_t, a_f, b_t, b_f), conn in self.connections.items()
            ],
        }
        try:
            STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"warning: could not save {STATE_FILE.name}: {exc}")

    def _on_close(self) -> None:
        self.save_state()
        self.root.destroy()

    def update_scrollregion(self) -> None:
        bbox = self.canvas.bbox("all")
        if bbox:
            pad = 40
            self.canvas.configure(
                scrollregion=(bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
            )

    # -- row interaction --------------------------------------------
    def on_row_enter(self, event, row: Row) -> None:
        self.canvas.config(cursor="hand2")
        self.tooltip.show(event.x_root, event.y_root, f"{row.field}: {row.description}")

    def on_row_leave(self) -> None:
        self.canvas.config(cursor="")
        self.tooltip.hide()

    def on_row_click(self, row: Row) -> None:
        if self.selected_row is None:
            self._set_selected(row, True)
            self.selected_row = row
            self.set_status(f"Selected {row.table.name}.{row.field} — click another row to connect.")
            return

        if row is self.selected_row:
            self._set_selected(row, False)
            self.selected_row = None
            self.set_status("Selection cleared.")
            return

        first = self.selected_row
        self._set_selected(first, False)
        self.selected_row = None
        self.toggle_connection(first, row)

    def _set_selected(self, row: Row, value: bool) -> None:
        row.selected = value
        row.table.redraw_row(row)

    # -- connections ---------------------------------------------
    @staticmethod
    def _conn_key(a: Row, b: Row) -> tuple:
        key_a = (a.table.name, a.field)
        key_b = (b.table.name, b.field)
        return (key_a + key_b) if key_a <= key_b else (key_b + key_a)

    def toggle_connection(self, a: Row, b: Row) -> None:
        key = self._conn_key(a, b)
        if key in self.connections:
            self._remove_connection(key)
            self.set_status(f"Removed connection {key[0]}.{key[1]} ↔ {key[2]}.{key[3]}")
            return
        self._add_connection(key, a, b)
        self.set_status(f"Connected {key[0]}.{key[1]} ↔ {key[2]}.{key[3]}")
        self.save_state()

    def _create_connection_if_valid(self, a_table, a_field, b_table, b_field, color=COLOR_LINE) -> None:
        ta = self.tables.get(a_table)
        tb = self.tables.get(b_table)
        if not ta or not tb:
            return
        ra = ta.find_row(a_field)
        rb = tb.find_row(b_field)
        if not ra or not rb:
            return
        key = self._conn_key(ra, rb)
        if key not in self.connections:
            self._add_connection(key, ra, rb, color=color)

    def _add_connection(self, key: tuple, a: Row, b: Row, color: str | None = None) -> None:
        color = color or self.current_line_color
        line_id = self.canvas.create_line(
            *self._connection_points(a, b),
            smooth=True, fill=color, width=2, tags=("connection",),
        )
        self.canvas.tag_lower(line_id, "row")
        self.canvas.tag_bind(line_id, "<Button-3>", lambda e, k=key: self._on_connection_right_click(e, k))
        self.canvas.tag_bind(line_id, "<Enter>", lambda e, i=line_id: self.canvas.itemconfig(i, fill=COLOR_LINE_HOVER))
        self.canvas.tag_bind(
            line_id, "<Leave>",
            lambda e, i=line_id, k=key: self.canvas.itemconfig(i, fill=self.connections[k]["color"]),
        )
        self.connections[key] = {"line": line_id, "a": a, "b": b, "color": color}

    def _remove_connection(self, key: tuple) -> None:
        conn = self.connections.pop(key, None)
        if conn:
            self.canvas.delete(conn["line"])
        self.save_state()

    def _on_connection_right_click(self, event, key: tuple) -> None:
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Change color…", command=lambda: self._change_connection_color(key))
        menu.add_command(label="Delete connection", command=lambda: self._delete_connection(key))
        menu.tk_popup(event.x_root, event.y_root)

    def _delete_connection(self, key: tuple) -> None:
        self._remove_connection(key)
        self.set_status(f"Removed connection {key[0]}.{key[1]} ↔ {key[2]}.{key[3]}")

    def _change_connection_color(self, key: tuple) -> None:
        conn = self.connections.get(key)
        if not conn:
            return
        _, hex_color = colorchooser.askcolor(
            color=conn["color"], title="Connection color", parent=self.root
        )
        if not hex_color:
            return
        conn["color"] = hex_color
        self.canvas.itemconfig(conn["line"], fill=hex_color)
        self.save_state()
        self.set_status(f"Recolored {key[0]}.{key[1]} ↔ {key[2]}.{key[3]} to {hex_color}")

    def clear_connections(self) -> None:
        for key in list(self.connections.keys()):
            self._remove_connection(key)
        self.set_status("Cleared all connections.")

    @staticmethod
    def _connection_points(a: Row, b: Row) -> tuple[int, ...]:
        a_center = a.table.x + a.table.width / 2
        b_center = b.table.x + b.table.width / 2
        if a_center <= b_center:
            x1, y1 = a.right_x(), a.center_y()
            x2, y2 = b.left_x(), b.center_y()
        else:
            x1, y1 = a.left_x(), a.center_y()
            x2, y2 = b.right_x(), b.center_y()
        mid_x = (x1 + x2) / 2
        return (x1, y1, mid_x, y1, mid_x, y2, x2, y2)

    def redraw_connections_for_table(self, table_name: str) -> None:
        for key, conn in self.connections.items():
            a, b = conn["a"], conn["b"]
            if a.table.name == table_name or b.table.name == table_name:
                self.canvas.coords(conn["line"], *self._connection_points(a, b))

    def set_status(self, text: str) -> None:
        self.status.config(text=text)


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
