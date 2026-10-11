"""Findings report as a polished A4 PDF: reports/findings_report.pdf.

Isolated addition: reads the same data as report.py (analysis_agent.latest_or_deterministic()) and never changes it.
Uses only real values from that data; excluded runs (quality == "excluded") appear only in the appendix.
Run: python report_pdf.py
"""
import html
import io
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, PageBreak, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

import analysis
import analysis_agent
import report

PDF_PATH = analysis.REPORTS_DIR / "findings_report.pdf"
TEAM_NAME = os.environ.get("CHAOS_TEAM_NAME", "Team 9 · Bosch Hackathon")
DETAILED_FINDINGS = 6          # full blocks; the rest go into a compact table
ERROR_CHART_MAX = 10

ACCENT = colors.HexColor("#1f4e8c")
INK, MUTED, LINE, PANEL = colors.HexColor("#1d2330"), colors.HexColor("#5b6577"), colors.HexColor("#d9dee8"), colors.HexColor("#f3f5f9")
SEV_HEX = {"Critical": "#c62828", "High": "#ef6c00", "Medium": "#f9a825", "Low": "#607d8b"}
GOOD_HEX, WARN_HEX, BAD_HEX = "#2e7d32", "#f9a825", "#c62828"
SCORE_RED_BELOW, SCORE_GREEN_FROM = 50, 75
QUALITY_HEX = {"ok": "#2e7d32", "low_evidence": "#f9a825", "excluded": "#607d8b"}

