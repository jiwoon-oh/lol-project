"""M5 2단계 — 이길 가치를 시간대별 중간 결과로 다시 재기 (2026-11-08)

문제: 지금 이길 가치(m4_model.win_value_table = M2 챔피언 단독 + M1 성격)는 승패(0/1)만 보고 추정 → 대회 표본이 적어 흔들림,
      시간대(초반·후반) 구분 없음
방법 (경로 분해, 같은 챔피언 칸 구조)
  ① 상태 → 승률 환산: 출처(솔랭/대회)·시점(10·15·20·25분)마다 승리 ~ 팀 골드 차 (학습 자료로만)
     L_t = 그 시점 골드 차로 본 승리 로그 오즈. **t 전에 끝난 경기 = 이미 결정 → ±K (K = 3.5 ≈ 97%)** (일찍 끝난 판을 빼지 않음)
  ② 시점마다 L_t ~ 챔피언@포지션(공통 + 대회 편차 [+ 챔피언×패치 편차]) + 통제(리그 절편, 솔랭 프로 수, 대회 Elo·숙련) — 릿지
  ③ 후반(같은 상태에서 이기는 몫): 승리 ~ 오프셋 L_25 + 챔피언 + 통제 — 로지스틱, 강한 규제
  챔피언 값 V = β_25 + γ,  나눔 = 초반(~10분) β_10 · 10~15 · 15~20 · 20~25 · 25분 이후·같은 상태 γ
비교 (리허설 26.14 · 26.15, 학습 = 그 패치 창 전, 평가 = 그 패치 경기. 플레이오프 안 봄)
  A 통제만 · B 지금 이길 가치(win_value_table, 08-25 까지 전부로 만든 값이라 학습 범위가 겹침 — 참고)
  D 같은 챔피언 칸을 승패만으로 (로지스틱) · C 위 경로 분해
  예측 = 통제 모델 + k × (블루 − 레드 챔피언 값 합). k(대회·솔랭 따로)는 학습 기간 마지막 15% 날짜로 맞춤 (평가 경기 안 봄)
  안정성: 경기를 반으로 나눠 두 번 맞춘 챔피언 값의 상관 (C vs D)
설명 표 (최종, 08-25 까지 전부): 오브젝트(드래곤·유충·전령·포탑·포탑 방패·바론·아타칸·첫 획득) 차이, 솔랭 시간대별 교전(0~10·10~20·20분~)
  → 이길 가치 합계에는 넣지 않음 (골드·승리 경로와 겹쳐 두 번 세게 됨)
출력: m5/m5_value_champion.csv (m5_sim 이 읽음), m5_value_objectives.csv, m5_value_fights.csv, m5_value_selection.csv, m5_value_eval.md

    python src/pipeline/m5_value.py
"""
from __future__ import annotations

import gzip
import itertools
import json
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m2_synergy as M2  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m5"
POS = H.POS
MINS = (10, 15, 20, 25)
K_END = 3.5
PP = 25.0                     # 로그 오즈 → %p (50% 근처)
GRID_C = {"lam": [100.0, 300.0, 1000.0, 3000.0, 10000.0], "chp": [False, True], "lam_late": [30.0, 100.0, 300.0, 1000.0, 3000.0]}
GRID_D = {"lam": [100.0, 300.0, 1000.0, 3000.0, 10000.0], "chp": [False, True]}
OBJ = ["dragons", "void_grubs", "heralds", "towers", "turretplates", "barons", "atakhans"]
FIRST = ["first_dragon", "first_tower", "first_herald"]
SOLO_OBJ = {"dragon": "dragons", "horde": "void_grubs", "riftHerald": "heralds", "tower": "towers", "baron": "barons", "atakhan": "atakhans"}
WINDOWS = [("0~10분", 0, 600), ("10~20분", 600, 1200), ("20분~", 1200, 10 ** 6)]
SEL_PATCHES = ("26.14", "26.15")   # 설정 고르기 (decisions 2026-11-04)
# 평가 패치: 2026-11-08 리허설 확대 — 26.08·26.09·26.10 추가 (대회 361 → 약 970판). 설정은 SEL_PATCHES 로만 고르고 나머지는 평가에만
REH_PATCHES = (sys.argv[sys.argv.index("--patches") + 1].split(",") if "--patches" in sys.argv else ["26.08", "26.09", "26.10", "26.14", "26.15"])
LOSS = {}                          # (패치, 변형, 설정) → (대회 경기별 로그 손실, 솔랭 경기별 로그 손실)
if "--quick" in sys.argv:     # 코드 점검용 (격자 1칸씩, 결과는 m5/quick/)
    GRID_C = {"lam": [100.0], "chp": [False], "lam_late": [3000.0]}
    GRID_D = {"lam": [100.0], "chp": [False]}
    OUT = OUT / "quick"


# ---------------------------------------------------------------- 자료
def solo_objectives() -> pd.DataFrame:
    f = PROCESSED / "m5" / "solo_objectives.csv"
    if f.exists():
        return pd.read_csv(f, dtype={"matchId": str})
    rows = []
    for p in H.MATCH_DIR.glob("*.json.gz"):
        try:
            info = json.load(gzip.open(p))["info"]
        except Exception:
            continue
        t = {x.get("teamId"): x.get("objectives", {}) for x in info.get("teams", [])}
        if 100 not in t or 200 not in t:
            continue
        r = {"matchId": p.name[:-8]}
        for k, nm in SOLO_OBJ.items():
            r[nm] = t[100].get(k, {}).get("kills", 0) - t[200].get(k, {}).get("kills", 0)
        r["first_dragon"] = int(t[100].get("dragon", {}).get("first", False))
        r["first_tower"] = int(t[100].get("tower", {}).get("first", False))
        r["first_herald"] = int(t[100].get("riftHerald", {}).get("first", False))
        rows.append(r)
    d = pd.DataFrame(rows)
    H.save_csv(d, f)
    return d


def pro_team() -> pd.DataFrame:
    cols = ["gameid", "league", "date", "side", "position", "datacompleteness", "firstdragon", "firsttower", "firstherald"] + \
           [f"golddiffat{m}" for m in MINS] + OBJ + [f"opp_{o}" for o in OBJ]
    oe = pd.read_csv(M1.OE_FILE, usecols=cols, low_memory=False)
    oe = oe[oe["league"].isin(M1.LEAGUES) & (oe["date"].str[:10] <= M1.END) & (oe["position"] == "team") & (oe["side"] == "Blue")]
    d = pd.DataFrame({"matchId": oe["gameid"].astype(str).to_numpy()})
    comp = (oe["datacompleteness"] == "complete").to_numpy()
    for m in MINS:
        d[f"gd{m}"] = np.where(comp, oe[f"golddiffat{m}"].to_numpy(float) / 1000, np.nan)
    for o in OBJ:
        d[o] = np.where(comp, oe[o].to_numpy(float) - oe[f"opp_{o}"].to_numpy(float), np.nan)
    for a, b in (("first_dragon", "firstdragon"), ("first_tower", "firsttower"), ("first_herald", "firstherald")):
        d[a] = np.where(comp, oe[b].to_numpy(float), np.nan)
    return d


