"""求解器自测：含与"逐分钟显式枚举"的小规模等价性对照。

枚举仅用于测试校验，产品代码（app/solver.py）不做逐分钟/逐 Wh 铺开。
暴力法逐分钟枚举每段的取值序列，汇总成"各 15 分钟窗电量向量"，
再用集合 DP 合并各段；对照用例段长 ≤10 分钟、上限 U≤3，规模可控。
"""
import itertools
import unittest

from app import solver


def brute_reachable_vectors(readings, upper):
    """逐分钟语义下，所有可行的各窗电量向量集合（独立于产品实现）。"""
    t0, _ = readings[0]
    t1, _ = readings[-1]
    n_minutes = t1 - t0
    nw = n_minutes // 15
    reachable = {tuple([0] * nw)}

    for (ta, va), (tb, vb) in zip(readings, readings[1:]):
        n = tb - ta
        d = vb - va
        if d < 0 or d > n * upper:
            return set()
        a = ta - t0
        contribs = set()
        for minutes in itertools.product(range(upper + 1), repeat=n):
            if sum(minutes) != d:
                continue
            vec = [0] * nw
            for k, val in enumerate(minutes):
                vec[(a + k) // 15] += val
            contribs.add(tuple(vec))
        if not contribs:
            return set()
        merged = set()
        for base in reachable:
            for c in contribs:
                merged.add(tuple(x + y for x, y in zip(base, c)))
        reachable = merged
    return reachable


def brute_audit(readings, upper, peak, target_window):
    vectors = brute_reachable_vectors(readings, upper)
    if not vectors:
        return "impossible"
    target_hit = any(v[target_window] == peak for v in vectors)
    if not target_hit:
        return "target_unreachable"
    if any(v[target_window] == peak
           and all(v[w] <= peak for w in range(len(v)) if w != target_window)
           for v in vectors):
        return "feasible"
    return "others_breach"


# 对照读数（分钟偏移, 累计 Wh）：段长 ≤10，覆盖窗边界穿越与缺读
FULL = [(0, 0), (5, 4), (10, 9), (15, 13), (20, 17), (25, 21), (30, 24)]
GAP = [(0, 0), (10, 8), (20, 16), (30, 21)]           # 每段都缺读
MIXED = [(0, 2), (8, 5), (15, 8), (25, 14), (30, 18)]  # 实读+缺读混合


class SolverTests(unittest.TestCase):
    def test_brute_equivalence_all_cases(self):
        for readings, upper in [(FULL, 2), (GAP, 2), (MIXED, 3)]:
            nw = (readings[-1][0] - readings[0][0]) // 15
            total = readings[-1][1] - readings[0][1]
            for tw in range(nw):
                for peak in range(0, min(upper * 15, total) + 1):
                    with self.subTest(readings=readings, peak=peak, tw=tw):
                        r = solver.audit(readings, upper, peak, tw)
                        expect = brute_audit(readings, upper, peak, tw)
                        self.assertEqual(r.status, expect)
                        if expect == "feasible":
                            self.assertEqual(sum(r.window_totals), total)
                            self.assertEqual(r.window_totals[tw], peak)
                            self.assertTrue(
                                all(v <= peak for v in r.window_totals))

    def test_others_breach_diagnostics(self):
        # GAP 各窗可在 [8,13] 间分配（总量 21）。报窗0=8 可达，
        # 此时窗1 被迫为 13，超过峰值 8 → others_breach，最小越限 5
        r = solver.audit(GAP, 2, 8, 0)
        self.assertEqual(r.status, "others_breach")
        self.assertIn(1, r.breach_windows)
        self.assertEqual(r.window_totals[0], 8)
        self.assertEqual(r.min_overflow, 5)
        self.assertEqual(
            r.min_overflow,
            sum(max(0, v - 8) for w, v in
                enumerate(r.window_totals) if w != 0))
        # 与暴力判定一致
        self.assertEqual(brute_audit(GAP, 2, 8, 0), "others_breach")

    def test_target_unreachable_reports_interval(self):
        # GAP 共 21Wh / U=2，报窗 0 一个高于其可达最大值的峰值
        pieces, seg_d = solver.build_pieces(GAP, 2, 2)
        tmax = solver.window_extreme(GAP, 2, 2, 0, pieces, seg_d, True)
        r = solver.audit(GAP, 2, tmax + 1, 0)
        self.assertEqual(r.status, "target_unreachable")
        self.assertEqual(r.target_max, tmax)
        tmin = solver.window_extreme(GAP, 2, 2, 0, pieces, seg_d, False)
        self.assertEqual(r.target_min, tmin)
        # 暴力法验证可达区间端点
        vectors = brute_reachable_vectors(GAP, 2)
        self.assertEqual(tmin, min(v[0] for v in vectors))
        self.assertEqual(tmax, max(v[0] for v in vectors))

    def test_feasible_totals_sum_matches_readings(self):
        r = solver.audit(MIXED, 3, 10, 1)
        self.assertEqual(r.status, "feasible")
        self.assertEqual(sum(r.window_totals),
                         MIXED[-1][1] - MIXED[0][1])

    def test_piece_count_is_segment_window_order(self):
        # 45 分钟缺读段：切块数只与段/窗边界有关，不随分钟数、Wh 数膨胀
        readings = [(0, 0), (45, 30), (60, 42)]
        pieces, _ = solver.build_pieces(readings, 2, 4)
        self.assertLessEqual(len(pieces), 3 + 4)
        for p in pieces:
            self.assertGreaterEqual(p.lower, 0)
            self.assertLessEqual(p.upper, 2 * p.n)

    def test_period_validation(self):
        with self.assertRaises(solver.SolverError):
            solver.audit([(1, 0), (16, 5)], 5, 5, 0)          # 未对齐
        with self.assertRaises(solver.SolverError):
            solver.audit([(0, 10), (15, 5)], 5, 5, 0)         # 回卷
        with self.assertRaises(solver.SolverError):
            solver.audit([(0, 0), (32 * 1440, 1)], 5, 1, 0)   # 超 31 天

    def test_segment_exceeds_cap(self):
        with self.assertRaises(solver.SolverError):
            solver.audit([(0, 0), (15, 40)], 2, 10, 0)        # 最多 30

    def test_large_period_no_minute_explosion(self):
        # 31 天、仅首尾两个读数：切块数 = 窗数（2976），与 44640 分钟无关
        readings = [(0, 0), (31 * 1440, 31 * 1440 * 2)]
        pieces, _ = solver.build_pieces(readings, 5, 31 * 96)
        self.assertEqual(len(pieces), 31 * 96)
        r = solver.audit(readings, 5, 30, 100)
        self.assertIn(r.status,
                      ("feasible", "target_unreachable", "others_breach"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
