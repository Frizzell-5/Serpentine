"""Serpentine analysis engine.

Runs inside the browser (Pyodide web worker) and as an ordinary Python script.
Everything goes through `api(cmd, args_json)`, which returns a JSON string.
"""

import base64
import csv
import io
import json
import logging
import math
import re
import sys
import textwrap
import traceback

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager, patheffects  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Patch  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.colors import to_rgba, to_rgb, to_hex  # noqa: E402
from matplotlib.ticker import FuncFormatter, MultipleLocator, FormatStrFormatter, LogLocator, NullFormatter, MaxNLocator  # noqa: E402
import scipy  # noqa: E402
from scipy import stats  # noqa: E402
from scipy.optimize import curve_fit  # noqa: E402

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

ENGINE_VERSION = "0.8.0"


class UserError(Exception):
    """A problem the user can fix (shown as-is in the app)."""


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------

_CLOCK = re.compile(r"^-?\d+:\d{1,2}(:\d{1,2})?(\.\d+)?$")
_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_DEC_COMMA = re.compile(r"^-?\d*,\d+$")
_WITH_UNIT = re.compile(r"^(-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(?:[a-zA-Zµμ°/%·^²³]+[a-zA-Z0-9µμ°/%·^²³.\-]*)$")
_MISSING = {"", "nan", "na", "n/a", "-", "--", "null", "none", "#n/a", "#value!", "#div/0!", "#num!", "*", "**", "-.-", "—", "–", "?", "inf", "-inf", "missing", "excluded"}


def _num(cell, comma_decimal=False):
    s = str(cell).strip().replace("−", "-")
    if s.lower() in _MISSING:
        return None
    if s.endswith("%"):
        s = s[:-1].strip()
    if comma_decimal:
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
    elif _THOUSANDS.match(s):
        s = s.replace(",", "")
    elif _DEC_COMMA.match(s):
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        m = _WITH_UNIT.match(s)  # "48.2 mL/min", "12 BPM", "3.5mV"
        if not m:
            return None
        try:
            v = float(m.group(1))
        except ValueError:
            return None
    return v if math.isfinite(v) else None


def _clock(cell):
    s = str(cell).strip().replace("−", "-")
    if not _CLOCK.match(s):
        return None
    neg = s.startswith("-")
    try:
        vals = [float(p) for p in s.lstrip("-").split(":")]
    except ValueError:
        return None
    sec = vals[0] * 60 + vals[1] if len(vals) == 2 else vals[0] * 3600 + vals[1] * 60 + vals[2]
    return -sec if neg else sec


def _is_value(cell, comma_decimal):
    return _num(cell, comma_decimal) is not None or _clock(cell) is not None


_TABLE_CACHE = {}


def read_table(text):
    """Cached wrapper: re-running an analysis doesn't re-parse unchanged tables."""
    key = (len(text or ""), hash(text or ""))
    hit = _TABLE_CACHE.get(key)
    if hit is not None and hit[0] == text:
        return hit[1], hit[2]
    df, info = _read_table(text)
    if len(_TABLE_CACHE) > 64:
        _TABLE_CACHE.clear()
    _TABLE_CACHE[key] = (text, df, info)
    return df, info


def _read_table(text):
    """Parse pasted or exported text (LabChart Data Pad, Excel, Prism, CSV) into a DataFrame.

    Returns (df, info). Numeric and clock-time columns become floats (clock times in seconds);
    other columns stay text. info["kinds"] maps column -> "number" | "time" | "text".
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    lines = [ln for ln in text.split("\n") if ln.strip() != ""]
    if not lines:
        raise UserError("This table is empty. Paste data or upload a file.")
    sample = "\n".join(lines[:30])
    if "\t" in sample:
        sep, sep_name = "\t", "tab"
    elif sample.count(";") > sample.count(","):
        sep, sep_name = ";", "semicolon"
    else:
        sep, sep_name = ",", "comma"
    comma_decimal = sep == ";"
    rows = list(csv.reader(lines, delimiter=sep))
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    if len(rows) == 1 and not any(_is_value(c, comma_decimal) for c in rows[0]):
        raise UserError("This table has column names but no values under them yet.")

    # The first row where most cells are values is where the data starts;
    # everything above it (up to 3 rows) is joined into the column names.
    start = None
    for i, r in enumerate(rows[:50]):
        filled = [c for c in r if str(c).strip() != ""]
        if filled and sum(_is_value(c, comma_decimal) for c in filled) >= max(1, 0.5 * len(filled)):
            start = i
            break
    if start is None:
        start = 1 if len(rows) > 1 else 0
    header_rows = rows[max(0, start - 4):start]
    names = _column_names(header_rows, width)
    seen = {}
    for j, n in enumerate(names):
        if n in seen:
            seen[n] += 1
            names[j] = "%s (%d)" % (n, seen[n])
        else:
            seen[n] = 1

    body = rows[start:]
    cols, kinds = {}, {}
    for j, name in enumerate(names):
        raw = [r[j] for r in body]
        filled = [c for c in raw if str(c).strip().lower() not in _MISSING]
        if not filled:
            continue
        nums = [_num(c, comma_decimal) for c in raw]
        clocks = [_clock(c) for c in raw]
        n_num = sum(x is not None for x in nums)
        n_clock = sum(x is not None for x in clocks)
        if n_clock and (n_clock + n_num) >= 0.6 * len(filled):
            vals = [c if c is not None else (n if n is not None else np.nan) for c, n in zip(clocks, nums)]
            cols[name], kinds[name] = np.array(vals, float), "time"
        elif n_num >= 0.6 * len(filled):
            cols[name], kinds[name] = np.array([x if x is not None else np.nan for x in nums], float), "number"
        else:
            cols[name], kinds[name] = [str(c).strip() for c in raw], "text"
    if not cols:
        raise UserError("This table has column names but no values under them yet." if header_rows and len(body) <= 1
                        and not any(str(c).strip() for r in body for c in r) else "No columns with data were found.")
    df = pd.DataFrame(cols)
    value_cols = [c for c in df.columns if kinds[c] != "text"]
    if value_cols:
        keep = df[value_cols].notna().any(axis=1)
        text_cols = [c for c in df.columns if kinds[c] == "text"]
        if text_cols:
            keep |= (df[text_cols] != "").any(axis=1)
        df = df[keep].reset_index(drop=True)
    info = {"kinds": kinds, "separator": sep_name, "header_rows": len(header_rows), "n_rows": len(df)}
    return df, info


_UNIT = re.compile(r"^(\(?[a-zA-Zµμ°%/·^²³0-9.\-]{1,12}\)?)$")


def _looks_like_units(row):
    cells = [str(c).strip() for c in row if str(c).strip()]
    return bool(cells) and all(_UNIT.match(c) and " " not in c and not c[0].isdigit() for c in cells) \
        and sum(len(c) for c in cells) / len(cells) <= 6


def _column_names(header_rows, width):
    """Names from up to four header rows, as LabChart, Prism and Excel write them.

    The row nearest the numbers gives the name; a units row (s, V, BPM, mL/min) is added in
    parentheses; rows above only add their text when it's needed to tell columns apart."""
    rows = [list(r) + [""] * (width - len(r)) for r in header_rows]
    units = [""] * width
    if len(rows) >= 2 and _looks_like_units(rows[-1]):
        units = [str(c).strip().strip("()") for c in rows[-1]]
        rows = rows[:-1]
    names = []
    for j in range(width):
        cells = [str(r[j]).strip() for r in rows]
        names.append(next((c for c in reversed(cells) if c), ""))
    filled = [n for n in names if n]
    if len(set(filled)) < len(filled) and len(rows) >= 2:
        # Some names repeat (e.g. Rep 1 under Group A and Group B): prefix every column with the
        # nearest row above that tells them apart. Merged cells repeat to the right.
        for r in reversed(rows[:-1]):
            row = [str(c).strip() for c in r]
            carried, last = [], ""
            for j, c in enumerate(row):
                last = c or (last if names[j] else "")
                carried.append(last)
            if len({carried[j] for j in range(width) if names[j] and carried[j]}) > 1:
                names = ["%s · %s" % (carried[j], names[j]) if names[j] and carried[j] and carried[j] != names[j] else names[j]
                         for j in range(width)]
                break
    out = []
    for j in range(width):
        n = names[j] or ("Column %d" % (j + 1))
        if units[j] and names[j]:
            n = "%s (%s)" % (n, units[j])
        out.append(n)
    return out


def _labels(df, col, info):
    """A column as display strings, so numeric group codes read as 1, 2, 3 rather than 1.0."""
    kind = info["kinds"].get(col)
    if kind == "number":
        return df[col].map(lambda v: _fmt(v) if isinstance(v, float) and math.isfinite(v) else "")
    if kind == "time":
        return df[col].map(lambda v: _fmt_clock(v) if isinstance(v, float) and math.isfinite(v) else "")
    return df[col].astype(str).str.strip()


def inspect(text):
    df, info = read_table(text)
    preview = []
    for _, row in df.head(8).iterrows():
        cells = []
        for c in df.columns:
            v = row[c]
            if info["kinds"][c] == "time" and isinstance(v, float) and np.isfinite(v):
                cells.append(_fmt_clock(v))
            elif isinstance(v, float):
                cells.append("" if not np.isfinite(v) else _fmt(v))
            else:
                cells.append(str(v))
        preview.append(cells)
    return {
        "columns": [{"name": c, "kind": info["kinds"][c]} for c in df.columns],
        "n_rows": info["n_rows"],
        "separator": info["separator"],
        "preview": preview,
    }


def excel_to_tables(b64):
    raw = base64.b64decode(b64)
    try:
        sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, header=None, dtype=str)
    except Exception as e:
        raise UserError("Couldn't read this Excel file (%s). Try saving it as .xlsx or CSV." % e)
    out = []
    for name, sdf in sheets.items():
        sdf = sdf.dropna(how="all").dropna(axis=1, how="all").fillna("")
        if sdf.empty:
            continue
        out.append({"name": str(name), "text": sdf.to_csv(sep="\t", index=False, header=False)})
    if not out:
        raise UserError("The workbook has no sheets with data.")
    return out


# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------

def _fmt(x):
    if x is None or not np.isfinite(x):
        return ""
    if x == 0:
        return "0"
    a = abs(x)
    if a >= 1e5 or a < 1e-3:
        return "%.3g" % x
    return "%.4g" % x