def load():
    import m2_fights
    G = pd.read_csv(PROCESSED / "m1" / "m1_games.csv", dtype={"patch": str, "matchId": str}, low_memory=False)
    G = G[["matchId", "league", "day", "patch", "blue_win", "pro_diff", "elo_diff", "mastery_diff"]]
    pro = M1.pro_players()
    solo, _ = M1.solo_players()
    players = pd.concat([solo, pro.drop(columns=["pid"])], ignore_index=True)
    S = M2.slots(players, G)
    ok = S.notna().all(axis=1).to_numpy()
    G, S = G[ok].reset_index(drop=True), S[ok].reset_index(drop=True)
    sg = m2_fights.team_gold()
    sg.columns = [f"gd{m}" for m in sg.columns]
    so = solo_objectives().set_index("matchId")
    solo_part = sg.join(so, how="outer")
    pt = pro_team().set_index("matchId")
    allx = pd.concat([solo_part, pt])
    allx = allx[~allx.index.duplicated()]
    G = G.join(allx.reindex(G["matchId"]).reset_index(drop=True))
    log.info("경기 %d (대회 %d) · 10분 골드 있음 %d · 25분 %d", len(G), (G["league"] != "SOLO").sum(), G["gd10"].notna().sum(), G["gd25"].notna().sum())
    pd.to_pickle((G, S), PROCESSED / "m5" / "value_input.pkl")
    pd.to_pickle(players, PROCESSED / "m5" / "value_players.pkl")
    return G, S


def load_cached():
    """자료 준비(약 6분) 결과 캐시. 원본(m1_games·OE·솔랭)을 다시 만들었으면 value_input.pkl · value_players.pkl 을 지우고 다시."""
    f = PROCESSED / "m5" / "value_input.pkl"
    G, S = pd.read_pickle(f) if f.exists() else load()
    m1 = pd.read_csv(PROCESSED / "m1" / "m1_games.csv", usecols=["matchId", "m1_pred_oof"], dtype={"matchId": str})
    G["m1"] = M2.logit(G["matchId"].map(m1.set_index("matchId")["m1_pred_oof"]).fillna(0.5).to_numpy())   # 최종 표용 (전체 교차 예측)
    return G, S


def load_players():
    f = PROCESSED / "m5" / "value_players.pkl"
    if f.exists():
        return pd.read_pickle(f)
    pro = M1.pro_players()
    solo, _ = M1.solo_players()
    players = pd.concat([solo, pro.drop(columns=["pid"])], ignore_index=True)
    pd.to_pickle(players, f)
    return players


def m1_rehearsal(players, G, Gtr, Gte, p):
    """M1 성격 모델을 패치 p 창 전 자료로 (그 시점 속성표) → 학습 경기 교차 예측 · 평가 경기 예측 (로그 오즈). M2 리허설과 같은 방식.
    결과는 m5/m1_rehearsal_{p}.pkl 에 저장 (점검 단계에서 다시 씀)."""
    f = PROCESSED / "m5" / f"m1_rehearsal_{p}.pkl"
    if f.exists():
        a, b = pd.read_pickle(f)
    else:
        ctrl = {c: G.set_index("matchId")[c].to_dict() for c in ("pro_diff", "elo_diff", "mastery_diff")}
        g = M1.game_rows(players[players["matchId"].isin(set(Gtr["matchId"]) | set(Gte["matchId"]))],
                         M1.load_attr(M1.M0 / f"m0_attributes_asof_{p}.csv"), ctrl)
        tr_g = g[g["matchId"].isin(set(Gtr["matchId"]))].reset_index(drop=True)
        te_g = g[g["matchId"].isin(set(Gte["matchId"]))].reset_index(drop=True)
        oof, pte = M2.m1_offsets(tr_g, te_g, M1.load_model().params())
        a = pd.Series(M2.logit(oof), index=tr_g["matchId"])
        b = pd.Series(M2.logit(pte), index=te_g["matchId"])
        pd.to_pickle((a, b), f)
    log.info("  M1 리허설 예측 %s: 학습 %d/%d · 평가 %d/%d 경기", p, len(a), len(Gtr), len(b), len(Gte))
    return Gtr["matchId"].map(a).fillna(0.0).to_numpy(), Gte["matchId"].map(b).fillna(0.0).to_numpy()


# ---------------------------------------------------------------- 설계 행렬
def ch_terms(row, patch, chp):
    out = []
    for team, v in ((row[:5], 1.0), (row[5:], -1.0)):
        for k in team:
            out.append(("ch", k, v))
            if chp:
                out.append(("chp", f"{k}|{patch}", v))
    return out


def controls(G):
    pro = (G["league"] != "SOLO").to_numpy(float)
    lg = np.column_stack([(G["league"] == L).to_numpy(float) for L in ["SOLO"] + M1.LEAGUES])
    return np.column_stack([lg, G["pro_diff"].fillna(0).to_numpy() * (1 - pro), G["elo_diff"].fillna(0).to_numpy() * pro,
                            G["mastery_diff"].fillna(0).to_numpy() * pro])


class Design:
    """use_m1 = M1 성격 예측(로그 오즈, G['m1'])을 통제 칸으로 → 챔피언 칸은 조합 성격으로 설명 안 되는 나머지."""

    def __init__(self, G, S, chp, coder=None, use_m1=False):
        T = [ch_terms(r, p, chp) for r, p in zip(S.to_numpy(), G["patch"].astype(str))]
        isp = (G["league"] != "SOLO").to_numpy()
        self.coder = coder or M2.Coder(T, isp)
        self.Xc = self.coder.matrix(T, isp)
        self.C = controls(G)
        if use_m1:
            self.C = np.column_stack([self.C, G["m1"].to_numpy(float)])
        self.X = sparse.hstack([self.Xc, sparse.csr_matrix(self.C)]).tocsr()

    def pen(self, lam):
        return np.r_[self.coder.penalty({"ch": lam, "chp": lam * M2.PATCH_MULT}), np.full(self.C.shape[1], 1e-3)]

    def vdiff(self, V):
        return self.Xc @ V


def conversion(G):
    """시점·출처별 승리 ~ 골드 차 (그 시점에 살아 있던 경기)."""
    conv = {}
    for src, m_ in (("solo", G["league"] == "SOLO"), ("pro", G["league"] != "SOLO")):
        for m in MINS:
            t = G[m_ & G[f"gd{m}"].notna()]
            lr = LogisticRegression(C=1e4).fit(t[[f"gd{m}"]].to_numpy(), t["blue_win"])
            conv[(src, m)] = (float(lr.intercept_[0]), float(lr.coef_[0][0]))
    return conv


def targets(G, conv, k_end=K_END):
    L = {}
    alive10 = G["gd10"].notna().to_numpy()
    src = np.where(G["league"] == "SOLO", "solo", "pro")
    y = G["blue_win"].to_numpy(float)
    for m in MINS:
        g = G[f"gd{m}"].to_numpy(float)
        a = np.array([conv[(s, m)][0] for s in src])
        b = np.array([conv[(s, m)][1] for s in src])
        v = a + b * g
        ended = np.isnan(g) & alive10
        v[ended] = np.where(y[ended] == 1, k_end, -k_end)
        v[~alive10] = np.nan
        L[m] = v
    return L


