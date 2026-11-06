"""The oracle's statistics against textbook values.

Chi-square critical values are the standard table's. The log-rank and Kaplan-Meier cases are the
Freireich et al. (1963) 6-MP leukemia remission trial, as analysed in Kleinbaum and Klein,
"Survival Analysis: A Self-Learning Text" (chapter 2): log-rank chi-square 16.79 on 1 df,
expected events 19.25 (6-MP) and 10.75 (placebo), median remission 23 and 8 weeks.
"""

import math
import unittest

from orion_evals.stats import Observation, chi2_sf, kaplan_meier, km_median, logrank

SIX_MP_EVENTS = [6, 6, 6, 7, 10, 13, 16, 22, 23]
SIX_MP_CENSORED = [6, 9, 10, 11, 17, 19, 20, 25, 32, 32, 34, 35]
PLACEBO_EVENTS = [1, 1, 2, 2, 3, 4, 4, 5, 5, 8, 8, 8, 8, 11, 11, 12, 12, 15, 17, 22, 23]


def six_mp() -> list[Observation]:
    return [Observation(t, True) for t in SIX_MP_EVENTS] + [Observation(t, False) for t in SIX_MP_CENSORED]


def placebo() -> list[Observation]:
    return [Observation(t, True) for t in PLACEBO_EVENTS]


class ChiSquare(unittest.TestCase):
    def test_critical_values(self) -> None:
        table = [(1, 3.841458820694124, 0.05), (1, 6.634896601021214, 0.01), (2, 5.991464547107979, 0.05), (3, 7.814727903251179, 0.05), (5, 11.070497693516351, 0.05), (10, 18.307038053275146, 0.05), (10, 23.209251158954356, 0.01)]
        for df, x, p in table:
            with self.subTest(df=df, x=x):
                self.assertAlmostEqual(chi2_sf(x, df), p, places=10)

    def test_even_df_closed_form_both_branches(self) -> None:
        # For even df the tail is exp(-x/2) * sum_{k < df/2} (x/2)^k / k!.
        for df, x in ((4, 2.0), (4, 20.0), (6, 30.0), (8, 1.5)):
            with self.subTest(df=df, x=x):
                half = x / 2
                expected = math.exp(-half) * sum(half**k / math.factorial(k) for k in range(df // 2))
                self.assertAlmostEqual(chi2_sf(x, df) / expected, 1.0, places=10)

    def test_edges(self) -> None:
        self.assertEqual(chi2_sf(0.0, 1), 1.0)
        self.assertLess(chi2_sf(266.34, 1), 1e-58)
        with self.assertRaises(ValueError):
            chi2_sf(1.0, 0)


class LogRank(unittest.TestCase):
    def test_freireich_6mp(self) -> None:
        result = logrank([six_mp(), placebo()])
        self.assertEqual(result.observed, (9, 21))
        self.assertAlmostEqual(result.expected[0], 19.25, places=2)
        self.assertAlmostEqual(result.expected[1], 10.75, places=2)
        self.assertAlmostEqual(result.statistic, 16.79, places=2)
        self.assertEqual(result.df, 1)
        self.assertAlmostEqual(result.p_value, 4.17e-05, delta=0.01e-05)

    def test_identical_groups_give_p_one(self) -> None:
        result = logrank([placebo(), placebo()])
        self.assertAlmostEqual(result.statistic, 0.0, places=12)
        self.assertAlmostEqual(result.p_value, 1.0, places=12)

    def test_k_groups_do_not_depend_on_the_dropped_group(self) -> None:
        a, b = six_mp(), placebo()
        c = [Observation(t + 0.5, i % 3 != 0) for i, t in enumerate(range(2, 30, 2))]
        orders = [[a, b, c], [b, c, a], [c, a, b]]
        stats = [logrank(groups).statistic for groups in orders]
        self.assertEqual(logrank(orders[0]).df, 2)
        for value in stats[1:]:
            self.assertAlmostEqual(value, stats[0], places=9)

    def test_needs_two_groups(self) -> None:
        with self.assertRaises(ValueError):
            logrank([six_mp()])


class KaplanMeier(unittest.TestCase):
    def test_6mp_curve(self) -> None:
        curve = {p.time: round(p.survival, 3) for p in kaplan_meier(six_mp())}
        self.assertEqual(curve, {0.0: 1.0, 6: 0.857, 7: 0.807, 10: 0.753, 13: 0.69, 16: 0.627, 22: 0.538, 23: 0.448})

    def test_medians(self) -> None:
        self.assertEqual(km_median(six_mp()), 23)
        self.assertEqual(km_median(placebo()), 8)
        # At t = 6, two of three at risk survive: the estimate stays at 2/3 and the median is never reached.
        self.assertIsNone(km_median([Observation(5, False), Observation(6, True), Observation(9, False), Observation(10, False)]))
        self.assertEqual(km_median([Observation(5, False), Observation(6, True), Observation(9, False)]), 6)


if __name__ == "__main__":
    unittest.main()
