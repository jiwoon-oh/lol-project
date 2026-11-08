"""M5 밴픽 대시보드 엔진 (2026-11-08) — m5_sim.Sim 을 한 단계씩 진행 (dashboard/draft_server.py 가 씀)

  - 상대 차례: M4 예측 상위 후보 (자동 진행 = 1순위, 또는 사용자가 실제 선택 입력)
  - T1 차례: 추천 (m5_sim.mc_rank: 1수 앞보기 · 팀·선수별 이길 가치 · 밴 동점권) + 화면 근거
      조합 = 우리 팀에 c 가 주는 가치 (이길 가치 + 숙련 편차 × 0.32), 뺏기 = 상대가 남은 픽에서 c 를 고를 확률 × 상대에게 주는 가치,
      응수 = 1수 앞보기로 바뀐 몫 (앞보기 − 앞보기 없이). 세 칸은 설명용이고 합이 예상 승률 차이와 정확히 같지는 않음
  - 두 장 동시 픽: T1 이 연달아 두 번 고르는 차례(예: 후픽 8·9, 선픽 10·11, 18·19)면 단일 추천 상위 6개로 만든 쌍을 굴려 비교
  - 팀 실력 차: 승리 로그 오즈 기본값 = 진영 절편 × (블루 +1 / 레드 −1) + 계수 × (T1 Elo − 상대 Elo) / 400
      계수 = 대회 5리그 경기(m1_games.csv)의 블루 승 ~ Elo 차 로지스틱, Elo = m1_comp.elo 와 같은 식으로 08-25 까지 경기 순서대로
      이길 가치 표(m5_value)가 같은 통제(Elo) 위에서 맞춰졌으므로 로그 오즈로 더함
  - 피어리스 다음 세트: 끝난 세트를 기록 표에 더하고 진 팀이 선픽 (가정)
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m4_v6 as V6  # noqa: E402
import m5_sim as S  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
US = S.US
POS = S.POS
ROLE_KO = S.ROLE_KO
LCK_TEAMS = ["Gen.G", "Hanwha Life Esports", "Dplus Kia", "KT Rolster", "BNK FEARX", "Nongshim RedForce", "HANJIN BRION",
             "Kiwoom DRX", "DN SOOPers"]


def team_strength():
    """(팀 Elo {팀: 점수}, 진영 절편 a, Elo 계수 b). Elo 는 리그 안, 08-25 까지 (m1_comp.elo 와 같은 식, K = 24)."""
    pro = M1.pro_players()
    g = pro.groupby(["matchId", "teamId"]).agg(league=("league", "first"), date=("date", "first"),
                                               team=("teamname", "first"), win=("win", "first")).reset_index()
    g = g.pivot_table(index=["matchId", "league", "date"], columns="teamId", values=["team", "win"], aggfunc="first")
    g.columns = [f"{a}{b}" for a, b in g.columns]
    g = g.reset_index().sort_values("date")
    rating = {}
    for r in g.itertuples():
        a_, b_ = (r.league, r.team100), (r.league, r.team200)
        ra, rb = rating.get(a_, 1500.0), rating.get(b_, 1500.0)
        ea = 1 / (1 + 10 ** ((rb - ra) / 400))
        s = float(r.win100)
        rating[a_] = ra + 24 * (s - ea)
        rating[b_] = rb - 24 * (s - ea)
    elo = {t: v for (lg, t), v in rating.items() if lg == "LCK"}
    m = pd.read_csv(PROCESSED / "m1" / "m1_games.csv", usecols=["league", "elo_diff", "blue_win"], low_memory=False)
    m = m[m["league"] != "SOLO"].dropna()
    from sklearn.linear_model import LogisticRegression
    lr = LogisticRegression(C=1e4).fit(m[["elo_diff"]].to_numpy(), m["blue_win"].to_numpy())
    return elo, float(lr.intercept_[0]), float(lr.coef_[0][0])


class Draft:
    def __init__(self, R=1500):
        t0 = time.time()
        self.sim = S.Sim()
        self.sim.mc = R
        self.sim.mc_setup()
        self.elo, self.side_a, self.elo_b = team_strength()
        ex = self.sim.ex
        self.pool_sorted = ex[0]
        syn = pd.read_csv(PROCESSED / "m2" / "combos" / "m2_combo_synergy.csv", usecols=["key", "class", "duo_gold", "lift"])
        fav = pd.read_csv(PROCESSED / "m2" / "combos" / "m2_combo_pro_favoured.csv", usecols=["key", "lift"])
        self.combo = {}
        for r in syn.itertuples():
            a_, b_ = r.key.split("+")
            txt = f"검증된 조합 · 듀오 15분 골드 {r.duo_gold:+.0f}" if pd.notna(r.duo_gold) else "의미 있는 시너지"
            self.combo[(a_, b_)] = self.combo[(b_, a_)] = txt
        for r in fav.itertuples():
            a_, b_ = r.key.split("+")
            self.combo.setdefault((a_, b_), f"프로가 자주 묶음 (우연의 {r.lift:.1f}배)")
            self.combo.setdefault((b_, a_), f"프로가 자주 묶음 (우연의 {r.lift:.1f}배)")
        self.opp = None
        self.rec_cache = {}
        log.info("밴픽 엔진 준비 (%.0f초)", time.time() - t0)

    # ------------------------------------------------------------ 경기 · 세트
    def new(self, opp, t1_first=False, t1_blue=True):
        self.sim.begin_match(opp)
        self.opp, self.t1_blue = opp, t1_blue
        self.history = []
        self._start_set(t1_first)

    def _start_set(self, t1_first):
        sim = self.sim
        self.t1_first = t1_first
        self.fp, self.lp = (US, self.opp) if t1_first else (self.opp, US)
        self.gid = f"{sim.mid}/{sim.set_no}"
        sim.set_lineup(self.gid, self.fp, self.lp)
        sim.team_values(self.fp, self.lp)
        side = 1.0 if self.t1_blue else -1.0
        sim.base = side * self.side_a + self.elo_b * (self.elo.get(US, 1500) - self.elo.get(self.opp, 1500)) / 400
        self.done = []
        self.rec_cache = {}

    def next_set(self, winner):
        """끝난 세트를 기록 표에 더하고(피어리스·시리즈 특징) 다음 세트 시작. 진 팀이 선픽, 진영은 바꿔 봄 (가정)."""
        sim = self.sim
        self.history.append({"set": sim.set_no, "fp": self.fp, "lp": self.lp, "winner": winner,
                             "done": [(st, c, ro) for st, c, ro in self.done]})
        sim.finish_set(self.gid, self.fp, self.lp, self.done, winner)
        self.t1_blue = not self.t1_blue
        self._start_set(t1_first=(winner != US))

    # ------------------------------------------------------------ 상태
    def step(self):
        return len(self.done) + 1

    def team_of(self, step):
        return self.fp if V6.ORDER[step - 1][1] == "fp" else self.lp

    def picks(self):
        p = defaultdict(list)
        for st, c, ro in self.done:
            if V6.ORDER[st - 1][0] == "pick":
                p[self.team_of(st)].append((c, ro))
        return p

    def open_roles(self):
        p = self.picks()
        return {t: [r for r in POS if r not in {ro for _, ro in p[t]}] for t in (self.fp, self.lp)}

    def unavailable(self):
        return {c for _, c, _ in self.done} | set(self.sim.fearless)

    def win_now(self):
        """지금까지 픽만으로 (남은 자리 평균 0) 본 T1 승률 — 실력 차 · 진영만 반영한 기본값 포함."""
        sim = self.sim
        v = {self.fp: 0.0, self.lp: 0.0}
        for st, c, ro in self.done:
            if V6.ORDER[st - 1][0] == "pick" and ro and c in sim.ci_:
                t = self.team_of(st)
                v[t] += sim.VALt[t][sim.ci_[c], POS.index(ro)]
        opp = self.opp
        return float(1 / (1 + np.exp(-(sim.base + (v[US] - v[opp]) / 25.0)))), float(1 / (1 + np.exp(-sim.base)))

    def probs_now(self):
        step = self.step()
        if step > 20:
            return {}
        return self.sim.probs(self.sim.row_at(self.gid, self.fp, self.lp, self.done, step))

    def state(self):
        sim = self.sim
        step = self.step()
        steps = []
        dmap = {st: (c, ro) for st, c, ro in self.done}
        for st in range(1, 21):
            ac, po, _ = V6.ORDER[st - 1]
            tm = self.team_of(st)
            c, ro = dmap.get(st, (None, None))
            steps.append({"step": st, "action": ac, "team": tm, "us": tm == US, "cid": c, "role": ro,
                          "name": sim.ko(c) if c else None})
        out = {"opp": self.opp, "set": sim.set_no, "fp": self.fp, "lp": self.lp, "t1_blue": self.t1_blue,
               "blue": US if self.t1_blue else self.opp, "red": self.opp if self.t1_blue else US,
               "step": step, "done": step > 20, "steps": steps,
               "lineup": {t: {r: sim.team_lineup(t).get(r) for r in POS} for t in (US, self.opp)},
               "elo": {US: round(self.elo.get(US, 1500)), self.opp: round(self.elo.get(self.opp, 1500))},
               "fearless": sorted(sim.fearless), "history": [{"set": h["set"], "winner": h["winner"]} for h in self.history]}
        wn, wb = self.win_now()
        out["win_now"], out["win_base"] = wn, wb
        if step <= 20:
            ac, _, _ = V6.ORDER[step - 1]
            tm = self.team_of(step)
            out["action"], out["team"], out["us_turn"] = ac, tm, tm == US
            nxt = step + 1
            out["pair_turn"] = bool(tm == US and ac == "pick" and nxt <= 20 and self.team_of(nxt) == US and V6.ORDER[nxt - 1][0] == "pick")
            if tm != US:
                p = self.probs_now()
                op = self.open_roles()[tm]
                top = sorted(p.items(), key=lambda kv: -kv[1])
                if ac == "pick":
                    top = [(c, q) for c, q in top if sim.role_for(c, op)[1] >= 0.1] or top
                out["pred"] = [{"cid": c, "name": sim.ko(c), "p": float(q),
                                "role": (sim.role_for(c, op)[0] if ac == "pick" else None)} for c, q in top[:8]]
        return out

    # ------------------------------------------------------------ 행동
    def act(self, cid, role=None):
        step = self.step()
        if step > 20:
            raise ValueError("드래프트 끝")
        if cid in self.unavailable():
            raise ValueError(f"{cid} 는 이미 나갔거나 피어리스로 못 씀")
        ac, _, _ = V6.ORDER[step - 1]
        tm = self.team_of(step)
        if ac == "pick":
            op = self.open_roles()[tm]
            if role not in op:
                role = self.sim.role_for(cid, op)[0]
        else:
            role = None
        self.done.append((step, cid, role))
        self.rec_cache = {}

    def auto(self):
        """상대 차례 자동 = M4 1순위 (픽은 빈 포지션에 맞는 것)."""
        st = self.state()
        if st["done"] or st["us_turn"]:
            raise ValueError("상대 차례가 아님")
        self.act(st["pred"][0]["cid"], st["pred"][0]["role"])

    def undo(self):
        """직전 T1 행동 전으로: 그 뒤 상대 행동을 지우고 T1 행동 하나를 지움 (T1 행동이 없으면 처음으로)."""
        while self.done and self.team_of(self.done[-1][0]) != US:
            self.done.pop()
        if self.done:
            self.done.pop()
        self.rec_cache = {}

    # ------------------------------------------------------------ 추천
    def recommend(self):
        key = tuple(self.done)
        if key in self.rec_cache:
            return self.rec_cache[key]
        sim = self.sim
        step = self.step()
        ac, _, _ = V6.ORDER[step - 1]
        if self.team_of(step) != US:
            raise ValueError("T1 차례가 아님")
        t0 = time.time()
        op = self.open_roles()
        picks = self.picks()
        p_us = self.probs_now()
        opp = self.opp
        sim.resp = {}
        rec = sim.recommend(self.gid, self.fp, self.lp, self.done, step, ac, opp, op, picks, p_us)
        rec = sim.mc_rank(self.gid, self.fp, self.lp, self.done, step, ac, rec, p_us, sim.mc, op)
        cards = []
        for i, x in rec.head(8).iterrows():
            c = x["cid"]
            j = sim.ci_[c]
            card = {"cid": c, "name": x["name"], "win": float(x["mc_wp"]) / 100 if "mc_wp" in x and pd.notna(x["mc_wp"]) else None,
                    "d": float(x["mc_d"]), "se": float(x["mc_se"]), "d0": float(x["mc_d0"]), "habit": float(x["p_us_habit"]),
                    "p_opp": float(x["p_opp_mc"]), "chips": []}
            ro_o = sim.role_for(c, op[opp])[0]
            v_opp = sim.VALt[opp][j, POS.index(ro_o)] if ro_o else 0.0
            card["deny"] = float(x["p_opp_mc"]) * v_opp
            card["resp_delta"] = float(x["mc_d"] - x["mc_d0"])
            if c in sim.resp:
                card["resp"] = {"action": sim.resp[c]["action"],
                                "top": [{"cid": q, "name": sim.ko(q), "p": p} for q, p in sim.resp[c]["top"]]}
            if ac == "pick":
                ro = sim.role_for(c, op[US])[0]
                pl = sim.team_lineup(US).get(ro)
                card.update(role=ro, role_ko=ROLE_KO.get(ro, ""), player=pl, own=float(sim.VALt[US][j, POS.index(ro)]) if ro else 0.0)
                rk = self._pool_rank(pl, c)
                if rk:
                    card["chips"].append(("주력", f"{pl}의 {rk}순위 챔피언"))
                sk = sim.skill_of(US, c, ro)
                if sk and abs(sk[1]) >= 100 and sk[3] + sk[4] >= 5:
                    card["chips"].append(("숙련" if sk[1] > 0 else "숙련 약함", f"{pl} 15분 골드 {sk[1]:+.0f} ({sk[3]}+{sk[4]}판)"))
                for q, qr in picks[US]:
                    t_ = self.combo.get((f"{c}@{ro}", f"{q}@{qr}"))
                    if t_:
                        card["chips"].append(("조합", f"{sim.ko(q)} 짝 — {t_}"))
                if x.get("vs"):
                    card["chips"].append(("상성", f"vs {x['vs']} 15분 골드 환산 {x['counter']:+.1f}%p (근거)"))
                who = self._opp_main(c, op[opp])
                if who and card["p_opp"] >= 0.05:
                    card["chips"].append(("뺏기", f"{who[0]} 주력 {who[1]}순위 · 고를 확률 {card['p_opp']:.0%}"))
                card["early"], card["late"] = (None if pd.isna(x.get("early")) else float(x["early"])), (None if pd.isna(x.get("late")) else float(x["late"]))
                card["wv"] = float(x["wv"])
            else:
                card.update(own=0.0, tie=bool(x["tie"]), threat=float(x["threat"]))
                sk = x.get("skill_opp")
                if sk:
                    card["chips"].append(("상대 숙련", f"{sk[0]} 15분 골드 {sk[1]:+.0f} ({sk[3]}+{sk[4]}판)"))
                who = self._opp_main(c, op[opp])
                if who:
                    card["chips"].append(("상대 주력", f"{who[0]}의 {who[1]}순위"))
                if card["p_opp"] >= 0.05:
                    card["chips"].append(("상대 확률", f"상대가 남은 픽에서 고를 확률 {card['p_opp']:.0%}"))
            cards.append(card)
        out = {"step": step, "action": ac, "base_win": float(rec.attrs["mc_base"]) / 100, "R": int(rec.attrs.get("R", sim.mc)),
               "cards": cards, "pairs": [], "secs": 0.0}
        if ac == "pick" and step + 1 <= 20 and self.team_of(step + 1) == US and V6.ORDER[step][0] == "pick":
            out["pairs"] = self._pairs([cd["cid"] for cd in cards[:6]], step, op, out["base_win"])
        out["secs"] = round(time.time() - t0, 1)
        self.rec_cache[key] = out
        return out

    def _pool_rank(self, pl, c, top=5):
        lst = self.pool_sorted.get(pl, []) if pl else []
        return lst.index(c) + 1 if c in lst[:top] else None

    def _opp_main(self, c, open_opp):
        lu = self.sim.team_lineup(self.opp)
        best = None
        for r in (open_opp or POS):
            pl = lu.get(r)
            rk = self._pool_rank(pl, c)
            if rk and (best is None or rk < best[1]):
                best = (pl, rk)
        return best

    def _pairs(self, cands, step, op, base):
        """T1 이 연달아 두 번 고르는 차례: 단일 추천 상위 6개로 만든 쌍 (포지션 겹치지 않게) → 두 장 고른 뒤 상태로 굴림."""
        sim = self.sim
        R = sim.mc
        res = []
        for i, c1 in enumerate(cands):
            for c2 in cands[i + 1:]:
                r1, sh1 = sim.role_for(c1, op[US])
                rest = [r for r in op[US] if r != r1]
                r2, sh2 = sim.role_for(c2, rest)
                if not r1 or not r2 or sh2 < 0.1:
                    continue
                d2 = self.done + [(step, c1, r1), (step + 1, c2, r2)]
                FP = sim.future_probs(self.gid, self.fp, self.lp, d2, step + 2) if step + 2 <= 20 else {}
                wp = sim.roll(FP, step + 2, self.fp, self.lp, d2, R, 0)[0]
                k1, k2 = f"{c1}@{r1}", f"{c2}@{r2}"
                res.append({"a": c1, "b": c2, "a_name": sim.ko(c1), "b_name": sim.ko(c2), "a_role": ROLE_KO[r1], "b_role": ROLE_KO[r2],
                            "win": float(wp.mean()), "d": float(wp.mean() - base) * 100, "combo": self.combo.get((k1, k2))})
        return sorted(res, key=lambda r: -r["win"])[:5]