# ---------------------------------------------------------------- 모델
def fit_path(G, D, lam, lam_late, k_end=K_END):
    conv = conversion(G)
    L = targets(G, conv, k_end)
    y = G["blue_win"].to_numpy(float)
    beta = {}
    b0 = None
    for m in MINS:
        ok = ~np.isnan(L[m])
        b0 = M2.fit_gauss(D.X[ok], L[m][ok], D.pen(lam), b0)
        beta[m] = b0.copy()
    ok = ~np.isnan(L[25])
    gam = M2.fit_logit(D.X[ok], y[ok], L[25][ok], D.pen(lam_late))
    w = D.coder.width
    V = beta[25][:w] + gam[:w]
    return {"V": V, "beta": {m: b[:w] for m, b in beta.items()}, "gam": gam[:w], "conv": conv, "beta_full": beta}


def fit_binary(G, D, lam, off=None):
    y = G["blue_win"].to_numpy(float)
    b = M2.fit_logit(D.X, y, np.zeros(len(y)) if off is None else off, D.pen(lam))
    return {"V": b[:D.coder.width]}


def fit_base(G):
    C = sparse.csr_matrix(controls(G))
    return M2.fit_logit(C, G["blue_win"].to_numpy(float), np.zeros(len(G)), np.full(C.shape[1], 1e-3))


def base_off(G, b):
    return controls(G) @ b


def fit_k(vd, y, off, is_pro):
    X = sparse.csr_matrix(np.column_stack([vd * is_pro, vd * (1 - is_pro)]))
    return M2.fit_logit(X, y, off, np.array([1e-3, 1e-3]))


def predict(vd, off, k, is_pro):
    return M2.sigm(off + k[0] * vd * is_pro + k[1] * vd * (1 - is_pro))


# ---------------------------------------------------------------- 지금 이길 가치 (참고 B)
def old_values(S):
    import m4_model as MM
    wv = MM.win_value_table()
    out = []
    for row in S.to_numpy():
        s = 0.0
        for k, v in zip(row, [1.0] * 5 + [-1.0] * 5):
            c, p = k.split("@")
            s += v * wv.get((c, p), 0.0)
        out.append(s)
    return np.array(out)


# ---------------------------------------------------------------- 리허설
def rehearsal(G, S, players):
    w = H.research_windows().set_index("patch")
    sel, t0 = [], time.time()
    for p in REH_PATCHES:
        cut = w.loc[p, "window_from"]
        te = (G["patch"] == p).to_numpy()
        tr = ((G["day"] < cut) & ~te).to_numpy()
        Gtr, Str, Gte, Ste = G[tr].reset_index(drop=True), S[tr].reset_index(drop=True), G[te].reset_index(drop=True), S[te].reset_index(drop=True)
        Gtr["m1"], Gte["m1"] = m1_rehearsal(players, G, Gtr, Gte, p)   # 최종 표용 전체 교차 예측을 리허설 시점 값으로 바꿈
        # k 를 맞출 구간 = 출처(대회·솔랭)마다 학습 기간 마지막 15% 경기 (날짜로 자르면 MSI 휴식기에 대회 경기가 0판이 될 수 있음)
        A = np.ones(len(Gtr), bool)
        for m_ in ((Gtr["league"] == "SOLO").to_numpy(), (Gtr["league"] != "SOLO").to_numpy()):
            d = np.sort(Gtr.loc[m_, "day"].to_numpy())
            A &= ~(m_ & (Gtr["day"] >= d[int(len(d) * 0.85)]).to_numpy())
        Ga, Sa, Gh, Sh = Gtr[A].reset_index(drop=True), Str[A].reset_index(drop=True), Gtr[~A].reset_index(drop=True), Str[~A].reset_index(drop=True)
        yh, yte = Gh["blue_win"].to_numpy(float), Gte["blue_win"].to_numpy(float)
        ph, pte = (Gh["league"] != "SOLO").to_numpy(float), (Gte["league"] != "SOLO").to_numpy(float)
        ba, bf = fit_base(Ga), fit_base(Gtr)
        offh, offte = base_off(Gh, ba), base_off(Gte, bf)

        def record(name, cfg, pr, k):
            ip = pte == 1
            sel.append({"patch": p, "variant": name, "cfg": cfg, "k_pro": k[0], "k_solo": k[1],
                        "ll_pro": M2.ll(yte[ip], pr[ip]), "ll_solo": M2.ll(yte[~ip], pr[~ip]), "n_pro": int(ip.sum()), "n_solo": int((~ip).sum())})
            q = np.clip(pr, 1e-4, 1 - 1e-4)
            per = -(yte * np.log(q) + (1 - yte) * np.log(1 - q))
            LOSS[(p, name, cfg)] = (per[ip], per[~ip])
        record("A 통제만", "", M2.sigm(offte), (0.0, 0.0))
        record("M1 성격만", "", M2.sigm(Gte["m1"].to_numpy()), (0.0, 0.0))
        vo_h, vo_te = old_values(Sh), old_values(Ste)
        k = fit_k(vo_h, yh, offh, ph)
        record("B 지금 이길 가치 (참고·겹침)", "", predict(vo_te, offte, k, pte), k)
        for chp in GRID_D["chp"]:
            Da = Design(Ga, Sa, chp)
            Dh = Design(Gh, Sh, chp, Da.coder)
            Dtr = Design(Gtr, Str, chp)
            Dte = Design(Gte, Ste, chp, Dtr.coder)
            for lam in GRID_D["lam"]:
                ma, mf = fit_binary(Ga, Da, lam), fit_binary(Gtr, Dtr, lam)
                k = fit_k(Dh.vdiff(ma["V"]), yh, offh, ph)
                record("D 승패만", f"lam {lam:g} · 패치 {'O' if chp else 'X'}", predict(Dte.vdiff(mf["V"]), offte, k, pte), k)
            for lam, ll_ in itertools.product(GRID_C["lam"], GRID_C["lam_late"]):
                ma, mf = fit_path(Ga, Da, lam, ll_), fit_path(Gtr, Dtr, lam, ll_)
                k = fit_k(Dh.vdiff(ma["V"]), yh, offh, ph)
                record("C 시간대 경로", f"lam {lam:g} · 후반 {ll_:g} · 패치 {'O' if chp else 'X'}", predict(Dte.vdiff(mf["V"]), offte, k, pte), k)
            # E = M1 성격 예측을 기본(오프셋)으로, 챔피언 칸은 그 나머지
            mh, mte = Gh["m1"].to_numpy(), Gte["m1"].to_numpy()
            for lam in GRID_D["lam"]:
                ma = fit_binary(Ga, Da, lam, Ga["m1"].to_numpy())
                mf = fit_binary(Gtr, Dtr, lam, Gtr["m1"].to_numpy())
                k = fit_k(Dh.vdiff(ma["V"]), yh, mh, ph)
                record("E-D M1 + 승패만", f"lam {lam:g} · 패치 {'O' if chp else 'X'}", predict(Dte.vdiff(mf["V"]), mte, k, pte), k)
            DaE, DtrE = Design(Ga, Sa, chp, use_m1=True), Design(Gtr, Str, chp, use_m1=True)
            for lam, ll_ in itertools.product(GRID_C["lam"], GRID_C["lam_late"]):
                ma, mf = fit_path(Ga, DaE, lam, ll_), fit_path(Gtr, DtrE, lam, ll_)
                k = fit_k(Dh.vdiff(ma["V"]), yh, mh, ph)
                record("E-C M1 + 시간대 경로", f"lam {lam:g} · 후반 {ll_:g} · 패치 {'O' if chp else 'X'}",
                       predict(Dte.vdiff(mf["V"]), mte, k, pte), k)
            log.info("  %s 패치칸 %s 끝 (%.0f초)", p, chp, time.time() - t0)
    return pd.DataFrame(sel)


