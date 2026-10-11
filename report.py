"""Findings report: reports/findings_report.md and a self-contained reports/findings_report.html.

Built from the cached analysis (reports/latest.json) when it covers the current results, otherwise from a
fresh deterministic analysis. No LLM call here. Run: python report.py
"""
import html
import json
import sys

import analysis
import analysis_agent
import run_experiment as exp

MD_PATH = analysis.REPORTS_DIR / "findings_report.md"
HTML_PATH = analysis.REPORTS_DIR / "findings_report.html"
QUALITY_LABEL = {"ok": "ok", "low_evidence": "low evidence", "excluded": "excluded"}
QUALITY_CLASS = {"ok": "ok", "low_evidence": "Medium", "excluded": "Low"}
SOURCES = {"analysis_agent": "AI analysis agent (validated against stored evidence)",
           "analysis_fallback": "deterministic rules only (AI analysis not used)"}


def fmt_value(item):
    """One-line, human-readable value of an evidence item."""
    v, kind = item["value"], item["kind"]
    n = lambda x, unit="": "no data" if x is None else f"{x:g}{unit}"
    if kind == "impact":
        unit = " ms" if item["id"].endswith("p95_ms") else "%"
        sign = "+" if (v["change"] or 0) > 0 else ""
        return f"{n(v['baseline'], unit)} -> {n(v['peak'], unit)} ({sign}{n(v['change'], unit)})"
    if kind == "amplification":
        return f"+{v['increase_ms']:g} ms for {v['delay_ms']:g} ms injected = {v['ratio']:g}x" + (" (amplified)" if v["amplified"] else "")
    if kind == "recovery":
        return f"recovered after {v['seconds']} s" if v["recovered"] else f"not recovered ({v.get('note') or 'timeout ' + str(v['timeout_s']) + ' s'})"
    if kind == "guard":
        return f"{'aborted' if v['aborted'] else 'completed'} after {v['elapsed_s']} s: {v['reason']}"
    if kind == "claim":
        return f"[{v['status']}] {v['claim']}: {v['evidence']}"
    if kind == "plan":
        return f"{v['fault']} on {v['target']}" + (f", {v['magnitude']} ms" if v["fault"] == "network_delay" else "") + f", {v['duration_s']} s"
    if kind == "link":
        return v["url"]
    return json.dumps(v, default=str)[:200]


def _experiments(data):
    return {e["id"]: e for e in data["experiments"]}


def method_lines(data):
    sw, w = data["severity_weights"], exp.SCORE_WEIGHTS
    comp = ", ".join(f"{v:g} x {k}" for k, v in sw["components"].items())
    return [
        "Experiment resilience score = 100 - impact, impact = 100 x (" + ", ".join(f"{v:g} x {k}" for k, v in w.items())
        + "), from measured error rise, p95 rise, probe availability and recovery time (run_experiment.score_experiment).",
        f"Weakness severity (0-100) = 100 x pattern weight x ({comp}) x ({sw['conf_floor']:g} + {1 - sw['conf_floor']:g} x confidence); "
        "Critical >= 70, High >= 50, Medium >= 30, else Low. Confidence grows with the number of runs that show the same weakness "
        "and falls with missing data.",
        f"Service resilience = mean of its experiment scores minus {analysis.WEAKNESS_PENALTY['Critical']} per open Critical, "
        f"{analysis.WEAKNESS_PENALTY['High']} per open High and {analysis.WEAKNESS_PENALTY['Medium']} x confidence per open Medium "
        f"weakness (at most {analysis.WEAKNESS_PENALTY_CAP}); the overall score is provisional with fewer than "
        f"{analysis.MIN_RELIABLE_RUNS} reliable (quality ok) runs; overall = mean over "
        "tested services weighted by 1 + criticality. Untested services are shown as not tested, never as 100.",
        "Deterministic: evidence index, weakness rules (thresholds in analysis.py), severity and all scores. "
        "AI (when available): root-cause explanation, source/config/trace investigation, specific fix and verification. "
        "Code rejects AI findings that cite unknown evidence, raise severity by more than one level, or quote numbers "
        "that are not in the cited evidence (that text is replaced by the rule's own text).",
    ]


# ---- markdown --------------------------------------------------------------------------

def _md_cell(x):
    return str(x).replace("|", "\\|").replace("\n", " ")