_FONT_DIR = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
pdfmetrics.registerFont(TTFont("DejaVu", str(_FONT_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(_FONT_DIR / "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Oblique", str(_FONT_DIR / "DejaVuSans-Oblique.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-BoldOblique", str(_FONT_DIR / "DejaVuSans-BoldOblique.ttf")))
pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu-Oblique", boldItalic="DejaVu-BoldOblique")


def _style(name, **kw):
    base = dict(fontName="DejaVu", fontSize=9, leading=12.5, textColor=INK, alignment=TA_LEFT)
    base.update(kw)
    return ParagraphStyle(name, **base)


S = {
    "title": _style("title", fontName="DejaVu-Bold", fontSize=19, leading=23, textColor=ACCENT),
    "sub": _style("sub", fontSize=9, textColor=MUTED),
    "h2": _style("h2", fontName="DejaVu-Bold", fontSize=12.5, leading=16, textColor=ACCENT, spaceBefore=10, spaceAfter=4),
    "h3": _style("h3", fontName="DejaVu-Bold", fontSize=10.5, leading=14),
    "body": _style("body"),
    "small": _style("small", fontSize=8, leading=10.5, textColor=MUTED),
    "tiny": _style("tiny", fontSize=7, leading=9),
    "cell": _style("cell", fontSize=7.5, leading=9.5),
    "cellb": _style("cellb", fontName="DejaVu-Bold", fontSize=7.5, leading=9.5, textColor=MUTED),
    "cap": _style("cap", fontName="DejaVu-Bold", fontSize=9.5, leading=13, spaceBefore=6, spaceAfter=3),
    "score": _style("score", fontName="DejaVu-Bold", fontSize=34, leading=38, textColor=ACCENT),
    "label": _style("label", fontName="DejaVu-Bold", fontSize=8, leading=11, textColor=ACCENT),
}


def esc(x):
    return html.escape(str(x))


# ---- data helpers (pure; unit-tested) ---------------------------------------------------

def _excluded_ids(data):
    ids = {x["id"] for x in data.get("excluded", [])}
    ids |= {e["id"] for e in data["experiments"] if e.get("quality") == "excluded"}
    return ids


def kept_experiments(data):
    """Experiments that feed the analysis (everything not excluded)."""
    gone = _excluded_ids(data)
    return [e for e in data["experiments"] if e["id"] not in gone]


def run_time(run_id):
    """'20261011T041909Z' -> '11 Oct 04:19 UTC'."""
    try:
        return datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").strftime("%d %b %H:%M UTC")
    except ValueError:
        return str(run_id)


def _evidence_of(data, run_id):
    return [v for v in data["evidence"].values() if v["result_id"] == run_id]


def _is_p95(item):
    return item["kind"] == "impact" and item["id"].endswith("p95_ms")


def _is_err(item):
    return item["kind"] == "impact" and item["id"].endswith("error_rate")


def _caller(item):
    return item["label"].split(" ")[0]


def chart_data(data):
    """The three charts' inputs. Excluded runs never contribute."""
    gone = _excluded_ids(data)
    kept = kept_experiments(data)
    kept_ids = {e["id"] for e in kept}
    items = [v for v in data["evidence"].values() if v["result_id"] in kept_ids and v["result_id"] not in gone]

    svc = data["scores"]["services"]
    tested = sorted(((s, v["score"]) for s, v in svc.items() if v.get("score") is not None), key=lambda kv: (kv[1], kv[0]))
    untested = sorted(s for s, v in svc.items() if v.get("score") is None)

    by_fault = {}
    exp = {e["id"]: e for e in kept}
    for it in items:
        if not _is_p95(it) or it["value"]["baseline"] is None or it["value"]["peak"] is None:
            continue
        e = exp[it["result_id"]]
        cur = by_fault.get(e["fault"])
        if cur is None or (it["value"]["change"] or 0) > cur["change"]:
            by_fault[e["fault"]] = {"fault": e["fault"], "run": e["id"], "target": e["target"], "caller": _caller(it),
                                    "baseline": it["value"]["baseline"], "peak": it["value"]["peak"],
                                    "change": it["value"]["change"] or 0}
    p95 = sorted(by_fault.values(), key=lambda r: -r["change"])

    err = {}
    for it in items:
        if _is_err(it) and it["value"]["change"] is not None:
            e = exp[it["result_id"]]
            if e["id"] not in err or it["value"]["change"] > err[e["id"]]["change"]:
                err[e["id"]] = {"run": e["id"], "target": e["target"], "fault": e["fault"], "caller": _caller(it),
                                "change": it["value"]["change"]}
    errors = sorted(err.values(), key=lambda r: (-r["change"], r["run"]))
    return {"services": tested, "untested": untested, "p95": p95, "errors": errors, "error_runs": len(kept)}


def _unit(item):
    return " ms" if _is_p95(item) else "%"


def _impact_cells(item):
    """(baseline -> peak, change) text of an impact item; same numbers as report.fmt_value."""
    v, u = item["value"], _unit(item)
    n = lambda x: "no data" if x is None else f"{x:g}{u}"
    sign = "+" if (v["change"] or 0) > 0 else ""
    return f"{n(v['baseline'])} → {n(v['peak'])}", f"{sign}{n(v['change'])}"


def _first_sentence(text, limit=190):
    text = re.split(r"(?<=[.!?])\s", (text or "").strip(), maxsplit=1)[0]
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def best_run(data, f):
    """The finding's strongest non-excluded run (largest summed change of its impact evidence)."""
    gone = _excluded_ids(data)
    runs = [r for r in f["runs"] if r not in gone]
    if not runs:
        return None
    wt = lambda r: sum(abs(i["value"]["change"] or 0) / (100 if _is_p95(i) else 1)
                       for i in _evidence_of(data, r) if i["kind"] == "impact" and i["id"] in f["evidence_ids"])
    return max(runs, key=lambda r: (wt(r), r))


def what_happened(data, f):
    """Plain-language sentences about the strongest run, from its evidence items."""
    run = best_run(data, f)
    if run is None:
        return []
    mine = [data["evidence"][i] for i in f["evidence_ids"] if i in data["evidence"] and data["evidence"][i]["result_id"] == run]
    out = []
    errs = sorted((i for i in mine if _is_err(i) and (i["value"]["change"] or 0) > 0), key=lambda i: -i["value"]["change"])
    lats = sorted((i for i in mine if _is_p95(i) and (i["value"]["change"] or 0) > 0), key=lambda i: -i["value"]["change"])
    for i in errs[:2]:
        a, b = _impact_cells(i)[0].split(" → ")
        out.append(f"{_caller(i)} error rate rose from {a} to {b}.")
    for i in lats[:2]:
        a, b = _impact_cells(i)[0].split(" → ")
        out.append(f"{_caller(i)} p95 latency rose from {a} to {b}.")
    for kind in ("amplification", "recovery", "guard"):
        for i in mine:
            if i["kind"] == kind:
                out.append(_cap(report.fmt_value(i).replace("->", "→").rstrip(".")) + ".")
                break
    if not out:
        out.append("No measured change in the cited evidence beyond the plan itself.")
    n_ok = len([r for r in f["runs"] if r not in _excluded_ids(data)])
    out.append(f"Strongest run: {run_time(run)} ({n_ok} run(s) in total).")
    return out


def _cap(text):
    return text[:1].upper() + text[1:]


def evidence_rows(data, f, limit=7):
    run = best_run(data, f)
    if run is None:
        return []
    mine = [data["evidence"][i] for i in f["evidence_ids"] if i in data["evidence"] and data["evidence"][i]["result_id"] == run]
    imp = sorted((i for i in mine if i["kind"] == "impact"), key=lambda i: -abs(i["value"]["change"] or 0))
    rows = []
    for i in imp[:limit]:
        a, b = _impact_cells(i)
        rows.append((i["label"], a, b))
    for i in mine:
        if i["kind"] in ("amplification", "recovery", "guard") and len(rows) < limit + 2:
            rows.append((i["label"], report.fmt_value(i).replace("->", "→"), ""))
    return rows


def evidence_index(data):
    """finding -> {run id: [evidence kinds]} for the appendix."""
    out = []
    for f in data["findings"]:
        per = {}
        for e in f["evidence_ids"]:
            run, _, what = e.partition("#")
            per.setdefault(run, []).append(what)
        out.append((f, per))
    return out


# ---- charts (matplotlib OO API: thread-safe, rendered to PNG in memory) -------------------

def _png(fig):
    buf = io.BytesIO()
    FigureCanvasAgg(fig)
    fig.savefig(buf, format="png", dpi=200)
    buf.seek(0)
    return buf


def _style_ax(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#9aa3b2")
    ax.tick_params(colors="#3b4252", labelsize=7.5)
    ax.xaxis.grid(True, color="#e6e9ef", lw=0.6)
    ax.set_axisbelow(True)


def _score_color(s):
    return BAD_HEX if s < SCORE_RED_BELOW else (GOOD_HEX if s >= SCORE_GREEN_FROM else WARN_HEX)


def chart_services(cd):
    rows = cd["services"] + [(s, None) for s in cd["untested"]]
    h = max(1.6, 0.175 * len(rows) + 0.45)
    fig = Figure(figsize=(6.85, h))
    ax = fig.add_axes([0.2, 0.12, 0.74, 0.86])
    ys = list(range(len(rows)))[::-1]                   # weakest on top
    for y, (s, v) in zip(ys, rows):
        if v is None:
            ax.text(1, y, "not tested", va="center", fontsize=7, color="#7b8494", style="italic")
        else:
            ax.barh(y, v, color=_score_color(v), height=0.68)
            ax.text(v + 1, y, str(v), va="center", fontsize=7.5, color="#1d2330")
    ax.set_yticks(ys)
    ax.set_yticklabels([s for s, _ in rows])
    ax.set_xlim(0, 105)
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.set_xlabel(f"Resilience score (red < {SCORE_RED_BELOW}, amber {SCORE_RED_BELOW}–{SCORE_GREEN_FROM - 1}, green ≥ {SCORE_GREEN_FROM})", fontsize=7.5)
    _style_ax(ax)
    ax.yaxis.grid(False)
    return _png(fig), 6.85, h


def chart_latency(cd):
    rows = cd["p95"]
    fig = Figure(figsize=(6.85, 2.6))
    ax = fig.add_axes([0.09, 0.30, 0.89, 0.62])
    if not rows:
        ax.text(0.5, 0.5, "No latency evidence yet", ha="center", transform=ax.transAxes)
        return _png(fig), 6.85, 2.9
    x = range(len(rows))
    w = 0.36
    base = [r["baseline"] for r in rows]
    peak = [r["peak"] for r in rows]
    pos = [v for v in base + peak if v > 0]
    use_log = bool(pos) and max(pos) / min(pos) > 20
    b1 = ax.bar([i - w / 2 for i in x], base, w, color="#9aa5b1", label="Normal (baseline)")
    b2 = ax.bar([i + w / 2 for i in x], peak, w, color="#1f4e8c", label="Under attack (peak)")
    for bars in (b1, b2):
        for b in bars:
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(), f"{b.get_height():,.0f}" if b.get_height() >= 100 else f"{b.get_height():g}", ha="center", va="bottom", fontsize=6.5)
    if use_log:
        ax.set_yscale("log")
        ax.set_ylim(min(pos) * 0.6, max(pos) * 3)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{r['fault']}\n{r['target']} → {r['caller']}" for r in rows], fontsize=7)
    ax.set_ylabel("p95 latency (ms)" + (", log scale" if use_log else ""), fontsize=7.5)
    ax.legend(fontsize=7, frameon=False, loc="upper right")
    _style_ax(ax)
    ax.xaxis.grid(False)
    ax.yaxis.grid(True, color="#e6e9ef", lw=0.6)
    return _png(fig), 6.85, 2.9


def chart_errors(cd):
    rows = cd["errors"][:ERROR_CHART_MAX]
    h = max(1.4, 0.2 * len(rows) + 0.5)
    fig = Figure(figsize=(6.85, h))
    ax = fig.add_axes([0.37, 0.18, 0.57, 0.80])
    if not rows:
        ax.text(0.5, 0.5, "No error-rate evidence yet", ha="center", transform=ax.transAxes)
        return _png(fig), 6.85, h
    ys = list(range(len(rows)))[::-1]
    for y, r in zip(ys, rows):
        ax.barh(y, r["change"], color="#1f4e8c", height=0.66)
        ax.text(r["change"] + 1, y, f"{r['change']:+g}", va="center", fontsize=7)
    ax.set_yticks(ys)
    ax.set_yticklabels([f"{run_time(r['run'])[:-4]}  {r['fault']} · {r['target']}" for r in rows], fontsize=6.5)
    ax.set_xlim(0, max(105, max(r["change"] for r in rows) * 1.12))
    ax.set_xlabel("Error-rate change at the worst-hit service (percentage points)", fontsize=7.5)
    _style_ax(ax)
    ax.yaxis.grid(False)
    return _png(fig), 6.85, h


def _img(chart, width_mm=174):
    buf, w, h = chart
    return Image(buf, width=width_mm * mm, height=width_mm * mm * h / w)


# ---- flowables -------------------------------------------------------------------------

def pill(text, hex_color, dark_text=False):
    t = Table([[Paragraph(f"<font color='{'#1b1203' if dark_text else 'white'}'><b>{esc(text)}</b></font>",
                          _style("pill", fontSize=7, leading=9, alignment=1))]], colWidths=None)
    t._argW = [pdfmetrics.stringWidth(str(text), "DejaVu-Bold", 7) + 10]
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(hex_color)), ("ROUNDEDCORNERS", [6, 6, 6, 6]),
                           ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                           ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    return t


def sev_pill(sev):
    return pill(sev.upper(), SEV_HEX.get(sev, "#607d8b"), dark_text=(sev == "Medium"))


def conf_pill(c):
    label = "High" if c >= 0.7 else ("Medium" if c >= 0.4 else "Low")
    return pill(f"{label} confidence ({c:g})", "#e8edf5", dark_text=True)


def _tbl(rows, widths, header=True, zebra=True, extra=None):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0, splitByRow=1)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
          ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
          ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), PANEL), ("LINEBELOW", (0, 0), (-1, 0), 0.8, ACCENT)]
    if extra:
        st += extra
    t.setStyle(TableStyle(st))
    return t


