#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
senegal_wc_tracker.py — Suivi temps réel des chances de qualification du Sénégal
pour les 16es de finale (Round of 32) de la Coupe du Monde 2026.

VARIABLES INTÉGRÉES
  - Elo réels par sélection (eloratings.net, 48 équipes, cache quotidien,
    fallback embarqué). Détection auto du code pays + dernière cote notée.
  - Avantage hôte : USA / Canada / Mexique jouent à domicile (+HOST_ADV Elo).
  - Modèle de buts Poisson BIVARIÉ (corrélation des scores via composante
    partagée) -> taux de nuls réaliste.
  - Départages FIFA complets :
      * Groupe : points -> diff -> buts pour -> confrontation directe
        (points/diff/bp entre ex æquo) -> tirage au sort (aléatoire par sim).
      * Meilleurs 3es : points -> diff -> buts pour -> tirage au sort.
  - Règle 2026 : 2 premiers de chaque groupe + 8 meilleurs 3es sur 12 groupes.
  - Overlay live optionnel (API-Football) : scores des matchs EN COURS, le reste
    du match simulé au prorata du temps restant. Sans clé -> mode après-match.

NON intégré (assumé, second ordre) : départage fair-play (cartons absents
d'openfootball -> remplacé par tirage au sort, qui est l'étape FIFA suivante) ;
rotation/turnover des équipes déjà qualifiées ; altitude.

Source données matchs : openfootball/worldcup.json (libre, sans clé).
Notifications : Telegram Bot API, sur Δ≥5 pts, seuils 25/50/75 %,
bascule qualifiable/éliminé, fin du match suivi.

