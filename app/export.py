"""The Dashboard's Excel report: a summary sheet with native charts, plus the data behind them.

workbook() takes plain data and returns the file's bytes, so it knows nothing about
the web layer. Invoice text comes from uploaded documents and can start with '=', so
every string is written as text and never becomes a live formula.
"""
import io
from datetime import datetime

import xlsxwriter

from .rules import CODE_LABEL

INK, INK_2, RULE, PEN = "#1B2A41", "#4B5A70", "#DCE6DF", "#2446A6"
# bar fills are stepped from the app's status colours: its amber and red can't be told apart with red-green colour blindness
OUTCOMES = [("APPROVE", "Approved", "#1E7A4C", "#DDF0E5"), ("HOLD", "Held", "#C98512", "#FBEBD0"), ("REJECT", "Rejected", "#A61C14", "#F9DEDA")]
BAD = "#A61C14"
INR = '[>=10000000]"₹"##\\,##\\,##\\,##0.00;[>=100000]"₹"##\\,##\\,##0.00;"₹"##,##0.00'
INR_WHOLE = INR.replace(".00", "")
BOOK = dict(in_memory=True, strings_to_formulas=False, strings_to_urls=False)
HEAD = dict(bold=True, font_color=INK, bottom=2, bottom_color=INK, text_wrap=True, valign="top")
CHART_W, CHART_H = 560, 320
TOP_N = 8


def _put(ws, row, col, value, fmt=None):
    if value is None or value == "":
        ws.write_blank(row, col, None, fmt)
    elif isinstance(value, datetime):
        ws.write_datetime(row, col, value, fmt)
    elif isinstance(value, (int, float)):
        ws.write_number(row, col, value, fmt)
    else:
        ws.write_string(row, col, str(value)[:32767], fmt)


def _sheet(wb, name, head, columns, rows):
    """A filterable data sheet. columns: (title, width, format or None, row -> value)."""
    ws = wb.add_worksheet(name)
    for c, (title, width, *_rest) in enumerate(columns):
        ws.set_column(c, c, width)
        ws.write_string(0, c, title, head)
    for r, row in enumerate(rows, 1):
        for c, (_title, _width, fmt, get) in enumerate(columns):
            _put(ws, r, c, get(row), fmt(row) if callable(fmt) else fmt)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, max(len(rows), 1), len(columns) - 1)
    return ws


def _bar(wb, title, sheet, first, last, value_format=None, colors=None, kind="bar", height=CHART_H, longest=None):
    """One-series chart: values labelled directly, so the value axis and grid are left out."""
    chart = wb.add_chart({"type": kind})
    series = dict(categories=[sheet, first, 0, last, 0], values=[sheet, first, 1, last, 1], fill={"color": PEN}, gap=55,
                  data_labels=dict(value=True, font=dict(color=INK, size=10), **({"num_format": value_format} if value_format else {})))
    if colors:
        series["points"] = [{"fill": {"color": c}} for c in colors]
    chart.add_series(series)
    chart.set_title(dict(name=title, overlay=False, name_font=dict(size=12, bold=True, color=INK)))
    chart.set_legend(dict(none=True))
    cat_axis = dict(num_font=dict(color=INK_2, size=10), line=dict(color=RULE), major_tick_mark="none")
    value_axis = dict(visible=False, major_gridlines=dict(visible=False), min=0)
    if longest:
        value_axis["max"] = longest * 1.3  # room for a long label past the end of the longest bar
    if kind == "bar":  # xlsxwriter's bar charts swap the axes: y is the category axis
        chart.set_y_axis(dict(cat_axis, reverse=True))  # keep the table's order, largest at the top
        chart.set_x_axis(value_axis)
    else:
        chart.set_x_axis(cat_axis)
        chart.set_y_axis(value_axis)
    chart.set_chartarea(dict(border=dict(color=RULE)))
    chart.set_size(dict(width=CHART_W, height=height))
    return chart