def render_md(data):
    ov, ex = data["scores"]["overall"], _experiments(data)
    out = ["# Chaos Copilot findings report", "",
           f"Generated {data['generated_at']} from {len(data['results_used'])} experiments "
           f"({len(data['skipped'])} skipped). Analysis: {SOURCES.get(data['source'], data['source'])}"
           + (f" - {data['note']}" if data.get("note") else "") + ".", "",
           f"**Overall resilience: {ov['score'] if ov['score'] is not None else 'not tested'}"
           f"{'/100' if ov['score'] is not None else ''}**"
           + (f" **({ov['provisional_note']})**" if ov.get("provisional") else "") + f" ({ov['explain']})", "",
           f"> {data['summary']}", "", "## Resilience by service", "",
           "| Service | Score | How it is computed |", "|---|---:|---|"]
    for s, v in sorted(data["scores"]["services"].items(), key=lambda kv: (kv[1]["score"] is None, kv[1]["score"] or 0, kv[0])):
        out.append(f"| {s} | {v['score'] if v['score'] is not None else v['status']} | {_md_cell(v['explain'])} |")
    out += ["", "## Findings, ranked by severity", ""]
    if not data["findings"]:
        out += ["No weaknesses were found in the experiments run so far.", ""]
    for i, f in enumerate(data["findings"], 1):
        out += [f"### {i}. [{f['severity']}] {f['title']}", "",
                f"Service **{f['service']}**" + (f", edge `{f['edge']}`" if f["edge"] else "")
                + f", pattern `{f['pattern']}`, severity score {f['severity_score']}, confidence {f['confidence']}, "
                  f"{len(f['runs'])} run(s), source: {f['source']}.", "",
                f"**Root cause.** {f['root_cause']}", ""]
        if f.get("agent_adjustment"):
            out += [f"**Agent adjustment** (rules said {f['deterministic_severity']}). {f['agent_adjustment']}", ""]
        out += ["| Evidence | What | Value |", "|---|---|---|"]
        for e in f["evidence_ids"][:12]:
            item = data["evidence"].get(e)
            if item and item["kind"] != "link":
                out.append(f"| `{e}` | {_md_cell(item['label'])} | {_md_cell(fmt_value(item))} |")
        links = [f"[Grafana {r}]({ex[r]['grafana_url']}) / [Jaeger]({ex[r]['jaeger_url']})" for r in f["runs"][:4] if r in ex]
        out += ["", "Links: " + (", ".join(links) or "none"), "",
                f"**Fix.** {f['fix']}" + (f" (at `{f['fix_location']}`)" if f.get("fix_location") else ""), "",
                f"**Verify by.** {f['verify_by']}", ""]
    out += ["## Strengths: what held up", ""]
    out += [f"- **{s['title']}** ({len(s['runs'])} run(s)): {s['detail']}" for s in data["strengths"]] or ["- None observed yet."]
    out += [f"- {n}" for n in data.get("strength_notes", [])]
    qc = data.get("quality_counts") or {}
    out += ["", "## Experiments", "", f"{qc.get('ok', 0)} ok, {qc.get('low_evidence', 0)} low evidence, "
            f"{qc.get('excluded', 0)} excluded. Low-evidence runs count less towards a finding's confidence and never carry "
            "a Critical/High finding alone; excluded runs feed no finding or score.", "",
            "| Result | Target | Fault | Magnitude | Outcome | Recovery | Score | Quality |", "|---|---|---|---:|---|---|---:|---|"]
    for e in data["experiments"]:
        rec = f"{e['recovery_s']} s" if e["recovered"] else "not recovered"
        quality = QUALITY_LABEL.get(e.get("quality", "ok"), e.get("quality")) + (f": {e['quality_reason']}" if e.get("quality_reason") else "")
        out.append(f"| {e['id']} | {e['target']} | {e['fault']} | {e['magnitude']} | {_md_cell(e['outcome'][:90])} | {rec} | {e['score']} | {_md_cell(quality)} |")
    out += ["", "## Excluded runs", ""]
    out += [f"- `{x['id']}` ({x['fault']} on {x['target']}): {x['reason']}" for x in data.get("excluded", [])] \
        or ["None: every run feeds the analysis."]
    out += ["", "## Method", ""] + [f"- {line}" for line in method_lines(data)] + [""]
    return "\n".join(out)


# ---- html ------------------------------------------------------------------------------