Configuration (variables d'environnement) :
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  -> notifications Telegram (sinon : log seul)
  FOOTBALL_API_KEY                      -> overlay live api-sports.io (sinon : après-match)
  WC_TRACK_TEAM   (défaut "Senegal")    -> équipe suivie
  WC_TRACKER_HOME (défaut ./data)       -> dossier state/logs

Usage :
  python3 senegal_wc_tracker.py            # un cycle (throttle auto)
  python3 senegal_wc_tracker.py --dry-run  # calcule + affiche, n'envoie rien
  python3 senegal_wc_tracker.py --force     # recalcule et notifie
  python3 senegal_wc_tracker.py --refresh-elo  # force le rafraîchissement Elo
  python3 senegal_wc_tracker.py --sims 20000
"""

import os
import sys
import json
import math
import time
import random
import argparse
import subprocess
import urllib.request
import urllib.parse
from collections import Counter
from datetime import datetime, timezone, timedelta

# ----------------------------------------------------------------------------
# Chemins / constantes
# ----------------------------------------------------------------------------
BASE = os.environ.get("WC_TRACKER_HOME") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
STATE_DIR = os.path.join(BASE, "state")
LOG_DIR = os.path.join(BASE, "logs")
STATE_FILE = os.path.join(STATE_DIR, "senegal_wc.json")
ELO_CACHE = os.path.join(STATE_DIR, "elo_ratings.json")
OPENFOOTBALL_URL = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"

TEAM = os.environ.get("WC_TRACK_TEAM", "Senegal")  # équipe suivie (paramétrable)
N_BEST_THIRDS = 8
DEFAULT_SIMS = 12000
PIVOTAL_MIN_IMPACT = 4.0   # n'afficher un match "à surveiller" qu'au-dessus de X pts d'impact
MIN_CONDITIONAL_SAMPLE = 30   # sous ce nombre de tirages, une proba conditionnelle n'est pas affichée

DELTA_PCT_TRIGGER = 5.0
THRESHOLDS = [25.0, 50.0, 75.0]

HEAVY_THROTTLE_IDLE_S = 1800
MATCH_WINDOW_PRE_MIN = 15
MATCH_WINDOW_POST_MIN = 150
LIVE_MIN_INTERVAL_S = 170   # appel API live au plus toutes les ~3 min (quota 100/j)

# Modèle de buts
MU_TOTAL = 2.55
SUP_DIV = 220.0
LAMBDA_FLOOR = 0.18
COVAR = 0.10           # composante partagée (corrélation des scores)
HOST_ADV = 80          # avantage Elo pour une nation hôte à domicile
HOSTS = {"USA", "Canada", "Mexico"}

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ----------------------------------------------------------------------------
# Elo : carte noms openfootball -> fichiers eloratings.net + fallback embarqué
# (fallback = relevé réel du 25/06/2026, sert si le réseau eloratings tombe)
# ----------------------------------------------------------------------------
ELO_FILE = {
    "Mexico": "Mexico", "South Korea": "South_Korea", "Czech Republic": "Czechia",
    "South Africa": "South_Africa", "Switzerland": "Switzerland", "Canada": "Canada",
    "Qatar": "Qatar", "Bosnia & Herzegovina": "Bosnia_and_Herzegovina", "Brazil": "Brazil",
    "Morocco": "Morocco", "Scotland": "Scotland", "Haiti": "Haiti", "USA": "United_States",
    "Paraguay": "Paraguay", "Turkey": "Turkey", "Australia": "Australia", "Germany": "Germany",
    "Ecuador": "Ecuador", "Ivory Coast": "Ivory_Coast", "Curaçao": "Curacao",
    "Netherlands": "Netherlands", "Japan": "Japan", "Sweden": "Sweden", "Tunisia": "Tunisia",
    "Belgium": "Belgium", "Iran": "Iran", "Egypt": "Egypt", "New Zealand": "New_Zealand",
    "Spain": "Spain", "Uruguay": "Uruguay", "Saudi Arabia": "Saudi_Arabia",
    "Cape Verde": "Cape_Verde", "France": "France", "Norway": "Norway", "Senegal": "Senegal",
    "Iraq": "Iraq", "Argentina": "Argentina", "Austria": "Austria", "Algeria": "Algeria",
    "Jordan": "Jordan", "Colombia": "Colombia", "DR Congo": "DR_Congo", "Portugal": "Portugal",
    "Uzbekistan": "Uzbekistan", "England": "England", "Croatia": "Croatia", "Ghana": "Ghana",
    "Panama": "Panama",
}
ELO_FALLBACK = {
    "Argentina": 2144, "Spain": 2134, "France": 2090, "England": 2028, "Brazil": 2009,
    "Colombia": 2006, "Portugal": 1988, "Netherlands": 1972, "Germany": 1954, "Norway": 1951,
    "Japan": 1925, "Switzerland": 1914, "Mexico": 1912, "Croatia": 1896, "Morocco": 1877,
    "Belgium": 1869, "Ecuador": 1864, "Uruguay": 1851, "Czech Republic": 1843, "Austria": 1841,
    "USA": 1820, "Senegal": 1817, "Paraguay": 1816, "Turkey": 1813, "Australia": 1799,
    "Algeria": 1780, "Iran": 1766, "Canada": 1748, "Scotland": 1745, "Egypt": 1740,
    "Ivory Coast": 1728, "Sweden": 1727, "South Korea": 1723, "Uzbekistan": 1677,
    "Panama": 1668, "DR Congo": 1666, "Jordan": 1632, "Cape Verde": 1625,
    "Bosnia & Herzegovina": 1622, "Saudi Arabia": 1593, "Iraq": 1586, "Ghana": 1584,
    "South Africa": 1575, "Tunisia": 1570, "New Zealand": 1549, "Haiti": 1517,
    "Curaçao": 1453, "Qatar": 1411,
}

LIVE_NAME_MAP = {
    "Korea Republic": "South Korea", "United States": "USA", "IR Iran": "Iran",
    "Côte d'Ivoire": "Ivory Coast", "Cote d'Ivoire": "Ivory Coast", "Czechia": "Czech Republic",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina", "Turkiye": "Turkey", "Türkiye": "Turkey",
    "Cape Verde Islands": "Cape Verde", "Curacao": "Curaçao", "Congo DR": "DR Congo",
}

ELO = {}  # rempli par load_elo()


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "senegal_wc.log"), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Elo : récupération + cache
# ----------------------------------------------------------------------------
def _fetch_one_elo(fname):
    url = f"https://www.eloratings.net/{fname}.tsv"
    req = urllib.request.Request(url, headers={"User-Agent": "mozilla"})
    raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    rows = [l.split("\t") for l in raw.strip().split("\n")
            if l.strip() and len(l.split("\t")) >= 12]
    cnt = Counter()
    for r in rows:
        cnt[r[3]] += 1
        cnt[r[4]] += 1
    code = cnt.most_common(1)[0][0]
    for last in reversed(rows):
        if code not in (last[3], last[4]):
            continue
        col = last[10] if last[3] == code else last[11]
        v = col.replace("−", "-")
        if v.lstrip("-").isdigit():
            return int(v)
    raise ValueError("aucune ligne notée")


def refresh_elo():
    """Récupère les 48 Elo depuis eloratings.net. Renvoie dict ou lève."""
    out = {}
    for team, fname in ELO_FILE.items():
        out[team] = _fetch_one_elo(fname)
    return out


def load_elo(force_refresh=False, allow_network=True):
    """Charge les Elo : cache du jour > rafraîchissement réseau > fallback."""
    today = datetime.now().strftime("%Y-%m-%d")
    cache = {}
    if os.path.exists(ELO_CACHE):
        try:
            cache = json.load(open(ELO_CACHE))
        except (json.JSONDecodeError, OSError):
            cache = {}
    if not force_refresh and cache.get("_date") == today:
        return {k: v for k, v in cache.items() if not k.startswith("_")}
    if allow_network or force_refresh:
        try:
            fresh = refresh_elo()
            fresh["_date"] = today
            json.dump(fresh, open(ELO_CACHE, "w"), ensure_ascii=False, indent=0)
            log(f"Elo rafraîchis ({len(fresh)-1} équipes) depuis eloratings.net")
            return {k: v for k, v in fresh.items() if not k.startswith("_")}
        except Exception as e:
            log(f"WARN rafraîchissement Elo échoué ({e}) — repli cache/fallback")
    if cache:
        return {k: v for k, v in cache.items() if not k.startswith("_")}
    return dict(ELO_FALLBACK)


def elo(team):
    if team not in ELO:
        log(f"WARN Elo manquant '{team}', fallback {ELO_FALLBACK.get(team, 1600)}")
        return ELO_FALLBACK.get(team, 1600)
    return ELO[team]


def eff_elo(team):
    return elo(team) + (HOST_ADV if team in HOSTS else 0)


# ----------------------------------------------------------------------------
# Données matchs (openfootball) + overlay live
# ----------------------------------------------------------------------------
def fetch_openfootball():
    req = urllib.request.Request(OPENFOOTBALL_URL, headers={"User-Agent": "pdc-senegal-tracker"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def parse_kickoff(m):
    d, t = m.get("date"), m.get("time", "")
    if not d:
        return None
    try:
        hh, mm, off_min = 12, 0, 0
        if t:
            parts = t.split()
            hh, mm = int(parts[0].split(":")[0]), int(parts[0].split(":")[1])
            if len(parts) > 1 and parts[1].upper().startswith("UTC"):
                raw = parts[1].upper().replace("UTC", "").strip()
                if raw:
                    sign = -1 if raw.startswith("-") else 1
                    oh, _, om = raw.lstrip("+-").partition(":")
                    off_min = sign * (int(oh) * 60 + int(om or 0))
        local = datetime(int(d[:4]), int(d[5:7]), int(d[8:10]), hh, mm)
        return (local - timedelta(minutes=off_min)).replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def build_world(data):
    """Renvoie : played_by_group {g:[(t1,g1,t2,g2)]}, teams_by_group {g:set},
    remaining [{group,team1,team2,kickoff}]."""
    played = {}
    teams = {}
    remaining = []
    for m in data["matches"]:
        g = m.get("group")
        if not g or not g.startswith("Group"):
            continue
        t1, t2 = m.get("team1"), m.get("team2")
        teams.setdefault(g, set()).update([t1, t2])
        played.setdefault(g, [])
        sc = (m.get("score") or {}).get("ft")
        if sc:
            played[g].append((t1, sc[0], t2, sc[1]))
        else:
            remaining.append({"group": g, "team1": t1, "team2": t2, "kickoff": parse_kickoff(m)})
    return played, teams, remaining


def get_api_key():
    """Clé api-sports.io : variable d'environnement d'abord, Trousseau macOS en
    secours (optionnel). Vide -> mode après-match."""
    k = os.environ.get("FOOTBALL_API_KEY", "").strip()
    if k:
        return k
    try:  # secours macOS : security find-generic-password -s FOOTBALL_API_KEY -a api-football
        return subprocess.check_output(
            ["security", "find-generic-password", "-s", "FOOTBALL_API_KEY",
             "-a", "api-football", "-w"], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""


def fetch_live_overlay():
    key = get_api_key()
    if not key:
        return {}
    try:
        req = urllib.request.Request(
            "https://v3.football.api-sports.io/fixtures?live=all",
            headers={"x-apisports-key": key, "User-Agent": "pdc-senegal-tracker"})
        data = json.load(urllib.request.urlopen(req, timeout=20))
        overlay = {}
        for fx in data.get("response", []):
            home = LIVE_NAME_MAP.get(fx["teams"]["home"]["name"], fx["teams"]["home"]["name"])
            away = LIVE_NAME_MAP.get(fx["teams"]["away"]["name"], fx["teams"]["away"]["name"])
            gh, ga = fx["goals"]["home"] or 0, fx["goals"]["away"] or 0
            minute = fx["fixture"]["status"].get("elapsed") or 0
            overlay[frozenset((home, away))] = (home, gh, away, ga, minute)
        if overlay:
            log(f"Overlay live : {len(overlay)} match(s) en cours")
        return overlay
    except Exception as e:
        log(f"WARN overlay live indisponible : {e}")
        return {}


# ----------------------------------------------------------------------------
# Modèle de match (Poisson bivarié + avantage hôte + score live partiel)
# ----------------------------------------------------------------------------
def lambdas(t1, t2):
    sup = (eff_elo(t1) - eff_elo(t2)) / SUP_DIV
    return (max(LAMBDA_FLOOR, (MU_TOTAL + sup) / 2.0),
            max(LAMBDA_FLOOR, (MU_TOTAL - sup) / 2.0))


def pois(lmbda):
    if lmbda <= 0:
        return 0
    L, k, p = math.exp(-lmbda), 0, 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


def sim_match(m, overlay):
    t1, t2 = m["team1"], m["team2"]
    l1, l2 = lambdas(t1, t2)
    base1 = base2 = 0
    frac = 1.0
    key = frozenset((t1, t2))
    if key in overlay:
        h, gh, a, ga, minute = overlay[key]
        base1, base2 = (gh, ga) if h == t1 else (ga, gh)
        frac = max(0.0, (90 - min(minute, 90)) / 90.0)
    l1f, l2f = l1 * frac, l2 * frac
    l3 = min(COVAR * frac, 0.8 * min(l1f, l2f)) if frac > 0 else 0.0
    z = pois(l3)
    g1 = base1 + pois(l1f - l3) + z
    g2 = base2 + pois(l2f - l3) + z
    return t1, g1, t2, g2


# ----------------------------------------------------------------------------
# Classement + départages FIFA
# ----------------------------------------------------------------------------
def table_from(results, teams):
    tbl = {t: [0, 0, 0] for t in teams}  # pts, diff, bp
    for t1, g1, t2, g2 in results:
        if t1 not in tbl or t2 not in tbl:
            continue
        tbl[t1][1] += g1 - g2; tbl[t1][2] += g1
        tbl[t2][1] += g2 - g1; tbl[t2][2] += g2
        if g1 > g2:
            tbl[t1][0] += 3
        elif g2 > g1:
            tbl[t2][0] += 3
        else:
            tbl[t1][0] += 1; tbl[t2][0] += 1
    return tbl


def break_tie(tie, results):
    """Confrontation directe entre ex æquo, puis tirage au sort (aléatoire)."""
    S = set(tie)
    h2h = table_from([r for r in results if r[0] in S and r[2] in S], tie)
    return sorted(tie, key=lambda t: (h2h[t][0], h2h[t][1], h2h[t][2], random.random()),
                  reverse=True)


def rank_group(teams, results):
    tbl = table_from(results, teams)
    order = sorted(teams, key=lambda t: (tbl[t][0], tbl[t][1], tbl[t][2]), reverse=True)
    final, i = [], 0
    while i < len(order):
        j = i
        ki = tuple(tbl[order[i]])
        while j + 1 < len(order) and tuple(tbl[order[j + 1]]) == ki:
            j += 1
        tie = order[i:j + 1]
        final.extend(tie if len(tie) == 1 else break_tie(tie, results))
        i = j + 1
    return final, tbl


def simulate_once(played, teams, remaining, overlay, other_pos, track_pos, sen_group):
    """Un tirage. Retourne (qualifié, issue, marge, [(pos, '1'|'X'|'2')]) où `issue`
    vaut 'V'/'N'/'D' pour le PROCHAIN match de l'équipe suivie (None s'il n'y en a
    plus) et `marge` sa différence de buts, vue de l'équipe suivie."""
    results = {g: list(played[g]) for g in played}
    outcome = None
    margin = 0
    outs = []
    for pos, m in enumerate(remaining):
        t1, g1, t2, g2 = sim_match(m, overlay)
        results[m["group"]].append((t1, g1, t2, g2))
        if pos == track_pos:
            margin = (g1 - g2) if t1 == TEAM else (g2 - g1)
            outcome = 'V' if margin > 0 else ('N' if margin == 0 else 'D')
        elif pos in other_pos:
            outs.append((pos, '1' if g1 > g2 else ('2' if g2 > g1 else 'X')))

    thirds = []
    sen_top2 = False
    for g in teams:
        order, tbl = rank_group(teams[g], results[g])
        if g == sen_group and TEAM in order[:2]:
            sen_top2 = True
        t3 = order[2]
        thirds.append((tbl[t3][0], tbl[t3][1], tbl[t3][2], random.random(), t3))

    thirds.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    best8 = {x[4] for x in thirds[:N_BEST_THIRDS]}
    return (sen_top2 or TEAM in best8), outcome, margin, outs


