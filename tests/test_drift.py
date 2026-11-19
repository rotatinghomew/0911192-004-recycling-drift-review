"""漂移追查报告的业务约束测试。"""
from datetime import date
from pathlib import Path
import unittest

from inspect_inputs import load_tables

import drift_report as dr


DATA = Path(__file__).resolve().parents[1] / "data"


class DriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = load_tables(DATA)
        cls.policy = dr.load_policy(DATA)
        cls.report = dr.analyze(cls.tables, cls.policy)

    def test_as_of_defaults_to_latest_activity(self):
        self.assertEqual(self.report.as_of, date(2026, 3, 15))

    def test_targeted_sample_stays_out_of_baseline(self):
        assessment = self.report.assessments["b6"]
        flag = assessment.flags[0]
        baseline_ids = {s.sample for s in flag.baseline}
        self.assertEqual(baseline_ids, {"s2", "s4"})
        self.assertNotIn("s7", baseline_ids)
        # 追加抽样只作该批处置证据
        self.assertEqual([s.sample for s, _ in assessment.targeted], ["s7"])

    def test_repeated_readings_count_as_one_sample(self):
        # 同一样本的新旧读数配成一对，不按读数条数累计
        pairs = self.report.shifts["A"].pairs
        self.assertEqual([p[0] for p in pairs], ["s1", "s3", "s5"])
        self.assertAlmostEqual(self.report.shifts["A"].diff,
                               (0.02 + 0.02 + 0.031) / 3)

    def test_conversion_refused_outside_common_support(self):
        shifts = self.report.shifts
        # B 级共同支持只有 old 0.090 / new 0.140 一个点
        self.assertIsNone(dr.convert(shifts, "B", 0.15, "new", "old"))
        self.assertIsNone(dr.convert(shifts, "B", 0.08, "old", "new"))
        self.assertAlmostEqual(
            dr.convert(shifts, "B", 0.14, "new", "old"), 0.09)
        # A 级支持范围内可以换算
        self.assertAlmostEqual(
            dr.convert(shifts, "A", 0.05, "old", "new"), 0.05 + shifts["A"].diff)
        self.assertIsNone(dr.convert(shifts, "A", 0.10, "old", "new"))

    def test_single_explanation_clears_b5(self):
        assessment = self.report.assessments["b5"]
        self.assertEqual(assessment.status, dr.EXPLAINED)
        method = assessment.flags[0].explanations[0]
        self.assertEqual(method.status, dr.STANDS)
        self.assertIn("a12", method.evidence)
        for expl in assessment.flags[0].explanations[1:]:
            self.assertEqual(expl.status, dr.RULED_OUT)

    def test_b6_keeps_multiple_explanations(self):
        assessment = self.report.assessments["b6"]
        self.assertEqual(assessment.status, dr.PENDING)
        statuses = {e.kind: e.status for e in assessment.flags[0].explanations}
        self.assertEqual(statuses["检测规程差异"], dr.UNKNOWN)
        self.assertEqual(statuses["线内变化"], dr.OPEN)
        self.assertEqual(statuses["来料构成"], dr.OPEN)

    def test_retest_plan_respects_retention_and_budget(self):
        plan = self.report.retests
        self.assertLessEqual(len(plan), self.policy.max_retests)
        self.assertEqual([i.sample for i in plan], ["s6", "s7"])
        self.assertTrue(all("old" in i.methods for i in plan))
        samples = {s.sample: s for s in self.report.samples}
        for item in plan:
            self.assertTrue(samples[item.sample].retained_on(self.report.as_of))
        # 已销毁的留样不进计划，销毁事实保留
        self.assertIsNotNone(samples["s2"].disposal)
        self.assertNotIn("s2", [i.sample for i in plan])

    def test_actual_decisions_are_not_rewritten(self):
        states = {h["shipment"]: h["state"] for h in self.report.holds}
        self.assertEqual(states,
                         {"ship1": "hold", "ship2": "hold", "ship3": "released"})
        advices = {a.shipment: a for a in self.report.advices}
        self.assertEqual(advices["ship3"].action, "维持实际放行")
        self.assertEqual(advices["ship1"].action, "交回正常复核")
        self.assertIn(dr.REASON_CALIBRATION, advices["ship1"].reason)
        self.assertIn("a12", advices["ship1"].reason)
        self.assertEqual(advices["ship2"].action, "继续暂缓")
        self.assertIn(dr.REASON_BOUNDARY, advices["ship2"].reason)

    def test_non_separable_blocks_independent_release(self):
        advices = {a.shipment: a for a in self.report.advices}
        seg = {s.batch: s for s in advices["ship2"].segments}
        self.assertFalse(seg["b4"].separable)
        self.assertIn("不能独立放行", seg["b4"].note)
        # ship1 整体交回复核，无需分段拣回
        self.assertEqual(advices["ship1"].action, "交回正常复核")

    def test_documented_range_attribution(self):
        assessment = self.report.assessments["b6"]
        feed = assessment.flags[0].explanations[2]
        # B 级试样只归属 i6 的单据范围，不指向 i5 的 A 段
        self.assertIn("i6", feed.detail)
        self.assertIn("500–750", feed.detail)
        self.assertIn("i5", feed.detail)
        self.assertIn("不指向", feed.detail)
        # 整批到料不都算给一条线
        b5_feed = self.report.assessments["b5"].flags[0].explanations[2]
        self.assertIn("900–1100", b5_feed.detail)
        self.assertNotIn("1500 kg 归属", b5_feed.detail)

    def test_untracked_intake_is_flagged(self):
        self.assertTrue(any("i2" in note for note in self.report.notes))

    def test_as_of_before_cross_reading_keeps_b5_pending(self):
        earlier = dr.analyze(self.tables, self.policy, date(2026, 3, 13))
        self.assertEqual(earlier.assessments["b5"].status, dr.PENDING)
        self.assertIn("s5", [i.sample for i in earlier.retests])
        advices = {a.shipment: a for a in earlier.advices}
        self.assertEqual(advices["ship1"].action, "继续暂缓")


if __name__ == "__main__":
    unittest.main()