CSS = """
:root { --bg:#0b0e15; --surface:#121723; --surface-2:#192033; --line:#222a3c; --text:#e9ecf3; --text-2:#b3bbcd;
  --text-3:#8d96ac; --accent:#6b97ff; --ok:#46d39a; --warn:#f6ad37; --bad:#ff7673; color-scheme:dark; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:1080px; margin:0 auto; padding:32px 20px 64px; }
h1 { font-size:26px; margin:0 0 4px; } h2 { font-size:19px; margin:36px 0 12px; border-bottom:1px solid var(--line); padding-bottom:6px; }
h3 { font-size:16px; margin:0 0 6px; } a { color:var(--accent); } code { font:12px ui-monospace,Consolas,monospace; color:var(--text-2); }
.muted { color:var(--text-3); } .summary { border-left:3px solid var(--accent); padding:8px 14px; background:var(--surface); }
.overall { font-size:40px; font-weight:700; } .card { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin:12px 0; }
.pill { display:inline-block; padding:1px 9px; border-radius:99px; font-size:12px; font-weight:650; border:1px solid var(--line); }
.Critical { color:#fff; background:#cf3434; border-color:#cf3434; } .High { color:#1b1203; background:var(--warn); border-color:var(--warn); }
.Medium { color:var(--accent); border-color:var(--accent); } .Low { color:var(--text-2); }
.table-wrap { overflow-x:auto; } table { width:100%; border-collapse:collapse; font-size:13px; margin:8px 0; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--text-3); font-size:11px; text-transform:uppercase; letter-spacing:.06em; }
dl { display:grid; grid-template-columns:110px minmax(0,1fr); gap:6px 14px; margin:10px 0 0; } dt { color:var(--text-3); } dd { margin:0; }
li { margin:4px 0; }
"""