def run_monte_carlo(played, teams, remaining, overlay, n):
    """Renvoie un dict : proba, P(victoire), P(qualif|issue) pour V/N/D, scénarios
    par marge, et matchs pivots d'autres groupes (analyse de sensibilité)."""
    sen_group = next(g for g in teams if TEAM in teams[g])
    other = [(i, m) for i, m in enumerate(remaining)
             if TEAM not in (m["team1"], m["team2"]) and m["group"] != sen_group]
    other_pos = {i for i, _ in other}
    track_pos = next_team_match_index(remaining)

    q = 0
    otot = {'V': 0, 'N': 0, 'D': 0}; oq = {'V': 0, 'N': 0, 'D': 0}   # issues du prochain match
    mtot = {1: 0, 2: 0, 3: 0}; mq = {1: 0, 2: 0, 3: 0}        # scénarios par marge
    ptot = {i: {'1': 0, 'X': 0, '2': 0} for i in other_pos}    # sensibilité par match
    pq = {i: {'1': 0, 'X': 0, '2': 0} for i in other_pos}

    for _ in range(n):
        ok, oc_team, mg, outs = simulate_once(
            played, teams, remaining, overlay, other_pos, track_pos, sen_group)
        q += ok
        if oc_team:
            otot[oc_team] += 1; oq[oc_team] += ok
        if oc_team == 'V':
            b = 3 if mg >= 3 else mg
            mtot[b] += 1; mq[b] += ok
        for pos, oc in outs:
            ptot[pos][oc] += 1
            if ok:
                pq[pos][oc] += 1

    scenarios = {b: (100.0 * mq[b] / mtot[b] if mtot[b] >= MIN_CONDITIONAL_SAMPLE else None)
                 for b in (1, 2, 3)}
    p_q_given = {o: (100.0 * oq[o] / otot[o] if otot[o] >= MIN_CONDITIONAL_SAMPLE else None)
                 for o in 'VND'}
    pivotal = []
    minn = max(40, int(0.015 * n))
    for i, m in other:
        ps = {o: 100.0 * pq[i][o] / ptot[i][o] for o in '1X2' if ptot[i][o] >= minn}
        if len(ps) >= 2:
            best = max(ps, key=ps.get); worst = min(ps, key=ps.get)
            pivotal.append({"m": m, "impact": ps[best] - ps[worst], "best": best,
                            "best_p": ps[best], "worst_p": ps[worst]})
    pivotal.sort(key=lambda x: -x["impact"])
    return {"prob": 100.0 * q / n, "p_win": 100.0 * otot['V'] / n,
            "p_q_given": p_q_given,
            "p_q_given_win": p_q_given['V'] if p_q_given['V'] is not None else 0.0,
            "scenarios": scenarios, "pivotal": pivotal}