def P(text, style="body"):
    return Paragraph(text, S[style])


def _row_of(*cells, widths=None):
    return Table([list(cells)], colWidths=widths, hAlign="LEFT",
                 style=[("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])


# ---- pages -----------------------------------------------------------------------------

def _meaning(score):
    if score is None:
        return "Nothing has been tested yet."
    if score >= SCORE_GREEN_FROM:
        return "Mostly resilient: injected faults were absorbed with limited customer impact."
    if score >= SCORE_RED_BELOW:
        return "Mixed: the system survives, but some dependencies fail hard or degrade silently."
    return "Fragile: several faults caused visible customer-facing failures."


def page_summary(data):
    kept = kept_experiments(data)
    ov = data["scores"]["overall"]
    n_fault = len({e["fault"] for e in kept})
    date = datetime.now(timezone.utc).strftime("%d %B %Y")
    el = [P("Chaos Copilot: Resilience Report, OpenTelemetry Astronomy Shop", "title"), Spacer(1, 2),
          P(f"{date} · {esc(TEAM_NAME)}", "sub"),
          P(f"<b>{len(kept)}</b> experiments · <b>{n_fault}</b> fault types · <b>{ov['tested']}</b> services tested "
            f"(of {ov['services']})", "sub"), Spacer(1, 8)]

    score_txt = f"{ov['score']} / 100" if ov["score"] is not None else "not tested"
    right = [P("<b>Overall resilience</b>", "body"), P(_meaning(ov["score"]), "body")]
    if ov.get("provisional"):
        right.insert(1, P(f"<font color='#b26a00'><b>Provisional:</b> {esc(ov.get('provisional_note') or '')}</font>", "small"))
    box = Table([[P(esc(score_txt), "score"), right]], colWidths=[62 * mm, 112 * mm])
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PANEL), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                             ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                             ("LINEBEFORE", (0, 0), (0, 0), 3, ACCENT)]))
    el += [box, P("Top 3 weaknesses", "h2")]

    if not data["findings"]:
        el.append(P("No weaknesses were found in the experiments run so far.", "body"))
    for f in data["findings"][:3]:
        impact = _first_sentence(f["root_cause"])
        if len(f["runs"]) == 1:
            ex = next((e for e in data["experiments"] if e["id"] == f["runs"][0]), None)
            if ex and ex.get("quality") != "excluded" and (ex["recovery_s"] or 0) > 0:
                impact += f" Recovered in {ex['recovery_s']:g} s." if ex["recovered"] else " Did not recover in time."
        card = Table([[_row_of(sev_pill(f["severity"]), P(f"<b>{esc(f['title'])}</b>", "h3"), widths=[24 * mm, 140 * mm])],
                      [P(esc(impact), "body")],
                      [P(f"<font color='#1f4e8c'><b>Fix:</b></font> {esc(_first_sentence(f['fix'], 170))}", "small")]],
                     colWidths=[174 * mm])
        card.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor(SEV_HEX.get(f["severity"], "#607d8b"))),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                                  ("TOPPADDING", (0, 0), (0, 0), 6), ("BOTTOMPADDING", (0, -1), (-1, -1), 6)]))
        el += [KeepTogether(card), Spacer(1, 5)]

    el.append(P("What held up", "h2"))
    held = [f"<b>{esc(s['title'])}.</b> {esc(s['detail'])}" for s in data["strengths"][:2]]
    for line in held or ["No clear strength has been observed yet."]:
        el.append(P("• " + line, "body"))
    el.append(P("How it works", "h2"))
    for line in ("<b>1.</b> An AI agent plans the experiment from the live service dependency map.",
                 "<b>2.</b> Policy checks and a human approval gate it. The AI never executes anything.",
                 "<b>3.</b> A guarded injection runs on the target Compose project only, with a verified rollback saved first.",
                 "<b>4.</b> An AI analysis agent explains the result, and code checks every claim against stored evidence."):
        el.append(P(line, "body"))
    return el


