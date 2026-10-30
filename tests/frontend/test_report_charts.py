"""Visible report legends preserve authoritative counts and honest empty states."""

import re
import unittest

from preview.fixtures import report_fixture, STATUS_LABELS
from preview.server import create_preview_app
if __package__:
    from .test_markup import Markup
else:
    from test_markup import Markup


class ReportChartCase(unittest.TestCase):
    def setUp(self):
        self.client = create_preview_app(testing=True).test_client()

    def test_status_legend_preserves_all_counts_and_authoritative_total(self):
        for case in ("", "rework", "recompleted"):
            own, overall = report_fixture(case)
            for path, report in (("/reports/department?role=head", own),
                                 ("/admin/statistics?role=admin", overall)):
                with self.subTest(path=path, case=case):
                    markup = Markup(self.client.get(path + "&report_case=" + case).text)
                    chart = next(n for n in markup.nodes if "data-chart-total" in n["attrs"])
                    rows = [n for n in markup.nodes if "data-chart-status" in n["attrs"]]
                    counts = {n["attrs"]["data-chart-status"]: int(n["attrs"]["data-count"])
                              for n in rows}
                    self.assertEqual(len(rows), 5)
                    self.assertEqual(counts, report["overall"]["statuses"])
                    self.assertEqual(sum(counts.values()), int(chart["attrs"]["data-chart-total"]))
                    self.assertEqual(int(chart["attrs"]["data-chart-total"]), report["overall"]["total"])
                    for row in rows:
                        self.assertTrue(row["parent"]["attrs"].get("aria-label"))
                        self.assertIn(STATUS_LABELS[row["attrs"]["data-chart-status"]], "".join(row["text"]))
                        numbers = [n for n in markup.nodes if n["parent"] is row and n["tag"] == "strong"]
                        self.assertEqual(["".join(n["text"]) for n in numbers], [row["attrs"]["data-count"]])
                        ancestor = row
                        while ancestor:
                            self.assertNotIn("hidden", ancestor["attrs"])
                            self.assertNotEqual(ancestor["attrs"].get("aria-hidden"), "true")
                            ancestor = ancestor["parent"]

    def test_empty_visuals_and_average_explanation_do_not_invent_values(self):
        for path in ("/reports/department?role=head", "/admin/statistics?role=admin"):
            with self.subTest(path=path):
                markup = Markup(self.client.get(path + "&report_case=empty").text)
                chart = next(n for n in markup.nodes if "data-chart-total" in n["attrs"])
                self.assertEqual(chart["attrs"]["data-chart-total"], "0")
                segment_styles = [n["attrs"]["style"] for n in markup.nodes
                                  if "--seg-new:" in n["attrs"].get("style", "")]
                self.assertEqual(len(segment_styles), 1)
                segments = re.findall(r"--seg-[a-z-]+:([0-9.]+)%", segment_styles[0])
                self.assertEqual(len(segments), 5)
                self.assertTrue(all(float(value) == 0 for value in segments))
                bars = [n["attrs"]["style"] for n in markup.nodes
                        if "--bar-size:" in n["attrs"].get("style", "")]
                self.assertTrue(bars)
                for style in bars:
                    value = re.fullmatch(r"--bar-size:([0-9.]+)%", style)
                    self.assertIsNotNone(value)
                    self.assertEqual(float(value.group(1)), 0)
                for case, expected_note in (("no-average", True), ("zero-average", False)):
                    average = Markup(self.client.get(path + "&report_case=" + case).text)
                    group = next(n for n in average.nodes if n["attrs"].get("data-report-average") == "overall")
                    self.assertEqual("Нет выполненных задач с подходящими датами." in "".join(group["text"]),
                                     expected_note)