# ----------------------------------------------------------------------------
# État + notifications
# ----------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(s):
    json.dump(s, open(STATE_FILE, "w"), indent=2, ensure_ascii=False)


def world_signature(played, overlay):
    parts = []
    for g in sorted(played):
        for (t1, g1, t2, g2) in played[g]:
            parts.append(f"{g}:{t1}{g1}-{g2}{t2}")
    for v in sorted(f"{x[0]}{x[1]}-{x[3]}{x[2]}:{x[4]}" for x in overlay.values()):
        parts.append(v)
    return "|".join(parts)


def relevant_window(remaining, overlay):
    if overlay:
        return True
    now = datetime.now(timezone.utc)
    for m in remaining:
        ko = m["kickoff"]
        if ko and (ko - timedelta(minutes=MATCH_WINDOW_PRE_MIN)) <= now <= (ko + timedelta(minutes=MATCH_WINDOW_POST_MIN)):
            return True
    return False


def crossed_threshold(old, new):
    out = []
    for th in THRESHOLDS:
        if (old < th <= new) or (new < th <= old):
            out.append(th)
    if old > 0.5 >= new:
        out.append("ELIM")
    if old <= 0.5 < new:
        out.append("VIVANT")
    return out


def send_telegram(msg):
    """Envoi via l'API Bot Telegram (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).
    Sans ces variables : on logue seulement (utile pour --dry-run / tests)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        log("Telegram non configuré (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID absents) — message non envoyé :\n" + msg)
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        resp = json.load(urllib.request.urlopen(req, timeout=20))
        if resp.get("ok"):
            log("Telegram envoyé")
            return True
        log(f"Telegram échec : {resp.get('description')}")
    except Exception as e:
        log(f"Telegram exception : {e}")
    return False


def notify_error(e):
    """Alerte Telegram en cas de panne, avec garde anti-spam de 30 min."""
    cd = os.path.join(STATE_DIR, ".last_error_notif")
    now = time.time()
    try:
        if os.path.exists(cd) and (now - os.path.getmtime(cd)) < 1800:
            return
    except OSError:
        pass
    send_telegram(f"⚠️ Agent {TEAM} (Mondial) — panne : {type(e).__name__}: {str(e)[:180]}")
    try:
        open(cd, "w").close()
    except OSError:
        pass


def next_team_match_index(remaining):
    """Index dans `remaining` du prochain match de l'équipe suivie (le plus tôt au
    coup d'envoi ; les matchs sans horaire connu passent en dernier). None si aucun."""
    cands = [(i, m) for i, m in enumerate(remaining) if TEAM in (m["team1"], m["team2"])]
    if not cands:
        return None
    far = datetime.max.replace(tzinfo=timezone.utc)
    return min(cands, key=lambda im: (im[1]["kickoff"] or far, im[0]))[0]