def page_charts(data):
    cd = chart_data(data)
    weakest = cd["services"][:2]
    t1 = (f"{' and '.join(s for s, _ in weakest)} {'are' if len(weakest) > 1 else 'is'} the weakest service{'s' if len(weakest) > 1 else ''}"
          if weakest else "No service has been scored yet")
    if cd["untested"]:
        t1 += f"; {len(cd['untested'])} not tested yet"
    el = [PageBreak(), P("Charts", "h2"), KeepTogether([P(esc(t1), "cap"), _img(chart_services(cd))])]
    if cd["p95"]:
        w = cd["p95"][0]
        t2 = f"Worst case: {w['fault']} on {w['target']} pushed {w['caller']} p95 from {w['baseline']:g} ms to {w['peak']:,.0f} ms"
    else:
        t2 = "No latency evidence yet"
    el += [KeepTogether([P(esc(t2), "cap"), _img(chart_latency(cd))])]
    if cd["errors"]:
        w = cd["errors"][0]
        t3 = (f"{w['fault']} on {w['target']} caused the largest error-rate jump: {w['change']:+g} points at {w['caller']}"
              f" (top {min(len(cd['errors']), ERROR_CHART_MAX)} of {cd['error_runs']} experiments shown)")
    else:
        t3 = "No error-rate evidence yet"
    el += [KeepTogether([P(esc(t3), "cap"), _img(chart_errors(cd))])]
    return el


