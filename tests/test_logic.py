"""Tests de la logique pure : classement, départages FIFA, seuils, dates.
Aucun réseau, aucun Elo requis. Lancer : python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timezone

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


class TestNextMatch(unittest.TestCase):
    """Le « prochain match » est le plus tôt au coup d'envoi, pas le premier du fichier."""

    @staticmethod
    def _m(t1, t2, day):
        ko = None if day is None else datetime(2026, 6, day, 18, tzinfo=timezone.utc)
        return {"group": "Group A", "team1": t1, "team2": t2, "kickoff": ko}

    def test_ordre_fichier_non_chronologique(self):
        rem = [self._m("X", "Y", 11),
               self._m(T.TEAM, "B", 20),     # match de l'équipe, mais plus tard
               self._m("A", T.TEAM, 14)]     # celui-ci vient en premier
        self.assertEqual(T.next_team_match_index(rem), 2)
        self.assertIs(T.next_senegal_match(rem), rem[2])

    def test_horaire_inconnu_en_dernier(self):
        rem = [self._m(T.TEAM, "B", None), self._m("A", T.TEAM, 14)]
        self.assertEqual(T.next_team_match_index(rem), 1)

    def test_aucun_match(self):
        rem = [self._m("X", "Y", 11)]
        self.assertIsNone(T.next_team_match_index(rem))
        self.assertIsNone(T.next_senegal_match(rem))


class TestSimulateOnceOutcome(unittest.TestCase):
    """L'issue rapportée est celle du match suivi, pas du dernier match simulé."""

    def test_issue_du_match_suivi(self):
        teams = {"Group A": {T.TEAM, "B", "C", "D"}}
        played = {"Group A": []}
        rem = [
            {"group": "Group A", "team1": T.TEAM, "team2": "B", "kickoff": None},  # suivi
            {"group": "Group A", "team1": "C", "team2": "D", "kickoff": None},
            {"group": "Group A", "team1": "C", "team2": T.TEAM, "kickoff": None},  # plus tard
        ]
        scores = {(T.TEAM, "B"): (3, 0), ("C", "D"): (1, 1), ("C", T.TEAM): (5, 0)}

        def fake_sim(m, overlay):
            g1, g2 = scores[(m["team1"], m["team2"])]
            return m["team1"], g1, m["team2"], g2

        real = T.sim_match
        T.sim_match = fake_sim
        try:
            _, outcome, margin, _ = T.simulate_once(
                played, teams, rem, {}, set(), track_pos=0, sen_group="Group A")
        finally:
            T.sim_match = real
        # Suivi = victoire 3-0 ; l'ancien code renvoyait la défaite 0-5 du 3e match.
        self.assertEqual(outcome, "V")
        self.assertEqual(margin, 3)

    def test_aucun_match_suivi(self):
        teams = {"Group A": {T.TEAM, "B", "C", "D"}}
        played = {"Group A": [(T.TEAM, 1, "B", 0), ("C", 0, "D", 0)]}
        rem = []
        _, outcome, margin, _ = T.simulate_once(
            played, teams, rem, {}, set(), track_pos=None, sen_group="Group A")
        self.assertIsNone(outcome)
        self.assertEqual(margin, 0)


class TestFmtOutcomes(unittest.TestCase):
    """Le message ne doit pas affirmer une élimination qu'il n'a pas calculée."""

    def test_nul_encore_qualifiant(self):
        line = T.fmt_outcomes({"V": 92.0, "N": 66.0, "D": 43.0})
        self.assertIn("~92%", line)
        self.assertIn("Nul : ~66%", line)
        self.assertIn("Défaite : ~43%", line)
        self.assertNotIn("éliminé", line)

    def test_nul_et_defaite_eliminatoires(self):
        line = T.fmt_outcomes({"V": 88.0, "N": 0.0, "D": 0.0})
        self.assertIn("Nul ou défaite : éliminé.", line)

    def test_echantillon_insuffisant(self):
        line = T.fmt_outcomes({"V": 88.0, "N": None, "D": None})
        self.assertEqual(line, "Si victoire : ~88% de qualif.")
        self.assertEqual(T.fmt_outcomes({"V": None, "N": None, "D": None}), "")


class TestKickoffOffsets(unittest.TestCase):
    def test_offset_demi_heure(self):
        ko = T.parse_kickoff({"date": "2026-06-26", "time": "15:00 UTC+5:30"})
        self.assertIsNotNone(ko)
        self.assertEqual((ko.hour, ko.minute), (9, 30))

    def test_offset_negatif_demi_heure(self):
        ko = T.parse_kickoff({"date": "2026-06-26", "time": "15:00 UTC-3:30"})
        self.assertEqual((ko.hour, ko.minute), (18, 30))

    def test_offset_positif_entier(self):
        ko = T.parse_kickoff({"date": "2026-06-26", "time": "15:00 UTC+2"})
        self.assertEqual(ko.hour, 13)


if __name__ == "__main__":
    unittest.main()