# ---------------------------------------------------------------- 점검 (채택 설정 E-C 하나로)
LAM_APX = 10.0      # M1 예측을 챔피언 합으로 근사할 때 규제
K_GRID = [2.0, 3.5, 5.0]


def patch_split(G, S, players, p):
    w = H.research_windows().set_index("patch")
    cut = w.loc[p, "window_from"]
    te = (G["patch"] == p).to_numpy()
    tr = ((G["day"] < cut) & ~te).to_numpy()
    Gtr, Str, Gte, Ste = G[tr].reset_index(drop=True), S[tr].reset_index(drop=True), G[te].reset_index(drop=True), S[te].reset_index(drop=True)
    Gtr["m1"], Gte["m1"] = m1_rehearsal(players, G, Gtr, Gte, p)
    A = np.ones(len(Gtr), bool)
    for m_ in ((Gtr["league"] == "SOLO").to_numpy(), (Gtr["league"] != "SOLO").to_numpy()):
        d = np.sort(Gtr.loc[m_, "day"].to_numpy())
        A &= ~(m_ & (Gtr["day"] >= d[int(len(d) * 0.85)]).to_numpy())
    r = dict(Gtr=Gtr, Str=Str, Gte=Gte, Ste=Ste, Ga=Gtr[A].reset_index(drop=True), Sa=Str[A].reset_index(drop=True),
             Gh=Gtr[~A].reset_index(drop=True), Sh=Str[~A].reset_index(drop=True))
    return r


def m1_approx(Gfit, Sfit, Gs_Ss):
    """M1 예측(로그 오즈)을 챔피언 칸 합 + 통제로 근사 (시험판은 챔피언 하나씩 값만 더할 수 있음). Gs_Ss = [(G, S)] 에 대해 근사값."""
    D = Design(Gfit, Sfit, False)
    b = M2.fit_gauss(D.X, Gfit["m1"].to_numpy(float), D.pen(LAM_APX))
    return [Design(g, s, False, D.coder).X @ b for g, s in Gs_Ss], D, b


def attr_diff(S, m1v):
    return np.array([sum(v * m1v.get(k, 0.0) for k, v in zip(row, [1.0] * 5 + [-1.0] * 5)) for row in S.to_numpy()])


def r2(y, p):
    ok = ~np.isnan(y)
    return float(1 - np.sum((y[ok] - p[ok]) ** 2) / np.sum((y[ok] - y[ok].mean()) ** 2))


def checks(G, S, players, cfg):
    """① 시험판 방식(챔피언 하나씩 값의 합)이 실제 M1 + 경로만큼 맞히나 ② 시점별 중간 결과 R² ③ K 민감도."""
    m1v = m1_attr_values()
    out, r2rows = [], []
    for p in ("26.14", "26.15"):
        d = patch_split(G, S, players, p)
        Ga, Sa, Gh, Sh, Gtr, Str, Gte, Ste = (d[k] for k in ("Ga", "Sa", "Gh", "Sh", "Gtr", "Str", "Gte", "Ste"))
        yh, yte = Gh["blue_win"].to_numpy(float), Gte["blue_win"].to_numpy(float)
        ph, pte = (Gh["league"] != "SOLO").to_numpy(float), (Gte["league"] != "SOLO").to_numpy(float)
        ip = pte == 1

        def rec(name, pr, extra=None):
            out.append({"patch": p, "check": name, "ll_pro": M2.ll(yte[ip], pr[ip]), "ll_solo": M2.ll(yte[~ip], pr[~ip]), **(extra or {})})
        DaE, DtrE = Design(Ga, Sa, cfg["chp"], use_m1=True), Design(Gtr, Str, cfg["chp"], use_m1=True)
        Dh, DteE = Design(Gh, Sh, cfg["chp"], DaE.coder, use_m1=True), Design(Gte, Ste, cfg["chp"], DtrE.coder, use_m1=True)
        for kk in K_GRID:
            ma, mf = fit_path(Ga, DaE, cfg["lam"], cfg["lam_late"], kk), fit_path(Gtr, DtrE, cfg["lam"], cfg["lam_late"], kk)
            path_h, path_te = Dh.vdiff(ma["V"]), DteE.vdiff(mf["V"])
            k = fit_k(path_h, yh, Gh["m1"].to_numpy(), ph)
            rec(f"K {kk:g} · 실제 M1 + 경로", predict(path_te, Gte["m1"].to_numpy(), k, pte), {"k_pro": k[0]})
            if kk != K_END:
                continue
            # ① 시험판 방식: M1 을 챔피언 합으로 근사
            (apx_h,), _, _ = m1_approx(Ga, Sa, [(Gh, Sh)])
            (apx_te, apx_tr), _, _ = m1_approx(Gtr, Str, [(Gte, Ste), (Gtr, Str)])
            k2 = fit_k(path_h, yh, apx_h, ph)
            rec("시험판 방식: M1 챔피언 합 근사 + 경로", predict(path_te, apx_te, k2, pte),
                {"k_pro": k2[0], "apx_r2_pro": r2(Gte["m1"].to_numpy()[ip], apx_te[ip]), "apx_r2_solo": r2(Gte["m1"].to_numpy()[~ip], apx_te[~ip]),
                 "apx_r2_train": r2(Gtr["m1"].to_numpy(), apx_tr)})
            rec("M1 챔피언 합 근사만", M2.sigm(apx_te))
            # 예전 시험판 방식: 속성 계수 선형 근사 (M1 계수가 08-25 까지 전부 → 겹침)
            ba = fit_base(Ga)
            Xh = sparse.csr_matrix(np.column_stack([attr_diff(Sh, m1v), path_h * ph, path_h * (1 - ph)]))
            kk3 = M2.fit_logit(Xh, yh, base_off(Gh, ba), np.full(3, 1e-3))
            bf = fit_base(Gtr)
            z = base_off(Gte, bf) + kk3[0] * attr_diff(Ste, m1v) + kk3[1] * path_te * pte + kk3[2] * path_te * (1 - pte)
            rec("예전 시험판 방식: 속성 계수 근사 + 경로 (겹침)", M2.sigm(z), {"k_pro": kk3[1]})
            # ② 시점별 R² (평가 경기, 환산은 학습 자료로)
            Lte = targets(Gte, mf["conv"], kk)
            Ltr = targets(Gtr, mf["conv"], kk)
            for t_ in MINS:
                ok = ~np.isnan(Ltr[t_])
                bb = M2.fit_gauss(sparse.csr_matrix(DtrE.C[ok]), Ltr[t_][ok], np.full(DtrE.C.shape[1], 1e-3))
                pb, pf = DteE.C @ bb, DteE.X @ mf["beta_full"][t_]
                for src, msk in (("대회", ip), ("솔랭", ~ip)):
                    y_ = Lte[t_][msk]
                    alive = ~np.isnan(Gte[f"gd{t_}"].to_numpy()[msk])
                    r2rows.append({"patch": p, "minute": t_, "source": src, "n": int((~np.isnan(y_)).sum()),
                                   "ended_share": float(1 - alive[~np.isnan(y_)].mean()) if (~np.isnan(y_)).any() else np.nan,
                                   "r2_base": r2(y_, pb[msk]), "r2_full": r2(y_, pf[msk])})
        log.info("  점검 %s 끝", p)
    return pd.DataFrame(out), pd.DataFrame(r2rows)