def finding_block(data, i, f):
    head = Table([[_row_of(sev_pill(f["severity"]), conf_pill(f["confidence"]), widths=[24 * mm, 50 * mm])],
                  [P(f"{i}. {esc(f['title'])}", "h3")]], colWidths=[174 * mm])
    head.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    runs = [r for r in f["runs"] if r not in _excluded_ids(data)]
    low = sum(1 for r in runs if next((e.get("quality") for e in data["experiments"] if e["id"] == r), "ok") == "low_evidence")
    parts = [head, P(f"Based on {len(runs)} run(s) ({low} low evidence) · service {esc(f['service'])}"
                     + (f" · edge {esc(f['edge'])}" if f["edge"] else ""), "small")]
    sections = [("WHAT HAPPENED", " ".join(esc(s) for s in what_happened(data, f))),
                ("WHY", esc(f["root_cause"])),
                ("FIX", esc(f["fix"]) + (f" <font color='#5b6577'>(at {esc(f['fix_location'])})</font>" if f.get("fix_location") else "")),
                ("HOW TO VERIFY", esc(f["verify_by"]))]
    for label, text in sections:
        parts += [Spacer(1, 3), P(label, "label"), P(text, "body")]
    rows = evidence_rows(data, f)
    if rows:
        run = best_run(data, f)
        tab = [[P("What", "cellb"), P("Baseline → peak", "cellb"), P("Change", "cellb"), P("Run", "cellb")]]
        tab += [[P(esc(a), "cell"), P(esc(b), "cell"), P(esc(c), "cell"), P(run_time(run), "cell")] for a, b, c in rows]
        parts += [Spacer(1, 4), _tbl(tab, [48 * mm, 58 * mm, 26 * mm, 28 * mm])]
    outer = Table([[parts]], colWidths=[174 * mm])
    outer.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor(SEV_HEX.get(f["severity"], "#607d8b"))),
                               ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                               ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return outer


