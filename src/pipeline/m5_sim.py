"""M5 시험판 — 고정 M4 + 이길 가치로 가상 드래프트를 한 단계씩 진행하며 T1 밴·픽 추천 (2026-11-07, 시험용)

목적: "지금 있는 모델로 어디까지 되나" 확인. M5 정식 구현이 아님 (docs/M5_HANDOFF.md).
  - 자료 범위 = 리서치 기간 (2026-08-25 까지). 가상 경기일 D = 2026-08-26 → 특징은 모두 '전날까지'. 플레이오프 기록은 안 씀
  - 가상 경기를 드래프트 기록 표에 한 판 끼워 넣고 m4_v1.build_rows 등 기존 특징 계산을 그대로 씀
  - 상대 차례는 M4 1순위 예측을 '실제 선택'으로 가정 (--given 으로 직접 넣을 수 있음)
  - T1 차례는 아래 추천 점수 1순위를 고른다고 가정

추천 점수 (시험용, 가중치는 임시 — M5 에서 리허설로 정해야 함)
  밴 = P(상대가 이번 페이즈 남은 픽에서 c 를 고름) × (기본 2%p + max(이길 가치, 0))
  픽 = 이길 가치(m5_value.py: M1 조합 성격 근사 + 시간대 경로 나머지, 대회 %p. 없으면 예전 M1·M2 챔피언 단독) + 뺏기(P(상대 다음 픽 c) × (2%p + 이길 가치))
  맞라인 상성(M2 15분 골드 → %p)은 **점수에 안 넣고 근거 칸에만** (2026-11-08 m5_counter 리허설: 승패 예측에 더하면 대회 로그 손실 0.6552 → 0.6613)
       후보 = T1 이 이 상황에서 고를 확률 3% 이상 (최소 3개, 최대 15. M4 를 T1 입장으로 돌린 확률 — 선수 숙련·풀 반영)
확률 = M4 점수 소프트맥스. 온도 τ·바닥 ε 은 m5_calibrate.py 결과(리허설 학습 밖 점수로 맞춘 페이즈별 값, m5/calibration.json)
      없으면 08-19~08-25 LCK 경기로 맞춘 학습 안 τ (과신 쪽, 예전 방식)

사용
  python src/pipeline/m5_sim.py --opp "Gen.G" --opp-first
  python src/pipeline/m5_sim.py --opp "Hanwha Life Esports" --opp-first
  --given "7:Ahri,10:Vi"   상대(또는 T1) 단계별 실제 선택을 직접 지정 (DDragon id)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_features as F  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v1 as V  # noqa: E402
import m4_v4 as V4  # noqa: E402
import m4_v6 as V6  # noqa: E402
import m4_v7 as V7  # noqa: E402
import m4_validate as MV  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m5"
D = "2026-08-26"
US = "T1"
POS = M1.POS
ROLE_KO = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서폿"}
BASE_PP = 2.0          # 상대가 원하는 픽을 빼는 기본 가치 (%p, 임시)
CAND_PICK = 15
PHASE = lambda s: "1밴" if s <= 6 else ("1픽" if s <= 12 else ("2밴" if s <= 16 else "2픽"))  # noqa: E731
MIN_HABIT = 0.03       # 픽 후보 = T1 이 이 상황에서 고를 확률 3% 이상 (최소 3개) — 안 쓰는 챔피언을 이길 가치만으로 올리지 않게


class Sim:
    def __init__(self):
        self.a, self.pro_pick, _, self.lineup, self.sq, self.champs = B.load()
        self.a = self.a.assign(league="LCK", fearless=True)
        cid = B.cid_map()
        ev = F.pro_events(cid)
        tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
        self.ds = F.DayStats(D, ev, self.pro_pick, self.sq, tmeta, self.champs)
        self.ex = V.day_extras(D, self.pro_pick, self.sq)
        self.ev_all, self.gm = V4.patch_events(cid)
        s = pd.read_csv(PROCESSED / "c_patch_champion_summary.csv", dtype={"patch": str})
        self.buff = {(r.patch, r.champion_id): (1.0 if r.label == "buff" else -1.0 if r.label == "nerf" else 0.0) for r in s.itertuples()}
        self.wins = V7.winners(self.a)
        self.spec, self.pick_m, self.ban_m = MV.load_final()
        self.tau = self.fit_tau()   # V.Cum 을 바꾸기 전에 (보정은 원래 build_rows 그대로)
        self.calib = self.load_calib()
        # 대회 누적 집계는 한 번만 (build_rows 가 매번 새로 만들지 않게)
        cum = V.Cum()
        for g in V.pro_games(cid):
            if g["day"] < D:
                cum.add(g)
        self.cum = cum
        V.Cum = lambda: cum
        self.wv = MM.win_value_table()   # 예전 이길 가치 (새 표가 없을 때만)
        f = OUT / "m5_value_champion.csv"  # m5_value.py: M1 조합 성격 근사 + 시간대 경로 나머지 (대회 %p)
        self.val = pd.read_csv(f).set_index("key") if f.exists() else None
        if self.val is None:
            log.warning("m5_value_champion.csv 없음 → 예전 이길 가치(win_value_table) 사용")
        lc = pd.read_csv(PROCESSED / "m2" / "m2_lane_counter.csv")
        self.counter = {}
        for r in lc.itertuples():
            x, y = r.key.split(">")
            self.counter[(x, y)] = r.win_pp
        self.lck_patch = self.gm[self.gm["league"] == "LCK"].sort_values("day")["patch"].iloc[-1]
        ko = json.loads((H.ROOT / "dashboard" / "career" / "champ_ko.json").read_text(encoding="utf-8"))["names"]
        self.ko = lambda c: ko.get(c, c)

    # ------------------------------------------------------------ 라인업 (08-25 이전 마지막 LCK 경기)
    def team_lineup(self, team):
        p = self.pro_pick[(self.pro_pick["league"] == "LCK") & (self.pro_pick["teamname"] == team)]
        last = p.sort_values("day")["gameid"].iloc[-1]
        return p[p["gameid"] == last].set_index("role")["playername"].to_dict()

    # ------------------------------------------------------------ 가상 경기 → M4 행
    def row_at(self, gid, fp, lp, done, target):
        """done = [(step, cid, role)], target = 점수를 낼 단계 번호. 그 단계 앞의 안 일어난 단계는 건너뜀."""
        recs = []
        used = {c for _, c, _ in done}
        filler = [c for c in self.champs if c not in used]
        for st, c, ro in done:
            act, po, _ = V6.ORDER[st - 1]
            recs.append((st, act, po, c, ro))
        act, po, _ = V6.ORDER[target - 1]
        recs.append((target, act, po, filler[0], None))
        k = 1
        while len(recs) < 20:
            recs.append((100 + k, "ban", "fp", filler[k], None))
            k += 1
        rpg = f"SIM_{gid}"
        df = pd.DataFrame([{"GameId": gid, "MatchId": gid, "N_GameInMatch": 1, "RiotPlatformGameId": rpg, "day": D,
                            "date_kst": D + " 17:00", "Patch": "26.16", "step": st, "action": ac, "pick_order": po,
                            "team": fp if po == "fp" else lp, "opponent": lp if po == "fp" else fp, "cid": c, "role": ro,
                            "league": "LCK", "fearless": True, "is_winner": np.nan} for st, ac, po, c, ro in recs])
        a = pd.concat([self.a, df], ignore_index=True)
        rows = V.build_rows(a, [gid], {D: self.ds}, {D: self.ex}, self.lineup, self.champs, [], {}, None)
        rows = [list(r) for r in rows if r[5] == target]
        rows = V6.c2_feature(rows, a)
        gm = pd.concat([self.gm, pd.DataFrame([{"gameid": rpg, "league": "LCK", "day": D, "patch": self.lck_patch}])], ignore_index=True)
        rows = V4.p1_features(rows, a, self.ev_all, gm, self.buff, five_only=True)
        for r in rows:
            r.append("LCK")
        rows = V7.series_features(rows, a, self.wins)
        return rows[0]

    def set_lineup(self, gid, fp, lp):
        rpg = f"SIM_{gid}"
        add = {}
        for team in (fp, lp):
            for ro, pl in self.team_lineup(team).items():
                add[(rpg, team, ro)] = pl
        s = pd.Series(add)
        s.index.names = self.lineup.index.names
        self.lineup = pd.concat([self.lineup, s])

    # ------------------------------------------------------------ 점수 · 확률
    def raw_scores(self, row):
        if row[2] == "pick":
            _, _, X, _ = V7.mats([row], "pick", False)
            return self.pick_m.predict(np.hstack([X, np.ones((len(X), 1))]))
        for seg, gbm in self.ban_m:
            lo, hi = seg["steps"]
            if lo <= row[5] <= hi:
                sid, _, X, _ = V7.mats([row], "ban", True)
                lin = ((X - np.array(seg["linear"]["mu"])) / np.array(seg["linear"]["sd"])) @ np.array(seg["linear"]["coef"])
                gb = gbm.predict(np.hstack([X, np.ones((len(X), 1))]))
                return V6.zscore_within(sid, lin) + V6.zscore_within(sid, gb)
        raise ValueError(row[5])

    def probs(self, row):
        s = self.raw_scores(row)
        c = self.calib[PHASE(row[5])] if self.calib else {"tau": self.tau[row[2]], "eps": 0.0}
        z = s / c["tau"]
        p = np.exp(z - z.max())
        p = (1 - c["eps"]) * p / p.sum() + c["eps"] / len(p)
        return dict(zip(row[8]["cands"], p))

    def load_calib(self):
        """m5_calibrate.py 결과 (리허설 학습 밖 점수로 맞춘 페이즈별 τ·ε). 없으면 None → 학습 안 τ (과신)."""
        f = OUT / "calibration.json"
        if not f.exists():
            log.warning("calibration.json 없음 → 08-19~25 학습 안 τ 사용 (과신 쪽). 먼저 m5_calibrate.py 실행")
            return None
        return json.loads(f.read_text(encoding="utf-8"))["phase"]

    def fit_tau(self):
        f = OUT / "tau.json"
        if f.exists():
            return json.loads(f.read_text())
        OUT.mkdir(exist_ok=True)
        games = sorted(self.a.loc[(self.a["day"] >= "2026-08-19") & (self.a["day"] <= "2026-08-25"), "GameId"].unique())
        rows, _, _ = MV.build_rows_for(games, self.a, self.pro_pick, self.lineup, self.sq, self.champs)
        tau = {}
        for act in ("pick", "ban"):
            rr = [r for r in rows if r[2] == act and (act == "pick" or r[5] <= 6 or 13 <= r[5] <= 16)]
            S = [(self.raw_scores(r), r[3]) for r in rr]
            best = None
            for t in np.exp(np.linspace(np.log(0.05), np.log(5), 60)):
                nll = 0.0
                for s, y in S:
                    z = s / t
                    z = z - z.max()
                    nll -= z[y.argmax()] - np.log(np.exp(z).sum())
                nll /= len(S)
                if best is None or nll < best[0]:
                    best = (nll, t)
            tau[act] = float(best[1])
            log.info("τ %s = %.3f (평균 로그 손실 %.3f, %d단계)", act, best[1], best[0], len(S))
        f.write_text(json.dumps(tau))
        return tau

    # ------------------------------------------------------------ 포지션 · 이길 가치
    def role_for(self, c, open_roles):
        sh = {r: self.ds.role_share[r].get(c, 0) for r in POS}
        tot = sum(sh.values())
        if not open_roles:
            return None, 0.0
        r = max(open_roles, key=lambda r: sh[r])
        return r, (sh[r] / tot if tot else 0.0)

    def wv_pp(self, c, r):
        if not r:
            return 0.0
        if self.val is not None:
            k = f"{c}@{r}"
            return float(self.val.at[k, "value_pro"]) if k in self.val.index else 0.0
        return 25.0 * self.wv.get((c, r), 0.0)

    def timing(self, c, r):
        """(초반 ~15분, 후반 25분~·같은 상태) 나머지 몫 %p — 설명용."""
        k = f"{c}@{r}"
        if self.val is None or not r or k not in self.val.index:
            return np.nan, np.nan
        v = self.val.loc[k]
        return float(v["early10_pro"] + v["m15_pro"]), float(v["late_pro"])

    def ci(self, c, r):
        """이길 가치 95% 구간 (부트스트랩, m5_value.py)."""
        k = f"{c}@{r}"
        if self.val is None or not r or k not in self.val.index or "lo" not in self.val.columns:
            return np.nan, np.nan
        return float(self.val.at[k, "lo"]), float(self.val.at[k, "hi"])

    # ------------------------------------------------------------ 한 판 진행
    def play(self, opp, opp_first=True, given=None):
        given = given or {}
        fp, lp = (opp, US) if opp_first else (US, opp)
        gid = f"SIM/{US}_vs_{opp}"
        self.set_lineup(gid, fp, lp)
        done, log_ = [], []
        for step in range(1, 21):
            act, po, _ = V6.ORDER[step - 1]
            team = fp if po == "fp" else lp
            other = lp if team == fp else fp
            picks = defaultdict(list)
            for st, c, ro in done:
                picks[fp if V6.ORDER[st - 1][1] == "fp" else lp].append((c, ro))
            open_ = {t: [r for r in POS if r not in {ro for _, ro in picks[t]}] for t in (fp, lp)}
            row = self.row_at(gid, fp, lp, done, step)
            p_self = self.probs(row)
            entry = {"step": step, "team": team, "action": act}
            if team == US:
                rec = self.recommend(gid, fp, lp, done, step, act, other, open_, picks, p_self)
                choice = given.get(step, rec.iloc[0]["cid"])
                entry["rec"] = rec
            else:
                top = sorted(p_self.items(), key=lambda kv: -kv[1])
                if act == "pick":
                    top = [(c, p) for c, p in top if self.role_for(c, open_[team])[1] >= 0.1] or top
                entry["pred"] = top[:5]
                choice = given.get(step, top[0][0])
            ro = self.role_for(choice, open_[team])[0] if act == "pick" else None
            entry["choice"], entry["role"] = choice, ro
            done.append((step, choice, ro))
            log_.append(entry)
            log.info("%2d %s %s → %s %s", step, team, act, choice, ro or "")
        return fp, lp, log_

    def opp_next_pick_steps(self, step, opp_po):
        phase = range(7, 13) if step <= 12 else range(17, 21)
        return [s for s in phase if s > step and V6.ORDER[s - 1][0] == "pick" and V6.ORDER[s - 1][1] == opp_po]

    def recommend(self, gid, fp, lp, done, step, act, opp, open_, picks, p_us):
        opp_po = "fp" if opp == fp else "lp"
        nxt = self.opp_next_pick_steps(step, opp_po)
        # 상대가 남은 픽에서 c 를 고를 확률 (단계마다 따로 계산해 합침, 그 사이 단계는 건너뛴 근사)
        p_any, p_next = defaultdict(float), {}
        miss = defaultdict(lambda: 1.0)
        for i, s in enumerate(nxt):
            ps = self.probs(self.row_at(gid, fp, lp, done, s))
            for c, p in ps.items():
                miss[c] *= (1 - p)
            if i == 0:
                p_next = ps
        for c, m in miss.items():
            p_any[c] = 1 - m
        rows = []
        if act == "ban":
            for c in p_us:
                ro, _ = self.role_for(c, open_[opp])
                w = self.wv_pp(c, ro)
                rows.append({"cid": c, "name": self.ko(c), "score": p_any[c] * (BASE_PP + max(w, 0)),
                             "p_opp": p_any[c], "wv_opp": w, "p_us_habit": p_us[c]})
        else:
            cand = sorted(p_us, key=lambda c: -p_us[c])
            cand = [c for c in cand if self.role_for(c, open_[US])[1] >= 0.1]
            cand = [c for i, c in enumerate(cand) if i < 3 or p_us[c] >= MIN_HABIT][:CAND_PICK]
            for c in cand:
                ro, _ = self.role_for(c, open_[US])
                w = self.wv_pp(c, ro)
                cnt, vs = 0.0, ""
                for q, qr in picks[opp]:
                    if qr == ro:   # 상성 표는 쌍마다 한 방향만 저장 → 반대 방향은 부호를 바꿈
                        x, y = f"{c}@{ro}", f"{q}@{qr}"
                        cnt = self.counter.get((x, y), -self.counter.get((y, x), 0.0))
                        vs = self.ko(q)
                ro_o, _ = self.role_for(c, open_[opp])
                deny = p_next.get(c, 0.0) * (BASE_PP + max(self.wv_pp(c, ro_o), 0))
                early, late = self.timing(c, ro)
                lo, hi = self.ci(c, ro)
                rows.append({"cid": c, "name": self.ko(c), "role": ROLE_KO.get(ro, ""), "score": w + deny,   # 상성은 점수에 안 넣음 (근거 칸만, m5_counter 리허설: 승패에 더하면 대회 예측이 나빠짐)
                             "wv": w, "counter": cnt, "vs": vs, "deny": deny, "p_opp_next": p_next.get(c, 0.0),
                             "p_us_habit": p_us[c], "early": early, "late": late, "lo": lo, "hi": hi})
        return pd.DataFrame(rows).sort_values("score", ascending=False).head(8).reset_index(drop=True)


def report(sim, opp, fp, lp, lg):
    L = [f"## {US} vs {opp} (1세트, 선픽 {fp} · 후픽 {lp}, 가상 경기일 {D})", ""]
    L.append(f"라인업 {US}: " + " · ".join(f"{ROLE_KO[r]} {p}" for r, p in sim.team_lineup(US).items()))
    L.append(f"라인업 {opp}: " + " · ".join(f"{ROLE_KO[r]} {p}" for r, p in sim.team_lineup(opp).items()))
    L.append("")
    for e in lg:
        ph = "1밴" if e["step"] <= 6 else ("1픽" if e["step"] <= 12 else ("2밴" if e["step"] <= 16 else "2픽"))
        what = "밴" if e["action"] == "ban" else "픽"
        ch = sim.ko(e["choice"]) + (f"({ROLE_KO.get(e['role'], '')})" if e["role"] else "")
        if e["team"] == US:
            L.append(f"### {e['step']}단계 · {ph} · **{US} {what} 추천** → 선택 {ch}")
            r = e["rec"]
            if e["action"] == "ban":
                L += ["| 순위 | 챔피언 | 점수 | 상대가 남은 픽에서 고를 확률 | 상대 기준 이길 가치 %p | T1 평소 밴 확률 |", "|---|---|---|---|---|---|"]
                for i, x in r.head(5).iterrows():
                    L.append(f"| {i + 1} | {x['name']} | {x['score']:.2f} | {x['p_opp']:.0%} | {x['wv_opp']:+.1f} | {x['p_us_habit']:.0%} |")
            else:
                L += ["| 순위 | 챔피언 | 포지션 | 점수 %p | 이길 가치 [95% 구간] (초반 · 후반) | 맞라인 상성 (근거, 점수 밖) | 뺏기 | 상대 다음 픽 확률 | T1 평소 고를 확률 |", "|---|---|---|---|---|---|---|---|---|"]
                for i, x in r.head(5).iterrows():
                    vs = f"{x['counter']:+.1f} (vs {x['vs']})" if x["vs"] else "—"
                    tm = f" [{x['lo']:+.1f}, {x['hi']:+.1f}] ({x['early']:+.1f} · {x['late']:+.1f})" if pd.notna(x.get("early")) else ""
                    L.append(f"| {i + 1} | {x['name']} | {x['role']} | {x['score']:+.1f} | {x['wv']:+.1f}{tm} | {vs} | {x['deny']:+.1f} | {x['p_opp_next']:.0%} | {x['p_us_habit']:.0%} |")
        else:
            pr = " · ".join(f"{sim.ko(c)} {p:.0%}" for c, p in e["pred"])
            L.append(f"### {e['step']}단계 · {ph} · {e['team']} {what} → {ch}  (M4 예측: {pr})")
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--opp", action="append", required=True)
    ap.add_argument("--opp-first", action="store_true")
    ap.add_argument("--given", default="")
    args = ap.parse_args()
    given = {int(k): v for k, v in (x.split(":") for x in args.given.split(",") if x)}
    sim = Sim()
    OUT.mkdir(exist_ok=True)
    if sim.calib:
        cal = " · ".join(f"{ph} τ {c['tau']:.2f} ε {c['eps']}" for ph, c in sim.calib.items())
        head = f"보정: 리허설 학습 밖 ({cal})"
    else:
        head = f"보정: 학습 안 τ 픽 {sim.tau['pick']:.2f} · 밴 {sim.tau['ban']:.2f} (과신 쪽)"
    parts = [f"# M5 시험판 — 가상 드래프트 ({head})", ""]
    for opp in args.opp:
        fp, lp, lg = sim.play(opp, args.opp_first, given)
        parts.append(report(sim, opp, fp, lp, lg))
    (OUT / "m5_sim.md").write_text("\n".join(parts), encoding="utf-8")
    log.info("→ %s", OUT / "m5_sim.md")


if __name__ == "__main__":
    main()
