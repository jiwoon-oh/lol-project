"""M5 4단계 — 선수 × 챔피언 숙련 편차 δ (2026-11-08)  — docs/PIPELINE.md 7-7 "M5 4단계" (처음 설계 = 2026-11-05 "상대 선수 약점 픽", decisions.md)

질문: "이 선수가 이 챔피언을 (그 챔피언 평균·그 선수 평소 수준보다) 잘하나/못하나" 가 실제로 다음 경기에 맞나
결과 = 선수 경기 1행, 라인 상대 대비 15분 골드 차 (천 단위). 대회(OE 5리그, golddiffat15) + 솔랭(리서치 선수 58명, gold_diff15)
칸 (릿지, 판수가 적으면 0 쪽으로)
  기준: 챔피언@포지션, 챔피언@포지션×패치, 상대 챔피언(−), 선수 평소 수준, 선수×출처, 상대 선수(−, 대회만), 팀 Elo(대회), 출처 절편
  δ = 선수×챔피언 (솔랭·대회 공통) + 대회 편차 (대회 행만, 더 강하게 당김)   ← 설계: 솔랭 δ 를 사전 정보로, 대회 δ 는 벗어난 만큼
검증 (시간 분할, 플레이오프 안 봄): 평가 패치 p (26.08 · 26.09 · 26.10 · 26.14 · 26.15) 마다 그 패치 창 전 자료로 학습
  ① 그 패치 대회 경기 선수 15분 골드 R²: δ 없음 vs 있음 (전체 · 그 선수×챔피언 이전 판수 3 이상)
  ② 승패: 기본 = 통제(리그·Elo·대회 숙련 경기 수) 로지스틱 또는 M1 성격 예측 + k × (블루 − 레드 δ 합), k 는 학습 마지막 15% 대회 경기로.
     (M1 성격 예측은 5개 패치 리허설에서 26.08·26.09 가 통제만보다 나빴음 → 두 기본 모두로 확인)
     비교: 기본 / + 선수 평소 수준 합 / + δ 합 / + 둘 다. 5개 패치 합 + 경기 다시 뽑기 구간
출력: m5/m5_skill.csv (LCK 선수 × 챔피언 δ, 판수), m5_skill_selection.csv, m5_skill_eval.md

    python src/pipeline/m5_skill.py     (m5_value.py 의 value_input.pkl · m1_rehearsal_{p}.pkl 이 있어야 함)
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.sparse.linalg import spsolve

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m2_synergy as M2  # noqa: E402
import m4_baseline as B  # noqa: E402
import m5_value as MV  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m5"
PATCHES = ["26.08", "26.09", "26.10", "26.14", "26.15"]
SEL = ("26.14", "26.15")
SLOPE, PP = 0.456, 25.0
LAM = {"c": 10.0, "cp": 30.0, "oc": 10.0, "p": 30.0, "ps": 30.0, "op": 30.0}
GRID_PC = [10.0, 30.0, 100.0, 300.0]
PRO_DEV = 3.0                 # 대회 편차 δ 규제 배율
BIG = 1e9


# ---------------------------------------------------------------- 자료
def load_rows() -> pd.DataFrame:
    f = OUT / "skill_rows.pkl"
    if f.exists():
        return pd.read_pickle(f)
    cid = B.cid_map()
    cols = ["gameid", "league", "date", "patch", "side", "position", "playername", "champion", "golddiffat15", "result", "datacompleteness"]
    oe = pd.read_csv(M1.OE_FILE, usecols=cols, low_memory=False, dtype={"patch": str})
    oe = oe[oe["league"].isin(M1.LEAGUES) & (oe["date"].str[:10] <= M1.END) & oe["position"].isin(H.OE_POS)
            & (oe["datacompleteness"] == "complete")].copy()
    oe["pos"] = oe["position"].map(H.OE_POS)
    oe["cid"] = oe["champion"].map(cid)
    oe["patch"] = oe["patch"].map(M1.oe_patch_to_ours)
    oe["day"] = oe["date"].str[:10]
    opp = oe.set_index(["gameid", "pos", "side"])
    other = oe.apply(lambda r: ("Red" if r["side"] == "Blue" else "Blue"), axis=1)
    key = list(zip(oe["gameid"], oe["pos"], other))
    oe["opp_player"] = [opp["playername"].get(k) for k in key]
    oe["opp_cid"] = [opp["cid"].get(k) for k in key]
    G, _ = pd.read_pickle(OUT / "value_input.pkl")
    elo = G.set_index("matchId")["elo_diff"]
    pro = pd.DataFrame({"src": "pro", "match": oe["gameid"].astype(str), "day": oe["day"], "patch": oe["patch"].astype(str),
                        "side": oe["side"], "pos": oe["pos"], "player": oe["playername"], "cid": oe["cid"],
                        "opp_player": oe["opp_player"], "opp_cid": oe["opp_cid"], "y": oe["golddiffat15"] / 1000,
                        "win": oe["result"].astype(float)})
    pro["elo"] = pro["match"].map(elo).fillna(0.0) * np.where(pro["side"] == "Blue", 1.0, -1.0)
    s = pd.read_csv(PROCESSED / "g_research_soloq_games.csv", low_memory=False,
                    usecols=["matchId", "game_start_kst", "patch", "player", "teamPosition", "championName", "opp_champion",
                             "gold_diff15", "win", "on_role", "suspect_account", "wasAfk", "is_remake"], dtype={"patch": str})
    s = s[(s["on_role"] == True) & ~s["suspect_account"].fillna(False).astype(bool) & ~s["wasAfk"].fillna(False).astype(bool)  # noqa: E712
          & ~s["is_remake"].fillna(False).astype(bool)]
    s["day"] = s["game_start_kst"].astype(str).str[:10]
    s = s[s["day"] <= M1.END]
    solo = pd.DataFrame({"src": "solo", "match": s["matchId"].astype(str), "day": s["day"], "patch": s["patch"].astype(str),
                         "side": "", "pos": s["teamPosition"], "player": s["player"], "cid": s["championName"].map(cid),
                         "opp_player": None, "opp_cid": s["opp_champion"].map(cid), "y": s["gold_diff15"] / 1000,
                         "win": s["win"].astype(float), "elo": 0.0})
    d = pd.concat([pro, solo], ignore_index=True)
    d = d.dropna(subset=["cid", "y", "pos", "player"])
    d["key"] = d["cid"].astype(str) + "@" + d["pos"]
    d["okey"] = d["opp_cid"].astype(str) + "@" + d["pos"]
    pd.to_pickle(d, f)
    log.info("행: 대회 %d · 솔랭 %d", (d["src"] == "pro").sum(), (d["src"] == "solo").sum())
    return d


# ---------------------------------------------------------------- 칸 · 적합
def terms(r) -> list:
    t = [("c", r.key, 1.0), ("cp", f"{r.key}|{r.patch}", 1.0), ("p", r.player, 1.0), ("ps", f"{r.player}|{r.src}", 1.0),
         ("pc", f"{r.player}|{r.key}", 1.0)]
    if isinstance(r.opp_cid, str) and r.opp_cid != "nan":
        t.append(("oc", r.okey, -1.0))
    if r.src == "pro":
        t.append(("pcp", f"{r.player}|{r.key}", 1.0))
        if isinstance(r.opp_player, str):
            t.append(("op", r.opp_player, -1.0))
    return t


class Model:
    def __init__(self, lam_pc):
        self.lam = dict(LAM, pc=lam_pc, pcp=lam_pc * PRO_DEV if lam_pc < BIG else BIG)

    def _X(self, d, T, fit):
        if fit:
            cnt = Counter((k, key) for t in T for k, key, _ in t)
            self.cols = list(cnt)
            self.idx = {c: i for i, c in enumerate(self.cols)}
            self.n = cnt
        ri, ci, v = [], [], []
        for i, t in enumerate(T):
            for k, key, s in t:
                j = self.idx.get((k, key))
                if j is not None:
                    ri.append(i); ci.append(j); v.append(s)
        X = sparse.csr_matrix((v, (ri, ci)), shape=(len(T), len(self.cols)))
        C = np.column_stack([(d["src"] == "pro").to_numpy(float), (d["src"] == "solo").to_numpy(float), d["elo"].to_numpy(float)])
        return sparse.hstack([X, sparse.csr_matrix(C)]).tocsr()

    def fit(self, d):
        T = [terms(r) for r in d.itertuples()]
        X = self._X(d, T, True)
        lam = np.r_[[self.lam[k] for k, _ in self.cols], [1e-3] * 3]
        A = (X.T @ X + sparse.diags(lam)).tocsc()
        self.b = spsolve(A, X.T @ d["y"].to_numpy(float))
        return self

    def predict(self, d, drop=()):
        T = [[t for t in terms(r) if t[0] not in drop] for r in d.itertuples()]
        return self._X(d, T, False) @ self.b

    def part(self, d, kinds):
        """행마다 그 종류 칸의 몫 (부호 포함)."""
        T = [[t for t in terms(r) if t[0] in kinds] for r in d.itertuples()]
        return self._X(d, T, False) @ self.b


def r2(y, p):
    return float(1 - np.sum((y - p) ** 2) / np.sum((y - y.mean()) ** 2))


def team_sum(d, vals, matches):
    """경기마다 블루 − 레드 합."""
    s = pd.Series(vals * np.where(d["side"].to_numpy() == "Blue", 1.0, -1.0), index=d["match"].to_numpy())
    return s.groupby(level=0).sum().reindex(matches).fillna(0.0).to_numpy()


def logit_k(X, y, off):
    return M2.fit_logit(sparse.csr_matrix(X), y, off, np.full(X.shape[1], 1e-3))


# ---------------------------------------------------------------- 검증
def validate(d):
    w = H.research_windows().set_index("patch")
    G, _ = pd.read_pickle(OUT / "value_input.pkl")
    gwin = G.set_index("matchId")["blue_win"]
    rows, wins, t0 = [], [], time.time()
    for p in PATCHES:
        f = OUT / f"m1_rehearsal_{p}.pkl"
        if not f.exists():
            log.warning("%s 없음 → 승패 검증 건너뜀 (m5_value.py 먼저)", f.name)
        m1_tr, m1_te = pd.read_pickle(f) if f.exists() else (pd.Series(dtype=float), pd.Series(dtype=float))
        cut = w.loc[p, "window_from"]
        tr = d[d["day"] < cut]
        te = d[(d["src"] == "pro") & (d["patch"] == p)]
        prd = tr[tr["src"] == "pro"]
        pd_days = np.sort(prd["day"].unique())
        hcut = pd_days[int(len(pd_days) * 0.85)]
        A = tr[~((tr["src"] == "pro") & (tr["day"] >= hcut))]
        hold = tr[(tr["src"] == "pro") & (tr["day"] >= hcut)]
        prior = Counter(zip(tr["player"], tr["key"]))
        many = np.array([prior[(a, b)] >= 3 for a, b in zip(te["player"], te["key"])])
        y = te["y"].to_numpy()
        Gp = G[G["league"] != "SOLO"].set_index("matchId")
        tm = sorted(set(te["match"]) & set(Gp.index) & set(m1_te.index))
        hm = sorted(set(hold["match"]) & set(Gp.index) & set(m1_tr.index))
        bases = {"통제": (MV.base_off(Gp.loc[hm].reset_index(), MV.fit_base(Gp[Gp["day"] < hcut].reset_index())),
                         MV.base_off(Gp.loc[tm].reset_index(), MV.fit_base(Gp[Gp["day"] < cut].reset_index()))),
                 "M1": (m1_tr.reindex(hm).to_numpy(), m1_te.reindex(tm).to_numpy())}
        for lam in GRID_PC + [BIG]:
            mf = Model(lam).fit(tr)
            pr = mf.predict(te)
            rows.append({"patch": p, "lam_pc": lam, "n": len(te), "n_many": int(many.sum()), "r2": r2(y, pr),
                         "r2_many": r2(y[many], pr[many]) if many.sum() > 30 else np.nan})
            if lam >= BIG or not tm:
                continue
            ma = Model(lam).fit(A)
            # 경기 합: δ (선수×챔피언 + 대회 편차), 선수 평소 수준 (선수 + 선수×출처)
            dh_d, dh_p = team_sum(hold, ma.part(hold, {"pc", "pcp"}), hm), team_sum(hold, ma.part(hold, {"p", "ps"}), hm)
            dt_d, dt_p = team_sum(te, mf.part(te, {"pc", "pcp"}), tm), team_sum(te, mf.part(te, {"p", "ps"}), tm)
            yh, yt = gwin.reindex(hm).to_numpy(float), gwin.reindex(tm).to_numpy(float)
            for bname, (oh, ot) in bases.items():
                for name, Xh, Xt in (("기본", None, None), ("+ 선수 평소 수준", dh_p[:, None], dt_p[:, None]),
                                     ("+ 숙련 편차 δ", dh_d[:, None], dt_d[:, None]),
                                     ("+ 선수 수준 + δ", np.c_[dh_p, dh_d], np.c_[dt_p, dt_d])):
                    if Xh is None:
                        z, k = ot, []
                    else:
                        k = logit_k(Xh * SLOPE, yh, oh)
                        z = ot + (Xt * SLOPE) @ k
                    q = np.clip(1 / (1 + np.exp(-z)), 1e-4, 1 - 1e-4)
                    per = -(yt * np.log(q) + (1 - yt) * np.log(1 - q))
                    for mm, l_ in zip(tm, per):
                        wins.append({"patch": p, "lam_pc": lam, "base": bname, "variant": name, "match": mm, "loss": l_,
                                     "k": list(np.round(k, 3))})
        log.info("  %s 끝 (평가 대회 선수 행 %d, 경기 %d, %.0f초)", p, len(te), len(tm), time.time() - t0)
    return pd.DataFrame(rows), pd.DataFrame(wins)


# ---------------------------------------------------------------- 최종
def final_table(d, lam):
    m = Model(lam).fit(d)
    lck = d[(d["src"] == "pro")]
    lck_players = set(pd.read_csv(M1.OE_FILE, usecols=["league", "playername"], low_memory=False).query("league == 'LCK'")["playername"].dropna())
    out = []
    n_pro = Counter(zip(d.loc[d["src"] == "pro", "player"], d.loc[d["src"] == "pro", "key"]))
    n_solo = Counter(zip(d.loc[d["src"] == "solo", "player"], d.loc[d["src"] == "solo", "key"]))
    ko = M2.ko_names()
    for (kind, key), j in m.idx.items():
        if kind != "pc":
            continue
        pl, ck = key.split("|", 1)
        if pl not in lck_players:
            continue
        jp = m.idx.get(("pcp", key))
        dlt = m.b[j] + (m.b[jp] if jp is not None else 0.0)
        out.append({"player": pl, "key": ck, "a": M2.label(ck, ko), "n_pro": n_pro.get((pl, ck), 0), "n_solo": n_solo.get((pl, ck), 0),
                    "delta_gold": dlt * 1000, "delta_common": m.b[j] * 1000, "delta_win_pp": dlt * SLOPE * PP})
    t = pd.DataFrame(out)
    pl_lvl = {key: m.b[j] for (kind, key), j in m.idx.items() if kind == "p"}
    t["player_level_gold"] = t["player"].map(pl_lvl).fillna(0.0) * 1000
    return t.sort_values(["player", "delta_gold"], ascending=[True, False]), m


def boot_diff(W, var, ref, n=2000):
    a = W[W["variant"] == var].set_index(["patch", "match"])["loss"]
    b = W[W["variant"] == ref].set_index(["patch", "match"])["loss"].reindex(a.index)
    dd = (a - b).to_numpy()
    rng = np.random.default_rng(0)
    bs = dd[rng.integers(0, len(dd), (n, len(dd)))].mean(1)
    return dd.mean(), *np.percentile(bs, [2.5, 97.5]), len(dd)


def main():
    t0 = time.time()
    d = load_rows()
    log.info("자료 (%.0f초): 대회 %d · 솔랭 %d 행", time.time() - t0, (d["src"] == "pro").sum(), (d["src"] == "solo").sum())
    R, W = validate(d)
    H.save_csv(R, OUT / "m5_skill_selection.csv")
    H.save_csv(W, OUT / "m5_skill_wins.csv")
    rs = R[R["patch"].isin(SEL)].groupby("lam_pc")[["r2", "r2_many"]].mean()
    lam = float(rs.drop(index=BIG, errors="ignore")["r2"].idxmax())
    log.info("고른 δ 규제 %g (%.0f초)", lam, time.time() - t0)
    T, m = final_table(d, lam)
    H.save_csv(T, OUT / "m5_skill.csv")
    (OUT / "m5_skill_config.json").write_text(json.dumps({"lam_pc": lam, "pro_dev": PRO_DEV, "lam": LAM, "slope": SLOPE}, indent=1), encoding="utf-8")
    report(R, W, lam, T)
    log.info("끝 (%.0f초)", time.time() - t0)


def report(R, W, lam, T):
    L = ["# M5 4단계 — 선수 × 챔피언 숙련 편차 δ", "",
         "- 결과 = 선수 경기 1행, 라인 상대 대비 15분 골드 차. 대회 5리그 + 리서치 선수 58명 솔랭",
         "- 기준 칸: 챔피언@포지션(+패치), 상대 챔피언, 선수 평소 수준(+출처), 상대 선수, 팀 Elo. δ = 선수×챔피언 (공통 + 대회 편차 ×3 규제)",
         "- 시간 분할: 평가 패치마다 그 패치 창 전 자료로 학습. δ 규제는 26.14 · 26.15 골드 R² 로 고름. 플레이오프 안 봄", "",
         "## 1. 15분 골드 설명력 (평가 패치 대회 선수 행)", "",
         "| δ 규제 | " + " | ".join(PATCHES) + " | 평균 | 이전 판수 3+ 행 평균 |", "|---" * (len(PATCHES) + 3) + "|"]
    for lam_, g in R.groupby("lam_pc"):
        g = g.set_index("patch")
        nm = "δ 없음" if lam_ >= BIG else f"{lam_:g}"
        mk = "**" if lam_ == lam else ""
        L.append(f"| {mk}{nm}{mk} | " + " | ".join(f"{g.loc[p, 'r2']:.4f}" if p in g.index else "—" for p in PATCHES)
                 + f" | {g['r2'].mean():.4f} | {g['r2_many'].mean():.4f} |")
    n_many = R.groupby("patch")["n_many"].first()
    L += ["", "- 이전 판수 3+ 행 = 그 선수가 그 챔피언을 학습 기간에 3판 이상 한 평가 행: " + ", ".join(f"{p} {n_many.get(p, 0)}/{R[R['patch'] == p]['n'].iloc[0]}" for p in PATCHES if p in n_many.index), ""]
    for bname, desc in (("통제", "통제 (리그·Elo·대회 숙련 경기 수)"), ("M1", "M1 성격 예측 (리허설 시점)")):
        Wl = W[(W["lam_pc"] == lam) & (W["base"] == bname)] if len(W) else W
        if not len(Wl):
            continue
        L += [f"## 2-{bname}. 승패 — 기본 = {desc}, 평가 패치 대회 경기", "", "| 변형 | 경기 | 로그 손실 | 기본과 차이 [95%] |", "|---|---|---|---|"]
        for v in Wl["variant"].unique():
            mean = Wl[Wl["variant"] == v]["loss"].mean()
            if v == "기본":
                L.append(f"| {v} | {Wl[Wl['variant'] == v].shape[0]} | {mean:.4f} | — |")
            else:
                dd, lo, hi, n = boot_diff(Wl, v, "기본")
                L.append(f"| {v} | {n} | {mean:.4f} | {dd:+.4f} [{lo:+.4f}, {hi:+.4f}] |")
        L += ["", "| 패치 | " + " | ".join(Wl["variant"].unique()) + " |", "|---" * (1 + Wl["variant"].nunique()) + "|"]
        for p in PATCHES:
            g = Wl[Wl["patch"] == p]
            if len(g):
                L.append(f"| {p} ({g['match'].nunique()}경기) | " + " | ".join(f"{g[g['variant'] == v]['loss'].mean():.4f}" for v in Wl["variant"].unique()) + " |")
        L.append("")
    if len(W):
        L += ["### δ 규제별 (기본 = 통제, δ 더한 것 − 기본, 5개 패치 합)", "", "| δ 규제 | 차이 [95%] |", "|---|---|"]
        for lam_ in sorted(W["lam_pc"].unique()):
            Wb = W[(W["lam_pc"] == lam_) & (W["base"] == "통제")]
            dd, lo, hi, n = boot_diff(Wb, "+ 숙련 편차 δ", "기본")
            L.append(f"| {lam_:g} | {dd:+.4f} [{lo:+.4f}, {hi:+.4f}] |")
        L.append("")
    L += ["## 3. 숙련 편차 예 (최종, 08-25 까지 전부, LCK 선수, 대회+솔랭 10판 이상)", ""]
    t = T[(T["n_pro"] + T["n_solo"]) >= 10]
    for pl in ("Faker", "Zeus", "Oner", "Gumayusi", "Keria", "Doran", "Peyz", "Chovy", "Canyon", "Kiin"):
        tp = t[t["player"] == pl]
        if len(tp):
            hi_ = tp.sort_values("delta_gold", ascending=False).head(3)
            lo_ = tp.sort_values("delta_gold").head(2)
            L.append(f"- **{pl}** 잘하는: " + ", ".join(f"{r.a} {r.delta_gold:+.0f}골드 ({r.n_pro}+{r.n_solo}판)" for r in hi_.itertuples())
                     + " / 덜 하는: " + ", ".join(f"{r.a} {r.delta_gold:+.0f}골드 ({r.n_pro}+{r.n_solo}판)" for r in lo_.itertuples()))
    (OUT / "m5_skill_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