def parse_cfg(s):
    d = {}
    for part in s.split(" · "):
        k, v = part.split(" ")
        d[k] = v
    return d


# ---------------------------------------------------------------- 안정성 (반으로 나눠 두 번)
def ch_values(D, V):
    """챔피언 칸 → (공통 값, 대회 값, 판수, 대회 판수)."""
    out = {}
    for (kind, key), j in D.coder.idx.items():
        if kind == "ch":
            vp = D.coder.idx_pro.get((kind, key))
            out[key] = (V[j], V[j] + (V[vp] if vp is not None else 0.0), D.coder.n.get((kind, key), 0), D.coder.n_pro.get((kind, key), 0))
    return out


def split_half(G, S, cfgs):
    """cfgs = {이름: (종류, 설정)}. 종류 C·D·EC·ED."""
    half = G["matchId"].map(lambda s: zlib.crc32(("half" + s).encode()) % 2).to_numpy()
    out = {}
    for name, (kind, cfg) in cfgs.items():
        Vs = []
        for h in (0, 1):
            g, s = G[half == h].reset_index(drop=True), S[half == h].reset_index(drop=True)
            D = Design(g, s, cfg["chp"], use_m1=(kind == "EC"))
            if kind in ("C", "EC"):
                m = fit_path(g, D, cfg["lam"], cfg["lam_late"])
            else:
                m = fit_binary(g, D, cfg["lam"], g["m1"].to_numpy() if kind == "ED" else None)
            Vs.append(ch_values(D, m["V"]))
        common = [k for k in Vs[0] if k in Vs[1] and min(Vs[0][k][2], Vs[1][k][2]) >= 50]
        pro_k = [k for k in common if min(Vs[0][k][3], Vs[1][k][3]) >= 15]
        out[name] = {"n": len(common), "r": float(np.corrcoef([Vs[0][k][0] for k in common], [Vs[1][k][0] for k in common])[0, 1]),
                     "n_pro": len(pro_k),
                     "r_pro": float(np.corrcoef([Vs[0][k][1] for k in pro_k], [Vs[1][k][1] for k in pro_k])[0, 1]) if len(pro_k) > 5 else np.nan}
    return out


# ---------------------------------------------------------------- 최종 표
def m1_attr_values() -> dict:
    """M1 조합 성격 몫을 챔피언 하나로 근사 (m4_model.win_value_table 의 M1 부분과 같은 계산, 로그 오즈)."""
    co = pd.read_csv(PROCESSED / "m1" / "m1_coefs.csv")
    co = co[co["level"] == "LCK"].set_index("feature")
    at = pd.read_csv(PROCESSED / "m0" / "m0_attributes.csv", low_memory=False, dtype={"patch": str})
    at = at[at["patch"] == "ALL"]
    out = {}
    for r in at.itertuples(index=False):
        d = r._asdict()
        v = 0.0
        for f, col in M1.AX.items():
            if f in co.index and pd.notna(d.get(col)):
                v += co.loc[f, "coef"] * ((d[col] - 50) / 5) / co.loc[f, "sd_unit"]
        out[f"{d['champion_id']}@{d['position']}"] = v
    return out


def ch_pro_value(D, vec, key):
    """챔피언 칸의 대회 값 = 공통 + 대회 편차."""
    j = D.coder.idx.get(("ch", key))
    if j is None:
        return 0.0
    jp = D.coder.idx_pro.get(("ch", key))
    return vec[j] + (vec[jp] if jp is not None else 0.0)


def value_once(G, S, cfg, k_pro, w_pro=1.0, use_m1=True):
    """한 번 맞춰 챔피언별 (조합 성격 몫 = w × M1 예측의 챔피언 합 근사, 경로 몫 × k) 대회 %p.
    w_pro · use_m1: 2026-11-08 M1 가중치 학습 (m5_value_w.py). 기본값 = E-C 그대로 (w 1, 경로에 M1 통제)."""
    D = Design(G, S, cfg["chp"], use_m1=use_m1)
    m = fit_path(G, D, cfg["lam"], cfg["lam_late"])
    _, Da, ba = m1_approx(G, S, [])
    keys = [k for kind, k in D.coder.idx if kind == "ch"]
    return {k: (w_pro * PP * ch_pro_value(Da, ba, k), k_pro * PP * ch_pro_value(D, m["V"], k)) for k in keys}, m, D


def champion_table(G, S, cfg, ko, k_pro, boot=50, w_pro=1.0, use_m1=True):
    """E-C (M1 성격 + 시간대 경로) 를 08-25 까지 전부로.
    value_pro = 조합 성격 몫 + k × 경로 몫 (대회 %p) → m5_sim 이 씀.
      조합 성격 몫 = M1 예측(로그 오즈)을 챔피언 칸 합 + 통제로 회귀한 챔피언 값 (점검 ①에서 리허설 검증).
      예전 방식(M1 계수 × 챔피언 속성 선형 근사)은 m1_attr_pro 칸에 참고로만.
    구간: 리그 안 경기 다시 뽑기 boot 회 (조합 성격 몫 · 경로 몫을 매번 다시 맞춤), 95%."""
    base, m, D = value_once(G, S, cfg, k_pro, w_pro, use_m1)
    c = D.coder
    m1v = m1_attr_values()
    rng = np.random.default_rng(0)
    groups = list(G.groupby("league").indices.values())
    B = {k: [] for k in base}
    t0 = time.time()
    for b in range(boot):
        idx = np.concatenate([rng.choice(ix, len(ix), replace=True) for ix in groups])
        vb, _, _ = value_once(G.iloc[idx].reset_index(drop=True), S.iloc[idx].reset_index(drop=True), cfg, k_pro, w_pro, use_m1)
        for k in B:
            if k in vb:
                B[k].append((vb[k][0] + vb[k][1], vb[k][1]))
        if (b + 1) % 10 == 0:
            log.info("  부트스트랩 %d/%d (%.0f초)", b + 1, boot, time.time() - t0)
    rows = []
    for (kind, key), j in c.idx.items():
        if kind != "ch":
            continue
        jp = c.idx_pro.get((kind, key))

        def val(vec, pro):
            return vec[j] + (vec[jp] if (pro and jp is not None) else 0.0)
        r = {"key": key, "n": c.n.get((kind, key), 0), "n_pro": c.n_pro.get((kind, key), 0), "a": M2.label(key, ko),
             "m1_attr_pro": PP * m1v.get(key, 0.0), "m1_part_pro": base[key][0]}
        for pro, sfx, s_ in ((False, "", 1.0), (True, "_pro", k_pro)):
            bb = {mm: val(m["beta"][mm], pro) for mm in MINS}
            g = val(m["gam"], pro)
            r[f"early10{sfx}"] = s_ * PP * bb[10]
            r[f"m15{sfx}"] = s_ * PP * (bb[15] - bb[10])
            r[f"m20{sfx}"] = s_ * PP * (bb[20] - bb[15])
            r[f"m25{sfx}"] = s_ * PP * (bb[25] - bb[20])
            r[f"late{sfx}"] = s_ * PP * g
            r[f"path{sfx}"] = s_ * PP * (bb[25] + g)
        r["value_pro"] = r["m1_part_pro"] + r["path_pro"]
        arr = np.array(B[key]) if B[key] else np.full((1, 2), np.nan)
        r["lo"], r["hi"] = np.nanpercentile(arr[:, 0], [2.5, 97.5])
        r["path_lo"], r["path_hi"] = np.nanpercentile(arr[:, 1], [2.5, 97.5])
        r["n_boot"] = len(B[key])
        r["sig"] = bool(r["lo"] > 0 or r["hi"] < 0)
        rows.append(r)
    t = pd.DataFrame(rows)
    t["patch"] = max(p for p in G["patch"].astype(str).unique() if p.startswith("26."))
    return t.sort_values("value_pro", ascending=False), m, D