def page_findings(data):
    el = [PageBreak(), P("Findings, ranked by severity", "h2")]
    fs = data["findings"]
    if not fs:
        return el + [P("No weaknesses were found in the experiments run so far.", "body")]
    for i, f in enumerate(fs[:DETAILED_FINDINGS], 1):
        el += [KeepTogether([finding_block(data, i, f)]), Spacer(1, 8)]
    rest = fs[DETAILED_FINDINGS:]
    if rest:
        el.append(P(f"Other findings ({len(rest)})", "h2"))
        rows = [[P("#", "cellb"), P("Severity", "cellb"), P("Finding", "cellb"), P("Confidence", "cellb"), P("Runs", "cellb")]]
        for i, f in enumerate(rest, DETAILED_FINDINGS + 1):
            rows.append([P(str(i), "cell"), P(f"<font color='{SEV_HEX.get(f['severity'], '#607d8b')}'><b>{esc(f['severity'])}</b></font>", "cell"),
                         P(esc(f["title"]), "cell"), P(f"{f['confidence']:g}", "cell"), P(str(len(f["runs"])), "cell")])
        el.append(_tbl(rows, [8 * mm, 20 * mm, 112 * mm, 20 * mm, 14 * mm]))
    return el


def _short(text, limit=60):
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def page_experiments(data):
    el = [PageBreak(), P("Experiments", "h2")]
    qc = data.get("quality_counts") or {}
    el.append(P(f"{qc.get('ok', 0)} ok · {qc.get('low_evidence', 0)} low evidence · {qc.get('excluded', 0)} excluded (listed in the appendix). "
                "Low-evidence runs count less towards a finding's confidence; excluded runs feed no finding or score.", "small"))
    el.append(Spacer(1, 4))
    rows = [[P(h, "cellb") for h in ("Time", "Target", "Fault", "Mag.", "Outcome", "Recovery", "Score", "Quality")]]
    for e in sorted(kept_experiments(data), key=lambda e: e["id"]):
        q = e.get("quality", "ok")
        rec = f"{e['recovery_s']:g} s" if e["recovered"] else "not recovered"
        mag = f"{e['magnitude']:g} ms" if e["fault"] == "network_delay" else (f"{e['magnitude']:g}" if e["magnitude"] else "–")
        rows.append([P(run_time(e["id"])[:-4], "cell"), P(esc(e["target"]), "cell"), P(esc(e["fault"]), "cell"), P(mag, "cell"),
                     P(esc(_short(e["outcome"])), "cell"), P(rec, "cell"), P(str(e["score"]), "cell"),
                     P(f"<font color='{QUALITY_HEX.get(q, '#607d8b')}'><b>{esc(report.QUALITY_LABEL.get(q, q))}</b></font>", "cell")])
    el.append(_tbl(rows, [21 * mm, 26 * mm, 25 * mm, 16 * mm, 37 * mm, 20 * mm, 12 * mm, 17 * mm]))
    return el