def _vendor_sheet(wb, name, vendors, runs):
    """One row per vendor, with what has been approved for it so far."""
    head, wrap = wb.add_format(HEAD), wb.add_format(dict(text_wrap=True, valign="top"))
    money, stamp = wb.add_format(dict(num_format=INR)), wb.add_format(dict(num_format="dd-mmm-yyyy hh:mm"))
    approved = {}
    for r in runs:
        if r["status"] == "done" and (r["final_decision"] or r["decision"]) == "APPROVE" and r["vendor_id"]:
            approved.setdefault(r["vendor_id"], []).append(r["total"] or 0)
    moment = lambda s: datetime.fromisoformat(s) if s else None
    return _sheet(wb, name, head, [
        ("Vendor ID", 9, None, lambda v: v["vendor_id"]), ("Name", 36, None, lambda v: v["name"]), ("GSTIN", 18, None, lambda v: v["gstin"]),
        ("Also known as", 30, wrap, lambda v: ", ".join(v["aliases"])), ("Status", 10, None, lambda v: v["status"]), ("Source", 16, None, lambda v: v["source"]),
        ("Invoices approved", 10, None, lambda v: len(approved.get(v["vendor_id"], []))), ("Approved value (gross)", 16, money, lambda v: sum(approved.get(v["vendor_id"], []))),
        ("Approved on", 17, stamp, lambda v: moment(v.get("approved_at"))), ("Approval note", 36, wrap, lambda v: v.get("note")),
    ], vendors)


def _approved(vendors):
    return [v for v in vendors if v["status"].lower() == "approved"]


def approved_vendors(vendors, runs):
    """The approved vendor list as a workbook of its own."""
    out = io.BytesIO()
    wb = xlsxwriter.Workbook(out, BOOK)
    _vendor_sheet(wb, "Approved vendors", _approved(vendors), runs)
    wb.close()
    return out.getvalue()