def objective_table(G, S, ko, lam=100.0):
    D = Design(G, S, False)
    rows = {}
    for o in OBJ + FIRST:
        y = G[o].to_numpy(float)
        if o in FIRST:
            y = y - 0.5
        ok = ~np.isnan(y)
        if ok.sum() < 500:
            continue
        b = M2.fit_gauss(D.X[ok], y[ok], D.pen(lam))
        for (kind, key), j in D.coder.idx.items():
            if kind == "ch":
                jp = D.coder.idx_pro.get((kind, key))
                r = rows.setdefault(key, {"key": key, "a": M2.label(key, ko), "n": D.coder.n.get((kind, key), 0)})
                r[o] = b[j] * (100 if o in FIRST else 1)
                r[f"{o}_pro"] = (b[j] + (b[jp] if jp is not None else 0.0)) * (100 if o in FIRST else 1)
    return pd.DataFrame(rows.values())


def fight_table(G, S, ko, lam=100.0):
    import m2_fights
    FA = m2_fights.fights()
    FA = FA[(FA["n_kills"] >= M2.FIGHTS_MIN_KILLS) & (FA["blue_k"] != FA["red_k"])]
    solo = (G["league"] == "SOLO").to_numpy()
    Gs, Ss = G[solo].reset_index(drop=True), S[solo].reset_index(drop=True)
    D = Design(Gs, Ss, False)
    pos = pd.Series(np.arange(len(Gs)), index=Gs["matchId"])
    FA = FA[FA["matchId"].isin(pos.index)]
    rows = {}
    for nm, lo, hi in WINDOWS:
        f = FA[(FA["t"] >= lo) & (FA["t"] < hi)]
        gi = pos.reindex(f["matchId"]).to_numpy()
        X = sparse.hstack([D.Xc[gi], sparse.csr_matrix(np.column_stack([f["gold_diff"].fillna(0).to_numpy(), np.ones(len(f))]))]).tocsr()
        lamv = np.r_[D.coder.penalty({"ch": lam, "chp": lam}), [1e-3, 1e-3]]
        b = M2.fit_logit(X, (f["blue_k"] > f["red_k"]).to_numpy(float), np.zeros(len(f)), lamv)
        for (kind, key), j in D.coder.idx.items():
            if kind == "ch":
                r = rows.setdefault(key, {"key": key, "a": M2.label(key, ko), "n": D.coder.n.get((kind, key), 0)})
                r[nm] = PP * b[j]
        log.info("  교전 %s: %d건", nm, len(f))
    return pd.DataFrame(rows.values())


# ---------------------------------------------------------------- main
NAMES = {"C": "C 시간대 경로", "D": "D 승패만", "EC": "E-C M1 + 시간대 경로", "ED": "E-D M1 + 승패만"}


def cfg_of(avg, kind):
    best = avg.loc[NAMES[kind], "ll"].idxmin()
    p = parse_cfg(best)
    c = {"lam": float(p["lam"]), "chp": p["패치"] == "O"}
    if "후반" in p:
        c["lam_late"] = float(p["후반"])
    return best, c


def main():
    t0 = time.time()
    OUT.mkdir(exist_ok=True)
    G, S = load_cached()
    ko = M2.ko_names()
    log.info("자료 (%.0f초)", time.time() - t0)
    f_sel = OUT / "m5_value_selection.csv"
    if "--reuse" in sys.argv and f_sel.exists():      # 리허설 결과 다시 쓰기 (표·보고서만 다시)
        sel = pd.read_csv(f_sel, dtype={"patch": str})
        sel["cfg"] = sel["cfg"].fillna("")
    else:
        sel = rehearsal(G, S, load_players())
        H.save_csv(sel, f_sel)
        pd.to_pickle(LOSS, OUT / "m5_value_losses.pkl")
    if not LOSS and (OUT / "m5_value_losses.pkl").exists():
        LOSS.update(pd.read_pickle(OUT / "m5_value_losses.pkl"))
    sel_all = sel
    sel = sel[sel["patch"].astype(str).isin(SEL_PATCHES)]
    avg = sel.groupby(["variant", "cfg"], sort=False)[["ll_pro", "ll_solo", "k_pro", "k_solo"]].mean()
    avg["ll"] = avg["ll_pro"] + avg["ll_solo"]   # 고르는 기준 = 대회 + 솔랭 (대회 361판만으로는 잡음이 큼, M2 와 같은 기준)
    best, cfgs = {}, {}
    for kind in NAMES:
        best[kind], cfgs[kind] = cfg_of(avg, kind)
    k_pro = float(avg.loc[(NAMES["EC"], best["EC"]), "k_pro"])
    log.info("고른 설정 %s · E-C 대회 배율 k %.2f (%.0f초)", cfgs, k_pro, time.time() - t0)
    stab = split_half(G, S, {k: (k, cfgs[k]) for k in ("D", "C", "ED", "EC")})
    log.info("안정성 %s (%.0f초)", stab, time.time() - t0)
    CK, R2 = checks(G, S, load_players(), cfgs["EC"])
    H.save_csv(CK, OUT / "m5_value_checks.csv")
    H.save_csv(R2, OUT / "m5_value_r2.csv")
    log.info("점검 끝 (%.0f초)", time.time() - t0)
    boot = int(sys.argv[sys.argv.index("--boot") + 1]) if "--boot" in sys.argv else 50
    T, m, D = champion_table(G, S, cfgs["EC"], ko, k_pro, boot)
    H.save_csv(T, OUT / "m5_value_champion.csv")
    O = objective_table(G, S, ko)
    H.save_csv(O, OUT / "m5_value_objectives.csv")
    F = fight_table(G, S, ko)
    H.save_csv(F, OUT / "m5_value_fights.csv")
    (OUT / "m5_value_config.json").write_text(json.dumps({"chosen": "E-C", "cfg": cfgs, "k_pro": k_pro, "K_END": K_END,
                                                          "conversion": {f"{s}_{mm}": v for (s, mm), v in m["conv"].items()}},
                                                         ensure_ascii=False, indent=1), encoding="utf-8")
    report(sel, avg, best, stab, T, O, F, m, k_pro, CK, R2, boot)
    log.info("끝 (%.0f초)", time.time() - t0)