def next_senegal_match(remaining):
    i = next_team_match_index(remaining)
    return None if i is None else remaining[i]


def fmt_outcomes(p_q_given):
    """Ligne « si victoire / nul / défaite », calculée et non supposée.
    Si nul ET défaite mènent tous deux à l'élimination, on le dit d'un trait."""
    v, nul, dfa = (p_q_given.get(o) for o in 'VND')
    out = []
    if v is not None:
        out.append(f"Si victoire : ~{v:.0f}% de qualif.")
    if nul is not None and dfa is not None and nul < 0.5 and dfa < 0.5:
        out.append("Nul ou défaite : éliminé.")
    else:
        rest = [(lab, p) for lab, p in (("Nul", nul), ("Défaite", dfa)) if p is not None]
        if rest:
            out.append(" ".join(f"{lab} : ~{p:.0f}%." for lab, p in rest))
    return " ".join(out)


def fmt_message(mc, delta, played, teams, remaining, reasons):
    prob = mc["prob"]
    sen_group = next(g for g in teams if TEAM in teams[g])
    order, tbl = rank_group(teams[sen_group], played[sen_group])
    pos = next((i for i, t in enumerate(order, 1) if t == TEAM), 4)
    st = tbl[TEAM]
    flag = "🇸🇳 " if TEAM == "Senegal" else ""
    arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
    msg = f"{flag}{TEAM} — Mondial 2026 (16es)\n"
    msg += f"Proba qualification : {prob:.0f}%  {arrow}{abs(delta):.0f} pt\n"
    msg += f"Groupe {sen_group[-1]} : {pos}e — {st[0]} pts, diff {st[1]:+d}, {st[2]} bp\n"
    sm = next_senegal_match(remaining)
    if sm:
        ko = sm["kickoff"]
        when = ko.astimezone().strftime("%a %d/%m %Hh%M") if ko else "à venir"
        opp = sm["team2"] if sm["team1"] == TEAM else sm["team1"]
        msg += f"\nReste : {TEAM} – {opp} ({when})\n"
        line = fmt_outcomes(mc["p_q_given"])
        if line:
            msg += line + "\n"
        labels = {1: "1 but", 2: "2 buts", 3: "3+ buts"}
        parts = [f"{labels[b]} {mc['scenarios'][b]:.0f}%" for b in (1, 2, 3)
                 if mc["scenarios"].get(b) is not None]
        if parts:
            msg += "  ↳ marge : " + " · ".join(parts) + "\n"
    else:
        if prob < 0.5:
            msg += f"\n{TEAM} éliminé.\n"
        elif prob > 99.5:
            msg += f"\n{TEAM} qualifié.\n"
        else:
            msg += "\nSort suspendu aux autres groupes.\n"
    piv = [p for p in mc["pivotal"] if p["impact"] >= PIVOTAL_MIN_IMPACT][:2]
    if piv:
        msg += "\nÀ surveiller ailleurs :\n"
        for p in piv:
            m = p["m"]
            fav = "un nul" if p["best"] == "X" else "victoire " + (
                m["team1"] if p["best"] == "1" else m["team2"])
            msg += f"• {m['team1']}–{m['team2']} : {fav} t'aide ({p['worst_p']:.0f}→{p['best_p']:.0f}%)\n"
    if reasons:
        msg += "\n" + " · ".join(reasons)
    msg += f"\n🕐 Relevé : {datetime.now().strftime('%d/%m à %Hh%M')}"
    return msg.strip()


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--refresh-elo", action="store_true")
    ap.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    args = ap.parse_args()

    global ELO
    state = load_state()
    now_ts = time.time()

    try:
        data = fetch_openfootball()
    except Exception as e:
        log(f"ERREUR fetch openfootball : {e}")
        notify_error(e)
        sys.exit(1)

    played, teams, remaining = build_world(data)
    in_window = relevant_window(remaining, {})   # gate calendrier, sans appel API
    key_present = bool(get_api_key())
    log(f"Clé live API : {'présente' if key_present else 'absente (mode après-match)'}")

    # Appel API live UNIQUEMENT en fenêtre de match + intervalle mini (quota 100/j)
    overlay = {}
    if in_window and key_present and (
            args.force or (now_ts - state.get("last_live_fetch_ts", 0)) >= LIVE_MIN_INTERVAL_S):
        overlay = fetch_live_overlay()
        state["last_live_fetch_ts"] = now_ts

    sig = world_signature(played, overlay)
    if not args.force and not in_window and sig == state.get("last_signature") \
            and (now_ts - state.get("last_heavy_run_ts", 0)) < HEAVY_THROTTLE_IDLE_S:
        log("Throttle : rien de neuf, pas de recalcul.")
        return

    # Elo : pas de fetch réseau lourd pendant une fenêtre de match si on a déjà un cache
    ELO = load_elo(force_refresh=args.refresh_elo, allow_network=not in_window)

    log(f"Recalcul ({args.sims} sims) — fenêtre match : {in_window}, "
        f"matchs poule restants : {len(remaining)}")
    mc = run_monte_carlo(played, teams, remaining, overlay, args.sims)
    prob = mc["prob"]
    log(f"P(qualif)={prob:.1f}%  P(victoire)={mc['p_win']:.1f}%  "
        f"P(qualif|victoire)={mc['p_q_given_win']:.1f}%")

    state["last_heavy_run_ts"] = now_ts
    state["last_signature"] = sig
    state["last_run_prob"] = prob

    prev = state.get("last_notified_prob")
    delta = 0.0 if prev is None else (prob - prev)

    reasons, should = [], False
    if args.force:
        should = True; reasons.append("Mise à jour manuelle")
    if prev is None:
        should = True; reasons.append("Premier relevé")
    else:
        if abs(delta) >= DELTA_PCT_TRIGGER:
            should = True; reasons.append(f"Variation {delta:+.0f} pt")
        for c in crossed_threshold(prev, prob):
            should = True
            reasons.append({"ELIM": "Élimination", "VIVANT": "De nouveau en course"}.get(
                c, f"Seuil {c:.0f}% franchi"))

    # Coup d'envoi : équipe suivie en direct (confirme que le pipeline live tourne)
    sen_live = any(TEAM in k for k in overlay)
    if sen_live:
        if not state.get("sen_live_announced"):
            should = True; reasons.append("Coup d'envoi — suivi en direct")
            state["sen_live_announced"] = True
    else:   # match terminé : on réarme pour le prochain coup d'envoi
        state["sen_live_announced"] = False

    sen_pending = next_senegal_match(remaining) is not None
    if state.get("senegal_match_pending", True) and not sen_pending:
        should = True; reasons.append(f"Match du {TEAM} terminé")
    state["senegal_match_pending"] = sen_pending

    if should and not args.dry_run:
        if send_telegram(fmt_message(mc, delta, played, teams, remaining, reasons)):
            state["last_notified_prob"] = prob
    elif should and args.dry_run:
        log("DRY-RUN — message :\n" + fmt_message(mc, delta, played, teams, remaining, reasons))
    else:
        log("Pas de déclencheur — aucune notification.")

    state.setdefault("history", []).append(
        {"ts": datetime.now().isoformat(timespec="seconds"), "prob": round(prob, 1)})
    state["history"] = state["history"][-200:]
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        notify_error(e)
        raise