def _fmt_clock(sec):
    neg = sec < 0
    sec = abs(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    s_txt = ("%04.1f" % s) if abs(s - round(s)) > 1e-6 else "%02d" % round(s)
    txt = ("%d:%02d:%s" % (h, m, s_txt)) if h else ("%d:%s" % (m, s_txt))
    return "-" + txt if neg else txt


def fmt_p(p):
    if p is None or not np.isfinite(p):
        return "p = n/a"
    if p < 0.0001:
        return "p < 0.0001"
    if p < 0.001:
        return "p = %.4f" % p
    return "p = %.3f" % p


def stars(p):
    if p is None or not np.isfinite(p):
        return "ns"
    for cut, s in ((0.0001, "****"), (0.001, "***"), (0.01, "**"), (0.05, "*")):
        if p < cut:
            return s
    return "ns"


def _r(x, digits=6):
    """Round for JSON output (keeps NaN as None)."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return x
    if not np.isfinite(x):
        return None
    if x == 0:
        return 0.0
    return float("%.*g" % (digits, x))


def _holm(ps):
    ps = np.asarray(ps, float)
    n = len(ps)
    adj = np.empty(n)
    running = 0.0
    for rank, idx in enumerate(np.argsort(ps)):
        running = max(running, min(1.0, (n - rank) * ps[idx]))
        adj[idx] = running
    return adj


# ----------------------------------------------------------------------------
# Style
# ----------------------------------------------------------------------------

PALETTES = {
    "serpentine": ["#2F5D8A", "#D1495B", "#E9A23B", "#00798C", "#6B4E9B", "#7A7A7A", "#3E8E41", "#B5651D"],
    "okabe": ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9", "#F0E442", "#000000"],
    "tol": ["#4477AA", "#EE6677", "#228833", "#CCBB44", "#66CCEE", "#AA3377", "#BBBBBB"],
    "muted": ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3", "#937860", "#DA8BC3", "#8C8C8C"],
    "pastel": ["#8DA0CB", "#FC8D62", "#66C2A5", "#E78AC3", "#A6D854", "#FFD92F", "#E5C494", "#B3B3B3"],
    "ocean": ["#023E8A", "#0096C7", "#48CAE4", "#03045E", "#00B4D8", "#90E0EF"],
    "sunset": ["#3D348B", "#F18701", "#7678ED", "#F35B04", "#F7B801", "#9B2226"],
    "earth": ["#5F7161", "#C9A66B", "#8C5E58", "#A3B18A", "#3A5A40", "#DDB892"],
    "neon": ["#00F5D4", "#F15BB5", "#FEE440", "#00BBF9", "#9B5DE5", "#F72585"],
    "vivid": ["#E64B35", "#4DBBD5", "#00A087", "#3C5488", "#F39B7F", "#8491B4", "#91D1C2", "#7E6148"],
    "jewel": ["#1B4965", "#9E2A2B", "#2D6A4F", "#7B2CBF", "#C9A227", "#0F7173"],
    "retro": ["#E07A5F", "#3D405B", "#81B29A", "#F2CC8F", "#6D597A", "#B56576"],
    "candy": ["#FF6B9A", "#5CC8FF", "#FFC145", "#8E7DFF", "#3DDC97", "#FF8C61"],
    "forest": ["#264027", "#6F732F", "#B38A58", "#3B6064", "#55828B", "#87BBA2"],
    "grayscale": ["#1F1F1F", "#8A8A8A", "#C4C4C4", "#525252", "#A8A8A8", "#6E6E6E"],
}
QUALITATIVE_CMAPS = {"tab10", "tab20", "tab20b", "tab20c", "Set1", "Set2", "Set3", "Dark2", "Paired",
                     "Accent", "Pastel1", "Pastel2"}

# Fonts that ship with matplotlib, plus web fonts the app downloads into the engine on demand.
FONTS = {
    "sans": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
    "serif": ["Times New Roman", "Times", "Liberation Serif", "DejaVu Serif"],
    "mono": ["Menlo", "Consolas", "DejaVu Sans Mono"],
    "lato": ["Lato", "DejaVu Sans"],
    "fira": ["Fira Sans", "DejaVu Sans"],
    "barlow": ["Barlow", "DejaVu Sans"],
    "poppins": ["Poppins", "DejaVu Sans"],
    "ptserif": ["PT Serif", "DejaVu Serif"],
    "crimson": ["Crimson Text", "DejaVu Serif"],
    "courier": ["Courier Prime", "DejaVu Sans Mono"],
    "spacemono": ["Space Mono", "DejaVu Sans Mono"],
    "comic": ["Comic Neue", "DejaVu Sans"],
}

# Everything about how a figure looks (presets reset these). Content fields such as titles,
# axis limits and per-group settings live in STYLE_DEFAULTS only.
LOOK_DEFAULTS = {
    "width": 4.0, "height": 3.4, "theme": "light", "bg_color": "", "axes_color": "", "ink_color": "",
    "font": "sans", "font_size": 10, "title_size": "", "label_size": "", "tick_size": "",
    "title_bold": True, "label_bold": False, "title_align": "center", "title_italic": False, "label_italic": False,
    "tick_italic": False, "legend_size": "",
    "palette": "serpentine", "palette_reverse": False,
    "plot_kind": "bar", "orientation": "vertical", "bar_width": 0.62, "jitter": 0.12,
    "fill": "light", "fill_alpha": 0.25, "edge_width": "",
    "show_points": True, "point_shape": "o", "marker_size": 5.0, "point_alpha": 0.85,
    "point_edge": "auto", "point_edge_width": 0.5,
    "line_width": 1.5, "axis_width": "", "error": "sem", "error_dir": "both", "cap_size": "", "error_width": "",
    "line_markers": False, "band_alpha": 0.18, "trace_alpha": 0.35,
    "sig": "stars", "show_ns": False, "sig_size": "", "show_n": False,
    "spine_left": True, "spine_bottom": True, "spine_top": False, "spine_right": False,
    "offset_axes": 0, "trim_axes": False, "tick_dir": "out", "tick_length": 3.5, "minor_ticks": False,
    "cat_ticks": False, "grid": "none", "grid_style": "solid", "grid_alpha": 0.15,
    "legend": "best", "legend_frame": False, "legend_cols": 1,
    "bar_shape": "square", "shadow": "none", "stripes": False, "value_labels": "none", "box_notch": False, "show_mean": False,
}
STYLE_DEFAULTS = dict(LOOK_DEFAULTS, **{
    "custom_colors": "", "groups": {}, "title": "", "xlabel": "", "ylabel": "", "legend_title": "",
    "ymin": "", "ymax": "", "xmin": "", "xmax": "", "logy": False, "logx": False,
    "ytick_step": "", "xtick_step": "", "y_nticks": "", "x_nticks": "", "y_decimals": "", "xrot": 0, "panel_label": "", "ref_lines": "",
    "endpoint_width": "", "dpi": 300, "transparent": False, "custom_rc": "", "custom_code": "",
})

NUMERIC = {  # key: (default, low, high); default None means "derived from another setting"
    "width": (4.0, 1.0, 30), "height": (3.4, 1.0, 30), "font_size": (10, 4, 48),
    "title_size": (None, 4, 72), "label_size": (None, 4, 72), "tick_size": (None, 3, 72),
    "line_width": (1.5, 0.1, 12), "axis_width": (None, 0.1, 12), "marker_size": (5.0, 0.5, 40),
    "point_alpha": (0.85, 0, 1), "fill_alpha": (0.25, 0, 1), "bar_width": (0.62, 0.05, 1.0),
    "jitter": (0.12, 0, 0.45), "edge_width": (None, 0, 12), "point_edge_width": (0.5, 0, 6),
    "cap_size": (None, 0, 30), "error_width": (None, 0.1, 12), "sig_size": (None, 4, 72),
    "offset_axes": (0, 0, 60), "tick_length": (3.5, 0, 25), "xrot": (0, -90, 90), "legend_cols": (1, 1, 12),
    "grid_alpha": (0.15, 0, 1), "band_alpha": (0.18, 0, 1), "trace_alpha": (0.35, 0, 1), "dpi": (300, 50, 2400),
    "endpoint_width": (None, 1.0, 30), "legend_size": (None, 4, 72), "y_nticks": (None, 2, 30), "x_nticks": (None, 2, 30),
}


def _color_or(c, fallback):
    c = (c or "").strip() if isinstance(c, str) else c
    if not c:
        return fallback
    try:
        to_rgb(c)
        return c
    except (ValueError, TypeError):
        return fallback


def _parse_rc(text, notes):
    out = {}
    for line in str(text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line and "=" not in line:
            notes.append('Matplotlib setting "%s" needs the form key: value.' % line)
            continue
        key, val = re.split(r"\s*[:=]\s*", line, maxsplit=1)
        key = key.strip()
        if key not in matplotlib.rcParams:
            notes.append('"%s" isn\'t a matplotlib setting.' % key)
            continue
        try:
            out[key] = matplotlib.rcParams.validate[key](val.strip().strip("'\""))
        except Exception as e:
            notes.append('Matplotlib setting %s: %s' % (key, e))
    return out


def _style(style):
    raw = dict(style or {})
    # Older projects used a single despine switch and a boolean grid.
    if "despine" in raw and "spine_top" not in raw:
        raw["spine_top"] = raw["spine_right"] = not raw.get("despine", True)
    if isinstance(raw.get("grid"), bool):
        raw["grid"] = "both" if raw["grid"] else "none"
    s = dict(STYLE_DEFAULTS)
    for k, v in raw.items():
        if v is not None:
            s[k] = v
    for k, (default, lo, hi) in NUMERIC.items():
        try:
            v = float(s.get(k))
            s[k] = min(max(v, lo), hi) if math.isfinite(v) else default
        except (TypeError, ValueError):
            s[k] = default
    fs = s["font_size"]
    lw = s["line_width"]
    s["title_size"] = s["title_size"] or fs * 1.15
    s["label_size"] = s["label_size"] or fs
    s["tick_size"] = s["tick_size"] or fs * 0.9
    s["sig_size"] = s["sig_size"] or fs
    s["axis_width"] = s["axis_width"] or max(0.6, lw * 0.6)
    s["edge_width"] = lw if s["edge_width"] is None else s["edge_width"]
    s["error_width"] = s["error_width"] or lw
    s["cap_size"] = s["marker_size"] * 0.9 if s["cap_size"] is None else s["cap_size"]
    dark = s["theme"] == "dark"
    s["fg"] = _color_or(s["ink_color"], "#E6E8EC" if dark else "#1A1D23")
    s["bg"] = _color_or(s["bg_color"], "#15181D" if dark else "#FFFFFF")
    s["ax_bg"] = _color_or(s["axes_color"], s["bg"])
    if not isinstance(s.get("groups"), dict):
        s["groups"] = {}
    s["_notes"] = []
    s["_rc"] = _parse_rc(s.get("custom_rc"), s["_notes"])
    return s


def _colors(s, n):
    n = max(n, 1)
    pal = str(s.get("palette") or "serpentine")
    if pal == "lablens":  # name before the rename
        pal = "serpentine"
    if pal.startswith("cmap:"):
        name = pal[5:]
        try:
            cmap = matplotlib.colormaps[name]
        except KeyError:
            cmap = matplotlib.colormaps["viridis"]
            name = "viridis"
        if name in QUALITATIVE_CMAPS:
            base = [to_hex(cmap(i % cmap.N)) for i in range(n)]
        else:
            base = [to_hex(cmap(x)) for x in (np.linspace(0.12, 0.88, n) if n > 1 else [0.5])]
    else:
        src = PALETTES.get(pal, PALETTES["serpentine"])
        base = [src[i % len(src)] for i in range(n)]
    if s.get("palette_reverse"):
        base = base[::-1]
    custom = [c.strip() for c in re.split(r"[,\s]+", s.get("custom_colors") or "") if c.strip()]
    for i, c in enumerate(custom[:n]):
        base[i] = _color_or(c, base[i])
    return base


def _gstyles(s, names):
    """Per-group colour, marker, hatch, line style and display label."""
    cols = _colors(s, len(names))
    out = []
    for i, nm in enumerate(names):
        g = s["groups"].get(str(nm)) or {}
        if not isinstance(g, dict):
            g = {}
        out.append({
            "name": nm,
            "label": (g.get("label") or "").strip() or str(nm),
            "color": _color_or(g.get("color"), cols[i]),
            "marker": g.get("marker") or s["point_shape"] or "o",
            "hatch": g.get("hatch") or None,
            "ls": g.get("linestyle") or "-",
        })
    return out


def _rc(s):
    fs = s["font_size"]
    tl = s["tick_length"]
    aw = s["axis_width"]
    rc = {
        "font.family": FONTS.get(s.get("font"), FONTS["sans"]),
        "font.size": fs,
        "axes.titlesize": s["title_size"],
        "axes.titleweight": "bold" if s.get("title_bold") else "normal",
        "axes.titlelocation": s["title_align"] if s["title_align"] in ("left", "center", "right") else "center",
        "axes.labelsize": s["label_size"],
        "axes.labelweight": "bold" if s.get("label_bold") else "normal",
        "xtick.labelsize": s["tick_size"],
        "ytick.labelsize": s["tick_size"],
        "legend.fontsize": s["legend_size"] or fs * 0.85,
        "legend.title_fontsize": fs * 0.9,
        "axes.linewidth": aw,
        "xtick.major.width": aw, "ytick.major.width": aw,
        "xtick.minor.width": aw * 0.7, "ytick.minor.width": aw * 0.7,
        "xtick.major.size": tl, "ytick.major.size": tl,
        "xtick.minor.size": tl * 0.55, "ytick.minor.size": tl * 0.55,
        "xtick.direction": s["tick_dir"] if s["tick_dir"] in ("in", "out", "inout") else "out",
        "ytick.direction": s["tick_dir"] if s["tick_dir"] in ("in", "out", "inout") else "out",
        "axes.edgecolor": s["fg"], "axes.labelcolor": s["fg"], "text.color": s["fg"],
        "xtick.color": s["fg"], "ytick.color": s["fg"],
        "figure.facecolor": s["bg"], "axes.facecolor": s["ax_bg"], "savefig.facecolor": s["bg"],
        "axes.grid": s["grid"] in ("x", "y", "both"),
        "axes.grid.axis": s["grid"] if s["grid"] in ("x", "y", "both") else "both",
        "grid.color": s["fg"], "grid.alpha": s["grid_alpha"], "grid.linewidth": max(0.4, aw * 0.8),
        "grid.linestyle": {"dashed": "--", "dotted": ":"}.get(s["grid_style"], "-"),
        "axes.axisbelow": True,
        "legend.frameon": bool(s["legend_frame"]), "legend.edgecolor": s["fg"], "legend.facecolor": s["ax_bg"],
        "legend.framealpha": 0.9,
        "hatch.color": s["fg"], "hatch.linewidth": max(0.5, s["line_width"] * 0.5),
        "svg.fonttype": "none", "pdf.fonttype": 42,
    }
    rc.update(s.get("_rc") or {})
    return rc


def register_fonts(paths):
    for p in paths:
        try:
            font_manager.fontManager.addfont(p)
        except Exception:
            pass
    cache = getattr(font_manager.fontManager, "_findfont_cached", None)
    if cache is not None and hasattr(cache, "cache_clear"):
        cache.cache_clear()
    return True


def _new_fig(s, width=None):
    with plt.rc_context(_rc(s)):
        fig, ax = plt.subplots(figsize=(width or s["width"], s["height"]))
    return fig, ax


def _shade(color, amount):
    """amount < 0 darkens, > 0 lightens."""
    r, g, b = to_rgb(color)
    if amount < 0:
        f = 1 + amount
        return (r * f, g * f, b * f)
    return (r + (1 - r) * amount, g + (1 - g) * amount, b + (1 - b) * amount)


def _point_edge(s, face):
    pe = s.get("point_edge") or "auto"
    if pe == "auto":
        return to_rgba(s["bg"], 0.9)
    if pe == "none":
        return "none"
    if pe == "dark":
        return _shade(face, -0.45)
    if pe == "ink":
        return s["fg"]
    return _color_or(pe, to_rgba(s["bg"], 0.9))


# Plain numbers (0.1, 1, 10) on log axes; scientific superscripts read poorly as editable SVG text.
_LOG_FMT = FuncFormatter(lambda v, _: ("%g" % v) if 1e-4 <= v < 1e6 else ("%.0e" % v))


def _limit(get, set_, lo_txt, hi_txt, log=False):
    lo, hi = get()
    try:
        lo = float(lo_txt) if str(lo_txt).strip() != "" else lo
    except ValueError:
        pass
    try:
        hi = float(hi_txt) if str(hi_txt).strip() != "" else hi
    except ValueError:
        pass
    if hi > lo and not (log and lo <= 0):  # a log axis can't start at 0 or below; leave its automatic limits
        set_(lo, hi)


def _finish(ax, s, xlabel="", ylabel="", title="", legend=False, title_pad=None):
    """Labels, scales, limits, ticks and legend. `ax._ll_cat` marks a category axis ('x' or 'y')."""
    cat = getattr(ax, "_ll_cat", None)
    ax.set_xlabel(s["xlabel"] or xlabel)
    ax.set_ylabel(s["ylabel"] or ylabel)
    t = s["title"] or title
    if t:
        ax.set_title(t, pad=title_pad or 10)
    value_axis = "x" if cat == "y" else "y"
    if s["logy"]:
        (ax.set_xscale if value_axis == "x" else ax.set_yscale)("log")
    if s["logx"] and cat is None:
        ax.set_xscale("log")
    for axis, get in ((ax.xaxis, ax.get_xlim), (ax.yaxis, ax.get_ylim)):
        if axis.get_scale() == "log":
            axis.set_major_formatter(_LOG_FMT)
            lo, hi = sorted(get())
            decades = math.log10(hi / lo) if lo > 0 and hi > 0 else 99
            if decades < 1.5:  # few or no powers of ten in view: label 2 and 5 times them as plain numbers
                axis.set_minor_locator(LogLocator(base=10, subs=(2.0, 5.0)))
                axis.set_minor_formatter(_LOG_FMT)
            else:
                axis.set_minor_formatter(NullFormatter())
    if cat != "y":
        _limit(ax.get_ylim, ax.set_ylim, s["ymin"], s["ymax"], ax.get_yscale() == "log")
    else:
        _limit(ax.get_ylim, lambda lo, hi: None, "", "")
    if cat != "x":
        _limit(ax.get_xlim, ax.set_xlim, s["xmin"], s["xmax"], ax.get_xscale() == "log")
    for key, axis, numeric in (("ytick_step", ax.yaxis, cat != "y"), ("xtick_step", ax.xaxis, cat != "x")):
        try:
            step = float(s[key])
            if numeric and step > 0 and axis.get_scale() == "linear":
                axis.set_major_locator(MultipleLocator(step))
        except (TypeError, ValueError):
            pass
    for key, axis, numeric in (("y_nticks", ax.yaxis, cat != "y"), ("x_nticks", ax.xaxis, cat != "x")):
        n_ticks = s.get(key)
        step_set = str(s.get("ytick_step" if key == "y_nticks" else "xtick_step") or "").strip() != ""
        if n_ticks and numeric and not step_set and axis.get_scale() == "linear":
            axis.set_major_locator(MaxNLocator(nbins=max(1, int(n_ticks) - 1), steps=[1, 2, 2.5, 5, 10]))
    try:
        dec = int(float(s["y_decimals"]))
        vaxis = ax.xaxis if value_axis == "x" else ax.yaxis
        if 0 <= dec <= 8 and vaxis.get_scale() == "linear":
            vaxis.set_major_formatter(FormatStrFormatter("%%.%df" % dec))
    except (TypeError, ValueError):
        pass
    if s["minor_ticks"]:
        ax.minorticks_on()
        if cat:
            ax.tick_params(axis=cat, which="minor", length=0, width=0)
    if cat and not s["cat_ticks"]:
        ax.tick_params(axis=cat, which="major", length=0)
    if s["xrot"]:
        for lab in ax.get_xticklabels():
            lab.set_rotation(s["xrot"])
            lab.set_ha("right" if 0 < s["xrot"] < 90 else ("left" if -90 < s["xrot"] < 0 else "center"))
            lab.set_rotation_mode("anchor")
    if s.get("title_italic"):
        ax.title.set_fontstyle("italic")
    if s.get("label_italic"):
        ax.xaxis.label.set_fontstyle("italic")
        ax.yaxis.label.set_fontstyle("italic")
    if s.get("tick_italic") and cat:  # group names on the category axis, not the numbers
        for lab in (ax.get_xticklabels() if cat == "x" else ax.get_yticklabels()):
            lab.set_fontstyle("italic")
    if legend and s["legend"] != "none":
        handles, labels = getattr(ax, "_ll_legend", None) or ax.get_legend_handles_labels()
        if handles:
            kw = {"ncol": int(s["legend_cols"]), "title": s["legend_title"] or None, "frameon": bool(s["legend_frame"])}
            if s["legend"] == "outside":
                ax.legend(handles, labels, loc="center left", bbox_to_anchor=(1.02, 0.5), **kw)
            elif s["legend"] == "outside top":
                kw["ncol"] = max(kw["ncol"], len(handles))
                ax.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, 1.02), **kw)
            else:
                ax.legend(handles, labels, loc=s["legend"], **kw)
            if s.get("tick_italic") and ax.get_legend():
                for t in ax.get_legend().get_texts():
                    t.set_fontstyle("italic")


def _parse_ref_lines(text):
    out = []
    for part in re.split(r"[;\n]", str(text or "")):
        part = part.strip()
        if not part:
            continue
        label = ""
        if ":" in part:
            part, label = [x.strip() for x in part.split(":", 1)]
        m = re.match(r"^([xy])\s*=\s*(x|-?[\d.eE+-]+)$", part.replace(" ", ""))
        if not m:
            raise UserError('Couldn\'t read the reference line "%s". Use y=100, x=0 or y=x.' % part)
        out.append((m.group(1), m.group(2), label))
    return out


def _stripes(ax, s, cat):
    """Zebra bands behind the data: one per group on a category axis, otherwise between value ticks."""
    xl, yl = ax.get_xlim(), ax.get_ylim()
    kw = dict(color=to_rgba(s["fg"], 0.05 if s["theme"] != "dark" else 0.07), lw=0, zorder=0)
    if cat:
        n = len(ax.get_xticks() if cat == "x" else ax.get_yticks())
        for k in range(0, n, 2):
            (ax.axvspan if cat == "x" else ax.axhspan)(k - 0.5, k + 0.5, **kw)
    else:
        lo, hi = sorted(yl)
        tk = [t for t in ax.get_yticks() if lo <= t <= hi]
        for a, b in zip(tk[0::2], tk[1::2]):
            ax.axhspan(a, b, **kw)
    if ax.get_xscale() != "log" or min(xl) > 0:
        ax.set_xlim(xl)
    if ax.get_yscale() != "log" or min(yl) > 0:
        ax.set_ylim(yl)


def _round_bars(ax, s):
    bars = [p for p in ax.patches if getattr(p, "_ll_bar", False)]
    H = s["orientation"] == "horizontal"
    if not bars or (ax.get_xscale() if H else ax.get_yscale()) != "linear":
        return
    o = ax.transData.transform((0, 0))
    sx = abs(ax.transData.transform((1, 0))[0] - o[0]) or 1.0
    sy = abs(ax.transData.transform((0, 1))[1] - o[1]) or 1.0
    for p in bars:
        x, y, w, h = p.get_x(), p.get_y(), p.get_width(), p.get_height()
        if w < 0:
            x, w = x + w, -w
        if h < 0:
            y, h = y + h, -h
        if w * sx < 1 or h * sy < 1:
            continue
        thick = h * sy if H else w * sx
        r = min(thick * 0.28, w * sx / 2, h * sy / 2)  # corner radius in pixels
        nb = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=%.6g" % float(r / sx), mutation_aspect=sx / sy,
                            facecolor=p.get_facecolor(), edgecolor=p.get_edgecolor(), linewidth=p.get_linewidth(),
                            hatch=p.get_hatch(), zorder=p.get_zorder())
        p.remove()
        ax.add_patch(nb)


def _effects(ax, s):
    mode = s.get("shadow") or "none"
    if mode not in ("drop", "glow"):
        return
    dark = s["theme"] == "dark"
    arts = list(ax.lines) + list(ax.patches) + list(ax.collections)
    for a in arts:
        try:
            if a.get_zorder() < 1.5:  # bands, shading and stripes stay flat
                continue
            if mode == "drop":
                if hasattr(a, "get_xydata"):
                    fx = [patheffects.SimpleLineShadow(offset=(1.2, -1.2), alpha=0.45 if dark else 0.22, shadow_color="black"), patheffects.Normal()]
                else:
                    fx = [patheffects.SimplePatchShadow(offset=(1.6, -1.6), alpha=0.5 if dark else 0.2, shadow_rgbFace="black"), patheffects.Normal()]
            else:
                if hasattr(a, "get_xydata"):
                    c, w = a.get_color(), a.get_linewidth()
                elif hasattr(a, "get_sizes"):  # scatter
                    fcs = a.get_facecolor()
                    c, w = (fcs[0] if len(fcs) else s["fg"]), s["marker_size"] * 0.8
                else:
                    c, w = a.get_edgecolor(), max(a.get_linewidth(), s["line_width"])
                    if hasattr(c, "ndim") and getattr(c, "ndim", 1) > 1:
                        c = c[0] if len(c) else s["fg"]
                if to_rgba(c)[3] * (a.get_alpha() if a.get_alpha() is not None else 1) < 0.5:
                    continue  # faint session traces would turn into fog
                c = to_rgb(c)
                fx = [patheffects.Stroke(linewidth=w * 5, foreground=c, alpha=0.07),
                      patheffects.Stroke(linewidth=w * 2.6, foreground=c, alpha=0.18), patheffects.Normal()]
            a.set_path_effects(fx)
        except Exception:
            continue


def _polish(fig, s, notes):
    """Final pass on every axes: axis lines, offsets, trimming, reference lines, panel letter."""
    for i, ax in enumerate(fig.axes):
        cat = getattr(ax, "_ll_cat", None)
        if s.get("stripes"):
            _stripes(ax, s, cat)
        if s.get("bar_shape") == "rounded":
            _round_bars(ax, s)
        _effects(ax, s)
        try:
            for axis, val, label in _parse_ref_lines(s["ref_lines"]):
                kw = dict(color=to_rgba(s["fg"], 0.55), lw=max(0.6, s["line_width"] * 0.6), ls=(0, (4, 3)), zorder=1)
                if val == "x":
                    (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
                    lo, hi = max(min(x0, y0), 1e-12) if ax.get_xscale() == "log" else min(x0, y0), max(x1, y1)
                    ax.axline((lo, lo), (hi, hi), **kw)  # two points work on log axes too
                    ax.set_xlim(x0, x1)
                    ax.set_ylim(y0, y1)
                    continue
                v = float(val)
                if axis == "y":
                    ax.axhline(v, **kw)
                    if label:
                        ax.annotate(label, xy=(1, v), xycoords=("axes fraction", "data"), xytext=(-3, 3),
                                    textcoords="offset points", ha="right", va="bottom",
                                    fontsize=s["font_size"] * 0.8, color=to_rgba(s["fg"], 0.75))
                else:
                    ax.axvline(v, **kw)
                    if label:
                        ax.annotate(label, xy=(v, 1), xycoords=("data", "axes fraction"), xytext=(3, -3),
                                    textcoords="offset points", ha="left", va="top",
                                    fontsize=s["font_size"] * 0.8, color=to_rgba(s["fg"], 0.75))
        except UserError as e:
            if i == 0:
                notes.append(str(e))
        for side in ("left", "bottom", "top", "right"):
            sp = ax.spines[side]
            sp.set_visible(bool(s["spine_" + side]))
            sp.set_color(s["fg"])
            if s["offset_axes"]:
                sp.set_position(("outward", s["offset_axes"]))
        if not s["spine_top"] and not s["spine_bottom"]:
            ax.tick_params(axis="x", which="both", bottom=False, top=False)
        elif s["spine_top"] and not s["spine_bottom"]:
            ax.tick_params(axis="x", which="both", bottom=False, top=True, labelbottom=True)
        if not s["spine_left"] and not s["spine_right"]:
            ax.tick_params(axis="y", which="both", left=False, right=False)
        elif s["spine_right"] and not s["spine_left"]:
            ax.tick_params(axis="y", which="both", left=False, right=True, labelleft=True)
        if s["trim_axes"]:
            for which, sides, get, ticks in (("x", ("bottom", "top"), ax.get_xlim, ax.get_xticks),
                                             ("y", ("left", "right"), ax.get_ylim, ax.get_yticks)):
                lo, hi = sorted(get())
                tk = [t for t in ticks() if lo - 1e-9 <= t <= hi + 1e-9]
                if cat == which:
                    tk = [t for t in tk if float(t).is_integer()] or tk
                if len(tk) >= 2:
                    for side in sides:
                        ax.spines[side].set_bounds(min(tk), max(tk))
        if i == 0 and str(s["panel_label"]).strip():
            ax.annotate(str(s["panel_label"]).strip(), xy=(0, 1), xycoords="axes fraction",
                        xytext=(-s["font_size"] * 3.4, s["font_size"] * 0.8), textcoords="offset points",
                        ha="left", va="bottom", fontsize=s["font_size"] * 1.6, fontweight="bold", color=s["fg"])


# ----------------------------------------------------------------------------
# Group plots + statistics
# ----------------------------------------------------------------------------

def _err(v, kind):
    v = v[np.isfinite(v)]
    n = len(v)
    if n < 2 or kind == "none":
        return 0.0
    sd = np.std(v, ddof=1)
    if kind == "sd":
        return sd
    sem = sd / math.sqrt(n)
    if kind == "ci95":
        return sem * stats.t.ppf(0.975, n - 1)
    return sem


ERROR_NAMES = {"sem": "SEM", "sd": "SD", "ci95": "95% CI", "none": ""}
PLOT_KINDS = ("bar", "dot", "mean", "box", "violin", "boxviolin", "raincloud", "beeswarm", "strip", "lollipop")


def _swarm_offsets(v, s, n_groups, value_span, horizontal):
    """Beeswarm: nudge points sideways just enough that markers don't overlap."""
    value_len = (s["width"] if horizontal else s["height"]) * 72 * 0.72
    cat_len = (s["height"] if horizontal else s["width"]) * 72 * 0.72
    d_val = s["marker_size"] * 1.08 / value_len * value_span
    d_cat = s["marker_size"] * 1.08 / (cat_len / (n_groups + 0.2))
    offs = np.zeros(len(v))
    placed = []
    for idx in np.argsort(np.where(np.isfinite(v), v, np.inf)):
        val = v[idx]
        if not np.isfinite(val):
            continue
        near = [(pv, po) for pv, po in placed if abs(pv - val) < d_val]
        cand = 0.0
        for k in range(400):
            cand = 0.0 if k == 0 else ((k + 1) // 2) * d_cat * (1 if k % 2 else -1)
            if all(((val - pv) / d_val) ** 2 + ((cand - po) / d_cat) ** 2 >= 1 for pv, po in near):
                break
        offs[idx] = cand
        placed.append((val, cand))
    widest = np.max(np.abs(offs)) if len(offs) else 0
    if widest > 0.42:
        offs *= 0.42 / widest
    return offs


def plot_groups(ax, names, values, s, paired_matrix=None, positions=None, styles=None, ticks=None):
    """Draw one category per group. values: list of 1-D arrays. Returns the top of each group.

    For grouped (clustered) figures pass `positions` (x of each group), `styles` (per-group style dicts,
    e.g. coloured by the second factor) and `ticks` ((positions, labels) of the category axis)."""
    G = styles or _gstyles(s, names)
    n = len(names)
    H = s["orientation"] == "horizontal"
    kind = s["plot_kind"] if s["plot_kind"] in PLOT_KINDS else "bar"
    fg, bg = s["fg"], s["bg"]
    lw, ew, ms, bw = s["line_width"], s["edge_width"], s["marker_size"], s["bar_width"]
    rng = np.random.default_rng(7)
    cats = np.arange(n, dtype=float) if positions is None else np.asarray(positions, float)
    vals = [np.asarray(v, float) for v in values]
    finite_all = np.concatenate([v[np.isfinite(v)] for v in vals]) if vals else np.array([])
    span = float(np.ptp(finite_all)) * 1.2 if len(finite_all) > 1 and np.ptp(finite_all) > 0 else 1.0

    def XY(c, v):
        return (v, c) if H else (c, v)

    def face(g):
        if s["fill"] == "solid":
            return to_rgba(g["color"], 0.92)
        if s["fill"] == "outline":
            return (0, 0, 0, 0)
        return to_rgba(g["color"], s["fill_alpha"])

    def summary_color(g):
        return fg if s["fill"] == "solid" else g["color"]

    def errbar(c, m, e, color):
        if not e or s["error"] == "none":
            return
        if s["error_dir"] == "up":
            arr = [[0], [e]] if m >= 0 else [[e], [0]]
        else:
            arr = e
        kw = dict(fmt="none", ecolor=color, elinewidth=s["error_width"], capsize=s["cap_size"],
                  capthick=s["error_width"], zorder=4)
        if H:
            ax.errorbar(m, c, xerr=arr, **kw)
        else:
            ax.errorbar(c, m, yerr=arr, **kw)

    show_pts = bool(s["show_points"]) or kind in ("dot", "raincloud", "beeswarm", "strip")
    point_c = []
    for i, v in enumerate(vals):
        center = cats[i] + (0.24 if kind == "raincloud" else 0.0)
        if paired_matrix is not None:
            pc = np.full(len(v), center)
        elif kind == "beeswarm":
            pc = center + _swarm_offsets(v, s, n, span, H)
        else:
            w = 0.05 if kind == "raincloud" else (s["jitter"] * 1.35 if kind in ("dot", "strip") else s["jitter"])
            pc = center + rng.uniform(-w, w, len(v))
        point_c.append(pc)

    tops = []
    half = 0.36 * bw / 0.62
    for i, (v, g) in enumerate(zip(vals, G)):
        fin = v[np.isfinite(v)]
        c = cats[i]
        col = g["color"]
        if not len(fin):
            tops.append(0.0)
            continue
        m = float(np.mean(fin))
        e = _err(fin, s["error"])
        top = [m + e, float(fin.max())]
        if kind == "bar":
            kw = dict(color=face(g), edgecolor=col, linewidth=ew, hatch=g["hatch"], zorder=2)
            bars = ax.barh(c, m, height=bw, **kw) if H else ax.bar(c, m, width=bw, **kw)
            for b in bars:
                b._ll_bar = True  # rounded later, once the axis limits are final
            errbar(c, m, e, summary_color(g))
        elif kind == "box":
            bp = ax.boxplot([fin], positions=[c], widths=bw * 0.8, patch_artist=True, vert=not H,
                            showfliers=not show_pts, notch=bool(s.get("box_notch")) and len(fin) > 2, zorder=2)
            for b in bp["boxes"]:
                b.set(facecolor=face(g), edgecolor=col, linewidth=ew, hatch=g["hatch"])
            for part in ("whiskers", "caps"):
                for ln in bp[part]:
                    ln.set(color=col, linewidth=ew)
            for ln in bp["medians"]:
                ln.set(color=summary_color(g), linewidth=max(ew, 0.5) * 1.4)
            for f in bp["fliers"]:
                f.set(marker=g["marker"], markeredgecolor=col, markersize=ms * 0.8)
        elif kind in ("violin", "raincloud", "boxviolin"):
            if len(fin) >= 2 and np.ptp(fin) > 0:
                width = {"violin": 1.2, "raincloud": 1.45, "boxviolin": 1.3}[kind] * bw
                vp = ax.violinplot([fin], positions=[c], widths=width, showextrema=False, vert=not H)
                for body in vp["bodies"]:
                    body.set_facecolor(face(g))
                    body.set_edgecolor(col)
                    body.set_linewidth(max(ew, 0.3) * 0.8)
                    body.set_alpha(None)
                    if g["hatch"]:
                        body.set_hatch(g["hatch"])
                    if kind == "raincloud":
                        verts = body.get_paths()[0].vertices
                        k = 1 if H else 0
                        verts[:, k] = np.minimum(verts[:, k], c)
                    body.set_zorder(2)
            q1, med, q3 = np.percentile(fin, [25, 50, 75])
            if kind == "boxviolin":
                bp = ax.boxplot([fin], positions=[c], widths=0.12 * bw / 0.62, patch_artist=True, vert=not H,
                                showfliers=False, zorder=3)
                for b in bp["boxes"]:
                    b.set(facecolor=bg, edgecolor=fg, linewidth=max(ew, 0.5) * 0.8)
                for part in ("whiskers", "caps"):
                    for ln in bp[part]:
                        ln.set(color=fg, linewidth=max(ew, 0.5) * 0.8)
                for ln in bp["medians"]:
                    ln.set(color=fg, linewidth=max(ew, 0.5) * 1.4)
            else:
                bx = c if kind == "violin" else c + 0.07
                ax.plot(*XY([bx, bx], [q1, q3]), color=fg, linewidth=lw * 2.6, solid_capstyle="butt", zorder=3)
                ax.scatter(*XY([bx], [med]), s=(ms * 1.1) ** 2, color=bg, edgecolor=fg,
                           linewidth=lw * 0.6, zorder=4)
        elif kind == "dot":
            ax.plot(*XY([c - half, c + half], [m, m]), color=summary_color(g), linewidth=lw * 1.6,
                    solid_capstyle="round", zorder=6)
            errbar(c, m, e, summary_color(g))
        elif kind == "mean":
            errbar(c, m, e, col)
            ax.scatter(*XY([c], [m]), s=(ms * 2.0) ** 2, marker=g["marker"], color=col,
                       edgecolor=_point_edge(s, col), linewidth=max(0.5, s["point_edge_width"]), zorder=6)
        elif kind == "lollipop":
            base = 0.0
            if s["logy"]:
                base = float(fin.min()) * 0.5 if fin.min() > 0 else 1e-3
            ax.plot(*XY([c, c], [base, m]), color=col, linewidth=lw * 1.4, zorder=3)
            errbar(c, m, e, col)
            ax.scatter(*XY([c], [m]), s=(ms * 2.2) ** 2, marker=g["marker"], color=col,
                       edgecolor=_point_edge(s, col), linewidth=max(0.5, s["point_edge_width"]), zorder=6)
        elif kind == "beeswarm":
            med = float(np.median(fin))
            ax.plot(*XY([c - half, c + half], [med, med]), color=fg, linewidth=lw * 1.4, zorder=6)
        if s.get("show_mean") and kind in ("box", "violin", "boxviolin", "raincloud", "beeswarm", "strip"):
            ax.scatter(*XY([c + (0.07 if kind == "raincloud" else 0)], [m]), s=(ms * 1.25) ** 2, marker="D",
                       color=bg, edgecolor=fg, linewidth=max(0.6, lw * 0.6), zorder=7)
        tops.append(max(top))

    labels_kind = s.get("value_labels") or "none"
    if labels_kind in ("mean", "median", "n"):
        try:
            dec = int(float(s["y_decimals"]))
        except (TypeError, ValueError):
            dec = None
        for i, v in enumerate(vals):
            fin = v[np.isfinite(v)]
            if not len(fin):
                continue
            if labels_kind == "n":
                text = "n = %d" % len(fin)
            else:
                val = float(np.mean(fin) if labels_kind == "mean" else np.median(fin))
                text = ("%%.%df" % dec) % val if dec is not None and 0 <= dec <= 8 else "%.3g" % val
            off = (s["font_size"] * 0.35, 0) if H else (0, s["font_size"] * 0.35)
            ax.annotate(text, xy=XY(cats[i], tops[i]), xytext=off, textcoords="offset points",
                        ha="left" if H else "center", va="center" if H else "bottom",
                        fontsize=s["tick_size"] * 0.95, color=s["fg"], zorder=7, annotation_clip=False)
            tops[i] += span * (0.09 if not H else 0.14) * s["tick_size"] / 10  # keep brackets clear of the label

    if paired_matrix is not None and n > 1:
        line_c = to_rgba(fg, 0.28)
        for row in paired_matrix:
            ax.plot(*XY([point_c[j][0] for j in range(n)], row), color=line_c,
                    linewidth=max(0.6, lw * 0.6), zorder=3)

    if show_pts:
        for i, (v, g) in enumerate(zip(vals, G)):
            ok = np.isfinite(v)
            pface = _shade(g["color"], -0.35) if s["fill"] == "solid" and kind in ("bar", "box", "lollipop") else g["color"]
            ax.scatter(*XY(point_c[i][ok], v[ok]), s=ms ** 2, marker=g["marker"],
                       color=to_rgba(pface, s["point_alpha"]), edgecolor=_point_edge(s, pface),
                       linewidth=s["point_edge_width"], zorder=5)

    if ticks is not None:
        tpos, labels = ticks
        lo_c, hi_c = float(np.min(cats)) - 0.6 * bw / 0.62, float(np.max(cats)) + 0.6 * bw / 0.62
        if not H and not s["xrot"]:
            per_group_pt = s["width"] * 72 * 0.7 / max(1, len(tpos))
            chars = max(6, int(per_group_pt / (s["tick_size"] * 0.62)))
            labels = [textwrap.fill(str(lab), chars, break_long_words=False) for lab in labels]
        if H:
            ax.set_yticks(tpos)
            ax.set_yticklabels(labels)
            ax.set_ylim(hi_c, lo_c)
        else:
            ax.set_xticks(tpos)
            ax.set_xticklabels(labels)
            ax.set_xlim(lo_c, hi_c)
        ax._ll_cat = "y" if H else "x"
        if kind in ("bar", "lollipop") and not s["logy"] and len(finite_all) and finite_all.min() >= 0:
            (ax.set_xlim(left=0) if H else ax.set_ylim(bottom=0))
        return tops
    labels = [g["label"] for g in G]
    if not H and not s["xrot"]:
        per_group_pt = s["width"] * 72 * 0.7 / max(1, n)
        chars = max(6, int(per_group_pt / (s["tick_size"] * 0.62)))
        labels = [textwrap.fill(lab, chars, break_long_words=False) for lab in labels]
    if s["show_n"]:
        labels = ["%s\n(n=%d)" % (lab, np.isfinite(v).sum()) for lab, v in zip(labels, vals)]
    if H:
        ax.set_yticks(cats)
        ax.set_yticklabels(labels)
        ax.set_ylim(n - 0.4, -0.6)
    else:
        ax.set_xticks(cats)
        ax.set_xticklabels(labels)
        ax.set_xlim(-0.6, n - 0.4)
    ax._ll_cat = "y" if H else "x"
    if kind in ("bar", "lollipop") and not s["logy"] and len(finite_all) and finite_all.min() >= 0:
        if H:
            ax.set_xlim(left=0)
        else:
            ax.set_ylim(bottom=0)
    return tops


def draw_brackets(ax, comparisons, tops, s, positions=None):
    """comparisons: list of (i, j, p). Stacks brackets beyond the groups on the value axis."""
    P = list(range(len(tops))) if positions is None else list(positions)
    show = [(i, j, p) for i, j, p in comparisons if s["show_ns"] or (p is not None and p < 0.05)]
    if s["sig"] == "none" or not show or not tops:
        return 0
    if len(show) > 12:  # a wall of brackets helps no one; the table has them all
        return -len(show)
    H = s["orientation"] == "horizontal"
    log = s["logy"]
    tr = (lambda y: math.log10(max(y, 1e-12))) if log else (lambda y: y)
    inv = (lambda y: 10 ** y) if log else (lambda y: y)
    lo, hi = ax.get_xlim() if H else ax.get_ylim()
    lo_t = tr(lo) if log else lo
    hi_t = tr(max(hi, max(tops)))
    span = (hi_t - lo_t) or 1.0
    gap, tick = span * 0.06, span * 0.025
    text_h = span * (0.075 if s["sig"] == "p" else 0.065) * s["sig_size"] / max(s["font_size"], 1)
    if H and s["sig"] == "stars":
        text_h *= 1.6  # unrotated stars are wider than they are tall
    level = [tr(t) if (t > 0 or not log) else lo_t for t in tops]
    top_used = hi_t
    for i, j, p in sorted(show, key=lambda c: (abs(c[1] - c[0]), c[0])):
        a, b = min(i, j), max(i, j)
        y = max(level[a:b + 1]) + gap
        cs = [P[a], P[a], P[b], P[b]]
        vs = [inv(y - tick), inv(y), inv(y), inv(y - tick)]
        ax.plot(*((vs, cs) if H else (cs, vs)), color=s["fg"], linewidth=max(0.7, s["axis_width"]),
                clip_on=False, zorder=6)
        label = stars(p) if s["sig"] == "stars" else fmt_p(p)
        size = s["sig_size"] * (1.0 if s["sig"] == "stars" else 0.8)
        if H:
            ax.text(inv(y + span * 0.01), (P[a] + P[b]) / 2, label, ha="left", va="center",
                    rotation=-90 if s["sig"] == "p" else 0, fontsize=size, color=s["fg"], zorder=6)
        else:
            ax.text((P[a] + P[b]) / 2, inv(y + span * 0.005), label, ha="center", va="bottom",
                    fontsize=size, color=s["fg"], zorder=6)
        for k in range(a, b + 1):
            level[k] = y + text_h
        top_used = max(top_used, y + text_h + span * 0.02)
    if H and str(s["xmax"]).strip() == "":
        ax.set_xlim(right=inv(top_used))
    elif not H and str(s["ymax"]).strip() == "":
        ax.set_ylim(top=inv(top_used))
    return len(show)


def describe(names, values, labels=None):
    cols = ["Group", "n", "Mean", "SD", "SEM", "95% CI low", "95% CI high", "Median", "Q1", "Q3", "Min", "Max", "CV (%)"]
    rows = []
    for nm, v in zip(labels or names, values):
        v = np.asarray(v, float)
        v = v[np.isfinite(v)]
        n = len(v)
        if n == 0:
            rows.append([nm, 0] + [None] * 11)
            continue
        m = np.mean(v)
        sd = np.std(v, ddof=1) if n > 1 else np.nan
        sem = sd / math.sqrt(n) if n > 1 else np.nan
        h = sem * stats.t.ppf(0.975, n - 1) if n > 1 else np.nan
        q1, med, q3 = np.percentile(v, [25, 50, 75])
        cv = sd / abs(m) * 100 if n > 1 and m != 0 else np.nan
        rows.append([nm, n] + [_r(x) for x in (m, sd, sem, m - h, m + h, med, q1, q3, v.min(), v.max())] + [_r(cv, 4)])
    return {"title": "Descriptive statistics", "columns": cols, "rows": rows}


def _rm_anova(mat):
    n, k = mat.shape
    grand = mat.mean()
    ss_subj = k * ((mat.mean(axis=1) - grand) ** 2).sum()
    ss_cond = n * ((mat.mean(axis=0) - grand) ** 2).sum()
    ss_tot = ((mat - grand) ** 2).sum()
    ss_err = ss_tot - ss_subj - ss_cond
    df1, df2 = k - 1, (n - 1) * (k - 1)
    if ss_err <= 0 or df2 <= 0:
        return np.nan, df1, df2, np.nan
    F = (ss_cond / df1) / (ss_err / df2)
    return F, df1, df2, stats.f.sf(F, df1, df2)


def _article(test):
    return test[0].lower() + test[1:] if test.split(" ")[0] in ("One-way", "Repeated-measures", "Paired") else test


_STATS_CACHE = {}


def compare_stats(names, values, paired=False, mode="auto", posthoc="all", equal_var=False):
    """Pick and run the right test. Never raises on awkward data; problems become notes.

    Results are cached, so changing only the figure's look doesn't recompute the statistics."""
    key = (tuple(names), tuple(np.asarray(v, float).tobytes() for v in values), bool(paired), mode, posthoc, bool(equal_var))
    hit = _STATS_CACHE.get(key)
    if hit is not None:
        return dict(hit, notes=list(hit["notes"]), pairs=list(hit["pairs"]))
    res = _compare_stats_safe(names, values, paired, mode, posthoc, equal_var)
    if len(_STATS_CACHE) > 128:
        _STATS_CACHE.clear()
    _STATS_CACHE[key] = res
    return dict(res, notes=list(res["notes"]), pairs=list(res["pairs"]))


def _tukey_pairs(vals, pairs):
    """Tukey-Kramer p-values for the requested pairs only (SciPy's tukey_hsd computes all k*k)."""
    k = len(vals)
    ns = np.array([len(v) for v in vals], float)
    means = np.array([np.mean(v) for v in vals])
    df = ns.sum() - k
    mse = sum(((v - v.mean()) ** 2).sum() for v in vals) / df
    out = []
    for i, j in pairs:
        se = math.sqrt(mse / 2 * (1 / ns[i] + 1 / ns[j]))
        q = abs(means[i] - means[j]) / se if se > 0 else np.inf
        out.append(float(stats.studentized_range.sf(q, k, df)) if np.isfinite(q) else 0.0)
    return out


def _compare_stats_safe(names, values, paired=False, mode="auto", posthoc="all", equal_var=False):
    try:
        return _compare_stats(names, values, paired, mode, posthoc, equal_var)
    except (ValueError, FloatingPointError, ZeroDivisionError, np.linalg.LinAlgError) as e:
        return {"test": "", "line": "", "p": None, "pairs": [], "method": "", "parametric": True, "normality": [],
                "notes": ["The statistical test couldn't be computed for these values (%s). This usually means "
                          "a group has identical values or too few points." % e]}


def _compare_stats(names, values, paired, mode, posthoc, equal_var):
    k = len(names)
    out = {"test": "", "line": "", "p": None, "pairs": [], "notes": [], "method": "", "parametric": True,
           "normality": []}
    vals = [np.asarray(v, float) for v in values]
    if paired:
        mat = np.column_stack(vals)
        complete = np.isfinite(mat).all(axis=1)
        if complete.sum() < len(mat):
            out["notes"].append("%d subject(s) with missing values were left out of the paired test."
                                % (len(mat) - complete.sum()))
        mat = mat[complete]
        vals = [mat[:, j] for j in range(k)]
    else:
        vals = [v[np.isfinite(v)] for v in vals]
    ns = [len(v) for v in vals]
    if k < 2:
        out["notes"].append("Add a second group to run a statistical test.")
        return out
    if min(ns) < 2:
        out["notes"].append("Every group needs at least 2 values for a test.")
        return out
    if all(np.ptp(v) == 0 for v in vals) and len(set(float(v[0]) for v in vals)) == 1:
        out["notes"].append("All values are identical, so there is nothing to test.")
        return out

    norm_sets = [vals[1] - vals[0]] if (paired and k == 2) else vals
    normal = True
    for nm, v in zip(["Differences"] if (paired and k == 2) else names, norm_sets):
        if len(v) >= 3 and np.ptp(v) > 0:
            p = stats.shapiro(v).pvalue
            out["normality"].append((nm, p))
            normal = normal and p > 0.05
        else:
            out["normality"].append((nm, None))
    if mode == "param":
        parametric = True
    elif mode == "nonparam":
        parametric = False
    else:
        parametric = normal
        if min(ns) < 3:
            out["notes"].append("Normality can't be checked with fewer than 3 values per group; a parametric test was used.")
            parametric = True
    out["parametric"] = parametric
    if min(ns) < 5:
        out["notes"].append("Small samples (n < 5 in a group): treat p-values with caution and report individual points.")

    if k == 2:
        a, b = vals
        if paired and parametric:
            d = b - a
            sd = np.std(d, ddof=1)
            if not sd > 1e-12 * max(1.0, float(np.max(np.abs(d)))):
                out["notes"].append("Every subject changed by exactly the same amount (%s), so a paired t statistic "
                                    "can't be computed. Report the difference itself." % _fmt(float(np.mean(d))))
                return out
            r = stats.ttest_rel(b, a)
            dz = np.mean(d) / sd if sd > 0 else np.nan
            out.update(test="Paired t-test", p=r.pvalue,
                       line="t(%d) = %.3f, %s, Cohen's dz = %.2f" % (len(d) - 1, r.statistic, fmt_p(r.pvalue), dz))
        elif paired:
            if np.all(b - a == 0):
                out["notes"].append("Every pair has the same value in both groups, so the Wilcoxon test can't run.")
                return out
            r = stats.wilcoxon(b, a)
            out.update(test="Wilcoxon matched-pairs signed-rank test", p=r.pvalue,
                       line="W = %.1f, %s" % (r.statistic, fmt_p(r.pvalue)))
        elif parametric:
            r = stats.ttest_ind(a, b, equal_var=bool(equal_var))
            n1, n2 = len(a), len(b)
            v1, v2 = np.var(a, ddof=1), np.var(b, ddof=1)
            if equal_var or (v1 == 0 and v2 == 0):
                df = n1 + n2 - 2
            else:
                df = (v1 / n1 + v2 / n2) ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
            sp = math.sqrt(((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2)) if n1 + n2 > 2 else 0.0
            g = (np.mean(b) - np.mean(a)) / sp * (1 - 3 / (4 * (n1 + n2) - 9)) if sp > 0 else np.nan
            name = "Student's t-test" if equal_var else "Welch's t-test"
            out.update(test=name, p=r.pvalue,
                       line="t(%.1f) = %.3f, %s, Hedges' g = %.2f" % (df, r.statistic, fmt_p(r.pvalue), g))
        else:
            r = stats.mannwhitneyu(a, b, alternative="two-sided")
            out.update(test="Mann-Whitney U test", p=r.pvalue, line="U = %.1f, %s" % (r.statistic, fmt_p(r.pvalue)))
        out["pairs"] = [(0, 1, out["p"])]
        out["method"] = "Groups were compared with a two-sided %s." % _article(out["test"])
        return out

    pairs = [(i, j) for i in range(k) for j in range(i + 1, k)] if posthoc == "all" else [(0, j) for j in range(1, k)]
    if paired:
        mat = np.column_stack(vals)
        if parametric:
            F, df1, df2, p = _rm_anova(mat)
            out.update(test="Repeated-measures one-way ANOVA", p=p,
                       line="F(%d, %d) = %.3f, %s" % (df1, df2, F, fmt_p(p)))
            raw = [stats.ttest_rel(mat[:, j], mat[:, i]).pvalue if np.std(mat[:, j] - mat[:, i]) > 0 else np.nan
                   for i, j in pairs]
            if any(not np.isfinite(x) for x in raw):
                out["notes"].append("Some pairs of groups differ by the same amount in every subject; their paired "
                                    "comparison can't be computed and is shown as not significant.")
            ph_name = "paired t-tests with Holm correction"
        else:
            r = stats.friedmanchisquare(*[mat[:, j] for j in range(k)])
            out.update(test="Friedman test", p=r.pvalue,
                       line="chi2(%d) = %.3f, %s" % (k - 1, r.statistic, fmt_p(r.pvalue)))
            raw = []
            for i, j in pairs:
                d = mat[:, j] - mat[:, i]
                raw.append(1.0 if np.all(d == 0) else stats.wilcoxon(mat[:, j], mat[:, i]).pvalue)
            ph_name = "Wilcoxon signed-rank tests with Holm correction"
        adj = _holm(np.nan_to_num(raw, nan=1.0))
    elif parametric:
        r = stats.f_oneway(*vals)
        N = sum(ns)
        out.update(test="One-way ANOVA", p=r.pvalue,
                   line="F(%d, %d) = %.3f, %s" % (k - 1, N - k, r.statistic, fmt_p(r.pvalue)))
        if posthoc == "all":
            adj = _tukey_pairs(vals, pairs)
            ph_name = "Tukey's multiple comparisons test"
        else:
            try:
                dn = stats.dunnett(*vals[1:], control=vals[0])
                adj = list(dn.pvalue)
                ph_name = "Dunnett's test against %s" % names[0]
            except Exception:
                adj = _holm([stats.ttest_ind(vals[j], vals[0], equal_var=False).pvalue for _, j in pairs])
                ph_name = "Welch t-tests against %s with Holm correction" % names[0]
    else:
        r = stats.kruskal(*vals)
        out.update(test="Kruskal-Wallis test", p=r.pvalue,
                   line="H(%d) = %.3f, %s" % (k - 1, r.statistic, fmt_p(r.pvalue)))
        raw = [stats.mannwhitneyu(vals[i], vals[j], alternative="two-sided").pvalue for i, j in pairs]
        adj = _holm(raw)
        ph_name = "pairwise Mann-Whitney U tests with Holm correction"
    out["pairs"] = [(i, j, float(p)) for (i, j), p in zip(pairs, adj)]
    out["posthoc"] = ph_name
    out["method"] = "Groups were compared with a %s followed by %s." % (_article(out["test"]), ph_name)
    return out


def stats_tables(names, res):
    tables = []
    if res.get("normality"):
        tables.append({"title": "Normality (Shapiro-Wilk)", "columns": ["Group", "p", "Looks normal?"],
                       "rows": [[nm, _r(p), "" if p is None else ("Yes" if p > 0.05 else "No")]
                                for nm, p in res["normality"]]})
    if res.get("pairs") and len(names) > 2:
        tables.append({"title": "Multiple comparisons (%s)" % res.get("posthoc", ""),
                       "columns": ["Comparison", "Adjusted p", "Summary"],
                       "rows": [["%s vs %s" % (names[i], names[j]), _r(p), stars(p)] for i, j, p in res["pairs"]]})
    return tables


def _methods(s, res, extra=""):
    err = ERROR_NAMES.get(s["error"], "SEM")
    kind = s["plot_kind"]
    if kind == "box":
        shown = "Box plots show the median and interquartile range (whiskers 1.5 x IQR)"
    elif kind in ("violin", "raincloud", "boxviolin"):
        shown = "Violin plots show the data distribution with median and interquartile range"
    elif kind == "beeswarm":
        shown = "Individual values are shown with the median"
    elif kind == "strip":
        shown = "Individual values are shown"
    else:
        shown = "Data are shown as mean" + (" +/- %s" % err if err else "")
    if (s["show_points"] or kind in ("dot", "raincloud")) and kind not in ("beeswarm", "strip"):
        shown += " with individual values"
    parts = [shown + "."]
    if extra:
        parts.append(extra)
    if res.get("normality"):
        parts.append("Normality was assessed with the Shapiro-Wilk test.")
    if res.get("method"):
        parts.append(res["method"])
    parts.append("P < 0.05 was considered statistically significant.")
    parts.append(_software_line())
    return " ".join(parts)


def _software_line():
    return ("Analyses were performed in Serpentine %s (Python %d.%d, NumPy %s, SciPy %s, pandas %s, matplotlib %s)."
            % (ENGINE_VERSION, sys.version_info[0], sys.version_info[1], np.__version__, scipy.__version__,
               pd.__version__, matplotlib.__version__))


# ----------------------------------------------------------------------------
# Analyses
# ----------------------------------------------------------------------------

def _pick_dataset(datasets, ds_id):
    usable = [d for d in datasets if (d.get("text") or "").strip()]
    if not usable:
        raise UserError("Add a data table first (Data step).")
    for d in usable:
        if d.get("id") == ds_id:
            return d
    return usable[0]


def _numeric_cols(df, info):
    return [c for c in df.columns if info["kinds"][c] in ("number", "time")]


def _choose(S, key, df, auto, notes, what):
    """The column the user picked, or the automatic choice if that column isn't in this table
    (e.g. settings left over from the example data)."""
    wanted = S.get(key) or ""
    if wanted and wanted in df.columns:
        return wanted
    if wanted and auto:
        notes.append('The %s column "%s" isn\'t in this table, so "%s" was used. Pick another in Settings if needed.'
                     % (what, wanted, auto))
    return auto


def _need_col(df, col, what):
    if col not in df.columns:
        raise UserError('Choose a %s column (couldn\'t find "%s").' % (what, col))
    return col


def _order(found, order_text):
    wanted = [g.strip() for g in (order_text or "").split(",") if g.strip()]
    ordered = [g for g in wanted if g in found]
    return ordered + [g for g in found if g not in ordered]


def _group_figure(names, values, s, res, paired, ylabel, title="", width=None):
    fig, ax = _new_fig(s, width)
    if s["logy"] and any((np.asarray(v, float) <= 0).any() for v in values):
        res["notes"].append("Values of 0 or below can't be drawn on a log scale and are hidden.")
    with plt.rc_context(_rc(s)):
        pm = np.column_stack(values) if paired else None
        tops = plot_groups(ax, names, values, s, paired_matrix=pm)
        H = s["orientation"] == "horizontal"
        _finish(ax, s, xlabel=ylabel if H else "", ylabel="" if H else ylabel, title=title)
        drawn = draw_brackets(ax, res["pairs"], tops, s)
    if drawn < 0:
        res["notes"].append("%d comparisons are significant, too many to draw as brackets. They're listed in the "
                            "Multiple comparisons table; you can also compare each group with the first only." % -drawn)
    return fig


def analysis_compare(datasets, S, s):
    d = _pick_dataset(datasets, S.get("dataset"))
    df, info = read_table(d["text"])
    col_notes = []
    layout = S.get("layout", "wide")
    paired = bool(S.get("paired")) and layout == "wide"
    if layout == "long":
        num = _numeric_cols(df, info)
        text_cols = [c for c in df.columns if info["kinds"][c] == "text"]
        vcol = _choose(S, "value_col", df, num[0] if num else None, col_notes, "value")
        gcol = _choose(S, "group_col", df, text_cols[0] if text_cols else None, col_notes, "group")
        if not vcol or not gcol:
            raise UserError("The values + group layout needs a value column and a group column.")
        _need_col(df, vcol, "value")
        _need_col(df, gcol, "group")
        if vcol == gcol:
            raise UserError("Pick different columns for the values and the groups.")
        labels = _labels(df, gcol, info)
        found = [g for g in dict.fromkeys(labels) if g and g.lower() != "nan"]
        names = _order(found, S.get("group_order"))
        values = [pd.to_numeric(df.loc[labels == g, vcol], errors="coerce").to_numpy(float) for g in names]
        ylabel = S.get("y_name") or vcol
    else:
        cols = [c for c in (S.get("columns") or []) if c in df.columns and info["kinds"][c] != "text"] \
            or _numeric_cols(df, info)
        if not cols:
            raise UserError("No numeric columns found to compare.")
        heads = [c.split(" · ", 1)[0] if " · " in c else None for c in cols]
        if S.get("combine_headings", True) and all(heads) and len(set(heads)) < len(heads):
            # Prism-style grouped table: replicate columns under one heading form one group
            pooled = {}
            for c, h in zip(cols, heads):
                pooled.setdefault(h, []).append(pd.to_numeric(df[c], errors="coerce").to_numpy(float))
            names = _order(list(pooled), S.get("group_order"))
            values = [np.concatenate(pooled[g]) for g in names]
            col_notes.append("Columns under the same heading were combined into one group each (%s)."
                             % ", ".join("%s: %d columns" % (g, len(pooled[g])) for g in names))
            if paired:
                paired = False
                col_notes.append("Paired testing was turned off because combined columns no longer line up by subject.")
        else:
            names = _order(cols, S.get("group_order"))
            values = [pd.to_numeric(df[c], errors="coerce").to_numpy(float) for c in names]
        ylabel = S.get("y_name") or ""
    if not names:
        raise UserError("No groups found.")
    norm_note = ""
    notes = []
    normalize = S.get("normalize", "none")
    if normalize in ("pct_control", "fold_control", "delta_control") and len(names) >= 1:
        ctrl = values[0][np.isfinite(values[0])]
        ref = float(np.mean(ctrl)) if len(ctrl) else np.nan
        if not np.isfinite(ref) or (ref == 0 and normalize != "delta_control"):
            notes.append("The first group's mean is %s, so values couldn't be expressed relative to it." % ("0" if ref == 0 else "missing"))
        else:
            if normalize == "pct_control":
                values = [v / ref * 100.0 for v in values]
                suffix, norm_note = "%% of %s" % names[0], "Values were expressed as a percentage of the mean of the %s group." % names[0]
            elif normalize == "fold_control":
                values = [v / ref for v in values]
                suffix, norm_note = "fold of %s" % names[0], "Values were expressed as fold of the mean of the %s group." % names[0]
            else:
                values = [v - ref for v in values]
                suffix, norm_note = "difference from %s" % names[0], "Values were expressed as the difference from the mean of the %s group." % names[0]
            ylabel = "%s (%s)" % (ylabel, suffix) if ylabel else suffix[0].upper() + suffix[1:]
    out_mode = S.get("outliers", "keep")
    out_rows = []
    if out_mode in ("flag", "remove"):
        method = S.get("outlier_method", "grubbs")
        values = [np.asarray(v, float).copy() for v in values]
        for gi, nm in enumerate(names):
            idx = grubbs_outliers(values[gi]) if method == "grubbs" else iqr_outliers(values[gi])
            for k in idx:
                out_rows.append([nm, _r(values[gi][k]), "Removed" if out_mode == "remove" else "Kept"])
            if out_mode == "remove" and idx:
                values[gi][idx] = np.nan
        mname = "Grubbs' test (alpha = 0.05)" if method == "grubbs" else "Tukey's fences (1.5 x IQR)"
        if out_rows:
            notes.append("%d possible outlier(s) by %s: %s. %s" % (
                len(out_rows), mname, ", ".join("%s in %s" % (_fmt(r[1]), r[0]) for r in out_rows[:6]),
                "They were removed from the statistics and the figure." if out_mode == "remove"
                else "They are still included; set Outliers to Remove to exclude them."))
            if out_mode == "remove":
                norm_note = (norm_note + " " if norm_note else "") + "Outliers identified by %s were excluded." % mname
        else:
            notes.append("No outliers found by %s." % mname)
    res = compare_stats(names, values, paired=paired, mode=S.get("test", "auto"),
                        posthoc=S.get("posthoc", "all"), equal_var=S.get("equal_var", False))
    fig = _group_figure(names, values, s, res, paired, ylabel)
    shown = [g["label"] for g in _gstyles(s, names)]
    summary = []
    if res["test"]:
        summary.append({"label": res["test"], "value": res["line"], "p": _r(res["p"])})
    tables = [describe(names, values, shown)] + stats_tables(shown, res)
    if out_rows:
        tables.append({"title": "Possible outliers", "columns": ["Group", "Value", "Action"], "rows": out_rows})
    return {
        "figures": [{"id": "groups", "title": "Group comparison", "fig": fig, "style": s}],
        "tables": tables, "summary": summary, "notes": col_notes + notes + res["notes"],
        "methods": _methods(s, res, norm_note), "groups": names,
    }


def _parse_clock_value(text, mult):
    t = str(text or "").strip().replace("−", "-")
    if not t:
        return None
    c = _clock(t)
    if c is not None:
        return c
    try:
        return float(t) * mult
    except ValueError:
        raise UserError('Couldn\'t read the time "%s". Use mm:ss (e.g. 10:30) or a number.' % t)


_EVENT_WORDS = re.compile(r"inj|dose|drug|stim|event|treat|admin|start|onset|challenge|agonist|infus|bolus|gavage|ip\b|i\.p", re.I)


def _event_from_comments(df, info, t):
    """(time, comment) for the event marked in a text column, if there's a clear one."""
    for col in [c for c in df.columns if info["kinds"][c] == "text"]:
        texts = df[col].astype(str).str.strip()
        marked = [(i, x) for i, x in enumerate(texts) if x and x.lower() != "nan" and i < len(t) and np.isfinite(t[i])]
        if not marked:
            continue
        hits = [m for m in marked if _EVENT_WORDS.search(m[1])]
        pick = hits[0] if hits else (marked[0] if len(marked) == 1 else None)
        if pick:
            return float(t[pick[0]]), pick[1][:40]
    return None


def _parse_ranges(text, mult):
    out = []
    for part in re.split(r"[;,]", str(text or "")):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.+?)\s*(?:-|–|—|to)\s*(.+)$", part)
        if not m:
            raise UserError('Couldn\'t read the exclusion "%s". Use start-end, e.g. 8:00-8:50.' % part)
        a, b = _parse_clock_value(m.group(1), mult), _parse_clock_value(m.group(2), mult)
        out.append((min(a, b), max(a, b)))
    return out


def _f(S, key, default):
    v = S.get(key, default)
    if v is None or str(v).strip() == "":
        return default
    try:
        v = float(v)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


NORM_LABELS = {"pct": "% of baseline", "delta": "change from baseline", "fold": "fold of baseline", "raw": ""}


def analysis_timecourse(datasets, S, s):
    sessions = [d for d in datasets if (d.get("text") or "").strip()]
    if not sessions:
        raise UserError("Add at least one recording (Data step). Each table is one session.")
    mult = {"seconds": 1.0, "minutes": 60.0, "hours": 3600.0}.get(S.get("time_unit", "seconds"), 1.0)
    bin_s = max(0.0, _f(S, "bin", 30.0))
    bs, be = _f(S, "baseline_start", -5.0), _f(S, "baseline_end", 0.0)
    ws, we = _f(S, "window_start", -10.0), _f(S, "window_end", 30.0)
    es, ee = _f(S, "endpoint_start", 25.0), _f(S, "endpoint_end", 30.0)
    for a, b, what in ((bs, be, "baseline"), (ws, we, "display"), (es, ee, "endpoint")):
        if a >= b:
            raise UserError("The %s window must start before it ends (now %g to %g min)." % (what, a, b))
    norm = S.get("norm", "pct") if S.get("norm") in NORM_LABELS else "pct"
    metric = S.get("metric", "mean")
    vmin, vmax = _f(S, "valid_min", None), _f(S, "valid_max", None)
    fcol = S.get("filter_col") or ""
    fmin, fmax = _f(S, "filter_min", None), _f(S, "filter_max", None)
    notes = []

    per = []
    detected = {}
    no_base, no_end = [], []
    tcol = vcol = None
    all_cols = [set(read_table(d["text"])[0].columns) for d in sessions]
    S = dict(S)
    for key, what in (("time_col", "time"), ("value_col", "measurement"), ("filter_col", "quality")):
        if S.get(key) and not any(S[key] in cols for cols in all_cols):
            if key == "filter_col":
                notes.append('The quality column "%s" isn\'t in this data, so the quality filter was skipped.' % S[key])
            else:
                notes.append('The %s column "%s" isn\'t in this data, so one was picked automatically. '
                             'Choose another in Settings if needed.' % (what, S[key]))
            S[key] = ""
    for d in sessions:
        meta = d.get("meta") or {}
        name = d.get("name") or "Session"
        df, info = read_table(d["text"])
        num = _numeric_cols(df, info)
        times = [c for c in df.columns if info["kinds"][c] == "time"]
        tcol = S.get("time_col") or (times[0] if times else (num[0] if num else None))
        vcol = S.get("value_col") or next((c for c in num if c != tcol), None)
        if not tcol or not vcol:
            raise UserError('Session "%s" needs a time column and at least one numeric measurement column. '
                            'Check how the table was read in the Data step.' % name)
        if tcol not in df.columns or vcol not in df.columns:
            missing = tcol if tcol not in df.columns else vcol
            raise UserError('Session "%s" has no column called "%s". All sessions need the same column names.' % (name, missing))
        if tcol == vcol:
            raise UserError("The time column and the measurement can't be the same column.")
        t = df[tcol].to_numpy(float)
        if info["kinds"][tcol] != "time":
            t = t * mult
        v = df[vcol].to_numpy(float)
        # A new LabChart block restarts time at 0. Keep the first block and say so.
        fin_idx = np.where(np.isfinite(t))[0]
        if len(fin_idx) > 1:
            back = np.where(np.diff(t[fin_idx]) < -1e-6)[0]
            if len(back):
                cut = fin_idx[back[0] + 1]
                notes.append('"%s": time jumps back from %s to %s at row %d (a new recording block?). Rows from there on were left out.'
                             % (name, _fmt_clock(t[fin_idx[back[0]]]), _fmt_clock(t[cut]), cut + 1))
                t, v = t[:cut], v[:cut]
                df = df.iloc[:cut]
        ok = np.isfinite(t) & np.isfinite(v)
        if vmin is not None:
            ok &= v >= vmin
        if vmax is not None:
            ok &= v <= vmax
        if fcol:
            if fcol in df.columns:
                fv = pd.to_numeric(df[fcol], errors="coerce").to_numpy(float)
                if fmin is not None:
                    ok &= fv >= fmin
                if fmax is not None:
                    ok &= fv <= fmax
            else:
                notes.append('"%s" has no column "%s", so the quality filter was skipped there.' % (name, fcol))
        for a, b in _parse_ranges(meta.get("exclude"), mult):
            ok &= ~((t >= a) & (t <= b))
        ev = _parse_clock_value(meta.get("event"), mult)
        if ev is None:
            # LabChart and many recorders mark the injection in a comment column: use that time.
            found = _event_from_comments(df, info, t)
            if found:
                ev, label = found
                detected[d.get("id") or name] = _fmt_clock(ev)
                notes.append('"%s": event time %s taken from the comment “%s”.' % (name, _fmt_clock(ev), label))
            else:
                ev = 0.0
                notes.append('"%s" has no event time, so time 0 was used.' % name)
        trel = t[ok] - ev
        vv = v[ok]
        if not len(trel):
            notes.append('"%s" has no usable data after filtering.' % name)
            continue
        if bin_s > 0:
            step = bin_s
        else:
            diffs = np.diff(np.unique(trel))
            step = float(np.median(diffs)) if len(diffs) else 1.0
            step = step if step > 0 else 1.0
        k = np.floor(trel / step + 1e-9).astype(int)
        binned = pd.Series(vv).groupby(k).mean()
        kk = binned.index.to_numpy()
        bv = binned.to_numpy(float)
        centers = (kk + 0.5) * step / 60.0
        in_base = (centers >= bs) & (centers <= be)
        base = float(np.mean(bv[in_base])) if in_base.any() else np.nan
        if not in_base.any():
            no_base.append(name)
        elif norm in ("pct", "fold") and base == 0:
            notes.append('"%s" has a baseline of 0, so it can\'t be expressed relative to baseline.' % name)
            base = np.nan
        with np.errstate(all="ignore"):
            if norm == "pct":
                nv, ref = bv / base * 100.0, 100.0
            elif norm == "fold":
                nv, ref = bv / base, 1.0
            elif norm == "delta":
                nv, ref = bv - base, 0.0
            else:
                nv, ref = bv, base
        per.append({"name": name, "group": (meta.get("group") or "Group").strip() or "Group",
                    "subject": (meta.get("subject") or name).strip(), "k": kk, "v": nv,
                    "centers": centers, "base": base, "ref": ref, "step": step, "event": ev})
    if not per:
        raise UserError("No session has usable data. Check the column choices and filters.")

    step = per[0]["step"]
    if len(set(round(p["step"], 6) for p in per)) > 1:
        notes.append("Sessions have different sampling intervals; set a bin size so they line up.")
    groups = _order(list(dict.fromkeys(p["group"] for p in per)), S.get("group_order"))
    G = _gstyles(s, groups)

    for p in per:
        sel = (p["centers"] >= es) & (p["centers"] <= ee) & np.isfinite(p["v"])
        y = p["v"][sel]
        p["end"] = np.nan
        if len(y):
            if metric == "min":
                p["end"] = float(np.min(y))
            elif metric == "max":
                p["end"] = float(np.max(y))
            elif metric == "auc":
                x = p["centers"][sel]
                ref = p["ref"] if np.isfinite(p["ref"]) else 0.0
                p["end"] = float(np.trapz(y - ref, x)) if len(y) > 1 else np.nan
            else:
                p["end"] = float(np.mean(y))
        if not np.isfinite(p["end"]):
            if not (norm != "raw" and not np.isfinite(p["base"])):  # already explained by the missing baseline
                no_end.append(p["name"])

    # One clear note per problem, with a window that fits the recordings
    first_min = min(float(p["centers"].min()) for p in per)
    last_min = max(float(p["centers"].max()) for p in per)
    if no_base:
        who = "No session" if len(no_base) == len(per) else ", ".join('"%s"' % n for n in no_base)
        hint = (" Recordings start about %.0f min before the event; try a baseline from %g to 0." % (-first_min, max(round(first_min), bs))
                if first_min < 0 else " Check the event time: there's no recording before it.")
        notes.append(("No session has data in the baseline window (%g to %g min).%s" % (bs, be, hint)) if who == "No session"
                     else "%s %s no data in the baseline window (%g to %g min)%s. Check %s event time and exclusions."
                     % (who, "has" if len(no_base) == 1 else "have", bs, be,
                        ", so it's left out of the results" if norm != "raw" and len(no_base) == 1 else
                        (", so they're left out of the results" if norm != "raw" else ""), "its" if len(no_base) == 1 else "their"))
    if no_end:
        who = "No session" if len(no_end) == len(per) else ", ".join('"%s"' % n for n in no_end)
        span = max(1.0, min(5.0, last_min / 2)) if last_min > 0 else 0
        hint = (" Recordings end about %.0f min after the event; try an endpoint from %g to %g min." % (last_min, round(max(0, last_min - span)), round(last_min))
                if last_min > 0 else " Check the event time: there's no recording after it.")
        notes.append(("No session has data in the endpoint window (%g to %g min).%s" % (es, ee, hint)) if who == "No session"
                     else "%s %s no data in the endpoint window (%g to %g min). Check %s event time, exclusions and recording length."
                     % (who, "has" if len(no_end) == 1 else "have", es, ee, "its" if len(no_end) == 1 else "their"))

    unit = NORM_LABELS.get(norm, "")
    ylabel = "%s (%s)" % (vcol, unit) if unit else vcol
    event_label = S.get("event_label") or "event"
    band = S.get("band", "sem")
    band_style = S.get("band_style", "band")
    fig, ax = _new_fig(s)
    lw = s["line_width"]
    tc_rows = []
    with plt.rc_context(_rc(s)):
        if S.get("shade_baseline", True):
            ax.axvspan(bs, be, color=to_rgba(s["fg"], 0.06), lw=0, zorder=0)
        if S.get("shade_endpoint", True):
            ax.axvspan(es, ee, color=to_rgba(G[min(1, len(G) - 1)]["color"], 0.08), lw=0, zorder=0)
        if norm != "raw":
            ax.axhline(per[0]["ref"], color=to_rgba(s["fg"], 0.35), lw=max(0.6, lw * 0.5), ls=(0, (2, 3)), zorder=1)
        for g in G:
            members = [p for p in per if p["group"] == g["name"]]
            all_k = sorted(set(np.concatenate([p["k"] for p in members]).tolist()))
            all_k = [k for k in all_k if ws <= (k + 0.5) * step / 60.0 <= we]
            if not all_k:
                continue
            mat = np.full((len(all_k), len(members)), np.nan)
            pos = {k: i for i, k in enumerate(all_k)}
            for j, p in enumerate(members):
                for k, val in zip(p["k"], p["v"]):
                    if k in pos:
                        mat[pos[k], j] = val
            x = (np.array(all_k) + 0.5) * step / 60.0
            with np.errstate(all="ignore"), np.testing.suppress_warnings() as sup:
                sup.filter(RuntimeWarning)
                n = np.isfinite(mat).sum(axis=1)
                mean = np.nanmean(mat, axis=1)
                sd = np.nanstd(mat, axis=1, ddof=1)
                sem = sd / np.sqrt(n)
            c = g["color"]
            if S.get("show_traces", True) and len(members) > 1:
                for j in range(len(members)):
                    ax.plot(x, mat[:, j], color=to_rgba(c, s["trace_alpha"]), lw=max(0.5, lw * 0.5), zorder=2)
            if band != "none" and len(members) > 1:
                e = sd if band == "sd" else sem
                if band_style == "bars":
                    ax.errorbar(x, mean, yerr=e, fmt="none", ecolor=c, elinewidth=s["error_width"] * 0.7,
                                capsize=s["cap_size"] * 0.6, zorder=3)
                else:
                    ax.fill_between(x, mean - e, mean + e, color=to_rgba(c, s["band_alpha"]), lw=0, zorder=2)
            kw = dict(color=c, lw=lw * 1.3, ls=g["ls"], zorder=4, label="%s (n=%d)" % (g["label"], len(members)))
            if s["line_markers"]:
                kw.update(marker=g["marker"], markersize=s["marker_size"] * 0.8, markeredgecolor=_point_edge(s, c),
                          markeredgewidth=s["point_edge_width"])
            ax.plot(x, mean, **kw)
            for xi, m_, s_, n_ in zip(x, mean, sem, n):
                tc_rows.append([g["label"], _r(xi), _r(m_), _r(s_), int(n_)])
        if S.get("show_event", True):
            ax.axvline(0, color=to_rgba(s["fg"], 0.6), lw=max(0.7, lw * 0.6), ls=(0, (4, 3)), zorder=1)
            ax.annotate(event_label, xy=(0, 1), xycoords=("data", "axes fraction"), xytext=(0, 3),
                        textcoords="offset points", ha="center", va="bottom", fontsize=s["font_size"] * 0.85,
                        color=to_rgba(s["fg"], 0.75))
        ax.set_xlim(ws, we)
        _finish(ax, s, xlabel="Time from %s (min)" % event_label.lower(), ylabel=ylabel, legend=True,
                title_pad=s["font_size"] * 2.0 if S.get("show_event", True) else None)

    paired = bool(S.get("pair_subjects")) and len(groups) > 1
    if paired:
        subjects = [sub for sub in dict.fromkeys(p["subject"] for p in per)
                    if all(any(p["subject"] == sub and p["group"] == g for p in per) for g in groups)]
        dropped = len(set(p["subject"] for p in per)) - len(subjects)
        if dropped:
            notes.append("%d subject(s) without a session in every group were left out of the paired comparison."
                         % dropped)
        dupes = [sub for sub in subjects for g in groups
                 if sum(1 for p in per if p["subject"] == sub and p["group"] == g) > 1]
        if dupes:
            notes.append("Subject %s has more than one session in a group; their endpoints were averaged."
                         % ", ".join(sorted(set(dupes))))
        values = [np.array([np.nanmean([p["end"] for p in per if p["subject"] == sub and p["group"] == g])
                            for sub in subjects]) if subjects else np.array([]) for g in groups]
    else:
        values = [np.array([p["end"] for p in per if p["group"] == g]) for g in groups]
    res = compare_stats(groups, values, paired=paired, mode=S.get("test", "auto"),
                        posthoc=S.get("posthoc", "all"), equal_var=S.get("equal_var", False))
    metric_name = {"mean": "Mean", "min": "Minimum", "max": "Maximum", "auc": "Net AUC"}.get(metric, "Mean")
    end_label = ("%s %s, %g to %g min" % (metric_name, unit or vcol, es, ee)) if metric != "auc" else \
        "Net AUC vs baseline, %g to %g min" % (es, ee)
    ep_width = s["endpoint_width"] or max(2.4, min(s["width"], 1.1 + 0.9 * len(groups)))
    if s["orientation"] == "horizontal" and not s["endpoint_width"]:
        ep_width = s["width"]
    s2 = dict(s, width=ep_width, title="", xlabel="", ylabel="", panel_label="", ref_lines="")
    fig2 = _group_figure(groups, values, s2, res, paired, end_label, width=ep_width)
    if norm != "raw" and metric != "auc":
        ax2 = fig2.axes[0]
        with plt.rc_context(_rc(s2)):
            line = ax2.axvline if s["orientation"] == "horizontal" else ax2.axhline
            line(per[0]["ref"], color=to_rgba(s["fg"], 0.35), lw=max(0.6, lw * 0.5), ls=(0, (2, 3)), zorder=1)

    shown = [g["label"] for g in G]
    sess_rows = [[p["name"], p["group"], p["subject"], _fmt_clock(p["event"]), _r(p["base"]), _r(p["end"])]
                 for p in per]
    tables = [
        {"title": "Sessions", "columns": ["Session", "Group", "Subject", "Event", "Baseline (raw)", end_label],
         "rows": sess_rows},
        describe(groups, values, shown),
    ] + stats_tables(shown, res) + [
        {"title": "Time course (group means)", "columns": ["Group", "Time (min)", "Mean", "SEM", "n"],
         "rows": tc_rows},
    ]
    summary = []
    if res["test"]:
        summary.append({"label": "%s: %s" % (res["test"], end_label), "value": res["line"], "p": _r(res["p"])})
    bin_txt = ("averaged in %g-s bins" % step) if bin_s > 0 else "analysed without binning"
    norm_txt = {
        "pct": "expressed as a percentage of each session's baseline",
        "fold": "expressed as fold of each session's baseline",
        "delta": "expressed as change from each session's baseline",
        "raw": "shown in raw units",
    }[norm]
    extra = ("%s was %s, aligned to the %s, and %s (baseline = mean from %g to %g min). "
             "The endpoint was the %s from %g to %g min after the %s."
             % (vcol, bin_txt, event_label.lower(), norm_txt, bs, be,
                metric_name.lower() if metric != "auc" else "net area under the curve relative to baseline",
                es, ee, event_label.lower()))
    s_meth = dict(s)
    s_meth["error"] = band if band != "none" else s["error"]
    return {
        "figures": [
            {"id": "timecourse", "title": "Time course", "fig": fig, "style": s},
            {"id": "endpoint", "title": "Endpoint", "fig": fig2, "style": s2},
        ],
        "tables": tables, "summary": summary, "notes": notes + res["notes"],
        "methods": _methods(s_meth, res, extra), "groups": groups, "events": detected,
    }


def analysis_correlation(datasets, S, s):
    d = _pick_dataset(datasets, S.get("dataset"))
    df, info = read_table(d["text"])
    num = _numeric_cols(df, info)
    col_notes = []
    xcol = _choose(S, "x_col", df, num[0] if num else None, col_notes, "X")
    ycol = _choose(S, "y_col", df, next((c for c in num if c != xcol), None), col_notes, "Y")
    if not xcol or not ycol:
        raise UserError("Correlation needs two numeric columns.")
    _need_col(df, xcol, "X")
    _need_col(df, ycol, "Y")
    if xcol == ycol:
        raise UserError("Pick two different columns for X and Y.")
    gcol = S.get("group_col") or ""
    if gcol and gcol in df.columns:
        labels = _labels(df, gcol, info)
        groups = _order([g for g in dict.fromkeys(labels) if g and g.lower() != "nan"], S.get("group_order"))
    else:
        labels = pd.Series(["All"] * len(df))
        groups = ["All"]
    method = S.get("method", "pearson")
    G = _gstyles(s, groups)
    fig, ax = _new_fig(s)
    rows, summary, notes = [], [], []
    lw, ms = s["line_width"], s["marker_size"]
    with plt.rc_context(_rc(s)):
        for g in G:
            sel = labels == g["name"]
            x = pd.to_numeric(df.loc[sel, xcol], errors="coerce").to_numpy(float)
            y = pd.to_numeric(df.loc[sel, ycol], errors="coerce").to_numpy(float)
            ok = np.isfinite(x) & np.isfinite(y)
            x, y = x[ok], y[ok]
            c = g["color"]
            ax.scatter(x, y, s=ms ** 2 * 1.3, marker=g["marker"], color=to_rgba(c, s["point_alpha"]),
                       edgecolor=_point_edge(s, c), linewidth=s["point_edge_width"], zorder=3,
                       label=g["label"] if len(groups) > 1 else None)
            if len(x) < 3:
                notes.append("%s: need at least 3 points for a correlation." % g["label"])
                continue
            if np.ptp(x) == 0 or np.ptp(y) == 0:
                notes.append("%s: X or Y doesn't vary, so there is no correlation to measure." % g["label"])
                continue
            if method == "spearman":
                r, p = stats.spearmanr(x, y)
                rname, sym = "Spearman rho", "rho"
            else:
                r, p = stats.pearsonr(x, y)
                rname, sym = "Pearson r", "r"
            lr = stats.linregress(x, y)
            rows.append([g["label"], len(x), _r(r), _r(p), _r(lr.slope), _r(lr.intercept), _r(lr.rvalue ** 2)])
            summary.append({"label": "%s%s" % (rname, " (%s)" % g["label"] if len(groups) > 1 else ""),
                            "value": "%s = %.3f, %s, n = %d" % (sym, r, fmt_p(p), len(x)), "p": _r(p)})
            if S.get("fit", True):
                xx = np.linspace(x.min(), x.max(), 100)
                yy = lr.intercept + lr.slope * xx
                ax.plot(xx, yy, color=c, lw=lw * 1.2, ls=g["ls"], zorder=2)
                if S.get("ci", True) and len(x) > 2:
                    n = len(x)
                    resid = y - (lr.intercept + lr.slope * x)
                    se = math.sqrt(np.sum(resid ** 2) / (n - 2))
                    tval = stats.t.ppf(0.975, n - 2)
                    sxx = np.sum((x - x.mean()) ** 2)
                    band = tval * se * np.sqrt(1 / n + (xx - x.mean()) ** 2 / sxx) if sxx > 0 else 0
                    ax.fill_between(xx, yy - band, yy + band, color=to_rgba(c, s["band_alpha"] * 0.85), lw=0, zorder=1)
        if S.get("annotate", True) and summary and len(groups) == 1:
            ax.text(0.03, 0.97, summary[0]["value"].rsplit(", n =", 1)[0], transform=ax.transAxes, ha="left",
                    va="top", fontsize=s["font_size"] * 0.85)
        _finish(ax, s, xlabel=xcol, ylabel=ycol, legend=len(groups) > 1)
    tables = [{"title": "Correlation and linear fit",
               "columns": ["Group", "n", "r" if method != "spearman" else "rho", "p", "Slope", "Intercept", "R squared"],
               "rows": rows}]
    meth = ("Associations were assessed with %s correlation%s. P < 0.05 was considered statistically significant. %s"
            % ("Spearman's rank" if method == "spearman" else "Pearson's",
               " and an ordinary least-squares linear fit (shaded band: 95% confidence interval)" if S.get("fit", True) else "",
               _software_line()))
    return {"figures": [{"id": "scatter", "title": "Correlation", "fig": fig, "style": s}],
            "tables": tables, "summary": summary, "notes": col_notes + notes, "methods": meth, "groups": groups}


def _four_pl(logx, bottom, top, log_ec50, hill):
    with np.errstate(over="ignore"):
        return bottom + (top - bottom) / (1 + 10 ** ((log_ec50 - logx) * hill))


def analysis_dose(datasets, S, s):
    d = _pick_dataset(datasets, S.get("dataset"))
    df, info = read_table(d["text"])
    num = _numeric_cols(df, info)
    col_notes = []
    xcol = _choose(S, "x_col", df, num[0] if num else None, col_notes, "concentration")
    if not xcol:
        raise UserError("Choose the concentration column.")
    _need_col(df, xcol, "concentration")
    ycols = [c for c in (S.get("y_cols") or []) if c in df.columns and c != xcol and info["kinds"][c] != "text"] \
        or [c for c in num if c != xcol]
    if not ycols:
        raise UserError("Choose at least one response column.")
    mode = S.get("mode", "curves")
    x_all = pd.to_numeric(df[xcol], errors="coerce").to_numpy(float)
    curves = [(S.get("curve_name") or "Response", ycols)] if mode == "replicates" else [(c, [c]) for c in ycols]
    G = _gstyles(s, [c[0] for c in curves])
    fix_bottom, fix_top = _f(S, "fix_bottom", None), _f(S, "fix_top", None)
    scale = _f(S, "x_scale", 1.0) or 1.0
    unit = S.get("x_unit") or ""
    fig, ax = _new_fig(s)
    rows, summary, notes, guides = [], [], [], []
    lw, ms = s["line_width"], s["marker_size"]
    with plt.rc_context(_rc(s)):
        for g, (name, cols) in zip(G, curves):
            c = g["color"]
            Y = np.column_stack([pd.to_numeric(df[cc], errors="coerce").to_numpy(float) for cc in cols])
            x = x_all * scale
            pos = np.isfinite(x) & (x > 0)
            if (~pos & np.isfinite(x)).any():
                notes.append("%s: concentrations of 0 or below can't go on a log axis and were left out." % g["label"])
            xs, Ys = x[pos], Y[pos]
            lx = np.log10(xs)
            px = np.repeat(lx, Ys.shape[1])
            py = Ys.reshape(-1)
            ok = np.isfinite(py)
            px, py = px[ok], py[ok]
            if len(px) == 0:
                notes.append("%s has no usable values." % g["label"])
                continue
            ux = np.unique(px)
            means = np.array([np.mean(py[px == u]) for u in ux])
            errs = np.array([_err(py[px == u], s["error"]) for u in ux])
            pe = _point_edge(s, c)
            if Ys.shape[1] > 1 and not S.get("all_points", False):
                ax.errorbar(10 ** ux, means, yerr=errs if s["error"] != "none" else None, fmt=g["marker"], color=c,
                            ms=ms * 1.1, mec=pe, mew=s["point_edge_width"], elinewidth=s["error_width"] * 0.8,
                            capsize=s["cap_size"] * 0.7, zorder=4, label=g["label"], ls="none")
            else:
                ax.scatter(10 ** px, py, s=ms ** 2 * 1.2, marker=g["marker"], color=to_rgba(c, s["point_alpha"]),
                           edgecolor=pe, linewidth=s["point_edge_width"], zorder=4, label=g["label"])
            if len(ux) < 4:
                notes.append("%s: a 4-parameter fit needs at least 4 concentrations." % g["label"])
                continue
            if np.ptp(means) <= 1e-9 * max(1.0, abs(float(np.mean(means)))):
                notes.append("%s: the response doesn't change with concentration, so no curve was fitted." % g["label"])
                continue
            fixed = {}
            if fix_bottom is not None:
                fixed["bottom"] = fix_bottom
            if fix_top is not None:
                fixed["top"] = fix_top
            free = [f for f in ("bottom", "top", "log_ec50", "hill") if f not in fixed]
            corr = np.corrcoef(px, py)[0, 1] if np.ptp(py) > 0 else 1.0
            slope_sign = float(np.sign(corr)) or 1.0
            guess = {"bottom": float(np.min(means)), "top": float(np.max(means)),
                     "log_ec50": float(np.median(ux)), "hill": slope_sign}

            def model(lxv, *params, _fixed=fixed, _free=free):
                kw = dict(_fixed)
                kw.update(dict(zip(_free, params)))
                return _four_pl(lxv, kw["bottom"], kw["top"], kw["log_ec50"], kw["hill"])

            try:
                popt, pcov = curve_fit(model, px, py, p0=[guess[f] for f in free], maxfev=20000)
            except Exception:
                notes.append("%s: the curve fit did not converge." % g["label"])
                continue
            par = dict(fixed)
            par.update(dict(zip(free, popt)))
            perr = dict(zip(free, np.sqrt(np.clip(np.diag(pcov), 0, None)))) if np.all(np.isfinite(pcov)) else {}
            pred = model(px, *popt)
            ss_res = np.sum((py - pred) ** 2)
            ss_tot = np.sum((py - py.mean()) ** 2)
            r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
            dof = max(1, len(px) - len(free))
            tval = stats.t.ppf(0.975, dof)
            se_l = perr.get("log_ec50", np.nan)
            ec50 = 10 ** par["log_ec50"]
            lo, hi = 10 ** (par["log_ec50"] - tval * se_l), 10 ** (par["log_ec50"] + tval * se_l)
            if par["log_ec50"] < ux.min() - 1 or par["log_ec50"] > ux.max() + 1:
                notes.append("%s: the midpoint lies outside the tested range, so treat it as an estimate." % g["label"])
            label = S.get("ec_label") or ("IC50" if par["hill"] * (par["top"] - par["bottom"]) < 0 else "EC50")
            xx = np.linspace(ux.min() - 0.3, ux.max() + 0.3, 300)
            ax.plot(10 ** xx, model(xx, *popt), color=c, lw=lw * 1.2, ls=g["ls"], zorder=3)
            guides.append((ec50, (par["top"] + par["bottom"]) / 2, c))
            rows.append([g["label"], label, _r(ec50), _r(lo), _r(hi), _r(par["log_ec50"]), _r(par["hill"]),
                         _r(par["bottom"]), _r(par["top"]), _r(r2), len(px)])
            summary.append({"label": "%s %s" % (g["label"], label),
                            "value": "%s %s (95%% CI %s to %s), Hill slope %.2f, R² = %.3f"
                                     % (_fmt(ec50), unit, _fmt(lo), _fmt(hi), par["hill"], r2), "p": None})
        ax.set_xscale("log")
        if S.get("show_ec50", True) and guides:
            lo_y, hi_y = ax.get_ylim()
            for ec50, yhalf, c in guides:
                ax.plot([ec50, ec50], [lo_y, yhalf], color=to_rgba(c, 0.6), lw=max(0.6, lw * 0.6),
                        ls=(0, (3, 3)), zorder=2)
            ax.set_ylim(lo_y, hi_y)
        _finish(ax, s, xlabel="%s%s" % (xcol, " (%s)" % unit if unit else ""),
                ylabel=ycols[0] if len(curves) == 1 and mode != "replicates" else "Response",
                legend=len(curves) > 1)
    tables = [{"title": "Four-parameter logistic fit",
               "columns": ["Curve", "Parameter", "Value", "95% CI low", "95% CI high", "log value", "Hill slope",
                           "Bottom", "Top", "R squared", "Points"], "rows": rows}]
    fixed_txt = []
    if fix_bottom is not None:
        fixed_txt.append("bottom fixed at %g" % fix_bottom)
    if fix_top is not None:
        fixed_txt.append("top fixed at %g" % fix_top)
    meth = ("Concentration-response data were fitted with a four-parameter logistic model (variable Hill slope%s) "
            "by nonlinear least squares on log-transformed concentrations. Confidence intervals were derived from the "
            "standard error of log EC50/IC50. %s" % ("; " + ", ".join(fixed_txt) if fixed_txt else "", _software_line()))
    if any(len(cols) > 1 for _, cols in curves) and not S.get("all_points", False):
        meth = "Points show mean +/- %s of replicates. " % ERROR_NAMES.get(s["error"], "SEM") + meth
    return {"figures": [{"id": "dose", "title": "Dose-response", "fig": fig, "style": s}],
            "tables": tables, "summary": summary, "notes": col_notes + notes, "methods": meth,
            "groups": [c[0] for c in curves]}


# ----------------------------------------------------------------------------
# Survival (Kaplan-Meier)
# ----------------------------------------------------------------------------

_EVENT_YES = {"1", "yes", "y", "true", "dead", "died", "death", "event", "deceased", "failed", "failure", "relapse", "relapsed"}


def _event_flags(df, col, info):
    if info["kinds"].get(col) == "number":
        return (pd.to_numeric(df[col], errors="coerce").fillna(0).to_numpy(float) > 0).astype(float)
    return df[col].astype(str).str.strip().str.lower().isin(_EVENT_YES).to_numpy(float)


def _km(t, e):
    """Kaplan-Meier estimate. Returns step times, survival, 95% CI (log-log) and censored points."""
    order = np.argsort(t, kind="stable")
    t, e = t[order], e[order]
    times, surv, lo, hi = [0.0], [1.0], [1.0], [1.0]
    s_val, gw, n = 1.0, 0.0, len(t)
    at_risk = [(0.0, n)]
    for u in np.unique(t):
        here = t == u
        d = float(e[here].sum())
        if d > 0 and n > 0:
            s_val *= 1 - d / n
            if n > d:
                gw += d / (n * (n - d))
            if 0 < s_val < 1:
                se = math.sqrt(gw) / abs(math.log(s_val))
                a, b = s_val ** math.exp(1.96 * se), s_val ** math.exp(-1.96 * se)
            else:
                a = b = s_val
            times.append(float(u))
            surv.append(s_val)
            lo.append(a)
            hi.append(b)
        n -= int(here.sum())
    cens = [(float(u), _km_at(times, surv, u)) for u in t[e == 0]]
    return np.array(times), np.array(surv), np.array(lo), np.array(hi), cens


def _km_at(times, surv, x):
    i = np.searchsorted(times, x, side="right") - 1
    return float(surv[max(i, 0)])


def _km_median(times, curve):
    below = np.nonzero(np.asarray(curve) <= 0.5)[0]
    return float(times[below[0]]) if len(below) else None


def _logrank(groups_t, groups_e):
    """k-group log-rank test. Returns chi2, df, p, observed and expected events, and the variance matrix."""
    k = len(groups_t)
    all_t = np.concatenate(groups_t)
    all_e = np.concatenate(groups_e)
    O, E = np.zeros(k), np.zeros(k)
    V = np.zeros((k, k))
    for u in np.unique(all_t[all_e > 0]):
        n = np.array([(gt >= u).sum() for gt in groups_t], float)
        d = np.array([((gt == u) & (ge > 0)).sum() for gt, ge in zip(groups_t, groups_e)], float)
        N, D = n.sum(), d.sum()
        if N < 1:
            continue
        O += d
        E += D * n / N
        if N > 1:
            f = D * (N - D) / (N - 1)
            V += f * (np.diag(n / N) - np.outer(n, n) / N ** 2)
    diff = (O - E)[:-1]
    sub = V[:-1, :-1]
    try:
        chi2 = float(diff @ np.linalg.pinv(sub) @ diff)
    except np.linalg.LinAlgError:
        chi2 = float("nan")
    p = float(stats.chi2.sf(chi2, k - 1)) if np.isfinite(chi2) else None
    return chi2, k - 1, p, O, E, V


def analysis_survival(datasets, S, s):
    d = _pick_dataset(datasets, S.get("dataset"))
    df, info = read_table(d["text"])
    num = _numeric_cols(df, info)
    col_notes, notes = [], []
    tcol = _choose(S, "time_col", df, num[0] if num else None, col_notes, "time")
    if not tcol:
        raise UserError("Survival needs a numeric column with the time of death or last follow-up.")
    _need_col(df, tcol, "time")
    auto_e = next((c for c in df.columns if c != tcol and re.search(r"event|status|dead|died|death|censor|outcome", c, re.I)), "")
    # "" means pick automatically, NONE means the user chose not to use that column
    NONE = "__none__"
    ecol = "" if S.get("event_col") == NONE else _choose(S, "event_col", df, auto_e, col_notes, "event") or ""
    auto_g = next((c for c in df.columns if c not in (tcol, ecol) and info["kinds"][c] == "text"), "")
    gcol = "" if S.get("group_col") == NONE else _choose(S, "group_col", df, auto_g, col_notes, "group") or ""
    if ecol == tcol or gcol == tcol:
        raise UserError("Pick different columns for time, event and group.")
    t_all = pd.to_numeric(df[tcol], errors="coerce").to_numpy(float)
    if ecol:
        e_all = _event_flags(df, ecol, info)
    else:
        e_all = np.ones(len(df))
        notes.append("No event column was chosen, so every subject is counted as an event (no censoring). "
                     "Choose one in Settings if some subjects were still alive or lost to follow-up.")
    if gcol:
        labels = _labels(df, gcol, info)
        groups = _order([g for g in dict.fromkeys(labels) if g and g.lower() != "nan"], S.get("group_order"))
    else:
        labels = pd.Series(["All"] * len(df))
        groups = ["All"]
    if (t_all[np.isfinite(t_all)] < 0).any():
        raise UserError("Survival times can't be negative.")
    pct = S.get("y_scale", "pct") == "pct"
    k = 100.0 if pct else 1.0
    unit = (S.get("time_unit") or "").strip()
    G = _gstyles(s, groups)
    fig, ax = _new_fig(s)
    lw, ms = s["line_width"], s["marker_size"]
    rows, med_rows, risk_rows, used_t, used_e = [], [], [], [], []
    ticks_risk = []
    with plt.rc_context(_rc(s)):
        for g in G:
            sel = (labels == g["name"]).to_numpy()
            t, e = t_all[sel], e_all[sel]
            ok = np.isfinite(t)
            t, e = t[ok], e[ok]
            if not len(t):
                notes.append("%s has no usable times." % g["label"])
                continue
            used_t.append(t)
            used_e.append(e)
            times, surv, lo, hi, cens = _km(t, e)
            end = float(t.max())
            xs, ys = np.append(times, end), np.append(surv, surv[-1])
            c = g["color"]
            ax.step(xs, ys * k, where="post", color=c, lw=lw * 1.2, ls=g["ls"], label=g["label"] if len(groups) > 1 else None, zorder=3)
            if S.get("show_ci", True):
                ax.fill_between(xs, np.append(lo, lo[-1]) * k, np.append(hi, hi[-1]) * k, step="post",
                                color=to_rgba(c, s["band_alpha"] * 0.85), lw=0, zorder=1)
            if S.get("show_censors", True) and cens:
                ax.scatter([x for x, _ in cens], [y * k for _, y in cens], marker="|", s=(ms * 1.6) ** 2,
                           color=c, linewidths=max(0.8, lw * 0.8), zorder=4)
            med = _km_median(times, surv)
            med_lo, med_hi = _km_median(times, lo), _km_median(times, hi)
            if med is not None and S.get("show_median", False):
                ax.plot([0, med, med], [50 * k / 100, 50 * k / 100, 0], color=to_rgba(c, 0.7),
                        lw=max(0.6, lw * 0.6), ls=(0, (3, 2)), zorder=2)
            fmt_t = lambda v: "not reached" if v is None else _fmt(v)
            med_rows.append([g["label"], len(t), int(e.sum()), int(len(t) - e.sum()), fmt_t(med),
                             "%s to %s" % (fmt_t(med_lo), fmt_t(med_hi))])
            rows.extend([g["label"], _fmt(x), _r(y * k, 4), _r(a * k, 4), _r(b * k, 4)]
                        for x, y, a, b in zip(times, surv, lo, hi))
            ticks_risk.append((g, t))
        if not used_t:
            raise UserError("No survival times found in the %s column." % tcol)
        ax.set_xlim(left=0)
        ax.set_ylim(0, 105 if pct else 1.05)
        xlabel = "Time" + (" (%s)" % unit if unit else "")
        _finish(ax, s, xlabel=xlabel, ylabel="Percent survival" if pct else "Probability of survival",
                legend=len(groups) > 1)
        if S.get("at_risk", False):
            xt = [x for x in ax.get_xticks() if ax.get_xlim()[0] - 1e-9 <= x <= ax.get_xlim()[1] + 1e-9]
            fs = s["tick_size"] * 0.95
            base = s["label_size"] * 1.6 + s["tick_size"] * 1.4 + s["tick_length"] + s["offset_axes"] + 8
            ax.annotate("Number at risk", xy=(0, 0), xycoords="axes fraction", xytext=(0, -base),
                        textcoords="offset points", ha="left", va="top", fontsize=fs, fontweight="bold")
            for r_i, (g, t) in enumerate(ticks_risk):
                y_off = -base - fs * 1.45 * (r_i + 1)
                ax.annotate(g["label"], xy=(0, 0), xycoords="axes fraction", xytext=(-8, y_off),
                            textcoords="offset points", ha="right", va="top", fontsize=fs, color=g["color"])
                counts = []
                for x in xt:
                    n_r = int((t >= x).sum())
                    counts.append(n_r)
                    ax.annotate(str(n_r), xy=(x, 0), xycoords=("data", "axes fraction"), xytext=(0, y_off),
                                textcoords="offset points", ha="center", va="top", fontsize=fs)
                risk_rows.append([g["label"]] + counts)
            risk_cols = ["Group"] + [_fmt(x) for x in xt]
    summary, tables = [], []
    stat_rows = []
    if len(used_t) > 1:
        chi2, df_, p, O, E, V = _logrank(used_t, used_e)
        if p is not None:
            summary.append({"label": "Log-rank (Mantel-Cox) test", "value": "chi2(%d) = %.2f, %s" % (df_, chi2, fmt_p(p)), "p": _r(p)})
            stat_rows.append(["Log-rank (Mantel-Cox)", "%.3f" % chi2, df_, _r(p), stars(p)])
        names_used = [r[0] for r in med_rows]
        if len(used_t) == 2 and V[0, 0] > 0:
            log_hr = (O[0] - E[0]) / V[0, 0]
            se = 1 / math.sqrt(V[0, 0])
            hr = math.exp(log_hr)
            summary.append({"label": "Hazard ratio (%s vs %s)" % (names_used[0], names_used[1]),
                            "value": "%.2f (95%% CI %.2f to %.2f)" % (hr, math.exp(log_hr - 1.96 * se), math.exp(log_hr + 1.96 * se))})
        if len(used_t) > 2:
            pairs = []
            for i in range(len(used_t)):
                for j in range(i + 1, len(used_t)):
                    pij = _logrank([used_t[i], used_t[j]], [used_e[i], used_e[j]])[2]
                    pairs.append((i, j, 1.0 if pij is None else pij))
            adj = _holm([p for _, _, p in pairs])
            tables.append({"title": "Pairwise log-rank tests (Holm-adjusted)", "columns": ["Comparison", "Adjusted p", "Summary"],
                           "rows": [["%s vs %s" % (names_used[i], names_used[j]), _r(a), stars(a)] for (i, j, _), a in zip(pairs, adj)]})
    tables.insert(0, {"title": "Median survival", "columns": ["Group", "n", "Events", "Censored", "Median", "95% CI"], "rows": med_rows})
    if stat_rows:
        tables.insert(1, {"title": "Curve comparison", "columns": ["Test", "Chi-square", "df", "p", "Summary"], "rows": stat_rows})
    if risk_rows:
        tables.append({"title": "Number at risk", "columns": risk_cols, "rows": risk_rows})
    tables.append({"title": "Survival curve", "columns": ["Group", "Time", "Survival", "Lower 95%", "Upper 95%"], "rows": rows})
    for r in med_rows:
        summary.append({"label": "Median survival (%s)" % r[0] if len(med_rows) > 1 else "Median survival",
                        "value": r[4] + (" %s" % unit if unit and r[4] != "not reached" else "")})
    meth = ("Survival was estimated with the Kaplan-Meier method%s%s. %s"
            % (" (shaded bands: 95% confidence intervals, log-log transformation)" if S.get("show_ci", True) else "",
               "; curves were compared with the log-rank (Mantel-Cox) test%s, and P < 0.05 was considered statistically significant"
               % (" with Holm-adjusted pairwise comparisons" if len(used_t) > 2 else "") if len(used_t) > 1 else "",
               _software_line()))
    return {"figures": [{"id": "survival", "title": "Survival", "fig": fig, "style": s}],
            "tables": tables, "summary": summary, "notes": col_notes + notes, "methods": meth, "groups": groups}


# ----------------------------------------------------------------------------
# Shared helpers for grouped (clustered) figures
# ----------------------------------------------------------------------------

def _text_cols(df, info):
    return [c for c in df.columns if info["kinds"][c] == "text"]


def _guess_col(cols, pattern, exclude=()):
    rx = re.compile(pattern, re.I)
    return next((c for c in cols if c not in exclude and rx.search(str(c))), None)


def _levels(labels, order_text):
    found = [g for g in dict.fromkeys(str(x) for x in labels) if g and g.lower() not in ("nan", "none", "")]
    return _order(found, order_text)


def _legend_proxies(s, G, kind):
    handles = []
    for g in G:
        if kind in ("dot", "mean", "lollipop", "beeswarm", "strip"):
            handles.append(Line2D([], [], ls="none", marker=g["marker"], ms=s["marker_size"] * 1.25, mfc=g["color"],
                                  mec=_point_edge(s, g["color"]), mew=s["point_edge_width"]))
        else:
            if s["fill"] == "solid":
                face = to_rgba(g["color"], 0.92)
            elif s["fill"] == "outline":
                face = (0, 0, 0, 0)
            else:
                face = to_rgba(g["color"], max(s["fill_alpha"], 0.25))
            handles.append(Patch(facecolor=face, edgecolor=g["color"], linewidth=max(0.6, s["edge_width"]),
                                 hatch=g["hatch"]))
    return handles, [g["label"] for g in G]


def _grouped_figure(a_levels, b_levels, cells, s, pairs, ylabel, xlabel="", legend_title="", a_labels=None):
    """Clustered plot: clusters along the category axis = levels of A, coloured members = levels of B.

    cells[(i, j)] holds the values for A level i and B level j. pairs: [((i, j), (i2, j2), p)]."""
    nA, nB = len(a_levels), len(b_levels)
    step = 0.84 / max(nB, 1)
    s2 = dict(s, bar_width=min(1.0, s["bar_width"] / 0.62 * 0.9) * step, jitter=s["jitter"] * step * 1.6)
    Gb = _gstyles(s, b_levels)
    names, values, styles, pos = [], [], [], []
    index = {}
    for i in range(nA):
        for j in range(nB):
            index[(i, j)] = len(names)
            names.append("%s · %s" % (a_levels[i], b_levels[j]))
            values.append(np.asarray(cells.get((i, j), []), float))
            styles.append(Gb[j])
            pos.append(i + (j - (nB - 1) / 2.0) * step)
    fig, ax = _new_fig(s)
    with plt.rc_context(_rc(s)):
        tops = plot_groups(ax, names, values, s2, positions=pos, styles=styles,
                           ticks=(list(range(nA)), a_labels or [str(a) for a in a_levels]))
        H = s["orientation"] == "horizontal"
        if nB > 1:
            ax._ll_legend = _legend_proxies(s, Gb, s["plot_kind"])
        if not s["legend_title"] and legend_title:
            s = dict(s, legend_title=legend_title)
        if s["legend"] == "best":  # "best" lands on top of bars; a row above the plot is cleaner
            s = dict(s, legend="outside top" if not s["title"] else "outside")
        _finish(ax, s, xlabel=ylabel if H else xlabel, ylabel=xlabel if H else ylabel, legend=nB > 1)
        flat = [(index[a], index[b], p) for a, b, p in pairs if a in index and b in index]
        drawn = draw_brackets(ax, flat, tops, s, positions=pos)
    return fig, drawn


# ----------------------------------------------------------------------------
# Two-way ANOVA
# ----------------------------------------------------------------------------

def _effect_coding(idx, k):
    X = np.zeros((len(idx), max(k - 1, 0)))
    for l in range(k - 1):
        X[:, l] = (idx == l).astype(float)
    X[idx == k - 1, :] = -1.0
    return X


def two_way_anova(cells, nA, nB):
    """Type III two-way ANOVA (effect coding). cells[(i, j)] = 1-D arrays. Returns a dict."""
    y, A, B = [], [], []
    for (i, j), v in cells.items():
        v = np.asarray(v, float)
        v = v[np.isfinite(v)]
        y.extend(v)
        A.extend([i] * len(v))
        B.extend([j] * len(v))
    y, A, B = np.asarray(y, float), np.asarray(A), np.asarray(B)
    N = len(y)
    XA, XB = _effect_coding(A, nA), _effect_coding(B, nB)
    XAB = np.column_stack([XA[:, p] * XB[:, q] for p in range(nA - 1) for q in range(nB - 1)])
    one = np.ones((N, 1))

    def sse(*blocks):
        X = np.column_stack(blocks)
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        r = y - X @ beta
        return float(r @ r), r

    full, resid = sse(one, XA, XB, XAB)
    df_err = N - nA * nB
    mse = full / df_err if df_err > 0 else np.nan
    terms = []
    for key, blocks, df in (("A", (one, XB, XAB), nA - 1), ("B", (one, XA, XAB), nB - 1),
                            ("AB", (one, XA, XB), (nA - 1) * (nB - 1))):
        ss = max(sse(*blocks)[0] - full, 0.0)
        F = (ss / df) / mse if mse and mse > 0 else np.nan
        p = float(stats.f.sf(F, df, df_err)) if np.isfinite(F) else np.nan
        terms.append({"key": key, "ss": ss, "df": df, "ms": ss / df, "F": F, "p": p,
                      "eta": ss / (ss + full) if ss + full > 0 else np.nan})
    return {"terms": terms, "sse": full, "df_err": df_err, "mse": mse, "resid": resid, "N": N}


def _sidak(p, m):
    return 1.0 - (1.0 - min(max(p, 0.0), 1.0)) ** m if m > 1 else p


def analysis_twoway(datasets, S, s):
    d = _pick_dataset(datasets, S.get("dataset"))
    df, info = read_table(d["text"])
    notes = []
    num, text = _numeric_cols(df, info), _text_cols(df, info)
    layout = S.get("layout", "long")
    if layout == "grouped":
        # Prism-style grouped table: first text column = rows (factor A), other columns = factor B
        acol = _choose(S, "row_col", df, text[0] if text else None, notes, "row label")
        if not acol:
            raise UserError("The grouped layout needs a first column with row names (factor A).")
        _need_col(df, acol, "row label")
        vcols = [c for c in num if c != acol]
        if not vcols:
            raise UserError("No numeric columns found.")
        a_lab = _labels(df, acol, info)
        a_levels = _levels(a_lab, S.get("a_order"))
        heads = [c.split(" · ", 1)[0] if " · " in c else re.sub(r"[\s_]*(\(?\d+\)?|rep\s*\d+|[A-Z])$", "", c).strip() or c
                 for c in vcols]
        b_levels = _order(list(dict.fromkeys(heads)), S.get("b_order"))
        cells = {}
        for i, a in enumerate(a_levels):
            sel_rows = (a_lab == a).to_numpy()
            for j, b in enumerate(b_levels):
                vals = [pd.to_numeric(df.loc[sel_rows, c], errors="coerce").to_numpy(float)
                        for c, h in zip(vcols, heads) if h == b]
                cells[(i, j)] = np.concatenate(vals) if vals else np.array([])
        a_name = S.get("a_name") or acol
        b_name = S.get("b_name") or ""
        ylabel = S.get("y_name") or ""
    else:
        acol = _choose(S, "a_col", df, text[0] if text else None, notes, "first factor")
        bcol = _choose(S, "b_col", df, next((c for c in text if c != acol), None), notes, "second factor")
        vcol = _choose(S, "value_col", df, num[0] if num else None, notes, "value")
        if not acol or not bcol or not vcol:
            raise UserError("Two-way ANOVA needs two factor columns (e.g. Genotype and Treatment) and a value column.")
        for c, w in ((acol, "first factor"), (bcol, "second factor"), (vcol, "value")):
            _need_col(df, c, w)
        if len({acol, bcol, vcol}) < 3:
            raise UserError("Pick three different columns: two factors and the values.")
        a_lab, b_lab = _labels(df, acol, info), _labels(df, bcol, info)
        a_levels, b_levels = _levels(a_lab, S.get("a_order")), _levels(b_lab, S.get("b_order"))
        v_all = pd.to_numeric(df[vcol], errors="coerce").to_numpy(float)
        cells = {(i, j): v_all[((a_lab == a) & (b_lab == b)).to_numpy()]
                 for i, a in enumerate(a_levels) for j, b in enumerate(b_levels)}
        a_name, b_name = S.get("a_name") or acol, S.get("b_name") or bcol
        ylabel = S.get("y_name") or vcol
    if len(a_levels) < 2 or len(b_levels) < 2:
        raise UserError("Each factor needs at least 2 levels (found %d and %d). With one factor, use Compare groups."
                        % (len(a_levels), len(b_levels)))
    if len(a_levels) * len(b_levels) > 60:
        raise UserError("Too many combinations (%d). Check that the factor columns hold group names, not values."
                        % (len(a_levels) * len(b_levels)))
    transform = S.get("transform", "none")
    if transform in ("log10", "log2", "sqrt"):
        bad = sum(int((v[np.isfinite(v)] <= 0).sum()) if transform != "sqrt" else int((v[np.isfinite(v)] < 0).sum())
                  for v in cells.values())
        f = {"log10": np.log10, "log2": np.log2, "sqrt": np.sqrt}[transform]

        def tf(v):
            v = np.array(v, float)
            v[(v < 0) if transform == "sqrt" else (v <= 0)] = np.nan
            return f(v)
        cells = {k: tf(v) for k, v in cells.items()}
        if bad:
            notes.append("%d value(s) of 0 or below can't be %s-transformed and were left out." % (bad, transform))
        ylabel = "%s(%s)" % (transform, ylabel) if ylabel else transform
    nA, nB = len(a_levels), len(b_levels)
    clean = {k: v[np.isfinite(v)] for k, v in cells.items()}
    empty = ["%s · %s" % (a_levels[i], b_levels[j]) for (i, j), v in clean.items() if len(v) == 0]
    if empty:
        raise UserError("Every combination needs at least one value. Missing: %s." % ", ".join(empty[:6]))
    res = two_way_anova(clean, nA, nB)
    if res["df_err"] < 1:
        raise UserError("Not enough replicates: two-way ANOVA needs more than one value in at least one combination.")
    names = {"A": a_name, "B": b_name or "Columns", "AB": "Interaction"}
    tA = {t["key"]: t for t in res["terms"]}
    summary = []
    for key in ("AB", "A", "B"):
        t = tA[key]
        summary.append({"label": "%s%s" % (names[key], "" if key == "AB" else " (main effect)"),
                        "value": "F(%d, %d) = %.3f, %s, partial η² = %.2f" % (t["df"], res["df_err"], t["F"], fmt_p(t["p"]), t["eta"]),
                        "p": _r(t["p"])})
    if tA["AB"]["p"] < 0.05:
        notes.append("The interaction is significant: the effect of %s depends on %s, so interpret main effects with care "
                     "and look at the comparisons within each level." % (names["B"].lower() if b_name else "the second factor", a_name))
    if len(res["resid"]) >= 3 and np.ptp(res["resid"]) > 0:
        sw = stats.shapiro(res["resid"]).pvalue
        if sw < 0.05:
            notes.append("The residuals don't look normally distributed (Shapiro-Wilk p = %.3f). Consider a log "
                         "transform (Settings) if the data are skewed." % sw)
    ns = [len(v) for v in clean.values()]
    if min(ns) < 3:
        notes.append("Some combinations have fewer than 3 values: treat p-values with caution.")
    if len(set(ns)) > 1:
        notes.append("Group sizes differ (n = %d to %d); Type III sums of squares were used." % (min(ns), max(ns)))
    # Post-hoc comparisons, using the pooled error term
    mode = S.get("posthoc", "within_a")
    mse, dfe = res["mse"], res["df_err"]
    means = {k: float(np.mean(v)) for k, v in clean.items()}
    comps = []
    if mode == "within_a":
        for i in range(nA):
            comps += [((i, j), (i, j2)) for j in range(nB) for j2 in range(j + 1, nB)]
    elif mode == "within_b":
        for j in range(nB):
            comps += [((i, j), (i2, j)) for i in range(nA) for i2 in range(i + 1, nA)]
    elif mode == "all":
        keys = [(i, j) for i in range(nA) for j in range(nB)]
        comps = [(keys[x], keys[y]) for x in range(len(keys)) for y in range(x + 1, len(keys))]
    rows_pc, pairs = [], []
    for a, b in comps:
        diff = means[b] - means[a]
        se = math.sqrt(mse * (1 / len(clean[a]) + 1 / len(clean[b])))
        if mode == "all":
            q = abs(diff) / (se / math.sqrt(2)) if se > 0 else np.inf
            p = float(stats.studentized_range.sf(q, nA * nB, dfe)) if np.isfinite(q) else 0.0
            stat = q
            crit = stats.studentized_range.ppf(0.95, nA * nB, dfe) * se / math.sqrt(2)
        else:
            tval = diff / se if se > 0 else np.inf
            p = _sidak(float(2 * stats.t.sf(abs(tval), dfe)), len(comps)) if np.isfinite(tval) else 0.0
            stat = tval
            crit = stats.t.ppf(1 - (1 - (0.95 ** (1 / max(len(comps), 1)))) / 2, dfe) * se
        lab = lambda k: "%s · %s" % (a_levels[k[0]], b_levels[k[1]])
        rows_pc.append([lab(b) + " vs " + lab(a), _r(diff), _r(diff - crit), _r(diff + crit), _r(stat), _r(p), stars(p)])
        pairs.append((a, b, p))
    ph_name = {"within_a": "Šidák-corrected comparisons of %s levels within each %s level" % (b_name or "column", a_name),
               "within_b": "Šidák-corrected comparisons of %s levels within each %s level" % (a_name, b_name or "column"),
               "all": "Tukey's multiple comparisons test across all combinations"}.get(mode, "")
    fig, drawn = _grouped_figure(a_levels, b_levels, cells, s, pairs, ylabel, xlabel=S.get("a_name") or a_name,
                                 legend_title=b_name)
    if drawn < 0:
        notes.append("%d comparisons are significant, too many to draw as brackets; see the table." % -drawn)
    tables = [{"title": "Two-way ANOVA (Type III)", "columns": ["Source", "SS", "df", "MS", "F", "p", "Partial eta squared", "Summary"],
               "rows": [[names[t["key"]], _r(t["ss"]), t["df"], _r(t["ms"]), _r(t["F"]), _r(t["p"]), _r(t["eta"]), stars(t["p"])]
                        for t in (tA["AB"], tA["A"], tA["B"])] + [["Residual", _r(res["sse"]), dfe, _r(mse), None, None, None, ""]]}]
    cell_names = ["%s · %s" % (a_levels[i], b_levels[j]) for i in range(nA) for j in range(nB)]
    tables.insert(0, describe(cell_names, [clean[(i, j)] for i in range(nA) for j in range(nB)]))
    if rows_pc:
        tables.append({"title": "Multiple comparisons (%s)" % ("Tukey" if mode == "all" else "Šidák"),
                       "columns": ["Comparison", "Mean difference", "95% CI low", "95% CI high", "q" if mode == "all" else "t",
                                   "Adjusted p", "Summary"], "rows": rows_pc})
    meth = ("Data were analyzed by two-way ANOVA with factors %s and %s, including their interaction (Type III sums of "
            "squares)%s.%s %s P < 0.05 was considered statistically significant. %s"
            % (a_name, b_name or "column group",
               "; values were %s-transformed before analysis" % transform if transform in ("log10", "log2", "sqrt") else "",
               (" Post-hoc testing used %s, with the pooled ANOVA error term." % ph_name) if ph_name else "",
               _methods_shown(s), _software_line()))
    return {"figures": [{"id": "twoway", "title": "Two-way ANOVA", "fig": fig, "style": s}],
            "tables": tables, "summary": summary, "notes": notes, "methods": meth, "groups": b_levels}


def _methods_shown(s):
    err = ERROR_NAMES.get(s["error"], "SEM")
    kind = s["plot_kind"]
    if kind == "box":
        return "Box plots show the median and interquartile range."
    if kind in ("violin", "raincloud", "boxviolin"):
        return "Violin plots show the distribution with median and interquartile range."
    if kind in ("beeswarm", "strip"):
        return "Individual values are shown."
    return "Data are shown as mean%s%s." % (" +/- %s" % err if err else "", " with individual values" if s["show_points"] else "")


# ----------------------------------------------------------------------------
# qPCR relative expression (2^-ΔΔCt / Pfaffl)
# ----------------------------------------------------------------------------

_REF_GENES = re.compile(r"^(gapdh|actb|b-?actin|beta-?actin|β-?actin|18s|18s rrna|rn18s|b2m|hprt1?|rplp0|36b4|tbp|ppia|"
                        r"ubc|ywhaz|gusb|rpl\d+[a-z]?|rps\d+[a-z]?|ef1a|eef1a1|pgk1|sdha|hmbs|tubb|tuba1a|cyclophilin|"
                        r"actin|ubiquitin|rp49|rpl32|gapdh-1)$", re.I)


def _parse_eff(text, notes):
    out = {}
    for part in re.split(r"[;,\n]", str(text or "")):
        if not part.strip():
            continue
        m = re.match(r"^\s*(.+?)\s*[=:]\s*([\d.]+)\s*(%?)\s*$", part)
        if not m:
            notes.append('Couldn\'t read the efficiency "%s". Use Gene = 1.95 or Gene = 95%%.' % part.strip())
            continue
        v = float(m.group(2))
        if m.group(3) or v > 2.5:
            v = 1 + v / 100.0
        out[m.group(1).strip().lower()] = v
    return out


def _strip_rep(name):
    t = re.sub(r"[\s_\-.]*(rep|r|#)?\s*\(?\d+\)?[a-z]?$", "", str(name), flags=re.I).strip()
    return t or str(name)


def analysis_qpcr(datasets, S, s):
    d = _pick_dataset(datasets, S.get("dataset"))
    df, info = read_table(d["text"])
    notes = []
    cols = list(df.columns)
    text, num = _text_cols(df, info), _numeric_cols(df, info)
    layout = S.get("layout", "long")
    scol = _choose(S, "sample_col", df, _guess_col(text, r"sample|animal|subject|mouse|patient|^id$|name") or (text[0] if text else None), notes, "sample")
    gcol_set = S.get("group_col") or ""
    if gcol_set == "__none__":
        gcol = None
    else:
        gcol = _choose(S, "group_col", df, _guess_col(text, r"group|condition|treat|genotype|cohort", exclude=(scol,)), notes, "group")
    rows = []  # (sample, group, gene, ct)
    undetermined = 0
    if layout == "long":
        tcol = _choose(S, "gene_col", df, _guess_col(text, r"target|gene|detector|assay|primer", exclude=(scol, gcol)), notes, "gene")
        ccol = _choose(S, "ct_col", df, _guess_col(cols, r"^c[tq]$|\bc[tq]\b|cq|ct ?mean|threshold cycle", exclude=(scol, gcol, tcol))
                       or (num[0] if num else None), notes, "Ct")
        if not scol or not tcol or not ccol:
            raise UserError("qPCR (long layout) needs a sample column, a gene/target column and a Ct column.")
        for c, w in ((scol, "sample"), (tcol, "gene"), (ccol, "Ct")):
            _need_col(df, c, w)
        raw_ct = df[ccol].astype(str).str.strip()
        undetermined = int(raw_ct.str.lower().isin(["undetermined", "undet", "no ct", "n/a", "nd", "no amp"]).sum())
        ct = pd.to_numeric(df[ccol], errors="coerce").to_numpy(float)
        sl, tl = _labels(df, scol, info), _labels(df, tcol, info)
        gl = _labels(df, gcol, info) if gcol and gcol in df.columns else None
        for k in range(len(df)):
            if str(tl.iloc[k]).lower() in ("nan", ""):
                continue
            g = gl.iloc[k] if gl is not None else _strip_rep(sl.iloc[k])
            rows.append((str(sl.iloc[k]), str(g), str(tl.iloc[k]), ct[k]))
    else:
        if not scol:
            raise UserError("qPCR (wide layout) needs a sample column and one Ct column per gene.")
        genes_cols = [c for c in (S.get("gene_cols") or []) if c in df.columns] or [c for c in num if c not in (scol, gcol)]
        if not genes_cols:
            raise UserError("No Ct columns found. Each gene should be its own numeric column.")
        sl = _labels(df, scol, info)
        gl = _labels(df, gcol, info) if gcol and gcol in df.columns else None
        for c in genes_cols:
            undetermined += int(df[c].astype(str).str.strip().str.lower().isin(["undetermined", "undet", "no ct", "nd"]).sum())
            ct = pd.to_numeric(df[c], errors="coerce").to_numpy(float)
            for k in range(len(df)):
                g = gl.iloc[k] if gl is not None else _strip_rep(sl.iloc[k])
                rows.append((str(sl.iloc[k]), str(g), str(c), ct[k]))
    if gcol is None or gcol not in df.columns:
        notes.append("No group column: groups were taken from sample names without their trailing numbers.")
    if undetermined:
        notes.append("%d well(s) were Undetermined (no amplification) and were left out." % undetermined)
    max_ct = _f(S, "max_ct", 35.0)
    late = [r for r in rows if np.isfinite(r[3]) and max_ct and r[3] > max_ct]
    if late:
        notes.append("%d well(s) have Ct above %g, near the detection limit: %s." % (
            len(late), max_ct, ", ".join("%s %s (%.1f)" % (r[0], r[2], r[3]) for r in late[:5]) + (" …" if len(late) > 5 else "")))
    data = pd.DataFrame(rows, columns=["sample", "group", "gene", "ct"])
    data = data[np.isfinite(data["ct"])]
    if data.empty:
        raise UserError("No Ct values found.")
    genes = list(dict.fromkeys(data["gene"]))
    # Technical replicates
    tech = data.groupby(["sample", "group", "gene"], sort=False)["ct"].agg(["mean", "std", "count"]).reset_index()
    spread = tech[(tech["count"] > 1) & (tech["std"] > _f(S, "rep_sd", 0.5))]
    if len(spread):
        notes.append("Technical replicates disagree by more than %.2g Ct (SD) in %d sample/gene pair(s): %s." % (
            _f(S, "rep_sd", 0.5), len(spread),
            ", ".join("%s %s (SD %.2f)" % (r["sample"], r["gene"], r["std"]) for _, r in spread.head(5).iterrows())))
    refs_set = [g for g in (S.get("ref_genes") or []) if g in genes]
    refs = refs_set or [g for g in genes if _REF_GENES.match(g.strip())] or genes[:1]
    if not refs_set:
        notes.append("Reference gene%s: %s (chosen automatically; change it in Settings)." % ("s" if len(refs) > 1 else "", ", ".join(refs)))
    targets = [g for g in genes if g not in refs]
    tsel = [g for g in (S.get("targets") or []) if g in targets]
    targets = tsel or targets
    if not targets:
        raise UserError("Every gene is a reference gene. Choose which genes are references in Settings.")
    groups = _order(list(dict.fromkeys(tech["group"])), S.get("group_order"))
    ctrl = S.get("control_group") if S.get("control_group") in groups else groups[0]
    if ctrl != groups[0]:
        groups = [ctrl] + [g for g in groups if g != ctrl]
    effs = _parse_eff(S.get("efficiency"), notes)
    E = {g: effs.get(g.lower(), 2.0) for g in genes}
    pivot = tech.pivot_table(index=["sample", "group"], columns="gene", values="mean", aggfunc="mean").reset_index()
    missing_ref = pivot[refs].isna().any(axis=1)
    if missing_ref.any():
        notes.append("%d sample(s) lack a reference Ct and were left out: %s." % (
            int(missing_ref.sum()), ", ".join(pivot.loc[missing_ref, "sample"].astype(str).head(6))))
        pivot = pivot[~missing_ref]
    is_ctrl = pivot["group"] == ctrl
    if not is_ctrl.any():
        raise UserError('No samples in the control group "%s".' % ctrl)
    # Pfaffl-style relative quantities against the control group's mean Ct (identical to 2^-ΔΔCt when E = 2)
    rq_ref = np.ones(len(pivot))
    for r in refs:
        rq_ref = rq_ref * E[r] ** (pivot.loc[is_ctrl, r].mean() - pivot[r].to_numpy(float))
    rq_ref = rq_ref ** (1.0 / len(refs))
    ref_ct = pivot[refs].mean(axis=1).to_numpy(float)
    out_kind = S.get("output", "fold")
    unit = {"fold": "Fold change (2^-ΔΔCt)", "log2": "log2 fold change", "dct": "Relative expression (2^-ΔCt)"}[out_kind if out_kind in ("fold", "log2", "dct") else "fold"]
    if any(abs(E[g] - 2.0) > 1e-9 for g in genes):
        unit = unit.replace(" (2^-ΔΔCt)", " (Pfaffl)")
    per_sample, cells, summary, stats_rows, pairs_all, gene_tables = [], {}, [], [], [], []
    posthoc = S.get("posthoc", "control")
    last_method = ""
    for gi, gene in enumerate(targets):
        ok = pivot[gene].notna().to_numpy()
        ctg = pivot[gene].to_numpy(float)
        rq_t = E[gene] ** (pivot.loc[is_ctrl & ok, gene].mean() - ctg)
        ratio = rq_t / rq_ref
        log2r = np.log2(ratio)
        if out_kind == "dct":
            vals_plot = E[gene] ** (-(ctg - ref_ct))
        elif out_kind == "log2":
            vals_plot = log2r
        else:
            vals_plot = ratio
        dct = ctg - ref_ct
        ddct = dct - np.nanmean(dct[(is_ctrl & ok).to_numpy()])
        for k in np.where(ok)[0]:
            per_sample.append([pivot["sample"].iloc[k], pivot["group"].iloc[k], gene, _r(ctg[k], 4), _r(ref_ct[k], 4),
                               _r(dct[k], 4), _r(ddct[k], 4), _r(ratio[k], 5), _r(log2r[k], 4)])
        vals_g, logs_g = [], []
        for j, grp in enumerate(groups):
            sel_g = ((pivot["group"] == grp).to_numpy()) & ok
            cells[(gi, j)] = vals_plot[sel_g]
            vals_g.append(vals_plot[sel_g])
            logs_g.append(log2r[sel_g])
            lv = log2r[sel_g]
            n = len(lv)
            gm = 2 ** np.mean(lv) if n else np.nan
            h = stats.t.ppf(0.975, n - 1) * np.std(lv, ddof=1) / math.sqrt(n) if n > 1 else np.nan
            fr = ratio[sel_g]
            gene_tables.append([gene, grp, n, _r(np.mean(fr) if n else np.nan), _r(np.std(fr, ddof=1) / math.sqrt(n) if n > 1 else np.nan),
                                _r(gm), _r(2 ** (np.mean(lv) - h) if n > 1 else np.nan), _r(2 ** (np.mean(lv) + h) if n > 1 else np.nan)])
        if len(groups) >= 2:
            res = compare_stats(groups, logs_g, paired=False, mode=S.get("test", "param"), posthoc=posthoc,
                                equal_var=S.get("equal_var", False))
            for n_ in res["notes"]:
                notes.append("%s: %s" % (gene, n_))
            if res["test"]:
                summary.append({"label": "%s: %s" % (gene, res["test"]), "value": res["line"], "p": _r(res["p"])})
                for i, j, p in res["pairs"]:
                    stats_rows.append([gene, "%s vs %s" % (groups[j], groups[i]), _r(p), stars(p)])
                    pairs_all.append(((gi, i), (gi, j), p))
            last_method = res.get("method", "")
    # Reference gene stability: its Ct shouldn't differ between groups
    for r in refs:
        vals_r = [pivot.loc[pivot["group"] == g, r].dropna().to_numpy(float) for g in groups]
        if len(groups) >= 2 and all(len(v) >= 2 for v in vals_r):
            try:
                p_ref = stats.f_oneway(*vals_r).pvalue if len(groups) > 2 else stats.ttest_ind(*vals_r, equal_var=False).pvalue
            except Exception:
                p_ref = np.nan
            if np.isfinite(p_ref) and p_ref < 0.05:
                notes.append("Reference gene %s has different Ct values between groups (p = %.3f), so it may not be a "
                             "stable reference here. Consider another or add a second reference gene." % (r, p_ref))
    s = dict(s)
    if len(targets) == 1:
        res_fig = {"pairs": [(a[1], b[1], p) for a, b, p in pairs_all], "notes": []}
        fig = _group_figure(groups, [cells[(0, j)] for j in range(len(groups))], s, res_fig, False,
                            unit, title=s["title"] or targets[0])
        if res_fig["notes"]:
            notes += res_fig["notes"]
    else:
        fig, drawn = _grouped_figure(targets, groups, cells, s, pairs_all, unit, xlabel="", legend_title="")
        if drawn < 0:
            notes.append("Too many significant comparisons to draw as brackets; see the table.")
    tables = [{"title": "Relative expression by group", "columns": ["Gene", "Group", "n", "Mean fold", "SEM", "Geometric mean",
                                                                   "95% CI low", "95% CI high"], "rows": gene_tables}]
    if stats_rows:
        tables.append({"title": "Comparisons (on log2 fold change)", "columns": ["Gene", "Comparison", "p", "Summary"], "rows": stats_rows})
    tables.append({"title": "Per sample", "columns": ["Sample", "Group", "Gene", "Ct (target)", "Ct (reference)", "ΔCt",
                                                       "ΔΔCt", "Fold change", "log2 fold"], "rows": per_sample})
    tables.append({"title": "Technical replicates", "columns": ["Sample", "Group", "Gene", "Mean Ct", "SD", "Wells"],
                   "rows": [[r["sample"], r["group"], r["gene"], _r(r["mean"], 4), _r(r["std"], 3), int(r["count"])]
                            for _, r in tech.iterrows()]})
    eff_txt = "assuming 100%% amplification efficiency (2^-ΔΔCt method)" if all(abs(E[g] - 2) < 1e-9 for g in genes) else \
        "with gene-specific amplification efficiencies (Pfaffl method; %s)" % ", ".join("%s %.0f%%" % (g, (E[g] - 1) * 100) for g in genes)
    meth = ("Relative gene expression was calculated from quantification cycle (Ct) values %s, normalized to %s and "
            "expressed relative to the mean of the %s group. Technical replicates were averaged. Statistical comparisons "
            "were performed on log2-transformed fold changes. %s %s"
            % (eff_txt, " and ".join(refs) if len(refs) < 3 else "the geometric mean of " + ", ".join(refs), ctrl,
               last_method, _software_line()))
    return {"figures": [{"id": "qpcr", "title": "Relative expression", "fig": fig, "style": s}],
            "tables": tables, "summary": summary, "notes": notes, "methods": meth, "groups": groups,
            "meta": {"qpcr": {"genes": genes, "groups": groups, "refs": refs}}}


# ----------------------------------------------------------------------------
# Standard curve: fit standards, interpolate unknowns (ELISA, BCA, Bradford, qPCR standards...)
# ----------------------------------------------------------------------------

STD_MODELS = {
    "linear": "Straight line", "linear0": "Straight line through the origin", "quadratic": "Second-order polynomial",
    "4pl": "Four-parameter logistic (4PL)", "5pl": "Five-parameter logistic (5PL)", "loglog": "Linear on log-log axes",
}


def _std_fit(model, x, y, w):
    """Returns (predict(x), inverse(y), params dict, n_params)."""
    sigma = 1 / np.sqrt(w) if w is not None else None
    if model in ("linear", "linear0", "quadratic"):
        if model == "linear0":
            b = float(np.sum((w if w is not None else 1) * x * y) / np.sum((w if w is not None else 1) * x * x))
            return (lambda xx: b * xx), (lambda yy: yy / b), {"Slope": b}, 1
        deg = 1 if model == "linear" else 2
        c = np.polyfit(x, y, deg, w=np.sqrt(w) if w is not None else None)
        if deg == 1:
            b, a = c
            return (lambda xx: a + b * xx), (lambda yy: (yy - a) / b), {"Intercept": a, "Slope": b}, 2
        c2, c1, c0 = c
        lo, hi = float(np.min(x)), float(np.max(x))

        def inv(yy):
            yy = np.atleast_1d(np.asarray(yy, float))
            out = np.full(len(yy), np.nan)
            for k, v in enumerate(yy):
                roots = np.roots([c2, c1, c0 - v])
                roots = roots[np.isreal(roots)].real
                if len(roots):
                    mid = (lo + hi) / 2
                    out[k] = roots[np.argmin(np.abs(roots - mid))]
            return out
        return (lambda xx: c0 + c1 * xx + c2 * xx ** 2), inv, {"Intercept": c0, "X": c1, "X²": c2}, 3
    if model == "loglog":
        ok = (x > 0) & (y > 0)
        b, a = np.polyfit(np.log10(x[ok]), np.log10(y[ok]), 1)
        return (lambda xx: 10 ** (a + b * np.log10(xx))), (lambda yy: 10 ** ((np.log10(yy) - a) / b)), \
            {"log intercept": a, "log slope": b}, 2
    # 4PL / 5PL: y = d + (a - d) / (1 + (x/c)^b)^g
    pos = x > 0
    xs, ys = x[pos], y[pos]
    order = np.argsort(xs)
    a0, d0 = float(ys[order][:2].mean()), float(ys[order][-2:].mean())
    c0 = float(np.exp(np.mean(np.log(xs))))

    def f4(xx, a, b, c, d):
        with np.errstate(all="ignore"):
            return d + (a - d) / (1 + (np.abs(xx) / abs(c)) ** b)

    def f5(xx, a, b, c, d, g):
        with np.errstate(all="ignore"):
            return d + (a - d) / (1 + (np.abs(xx) / abs(c)) ** b) ** g
    sg = sigma[pos] if sigma is not None else None
    if model == "4pl":
        p, _ = curve_fit(f4, xs, ys, p0=[a0, 1.0, c0, d0], sigma=sg, maxfev=40000)
        a, b, c, d = p
        c = abs(c)
        inv = lambda yy: c * (np.where((yy - d) * (a - d) > 0, (a - d) / (yy - d), np.nan) - 1) ** (1 / b)
        return (lambda xx: f4(xx, a, b, c, d)), (lambda yy: np.asarray(_nan_neg(inv(np.asarray(yy, float))))), \
            {"Bottom (a)": a, "Hill slope (b)": b, "Inflection (c)": c, "Top (d)": d}, 4
    p, _ = curve_fit(f5, xs, ys, p0=[a0, 1.0, c0, d0, 1.0], sigma=sg, maxfev=60000,
                     bounds=([-np.inf, -50, 1e-12, -np.inf, 0.05], [np.inf, 50, np.inf, np.inf, 20]))
    a, b, c, d, g = p
    inv5 = lambda yy: c * (np.where((yy - d) * (a - d) > 0, ((a - d) / (yy - d)) ** (1 / g), np.nan) - 1) ** (1 / b)
    return (lambda xx: f5(xx, a, b, c, d, g)), (lambda yy: np.asarray(_nan_neg(inv5(np.asarray(yy, float))))), \
        {"Bottom (a)": a, "Hill slope (b)": b, "Inflection (c)": c, "Top (d)": d, "Asymmetry (g)": g}, 5


def _nan_neg(v):
    with np.errstate(all="ignore"):
        v = np.asarray(v, float)
        return np.where(np.isfinite(v) & (v >= 0), v, np.nan)


def analysis_stdcurve(datasets, S, s):
    d = _pick_dataset(datasets, S.get("dataset"))
    df, info = read_table(d["text"])
    notes = []
    num, text = _numeric_cols(df, info), _text_cols(df, info)
    xcol = _choose(S, "conc_col", df, _guess_col(num, r"conc|standard|std|amount|µg|ug|ng|pg|mg|copies|known") or (num[0] if num else None), notes, "concentration")
    if not xcol:
        raise UserError("Choose the column with the standards' known concentrations.")
    _need_col(df, xcol, "concentration")
    ycols = [c for c in (S.get("signal_cols") or []) if c in df.columns and c != xcol] or [c for c in num if c != xcol]
    if not ycols:
        raise UserError("Choose the signal column(s): absorbance, fluorescence, Ct...")
    ncol = S.get("name_col") or ""
    ncol = ncol if ncol in df.columns else (_guess_col(text, r"sample|name|well|id|label") or (text[0] if text else ""))
    dcol = S.get("dilution_col") or ""
    x = pd.to_numeric(df[xcol], errors="coerce").to_numpy(float)
    Y = np.column_stack([pd.to_numeric(df[c], errors="coerce").to_numpy(float) for c in ycols])
    with np.errstate(all="ignore"):
        ymean = np.nanmean(np.where(np.isfinite(Y), Y, np.nan), axis=1)
    has_y = np.isfinite(Y).any(axis=1)
    std_rows = np.isfinite(x) & has_y
    unk_rows = ~np.isfinite(x) & has_y
    if std_rows.sum() < 3:
        raise UserError("Need at least 3 standards: rows with a known concentration and a signal. Leave the "
                        "concentration empty for unknown samples.")
    blank_mode = S.get("blank", "none")
    blank_vals = Y[std_rows & (x == 0)].reshape(-1)
    blank_vals = blank_vals[np.isfinite(blank_vals)]
    blank = float(np.mean(blank_vals)) if len(blank_vals) else 0.0
    if blank_mode == "subtract":
        if not len(blank_vals):
            notes.append("No blank (a standard with concentration 0) was found, so nothing was subtracted.")
        else:
            Y = Y - blank
            ymean = ymean - blank
    model = S.get("model", "4pl")
    if model not in STD_MODELS:
        model = "4pl"
    log_model = model in ("4pl", "5pl", "loglog")
    sx_all = np.repeat(x[std_rows], Y.shape[1])
    sy_all = Y[std_rows].reshape(-1)
    ok = np.isfinite(sy_all)
    if blank_mode == "subtract" and len(blank_vals):
        ok &= sx_all != 0
    if log_model:
        ok &= sx_all > 0
        if (x[std_rows] <= 0).any() and not (blank_mode == "subtract" and len(blank_vals) and (x[std_rows] >= 0).all()):
            notes.append("Standards at concentration 0 can't sit on a log axis and were left out of the %s fit." % model.upper())
    if model == "loglog":
        ok &= sy_all > 0
    sx, sy = sx_all[ok], sy_all[ok]
    if len(np.unique(sx)) < (5 if model == "5pl" else 4 if model == "4pl" else 3 if model == "quadratic" else 2):
        raise UserError("Not enough distinct standard concentrations for the %s model." % STD_MODELS[model].lower())
    wmode = S.get("weight", "none")
    w = None
    if wmode in ("1/y", "1/y2"):
        yy = np.abs(sy).clip(1e-12)
        w = 1 / yy if wmode == "1/y" else 1 / yy ** 2
    try:
        pred, inv, params, k = _std_fit(model, sx, sy, w)
    except Exception as e:
        raise UserError("The %s fit did not converge (%s). Try another model." % (STD_MODELS[model], e))
    fit = pred(sx)
    ss_res = float(np.sum((sy - fit) ** 2))
    ss_tot = float(np.sum((sy - sy.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
    lo_std, hi_std = float(np.min(sx)), float(np.max(sx))
    unit = S.get("unit") or ""
    global_dil = _f(S, "dilution", 1.0) or 1.0
    dil = pd.to_numeric(df[dcol], errors="coerce").to_numpy(float) if dcol in df.columns else np.full(len(df), np.nan)
    dil = np.where(np.isfinite(dil) & (dil > 0), dil, global_dil)
    # Back-calculated standards (accuracy)
    back_rows = []
    for xv in np.unique(x[std_rows]):
        if log_model and xv <= 0:
            continue
        sig = np.nanmean(Y[std_rows & (x == xv)])
        bc = float(np.asarray(inv(np.array([sig])), float).ravel()[0]) if np.isfinite(sig) else np.nan
        back_rows.append([_r(xv), _r(sig), _r(bc), _r(bc / xv * 100 if xv and np.isfinite(bc) else np.nan, 4)])
    bad_rec = [r for r in back_rows if r[3] is not None and abs(r[3] - 100) > 20]
    if bad_rec:
        notes.append("%d standard(s) back-calculate more than 20%% away from their nominal value (%s). Consider another "
                     "model, weighting or dropping that standard." % (len(bad_rec), ", ".join(_fmt(r[0]) for r in bad_rec[:5])))
    # LOD / LOQ from blank replicates
    lod_txt = ""
    if len(blank_vals) >= 2:
        sd_b = float(np.std(blank_vals, ddof=1))
        base = 0.0 if blank_mode == "subtract" else blank
        rising = pred(np.array([hi_std]))[0] > pred(np.array([lo_std]))[0]
        sgn = 1 if rising else -1
        lod = float(np.asarray(inv(np.array([base + sgn * 3 * sd_b])), float).ravel()[0])
        loq = float(np.asarray(inv(np.array([base + sgn * 10 * sd_b])), float).ravel()[0])
        lod_txt = (lod, loq)
    # Unknowns
    unk_table, ux, uy, flags = [], [], [], 0
    idx = np.where(unk_rows)[0]
    names = _labels(df, ncol, info) if ncol else pd.Series(["Sample %d" % (k + 1) for k in range(len(df))])
    for n_i, k in enumerate(idx):
        sig = ymean[k]
        reps = Y[k][np.isfinite(Y[k])]
        cv = float(np.std(reps, ddof=1) / np.mean(reps) * 100) if len(reps) > 1 and np.mean(reps) != 0 else np.nan
        xc = float(np.asarray(inv(np.array([sig])), float).ravel()[0]) if np.isfinite(sig) else np.nan
        flag = ""
        if not np.isfinite(xc):
            flag = "Outside the curve"
        elif xc > hi_std * 1.0001:
            flag = "Above the top standard"
        elif xc < lo_std * 0.9999:
            flag = "Below the lowest standard"
        if lod_txt and np.isfinite(xc) and xc < lod_txt[0]:
            flag = (flag + "; " if flag else "") + "below LOD"
        if flag:
            flags += 1
        nm = str(names.iloc[k]) if ncol else "Sample %d" % (n_i + 1)
        unk_table.append([nm, _r(sig), _r(cv, 3), _r(xc), _r(dil[k]), _r(xc * dil[k] if np.isfinite(xc) else np.nan), flag])
        if np.isfinite(xc):
            ux.append(xc)
            uy.append(sig)
    if flags:
        notes.append("%d unknown(s) fall outside the standard range or the curve; their values are extrapolated or "
                     "missing. Dilute and re-run them if you can." % flags)
    high_cv = [r for r in unk_table if r[2] is not None and r[2] > 15]
    if high_cv:
        notes.append("%d unknown(s) have replicate CV above 15%%: %s." % (len(high_cv), ", ".join(r[0] for r in high_cv[:5])))
    # Figure
    G = _gstyles(s, ["Standards", "Unknowns"])
    fig, ax = _new_fig(s)
    lw, ms = s["line_width"], s["marker_size"]
    with plt.rc_context(_rc(s)):
        c_std, c_unk = G[0]["color"], G[1]["color"]
        ux_std = np.unique(sx)
        means = np.array([np.mean(sy[sx == u]) for u in ux_std])
        errs = np.array([_err(sy[sx == u], s["error"]) for u in ux_std])
        if Y.shape[1] > 1:
            ax.errorbar(ux_std, means, yerr=errs if s["error"] != "none" else None, fmt=G[0]["marker"], color=c_std,
                        ms=ms * 1.1, mec=_point_edge(s, c_std), mew=s["point_edge_width"], elinewidth=s["error_width"] * 0.8,
                        capsize=s["cap_size"] * 0.7, ls="none", zorder=4, label=G[0]["label"])
        else:
            ax.scatter(sx, sy, s=ms ** 2 * 1.3, marker=G[0]["marker"], color=to_rgba(c_std, s["point_alpha"]),
                       edgecolor=_point_edge(s, c_std), linewidth=s["point_edge_width"], zorder=4, label=G[0]["label"])
        span_lo = min([lo_std] + [v for v in ux if v > 0]) if log_model else min([lo_std] + ux)
        span_hi = max([hi_std] + ux)
        xx = np.geomspace(span_lo / 1.3, span_hi * 1.3, 400) if log_model else np.linspace(min(0, span_lo), span_hi * 1.05, 400)
        ax.plot(xx, pred(xx), color=c_std, lw=lw * 1.2, ls=G[0]["ls"], zorder=3, label="%s fit" % STD_MODELS[model].split(" (")[0])
        if log_model:
            ax.set_xscale("log")
        if model == "loglog":
            ax.set_yscale("log")
        if ux and S.get("show_unknowns", True):
            ax.scatter(ux, uy, s=ms ** 2 * 1.5, marker=G[1]["marker"] if G[1]["marker"] != "o" else "D",
                       facecolor=s["bg"], edgecolor=c_unk, linewidth=max(1.0, lw * 0.8), zorder=5, label=G[1]["label"])
            if S.get("guides", True) and len(ux) <= 40:
                y0 = ax.get_ylim()[0]
                x0 = ax.get_xlim()[0]
                for a_, b_ in zip(ux, uy):
                    ax.plot([x0, a_, a_], [b_, b_, y0], color=to_rgba(c_unk, 0.35), lw=max(0.5, lw * 0.5),
                            ls=(0, (2, 2)), zorder=2)
                ax.set_xlim(left=x0)
                ax.set_ylim(bottom=y0)
        if S.get("annotate", True):
            ax.text(0.97, 0.04, "R² = %.4f" % r2, transform=ax.transAxes, ha="right", va="bottom", fontsize=s["font_size"] * 0.85)
        _finish(ax, s, xlabel="%s%s" % (S.get("x_name") or xcol, " (%s)" % unit if unit and unit not in (S.get("x_name") or xcol) else ""),
                ylabel=S.get("y_name") or (ycols[0] if len(ycols) == 1 else "Signal"), legend=True)
    summary = [{"label": "Standard curve", "value": "%s, R² = %.4f, %d standards" % (STD_MODELS[model], r2, len(np.unique(sx))), "p": None}]
    if lod_txt:
        u = " " + unit if unit else ""
        summary.append({"label": "Limits from blanks", "value": "LOD %s%s, LOQ %s%s (blank + 3 SD / 10 SD)" % (
            _fmt(lod_txt[0]), u, _fmt(lod_txt[1]), u), "p": None})
    if unk_table:
        ok_unk = [r for r in unk_table if r[5] is not None]
        summary.append({"label": "Unknowns", "value": "%d interpolated%s" % (len(ok_unk), ", %d flagged" % flags if flags else ""), "p": None})
    tables = [{"title": "Unknown samples", "columns": ["Sample", "Mean signal", "Replicate CV (%)", "Interpolated", "Dilution",
                                                        "Concentration%s" % (" (%s)" % unit if unit else ""), "Flag"], "rows": unk_table},
              {"title": "Curve parameters", "columns": ["Parameter", "Value"],
               "rows": [[k_, _r(v_)] for k_, v_ in params.items()] + [["R squared", _r(r2)], ["Standards used", len(sx)]]},
              {"title": "Back-calculated standards", "columns": ["Nominal", "Mean signal", "Back-calculated", "Recovery (%)"], "rows": back_rows}]
    if not unk_table:
        notes.append("No unknowns found. Add rows with a signal and an empty concentration to interpolate them.")
    meth = ("Concentrations were interpolated from a standard curve fitted with a %s model%s%s (R² = %.4f). %s%s %s"
            % (STD_MODELS[model].lower(), {"1/y": ", weighted 1/Y", "1/y2": ", weighted 1/Y²"}.get(wmode, ""),
               ", after subtracting the mean blank" if blank_mode == "subtract" and len(blank_vals) else "", r2,
               "Samples were multiplied by their dilution factor. " if (dcol in df.columns or global_dil != 1) else "",
               "The limit of detection was defined as the mean blank plus 3 SD. " if lod_txt else "", _software_line()))
    return {"figures": [{"id": "stdcurve", "title": "Standard curve", "fig": fig, "style": s}],
            "tables": tables, "summary": summary, "notes": notes, "methods": meth, "groups": ["Standards", "Unknowns"]}


# ----------------------------------------------------------------------------
# Nonlinear curve fitting with a model library and custom equations
# ----------------------------------------------------------------------------

def _g_lin(x, y):
    b, a = np.polyfit(x, y, 1) if np.ptp(x) > 0 else (0.0, float(np.mean(y)))
    return a, b


FIT_MODELS = {
    "linear": ("Straight line", "Y = Intercept + Slope·X", ["Intercept", "Slope"],
               lambda x, a, b: a + b * x, lambda x, y: list(_g_lin(x, y))),
    "poly2": ("Quadratic", "Y = B0 + B1·X + B2·X²", ["B0", "B1", "B2"],
              lambda x, a, b, c: a + b * x + c * x ** 2, lambda x, y: list(np.polyfit(x, y, 2)[::-1])),
    "poly3": ("Cubic", "Y = B0 + B1·X + B2·X² + B3·X³", ["B0", "B1", "B2", "B3"],
              lambda x, a, b, c, d: a + b * x + c * x ** 2 + d * x ** 3, lambda x, y: list(np.polyfit(x, y, 3)[::-1])),
    "exp_growth": ("Exponential growth", "Y = Y0·exp(K·X)", ["Y0", "K"],
                   lambda x, y0, k: y0 * np.exp(k * x),
                   lambda x, y: [float(y[np.argmin(x)]) or 1.0, math.log(max(abs(y.max()), 1e-9) / max(abs(y.min()), 1e-9)) / (np.ptp(x) or 1)]),
    "exp_decay": ("One-phase decay", "Y = (Y0 − Plateau)·exp(−K·X) + Plateau", ["Y0", "Plateau", "K"],
                  lambda x, y0, pl, k: (y0 - pl) * np.exp(-k * x) + pl,
                  lambda x, y: [float(y[np.argmin(x)]), float(y[np.argmax(x)]), 3.0 / (np.ptp(x) or 1)]),
    "exp_assoc": ("One-phase association", "Y = Y0 + (Plateau − Y0)·(1 − exp(−K·X))", ["Y0", "Plateau", "K"],
                  lambda x, y0, pl, k: y0 + (pl - y0) * (1 - np.exp(-k * x)),
                  lambda x, y: [float(y[np.argmin(x)]), float(y[np.argmax(x)]), 3.0 / (np.ptp(x) or 1)]),
    "exp2_decay": ("Two-phase decay", "Y = Plateau + SpanFast·exp(−Kfast·X) + SpanSlow·exp(−Kslow·X)",
                   ["Plateau", "SpanFast", "Kfast", "SpanSlow", "Kslow"],
                   lambda x, pl, sf, kf, ss, ks: pl + sf * np.exp(-kf * x) + ss * np.exp(-ks * x),
                   lambda x, y: [float(y[np.argmax(x)]), float(np.ptp(y)) * 0.6, 10.0 / (np.ptp(x) or 1),
                                 float(np.ptp(y)) * 0.4, 1.0 / (np.ptp(x) or 1)]),
    "mm": ("Michaelis-Menten", "Y = Vmax·X / (Km + X)", ["Vmax", "Km"],
           lambda x, vm, km: vm * x / (km + x), lambda x, y: [float(y.max()) * 1.1, float(np.median(x)) or 1.0]),
    "hill": ("Specific binding with Hill slope", "Y = Bmax·Xʰ / (Kdʰ + Xʰ)", ["Bmax", "Kd", "h"],
             lambda x, bm, kd, h: bm * np.abs(x) ** h / (np.abs(kd) ** h + np.abs(x) ** h),
             lambda x, y: [float(y.max()) * 1.1, float(np.median(x[x > 0])) if (x > 0).any() else 1.0, 1.0]),
    "one_site": ("One site, total binding", "Y = Bmax·X / (Kd + X) + NS·X", ["Bmax", "Kd", "NS"],
                 lambda x, bm, kd, ns: bm * x / (kd + x) + ns * x,
                 lambda x, y: [float(y.max()), float(np.median(x)) or 1.0, 0.0]),
    "logistic": ("Logistic growth", "Y = K / (1 + ((K − Y0)/Y0)·exp(−r·X))", ["Y0", "K", "r"],
                 lambda x, y0, K, r: K / (1 + ((K - y0) / y0) * np.exp(-r * x)),
                 lambda x, y: [max(float(y[np.argmin(x)]), 1e-6 * max(abs(float(y.max())), 1)), float(y.max()) * 1.05, 4.0 / (np.ptp(x) or 1)]),
    "gompertz": ("Gompertz growth (lag time)", "Y = Y0 + A·exp(−exp(μ·e/A·(λ − X) + 1))", ["Y0", "A", "mu", "lag"],
                 lambda x, y0, A, mu, lag: y0 + A * np.exp(-np.exp(mu * math.e / A * (lag - x) + 1)),
                 lambda x, y: [float(y.min()), float(np.ptp(y)) or 1.0, float(np.ptp(y)) / (np.ptp(x) or 1) * 2,
                               float(np.min(x) + np.ptp(x) * 0.2)]),
    "boltzmann": ("Boltzmann sigmoid", "Y = Bottom + (Top − Bottom) / (1 + exp((V50 − X)/Slope))", ["Bottom", "Top", "V50", "Slope"],
                  lambda x, b, t, v, sl: b + (t - b) / (1 + np.exp((v - x) / sl)),
                  lambda x, y: [float(y[np.argmin(x)]), float(y[np.argmax(x)]), float(np.median(x)), (np.ptp(x) or 1) / 10]),
    "gaussian": ("Gaussian peak", "Y = Baseline + Amplitude·exp(−½((X − Mean)/SD)²)", ["Baseline", "Amplitude", "Mean", "SD"],
                 lambda x, b, a, m, sd: b + a * np.exp(-0.5 * ((x - m) / sd) ** 2),
                 lambda x, y: [float(y.min()), float(np.ptp(y)), float(x[np.argmax(y)]), (np.ptp(x) or 1) / 6]),
    "power": ("Power law", "Y = A·X^B", ["A", "B"], lambda x, a, b: a * np.abs(x) ** b, lambda x, y: [float(np.mean(y)) or 1.0, 1.0]),
}

_EQ_FUNCS = {"exp": np.exp, "log": np.log, "ln": np.log, "log10": np.log10, "log2": np.log2, "sqrt": np.sqrt,
             "abs": np.abs, "sin": np.sin, "cos": np.cos, "tan": np.tan, "tanh": np.tanh, "arctan": np.arctan,
             "pi": np.pi, "e": np.e, "power": np.power, "minimum": np.minimum, "maximum": np.maximum, "where": np.where}


def _custom_model(equation, init_text):
    eq = str(equation or "").strip()
    if not eq:
        raise UserError("Type an equation, for example: Y = A*exp(-k*X) + C")
    eq = re.sub(r"^\s*[yY]\s*=", "", eq).replace("^", "**").replace("·", "*").replace("−", "-").strip()
    try:
        code = compile(eq, "<equation>", "eval")
    except SyntaxError as e:
        raise UserError("Couldn't read the equation: %s." % e.msg)
    names = [n for n in dict.fromkeys(code.co_names) if n not in _EQ_FUNCS and n not in ("X", "x")]
    if not names:
        raise UserError("The equation has no parameters to fit. Use letters for unknowns, e.g. A*exp(-k*X).")
    if "X" not in code.co_names and "x" not in code.co_names:
        raise UserError("The equation needs X in it.")
    init = {}
    for part in re.split(r"[;,\n]", str(init_text or "")):
        m = re.match(r"^\s*(\w+)\s*=\s*([-+\d.eE]+)\s*$", part)
        if m:
            init[m.group(1)] = float(m.group(2))

    def f(x, *p):
        env = dict(_EQ_FUNCS)
        env.update(zip(names, p))
        env["X"] = env["x"] = x
        with np.errstate(all="ignore"):
            return np.asarray(eval(code, {"__builtins__": {}}, env), float) * np.ones_like(x)
    return ("Custom equation", "Y = " + eq.replace("**", "^"), names, f, lambda x, y: [init.get(n, 1.0) for n in names])


def _fit_one(f, x, y, p0, w=None):
    popt, pcov = curve_fit(f, x, y, p0=p0, sigma=(1 / np.sqrt(w)) if w is not None else None, maxfev=40000)
    pred = f(x, *popt)
    if not np.all(np.isfinite(pred)):
        raise RuntimeError("the fitted curve isn't finite at every point")
    return popt, pcov, float(np.sum((y - pred) ** 2) if w is None else np.sum(w * (y - pred) ** 2))


def analysis_curvefit(datasets, S, s):
    d = _pick_dataset(datasets, S.get("dataset"))
    df, info = read_table(d["text"])
    notes = []
    num = _numeric_cols(df, info)
    xcol = _choose(S, "x_col", df, num[0] if num else None, notes, "X")
    if not xcol:
        raise UserError("Choose the X column.")
    _need_col(df, xcol, "X")
    model = S.get("model", "mm")
    if model == "custom":
        name, formula, pnames, f, guess = _custom_model(S.get("equation"), S.get("initial"))
    else:
        name, formula, pnames, f, guess = FIT_MODELS.get(model, FIT_MODELS["mm"])
    x_all = pd.to_numeric(df[xcol], errors="coerce").to_numpy(float)
    gcol = S.get("group_col") or ""
    if S.get("layout") == "long" and gcol in df.columns:
        ycol = _choose(S, "y_col", df, next((c for c in num if c != xcol), None), notes, "Y")
        _need_col(df, ycol, "Y")
        lab = _labels(df, gcol, info)
        groups = _levels(lab, S.get("group_order"))
        yv = pd.to_numeric(df[ycol], errors="coerce").to_numpy(float)
        curves = [(g, x_all[(lab == g).to_numpy()], yv[(lab == g).to_numpy()][:, None]) for g in groups]
        ylabel = ycol
    else:
        ycols = [c for c in (S.get("y_cols") or []) if c in df.columns and c != xcol] or [c for c in num if c != xcol]
        if not ycols:
            raise UserError("Choose at least one Y column.")
        Ymat = np.column_stack([pd.to_numeric(df[c], errors="coerce").to_numpy(float) for c in ycols])
        if S.get("mode") == "replicates":
            curves = [(S.get("curve_name") or "Data", x_all, Ymat)]
        else:
            curves = [(c, x_all, Ymat[:, [k]]) for k, c in enumerate(ycols)]
        ylabel = ycols[0] if len(ycols) == 1 or S.get("mode") == "replicates" else "Y"
    G = _gstyles(s, [c[0] for c in curves])
    wmode = S.get("weight", "none")
    fig, ax = _new_fig(s)
    lw, ms = s["line_width"], s["marker_size"]
    rows, summary, fits, resid_sets = [], [], [], []
    xmin_all, xmax_all = np.inf, -np.inf
    with plt.rc_context(_rc(s)):
        for g, (cname, xv, Ym) in zip(G, curves):
            c = g["color"]
            px = np.repeat(xv, Ym.shape[1])
            py = Ym.reshape(-1)
            ok = np.isfinite(px) & np.isfinite(py)
            px, py = px[ok], py[ok]
            if not len(px):
                notes.append("%s has no usable values." % g["label"])
                continue
            xmin_all, xmax_all = min(xmin_all, px.min()), max(xmax_all, px.max())
            pe = _point_edge(s, c)
            if Ym.shape[1] > 1 and not S.get("all_points", False):
                ux = np.unique(px)
                ax.errorbar(ux, [np.mean(py[px == u]) for u in ux],
                            yerr=[_err(py[px == u], s["error"]) for u in ux] if s["error"] != "none" else None,
                            fmt=g["marker"], color=c, ms=ms * 1.1, mec=pe, mew=s["point_edge_width"],
                            elinewidth=s["error_width"] * 0.8, capsize=s["cap_size"] * 0.7, ls="none", zorder=4, label=g["label"])
            else:
                ax.scatter(px, py, s=ms ** 2 * 1.2, marker=g["marker"], color=to_rgba(c, s["point_alpha"]),
                           edgecolor=pe, linewidth=s["point_edge_width"], zorder=4, label=g["label"])
            k = len(pnames)
            if len(px) <= k:
                notes.append("%s: %d points can't fit %d parameters." % (g["label"], len(px), k))
                continue
            w = None
            if wmode in ("1/y", "1/y2"):
                yy = np.abs(py).clip(1e-12)
                w = 1 / yy if wmode == "1/y" else 1 / yy ** 2
            try:
                p0 = [float(v) if np.isfinite(v) else 1.0 for v in guess(px, py)]
                popt, pcov, sse = _fit_one(f, px, py, p0, w)
            except Exception as e:
                notes.append("%s: the fit did not converge (%s). Try another model%s." % (
                    g["label"], str(e).split("\n")[0][:90], " or set starting values" if model == "custom" else ""))
                continue
            n = len(px)
            dof = n - k
            se = np.sqrt(np.clip(np.diag(pcov), 0, None)) if np.all(np.isfinite(pcov)) else np.full(k, np.nan)
            tval = stats.t.ppf(0.975, dof) if dof > 0 else np.nan
            pred = f(px, *popt)
            ss_res = float(np.sum((py - pred) ** 2))
            ss_tot = float(np.sum((py - py.mean()) ** 2))
            r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
            adj = 1 - (1 - r2) * (n - 1) / (dof - 1) if dof > 1 and np.isfinite(r2) else np.nan
            syx = math.sqrt(ss_res / dof) if dof > 0 else np.nan
            aicc = n * math.log(ss_res / n) + 2 * (k + 1) + (2 * (k + 1) * (k + 2)) / (n - k - 2) if ss_res > 0 and n - k - 2 > 0 else np.nan
            for pn, v, e in zip(pnames, popt, se):
                rows.append([g["label"], pn, _r(v), _r(e), _r(v - tval * e), _r(v + tval * e)])
            derived = []
            P = dict(zip(pnames, popt))
            if model in ("exp_decay", "exp_assoc") and P.get("K", 0) > 0:
                derived.append(("Half-life" if model == "exp_decay" else "Half-time", math.log(2) / P["K"]))
            if model == "exp_growth" and P.get("K", 0) > 0:
                derived.append(("Doubling time", math.log(2) / P["K"]))
            if model == "logistic" and P.get("r", 0) > 0:
                derived.append(("Doubling time (early)", math.log(2) / P["r"]))
            if model == "exp2_decay":
                for kk, lbl in (("Kfast", "Half-life fast"), ("Kslow", "Half-life slow")):
                    if P.get(kk, 0) > 0:
                        derived.append((lbl, math.log(2) / P[kk]))
            if model == "gaussian":
                derived.append(("FWHM", 2.3548 * abs(P["SD"])))
            if model == "gompertz" and P.get("A"):
                derived.append(("Max growth rate", P["mu"]))
            for dn, dv in derived:
                rows.append([g["label"], dn, _r(dv), None, None, None])
            rows.append([g["label"], "R squared", _r(r2), None, None, None])
            rows.append([g["label"], "Adjusted R squared", _r(adj), None, None, None])
            rows.append([g["label"], "Sy.x (RMSE)", _r(syx), None, None, None])
            rows.append([g["label"], "AICc", _r(aicc), None, None, None])
            rows.append([g["label"], "Points", n, None, None, None])
            key = ", ".join("%s = %s" % (pn, _fmt(v)) for pn, v in list(P.items())[:4])
            if derived:
                key += ", %s = %s" % (derived[0][0].lower(), _fmt(derived[0][1]))
            summary.append({"label": "%s%s" % (name, " · %s" % g["label"] if len(curves) > 1 else ""),
                            "value": "%s, R² = %.4f" % (key, r2), "p": None})
            fits.append((g, popt, px, py, w))
            resid_sets.append((g, px, py - pred))
            wide = np.ptp(px) or 1.0
            ext = _f(S, "extend", 0.0) or 0.0
            lo_x, hi_x = px.min() - ext * wide, px.max() + ext * wide
            xx = np.geomspace(max(lo_x, px[px > 0].min() * 0.8), hi_x, 400) if s["logx"] and (px > 0).any() else np.linspace(lo_x, hi_x, 400)
            yy = f(xx, *popt)
            ax.plot(xx, yy, color=c, lw=lw * 1.2, ls=g["ls"], zorder=3)
            if S.get("band", False) and np.all(np.isfinite(pcov)):
                # 95% confidence band by the delta method
                eps = 1e-6
                J = np.empty((len(xx), k))
                for j in range(k):
                    dp = np.array(popt, float)
                    h = eps * max(1.0, abs(dp[j]))
                    dp[j] += h
                    J[:, j] = (f(xx, *dp) - yy) / h
                var = np.einsum("ij,jk,ik->i", J, pcov, J)
                band = tval * np.sqrt(np.clip(var, 0, None))
                ax.fill_between(xx, yy - band, yy + band, color=to_rgba(c, s["band_alpha"]), lw=0, zorder=1)
        if S.get("annotate", True) and len(fits) == 1 and summary:
            ax.text(0.03, 0.97, "R² = %.4f" % float(summary[0]["value"].rsplit("R² = ", 1)[1]), transform=ax.transAxes,
                    ha="left", va="top", fontsize=s["font_size"] * 0.85)
        _finish(ax, s, xlabel=S.get("x_name") or xcol, ylabel=S.get("y_name") or ylabel, legend=len(curves) > 1)
    figures = [{"id": "fit", "title": name, "fig": fig, "style": s}]
    if S.get("residuals", False) and resid_sets:
        fig2, ax2 = _new_fig(s, s["width"])
        fig2.set_size_inches(s["width"], max(1.6, s["height"] * 0.55))
        with plt.rc_context(_rc(s)):
            for g, rx, rr in resid_sets:
                ax2.scatter(rx, rr, s=ms ** 2, marker=g["marker"], color=to_rgba(g["color"], s["point_alpha"]),
                            edgecolor=_point_edge(s, g["color"]), linewidth=s["point_edge_width"], zorder=3)
            ax2.axhline(0, color=s["fg"], lw=max(0.6, lw * 0.6), ls=(0, (3, 3)), zorder=2)
            s_res = dict(s, title="", ylabel="", xlabel="", ymin="", ymax="", logy=False, ref_lines="", panel_label="")
            _finish(ax2, s_res, xlabel=S.get("x_name") or xcol, ylabel="Residual")
        figures.append({"id": "residuals", "title": "Residuals", "fig": fig2, "style": s_res})
        runs_note = []
        for g, rx, rr in resid_sets:
            if len(rr) >= 8:
                sgn = np.sign(rr[np.argsort(rx)])
                sgn = sgn[sgn != 0]
                runs = 1 + int(np.sum(sgn[1:] != sgn[:-1]))
                n1, n2 = int((sgn > 0).sum()), int((sgn < 0).sum())
                if n1 and n2:
                    mu = 2 * n1 * n2 / (n1 + n2) + 1
                    var = 2 * n1 * n2 * (2 * n1 * n2 - n1 - n2) / ((n1 + n2) ** 2 * (n1 + n2 - 1))
                    z = (runs - mu) / math.sqrt(var) if var > 0 else 0
                    if stats.norm.cdf(z) < 0.05:
                        runs_note.append(g["label"])
        if runs_note:
            notes.append("Residuals cluster above or below zero (runs test p < 0.05) for %s: the model may not describe "
                         "the data's shape well." % ", ".join(runs_note))
    # Do the curves differ? Extra sum-of-squares F test, global (shared) vs separate fits
    comp = S.get("compare", "")
    if comp and len(fits) >= 2:
        shared = list(pnames) if comp == "all" else [comp] if comp in pnames else []
        if shared:
            try:
                Fv, df1, df2, p = _extra_ss_test(f, pnames, shared, fits)
                what = "all parameters" if comp == "all" else comp
                summary.append({"label": "Do the curves differ? (%s)" % what,
                                "value": "F(%d, %d) = %.3f, %s" % (df1, df2, Fv, fmt_p(p)), "p": _r(p)})
                notes.append("Extra sum-of-squares F test: one %s shared by every curve vs a separate one for each. A small p "
                             "means the curves differ in %s." % ("curve" if comp == "all" else comp, what))
            except Exception as e:
                notes.append("The curve comparison couldn't be computed (%s)." % str(e)[:80])
    tables = [{"title": "Best-fit values (%s)" % name, "columns": ["Curve", "Parameter", "Value", "SE", "95% CI low", "95% CI high"],
               "rows": rows}]
    meth = ("Data were fitted by %s regression to the model %s (%s)%s, with 95%% confidence intervals from the asymptotic "
            "standard errors. %s%s"
            % ("linear least-squares" if model in ("linear", "poly2", "poly3") else "nonlinear least-squares",
               name.lower(), formula, {"1/y": ", weighted by 1/Y", "1/y2": ", weighted by 1/Y²"}.get(wmode, ""),
               "Curves were compared with an extra sum-of-squares F test. " if comp and len(fits) >= 2 else "", _software_line()))
    return {"figures": figures, "tables": tables, "summary": summary, "notes": notes, "methods": meth,
            "groups": [c[0] for c in curves]}


def _extra_ss_test(f, pnames, shared, fits):
    k, m = len(pnames), len(fits)
    sse_sep = sum(_fit_one(f, px, py, list(popt), w)[2] for _, popt, px, py, w in fits)
    n_tot = sum(len(px) for _, _, px, _, _ in fits)
    df_sep = n_tot - k * m
    own = [p for p in pnames if p not in shared]
    X = np.concatenate([px for _, _, px, _, _ in fits])
    Yc = np.concatenate([py for _, _, _, py, _ in fits])
    Wc = np.concatenate([w if w is not None else np.ones(len(px)) for _, _, px, _, w in fits])
    ids = np.concatenate([np.full(len(px), i) for i, (_, _, px, _, _) in enumerate(fits)])
    p_start = [float(np.mean([dict(zip(pnames, popt))[p] for _, popt, _, _, _ in fits])) for p in shared]
    for _, popt, _, _, _ in fits:
        P = dict(zip(pnames, popt))
        p_start += [P[p] for p in own]

    def gf(_x, *theta):
        out = np.empty(len(X))
        sh = dict(zip(shared, theta[:len(shared)]))
        for i in range(m):
            o = theta[len(shared) + i * len(own): len(shared) + (i + 1) * len(own)]
            P = dict(sh)
            P.update(zip(own, o))
            sel = ids == i
            out[sel] = f(X[sel], *[P[p] for p in pnames])
        return out
    popt, _ = curve_fit(gf, X, Yc, p0=p_start, sigma=1 / np.sqrt(Wc), maxfev=60000)
    r = Yc - gf(X, *popt)
    sse_glob = float(np.sum(Wc * r ** 2))
    df_glob = n_tot - (len(shared) + len(own) * m)
    df1 = df_glob - df_sep
    if df1 <= 0 or df_sep <= 0:
        raise ValueError("not enough points")
    F = ((sse_glob - sse_sep) / df1) / (sse_sep / df_sep)
    return F, df1, df_sep, float(stats.f.sf(F, df1, df_sep))


# ----------------------------------------------------------------------------
# Contingency tables: chi-square, Fisher's exact, odds ratio, relative risk
# ----------------------------------------------------------------------------

def analysis_contingency(datasets, S, s):
    d = _pick_dataset(datasets, S.get("dataset"))
    df, info = read_table(d["text"])
    notes = []
    text, num = _text_cols(df, info), _numeric_cols(df, info)
    layout = S.get("layout", "counts")
    if layout == "raw":
        rcol = _choose(S, "row_col", df, text[0] if text else None, notes, "group")
        ccol = _choose(S, "col_col", df, next((c for c in df.columns if c != rcol), None), notes, "outcome")
        if not rcol or not ccol or rcol == ccol:
            raise UserError("Choose two different columns: the groups and the outcome.")
        rl, cl = _labels(df, rcol, info), _labels(df, ccol, info)
        rows_l = _levels(rl, S.get("row_order"))
        cols_l = _levels(cl, S.get("col_order"))
        obs = np.array([[int(((rl == r) & (cl == c)).sum()) for c in cols_l] for r in rows_l], float)
        row_name, col_name = rcol, ccol
    else:
        rcol = S.get("row_col") if S.get("row_col") in df.columns else (text[0] if text else None)
        ccols = [c for c in (S.get("count_cols") or []) if c in df.columns and c != rcol] or [c for c in num if c != rcol]
        if not ccols:
            raise UserError("Paste a table of counts: one row per group, one column per outcome.")
        rows_l = [str(v) for v in (_labels(df, rcol, info) if rcol else pd.Series(["Row %d" % (i + 1) for i in range(len(df))]))]
        M = np.column_stack([pd.to_numeric(df[c], errors="coerce").to_numpy(float) for c in ccols])
        keep = np.isfinite(M).any(axis=1)
        M = np.nan_to_num(M[keep])
        rows_l = [r for r, k in zip(rows_l, keep) if k]
        order = _order(rows_l, S.get("row_order"))
        M = np.array([M[rows_l.index(r)] for r in order])
        rows_l = order
        cols_l = _order(list(ccols), S.get("col_order"))
        obs = np.column_stack([M[:, list(ccols).index(c)] for c in cols_l])
        row_name, col_name = rcol or "Group", "Outcome"
        if (obs < 0).any() or (np.abs(obs - np.round(obs)) > 1e-9).any():
            raise UserError("Counts must be whole numbers of 0 or more. Use Compare groups for measurements.")
    if obs.shape[0] < 2 or obs.shape[1] < 2:
        raise UserError("A contingency table needs at least 2 groups and 2 outcomes (found %d × %d)." % obs.shape)
    keep_r, keep_c = obs.sum(axis=1) > 0, obs.sum(axis=0) > 0
    if not keep_r.all() or not keep_c.all():
        notes.append("Rows or columns with no counts were left out.")
        obs = obs[keep_r][:, keep_c]
        rows_l = [r for r, k in zip(rows_l, keep_r) if k]
        cols_l = [c for c, k in zip(cols_l, keep_c) if k]
    N = obs.sum()
    expected = np.outer(obs.sum(axis=1), obs.sum(axis=0)) / N
    test = S.get("test", "auto")
    is22 = obs.shape == (2, 2)
    summary = []
    if is22 and (test == "fisher" or (test == "auto" and expected.min() < 5) or (test == "auto" and N < 1000)):
        odds, p = stats.fisher_exact(obs)
        tname, line = "Fisher's exact test", "%s (two-sided)" % fmt_p(p)
    else:
        if test == "fisher" and not is22:
            notes.append("Fisher's exact test is only available for 2 × 2 tables here; the chi-square test was used.")
        corr = bool(S.get("yates", False)) and is22
        chi2, p, dof, _ = stats.chi2_contingency(obs, correction=corr)
        tname = "Chi-square test%s" % (" with Yates' correction" if corr else "")
        line = "χ²(%d) = %.3f, %s, N = %d" % (dof, chi2, fmt_p(p), N)
        if (expected < 5).mean() > 0.2 or expected.min() < 1:
            notes.append("Some expected counts are small (below 5), so the chi-square approximation may be unreliable."
                         + (" Fisher's exact test would be better." if is22 else " Consider combining categories."))
    chi2_v = stats.chi2_contingency(obs, correction=False)[0]
    cramer = math.sqrt(chi2_v / (N * (min(obs.shape) - 1)))
    summary.append({"label": tname, "value": line + ", Cramér's V = %.2f" % cramer, "p": _r(p)})
    assoc_rows = []
    if is22:
        a, b, c, dd = obs.ravel()
        aa, bb, cc, d2 = (a, b, c, dd) if min(a, b, c, dd) > 0 else (a + 0.5, b + 0.5, c + 0.5, dd + 0.5)
        OR = (aa * d2) / (bb * cc)
        se_or = math.sqrt(1 / aa + 1 / bb + 1 / cc + 1 / d2)
        z = 1.959964
        r1, r2 = aa / (aa + bb), cc / (cc + d2)
        RR = r1 / r2
        se_rr = math.sqrt(1 / aa - 1 / (aa + bb) + 1 / cc - 1 / (cc + d2))
        rd = a / (a + b) - c / (c + dd)
        se_rd = math.sqrt((a / (a + b)) * (1 - a / (a + b)) / (a + b) + (c / (c + dd)) * (1 - c / (c + dd)) / (c + dd))
        assoc_rows = [["Odds ratio", _r(OR), _r(OR * math.exp(-z * se_or)), _r(OR * math.exp(z * se_or))],
                      ["Relative risk", _r(RR), _r(RR * math.exp(-z * se_rr)), _r(RR * math.exp(z * se_rr))],
                      ["Risk difference", _r(rd), _r(rd - z * se_rd), _r(rd + z * se_rd)]]
        if min(a, b, c, dd) == 0:
            notes.append("A cell has 0 counts, so 0.5 was added to every cell for the odds ratio and relative risk.")
        summary.append({"label": "Odds ratio (%s, %s)" % (rows_l[0], cols_l[0]),
                        "value": "OR = %s (95%% CI %s to %s); relative risk %s (%s to %s)" % (
                            _fmt(OR), _fmt(assoc_rows[0][2]), _fmt(assoc_rows[0][3]), _fmt(RR), _fmt(assoc_rows[1][2]),
                            _fmt(assoc_rows[1][3])), "p": None})
    # Figure: stacked percent bars by default
    display = S.get("display", "stacked_pct")
    G = _gstyles(s, cols_l)
    fig, ax = _new_fig(s)
    H = s["orientation"] == "horizontal"
    bw = s["bar_width"]
    lw = s["edge_width"]
    pct = obs / obs.sum(axis=1, keepdims=True) * 100
    with plt.rc_context(_rc(s)):
        xs = np.arange(len(rows_l), dtype=float)
        base = np.zeros(len(rows_l))
        for j, g in enumerate(G):
            if s["fill"] == "outline":
                face = (0, 0, 0, 0)
            elif s["fill"] == "light":
                face = to_rgba(g["color"], max(0.35, s["fill_alpha"] * 2.2))
            else:
                face = to_rgba(g["color"], 0.92)
            kw = dict(color=face, edgecolor=g["color"] if s["fill"] != "solid" else s["bg"], linewidth=lw * 0.7,
                      hatch=g["hatch"], label=g["label"], zorder=2)
            if display == "grouped":
                step = 0.84 / len(G)
                pos = xs + (j - (len(G) - 1) / 2) * step
                vals = obs[:, j]
                bars = (ax.barh(pos, vals, height=step * 0.92, **kw) if H else ax.bar(pos, vals, width=step * 0.92, **kw))
            else:
                vals = pct[:, j] if display == "stacked_pct" else obs[:, j]
                bars = (ax.barh(xs, vals, height=bw, left=base, **kw) if H else ax.bar(xs, vals, width=bw, bottom=base, **kw))
                if S.get("labels", True):
                    for k_, b in enumerate(bars):
                        v = vals[k_]
                        if v <= 0 or (display == "stacked_pct" and v < 6):
                            continue
                        txt = "%.0f%%" % pct[k_, j] if display == "stacked_pct" else "%d" % obs[k_, j]
                        cx = b.get_x() + b.get_width() / 2
                        cy = b.get_y() + b.get_height() / 2
                        eff = [face[q] * face[3] + to_rgb(s["bg"])[q] * (1 - face[3]) for q in range(3)]
                        light = 0.2126 * eff[0] + 0.7152 * eff[1] + 0.0722 * eff[2] > 0.5
                        ax.text(cx, cy, txt, ha="center", va="center", fontsize=s["tick_size"] * 0.9,
                                color="#1A1D23" if light else "#FFFFFF", zorder=4)
                base = base + vals
            for b in bars:
                b._ll_bar = True
        (ax.set_yticks if H else ax.set_xticks)(xs)
        (ax.set_yticklabels if H else ax.set_xticklabels)(rows_l)
        if H:
            ax.set_ylim(len(rows_l) - 0.4, -0.6)
        ax._ll_cat = "y" if H else "x"
        vlabel = {"stacked_pct": "Percent of %s" % row_name.lower() if row_name else "Percent", "stacked": "Count",
                  "grouped": "Count"}.get(display, "Count")
        if display == "stacked_pct":
            (ax.set_xlim if H else ax.set_ylim)(0, 100)
        if s["legend"] == "best":
            s = dict(s, legend="outside")
        _finish(ax, s, xlabel=vlabel if H else row_name, ylabel=row_name if H else vlabel, legend=True,
                title=s["title"] or ("%s, %s" % (tname.split(" with")[0].replace(" test", ""), fmt_p(p)) if S.get("annotate", True) else ""))
    tables = [{"title": "Observed counts", "columns": [row_name] + cols_l + ["Total"],
               "rows": [[r] + [int(v) for v in obs[i]] + [int(obs[i].sum())] for i, r in enumerate(rows_l)]
               + [["Total"] + [int(v) for v in obs.sum(axis=0)] + [int(N)]]},
              {"title": "Row percentages", "columns": [row_name] + cols_l,
               "rows": [[r] + [_r(v, 4) for v in pct[i]] for i, r in enumerate(rows_l)]},
              {"title": "Expected counts (if there were no association)", "columns": [row_name] + cols_l,
               "rows": [[r] + [_r(v, 4) for v in expected[i]] for i, r in enumerate(rows_l)]}]
    if assoc_rows:
        tables.insert(1, {"title": "Association (2 × 2)", "columns": ["Measure", "Value", "95% CI low", "95% CI high"], "rows": assoc_rows})
    meth = ("The association between %s and %s was tested with %s%s. %s%s"
            % (row_name.lower(), col_name.lower(), tname if tname.startswith("Fisher") else "a " + tname[0].lower() + tname[1:],
               " (Cramér's V as effect size)" if not is22 else "",
               "Odds ratios and relative risks are given with Woolf/Katz 95% confidence intervals. " if is22 else "",
               _software_line()))
    return {"figures": [{"id": "contingency", "title": "Contingency", "fig": fig, "style": s}],
            "tables": tables, "summary": summary, "notes": notes, "methods": meth, "groups": cols_l}


# ----------------------------------------------------------------------------
# Sample size and power (no data needed)
# ----------------------------------------------------------------------------

def _power(design, n, eff, alpha, sides, ratio=1.0, k=3):
    n = float(n)
    if design == "two":
        n1, n2 = n, n * ratio
        if n1 < 2 or n2 < 2:
            return 0.0
        dfv = n1 + n2 - 2
        nc = eff * math.sqrt(n1 * n2 / (n1 + n2))
        tc = stats.t.ppf(1 - alpha / sides, dfv)
        return float(stats.nct.sf(tc, dfv, nc) + (stats.nct.cdf(-tc, dfv, nc) if sides == 2 else 0))
    if design == "paired":
        if n < 2:
            return 0.0
        dfv = n - 1
        nc = eff * math.sqrt(n)
        tc = stats.t.ppf(1 - alpha / sides, dfv)
        return float(stats.nct.sf(tc, dfv, nc) + (stats.nct.cdf(-tc, dfv, nc) if sides == 2 else 0))
    if design == "anova":
        N = n * k
        if n < 2:
            return 0.0
        df1, df2 = k - 1, N - k
        fc = stats.f.ppf(1 - alpha, df1, df2)
        return float(stats.ncf.sf(fc, df1, df2, eff ** 2 * N))
    if design == "props":
        n1, n2 = n, n * ratio
        z = stats.norm.ppf(1 - alpha / sides)
        val = abs(eff) * math.sqrt(n1 * n2 / (n1 + n2))
        return float(stats.norm.cdf(val - z) + (stats.norm.cdf(-val - z) if sides == 2 else 0))
    if design == "corr":
        if n <= 3:
            return 0.0
        z = stats.norm.ppf(1 - alpha / sides)
        val = abs(math.atanh(min(abs(eff), 0.999999))) * math.sqrt(n - 3)
        return float(stats.norm.cdf(val - z) + (stats.norm.cdf(-val - z) if sides == 2 else 0))
    raise UserError("Unknown design.")


def _n_for(design, eff, alpha, power, sides, ratio, k):
    if eff == 0:
        return None
    lo = 2 if design != "corr" else 4
    if _power(design, lo, eff, alpha, sides, ratio, k) >= power:
        return lo
    hi = lo
    while _power(design, hi, eff, alpha, sides, ratio, k) < power:
        hi *= 2
        if hi > 1e7:
            return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _power(design, mid, eff, alpha, sides, ratio, k) >= power:
            hi = mid
        else:
            lo = mid
    return hi


def analysis_power(datasets, S, s):
    notes = []
    design = S.get("design", "two")
    alpha = _f(S, "alpha", 0.05)
    if not 0 < alpha < 0.5:
        raise UserError("Alpha should be between 0 and 0.5 (usually 0.05).")
    target = _f(S, "power", 0.8)
    if target > 1:
        target /= 100.0
    if not 0.05 < target < 0.9999:
        raise UserError("Power should be between 0.05 and 0.9999 (usually 0.8 or 0.9).")
    sides = 1 if S.get("sides") == "one" else 2
    ratio = _f(S, "ratio", 1.0) or 1.0
    k = int(_f(S, "k", 3) or 3)
    dropout = (_f(S, "dropout", 0.0) or 0.0)
    dropout = dropout / 100 if dropout >= 1 else dropout
    entry = S.get("entry", "raw")
    unit = S.get("unit") or ""
    if design == "two" or design == "paired":
        if entry == "raw":
            m1, m2, sd = _f(S, "mean1", None), _f(S, "mean2", None), _f(S, "sd", None)
            if m1 is None or m2 is None or not sd:
                raise UserError("Enter both expected means and the standard deviation (or switch to effect size).")
            eff = abs(m2 - m1) / sd
            eff_txt = "a difference of %s%s (from %s to %s, SD %s; %s = %.2f)" % (
                _fmt(abs(m2 - m1)), " " + unit if unit else "", _fmt(m1), _fmt(m2), _fmt(sd),
                "Cohen's dz" if design == "paired" else "Cohen's d", eff)
            if design == "paired":
                eff_txt = eff_txt.replace("SD", "SD of the differences")
        else:
            eff = abs(_f(S, "effect", 0.8) or 0.8)
            eff_txt = "an effect size of %s = %.2f" % ("dz" if design == "paired" else "d", eff)
        eff_name = "dz" if design == "paired" else "d"
    elif design == "anova":
        if entry == "raw":
            means = [float(x) for x in re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", str(S.get("means") or ""))]
            sd = _f(S, "sd", None)
            if len(means) < 2 or not sd:
                raise UserError("Enter the expected group means (e.g. 10, 12, 15) and the standard deviation.")
            k = len(means)
            eff = float(np.std(means)) / sd
            eff_txt = "group means of %s (SD %s; Cohen's f = %.2f)" % (", ".join(_fmt(m) for m in means), _fmt(sd), eff)
        else:
            eff = abs(_f(S, "effect", 0.25) or 0.25)
            eff_txt = "an effect size of f = %.2f across %d groups" % (eff, k)
        if k < 2:
            raise UserError("ANOVA needs at least 2 groups.")
        eff_name = "f"
    elif design == "props":
        p1, p2 = _f(S, "p1", None), _f(S, "p2", None)
        if p1 is None or p2 is None:
            raise UserError("Enter the two expected proportions, e.g. 0.30 and 0.10 (or 30 and 10 for percent).")
        if p1 > 1 or p2 > 1:
            p1, p2 = p1 / 100, p2 / 100
        if not (0 < p1 < 1 and 0 < p2 < 1):
            raise UserError("Proportions must be between 0 and 1 (or 0 and 100%).")
        eff = abs(2 * math.asin(math.sqrt(p1)) - 2 * math.asin(math.sqrt(p2)))
        eff_txt = "proportions of %.0f%% vs %.0f%% (Cohen's h = %.2f)" % (p1 * 100, p2 * 100, eff)
        eff_name = "h"
    elif design == "corr":
        eff = abs(_f(S, "r", 0.5) or 0.5)
        if eff >= 1:
            raise UserError("The correlation must be between -1 and 1.")
        eff_txt = "a correlation of r = %.2f" % eff
        eff_name = "r"
    else:
        raise UserError("Unknown design.")
    if eff <= 0:
        raise UserError("The expected effect is zero, so no sample size can detect it.")
    solve = S.get("solve", "n")
    test_name = {"two": "two-sample t-test", "paired": "paired t-test", "anova": "one-way ANOVA",
                 "props": "comparison of two proportions", "corr": "test of a correlation"}[design]
    per = {"two": "per group", "paired": "pairs", "anova": "per group", "props": "per group", "corr": "in total"}[design]
    summary = []
    if solve == "power":
        n_have = _f(S, "n", 10) or 10
        pw = _power(design, n_have, eff, alpha, sides, ratio, k)
        n_req = _n_for(design, eff, alpha, target, sides, ratio, k)
        summary.append({"label": "Power", "value": "%.1f%% with n = %d %s" % (pw * 100, n_have, per), "p": None})
        if n_req:
            summary.append({"label": "For %.0f%% power" % (target * 100), "value": "n = %d %s" % (n_req, per), "p": None})
        headline = n_have
        meth_core = ("With n = %d %s, α = %g (%s-sided) and %s, a %s has %.0f%% power." % (
            n_have, per, alpha, "two" if sides == 2 else "one", eff_txt, test_name, pw * 100))
    elif solve == "effect":
        n_have = _f(S, "n", 10) or 10
        lo_e, hi_e = 1e-4, 50.0 if design not in ("corr",) else 0.9999
        if _power(design, n_have, hi_e, alpha, sides, ratio, k) < target:
            raise UserError("Even a huge effect can't reach that power with so few subjects.")
        for _ in range(80):
            mid = (lo_e + hi_e) / 2
            if _power(design, n_have, mid, alpha, sides, ratio, k) >= target:
                hi_e = mid
            else:
                lo_e = mid
        mde = hi_e
        if design in ("two", "paired") and entry == "raw" and _f(S, "sd", None):
            extra = " = a difference of %s%s" % (_fmt(mde * _f(S, "sd", 1)), " " + unit if unit else "")
        else:
            extra = ""
        summary.append({"label": "Smallest detectable effect", "value": "%s = %.3f%s (n = %d %s, %.0f%% power)" % (
            eff_name, mde, extra, n_have, per, target * 100), "p": None})
        headline = n_have
        eff = mde
        meth_core = ("With n = %d %s, α = %g (%s-sided) and %.0f%% power, the smallest effect a %s can detect is %s = %.2f%s." % (
            n_have, per, alpha, "two" if sides == 2 else "one", target * 100, test_name, eff_name, mde, extra))
    else:
        n_req = _n_for(design, eff, alpha, target, sides, ratio, k)
        if n_req is None:
            raise UserError("The effect is too small to reach that power with any realistic sample size.")
        n_drop = int(math.ceil(n_req / (1 - dropout))) if dropout > 0 else n_req
        total = n_req * (1 + ratio) if design in ("two", "props") else n_req * k if design == "anova" else n_req
        line = "n = %d %s" % (n_req, per)
        if design in ("two", "props") and abs(ratio - 1) > 1e-9:
            line = "n = %d and %d (ratio %g)" % (n_req, int(math.ceil(n_req * ratio)), ratio)
        if design in ("two", "props", "anova"):
            line += " · %d in total" % int(math.ceil(total))
        summary.append({"label": "Required sample size", "value": line, "p": None})
        summary.append({"label": "Achieved power", "value": "%.1f%%" % (_power(design, n_req, eff, alpha, sides, ratio, k) * 100), "p": None})
        if dropout > 0:
            summary.append({"label": "Allowing for %.0f%% dropout" % (dropout * 100), "value": "enroll %d %s" % (n_drop, per), "p": None})
        headline = n_req
        meth_core = ("To detect %s with %.0f%% power at α = %g (%s-sided) using a %s, %d %s are required%s." % (
            eff_txt, target * 100, alpha, "two" if sides == 2 else "one", test_name, n_req,
            "subjects " + per if design != "paired" else "pairs",
            "; allowing for %.0f%% attrition, %d %s will be enrolled" % (dropout * 100, n_drop, per) if dropout > 0 else ""))
    # Figure: power curves for the expected effect and +/- 25%
    G = _gstyles(s, ["Smaller effect", "Expected effect", "Larger effect"])
    fig, ax = _new_fig(s)
    lw = s["line_width"]
    n_max = max(10, int(headline * 2.2) if headline else 40)
    n_lo = 4 if design == "corr" else 2
    ns = np.unique(np.linspace(n_lo, n_max, min(200, n_max - n_lo + 1)).astype(int))
    with plt.rc_context(_rc(s)):
        for g, mult in zip(G, (0.75, 1.0, 1.25)):
            e = min(eff * mult, 0.9999) if design == "corr" else eff * mult
            pw = [_power(design, n_, e, alpha, sides, ratio, k) * 100 for n_ in ns]
            ax.plot(ns, pw, color=g["color"], lw=lw * (1.6 if mult == 1 else 1.0), ls=g["ls"] if mult == 1 else (0, (4, 2)),
                    zorder=3 if mult == 1 else 2, label="%s (%s = %.2f)" % (g["label"], eff_name, e))
        ax.axhline(target * 100, color=to_rgba(s["fg"], 0.45), lw=max(0.6, lw * 0.6), ls=(0, (2, 2)), zorder=1)
        if headline:
            hp = _power(design, headline, eff, alpha, sides, ratio, k) * 100
            ax.plot([headline, headline], [0, hp], color=to_rgba(G[1]["color"], 0.6), lw=max(0.6, lw * 0.7), ls=(0, (3, 2)), zorder=2)
            ax.scatter([headline], [hp], s=(s["marker_size"] * 1.6) ** 2, color=G[1]["color"], edgecolor=s["bg"],
                       linewidth=1, zorder=5)
            ax.annotate("n = %d" % headline, xy=(headline, hp), xytext=(6, -14), textcoords="offset points",
                        fontsize=s["font_size"] * 0.9, color=s["fg"])
        ax.set_ylim(0, 102)
        ax.set_xlim(ns.min(), ns.max())
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        _finish(ax, s, xlabel="Sample size (%s)" % per, ylabel="Power (%)", legend=True)
    # Table of n across power levels and effects
    rows = []
    for mult in (0.5, 0.75, 1.0, 1.25, 1.5):
        e = min(eff * mult, 0.9999) if design == "corr" else eff * mult
        rows.append(["%.3f%s" % (e, " (expected)" if mult == 1 else "")] +
                    [_n_for(design, e, alpha, pw, sides, ratio, k) for pw in (0.7, 0.8, 0.9, 0.95)])
    tables = [{"title": "Sample size %s for other effects and power levels" % per,
               "columns": ["Effect (%s)" % eff_name, "70% power", "80% power", "90% power", "95% power"], "rows": rows}]
    if design in ("two", "paired") and eff > 2.5:
        notes.append("That's a very large effect (%s = %.1f). Make sure the SD is realistic; pilot data or published "
                     "studies are the best source." % (eff_name, eff))
    if design in ("two", "paired", "anova"):
        notes.append("Sample sizes assume roughly normal data. For a non-parametric test, add about 15%.")
    meth = ("Sample size was calculated a priori. %s Calculations used the %s distribution. %s" % (
        meth_core, {"two": "noncentral t", "paired": "noncentral t", "anova": "noncentral F",
                    "props": "normal (arcsine-transformed)", "corr": "normal (Fisher z)"}[design], _software_line()))
    return {"figures": [{"id": "power", "title": "Power curve", "fig": fig, "style": s}],
            "tables": tables, "summary": summary, "notes": notes, "methods": meth,
            "groups": ["Smaller effect", "Expected effect", "Larger effect"]}


# ----------------------------------------------------------------------------
# Outliers (used by Compare groups)
# ----------------------------------------------------------------------------

def grubbs_outliers(v, alpha=0.05):
    """Iterative two-sided Grubbs test. Returns indices (into v) of outliers."""
    v = np.asarray(v, float)
    idx = np.where(np.isfinite(v))[0]
    out = []
    while len(idx) >= 3:
        x = v[idx]
        n = len(x)
        sd = np.std(x, ddof=1)
        if n < 3 or sd == 0:
            break
        dev = np.abs(x - x.mean())
        j = int(np.argmax(dev))
        G = dev[j] / sd
        t = stats.t.ppf(1 - alpha / (2 * n), n - 2)
        crit = (n - 1) / math.sqrt(n) * math.sqrt(t ** 2 / (n - 2 + t ** 2))
        if G <= crit:
            break
        out.append(int(idx[j]))
        idx = np.delete(idx, j)
        if len(idx) < 3:
            break
    return out


def iqr_outliers(v, k=1.5):
    v = np.asarray(v, float)
    fin = v[np.isfinite(v)]
    if len(fin) < 4:
        return []
    q1, q3 = np.percentile(fin, [25, 75])
    lo, hi = q1 - k * (q3 - q1), q3 + k * (q3 - q1)
    return [int(i) for i in np.where(np.isfinite(v) & ((v < lo) | (v > hi)))[0]]


# ----------------------------------------------------------------------------
# Multi-panel figures (the board)
# ----------------------------------------------------------------------------

def _svg_size_pt(svg):
    m = re.search(r'<svg[^>]*?width="([\d.]+)pt"[^>]*?height="([\d.]+)pt"', svg)
    if not m:
        return 288.0, 216.0
    return float(m.group(1)), float(m.group(2))


def board(panels, cols=2, width=7.0, fmt="png", dpi=300, letters="A", letter_size=14, gap=0.15, transparent=False):
    """Lay out several figures in a grid with panel letters. panels: [{config, datasets, index}]."""
    if not panels:
        raise UserError("Add figures to the board first.")
    if fmt not in ("png", "svg", "tiff", "jpg", "pdf"):
        raise UserError("Unknown format %s." % fmt)
    cols = max(1, min(int(cols or 2), 6))
    rendered = []
    try:
        for k, p in enumerate(panels):
            out = _render(p["config"], p.get("datasets") or [])
            figs = out["figures"]
            i = min(int(p.get("index", 0)), len(figs) - 1)
            f = figs[i]
            rendered.append((f, _svg(f["fig"], f["style"])))
            for j, other in enumerate(figs):
                if j != i:
                    plt.close(other["fig"])
        sizes = [_svg_size_pt(svg) for _, svg in rendered]
        bg = rendered[0][0]["style"]["bg"]
        fg = rendered[0][0]["style"]["fg"]
        font = FONTS.get(rendered[0][0]["style"].get("font"), FONTS["sans"])
        gap_pt = float(gap or 0) * 72
        natural = width is None or float(width or 0) <= 0
        if natural:
            cell_w = max(w for w, _ in sizes)
        else:
            cell_w = (float(width) * 72 - gap_pt * (cols - 1)) / cols
        lay, y = [], 0.0
        letter_pt = float(letter_size or 0) if letters and letters != "none" else 0.0
        top_pad = letter_pt * 0.15
        for r0 in range(0, len(rendered), cols):
            row = list(range(r0, min(r0 + cols, len(rendered))))
            scales = [min(cell_w / sizes[k][0], 3.0) for k in row]
            hs = [sizes[k][1] * sc for k, sc in zip(row, scales)]
            row_h = max(hs) + top_pad
            for c, (k, sc) in enumerate(zip(row, scales)):
                lay.append((k, c * (cell_w + gap_pt), y + top_pad, sizes[k][0] * sc, sizes[k][1] * sc, sc))
            y += row_h + gap_pt
        W, Hh = cols * cell_w + gap_pt * (cols - 1), y - gap_pt
        if len(rendered) < cols:
            W = len(rendered) * cell_w + gap_pt * (len(rendered) - 1)

        def letter(k):
            if not letters or letters == "none":
                return ""
            base = "a" if letters in ("a", "a)", "(a)") else "A"
            ch = chr(ord(base) + k) if k < 26 else str(k + 1)
            return {"A)": ch + ")", "a)": ch + ")", "(A)": "(" + ch + ")", "(a)": "(" + ch + ")"}.get(letters, ch)

        if fmt == "svg":
            parts = ['<?xml version="1.0" encoding="utf-8"?>',
                     '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                     'width="%.2fpt" height="%.2fpt" viewBox="0 0 %.2f %.2f" version="1.1">' % (W, Hh, W, Hh)]
            if not transparent:
                parts.append('<rect width="100%%" height="100%%" fill="%s"/>' % to_hex(bg))
            for k, x, y0, w, h, sc in lay:
                svg = rendered[k][1]
                pre = "p%d_" % k
                svg = re.sub(r'id="([^"]+)"', lambda m: 'id="%s%s"' % (pre, m.group(1)), svg)
                svg = re.sub(r'url\(#([^)]+)\)', lambda m: 'url(#%s%s)' % (pre, m.group(1)), svg)
                svg = re.sub(r'xlink:href="#([^"]+)"', lambda m: 'xlink:href="#%s%s"' % (pre, m.group(1)), svg)
                svg = re.sub(r'href="#([^"]+)"', lambda m: 'href="#%s%s"' % (pre, m.group(1)) if not m.group(1).startswith(pre) else m.group(0), svg)
                svg = re.sub(r'<svg([^>]*?)width="[\d.]+pt"([^>]*?)height="[\d.]+pt"',
                             lambda m: '<svg%sx="%.2f" y="%.2f" width="%.2f"%sheight="%.2f"' % (m.group(1), x, y0, w, m.group(2), h), svg, count=1)
                parts.append(svg)
            for k, x, y0, w, h, sc in lay:
                lt = letter(k)
                if lt:
                    parts.append('<text x="%.2f" y="%.2f" font-family="%s" font-size="%.1f" font-weight="bold" fill="%s">%s</text>' % (
                        x + 1, y0 - top_pad + letter_pt * 0.85, ", ".join(font).replace('"', ""), letter_pt, to_hex(fg), lt))
            parts.append("</svg>")
            return base64.b64encode("\n".join(parts).encode("utf-8")).decode("ascii")
        # Raster: render each panel at the resolution it will be shown at, then paste
        from PIL import Image, ImageDraw, ImageFont
        px = lambda v: int(round(v / 72.0 * dpi))
        mode = "RGBA" if transparent and fmt == "png" else "RGB"
        bgc = (0, 0, 0, 0) if mode == "RGBA" else tuple(int(c * 255) for c in to_rgb(bg))
        canvas = Image.new(mode, (max(1, px(W)), max(1, px(Hh))), bgc)
        for k, x, y0, w, h, sc in lay:
            f = rendered[k][0]
            buf = io.BytesIO()
            with plt.rc_context(_rc(f["style"])):
                f["fig"].savefig(buf, format="png", dpi=dpi * sc, bbox_inches="tight", pad_inches=0.08,
                                 transparent=bool(transparent), facecolor="none" if transparent else f["style"]["bg"])
            buf.seek(0)
            im = Image.open(buf).convert("RGBA")
            target = (max(1, px(w)), max(1, px(h)))
            if im.size != target:
                im = im.resize(target, Image.LANCZOS)
            canvas.paste(im, (px(x), px(y0)), im)
        if letter_pt:
            draw = ImageDraw.Draw(canvas)
            try:
                path = font_manager.findfont(font_manager.FontProperties(family=font, weight="bold"))
                pil_font = ImageFont.truetype(path, max(6, px(letter_pt)))
            except Exception:
                pil_font = ImageFont.load_default()
            fgc = tuple(int(c * 255) for c in to_rgb(fg))
            for k, x, y0, w, h, sc in lay:
                lt = letter(k)
                if lt:
                    draw.text((px(x + 1), px(y0 - top_pad)), lt, fill=fgc, font=pil_font)
        out = io.BytesIO()
        if fmt == "png":
            canvas.save(out, format="PNG", dpi=(dpi, dpi))
        elif fmt == "tiff":
            canvas.convert("RGB").save(out, format="TIFF", dpi=(dpi, dpi), compression="tiff_lzw")
        elif fmt == "jpg":
            canvas.convert("RGB").save(out, format="JPEG", dpi=(dpi, dpi), quality=95)
        else:
            canvas.convert("RGB").save(out, format="PDF", resolution=float(dpi))
        return base64.b64encode(out.getvalue()).decode("ascii")
    finally:
        for f, _ in rendered:
            plt.close(f["fig"])


ANALYSES = {
    "compare": analysis_compare,
    "timecourse": analysis_timecourse,
    "correlation": analysis_correlation,
    "dose": analysis_dose,
    "survival": analysis_survival,
    "twoway": analysis_twoway,
    "qpcr": analysis_qpcr,
    "stdcurve": analysis_stdcurve,
    "curvefit": analysis_curvefit,
    "contingency": analysis_contingency,
    "power": analysis_power,
}
NO_DATA = {"power"}

# ----------------------------------------------------------------------------
# Running + exporting
# ----------------------------------------------------------------------------

_LAST = {"figures": []}


def _svg(fig, s):
    buf = io.StringIO()
    with plt.rc_context(_rc(s)):
        fig.savefig(buf, format="svg", bbox_inches="tight", pad_inches=0.08, facecolor=s["bg"])
    txt = buf.getvalue()
    txt = re.sub(r"<style[^>]*>.*?</style>", "", txt, flags=re.S)  # global * rule would leak into the page
    return txt[txt.find("<svg"):]


def _run_user_code(code, fig, f, notes):
    ns = {"fig": fig, "ax": fig.axes[0] if fig.axes else None, "axes": fig.axes, "plt": plt, "np": np, "pd": pd,
          "matplotlib": matplotlib, "figure_id": f["id"], "style": f["style"]}
    try:
        with plt.rc_context(_rc(f["style"])):
            exec(compile(code, "<your code>", "exec"), ns)
    except Exception as e:
        line = next((fr.lineno for fr in reversed(traceback.extract_tb(e.__traceback__))
                     if fr.filename == "<your code>"), None)
        notes.append("Your Python code (%s figure%s): %s: %s" % (
            f["title"].lower(), ", line %d" % line if line else "", type(e).__name__, e))


def _render(config, datasets):
    """Run one analysis and finish its figures (polish + the user's own code). Doesn't touch _LAST."""
    kind = config.get("analysis", "compare")
    fn = ANALYSES.get(kind)
    if fn is None:
        raise UserError("Unknown analysis: %s" % kind)
    s = _style(config.get("style"))
    out = fn(datasets, config.get("settings") or {}, s)
    notes = list(out["notes"]) + list(s["_notes"])
    code = str(s.get("custom_code") or "").strip()
    for f in out["figures"]:
        with plt.rc_context(_rc(f["style"])):
            _polish(f["fig"], f["style"], notes)
        if code:
            _run_user_code(code, f["fig"], f, notes)
    out["notes"] = notes
    out["_s"] = s
    return out


def run(config, datasets):
    for f in _LAST["figures"]:
        plt.close(f["fig"])
    _LAST["figures"] = []
    out = _render(config, datasets)
    s, notes = out["_s"], out["notes"]
    _LAST["figures"] = out["figures"]
    figs = [{"id": f["id"], "title": f["title"], "svg": _svg(f["fig"], f["style"])} for f in out["figures"]]
    groups = [str(g) for g in out.get("groups", [])]
    gs = _gstyles(s, groups)
    return {"figures": figs, "tables": out["tables"], "summary": out["summary"], "notes": notes,
            "methods": out["methods"], "groups": groups,
            "group_styles": [{"name": g["name"], "color": to_hex(g["color"]), "label": g["label"]} for g in gs],
            "colors": {"bg": to_hex(s["bg"]), "fg": to_hex(s["fg"]), "ax_bg": to_hex(s["ax_bg"])},
            "events": out.get("events", {}), "meta": out.get("meta", {})}


def export_figure(index, fmt, dpi=300, transparent=False):
    if index >= len(_LAST["figures"]):
        raise UserError("Run the analysis first.")
    if fmt not in ("png", "svg", "pdf", "jpg", "tiff", "eps"):
        raise UserError("Unknown format %s." % fmt)
    f = _LAST["figures"][index]
    s = f["style"]
    buf = io.BytesIO()
    kw = {}
    if fmt in ("jpg", "tiff", "eps"):
        transparent = False
    if fmt == "jpg":
        kw["pil_kwargs"] = {"quality": 95}
    with plt.rc_context(_rc(s)):
        f["fig"].savefig(buf, format=fmt, dpi=dpi, bbox_inches="tight", pad_inches=0.08,
                         transparent=bool(transparent), facecolor="none" if transparent else s["bg"], **kw)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def versions():
    return {"engine": ENGINE_VERSION, "python": "%d.%d.%d" % sys.version_info[:3], "numpy": np.__version__,
            "scipy": scipy.__version__, "pandas": pd.__version__, "matplotlib": matplotlib.__version__,
            "colormaps": sorted(m for m in matplotlib.colormaps if not m.endswith("_r"))}


# ----------------------------------------------------------------------------
# Example data
# ----------------------------------------------------------------------------

def _tsv(header, rows):
    out = ["\t".join(header)]
    for r in rows:
        out.append("\t".join(str(x) for x in r))
    return "\n".join(out)


def example(kind):
    rng = np.random.default_rng(42)
    if kind == "compare":
        vehicle = rng.normal(812, 140, 8)
        low = rng.normal(640, 150, 8)
        high = rng.normal(395, 120, 8)
        rows = [["%.0f" % a, "%.0f" % b, "%.0f" % c] for a, b, c in zip(vehicle, low, high)]
        return {
            "datasets": [{"name": "Tumor volume, day 21", "text": _tsv(["Vehicle", "Drug 10 mg/kg", "Drug 30 mg/kg"], rows),
                          "meta": {}}],
            "settings": {"layout": "wide", "test": "auto", "posthoc": "all", "y_name": "Tumor volume (mm³)"},
        }
    if kind == "timecourse":
        # Heart rate in four volunteers after drinking water or an energy drink (same people, two visits)
        datasets = []
        for group, mean_rise in (("Water", 0.0), ("Energy drink", 0.16)):
            for m in range(4):
                rise = max(0.0, mean_rise + rng.normal(0, 0.04)) if mean_rise else rng.normal(0, 0.015)
                person = "P%02d" % (m + 1)
                drink = 600 + int(rng.integers(-90, 120))
                base_hr = rng.normal(68, 6)
                base_temp = rng.normal(33.2, 0.4)
                rows = []
                for t in range(0, drink + 30 * 60 + 1, 10):
                    after = max(0.0, (t - drink) / 60.0)
                    effect = rise * (1 - math.exp(-after / 9.0))
                    hr = base_hr * (1 + effect) * (1 + rng.normal(0, 0.03))
                    temp = base_temp + 0.4 * effect + rng.normal(0, 0.05)
                    quality = int(min(100, max(0, rng.normal(94, 4))))
                    if rng.random() < 0.03:  # brief sensor dropouts the quality filter removes
                        quality = int(rng.integers(20, 60))
                        hr = hr * rng.uniform(0.4, 1.8)
                    rows.append([_fmt_clock(t), "%.1f" % hr, "%.2f" % temp, quality])
                datasets.append({
                    "name": "%s %s" % (group, person),
                    "text": _tsv(["Time", "Heart rate (BPM)", "Skin temperature (°C)", "Signal quality (%)"], rows),
                    "meta": {"group": group, "subject": person, "event": _fmt_clock(drink), "exclude": ""},
                })
        return {"datasets": datasets,
                "settings": {"time_col": "Time", "value_col": "Heart rate (BPM)", "bin": 60,
                             "baseline_start": -5, "baseline_end": 0, "window_start": -8, "window_end": 30,
                             "endpoint_start": 25, "endpoint_end": 30, "norm": "pct", "metric": "mean",
                             "pair_subjects": True, "event_label": "Drink", "filter_col": "Signal quality (%)",
                             "filter_min": 80}}
    if kind == "correlation":
        rows = []
        for g, slope in (("Wild type", 2.1), ("Knockout", 3.4)):
            for _ in range(14):
                w = rng.normal(27, 3.2)
                glucose = 60 + slope * w + rng.normal(0, 9)
                rows.append([g, "%.1f" % w, "%.0f" % glucose])
        return {"datasets": [{"name": "Weight vs glucose", "meta": {},
                              "text": _tsv(["Genotype", "Body weight (g)", "Fasting glucose (mg/dL)"], rows)}],
                "settings": {"x_col": "Body weight (g)", "y_col": "Fasting glucose (mg/dL)", "group_col": "Genotype",
                             "method": "pearson", "fit": True, "ci": True}}
    if kind == "dose":
        conc = [0.1, 0.3, 1, 3, 10, 30, 100, 300, 1000, 3000]
        rows = []
        for cval in conc:
            lx = math.log10(cval)
            r = []
            for ic50, hill in ((25.0, 1.1), (180.0, 0.9), (900.0, 1.4)):
                y = 100 / (1 + 10 ** ((lx - math.log10(ic50)) * hill))
                r.append("%.1f" % (y + rng.normal(0, 4)))
            rows.append([cval] + r)
        return {"datasets": [{"name": "Kinase inhibition", "meta": {},
                              "text": _tsv(["Concentration", "Compound A", "Compound B", "Compound C"], rows)}],
                "settings": {"x_col": "Concentration", "mode": "curves", "x_unit": "nM", "show_ec50": True}}
    if kind == "survival":
        # Lifespan of flies on three diets; a few escape or are lost before dying (censored)
        rows = []
        for diet, scale, shape in (("Standard diet", 52.0, 4.0), ("Restricted diet", 64.0, 4.5), ("High-sugar diet", 41.0, 3.2)):
            for _ in range(30):
                day = scale * (-math.log(1 - rng.random())) ** (1 / shape)
                lost = rng.random() < 0.12
                if lost:
                    day *= rng.uniform(0.3, 0.9)
                rows.append([diet, "%d" % max(1, round(day)), 0 if lost else 1])
        return {"datasets": [{"name": "Fly lifespan", "meta": {},
                              "text": _tsv(["Diet", "Days", "Died"], rows)}],
                "settings": {"time_col": "Days", "event_col": "Died", "group_col": "Diet", "time_unit": "days",
                             "show_ci": True, "show_censors": True, "y_scale": "pct"}}
    if kind == "twoway":
        # Grip strength in wild-type and knockout mice given vehicle or drug: the drug rescues the knockout
        rows = []
        for geno, base in (("Wild type", 118.0), ("Knockout", 82.0)):
            for treat, boost in (("Vehicle", 0.0), ("Drug", 1.0)):
                gain = boost * (4.0 if geno == "Wild type" else 27.0)
                for _ in range(7):
                    rows.append([geno, treat, "%.1f" % (base + gain + rng.normal(0, 9))])
        return {"datasets": [{"name": "Grip strength", "meta": {}, "text": _tsv(["Genotype", "Treatment", "Grip strength (g)"], rows)}],
                "settings": {"layout": "long", "a_col": "Genotype", "b_col": "Treatment", "value_col": "Grip strength (g)",
                             "posthoc": "within_a"}}
    if kind == "qpcr":
        # Instrument-style export: one row per well, three technical replicates
        rows = []
        effects = {"GAPDH": (0.0, 0.0), "IL6": (0.0, -2.6), "TNF": (0.0, -1.4), "MYC": (0.0, 0.25)}
        base_ct = {"GAPDH": 18.2, "IL6": 29.4, "TNF": 27.1, "MYC": 24.6}
        for group in ("Untreated", "Treated"):
            for sidx in range(1, 5):
                sample = "%s %d" % (group, sidx)
                load = rng.normal(0, 0.35)
                for gene in ("GAPDH", "IL6", "TNF", "MYC"):
                    shift = effects[gene][1] if group == "Treated" else 0.0
                    bio = rng.normal(0, 0.3) if gene != "GAPDH" else 0.0
                    for _ in range(3):
                        rows.append([sample, group, gene, "%.2f" % (base_ct[gene] + load + shift + bio + rng.normal(0, 0.12))])
        return {"datasets": [{"name": "qPCR run", "meta": {}, "text": _tsv(["Sample Name", "Group", "Target Name", "CT"], rows)}],
                "settings": {"layout": "long", "sample_col": "Sample Name", "group_col": "Group", "gene_col": "Target Name",
                             "ct_col": "CT", "ref_genes": ["GAPDH"], "output": "fold", "posthoc": "control"}}
    if kind == "stdcurve":
        # ELISA: two-fold standard series in duplicate, a blank, and unknown samples (concentration left empty)
        a, b, c, dd = 0.06, 1.35, 180.0, 2.9
        f4 = lambda x: dd + (a - dd) / (1 + (x / c) ** b)
        rows = []
        for conc in (0, 7.8, 15.6, 31.25, 62.5, 125, 250, 500, 1000):
            sig = f4(conc) if conc else a
            rows.append(["Standard %g" % conc if conc else "Blank", conc] + ["%.3f" % (sig * (1 + rng.normal(0, 0.03)) + rng.normal(0, 0.002)) for _ in range(2)])
        for k, true in enumerate((40, 95, 210, 330, 610, 18, 140, 1500), 1):
            sig = f4(true)
            rows.append(["Serum %02d" % k, ""] + ["%.3f" % (sig * (1 + rng.normal(0, 0.04))) for _ in range(2)])
        return {"datasets": [{"name": "IL-6 ELISA", "meta": {}, "text": _tsv(["Sample", "Concentration", "OD450 a", "OD450 b"], rows)}],
                "settings": {"conc_col": "Concentration", "name_col": "Sample", "model": "4pl", "blank": "subtract",
                             "unit": "pg/mL", "dilution": 2, "y_name": "Absorbance (450 nm)", "x_name": "IL-6"}}
    if kind == "curvefit":
        # Enzyme kinetics for a wild-type and a mutant enzyme
        sub = [0.5, 1, 2, 4, 8, 16, 32, 64, 128]
        rows = []
        for sv in sub:
            wt = 52 * sv / (6.0 + sv) * (1 + rng.normal(0, 0.05))
            mu = 47 * sv / (19.0 + sv) * (1 + rng.normal(0, 0.05))
            rows.append([sv, "%.2f" % wt, "%.2f" % mu])
        return {"datasets": [{"name": "Enzyme kinetics", "meta": {}, "text": _tsv(["Substrate (µM)", "Wild type", "Mutant"], rows)}],
                "settings": {"x_col": "Substrate (µM)", "model": "mm", "compare": "Km", "y_name": "Velocity (nmol/min/mg)"}}
    if kind == "contingency":
        rows = [["Standard diet", 6, 34], ["High-fat diet", 17, 23]]
        return {"datasets": [{"name": "Tumor incidence", "meta": {}, "text": _tsv(["Diet", "Tumor", "No tumor"], rows)}],
                "settings": {"layout": "counts", "display": "stacked_pct", "test": "auto"}}
    if kind == "power":
        return {"datasets": [], "settings": {"design": "two", "entry": "raw", "mean1": 100, "mean2": 80, "sd": 15,
                                             "unit": "mg/dL", "alpha": 0.05, "power": 0.8, "dropout": 10, "solve": "n"}}
    raise UserError("No example for %s" % kind)


# ----------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------

def _clean(o):
    """Make any result JSON-safe: NaN/inf become null, numpy types become Python types."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return [_clean(v) for v in o.tolist()]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return float(o) if math.isfinite(o) else None
    if isinstance(o, (str, int, bool)) or o is None:
        return o
    return str(o)


def api(cmd, args_json):
    """Single entry point for the app. Always returns JSON: {"ok": ...} or {"error": ...}."""
    try:
        args = json.loads(args_json) if args_json else {}
        if cmd == "inspect":
            result = inspect(args["text"])
        elif cmd == "run":
            result = run(args["config"], args["datasets"])
        elif cmd == "export":
            result = export_figure(int(args.get("index", 0)), args.get("format", "png"),
                                   int(float(args.get("dpi", 300) or 300)), args.get("transparent", False))
        elif cmd == "excel":
            result = excel_to_tables(args["b64"])
        elif cmd == "example":
            result = example(args["kind"])
        elif cmd == "fonts":
            result = register_fonts(args.get("paths") or [])
        elif cmd == "versions":
            result = versions()
        elif cmd == "board":
            result = board(args.get("panels") or [], args.get("cols", 2), args.get("width", 7.0), args.get("format", "png"),
                           int(float(args.get("dpi", 300) or 300)), args.get("letters", "A"), args.get("letter_size", 14),
                           args.get("gap", 0.15), args.get("transparent", False))
        else:
            raise UserError("Unknown command %s" % cmd)
        return json.dumps({"ok": _clean(result)}, allow_nan=False)
    except UserError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:  # unexpected: give the user the message, keep the trace for debugging
        return json.dumps({"error": "Something went wrong: %s" % e, "trace": traceback.format_exc()})


def cli(config_json, datasets_json, out_dir="serpentine_output"):
    """Used by exported scripts: re-runs the analysis and saves figures and tables."""
    import os
    os.makedirs(out_dir, exist_ok=True)
    config = json.loads(config_json)
    datasets = json.loads(datasets_json)
    out = run(config, datasets)
    for i, f in enumerate(_LAST["figures"]):
        for fmt in ("png", "pdf", "svg"):
            path = os.path.join(out_dir, "%d_%s.%s" % (i + 1, f["id"], fmt))
            with plt.rc_context(_rc(f["style"])):
                f["fig"].savefig(path, dpi=f["style"]["dpi"], bbox_inches="tight", pad_inches=0.08,
                                 facecolor=f["style"]["bg"])
            print("saved", path)
    for t in out["tables"]:
        name = re.sub(r"[^A-Za-z0-9]+", "_", t["title"]).strip("_").lower()
        path = os.path.join(out_dir, name + ".csv")
        pd.DataFrame(t["rows"], columns=t["columns"]).to_csv(path, index=False)
        print("saved", path)
    for item in out["summary"]:
        print("%s: %s" % (item["label"], item["value"]))
    for n in out["notes"]:
        print("note:", n)
    print()
    print("Methods:", out["methods"])