def pooled_section(best, n_boot=2000):
    """평가 패치 전부(REH_PATCHES) 경기를 모아 고른 설정끼리 비교 + E-C 와의 차이 구간 (경기 다시 뽑기, 같은 경기끼리 짝)."""
    pats = [p for p in REH_PATCHES if any(k[0] == p for k in LOSS)]
    if not pats:
        return []
    vs = [("A 통제만", ""), ("M1 성격만", ""), ("B 지금 이길 가치 (참고·겹침)", "")] + [(NAMES[k], best[k]) for k in ("D", "C", "ED", "EC")]
    pro = {v: np.concatenate([LOSS[(p, v, c)][0] for p in pats]) for v, c in vs}
    solo = {v: np.concatenate([LOSS[(p, v, c)][1] for p in pats]) for v, c in vs}
    rng = np.random.default_rng(0)
    n = len(pro["A 통제만"])
    idx = rng.integers(0, n, (n_boot, n))
    ref = pro[NAMES["EC"]]
    L = [f"### 1-1. 평가 패치 {len(pats)}개 합 ({' · '.join(pats)}) — 대회 {n}판 · 솔랭 {len(solo['A 통제만'])}판", "",
         "- 설정은 26.14 · 26.15 로 고른 그대로. 나머지 패치는 평가에만. 차이 = 그 변형 − E-C (양수 = E-C 가 나음), 구간 = 대회 경기 다시 뽑기 95%", "",
         "| 변형 | 대회 | 솔랭 | 대회 차이 (− E-C) [95%] |", "|---|---|---|---|"]
    for v, _ in vs:
        d = pro[v] - ref
        bs = d[idx].mean(1)
        lo, hi = np.percentile(bs, [2.5, 97.5])
        L.append(f"| {v} | {pro[v].mean():.4f} | {solo[v].mean():.4f} | {d.mean():+.4f} [{lo:+.4f}, {hi:+.4f}] |")
    L.append("")
    return L


