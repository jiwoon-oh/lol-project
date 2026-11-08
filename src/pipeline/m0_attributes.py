"""M0 속성 보정 — 승패·게임 길이를 뺀 챔피언 성격 (docs/PIPELINE.md 7-7)

1차 (2026-11-05): 참가자 지표 28개를 판 단위 회귀로 보정.
2차 (2026-11-06): ① 타임라인 축도 보정·교차 계산 ② 잠수 판을 타임라인 골드 정지로 찾음
                  ③ 큰 변경(리워크) 챔피언은 데이터로 확인되면 변경 전후를 따로 계산 ④ 챔피언별 승패 효과

판 단위 회귀 (포지션 × 지표마다):
    지표 ~ 챔피언 + 패치 + 승패 + 게임 길이(10분위 구간)
    - 분당 값(횟수·피해)  : 포아송·로그, 게임 시간 가중 (= log 시간 오프셋)  → 보정값 = 값 × exp(기준 − 이 판)
    - 몫·0/1            : 이항·로짓 (분수 응답)                         → 보정값 = 값 − (예측 − 기준 예측)
    - 연속 (타임라인 포함) : 정규                                        → 보정값 = 값 + (기준 − 이 판)
    기준 = 승패 0.5, 게임 길이 효과는 그 포지션 평균
    챔피언별 승패 효과 (④): 공통 회귀의 잔차로 챔피언마다 (이긴 판 − 진 판) 차이를 구하고,
        판수 조화 평균 n_h 로 당김 → 편차 × n_h / (n_h + K_WIN). 보정 때 공통 승패 효과에 더함
보정한 판 값을 기존 aggregate·shrink·백분위 그대로 통과 → 같은 칸 이름의 속성표.

거르기 (②): wasAfk(16.2x 이후만 값 있음) + 타임라인 골드 정지 (5분 동안 골드 증가 < STALL_GAIN, 또는 10분 골드 < GOLD10_MIN).
    끝 기록(분당 골드·피해)은 잠수 선수도 정상과 비슷해서 쓰지 않음 (2026-11-06 확인). 매칭 배정 포지션 필드는 대부분 기간에 없음
    → 대신 `pos_share`(그 챔피언 판 중 이 포지션 비율)·`offrole`(< 5%) 칸을 붙임 (선수 단위 주 포지션 판정은 못 함)
큰 변경 (③): 패치노트 후보(변경 줄 수) 중, 변경 전후 지표 차이가 같은 날짜로 나눈 다른 챔피언들의 95% 분위보다 크면
    변경 전 경기를 따로 계산 (`era` 칸 = "pre{패치}", 전체 기간 행은 patch = "ALL<{패치}"). 변경 후가 기본(`ALL`)

산출 (data/processed/2026/m0/):
    m0_attributes.csv            전체 (08-25까지)
    m0_attributes_fold{0-4}.csv  교차 계산용: 경기 crc32 % 5 == k 를 뺀 나머지 (참가자·타임라인 축 모두)
    m0_attributes_asof_{패치}.csv 설정 고르기용: 그 LCK 패치 창 첫날 전 솔랭만 (26.14 · 26.15)
    m0_nuisance.csv · m0_champ_win.csv · m0_rework.csv · m0_eval.md

    python src/pipeline/m0_attributes.py            # 전부
    python src/pipeline/m0_attributes.py --only full
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.special import expit
from sklearn.linear_model import LogisticRegression, PoissonRegressor, Ridge

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402

PROCESSED, RAW, log, POS = H.PROCESSED, H.RAW, H.log, H.POS
OUT = PROCESSED / "m0"
OUT.mkdir(exist_ok=True)
FLAGS = OUT / "m0_flags.csv.gz"
TL_CACHE = OUT / "m0_timeline.pkl"

RATE = ["deaths_pm", "immob_pm", "cc_time_pm", "cc_kill_pm", "pick_pm", "ally_heal_shield_pm", "eff_heal_shield_pm",
        "taken_pm", "mitig_pm", "outnumbered_pm", "kills_near_turret_pm", "multikills_pm", "solokills_pm",
        "solo_turrets_late_pm", "bldg_pm", "plates_pm", "skillshots_pm", "r_casts_pm", "one_spell_mk_pm", "knock_kill_pm"]
FRAC = ["dmg_share", "taken_share", "mitig_share", "taken_phys_share", "lane_adv", "solo_kill_share"]
GAUSS = ["taken_per_death", "max_cs_adv"]
METRICS = RATE + FRAC + GAUSS
TL_COLS = ["gold_diff10", "gold_diff15", "gold_diff20", "gold_diff25", "dmg_share10", "dmg_share15", "dmg_share20",
           "dmg_share25", "growth", "dmg_curve", "late_seg", "late_end", "late_30_35", "gold_eff"]
N_BINS = 10
K_WIN = 100          # 챔피언별 승패 효과 당기기 (판수 조화 평균 기준)
STALL_GAIN = 800     # 5분 동안 골드 증가가 이보다 적으면 잠수 (기본 골드만 받으면 약 610, 실제 최저 0.05% 분위 약 900)
GOLD10_MIN = 1900    # 10분 골드가 이보다 적으면 잠수 (실제 최저 0.05% 분위 약 2,150)
OFFROLE = 0.05
REWORK_Q = 0.95


# ---------------------------------------------------------------- 타임라인 (캐시)
def timeline_rows():
    if TL_CACHE.exists():
        return pd.read_pickle(TL_CACHE)
    t = pd.Timestamp.now()
    out = H.timeline_table()
    pd.to_pickle(out, TL_CACHE)
    log.info("타임라인 캐시 %s (%s)", TL_CACHE.name, pd.Timestamp.now() - t)
    return out


def tl_participants(fr: pd.DataFrame, sg: pd.DataFrame) -> pd.DataFrame:
    """경기 × 참가자 1행: 분별 골드 차이·딜 몫·골드, 성장, 구간 딜 몫."""
    fr = fr[fr["teamPosition"].isin(POS)]
    key = ["matchId", "teamId", "teamPosition", "champion_id"]
    piv = fr.pivot_table(index=key, columns="minute", values=["gold_diff", "dmg_share", "gold"]).reset_index()
    piv.columns = key + [f"{a}{b}" for a, b in piv.columns[len(key):]]
    piv["growth"] = piv.get("gold_diff25") - piv.get("gold_diff10")
    piv["dmg_curve"] = piv.get("dmg_share25") - piv.get("dmg_share10")
    s = sg[sg["teamPosition"].isin(POS)].copy()
    for c in ("d0_10", "d20_25", "d25_end", "d30_35"):
        team = s.groupby(["matchId", "teamId"])[c].transform("sum")
        s[c.replace("d", "sh", 1)] = s[c] / team.where(team > 0)
    s["late_seg"] = s["sh20_25"] - s["sh0_10"]
    s["late_end"] = s["sh25_end"] - s["sh0_10"]
    s["late_30_35"] = s["sh30_35"] - s["sh0_10"]
    dpg_e = s["d0_10"] / s["g0_10"].where(s["g0_10"] > 0)
    dpg_l = s["d25_end"] / s["g25_end"].where(s["g25_end"] > 0)
    s["gold_eff"] = np.log(dpg_l.where(dpg_l > 0) / dpg_e.where(dpg_e > 0))
    return piv.merge(s[key + ["late_seg", "late_end", "late_30_35", "gold_eff"]], on=key, how="outer")


# ---------------------------------------------------------------- 잠수 (②)
def flags() -> pd.DataFrame:
    if FLAGS.exists():
        return pd.read_csv(FLAGS, dtype={"matchId": str, "wasAfk": str})
    rows = []
    files = sorted(glob.glob(str(H.MATCH_DIR / "*.json.gz")))
    for i, f in enumerate(files, 1):
        try:
            m = json.load(gzip.open(f))
        except Exception:
            continue
        info = m.get("info") or {}
        if info.get("queueId") != 420 or not info.get("participants"):
            continue
        for p in info["participants"]:
            rows.append((m["metadata"]["matchId"], p.get("teamId"), p.get("teamPosition"), p.get("wasAfk")))
        if i % 5000 == 0:
            log.info("플래그: 경기 원본 %d/%d", i, len(files))
    fl = pd.DataFrame(rows, columns=["matchId", "teamId", "teamPosition", "wasAfk"])
    fl.to_csv(FLAGS, index=False, compression="gzip")
    return fl.astype({"wasAfk": str})


def stall_rows(tp: pd.DataFrame, dur: pd.Series) -> pd.Series:
    """참가자 행마다 골드 정지 여부 (경기가 그 다음 프레임 + 1분 이상 이어졌을 때만 판단)."""
    d = tp["matchId"].map(dur)
    bad = tp["gold10"] < GOLD10_MIN
    for a, b in ((10, 15), (15, 20), (20, 25)):
        ga, gb = tp.get(f"gold{a}"), tp.get(f"gold{b}")
        if ga is None or gb is None:
            continue
        bad |= ((gb - ga) < STALL_GAIN) & (d >= b + 1)
    return bad.fillna(False)


def filter_games(df: pd.DataFrame, tp: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    fl = flags()
    afk = fl["wasAfk"].astype(str).str.lower() == "true"
    afk_field = set(fl.loc[afk, "matchId"])
    dur = df.groupby("matchId")["dur_min"].first()
    st = stall_rows(tp, dur)
    stall = set(tp.loc[st, "matchId"])
    ids = set(df["matchId"])
    # 검산: wasAfk 가 있는 판 중 골드 정지로도 잡힌 비율
    known = fl[fl["wasAfk"].isin(["True", "False", "true", "false"])].merge(
        tp.assign(stall=st)[["matchId", "teamId", "teamPosition", "stall"]], on=["matchId", "teamId", "teamPosition"])
    kk = known["wasAfk"].str.lower() == "true"
    info = {"games": len(ids), "wasAfk_games": len(afk_field & ids), "stall_games": len(stall & ids),
            "both": len(afk_field & stall & ids), "tl_games": tp["matchId"].nunique(),
            "check_afk_rows": int(kk.sum()), "check_afk_caught": int((known["stall"] & kk).sum()),
            "check_ok_rows": int((~kk).sum()), "check_ok_flagged": int((known["stall"] & ~kk).sum())}
    bad = afk_field | stall
    info["dropped_games"] = len(bad & ids)
    return df[~df["matchId"].isin(bad)].copy(), info, bad


# ---------------------------------------------------------------- 큰 변경 (③)
def rework_candidates() -> pd.DataFrame:
    c = pd.read_csv(PROCESSED / "c_patch_champion_changes.csv", dtype={"riot_patch": str, "patch": str})
    c["upd"] = c["stat"].astype(str).str.contains("업데이트|변경|신규|재설계|리워크")
    g = c.groupby(["riot_patch", "champion_id"]).agg(lines=("stat", "size"), updates=("upd", "sum")).reset_index()
    return g[(g["updates"] >= 4) | (g["lines"] >= 12)].reset_index(drop=True)


def profile_shift(df: pd.DataFrame, champ: str, patch: str) -> tuple[float, float, int, int]:
    """그 챔피언 주 포지션에서 변경 전후 지표 평균 차이 (포지션 안 챔피언 간 표준편차 단위, 지표 평균)
    + 같은 날짜로 나눈 다른 챔피언들의 같은 값 분포에서의 분위."""
    d = H.per_row(df)[METRICS].join(df[["teamPosition", "champion_id", "patch"]])
    d["post"] = d["patch"] >= patch
    sub = d[d["champion_id"] == champ]
    if sub.empty:
        return np.nan, np.nan, 0, 0
    pos = sub["teamPosition"].mode().iloc[0]
    p = d[d["teamPosition"] == pos]
    g = p.groupby(["champion_id", "post"])[METRICS].mean()
    n = p.groupby(["champion_id", "post"]).size().unstack()
    ok = n.index[(n.get(False, 0) >= 60) & (n.get(True, 0) >= 60)]
    sd = p.groupby("champion_id")[METRICS].mean().std().replace(0, np.nan)
    dist = {}
    for c in ok:
        dist[c] = float(((g.loc[(c, True)] - g.loc[(c, False)]).abs() / sd).mean())
    if champ not in dist:
        return np.nan, np.nan, int(n.loc[champ].get(False, 0)) if champ in n.index else 0, \
            int(n.loc[champ].get(True, 0)) if champ in n.index else 0
    others = np.array([v for k, v in dist.items() if k != champ])
    return dist[champ], float((others < dist[champ]).mean()), int(n.loc[champ, False]), int(n.loc[champ, True])


def rework_split(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    cand = rework_candidates()
    rows, split = [], {}
    for _, r in cand.iterrows():
        sh, q, n0, n1 = profile_shift(df, r["champion_id"], r["riot_patch"])
        use = bool(np.isfinite(q) and q >= REWORK_Q)
        rows.append({**r.to_dict(), "shift": sh, "quantile": q, "n_pre": n0, "n_post": n1, "split": use})
        if use:
            split[r["champion_id"]] = max(split.get(r["champion_id"], ""), r["riot_patch"])
    return split, pd.DataFrame(rows)


def era_ids(df: pd.DataFrame, split: dict) -> pd.Series:
    cid = df["champion_id"].copy()
    for c, p in split.items():
        m = (df["champion_id"] == c) & (df["patch"] < p)
        cid[m] = f"{c}@pre{p}"
    return cid


def restore_era(at: pd.DataFrame) -> pd.DataFrame:
    s = at["champion_id"].astype(str)
    pre = s.str.contains("@pre")
    at["era"] = np.where(pre, "pre" + s.str.split("@pre").str[-1], "")
    at["champion_id"] = s.str.split("@pre").str[0]
    at.loc[pre & (at["patch"] == "ALL"), "patch"] = "ALL<" + s[pre & (at["patch"] == "ALL")].str.split("@pre").str[-1]
    return at


# ---------------------------------------------------------------- 판 단위 보정
def design(sub: pd.DataFrame, bins: np.ndarray):
    ch = pd.Categorical(sub["champion_id"])
    pa = pd.Categorical(sub["patch"])
    db = np.clip(np.searchsorted(bins, sub["dur_min"].to_numpy(), side="right") - 1, 0, N_BINS - 1)
    n = len(sub)
    Xc = sparse.csr_matrix((np.ones(n), (np.arange(n), ch.codes)), shape=(n, len(ch.categories)))
    Xp = sparse.csr_matrix((np.ones(n), (np.arange(n), pa.codes)), shape=(n, len(pa.categories)))[:, 1:]
    Xd = sparse.csr_matrix((np.ones(n), (np.arange(n), db)), shape=(n, N_BINS))[:, 1:]
    w = sub["win"].astype(float).to_numpy()[:, None]
    return sparse.hstack([Xc, Xp, Xd, sparse.csr_matrix(w)]).tocsr(), db, len(ch.categories), len(pa.categories) - 1


def champ_win_dev(resid: np.ndarray, win: np.ndarray, champ: np.ndarray) -> np.ndarray:
    """챔피언별 (이긴 판 − 진 판) 잔차 평균 차이, 판수 조화 평균으로 당김 → 행마다 값."""
    t = pd.DataFrame({"r": resid, "w": win, "c": champ}).dropna()
    g = t.groupby(["c", "w"])["r"].agg(["mean", "size"]).unstack()
    dev = (g[("mean", 1.0)] - g[("mean", 0.0)]).fillna(0)
    nw, nl = g[("size", 1.0)].fillna(0), g[("size", 0.0)].fillna(0)
    nh = 2 * nw * nl / (nw + nl).replace(0, np.nan)
    dev = (dev * nh / (nh + K_WIN)).fillna(0)
    return pd.Series(champ).map(dev).fillna(0).to_numpy(), dev


def fit_adjust(sub: pd.DataFrame, y: np.ndarray, kind: str):
    """한 (포지션, 지표) 보정. 반환: 보정값, 계수 dict, 챔피언별 편차 Series."""
    bins = np.quantile(sub["dur_min"], np.linspace(0, 1, N_BINS + 1))[:-1]
    X, db, nc, npat = design(sub, bins)
    mn = sub["dur_min"].to_numpy()
    win = sub["win"].astype(float).to_numpy()
    share_bins = np.bincount(db, minlength=N_BINS) / len(db)
    ok = np.isfinite(y)
    if ok.sum() < 500:
        return None
    Xo = X[ok]
    if kind == "rate":
        scale = np.nanmean(y[ok]) or 1.0
        mdl = PoissonRegressor(alpha=1e-6, max_iter=500).fit(Xo, y[ok] / scale, sample_weight=mn[ok])
    elif kind == "frac":
        yy = np.clip(y[ok], 0, 1)
        mdl = LogisticRegression(C=1e6, max_iter=1000).fit(
            sparse.vstack([Xo, Xo]), np.r_[np.ones(ok.sum()), np.zeros(ok.sum())], sample_weight=np.r_[yy, 1 - yy] + 1e-9)
    else:
        mdl = Ridge(alpha=1e-3).fit(Xo, y[ok])
    beta = np.ravel(mdl.coef_)
    icpt = float(np.ravel(mdl.intercept_)[0])
    b_win = beta[-1]
    b_dur = np.r_[0.0, beta[nc + npat: nc + npat + N_BINS - 1]]
    ref = 0.5 * b_win + share_bins @ b_dur
    shift = ref - (win * b_win + b_dur[db])
    lin = X @ beta + icpt
    champ = sub["champion_id"].to_numpy()
    if kind == "rate":
        pred = np.exp(lin) * scale
        # 0 이 많은 횟수라 로그 잔차 대신 (실제 합 ÷ 예측 합)의 로그를 이긴 판·진 판으로 비교 (게임 시간 가중)
        t = pd.DataFrame({"o": np.where(ok, y, 0) * mn, "e": np.where(ok, pred, 0) * mn, "w": win, "c": champ})
        g = t.groupby(["c", "w"])[["o", "e"]].sum()
        a0 = 0.1 * float(g["e"].median())  # 작은 가상 판수 (실제 0 이면 로그가 터지지 않게)
        oe = np.log((g["o"] + a0) / (g["e"] + a0)).unstack()
        nn = t.groupby(["c", "w"]).size().unstack().fillna(0)
        raw = (oe.get(1.0) - oe.get(0.0)).fillna(0)
        nh = 2 * nn.get(1.0, 0) * nn.get(0.0, 0) / (nn.get(1.0, 0) + nn.get(0.0, 0)).replace(0, np.nan)
        dev = (raw * nh / (nh + K_WIN)).fillna(0)
        dev_row = pd.Series(champ).map(dev).fillna(0).to_numpy()
        yadj = y * np.exp(shift - (win - 0.5) * dev_row)
    elif kind == "frac":
        pred = expit(lin)
        dev_row, dev = champ_win_dev(np.where(ok, y - pred, np.nan), win, champ)
        yadj = np.clip(y + (expit(lin + shift) - pred) - (win - 0.5) * dev_row, 0, 1)
    else:
        dev_row, dev = champ_win_dev(np.where(ok, y - lin, np.nan), win, champ)
        yadj = y + shift - (win - 0.5) * dev_row
    co = {"b_win": b_win, "dur_short": b_dur[0] - share_bins @ b_dur, "dur_long": b_dur[-1] - share_bins @ b_dur,
          "n": int(ok.sum()), "dev_sd": float(dev.std())}
    return np.where(ok, yadj, np.nan), co, dev


def nuisance_adjust(df: pd.DataFrame):
    d = H.per_row(df)
    adj = d.copy()
    coefs, devs = [], []
    for pos, sub in df.groupby("teamPosition"):
        for m in METRICS:
            kind = "rate" if m in RATE else "frac" if m in FRAC else "gauss"
            y = d.loc[sub.index, m].to_numpy(dtype=float)
            if m == "taken_per_death":
                y = np.log1p(np.clip(y, 0, None))
            r = fit_adjust(sub, y, kind)
            if r is None:
                continue
            yadj, co, dev = r
            adj.loc[sub.index, m] = np.expm1(yadj) if m == "taken_per_death" else yadj
            coefs.append({"position": pos, "metric": m, "kind": kind, **co})
            devs.append(pd.DataFrame({"position": pos, "metric": m, "champion_id": dev.index, "win_dev": dev.values}))
    return adj, pd.DataFrame(coefs), pd.concat(devs, ignore_index=True)


def tl_adjust(tp: pd.DataFrame, bu: pd.DataFrame, meta: pd.DataFrame):
    """타임라인 판 값 보정 (정규). meta = 참가자 (matchId, teamId, teamPosition) → win, dur_min, patch, champion_id(시대 반영)."""
    key = ["matchId", "teamId", "teamPosition"]
    t = tp.drop(columns=["champion_id"]).merge(meta, on=key, how="inner")
    coefs = []
    for pos, sub in t.groupby("teamPosition"):
        for c in TL_COLS:
            if c not in sub:
                continue
            r = fit_adjust(sub, sub[c].to_numpy(dtype=float), "gauss")
            if r is None:
                continue
            t.loc[sub.index, c] = r[0]
            coefs.append({"position": pos, "metric": "tl_" + c, "kind": "gauss", **r[1]})
    b = bu.drop(columns=["champion_id"]).merge(meta, on=key, how="inner")
    b["lb"] = np.log(b["burst_per_hit"].clip(lower=1))
    for pos, sub in b.groupby("teamPosition"):
        r = fit_adjust(sub, sub["lb"].to_numpy(dtype=float), "gauss")
        if r is not None:
            b.loc[sub.index, "burst_per_hit"] = np.exp(r[0])
            coefs.append({"position": pos, "metric": "tl_burst_per_hit(log)", "kind": "gauss", **r[1]})
    return t, b, pd.DataFrame(coefs)


def tl_aggregate(t: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """(포지션, 챔피언[시대]) 전체 기간 평균 → 타임라인 축 (h_champion_attributes.timeline_features 와 같은 규칙)."""
    g = t.groupby(["teamPosition", "champion_id"])
    out = g[TL_COLS].mean()
    out["n_tl"] = g.size()
    out["n_tl25"] = g["growth"].count()
    out["n_seg"] = g["late_seg"].count()
    out["n_end"] = g["late_end"].count()
    out["n_30_35"] = g["late_30_35"].count()
    bb = b.groupby(["teamPosition", "champion_id"]).agg(burst_per_hit=("burst_per_hit", "median"),
                                                        n_burst=("burst_per_hit", "size"))
    out = out.join(bb).reset_index()
    gp = out.groupby("teamPosition")
    P = lambda c, n: gp[c].transform(H.pct).where(out[n] >= H.MIN_RELIABLE)  # noqa: E731
    out["ax_growth"] = P("growth", "n_tl25")
    out["ax_dmg_late"] = P("dmg_curve", "n_tl25")
    out["ax_burst"] = P("burst_per_hit", "n_burst")
    out["ax_late_seg"] = P("late_seg", "n_seg")
    out["ax_late_end"] = P("late_end", "n_end")
    out["ax_gold_eff"] = P("gold_eff", "n_end")
    out["ax_late_30_35"] = P("late_30_35", "n_30_35")
    out = out.rename(columns={c: f"tl_{c}" for c in TL_COLS if c.startswith(("gold_diff", "dmg_share"))})
    return out.rename(columns={"teamPosition": "position"})



# ---------------------------------------------------------------- 궁극기 (킬 피해 스킬별, 2026-11-06)
KDMG_DIR = RAW / "riot" / "timeline_killdmg"
ULT_CACHE = OUT / "m0_ult.pkl"
FIGHT_GAP = 15       # 킬 사이가 이 초 이하면 같은 한타로 묶음
TRANSFORM = {"Nidalee", "Elise", "Jayce", "Udyr", "Ivern"}  # R 을 일반 스킬처럼 자주 씀 → 궁 사용 빈도 축에서 뺌
ULT_FRAC = ["ult_dmg_share", "ult_kill_rate", "ult_multi_rate"]
ULT_GAUSS = ["ult_aoe"]
ULT_W = {"ult_dmg_share": "kd_tot", "ult_kill_rate": "k_contrib", "ult_multi_rate": "r_fights", "ult_aoe": "r_fights"}


def ult_rows() -> pd.DataFrame:
    """경기 × 참가자 1행: 킬에 넣은 피해 중 궁(R) 피해, 기여한 킬 중 궁이 맞은 킬, 한타(15초 묶음)별 궁에 맞은 희생자 수.
    피해는 희생자가 죽기 직전 받은 피해 기록(victimDamageReceived) 기준 → 살아남은 적에게 준 궁은 세지 못함."""
    if ULT_CACHE.exists():
        return pd.read_pickle(ULT_CACHE)
    key2id = pd.read_csv(PROCESSED / "d_champions.csv").set_index("champion_key")["champion_id"].to_dict()
    rows = []
    files = sorted(glob.glob(str(KDMG_DIR / "*.json.gz")))
    for i, f in enumerate(files, 1):
        mid = Path(f).name[:-8]
        mf = H.MATCH_DIR / f"{mid}.json.gz"
        try:
            k = json.loads(gzip.decompress(Path(f).read_bytes()))
            info = json.load(gzip.open(mf))["info"]
        except Exception:
            continue
        ps = {p["participantId"]: p for p in info["participants"]}
        st = {pid: dict(kd_tot=0.0, kd_r=0.0, k_contrib=0, k_r=0, r_fights=0, r_victims=0, r_multi=0) for pid in ps}
        kills = sorted(k["kills"], key=lambda x: x[0])
        fights, cur, last = [], [], None
        for kl in kills:
            if last is not None and kl[0] - last > FIGHT_GAP:
                fights.append(cur)
                cur = []
            cur.append(kl)
            last = kl[0]
        if cur:
            fights.append(cur)
        for fight in fights:
            r_hit = {}
            for sec, killer, victim, hits in fight:
                seen, seen_r = set(), set()
                for pid, slot, basic, ph, mg, tr, name, typ in hits:
                    if typ != "OTHER" or pid not in st:
                        continue
                    dmg = (ph or 0) + (mg or 0) + (tr or 0)
                    st[pid]["kd_tot"] += dmg
                    seen.add(pid)
                    if slot == 3:
                        st[pid]["kd_r"] += dmg
                        seen_r.add(pid)
                        r_hit.setdefault(pid, set()).add(victim)
                for pid in seen:
                    st[pid]["k_contrib"] += 1
                for pid in seen_r:
                    st[pid]["k_r"] += 1
            for pid, vs in r_hit.items():
                st[pid]["r_fights"] += 1
                st[pid]["r_victims"] += len(vs)
                st[pid]["r_multi"] += int(len(vs) >= 2)
        for pid, v in st.items():
            p = ps[pid]
            rows.append({"matchId": mid, "teamId": p["teamId"], "teamPosition": p["teamPosition"],
                         "champion_id": key2id.get(p["championId"]), **v})
        if i % 5000 == 0:
            log.info("궁극기: 킬 피해 %d/%d", i, len(files))
    u = pd.DataFrame(rows)
    u["ult_dmg_share"] = u["kd_r"] / u["kd_tot"].where(u["kd_tot"] > 0)
    u["ult_kill_rate"] = u["k_r"] / u["k_contrib"].where(u["k_contrib"] > 0)
    u["ult_aoe"] = u["r_victims"] / u["r_fights"].where(u["r_fights"] > 0)
    u["ult_multi_rate"] = u["r_multi"] / u["r_fights"].where(u["r_fights"] > 0)
    pd.to_pickle(u, ULT_CACHE)
    log.info("궁극기 캐시 %d행 (%d경기)", len(u), u["matchId"].nunique())
    return u


def ult_adjust(u: pd.DataFrame, meta: pd.DataFrame):
    key = ["matchId", "teamId", "teamPosition"]
    t = u.drop(columns=["champion_id"]).merge(meta, on=key, how="inner")
    coefs = []
    for pos, sub in t.groupby("teamPosition"):
        for c in ULT_FRAC + ULT_GAUSS:
            kind = "frac" if c in ULT_FRAC else "gauss"
            r = fit_adjust(sub, sub[c].to_numpy(dtype=float), kind)
            if r is None:
                continue
            t.loc[sub.index, c] = r[0]
            coefs.append({"position": pos, "metric": c, "kind": kind, **r[1]})
    return t, pd.DataFrame(coefs)


def ult_aggregate(t: pd.DataFrame) -> pd.DataFrame:
    """(포지션, 챔피언[시대]) 전체 기간. 분모 가중 평균 (= 보정 전이면 합의 비율)."""
    out = []
    for (pos, ch), g in t.groupby(["teamPosition", "champion_id"]):
        r = {"position": pos, "champion_id": ch, "n_ult_games": len(g)}
        for c, w in ULT_W.items():
            ok = g[c].notna() & (g[w] > 0)
            r[c] = float(np.average(g.loc[ok, c], weights=g.loc[ok, w])) if ok.any() else np.nan
        r["n_kill_contrib"] = int(g["k_contrib"].sum())
        r["n_r_fights"] = int(g["r_fights"].sum())
        r["r_fights_per_game"] = g["r_fights"].mean()
        out.append(r)
    out = pd.DataFrame(out)
    gp = out.groupby("position")
    P = lambda c, ok: gp[c].transform(H.pct).where(ok)  # noqa: E731
    enough = out["n_ult_games"] >= H.MIN_RELIABLE
    out["ax_ult_dmg"] = P("ult_dmg_share", enough)
    out["ax_ult_hit"] = P("ult_kill_rate", enough)
    out["ax_ult_aoe"] = P("ult_aoe", out["n_r_fights"] >= H.MIN_RELIABLE)
    # 2026-11-06 확인: 궁 적중·다중 처치는 툴팁 광역 CC 궁을 가려내지 못함 (AUC 0.38~0.46, 위쪽은 스웨인·나서스 같은
    # 지속 피해 궁) → "한타형 궁"이 아니라 "궁으로 킬을 내는 정도"로 씀. 한타형 궁은 ax_ult_fight(툴팁) + 궁 유형 이름표(③)
    return out


# ---------------------------------------------------------------- 한 벌 만들기
def build(df: pd.DataFrame, st: pd.DataFrame, tp: pd.DataFrame, bu: pd.DataFrame, split: dict, tag: str):
    t0 = time.time()
    df = df.copy()
    df["champion_id"] = era_ids(df, split)
    st_e = st.copy()
    for c, p in split.items():
        extra = st[(st["champion_id"] == c) & ((st["patch"] < p) | (st["patch"] == "ALL"))].assign(champion_id=f"{c}@pre{p}")
        st_e = pd.concat([st_e, extra], ignore_index=True)
    adj, coefs, devs = nuisance_adjust(df)
    orig = H.per_row
    H.per_row = lambda x: adj.loc[x.index]
    try:
        at = H.attributes(df, st_e)
    finally:
        H.per_row = orig
    # 타임라인 (같은 경기만, 시대 반영)
    meta = df[["matchId", "teamId", "teamPosition", "champion_id", "win", "dur_min", "patch"]]
    t, b, tco = tl_adjust(tp[tp["matchId"].isin(set(df["matchId"]))], bu, meta)
    tf = tl_aggregate(t, b)
    at = at.merge(tf, on=["position", "champion_id"], how="left")
    # 궁극기 (같은 경기만, 시대 반영)
    uu, uco = ult_adjust(ULT[ULT["matchId"].isin(set(df["matchId"]))], meta)
    at = at.merge(ult_aggregate(uu), on=["position", "champion_id"], how="left")
    at.loc[at["champion_id"].astype(str).str.split("@pre").str[0].isin(TRANSFORM), "ax_ult_uptime"] = np.nan
    at = restore_era(at)
    lab = ult_labels()
    if lab is not None:
        # 툴팁 기반 옛 한타형 궁은 참고로 남기고, 한타형 궁은 사람이 확인한 라벨로 (2026-11-06)
        at = at.rename(columns={"ax_ult_fight": "ax_ult_fight_tooltip"}).merge(lab, on="champion_id", how="left")
    devs["champion_id"] = devs["champion_id"].astype(str)
    # 포지션 비율 (이 경기들에서 그 챔피언 판 중 이 포지션 비율)
    base_id = df["champion_id"].astype(str).str.split("@pre").str[0]
    ps = df.assign(c=base_id).groupby(["c", "teamPosition"]).size()
    ps = (ps / ps.groupby(level=0).transform("sum")).rename("pos_share").reset_index() \
        .rename(columns={"c": "champion_id", "teamPosition": "position"})
    at = at.merge(ps, on=["champion_id", "position"], how="left")
    at["offrole"] = at["pos_share"] < OFFROLE
    log.info("M0 %s: %d경기 (타임라인 %d · 킬 피해 %d) · 속성표 %d행 · %.0f초", tag, df["matchId"].nunique(),
             t["matchId"].nunique(), uu["matchId"].nunique(), len(at), time.time() - t0)
    return at, pd.concat([coefs, tco, uco], ignore_index=True), devs, adj, df


# ---------------------------------------------------------------- 평가
AXES = ["ax_frontline", "ax_engage", "ax_catch", "ax_protect", "ax_carry", "ax_dive", "ax_split", "ax_lane", "ax_poke",
        "ax_ult_fight_tooltip", "ax_ult_dmg", "ax_ult_hit", "ax_ult_aoe", "ax_ult_uptime", "ax_growth", "ax_late_end", "ax_late_seg",
        "ax_burst", "ax_range"]
FA_COLS = [c for c in RATE + FRAC + GAUSS if c != "solo_kill_share"]


def winrate_corr(at: pd.DataFrame) -> dict:
    from scipy.stats import spearmanr
    a = at[(at["patch"] == "ALL") & at["reliable"]]
    out = {}
    for ax in AXES:
        if ax not in a:
            continue
        rs = [spearmanr(g[ax], g["win"], nan_policy="omit").statistic for _, g in a.groupby("position")
              if g[ax].notna().sum() >= 10]
        out[ax] = float(np.nanmedian(rs)) if rs else np.nan
    return out


def factor_analysis(at: pd.DataFrame, k: int = 7) -> list[str]:
    from sklearn.decomposition import FactorAnalysis
    a = at[(at["patch"] == "ALL") & at["reliable"]].copy()
    X = a[FA_COLS].groupby(a["position"]).transform(lambda s: (s - s.mean()) / (s.std() or 1)).fillna(0)
    fa = FactorAnalysis(n_components=k, rotation="varimax", random_state=0).fit(X)
    L = ["| 요인 | 크게 실린 지표 (적재량 ≥ 0.4) |", "|---|---|"]
    for i, comp in enumerate(fa.components_, 1):
        s = pd.Series(comp, index=FA_COLS)
        s = s[s.abs() >= 0.4].sort_values(key=np.abs, ascending=False)
        L.append(f"| F{i} | " + ", ".join(f"{c} {v:+.2f}" for c, v in s.items()) + " |")
    return L


def tl_stability(t: pd.DataFrame) -> list[str]:
    from scipy.stats import spearmanr
    h = t["matchId"].map(lambda s: zlib.crc32(s.encode()) % 2)
    L = ["| 값 | 행 | 반분 순위 상관 (포지션 안 중앙값) |", "|---|---|---|"]
    for c in ("growth", "late_end", "late_seg", "gold_diff15"):
        ab = [t[h == k].groupby(["teamPosition", "champion_id"])[c].agg(["mean", "count"]) for k in (0, 1)]
        j = ab[0].join(ab[1], lsuffix="_a", rsuffix="_b", how="inner")
        j = j[(j["count_a"] >= 30) & (j["count_b"] >= 30)].reset_index()
        r = np.nanmedian([spearmanr(x["mean_a"], x["mean_b"]).statistic for _, x in j.groupby("teamPosition") if len(x) >= 8])
        L.append(f"| {c} | {len(j)} | {r:.3f} |")
    return L



def label_check(at: pd.DataFrame) -> list[str]:
    """사람 라벨 ↔ 데이터 축이 서로 맞는지 (궁 유형별 평균 백분위, 한타형 궁의 이름표 AUC)."""
    if "ult_type" not in at:
        return ["(ult_types.csv 없음)"]
    a = at[(at["patch"] == "ALL") & at["reliable"]]
    L = ["| 궁 유형 (사람 확인) | 챔피언×포지션 행 | 궁 딜 비중 (ax_ult_dmg) | 궁 킬 관여 (ax_ult_hit) | 궁 다중 처치 (ax_ult_aoe) | 이니시 (ax_engage) |",
         "|---|---|---|---|---|---|"]
    for t, g in a.groupby("ult_type"):
        L.append(f"| {t} | {len(g)} | {g['ax_ult_dmg'].mean():.0f} | {g['ax_ult_hit'].mean():.0f} | {g['ax_ult_aoe'].mean():.0f} | "
                 f"{g['ax_engage'].mean():.0f} |")
    y = a["meraki_roles"].fillna("").str.contains("VANGUARD").to_numpy()
    tf = a["ult_teamfight"].to_numpy(dtype=float)
    cc = (a["ult_type"] == "광역 CC").to_numpy(dtype=float)
    L += ["", f"- 한타형 궁(광역 CC 또는 범위 Y) 비율: {np.nanmean(tf):.0%} (전체 기간·30판 이상 {len(a)}행)",
          f"- 이니시 이름표(VANGUARD) 가려내기 AUC: 한타형 궁 {H.auc(y, tf):.3f} · 광역 CC 만 {H.auc(y, cc):.3f} · "
          f"옛 툴팁 축 {H.auc(y, a['ax_ult_fight_tooltip'].to_numpy()):.3f}",
          f"- 툴팁 궁 광역 CC 판정과 일치: {((a['st_r_aoe_cc'].fillna(False).astype(bool)) == (a['ult_type'] == '광역 CC')).mean():.0%}",
          "- 데이터 축이 라벨과 맞으면: '딜' 행의 궁 딜 비중이 가장 높고, '광역 CC' 행의 이니시가 높아야 함"]
    return L


def report(finfo, at, base, m0v1, coefs, devs, st, rw, split, t_adj) -> str:
    from scipy.stats import spearmanr
    L = ["# M0 속성 보정 평가 (완성, 궁극기 포함)", "",
         f"- 자료: 솔랭 {finfo['games']:,}경기 → 잠수 판 {finfo['dropped_games']:,}경기 제외 "
         f"(wasAfk {finfo['wasAfk_games']:,} · 타임라인 골드 정지 {finfo['stall_games']:,} · 둘 다 {finfo['both']:,}; 타임라인 있는 경기 {finfo['tl_games']:,})",
         f"- 골드 정지 기준 검산 (wasAfk 값이 있는 판): 잠수 {finfo['check_afk_rows']}명 중 {finfo['check_afk_caught']}명 잡음, "
         f"정상 {finfo['check_ok_rows']:,}명 중 {finfo['check_ok_flagged']}명 잘못 잡음",
         f"- 기준: 5분 골드 증가 < {STALL_GAIN} 또는 10분 골드 < {GOLD10_MIN}. 챔피언별 승패 효과 당기기 K = {K_WIN}", ""]
    # 1. 계수
    L += ["## 1. 승패·게임 길이 효과 (포지션 중앙값)", "",
          "분당 값은 배수, 몫은 로짓 계수, 정규는 원래 단위. `챔피언별 편차 SD` = 당긴 뒤 챔피언별 승패 효과가 공통값에서 벗어난 정도", "",
          "| 지표 | 종류 | 승리 | 짧은 판 | 긴 판 | 챔피언별 편차 SD |", "|---|---|---|---|---|---|"]
    for m, g in coefs.groupby("metric", sort=False):
        k = g["kind"].iloc[0]
        f = (lambda v: f"×{np.exp(v):.2f}") if k == "rate" else (lambda v: f"{v:+.2f}")
        L.append(f"| {m} | {k} | {f(g['b_win'].median())} | {f(g['dur_short'].median())} | {f(g['dur_long'].median())} | "
                 f"{g['dev_sd'].median():.3f} |")
    # 챔피언별 승패 효과가 큰 예
    dd = devs[devs["metric"].isin(["dmg_share", "taken_share", "multikills_pm"])].copy()
    nm = st.loc[st["patch"] == "ALL"].set_index("champion_id")["name_ko"]
    L += ["", "챔피언별 승패 효과가 공통값과 가장 다른 예 (딜 몫·받은 피해 몫·멀티킬, 위·아래 3개):", ""]
    for (m, p), g in dd.groupby(["metric", "position"]):
        if p not in ("BOTTOM", "MIDDLE", "TOP"):
            continue
        g = g.sort_values("win_dev")
        f = lambda r: f"{nm.get(str(r.champion_id).split('@')[0], r.champion_id)} {r.win_dev:+.3f}"  # noqa: E731
        L.append(f"- {m} · {p}: 위 " + ", ".join(f(r) for r in g.tail(3)[::-1].itertuples())
                 + " / 아래 " + ", ".join(f(r) for r in g.head(3).itertuples()))
    # 2. 보정 전후
    wa, wb, w1 = winrate_corr(at), winrate_corr(base), winrate_corr(m0v1) if m0v1 is not None else {}
    b = base[(base["patch"] == "ALL") & base["reliable"]].set_index(["position", "champion_id"])
    a = at[(at["patch"] == "ALL") & at["reliable"]].set_index(["position", "champion_id"])
    j = a.join(b, rsuffix="_b", how="inner")
    L += ["", "## 2. 보정 전후 (전체 기간, 판수 30 이상) — 기준 = 보정 전 속성표 (h_champion_attributes.csv)", "",
          "| 축 | 전후 순위 상관 | 15점 넘게 바뀐 행 | 챔피언 승률과 상관: 보정 전 → M0 1차 → 2차 |", "|---|---|---|---|"]
    for ax in AXES:
        if ax not in j or f"{ax}_b" not in j:
            continue
        v = j[[ax, f"{ax}_b"]].dropna()
        if len(v) < 10:
            continue
        r = spearmanr(v[ax], v[f"{ax}_b"]).statistic
        L.append(f"| {ax} | {r:.3f} | {int(((v[ax] - v[f'{ax}_b']).abs() > 15).sum())}/{len(v)} | "
                 f"{wb.get(ax, np.nan):+.2f} → {w1.get(ax, np.nan):+.2f} → {wa.get(ax, np.nan):+.2f} |")
    L += ["", "- 승률과의 상관이 0 쪽으로 가면 \"이겨서 생긴 값\"이 빠졌다는 뜻", "", "가장 크게 바뀐 행:", ""]
    for ax in ["ax_growth", "ax_late_end", "ax_burst", "ax_split", "ax_carry"]:
        if ax not in j:
            continue
        v = j[[ax, f"{ax}_b", "name_ko", "win"]].dropna(subset=[ax, f"{ax}_b"]).copy()
        v["d"] = v[ax] - v[f"{ax}_b"]
        top = v.reindex(v["d"].abs().sort_values(ascending=False).index).head(3)
        L.append(f"- {ax}: " + ", ".join(f"{r.name_ko}({p[:3]}) {r[f'{ax}_b']:.0f}→{r[ax]:.0f} (승률 {r.win:.0%})"
                                          for (p, _), r in top.iterrows()))
    L += ["", "### 타임라인 축 안정성 (보정 후, 경기 반분)", ""] + tl_stability(t_adj)
    L += ["", "### 궁 유형 라벨 (사람 확인, research/config/ult_types.csv) ↔ 데이터 축", ""] + label_check(at)
    # 3. 요인분석
    L += ["", "## 3. 요인분석 (전체 기간, 포지션 안 표준화, varimax 7요인)", ""] + factor_analysis(at)
    # 4. 큰 변경
    L += ["", "## 4. 큰 변경 (리워크) — 변경 전후를 나눌지", "",
          f"패치노트 후보 → 변경 전후 지표 차이(챔피언 간 SD 단위 평균)가 같은 날짜로 나눈 다른 챔피언들보다 큰 분위 ≥ {REWORK_Q:.0%} 이면 나눔", "",
          "| 패치 | 챔피언 | 변경 줄 (업데이트) | 변경 전 판 | 변경 후 판 | 차이 | 분위 | 나눔 |", "|---|---|---|---|---|---|---|---|"]
    for r in rw.itertuples():
        L.append(f"| {r.riot_patch} | {nm.get(r.champion_id, r.champion_id)} | {r.lines} ({r.updates}) | {r.n_pre} | {r.n_post} | "
                 f"{r.shift:.2f} | {r.quantile:.0%} | {'예' if r.split else '아니오'} |")
    L += ["", f"- 나눈 챔피언: {', '.join(f'{nm.get(c, c)}({p})' for c, p in split.items()) or '없음'} → 변경 전 경기는 `era = pre{{패치}}`, 전체 기간 행 patch = `ALL<{{패치}}`", ""]
    # 5. 포지션 비율
    off = at[(at["patch"] == "ALL") & at["offrole"] & (at["n"] >= 30)]
    L += ["## 5. 비주류 포지션 행 (`pos_share` < 5%, 전체 기간, 판수 30 이상)", "",
          f"{len(off)}행: " + ", ".join(f"{r.name_ko}({r.position[:3]} {r.pos_share:.0%}, {r.n}판)" for r in off.sort_values('n', ascending=False).head(25).itertuples()), ""]
    L += ["---", "", "아래는 기존 속성표 평가를 보정한 표에 그대로 돌린 결과", ""]
    return "\n".join(L)



# ---------------------------------------------------------------- 궁 유형 이름표 초안 (③, 사람 확인용)
ULT_TYPES = ["딜", "광역 CC", "단일 CC", "자기 강화", "아군 보호", "이동·전역"]
KW = {
    "아군 보호": r"아군.{0,30}(보호막|회복|무적|부활|사망하지|죽지|피해를 받지|체력이 .{0,20}이하로)|(보호막|회복).{0,30}아군|대상 아군",
    "자기 강화": r"공격력|공격 속도|방어력|마법 저항력|피해 감소|피해를 덜|변신|형태|강화되|강화합|추가 효과를 얻|체력을 회복",
    "이동·전역": r"순간이동|도약|돌진|날아|맵|전역|어디로든|위치로 이동|빠르게 이동|이동 속도가 .{0,20}증가",
    "딜": r"피해를 입힙|물리 피해|마법 피해|고정 피해",
}


def dd_r_texts() -> pd.DataFrame:
    import re
    dirs = [d for d in (RAW / "ddragon").iterdir() if (d / "championFull_ko_KR.json").exists()]
    latest = max(dirs, key=lambda d: tuple(int(x) for x in d.name.split(".")))
    data = json.load(open(latest / "championFull_ko_KR.json", encoding="utf-8"))["data"]
    tag = re.compile(r"<[^>]+>|\{\{[^}]*\}\}")
    rows = []
    for key, c in data.items():
        r = c["spells"][3]
        rows.append({"champion_id": key, "r_name": r["name"],
                     "r_text": tag.sub("", r.get("description", "") + " " + r.get("tooltip", "")).strip()})
    return pd.DataFrame(rows), latest.name


ULT_LABELS = H.ROOT / "research" / "config" / "ult_types.csv"  # 사람이 확인한 궁 유형 (2026-11-06)


def ult_labels() -> pd.DataFrame | None:
    """research/config/ult_types.csv → champion_id, ult_type, ult_label_aoe(0/1), ult_teamfight(0/1).
    한타형 궁 = 광역 CC 이거나 여러 명에게 걸림(confirmed_aoe = Y). 사일러스(상대 궁)는 빈칸 → M1 에서 상대 궁으로 채움."""
    if not ULT_LABELS.exists():
        return None
    d = pd.read_csv(ULT_LABELS, dtype=str, encoding="utf-8-sig")
    ok = set(ULT_TYPES) | {"상대 궁"}
    bad = d[~d["confirmed_type"].isin(ok)]
    if len(bad):
        raise SystemExit(f"ult_types.csv 유형 표기 오류: {bad[['name_ko', 'confirmed_type']].values.tolist()}")
    aoe = d["confirmed_aoe"].str.strip().str.upper().map({"Y": 1.0, "N": 0.0})
    # 칸 이름: 데이터 축의 ult_aoe(한타당 궁에 맞고 죽은 적 수)와 겹치지 않게 ult_label_aoe
    out = pd.DataFrame({"champion_id": d["champion_id"], "ult_type": d["confirmed_type"], "ult_label_aoe": aoe})
    out["ult_teamfight"] = np.where(out["ult_type"] == "상대 궁", np.nan,
                                    ((out["ult_type"] == "광역 CC") | (out["ult_label_aoe"] == 1)).astype(float))
    return out


def ult_types_draft(at: pd.DataFrame) -> pd.DataFrame:
    import re
    rt, ver = dd_r_texts()
    a = at[at["patch"] == "ALL"].sort_values("n", ascending=False).drop_duplicates("champion_id")
    a = a.merge(rt, on="champion_id", how="left")
    out = []
    for r in a.itertuples():
        txt = r.r_text if isinstance(r.r_text, str) else ""
        kw = {k: bool(re.search(v, txt)) for k, v in KW.items()}
        dmg_hi = (r.ax_ult_dmg >= 60) if pd.notna(r.ax_ult_dmg) else False
        cc_txt = bool(H.HARD_CC.search(txt))
        aoe_txt = bool(H.AOE.search(txt))
        aoe_hi = (r.ax_ult_aoe >= 60) if pd.notna(r.ax_ult_aoe) else False
        ev = []
        if bool(r.st_r_aoe_cc) or (cc_txt and aoe_txt):  # 초안 규칙 (확정은 research/config/ult_types.csv)
            prim = "광역 CC"; ev.append("툴팁 궁 광역 CC" if bool(r.st_r_aoe_cc) else "궁 설명: 단단한 CC + 범위")
        elif bool(r.st_r_hard_cc) or cc_txt:
            prim = "단일 CC"; ev.append("툴팁 궁 단단한 CC" if bool(r.st_r_hard_cc) else "궁 설명: 단단한 CC")
        elif kw["아군 보호"]:
            prim = "아군 보호"; ev.append("툴팁: 아군 보호막·회복·무적")
        elif dmg_hi or (kw["딜"] and not kw["자기 강화"]):
            prim = "딜"; ev.append("관측 궁 딜 비중 상위" if dmg_hi else "툴팁: 피해")
        elif bool(r.r_global) or kw["이동·전역"]:
            prim = "이동·전역"; ev.append("궁 사거리 3000+" if bool(r.r_global) else "툴팁: 이동")
        elif kw["자기 강화"]:
            prim = "자기 강화"; ev.append("툴팁: 강화·변신")
        else:
            prim = "확인 필요"
        sec = [t for t in ULT_TYPES if t != prim and (
            (t == "딜" and (dmg_hi or kw["딜"])) or (t == "광역 CC" and aoe_hi and prim != "광역 CC")
            or (t == "이동·전역" and (bool(r.r_global) or kw["이동·전역"])) or (t in ("자기 강화", "아군 보호") and kw[t]))]
        out.append({"champion_id": r.champion_id, "name_ko": r.name_ko, "main_position": r.position, "n": r.n,
                    "suggested_type": prim, "secondary": " · ".join(sec), "evidence": " · ".join(ev),
                    "ult_dmg_share": round(r.ult_dmg_share, 3) if pd.notna(r.ult_dmg_share) else np.nan,
                    "ax_ult_dmg": r.ax_ult_dmg, "ult_aoe": round(r.ult_aoe, 2) if pd.notna(r.ult_aoe) else np.nan,
                    "ax_ult_aoe": r.ax_ult_aoe, "ax_ult_hit": r.ax_ult_hit, "r_name": r.r_name,
                    "r_text": (txt[:160] + "…") if len(txt) > 160 else txt, "confirmed_type": "", "memo": ""})
    d = pd.DataFrame(out)
    d.attrs["ddragon"] = ver
    return d.sort_values(["suggested_type", "name_ko"])


# ---------------------------------------------------------------- 축별 신뢰도
VALID = {"ax_frontline": ("TANK", None), "ax_engage": ("VANGUARD", ["JUNGLE", "UTILITY", "TOP"]),
         "ax_catch": ("CATCHER", ["UTILITY"]), "ax_protect": ("ENCHANTER", ["UTILITY"]), "ax_carry": ("MARKSMAN", None),
         "ax_dive": ("ASSASSIN", ["MIDDLE", "JUNGLE"]), "ax_split": ("JUGGERNAUT", ["TOP"]),
         "ax_poke": ("ARTILLERY", ["MIDDLE", "UTILITY"]), "ax_range": ("MARKSMAN|ARTILLERY", None),
         "ax_ult_fight_tooltip": ("VANGUARD", None)}
PER_PATCH = {"ax_frontline", "ax_engage", "ax_catch", "ax_protect", "ax_carry", "ax_dive", "ax_split", "ax_lane",
             "ax_poke", "ax_range", "ax_ult_uptime"}


def oe_by_champ() -> pd.DataFrame:
    oe = pd.read_csv(PROCESSED / "a_oe_lck_2026_raw.csv", usecols=["date", "position", "champion", "damagetakenperminute",
                                                                    "damageshare", "golddiffat10", "golddiffat15", "golddiffat25"])
    oe = oe[oe["position"].isin(H.OE_POS) & (oe["date"].str[:10] <= H.research_windows()["window_to"].max())]
    nm = pd.read_csv(PROCESSED / "d_champion_name_map.csv").set_index("alias")["champion_id"]
    oe["champion_id"] = oe["champion"].map(lambda x: nm.get(H.norm(x)))
    oe["position"] = oe["position"].map(H.OE_POS)
    oe["oe_growth"] = oe["golddiffat25"] - oe["golddiffat10"]
    return oe.groupby(["position", "champion_id"]).agg(n_oe=("damageshare", "size"), oe_taken=("damagetakenperminute", "mean"),
                                                      oe_share=("damageshare", "mean"), oe_gd15=("golddiffat15", "mean"),
                                                      oe_growth=("oe_growth", "mean")).reset_index()


def reliability(at: pd.DataFrame, halves: list[pd.DataFrame]) -> tuple[list[str], pd.DataFrame]:
    from scipy.stats import spearmanr
    a = at[(at["patch"] == "ALL") & at["reliable"]]
    h0, h1 = [h[h["patch"] == "ALL"] for h in halves]
    hj = h0.merge(h1, on=["position", "champion_id"], suffixes=("_a", "_b"))
    hj = hj[(hj["n_a"] >= 30) & (hj["n_b"] >= 30)]
    oe = oe_by_champ()
    xo = a.merge(oe[oe["n_oe"] >= 5], on=["position", "champion_id"])
    pro = {"ax_frontline": "oe_taken", "ax_carry": "oe_share", "ax_lane": "oe_gd15", "ax_growth": "oe_growth"}
    pp = at[~at["patch"].str.startswith("ALL") & (at["n"] >= 30)]
    ps = sorted(pp["patch"].unique())
    wr = winrate_corr(at)
    rows = []
    for ax in AXES:
        if ax not in a:
            continue
        med = lambda xs: float(np.nanmedian(xs)) if len(xs) else np.nan  # noqa: E731
        sh = med([spearmanr(g[f"{ax}_a"], g[f"{ax}_b"], nan_policy="omit").statistic for _, g in hj.groupby("position")
                  if g[[f"{ax}_a", f"{ax}_b"]].dropna().shape[0] >= 8])
        adj = np.nan
        if ax in PER_PATCH:
            cs = []
            for p1, p2 in zip(ps, ps[1:]):
                x = pp[pp["patch"] == p1].merge(pp[pp["patch"] == p2], on=["position", "champion_id"], suffixes=("_a", "_b"))
                for _, g in x.groupby("position"):
                    v = g[[f"{ax}_a", f"{ax}_b"]].dropna()
                    if len(v) >= 8:
                        cs.append(spearmanr(v.iloc[:, 0], v.iloc[:, 1]).statistic)
            adj = med(cs)
        auc = np.nan
        lab = ""
        if ax in VALID:
            lab, pos = VALID[ax]
            b = a if pos is None else a[a["position"].isin(pos)]
            y = b["meraki_roles"].fillna("").str.contains(lab).to_numpy()
            auc = H.auc(y, b[ax].to_numpy())
            lab = f"{lab}" + ("" if pos is None else f" ({'·'.join(q[:3] for q in pos)})")
        pr = np.nan
        if ax in pro:
            pr = med([spearmanr(g[ax], g[pro[ax]], nan_policy="omit").statistic for _, g in xo.groupby("position")
                      if g[[ax, pro[ax]]].dropna().shape[0] >= 8])
        filled = int(a[ax].notna().sum())
        if (sh >= 0.85 or np.isnan(sh)) and (np.isnan(auc) or auc >= 0.8):
            grade = "높음"
        elif sh >= 0.7 and (np.isnan(auc) or auc >= 0.7):
            grade = "보통"
        else:
            grade = "낮음"
        rows.append({"axis": ax, "filled_rows": filled, "split_half": sh, "adjacent_patch": adj, "validity_label": lab,
                     "validity_auc": auc, "pro_agree": pr, "winrate_corr": wr.get(ax, np.nan), "grade": grade})
    r = pd.DataFrame(rows)
    f = lambda v: "—" if pd.isna(v) else f"{v:.2f}"  # noqa: E731
    L = ["| 축 | 값 있는 행 (전체 기간, 30판+) | 반분 안정성 | 인접 패치 | 타당성 (이름표 AUC) | 대회 일치 | 승률 상관 | 신뢰도 |",
         "|---|---|---|---|---|---|---|---|"]
    for x in r.itertuples():
        L.append(f"| {x.axis} | {x.filled_rows} | {f(x.split_half)} | {f(x.adjacent_patch)} | "
                 f"{(x.validity_label + ' ' + f(x.validity_auc)) if x.validity_label else '—'} | {f(x.pro_agree)} | "
                 f"{f(x.winrate_corr)} | {x.grade} |")
    L += ["", "- 반분 안정성: 경기를 반으로 나눠 따로 만든 표끼리 같은 포지션 안 순위 상관 (중앙값). 판수 30 이상 행끼리",
          "- 인접 패치: 패치별 값이 있는 축만, 이웃한 패치끼리 같은 포지션 안 순위 상관 (중앙값). 타임라인·궁극기 축은 전체 기간 값 하나라 —",
          "- 타당성: Meraki 역할 이름표를 이 축 점수가 가려내는 정도 (0.5 = 못 가림, 1 = 완벽). 이름표가 없는 축은 —",
          "- 대회 일치: LCK 경기 기록(OE)의 비슷한 값과 같은 포지션 안 순위 상관 (대회 5판·솔랭 30판 이상)",
          "- 신뢰도: 높음 = 반분 ≥ 0.85 이고 AUC ≥ 0.8 (없으면 통과), 보통 = 반분 ≥ 0.7 이고 AUC ≥ 0.7, 그 밖 낮음"]
    return L, r


# ---------------------------------------------------------------- main
def save(df: pd.DataFrame, path: Path) -> None:
    """엑셀 등으로 열려 있어 잠긴 파일이면 이름 뒤에 _v2 를 붙여 저장."""
    try:
        H.save_csv(df, path)
    except PermissionError:
        alt = path.with_name(path.stem + "_v2" + path.suffix)
        H.save_csv(df, alt)
        log.warning("%s 가 잠겨 있어 %s 로 저장", path.name, alt.name)


ULT = pd.DataFrame()


def main():
    global ULT
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["full", "folds", "asof", "all"], default="all")
    ap.add_argument("--patches", default="26.14,26.15", help="asof 를 만들 LCK 패치 (쉼표). 2026-11-08: M5 리허설 확대로 26.08·26.09·26.10 추가")
    a = ap.parse_args()
    raw = pd.read_csv(H.PARTS, dtype={"patch": str, "game_version": str, "matchId": str, "date_window": str})
    fr, bu, sg = timeline_rows()
    tp = tl_participants(fr, sg)
    df, finfo, bad = filter_games(raw, tp)
    log.info("잠수 판 제외: %s", finfo)
    tp = tp[~tp["matchId"].isin(bad)]
    ULT = ult_rows()
    ULT = ULT[~ULT["matchId"].isin(bad)]
    bu = bu[~bu["matchId"].isin(bad)]
    st = pd.read_csv(PROCESSED / "h_champion_static.csv", dtype={"patch": str})
    base = pd.read_csv(PROCESSED / "h_champion_attributes.csv", dtype={"patch": str})
    v1p = OUT / "m0_attributes.csv"
    m0v1 = pd.read_csv(v1p, dtype={"patch": str}) if v1p.exists() and "era" not in pd.read_csv(v1p, nrows=1).columns else None
    split, rw = rework_split(df)
    save(rw, OUT / "m0_rework.csv")
    log.info("큰 변경 나눔: %s", split)

    if a.only in ("full", "all"):
        at, coefs, devs, adj, dfe = build(df, st, tp, bu, split, "full")
        save(at, OUT / "m0_attributes.csv")
        save(coefs, OUT / "m0_nuisance.csv")
        save(devs, OUT / "m0_champ_win.csv")
        meta = dfe[["matchId", "teamId", "teamPosition", "champion_id", "win", "dur_min", "patch"]]
        t_adj, _, _ = tl_adjust(tp, bu, meta)
        rep = report(finfo, at, base, m0v1, coefs, devs, st, rw, split, t_adj)
        hk = df["matchId"].map(lambda s_: zlib.crc32(s_.encode()) % 2)
        halves = [build(df[hk == k], st, tp, bu, split, f"half{k}")[0] for k in (0, 1)]
        rel_md, rel = reliability(at, halves)
        save(rel, OUT / "m0_reliability.csv")
        ud = ult_types_draft(at)
        save(ud, OUT / "m0_ult_types_draft.csv")
        cnt = ud["suggested_type"].value_counts()
        rep = rep.replace("## 1. 승패·게임 길이 효과", "## 0. 축별 신뢰도\n\n" + "\n".join(rel_md)
                          + "\n\n## 0-1. 궁 유형 이름표 초안 (`m0_ult_types_draft.csv`, Data Dragon " + ud.attrs.get("ddragon", "")
                          + ", 사람 확인 필요)\n\n" + ", ".join(f"{k} {v}" for k, v in cnt.items()) + "\n\n## 1. 승패·게임 길이 효과", 1)
        orig = H.per_row
        H.per_row = lambda x: adj.loc[x.index]
        try:
            at_eval = at[~at["patch"].str.startswith("ALL<")].copy()
            if "ax_ult_fight_tooltip" in at_eval:  # 기존 평가 함수는 옛 이름을 씀
                at_eval["ax_ult_fight"] = at_eval["ax_ult_fight_tooltip"]
            rep += "\n" + H.evaluate(dfe, at_eval, st)
        finally:
            H.per_row = orig
        (OUT / "m0_eval.md").write_text(rep, encoding="utf-8")
    if a.only in ("folds", "all"):
        fold = df["matchId"].map(lambda s: zlib.crc32(s.encode()) % 5)
        for k in range(5):
            at_k = build(df[fold != k], st, tp, bu, split, f"fold{k}")[0]
            save(at_k, OUT / f"m0_attributes_fold{k}.csv")
    if a.only in ("asof", "all"):
        w = H.research_windows().set_index("patch")
        for p in a.patches.split(","):
            cut = w.loc[p, "window_from"]
            sp = {c: q for c, q in split.items() if q < p}  # 그 시점에 아직 없던 변경은 나누지 않음
            at_p = build(df[df["day"] < cut], st, tp, bu, sp, f"asof_{p} (< {cut})")[0]
            save(at_p, OUT / f"m0_attributes_asof_{p}.csv")
    log.info("M0 끝")


if __name__ == "__main__":
    main()
