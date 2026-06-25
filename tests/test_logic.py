"""Tests de la logique pure : classement, départages FIFA, seuils, dates.
Aucun réseau, aucun Elo requis. Lancer : python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import senegal_wc_tracker as T  # noqa: E402


class TestTable(unittest.TestCase):
    def test_points_diff_buts(self):
        # A bat B 2-0, A perd 0-1 contre C, B bat C 3-1
        res = [("A", 2, "B", 0), ("C", 1, "A", 0), ("B", 3, "C", 1)]
        tbl = T.table_from(res, ["A", "B", "C"])
        self.assertEqual(tbl["A"][0], 3)          # 1 victoire
        self.assertEqual(tbl["A"][1], 2 - 0 + 0 - 1)   # diff = +1
        self.assertEqual(tbl["A"][2], 2)          # buts pour
        self.assertEqual(tbl["B"][0], 3)
        self.assertEqual(tbl["C"][0], 3)


class TestTiebreak(unittest.TestCase):
    def test_confrontation_directe(self):
        # A et B finissent à égalité (pts/diff/bp) mais A a battu B -> A devant B.
        # A-B: 2-1 | A-C: 0-1 | B-C: 1-0
        res = [("A", 2, "B", 1), ("A", 0, "C", 1), ("B", 1, "C", 0)]
        teams = ["A", "B", "C"]
        tbl = T.table_from(res, teams)
        # A et B : 3 pts, diff 0, 2 bp ; égalité stricte
        self.assertEqual(tuple(tbl["A"]), tuple(tbl["B"]))
        order, _ = T.rank_group(teams, res)
        self.assertEqual(order[0], "A")   # A départage B par la confrontation directe
        self.assertEqual(order[1], "B")
        self.assertEqual(order[2], "C")


class TestThresholds(unittest.TestCase):
    def test_franchissements(self):
        self.assertIn(25.0, T.crossed_threshold(20, 30))
        self.assertIn(50.0, T.crossed_threshold(60, 40))
        self.assertNotIn(75.0, T.crossed_threshold(20, 30))
        self.assertIn("ELIM", T.crossed_threshold(40.0, 0.0))
        self.assertIn("VIVANT", T.crossed_threshold(0.0, 40.0))


class TestKickoff(unittest.TestCase):
    def test_parse_utc(self):
        m = {"date": "2026-06-26", "time": "15:00 UTC-4"}
        ko = T.parse_kickoff(m)
        self.assertIsNotNone(ko)
        self.assertEqual(ko.hour, 19)            # 15h UTC-4 == 19h UTC
        self.assertEqual(ko.utcoffset().total_seconds(), 0)

    def test_parse_invalide(self):
        self.assertIsNone(T.parse_kickoff({"time": "15:00 UTC-4"}))  # pas de date


if __name__ == "__main__":
    unittest.main()