def report(sel, avg, best, stab, T, O, F, m, k_pro, CK, R2, boot):
    L = ["# M5 2단계 — 이길 가치를 시간대별 중간 결과로", "",
         f"- 상태 → 승률: 출처·시점별 승리 ~ 팀 골드 차. **그 시점 전에 끝난 경기 = ±{K_END}** (일찍 끝난 판을 빼지 않음)",
         "- C = 시점(10·15·20·25분)마다 L_t ~ 챔피언(공통 + 대회 편차 [+ 패치 편차]) + 통제 (릿지), 후반 = 승리 ~ 오프셋 L_25 + 챔피언 (로지스틱). 값 = β_25 + γ",
         "- D = 같은 챔피언 칸을 승패만으로. **E = M1 조합 성격 예측을 기본으로 두고 챔피언 칸은 그 나머지** (E-D 승패만 = 지금 M2 방식과 같은 구조, E-C 시간대 경로)",
         "- A = 통제(리그·Elo·숙련·솔랭 프로 수)만. B = 지금 m5_sim 이 쓰던 이길 가치 (08-25 까지 전부로 만든 값 → 평가 패치와 겹쳐 유리, 참고)",
         "- 예측 = 기본(통제 또는 M1) + k × 챔피언 값 차. k 는 출처마다 학습 기간 마지막 15% 경기로 맞춤. 리허설 M1 예측은 그 패치 시점 속성표로 다시 학습. 플레이오프 안 봄", "",
         "## 1. 리허설 (26.14 · 26.15 평균 로그 손실, 낮을수록 좋음 · 고르는 기준 = 대회 + 솔랭)", "",
         "| 변형 | 설정 | 대회 | 솔랭 | k 대회 · 솔랭 |", "|---|---|---|---|---|"]
    show = [("A 통제만", ""), ("M1 성격만", ""), ("B 지금 이길 가치 (참고·겹침)", "")]
    for kind in ("D", "C", "ED", "EC"):
        s = avg.loc[NAMES[kind]].sort_values("ll")
        show += [(NAMES[kind], c) for c in s.index[:2]]
    for v, c in show:
        r = avg.loc[(v, c)]
        mk = "**" if any(v == NAMES[k] and c == best[k] for k in NAMES) else ""
        L.append(f"| {mk}{v}{mk} | {c} | {r['ll_pro']:.4f} | {r['ll_solo']:.4f} | {r['k_pro']:+.2f} · {r['k_solo']:+.2f} |")
    L += ["", "| 패치 | 변형 (고른 설정) | 대회 경기 | 대회 | 솔랭 경기 | 솔랭 |", "|---|---|---|---|---|---|"]
    for p in ("26.14", "26.15"):
        for v, c in [("A 통제만", ""), ("M1 성격만", ""), ("B 지금 이길 가치 (참고·겹침)", "")] + [(NAMES[k], best[k]) for k in ("D", "C", "ED", "EC")]:
            r = sel[(sel["patch"].astype(str) == p) & (sel["variant"] == v) & (sel["cfg"] == c)].iloc[0]
            L.append(f"| {p} | {v} | {r['n_pro']} | {r['ll_pro']:.4f} | {r['n_solo']} | {r['ll_solo']:.4f} |")
    L += ["", f"- **채택: E-C** ({best['EC']}). 대회 배율 k = {k_pro:.2f} → 최종 표의 경로 몫에 곱함", ""]
    L += pooled_section(best)
    L += ["## 2. 안정성 (경기를 반으로 나눠 두 번 맞춘 챔피언 값의 상관)", "",
          "| 방식 | 공통 값 (양쪽 50판 이상) | 대회 값 (양쪽 대회 15판 이상) |", "|---|---|---|"]
    for k in ("D", "C", "ED", "EC"):
        s = stab[k]
        L.append(f"| {NAMES[k]} | r = {s['r']:.2f} ({s['n']}개) | r = {s['r_pro']:.2f} ({s['n_pro']}개) |")
    L += ["", "## 3. 상태 → 승률 환산 (최종 자료, 골드 1,000 당 로그 오즈)", "", "| 출처 | 10분 | 15분 | 20분 | 25분 |", "|---|---|---|---|---|"]
    for s, nm in (("solo", "솔랭"), ("pro", "대회")):
        L.append(f"| {nm} | " + " | ".join(f"{m['conv'][(s, mm)][1]:.2f}" for mm in MINS) + " |")
    ck = CK.groupby("check", sort=False)[["ll_pro", "ll_solo"]].mean()
    L += ["", "## 4. 점검", "", "### 4-1. 시험판이 쓰는 값 (챔피언 하나씩 값의 합) 이 실제 M1 + 경로만큼 맞히나 (26.14 · 26.15 평균)", "",
          "| 방식 | 대회 | 솔랭 |", "|---|---|---|"]
    for nm in ("K 3.5 · 실제 M1 + 경로", "시험판 방식: M1 챔피언 합 근사 + 경로", "M1 챔피언 합 근사만", "예전 시험판 방식: 속성 계수 근사 + 경로 (겹침)"):
        if nm in ck.index:
            L.append(f"| {nm} | {ck.loc[nm, 'll_pro']:.4f} | {ck.loc[nm, 'll_solo']:.4f} |")
    a_ = CK[CK["check"].str.startswith("시험판 방식")]
    if len(a_):
        L += ["", f"- M1 예측(로그 오즈)을 챔피언 합 + 통제로 근사한 설명력 R²: 학습 {a_['apx_r2_train'].mean():.2f} · 평가 대회 {a_['apx_r2_pro'].mean():.2f} · 평가 솔랭 {a_['apx_r2_solo'].mean():.2f} (두 패치 평균)"]
    L += ["", "### 4-2. 시점별 중간 결과 설명력 (평가 경기 R², 상태 L_t · 끝난 경기 ±K 포함)", "",
          "| 출처 | 시점 | 평가 경기 | 이미 끝난 비율 | 통제 + M1 | + 챔피언 (E-C) |", "|---|---|---|---|---|---|"]
    r2a = R2.groupby(["source", "minute"], sort=False)[["n", "ended_share", "r2_base", "r2_full"]].mean()
    for (src, mm), r in r2a.iterrows():
        L.append(f"| {src} | {mm}분 | {r['n']:.0f} | {r['ended_share']:.0%} | {r['r2_base']:.3f} | {r['r2_full']:.3f} |")
    L += ["", "### 4-3. K (그 시점 전에 끝난 경기 값) 민감도 — 실제 M1 + 경로, 같은 설정", "", "| K | 끝난 경기 승률로 치면 | 대회 | 솔랭 |", "|---|---|---|---|"]
    for kk in K_GRID:
        nm = f"K {kk:g} · 실제 M1 + 경로"
        if nm in ck.index:
            L.append(f"| {kk:g} | {1 / (1 + np.exp(-kk)):.1%} | {ck.loc[nm, 'll_pro']:.4f} | {ck.loc[nm, 'll_solo']:.4f} |")
    cols = ["m1_part_pro", "early10_pro", "m15_pro", "m20_pro", "m25_pro", "late_pro", "value_pro"]
    hdr = "| 챔피언 | 판수 (대회) | 조합 성격 (M1 합 근사) | ~10분 | 10~15 | 15~20 | 20~25 | 25분~·같은 상태 | 합 [95% 구간] |"

    def tab(t):
        out = [hdr, "|---|---|---|---|---|---|---|---|---|"]
        for r in t.itertuples():
            mk = "**" if r.sig else ""
            out.append(f"| {r.a} | {r.n:,} ({r.n_pro}) | " + " | ".join(f"{getattr(r, c):+.1f}" for c in cols[:-1])
                       + f" | {mk}{r.value_pro:+.1f}{mk} [{r.lo:+.1f}, {r.hi:+.1f}] |")
        return out
    big = T[T["n"] >= 200]
    L += ["", "## 5. 챔피언 이길 가치 (대회 기준 %p, 50% 근처 환산, 판수 200 이상)", "",
          "- 조합 성격 = M1 예측을 챔피언 합으로 근사한 값. 시간대 칸 = 조합 성격으로 설명 안 되는 나머지가 언제 생기나 (× k)",
          f"- 구간 = 리그 안 경기 다시 뽑기 {boot}회 95% (조합 성격 몫·경로 몫 모두 다시 맞춤). **굵게** = 구간이 0 을 안 걸침. "
          f"판수 200 이상 {int((T['n'] >= 200).sum())}개 중 {int(T.loc[T['n'] >= 200, 'sig'].sum())}개", "",
          "### 높은 쪽", ""] + tab(big.head(15))
    L += ["", "### 낮은 쪽", ""] + tab(big.tail(10).iloc[::-1])
    early = big.assign(e=big["early10_pro"] + big["m15_pro"]).sort_values("e", ascending=False)
    L += ["", "### 초반형 (나머지 몫 중 ~15분이 큰 쪽)", ""] + tab(early.head(8))
    L += ["", "### 후반형 (25분 이후·같은 상태 몫이 큰 쪽)", ""] + tab(big.sort_values("late_pro", ascending=False).head(8))
    import m4_model as MM
    wv = MM.win_value_table()
    chk = ["Rumble@TOP", "Annie@MIDDLE", "Rell@UTILITY", "TwistedFate@MIDDLE", "Lux@UTILITY", "Ashe@BOTTOM", "LeeSin@JUNGLE",
           "Seraphine@UTILITY", "Azir@MIDDLE", "Milio@UTILITY"]
    L += ["", "### 예전 값 → 새 값 (대회 %p)", "", "| 챔피언 | 판수 (대회) | 예전 (속성 계수 근사 + M2 단독) | 새 값 [95% 구간] |", "|---|---|---|---|"]
    for k in chk:
        t = T[T["key"] == k]
        if len(t):
            c, p = k.split("@")
            n_ = t.iloc[0]
            L.append(f"| {n_['a']} | {n_['n']:,} ({n_['n_pro']}) | {25 * wv.get((c, p), 0.0):+.1f} | {n_['value_pro']:+.1f} [{n_['lo']:+.1f}, {n_['hi']:+.1f}] |")
    L += ["", "## 6. 오브젝트 (대회 기준, 블루 − 레드 개수 차 / 첫 획득은 %p, 판수 200 이상) — 설명용, 합계에 안 넣음", ""]
    Ob = O[O["n"] >= 200]
    for o, nm in (("dragons", "드래곤"), ("void_grubs", "공허 유충"), ("towers", "포탑"), ("barons", "바론"), ("first_dragon", "첫 드래곤 (%p)")):
        if f"{o}_pro" in Ob:
            s = Ob.sort_values(f"{o}_pro", ascending=False)
            L.append(f"- **{nm}** 많은 쪽: " + ", ".join(f"{r.a} {getattr(r, o + '_pro'):+.2f}" for r in s.head(5).itertuples())
                     + " / 적은 쪽: " + ", ".join(f"{r.a} {getattr(r, o + '_pro'):+.2f}" for r in s.tail(3).itertuples()))
    L += ["", "## 7. 시간대별 교전 (솔랭, 교전 승률 %p, 교전 시점 골드 차 통제, 판수 200 이상) — 설명용", ""]
    Fb = F[F["n"] >= 200]
    for nm, _, _ in WINDOWS:
        s = Fb.sort_values(nm, ascending=False)
        L.append(f"- **{nm}** 강한 쪽: " + ", ".join(f"{r.a} {s.loc[r.Index, nm]:+.1f}" for r in s.head(6).itertuples())
                 + " / 약한 쪽: " + ", ".join(f"{r.a} {s.loc[r.Index, nm]:+.1f}" for r in s.tail(4).itertuples()))
    (OUT / "m5_value_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
