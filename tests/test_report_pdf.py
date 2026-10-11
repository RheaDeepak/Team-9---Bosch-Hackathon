import io
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfReader

import app
import report_pdf


def impact(run, svc, kind, base, peak):
    unit = "p95_ms" if kind == "p95" else "error_rate"
    i = f"{run}#impact.{svc}.{unit}"
    label = f"{svc} p95 (ms)" if kind == "p95" else f"{svc} error rate (%)"
    return i, {"id": i, "result_id": run, "kind": "impact", "label": label,
               "value": {"baseline": base, "peak": peak, "change": round(peak - base, 1)}}


def experiment(run, target, fault, quality="ok"):
    return {"id": run, "target": target, "fault": fault, "magnitude": 0, "outcome": "completed", "recovered": True,
            "recovery_s": 12.0, "score": 60, "verdict": "x", "quality": quality, "grafana_url": "", "jaeger_url": ""}


def fixture(n_findings=2):
    ok, bad = "20261011T041909Z", "20261010T091055Z"
    ev = dict([impact(ok, "frontend", "p95", 30.0, 900.0), impact(ok, "frontend", "err", 0.0, 40.0),
               impact(bad, "ghostsvc", "p95", 10.0, 99999.0), impact(bad, "ghostsvc", "err", 0.0, 100.0)])
    findings = [{"id": f"f{i}", "title": f"Finding number {i}", "service": "cart", "edge": "", "pattern": "p", "severity": "High",
                 "severity_score": 60.0, "root_cause": "Stopping cart raised frontend error rate from 0.0% to 40.0%. More.",
                 "evidence_ids": [k for k in ev if k.startswith(ok)], "fix": "Add a fallback.", "fix_location": None,
                 "verify_by": "Re-run it.", "confidence": 0.6, "runs": [ok], "source": "deterministic"} for i in range(n_findings)]
    return {"generated_at": "2026-10-11T00:00:00+00:00", "source": "analysis_fallback", "note": None, "results_used": [ok],
            "skipped": [], "excluded": [{"id": bad, "target": "ghostsvc", "fault": "container_stop", "reason": "test harness"}],
            "quality_counts": {"ok": 1, "low_evidence": 0, "excluded": 1}, "findings": findings,
            "strengths": [], "strength_notes": [], "summary": "s",
            "scores": {"overall": {"score": 71, "tested": 1, "services": 2, "explain": "", "reliable_runs": 1,
                                   "provisional": True, "provisional_note": "provisional: 1 reliable run"},
                       "services": {"cart": {"score": 40, "status": "tested"}, "email": {"score": None, "status": "not tested"}}},
            "experiments": [experiment(ok, "cart", "container_stop"), experiment(bad, "ghostsvc", "container_stop", "excluded")],
            "evidence": ev, "thresholds": {}, "severity_weights": {"components": {"a": 1.0}, "conf_floor": 0.5}}


def pdf_text(data, page=None):
    with tempfile.TemporaryDirectory() as d:
        path = report_pdf.build_pdf(data, Path(d) / "r.pdf")
        pages = PdfReader(str(path)).pages
        return len(pages), (pages[page].extract_text() if page is not None else "\n".join(p.extract_text() for p in pages))


class ReportPdfTest(unittest.TestCase):
    def test_builds_with_no_findings(self):
        n, text = pdf_text(fixture(0))
        self.assertGreaterEqual(n, 4)
        self.assertIn("No weaknesses", text)

    def test_builds_with_several_findings(self):
        n, _ = pdf_text(fixture(9))
        self.assertGreater(n, 4)

    def test_page_one_has_score_and_top_finding(self):
        _, text = pdf_text(fixture(2), page=0)
        self.assertIn("71 / 100", text)
        self.assertIn("Finding number 0", text)
        self.assertIn("provisional", text.lower())

    def test_excluded_runs_not_in_charts(self):
        cd = report_pdf.chart_data(fixture())
        self.assertEqual([r["run"] for r in cd["p95"]], ["20261011T041909Z"])
        self.assertEqual([r["run"] for r in cd["errors"]], ["20261011T041909Z"])
        self.assertEqual(cd["untested"], ["email"])
        self.assertNotIn("20261010T091055Z", [e["id"] for e in report_pdf.kept_experiments(fixture())])

    def test_no_raw_ids_before_appendix(self):
        _, text = pdf_text(fixture(2), page=2)
        self.assertNotIn("20261011T041909Z#", text)

    def test_route_returns_pdf(self):
        client = TestClient(app.app, base_url="http://127.0.0.1:8000")
        r = client.get("/api/report.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
