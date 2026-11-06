"""M3 이기는 경로 (가벼운 1차, 2026-11-06) — docs/PIPELINE.md 7-7

질문: 조합 성격이 **어떤 경로로** 이기게 하나 — 초반 골드로 앞서서(초반형)인가, 골드가 같아도 뒤에서 이겨서(후반형)인가.
  ① 골드 → 승률 환산: 10·15·20·25분 팀 골드 차이 1,000 당 승리 로그 오즈 (솔랭 · 대회 따로)
  ② 조합 → 15분 골드: gd15 ~ 조합 성격 19개(블루 − 레드, 표준화) + 통제 (릿지)   → 초반 골드 계수 a
  ③ 조합 → 첫 포탑·첫 용 (대회 OE, 솔랭 경기 원본의 first 표시)                  → 초반 오브젝트 계수
  ④ 승패 ~ gd15 + 조합 + 통제 (로지스틱)                                          → 골드가 같을 때의 계수 c (후반·운영 몫)
     성격마다 총효과 ≈ 간접(a × 골드 기울기) + 직접(c)
  ⑤ 조합 유형 점수: 팀마다 초반 지수 = Σ a·성격, 후반 지수 = Σ c·성격 (팀 5명 평균 성격, 표준화)
     → LCK 팀별 평균 = 팀이 자주 쓰는 조합이 초반형인가 후반형인가 (팀 프로필 대조용)
  검증: 26.14 이전 학습 → 26.14·26.15 경기에서 ② 설명력(R²), ⑤ 초반 지수 ↔ 실제 gd15, 후반 지수 ↔ gd15 를 뺀 승리
통제: 솔랭 = 프로 수 차이, 대회 = Elo·숙련 차이, 출처(솔랭/리그) 절편. 플레이오프 안 봄.
자료: m1/m1_games.csv (조합 성격 · 통제), 솔랭 타임라인 팀 골드, OE complete 경기

    python src/pipeline/m3_paths.py
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import log_loss, r2_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m3"
OUT.mkdir(exist_ok=True)
FEATS, NAMES, IX = M1.FEATS, M1.NAMES, M1.IX
MINS = (10, 15, 20, 25)
FIRST_CACHE = OUT / "m3_solo_first.csv"


# ---------------------------------------------------------------- 자료
def solo_gold() -> pd.DataFrame:
    import m2_fights
    g = m2_fights.team_gold()
    g.columns = [f"gd{m}" for m in g.columns]
    return g


def solo_first() -> pd.DataFrame:
    """솔랭 경기 원본 teams[].objectives 의 first (블루가 첫 포탑·첫 용·첫 전령)."""
    if FIRST_CACHE.exists():
        return pd.read_csv(FIRST_CACHE, dtype={"matchId": str})
    rows = []
    for f in H.MATCH_DIR.glob("*.json.gz"):
        try:
            info = json.load(gzip.open(f))["info"]
        except Exception:
            continue
        blue = next((t for t in info.get("teams", []) if t.get("teamId") == 100), None)
        if not blue:
            continue
        o = blue.get("objectives", {})
        rows.append({"matchId": f.name[:-8], "first_tower": int(o.get("tower", {}).get("first", False)),
                     "first_dragon": int(o.get("dragon", {}).get("first", False)),
                     "first_herald": int(o.get("riftHerald", {}).get("first", False))})
    d = pd.DataFrame(rows)
    H.save_csv(d, FIRST_CACHE)
    return d


def pro_paths() -> pd.DataFrame:
    cols = ["gameid", "league", "date", "side", "position", "datacompleteness", "teamname",
            "firsttower", "firstdragon", "firstherald"] + [f"golddiffat{m}" for m in MINS]
    oe = pd.read_csv(M1.OE_FILE, usecols=cols, low_memory=False)
    oe = oe[oe["league"].isin(M1.LEAGUES) & (oe["date"].str[:10] <= M1.END) & (oe["position"] == "team")]
    b = oe[oe["side"] == "Blue"].set_index("gameid")
    r = oe[oe["side"] == "Red"].set_index("gameid")
    d = pd.DataFrame({"blue_team": b["teamname"], "red_team": r["teamname"].reindex(b.index),
                      "complete": b["datacompleteness"] == "complete",
                      "first_tower": b["firsttower"], "first_dragon": b["firstdragon"], "first_herald": b["firstherald"]})
    for m in MINS:
        d[f"gd{m}"] = b[f"golddiffat{m}"] / 1000
    d.loc[~d["complete"], [f"gd{m}" for m in MINS]] = np.nan
    return d.rename_axis("matchId").reset_index()


def load() -> pd.DataFrame:
    G = pd.read_csv(PROCESSED / "m1" / "m1_games.csv", dtype={"patch": str, "matchId": str}, low_memory=False)
    sg = solo_gold().reset_index()
    sf = solo_first()
    pp = pro_paths()
    solo = G[G["league"] == "SOLO"].merge(sg, on="matchId", how="left").merge(sf, on="matchId", how="left")
    pro = G[G["league"] != "SOLO"].merge(pp, on="matchId", how="left")
    return pd.concat([solo, pro], ignore_index=True)


# ---------------------------------------------------------------- 설계 행렬
def design(d: pd.DataFrame, mu=None, sd=None):
    X = d[FEATS].to_numpy(float)
    mu = np.nanmean(X, axis=0) if mu is None else mu
    sd = np.nanstd(X, axis=0) if sd is None else sd
    sd = np.where(sd > 0, sd, 1)
    Z = np.nan_to_num((X - mu) / sd)
    pro = (d["league"] != "SOLO").to_numpy(float)[:, None]
    lg = np.column_stack([(d["league"] == L).to_numpy(float) for L in ["SOLO"] + M1.LEAGUES])
    ctrl = np.column_stack([d["pro_diff"].fillna(0) * (1 - pro[:, 0]), d["elo_diff"].fillna(0) * pro[:, 0],
                            d["mastery_diff"].fillna(0) * pro[:, 0]])
    # 성격 공통 + 대회 편차(×0.5, 같은 규제에서 더 당김) + 리그 절편 + 통제
    return np.hstack([Z, Z * pro * 0.5, lg, ctrl]), mu, sd


def eff(coef: np.ndarray) -> pd.DataFrame:
    k = len(FEATS)
    return pd.DataFrame({"solo": coef[:k], "pro": coef[:k] + coef[k:2 * k] * 0.5}, index=FEATS)


# ---------------------------------------------------------------- 단계
def conversion(d: pd.DataFrame) -> pd.DataFrame:
    """① 분별 골드 차이 → 승률 (M1 오프셋 없이, 출처 절편만)."""
    rows = []
    for src, s in (("솔랭", d[d["league"] == "SOLO"]), ("대회", d[d["league"] != "SOLO"])):
        for m in MINS:
            t = s.dropna(subset=[f"gd{m}"])
            if len(t) < 100:
                continue
            X = np.column_stack([t[f"gd{m}"].to_numpy(float)])
            lr = LogisticRegression(C=1e4).fit(X, t["blue_win"])
            b, a = lr.coef_[0][0], lr.intercept_[0]
            p = lambda g: 1 / (1 + np.exp(-(a + b * g)))  # noqa: E731
            rows.append({"source": src, "minute": m, "n": len(t), "slope_per_1k": b,
                         "win_at_+1k": p(1), "win_at_+2k": p(2), "win_at_+3k": p(3), "win_at_+5k": p(5),
                         "sd_gold": t[f"gd{m}"].std()})
    return pd.DataFrame(rows)


def fit_gold(d, alpha=30.0):
    t = d.dropna(subset=["gd15"])
    X, mu, sd = design(t)
    m = Ridge(alpha=alpha).fit(X, t["gd15"])
    return m, mu, sd


def fit_first(d, col):
    t = d.dropna(subset=[col])
    X, mu, sd = design(t)
    return LogisticRegression(C=0.05, max_iter=3000).fit(X, t[col].astype(int)), mu, sd


def fit_win_given_gold(d):
    t = d.dropna(subset=["gd15"])
    X, mu, sd = design(t)
    X = np.hstack([X, t[["gd15"]].to_numpy(float)])
    return LogisticRegression(C=0.05, max_iter=3000).fit(X, t["blue_win"]), mu, sd


def fit_win_total(d):
    t = d.dropna(subset=["gd15"])  # 같은 경기로 총효과 (비교용)
    X, mu, sd = design(t)
    return LogisticRegression(C=0.05, max_iter=3000).fit(X, t["blue_win"]), mu, sd


def team_index(d: pd.DataFrame, a: pd.Series, c: pd.Series) -> pd.DataFrame:
    """⑤ 팀(진영)별 초반·후반 지수. 팀 값 b_/r_ (IX 성격만 있음) 을 경기 전체 평균·표준편차로 표준화."""
    feats = [f for f in FEATS if f"b_{f}" in d]  # 점검 E2: 19개 전부 (전에는 IX 14개)
    if len(feats) < len(FEATS):
        log.warning("팀 값이 없는 성격 %s — python src/pipeline/m1_comp.py --team-values 먼저", sorted(set(FEATS) - set(feats)))
    tv = np.vstack([d[[f"b_{f}" for f in feats]].to_numpy(float), d[[f"r_{f}" for f in feats]].to_numpy(float)])
    mu, sd = np.nanmean(tv, axis=0), np.nanstd(tv, axis=0)
    sd = np.where(sd > 0, sd, 1)
    out = {}
    for side, p in (("blue", "b_"), ("red", "r_")):
        Z = np.nan_to_num((d[[f"{p}{f}" for f in feats]].to_numpy(float) - mu) / sd)
        out[f"{side}_early"] = Z @ a[feats].to_numpy()
        out[f"{side}_late"] = Z @ c[feats].to_numpy()
    return pd.DataFrame(out, index=d.index)


# ---------------------------------------------------------------- 2차: A 팀 수행력, B 역전 가능성
def sig(z):
    return 1 / (1 + np.exp(-z))


def team_execution(d: pd.DataFrame, gm: Ridge, mu, sd, league="LCK") -> pd.DataFrame:
    """A. 팀이 고른 조합이 예상한 15분 골드보다 실제로 더 버나(초반 수행), 15분 골드로 예상한 승률보다 더 이기나(굳히기·역전).
    조합 예상 = 조합 + 리그 절편 (실력 통제 칸은 0 으로 둠 → 팀 실력은 수행력 쪽에 남김)."""
    t = d[(d["league"] == league)].dropna(subset=["gd15"]).copy()
    X, _, _ = design(t, mu, sd)
    X[:, -3:] = 0  # 통제(프로 수·Elo·숙련) 빼고 조합만
    t["exp_gd"] = gm.predict(X)
    pro = d[(d["league"] != "SOLO")].dropna(subset=["gd15"])
    cv = LogisticRegression(C=1e4).fit(pro[["gd15"]], pro["blue_win"])
    t["p_blue"] = cv.predict_proba(t[["gd15"]])[:, 1]
    rows = []
    for side, s in (("blue", 1), ("red", -1)):
        rows.append(pd.DataFrame({"team": t[f"{side}_team"], "gd": s * t["gd15"], "exp_gd": s * t["exp_gd"],
                                  "win": t["blue_win"] if s == 1 else 1 - t["blue_win"],
                                  "p": t["p_blue"] if s == 1 else 1 - t["p_blue"]}))
    r = pd.concat(rows).dropna(subset=["team"])
    r["early_res"], r["conv_res"] = r["gd"] - r["exp_gd"], r["win"] - r["p"]
    g = r.groupby("team")
    out = g.agg(games=("win", "size"), gd=("gd", "mean"), exp_gd=("exp_gd", "mean"), early_exec=("early_res", "mean"),
                early_se=("early_res", lambda x: x.std() / np.sqrt(len(x))), win=("win", "mean"), exp_win=("p", "mean"),
                conv=("conv_res", "mean"), conv_se=("conv_res", lambda x: x.std() / np.sqrt(len(x))))
    for nm, m in (("ahead", r["gd"] > 1), ("behind", r["gd"] < -1)):
        a = r[m].groupby("team").agg(n=("win", "size"), w=("win", "mean"), p=("p", "mean"))
        out[f"{nm}_n"], out[f"{nm}_win"], out[f"{nm}_exp"] = a["n"], a["w"], a["p"]
    return out[out["games"] >= 20]


def comeback(d: pd.DataFrame, cut: str) -> dict:
    """B. 승리 ~ 15분 골드 + 후반 지수 차 + 15분 골드 × 후반 지수 차 + 초반 지수 차 + 통제. 출처별.
    후반형이면 15분에 지고 있어도 더 이기나 (상호작용) + 평가 패치에서 상호작용 칸이 예측을 돕나."""
    out = {}
    for src, s in (("대회", d[d["league"] != "SOLO"]), ("솔랭", d[d["league"] == "SOLO"])):
        s = s.dropna(subset=["gd15"]).copy()
        s["late_d"] = s["blue_late"] - s["red_late"]
        s["early_d"] = s["blue_early"] - s["red_early"]
        sdl = s["late_d"].std()
        s["late_z"] = s["late_d"] / sdl
        s["early_z"] = s["early_d"] / s["early_d"].std()
        ctrl = ["elo_diff", "mastery_diff"] if src == "대회" else ["pro_diff"]

        def X(t, inter=True):
            cols = [t["gd15"], t["late_z"], t["early_z"]] + ([t["gd15"] * t["late_z"]] if inter else []) + [t[c].fillna(0) for c in ctrl]
            return np.column_stack(cols)
        m = LogisticRegression(C=1e3, max_iter=3000).fit(X(s), s["blue_win"])
        b = m.coef_[0]
        grid = {}
        for g15 in (-3, -2, -1, 0, 1, 2):
            for lz in (-1, 0, 1):
                grid[(g15, lz)] = sig(m.intercept_[0] + b[0] * g15 + b[1] * lz + b[3] * g15 * lz)
        tr, te = s[s["day"] < cut], s[s["patch"].isin(["26.14", "26.15"])]
        ll = {}
        for inter in (True, False):
            mm = LogisticRegression(C=1e3, max_iter=3000).fit(X(tr, inter), tr["blue_win"])
            ll[inter] = log_loss(te["blue_win"], mm.predict_proba(X(te, inter))[:, 1])
        # 부트스트랩 구간 (상호작용 계수)
        rng = np.random.default_rng(0)
        bs = []
        for _ in range(100):
            i = rng.integers(0, len(s), len(s))
            bs.append(LogisticRegression(C=1e3, max_iter=3000).fit(X(s.iloc[i]), s["blue_win"].iloc[i]).coef_[0][3])
        out[src] = {"n": len(s), "coef": b, "inter_ci": np.percentile(bs, [2.5, 97.5]), "grid": grid, "ll": ll}
    return out


def main():
    t0 = time.time()
    d = load()
    log.info("M3 자료: 경기 %d (솔랭 15분 골드 %d · 대회 complete %d) (%.0f초)", len(d),
             d.loc[d["league"] == "SOLO", "gd15"].notna().sum(), d.loc[d["league"] != "SOLO", "gd15"].notna().sum(),
             time.time() - t0)
    conv = conversion(d)
    H.save_csv(conv, OUT / "m3_gold_to_win.csv")

    # 검증: 26.14 이전 학습 → 26.14·26.15
    cut = H.research_windows().set_index("patch").loc["26.14", "window_from"]
    tr, te = d[d["day"] < cut], d[d["patch"].isin(["26.14", "26.15"])]
    gm, mu, sd = fit_gold(tr)
    tt = te.dropna(subset=["gd15"])
    Xte, _, _ = design(tt, mu, sd)
    pr = gm.predict(Xte)
    ip = (tt["league"] != "SOLO").to_numpy()
    # 통제만 (조합 성격 0) 과 비교
    Xc = Xte.copy()
    k = len(FEATS)
    Xc[:, :2 * k] = 0
    pc = gm.predict(Xc)
    r2 = {s: (r2_score(tt["gd15"][m_], pr[m_]), r2_score(tt["gd15"][m_], pc[m_])) for s, m_ in (("대회", ip), ("솔랭", ~ip))}
    wm, mu2, sd2 = fit_win_given_gold(tr)
    tw = te.dropna(subset=["gd15"])
    Xw, _, _ = design(tw, mu2, sd2)
    Xw = np.hstack([Xw, tw[["gd15"]].to_numpy(float)])
    pw = wm.predict_proba(Xw)[:, 1]
    Xw0 = Xw.copy()
    Xw0[:, :2 * k] = 0
    pw0 = wm.predict_proba(Xw0)[:, 1]
    ipw = (tw["league"] != "SOLO").to_numpy()
    llw = {s: (log_loss(tw["blue_win"][m_], pw[m_]), log_loss(tw["blue_win"][m_], pw0[m_])) for s, m_ in (("대회", ipw), ("솔랭", ~ipw))}
    log.info("검증 끝 (%.0f초)", time.time() - t0)

    # 최종 (08-25 까지 전부)
    gm, mu, sd = fit_gold(d)
    a = eff(gm.coef_)
    wm, _, _ = fit_win_given_gold(d)
    c = eff(wm.coef_[0][:-1])
    b_gold = wm.coef_[0][-1]
    tm, _, _ = fit_win_total(d)
    tot = eff(tm.coef_[0])
    firsts = {}
    for col, nm in (("first_tower", "첫 포탑"), ("first_dragon", "첫 용"), ("first_herald", "첫 전령")):
        if d[col].notna().sum() > 1000:
            fm, _, _ = fit_first(d, col)
            firsts[nm] = eff(fm.coef_[0])
    co = pd.DataFrame({"name": [NAMES[f] for f in FEATS],
                       "gold15_solo": a["solo"], "gold15_pro": a["pro"],
                       "win_direct_solo": c["solo"], "win_direct_pro": c["pro"],
                       "win_total_solo": tot["solo"], "win_total_pro": tot["pro"]}, index=FEATS)
    co["win_via_gold_solo"] = co["gold15_solo"] * b_gold
    for nm, e in firsts.items():
        co[f"{nm}_pro"] = e["pro"]
    co["type"] = np.select(
        [(co["gold15_solo"] > 0.02) & (co["win_direct_solo"] <= 0), (co["gold15_solo"] <= 0) & (co["win_direct_solo"] > 0.01),
         (co["gold15_solo"] > 0.02) & (co["win_direct_solo"] > 0.01), (co["gold15_solo"] < -0.02) & (co["win_direct_solo"] < -0.01)],
        ["초반형", "후반형", "초반·후반 모두 +", "초반·후반 모두 −"], "중립")
    H.save_csv(co.rename_axis("feature").reset_index(), OUT / "m3_paths.csv")

    # 팀 지수
    ti = team_index(d, a["pro"], c["pro"])
    d = pd.concat([d, ti], axis=1)
    lck = d[d["league"] == "LCK"]
    rows = []
    for side in ("blue", "red"):
        rows.append(pd.DataFrame({"team": lck[f"{side}_team"], "early": lck[f"{side}_early"], "late": lck[f"{side}_late"],
                                  "gd15": lck["gd15"] * (1 if side == "blue" else -1),
                                  "win": lck["blue_win"] if side == "blue" else 1 - lck["blue_win"]}))
    tl = pd.concat(rows).dropna(subset=["team"])
    team = tl.groupby("team").agg(games=("win", "size"), early=("early", "mean"), late=("late", "mean"),
                                  gd15=("gd15", "mean"), win=("win", "mean"))
    team = team[team["games"] >= 20]
    z = lambda s: (s - s.mean()) / s.std()  # noqa: E731
    team["early_z"], team["late_z"] = z(team["early"]), z(team["late"])
    team["style"] = np.select([(team["early_z"] > 0.5) & (team["late_z"] < 0.5), (team["late_z"] > 0.5) & (team["early_z"] < 0.5),
                               (team["early_z"] > 0.5) & (team["late_z"] > 0.5)], ["초반형 조합", "후반형 조합", "둘 다 높음"], "중간")
    H.save_csv(team.reset_index(), OUT / "m3_team_style.csv")
    H.save_csv(d[["matchId", "league", "day", "patch", "blue_early", "blue_late", "red_early", "red_late"]], OUT / "m3_games.csv")
    # 지수 검증 (26.14·26.15, 학습은 이전 — 위 gm·wm 은 전체라 여기선 상관만 참고)
    v = d[d["patch"].isin(["26.14", "26.15"])].dropna(subset=["gd15"])
    early_diff = v["blue_early"] - v["red_early"]
    late_diff = v["blue_late"] - v["red_late"]
    resid_win = v["blue_win"] - 1 / (1 + np.exp(-b_gold * v["gd15"]))
    corr_e = np.corrcoef(early_diff, v["gd15"])[0, 1]
    corr_l = np.corrcoef(late_diff, resid_win)[0, 1]

    L = ["# M3 이기는 경로 (가벼운 1차)", "",
         f"- 자료: 경기 {len(d):,} (솔랭 15분 골드 {d.loc[d['league'] == 'SOLO', 'gd15'].notna().sum():,} · 대회 complete {d.loc[d['league'] != 'SOLO', 'gd15'].notna().sum():,}), 08-25 까지. 플레이오프 안 봄",
         "- 조합 성격 = M1 의 19개 (블루 − 레드, 표준화). 통제 = 솔랭 프로 수 · 대회 Elo·숙련 · 리그 절편", "",
         "## ① 골드 → 승률 (팀 골드 차이, 블루 기준)", "",
         "| 출처 | 분 | 경기 | 1,000 당 로그 오즈 | +1k 승률 | +2k | +3k | +5k | 골드 차이 표준편차 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in conv.itertuples():
        L.append(f"| {r.source} | {r.minute} | {r.n:,} | {r.slope_per_1k:+.3f} | {r._5:.0%} | {r._6:.0%} | {r._7:.0%} | {r._8:.0%} | {r.sd_gold:.2f}k |")
    L += ["", "- 같은 골드 우세라도 시간이 지날수록 기울기는 작아짐 (판이 커지며 같은 골드의 무게가 줄어듦) — 추천 화면의 '15분 골드 ±N' 을 승률로 바꿀 때 이 표를 씀", "",
          "## ② ④ 조합 성격의 경로 (08-25 까지 전부)", "",
          f"- 15분 골드 1,000 = 승리 로그 오즈 {b_gold:+.3f} (조합·통제를 같이 넣은 모델)",
          "- 초반 골드 = 성격 1 SD 당 15분 골드(천). 직접 = 15분 골드가 같을 때 승리 로그 오즈 (후반·운영 몫). 간접 = 초반 골드 × 골드 기울기. 총 = 골드를 빼고 본 승리 로그 오즈", "",
          "| 성격 | 초반 골드 (솔랭) | 대회 | 간접 | 직접 (솔랭) | 대회 | 총 (솔랭) | 유형 |", "|---|---|---|---|---|---|---|---|"]
    for f_, r in co.reindex(co["gold15_solo"].abs().sort_values(ascending=False).index).iterrows():
        L.append(f"| {r['name']} | {r['gold15_solo'] * 1000:+.0f} | {r['gold15_pro'] * 1000:+.0f} | {r['win_via_gold_solo']:+.3f} | "
                 f"{r['win_direct_solo']:+.3f} | {r['win_direct_pro']:+.3f} | {r['win_total_solo']:+.3f} | {r['type']} |")
    if firsts:
        L += ["", "### ③ 초반 오브젝트 (대회 계수, 성격 1 SD 당 로그 오즈)", "", "| 성격 | " + " | ".join(firsts) + " |",
              "|---|" + "---|" * len(firsts)]
        for f_ in FEATS:
            L.append(f"| {NAMES[f_]} | " + " | ".join(f"{firsts[n].loc[f_, 'pro']:+.3f}" for n in firsts) + " |")
    L += ["", "## 검증 (26.14 이전 학습 → 26.14·26.15)", "",
          "| 출처 | 15분 골드 R² (조합 + 통제) | 통제만 | 승패 로그 손실 (15분 골드 + 조합) | 15분 골드 + 통제만 |", "|---|---|---|---|---|"]
    for s in ("대회", "솔랭"):
        L.append(f"| {s} | {r2[s][0]:.4f} | {r2[s][1]:.4f} | {llw[s][0]:.4f} | {llw[s][1]:.4f} |")
    L += ["", f"- 초반 지수 차이 ↔ 실제 15분 골드 상관 {corr_e:.3f}, 후반 지수 차이 ↔ 골드로 설명 안 되는 승리 상관 {corr_l:.3f} (26.14·26.15, 참고)", "",
          "## ⑤ LCK 팀이 쓰는 조합의 유형 (팀 평균, 20경기 이상)", "",
          "- 초반 지수 = 조합으로 예상한 15분 골드 몫, 후반 지수 = 골드가 같을 때 조합이 주는 승리 몫. 팀 사이 표준화 (z)", "",
          "| 팀 | 경기 | 초반 z | 후반 z | 유형 | 실제 15분 골드 | 승률 |", "|---|---|---|---|---|---|---|"]
    for tname, r in team.sort_values("early_z", ascending=False).iterrows():
        L.append(f"| {tname} | {int(r['games'])} | {r['early_z']:+.2f} | {r['late_z']:+.2f} | {r['style']} | {r['gd15'] * 1000:+.0f} | {r['win']:.0%} |")
    L += ["", "- 팀 유형은 '그 팀이 고른 조합의 성격'이지 운영 실력이 아님. 팀 프로필(리서치) 운영 성향과 대조할 것", ""]

    # A 팀 수행력
    te_ = team_execution(d, gm, mu, sd)
    te_ = te_.join(team[["style", "early_z", "late_z"]], how="left")
    H.save_csv(te_.reset_index(), OUT / "m3_team_execution.csv")
    L += ["## A. LCK 팀 수행력 — 고른 조합 대비 실제 (대회 complete 경기)", "",
          "- 초반 수행 = 실제 15분 골드 − 조합이 예상한 15분 골드 (팀 실력은 빼지 않음 → 선수·운영 몫이 여기에)",
          "- 굳히기·역전 = 실제 승률 − 15분 골드로 예상한 승률 (대회 환산식). 앞섬 = 15분 +1k 이상, 뒤짐 = −1k 이하",
          "- ± = 표준오차. 상대 실력 보정 없음 (리그 안 경기라 대략 고름)", "",
          "| 팀 | 경기 | 조합 유형 | 실제 15분 골드 | 조합 예상 | **초반 수행** | 승률 | 골드 예상 승률 | **굳히기·역전** | 앞설 때 승률 (예상) | 뒤질 때 승률 (예상) |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for tn, r in te_.sort_values("early_exec", ascending=False).iterrows():
        ah = f"{r['ahead_win']:.0%} ({r['ahead_exp']:.0%}, {int(r['ahead_n'])}판)" if pd.notna(r.get("ahead_n")) else "—"
        bh = f"{r['behind_win']:.0%} ({r['behind_exp']:.0%}, {int(r['behind_n'])}판)" if pd.notna(r.get("behind_n")) else "—"
        L.append(f"| {tn} | {int(r['games'])} | {r.get('style', '')} | {r['gd'] * 1000:+.0f} | {r['exp_gd'] * 1000:+.0f} | "
                 f"**{r['early_exec'] * 1000:+.0f}** ±{r['early_se'] * 1000:.0f} | {r['win']:.0%} | {r['exp_win']:.0%} | "
                 f"**{r['conv'] * 100:+.1f}%p** ±{r['conv_se'] * 100:.1f} | {ah} | {bh} |")
    L += [""]

    # B 역전 가능성
    cb = comeback(d, cut)
    L += ["## B. 역전 가능성 — 15분 골드 × 후반 지수", "",
          "- 승리 ~ 15분 골드 + 후반 지수 차 + 초반 지수 차 + **15분 골드 × 후반 지수 차** + 통제. 지수 차는 출처 안 표준편차로 나눔 (z)", ""]
    for src, r in cb.items():
        b = r["coef"]
        L += [f"### {src} ({r['n']:,}경기)", "",
              f"- 계수: 15분 골드(천) {b[0]:+.3f} · 후반 지수 z {b[1]:+.3f} · 초반 지수 z {b[2]:+.3f} · "
              f"**골드 × 후반 {b[3]:+.3f} [{r['inter_ci'][0]:+.3f}, {r['inter_ci'][1]:+.3f}]** "
              "(− 이면 후반형일수록 15분 골드에 덜 휘둘림 = 지고 있어도 덜 짐)",
              f"- 평가 (26.14 이전 학습 → 26.14·26.15) 로그 손실: 상호작용 있음 {r['ll'][True]:.4f} · 없음 {r['ll'][False]:.4f}", "",
              "| 15분 골드 | 후반 지수 −1 SD | 0 | +1 SD |", "|---|---|---|---|"]
        for g15 in (-3, -2, -1, 0, 1, 2):
            L.append(f"| {g15:+d}k | " + " | ".join(f"{r['grid'][(g15, lz)]:.0%}" for lz in (-1, 0, 1)) + " |")
        L += [""]
    (OUT / "m3_eval.md").write_text("\n".join(L), encoding="utf-8")
    log.info("M3 끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