def workbook(stats, runs, pos, vendors, period=""):
    out = io.BytesIO()
    wb = xlsxwriter.Workbook(out, BOOK)
    head = wb.add_format(HEAD)
    money, date, stamp, pct = (wb.add_format(dict(num_format=f)) for f in (INR, "dd-mmm-yyyy", "dd-mmm-yyyy hh:mm", "0.0%"))
    wrap = wb.add_format(dict(text_wrap=True, valign="top"))
    outcome_fmt = {k: wb.add_format(dict(bg_color=wash, font_color=INK)) for k, _l, _c, wash in OUTCOMES}
    label = {k: lbl for k, lbl, _c, _w in OUTCOMES}

    eff = lambda r: r["final_decision"] or r["decision"]
    done = [r for r in runs if r["status"] in ("done", "error")]
    shown = [r for r in done if eff(r) != "REJECT"]  # same as the Dashboard; rejected documents get their own sheet
    moment = lambda s: datetime.fromisoformat(s) if s else None

    summary = wb.add_worksheet("Summary")
    summary.hide_gridlines(2)
    summary.set_landscape()
    summary.fit_to_pages(1, 1)

    invoice_columns = [
        ("Received", 17, stamp, lambda r: moment(r["created_at"])), ("File", 30, None, lambda r: r["filename"]),
        ("Vendor", 30, None, lambda r: r["vendor_name"]), ("Vendor ID", 9, None, lambda r: r["vendor_id"]),
        ("Invoice no.", 18, None, lambda r: r["invoice_number"]), ("Invoice date", 12, date, lambda r: moment(r["invoice_date"])),
        ("PO", 14, None, lambda r: r["po_number"]), ("Pre-tax", 15, money, lambda r: r["net"]), ("Total", 15, money, lambda r: r["total"]),
        ("Outcome", 10, lambda r: outcome_fmt.get(eff(r)), lambda r: label.get(eff(r))),
        ("System decision", 10, None, lambda r: label.get(r["decision"])), ("Resolved by a person", 10, None, lambda r: "Yes" if r["final_decision"] else "No"),
        ("Why it stopped", 44, wrap, lambda r: "; ".join(CODE_LABEL.get(c, c) for c in r["codes"])),
        ("Resolution note", 30, wrap, lambda r: r["override_note"]), ("Upload note", 30, wrap, lambda r: r["inputs"].get("note")),
        ("Read by", 12, None, lambda r: (r["extractor"] or "").split(":")[0]), ("Seconds", 9, None, lambda r: round(r["duration_ms"] / 1000, 1) if r["duration_ms"] else None),
    ]
    _sheet(wb, "Invoices", head, invoice_columns, shown)
    _sheet(wb, "PO ledger", head, [
        ("PO", 15, None, lambda p: p["po_number"]), ("Vendor", 30, None, lambda p: p["vendor_name"]), ("For", 42, None, lambda p: p["description"]),
        ("PO value (pre-tax)", 16, money, lambda p: p["net_amount"]), ("Billed so far", 15, money, lambda p: p["billed"]),
        ("Still open", 15, money, lambda p: max(p["remaining"], 0)), ("% billed", 9, pct, lambda p: p["pct_billed"] / 100),
        ("Approved invoices", 36, wrap, lambda p: ", ".join(i["invoice_number"] or "no number" for i in p["invoices"])),
    ], pos)
    _vendor_sheet(wb, "Approved vendors", _approved(vendors), runs)
    blocked = [v for v in vendors if v not in _approved(vendors)]
    if blocked:
        _vendor_sheet(wb, "Blocked vendors", blocked, runs)
    _sheet(wb, "Rejected", head, invoice_columns, [r for r in done if eff(r) == "REJECT"])

    # ---- the tables the charts draw from, so every chart has a plain-numbers view
    data = wb.add_worksheet("Chart data")
    data.set_column(0, 0, 40)
    data.set_column(1, 1, 18)

    def table(row, title, unit, rows, fmt=None):
        data.write_string(row, 0, title, head)
        data.write_string(row, 1, unit, head)
        for i, (name, value) in enumerate(rows, row + 1):
            data.write_string(i, 0, name)
            data.write_number(i, 1, value, fmt)
        return row + 1, row + len(rows)

    by_vendor = {}
    for r in shown:
        if eff(r) == "APPROVE" and r["total"]:
            key = r["vendor_name"] or "Unknown vendor"
            by_vendor[key] = by_vendor.get(key, 0) + r["total"]
    top_vendors = sorted(by_vendor.items(), key=lambda x: -x[1])[:TOP_N]
    reasons = [(CODE_LABEL.get(x["code"], x["code"]), x["count"]) for x in stats["reasons"]]
    billed = sorted(((p["po_number"], p["pct_billed"] / 100) for p in pos if p["billed"]), key=lambda x: -x[1])

    outcomes = [o for o in OUTCOMES if o[0] in stats["counts"]]
    counts = table(0, "Invoices by outcome", "Invoices", [(lbl, stats["counts"][k]) for k, lbl, _c, _w in outcomes])
    why = table(counts[1] + 2, "Why invoices stop", "Invoices", reasons)
    spend = table(why[1] + 2, "Approved value by vendor (gross)", "Rupees", top_vendors, money)
    ledger = table(spend[1] + 2, "PO value billed so far", "% of PO value", billed, pct)

    # ---- summary sheet
    summary.set_column(0, 0, 2)
    summary.set_column(1, 8, 13)
    summary.set_row(1, 30)
    summary.write_string(1, 1, "Invoice Flow report", wb.add_format(dict(font_size=20, bold=True, font_color=INK)))
    muted = wb.add_format(dict(font_color=INK_2))
    scope = f"Invoices dated {period}. The PO ledger is always the current position. " if period else ""
    summary.write_string(2, 1, f"Generated {datetime.now():%d %b %Y, %H:%M}. {scope}Charts are live: they read from the Chart data sheet.", muted)

    tile_value = wb.add_format(dict(font_size=18, bold=True, font_color=INK, align="left"))
    tile_money = wb.add_format(dict(font_size=18, bold=True, font_color=INK, align="left", num_format=INR))
    tile_label = wb.add_format(dict(font_color=INK_2, text_wrap=True, valign="top", top=2, top_color=INK))
    tiles = [("Invoices processed", stats["total"], tile_value), ("Approved with no human touch", stats["straight_through"] / 100, wb.add_format(dict(font_size=18, bold=True, font_color=INK, align="left", num_format="0%"))),
             ("Approved for payment (gross)", stats["approved_value"], tile_money), ("On hold (gross)", stats["pending_value"], tile_money)]
    summary.set_row(4, 32)
    summary.set_row(5, 28)
    for i, (text, value, fmt) in enumerate(tiles):
        col = 1 + i * 2
        summary.merge_range(4, col, 4, col + 1, value, fmt)
        summary.merge_range(5, col, 5, col + 1, text, tile_label)

    if not stats["total"]:
        summary.write_string(7, 1, "No invoices have been processed yet, so there is nothing to chart.", muted)
    charts = []
    if stats["total"]:
        charts.append(_bar(wb, "Invoices by outcome", "Chart data", *counts, colors=[c for _k, _l, c, _w in outcomes], kind="column"))
    if reasons:
        charts.append(_bar(wb, "Why invoices stop", "Chart data", *why))
    if top_vendors:
        charts.append(_bar(wb, "Approved value by vendor (gross, top 8)", "Chart data", *spend, value_format=INR_WHOLE, longest=top_vendors[0][1]))
    if billed:
        charts.append(_bar(wb, "PO value billed so far", "Chart data", *ledger, value_format="0%",
                           colors=[BAD if v > 1.0001 else PEN for _n, v in billed]))
    for i, chart in enumerate(charts):
        summary.insert_chart(7 + (i // 2) * 17, 1, chart, dict(x_offset=(i % 2) * (CHART_W + 16), object_position=3))

    wb.close()
    return out.getvalue()