def page_appendix(data):
    el = [PageBreak(), P("Appendix", "h2"), P("Method", "h3"), Spacer(1, 3)]
    for line in report.method_lines(data):
        el.append(P("• " + esc(line).replace("&gt;=", "≥").replace(" x ", " × "), "small"))
        el.append(Spacer(1, 2))
    el += [Spacer(1, 4), P("Safety invariants", "h3"), Spacer(1, 3)]
    for line in ("The AI never executes anything: it plans and analyses, code and a human approval do the rest.",
                 "A rollback is saved before any fault is injected, and recovery is verified afterwards.",
                 "Only the target Docker Compose project can be touched.",
                 "One experiment runs at a time; a guard aborts it if error-rate or latency thresholds are breached."):
        el.append(P("• " + line, "small"))
    th = data.get("thresholds", {})
    el += [Spacer(1, 6), P("Data-quality rules", "h3"), Spacer(1, 3),
           P(f"• ok: enough measurements to trust the run. low evidence: some metric was not measured; it counts less towards confidence "
             f"(confidence {th.get('LOW_EVIDENCE_CONFIDENCE', 0.5):g}) and can never carry a Critical or High finding alone "
             f"(its score is capped at {th.get('LOW_EVIDENCE_MAX_SCORE', 49.9):g}).", "small"),
           P("• excluded: the run measured something other than the planned fault (test harness, forced abort, concurrent outage). "
             "It feeds no finding, score or chart.", "small"),
           Spacer(1, 6), P("Excluded runs", "h3"), Spacer(1, 3)]
    ex = data.get("excluded", [])
    if ex:
        rows = [[P("Run", "cellb"), P("Time", "cellb"), P("Target · fault", "cellb"), P("Reason", "cellb")]]
        rows += [[P(esc(x["id"]), "cell"), P(run_time(x["id"]), "cell"), P(f"{esc(x['target'])} · {esc(x['fault'])}", "cell"), P(esc(x["reason"]), "cell")] for x in ex]
        el.append(_tbl(rows, [32 * mm, 28 * mm, 38 * mm, 76 * mm]))
    else:
        el.append(P("None: every run feeds the analysis.", "small"))
    el += [Spacer(1, 8), P("Evidence ID index (finding → evidence)", "h3"), Spacer(1, 3)]
    rows = [[P("#", "cellb"), P("Finding", "cellb"), P("Evidence IDs (run id, then evidence items)", "cellb")]]
    for i, (f, per) in enumerate(evidence_index(data), 1):
        ids = "<br/>".join(f"{esc(r)}: {esc(', '.join(w))}" for r, w in per.items())
        rows.append([P(str(i), "tiny"), P(esc(f["title"]), "tiny"), P(ids, "tiny")])
    el.append(_tbl(rows, [8 * mm, 54 * mm, 112 * mm]))
    return el


