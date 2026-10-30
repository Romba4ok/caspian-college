"""Department comparison uses incoming DTO counts and a shared, honest scale."""

import re
import unittest
from unittest.mock import patch

from preview.fixtures import report_fixture
from preview.server import create_preview_app
if __package__:
    from .test_markup import Markup
else:
    from test_markup import Markup


def inside(node, ancestor):
    parent = node["parent"]
    while parent:
        if parent is ancestor:
            return True
        parent = parent["parent"]
    return False


class DepartmentComparisonCase(unittest.TestCase):
    def setUp(self):
        self.client = create_preview_app(testing=True).test_client()

    def test_department_counts_and_segment_sizes_use_incoming_and_one_scale(self):
        for case in ("", "rework", "recompleted"):
            with self.subTest(case=case):
                _, report = report_fixture(case)
                markup = Markup(self.client.get("/admin/statistics?role=admin&report_case=" + case).text)
                root = next(n for n in markup.nodes if "data-department-comparison" in n["attrs"])
                maximum = max(d["incoming"]["total"] for d in report["departments"])
                self.assertEqual(int(root["attrs"]["data-scale"]), maximum)
                rows = [n for n in markup.nodes if "data-compare-department" in n["attrs"]]
                self.assertEqual(len(rows), len(report["departments"]))
                expected = {str(d["id"]): d["incoming"] for d in report["departments"]}
                self.assertEqual({n["attrs"]["data-compare-department"] for n in rows}, set(expected))
                for row in rows:
                    attrs = row["attrs"]
                    incoming = expected[attrs["data-compare-department"]]
                    self.assertEqual(int(attrs["data-total"]), incoming["total"])
                    self.assertEqual(int(attrs["data-active"]), incoming["statuses"]["new"] + incoming["statuses"]["in_progress"])
                    self.assertEqual(int(attrs["data-completed"]), incoming["statuses"]["completed"])
                    self.assertEqual(int(attrs["data-overdue"]), incoming["overdue"])
                    segments = [n for n in markup.nodes if "data-compare-status" in n["attrs"] and inside(n, row)]
                    self.assertEqual(len(segments), 5)
                    self.assertEqual({n["attrs"]["data-compare-status"]: int(n["attrs"]["data-count"])
                                      for n in segments}, incoming["statuses"])
                    self.assertEqual(sum(int(n["attrs"]["data-count"]) for n in segments), incoming["total"])
                    for segment in segments:
                        size = re.search(r"--segment-size:\s*([0-9.]+)%", segment["attrs"].get("style", ""))
                        self.assertIsNotNone(size)
                        self.assertAlmostEqual(float(size.group(1)), int(segment["attrs"]["data-count"]) / maximum * 100,
                                               delta=0.0001)
                    links = [n for n in markup.nodes if n["tag"] == "a" and inside(n, row)]
                    target = "report-department-" + attrs["data-compare-department"]
                    self.assertTrue(any(n["attrs"].get("href") == "#" + target for n in links))
                    self.assertTrue(any(n["attrs"].get("id") == target for n in markup.nodes))

    def test_zero_incoming_does_not_inherit_outgoing_load_or_invent_global_tasks(self):
        own, report = report_fixture("empty")
        for index, department in enumerate(report["departments"], start=1):
            department["outgoing"]["total"] = index * 100
            department["outgoing"]["statuses"]["new"] = index * 100
        with patch("preview.server.report_fixture", return_value=(own, report)):
            markup = Markup(self.client.get("/admin/statistics?role=admin").text)
        root = next(n for n in markup.nodes if "data-department-comparison" in n["attrs"])
        self.assertEqual(root["attrs"]["data-scale"], "0")
        rows = [n for n in markup.nodes if "data-compare-department" in n["attrs"]]
        self.assertEqual(len(rows), len(report["departments"]))
        for row in rows:
            for key in ("data-total", "data-active", "data-completed", "data-overdue"):
                self.assertEqual(row["attrs"][key], "0")
            segments = [n for n in markup.nodes if "data-compare-status" in n["attrs"] and inside(n, row)]
            self.assertEqual(len(segments), 5)
            for segment in segments:
                self.assertEqual(segment["attrs"]["data-count"], "0")
                size = re.search(r"--segment-size:\s*([0-9.]+)%", segment["attrs"].get("style", ""))
                self.assertIsNotNone(size)
                self.assertEqual(float(size.group(1)), 0)
        global_chart = next(n for n in markup.nodes if "data-chart-total" in n["attrs"])
        self.assertEqual(global_chart["attrs"]["data-chart-total"], "0")