def render_html(data):
    e = lambda x: html.escape(str(x))
    ov, ex = data["scores"]["overall"], _experiments(data)
    parts = [f"<title>Chaos Copilot findings report</title><style>{CSS}</style><main>",
             "<h1>Chaos Copilot findings report</h1>",
             f"<p class='muted'>Generated {e(data['generated_at'])} from {len(data['results_used'])} experiments "
             f"({len(data['skipped'])} skipped). Analysis: {e(SOURCES.get(data['source'], data['source']))}"
             f"{' - ' + e(data['note']) if data.get('note') else ''}.</p>",
             f"<div class='card'><div class='overall'>{e(ov['score']) if ov['score'] is not None else 'not tested'}"
             f"<span class='muted' style='font-size:16px'>{'/100' if ov['score'] is not None else ''} overall resilience</span></div>"
             + (f"<div><span class='pill Medium'>{e(ov['provisional_note'])}</span></div>" if ov.get("provisional") else "")
             + f"<div class='muted'>{e(ov['explain'])}</div></div>",
             f"<p class='summary'>{e(data['summary'])}</p>",
             "<h2>Resilience by service</h2><div class='table-wrap'><table><tr><th>Service</th><th>Score</th><th>How it is computed</th></tr>"]
    for s, v in sorted(data["scores"]["services"].items(), key=lambda kv: (kv[1]["score"] is None, kv[1]["score"] or 0, kv[0])):
        parts.append(f"<tr><td>{e(s)}</td><td>{e(v['score']) if v['score'] is not None else '<span class=muted>' + e(v['status']) + '</span>'}</td>"
                     f"<td class='muted'>{e(v['explain'])}</td></tr>")
    parts.append("</table></div><h2>Findings, ranked by severity</h2>")
    if not data["findings"]:
        parts.append("<p class='muted'>No weaknesses were found in the experiments run so far.</p>")
    for i, f in enumerate(data["findings"], 1):
        rows = "".join(f"<tr><td><code>{e(x)}</code></td><td>{e(data['evidence'][x]['label'])}</td><td>{e(fmt_value(data['evidence'][x]))}</td></tr>"
                       for x in f["evidence_ids"][:12] if x in data["evidence"] and data["evidence"][x]["kind"] != "link")
        links = ", ".join(f"<a href='{e(ex[r]['grafana_url'])}'>Grafana {e(r)}</a> / <a href='{e(ex[r]['jaeger_url'])}'>Jaeger</a>"
                          for r in f["runs"][:4] if r in ex) or "none"
        parts.append(
            f"<div class='card'><h3>{i}. <span class='pill {e(f['severity'])}'>{e(f['severity'])}</span> {e(f['title'])}</h3>"
            f"<div class='muted'>service {e(f['service'])}{', edge <code>' + e(f['edge']) + '</code>' if f['edge'] else ''}, "
            f"pattern <code>{e(f['pattern'])}</code>, severity score {e(f['severity_score'])}, confidence {e(f['confidence'])}, "
            f"{len(f['runs'])} run(s), source: {e(f['source'])}</div>"
            f"<dl><dt>Root cause</dt><dd>{e(f['root_cause'])}</dd>"
            + (f"<dt>Adjustment</dt><dd>rules said {e(f['deterministic_severity'])}: {e(f['agent_adjustment'])}</dd>" if f.get("agent_adjustment") else "")
            + f"<dt>Fix</dt><dd>{e(f['fix'])}{' <code>' + e(f['fix_location']) + '</code>' if f.get('fix_location') else ''}</dd>"
              f"<dt>Verify by</dt><dd>{e(f['verify_by'])}</dd><dt>Links</dt><dd>{links}</dd></dl>"
              f"<div class='table-wrap'><table><tr><th>Evidence</th><th>What</th><th>Value</th></tr>{rows}</table></div></div>")
    parts.append("<h2>Strengths: what held up</h2><ul>")
    parts += [f"<li><b>{e(s['title'])}</b> ({len(s['runs'])} run(s)): {e(s['detail'])}</li>" for s in data["strengths"]] \
        or ["<li class='muted'>None observed yet.</li>"]
    parts += [f"<li>{e(n)}</li>" for n in data.get("strength_notes", [])]
    qc = data.get("quality_counts") or {}
    parts.append(f"</ul><h2>Experiments</h2><p class='muted'>{qc.get('ok', 0)} ok, {qc.get('low_evidence', 0)} low evidence, "
                 f"{qc.get('excluded', 0)} excluded. Low-evidence runs count less towards a finding's confidence and never carry a "
                 "Critical/High finding alone; excluded runs feed no finding or score.</p><div class='table-wrap'><table><tr><th>Result</th><th>Target</th><th>Fault</th>"
                 "<th>Magnitude</th><th>Outcome</th><th>Recovery</th><th>Score</th><th>Quality</th></tr>")
    for x in data["experiments"]:
        q = x.get("quality", "ok")
        parts.append(f"<tr><td><a href='{e(x['grafana_url'])}'>{e(x['id'])}</a></td><td>{e(x['target'])}</td><td>{e(x['fault'])}</td>"
                     f"<td>{e(x['magnitude'])}</td><td>{e(x['outcome'][:90])}</td>"
                     f"<td>{e(str(x['recovery_s']) + ' s') if x['recovered'] else 'not recovered'}</td><td>{e(x['score'])}</td>"
                     f"<td><span class='pill {QUALITY_CLASS.get(q, '')}'>{e(QUALITY_LABEL.get(q, q))}</span>"
                     f"{' <span class=muted>' + e(x['quality_reason']) + '</span>' if x.get('quality_reason') else ''}</td></tr>")
    parts.append("</table></div><h2>Excluded runs</h2><ul>")
    parts += [f"<li><code>{e(x['id'])}</code> ({e(x['fault'])} on {e(x['target'])}): {e(x['reason'])}</li>" for x in data.get("excluded", [])] \
        or ["<li class='muted'>None: every run feeds the analysis.</li>"]
    parts.append("</ul><h2>Method</h2><ul>" + "".join(f"<li>{e(l)}</li>" for l in method_lines(data)) + "</ul></main>")
    return "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" \
           + parts[0] + "</head><body>" + "".join(parts[1:]) + "</body></html>"


def write(data=None):
    data = data or analysis_agent.latest_or_deterministic()
    analysis.REPORTS_DIR.mkdir(exist_ok=True)
    MD_PATH.write_text(render_md(data), encoding="utf-8")
    HTML_PATH.write_text(render_html(data), encoding="utf-8")
    return data


if __name__ == "__main__":
    try:
        d = write()
    except (OSError, ValueError, KeyError) as err:
        print(f"Cannot build the report: {err}")
        sys.exit(1)
    print(f"Wrote {MD_PATH} and {HTML_PATH} ({len(d['findings'])} findings, source {d['source']})")