# ---- build -----------------------------------------------------------------------------

def build_pdf(data=None, path=PDF_PATH):
    """Write the PDF and return its path."""
    data = data or analysis_agent.latest_or_deterministic()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("DejaVu", 7.5)
        canvas.setFillColor(MUTED)
        canvas.setStrokeColor(LINE)
        canvas.line(18 * mm, 14 * mm, 192 * mm, 14 * mm)
        canvas.drawString(18 * mm, 9.5 * mm, f"Chaos Copilot · generated {stamp}")
        canvas.drawRightString(192 * mm, 9.5 * mm, f"Page {doc.page}")
        canvas.setFillColor(ACCENT)
        canvas.rect(0, A4[1] - 3 * mm, A4[0], 3 * mm, stroke=0, fill=1)
        canvas.restoreState()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=19 * mm,
                          title="Chaos Copilot: Resilience Report", author=TEAM_NAME)
    doc.addPageTemplates([PageTemplate(id="main", frames=[Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f",
                                                                leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)],
                                      onPage=footer)])
    story = page_summary(data) + page_charts(data) + page_findings(data) + page_experiments(data) + page_appendix(data)
    doc.build(story)
    return path


def is_stale(path=PDF_PATH):
    """True when the PDF is missing or older than any result or the cached analysis."""
    path = Path(path)
    if not path.exists():
        return True
    sources = list(analysis.RESULTS_DIR.glob("*.json")) + [analysis_agent.LATEST_JSON]
    newest = max((p.stat().st_mtime for p in sources if p.exists()), default=0)
    return path.stat().st_mtime < newest


if __name__ == "__main__":
    try:
        out = build_pdf()
    except (OSError, ValueError, KeyError) as err:
        print(f"Cannot build the PDF report: {err}")
        sys.exit(1)
    print(f"Wrote {out}")
