"""M1 조합 승패 — 어떤 조합 성격이 이기게 하나 (docs/PIPELINE.md 7-7, 2026-11-06 1차)

행 = 경기 1개 (블루 기준). 변수 = 블루 − 레드 조합 성격 차이, 결과 = 블루 승. 진영 효과는 절편.
자료: 솔랭(M0 과 같은 27,177경기) + 대회 5리그(LCK·LCKC·LPL·LEC·LCS, OE, 08-25 까지)
계층: 공통 계수(솔랭이 대부분 결정) + 대회 공통 편차 + 리그별 편차. 편차 칸에 작은 배율(c_pro, c_league)을 곱해
     같은 규제에서 0 쪽으로 더 세게 당김 (feature augmentation). 엘라스틱넷 로지스틱.
통제: 솔랭 = 진영별 리서치 프로 수 차이 (점검 B2), 대회 = 경기 전 리그 안 Elo 차이.
속성: 솔랭 행 = M0 교차 5벌 (자기 경기 묶음을 뺀 표), 대회 행 = M0 전체 표.
      설정 고르기(26.14·26.15) 때는 그 시점 표(asof)를 모든 행에 씀 (점검 B1).
사일러스(궁 유형 "상대 궁")의 한타형 궁 = 그 경기 상대 4~5명 한타형 궁 비율.
2차 (2026-11-06): 상호작용 196칸 (상대 사이 b_x·r_y − r_x·b_y 91 + 같은 팀 b_x·b_y − r_x·r_y 105, 공통 칸만, 배율 c_int),
     대회 숙련 통제 (선수 × 챔피언 이전 대회 판수, 팀 평균 log(1+n) 차이). 정식 IPW 는 M4 뒤.

절차 (7-7 검증 규칙):
  1) 설정 고르기: cut = 26.14·26.15 LCK 창 첫날. 학습 = cut 이전 경기(그 패치 대회 경기 제외), 평가 = 그 패치 대회 경기.
     기준선(리그별 진영 승률, Elo 만)과 로그 손실 비교. 보정 곡선 (점검 A4)
  2) 고른 설정으로 08-25 까지 전부 재학습, 경기 단위 부트스트랩 구간, 5묶음 교차 예측(M2 오프셋용)
  플레이오프(08-26~)는 여기서 보지 않음 (한 번만 검증 — 전체 시스템 끝에서)

    python src/pipeline/m1_comp.py            # 설정 고르기 + 최종 학습
    python src/pipeline/m1_comp.py --boot 50  # 부트스트랩 횟수
"""
from __future__ import annotations

import argparse
import itertools
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score, brier_score_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
from d_champion_meta import norm  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
M0 = PROCESSED / "m0"
OUT = PROCESSED / "m1"
OUT.mkdir(exist_ok=True)
OE_FILE = H.ROOT / "data" / "inven_2026" / "games" / "2026_LoL_esports_match_data_from_OraclesElixir.csv"
LEAGUES = ["LCK", "LCKC", "LPL", "LEC", "LCS"]
END = "2026-08-25"
POS = H.POS

# 조합 성격 (팀 5명 평균, 축 = 포지션 안 백분위). 라인전은 탑·미드·서폿만 값이 있어 빈칸 = 50
AX = {"frontline": "ax_frontline", "engage": "ax_engage", "protect": "ax_protect", "dive": "ax_dive",
      "carry": "ax_carry", "poke": "ax_poke", "split": "ax_split", "late": "ax_late_end", "growth": "ax_growth",
      "burst": "ax_burst", "ult_dmg": "ax_ult_dmg", "lane": "ax_lane"}
FEATS = list(AX) + ["range_aa", "range_skill", "melee_n", "dmg_imbalance", "teamfight_ult_n", "cc_ult_n",
                    "protect_ult_n"]
NAMES = {"frontline": "앞라인", "engage": "이니시", "protect": "보호", "dive": "킬 결정력", "carry": "딜 비중 (캐리 성향)",
         "poke": "포킹", "split": "스플릿", "late": "후반 캐리", "growth": "중반 스노볼", "burst": "순간 딜",
         "ult_dmg": "궁 딜", "lane": "라인전", "range_aa": "기본 공격 사거리", "range_skill": "스킬 사거리",
         "melee_n": "근접 챔피언 수", "dmg_imbalance": "딜 쏠림 (AD·AP 한쪽)", "teamfight_ult_n": "한타형 궁 수",
         "cc_ult_n": "광역 CC 궁 수", "protect_ult_n": "아군 보호 궁 수"}
# 상호작용 (2차): 팀 값(표준화) 끼리의 곱. 상대 사이 = b_x·r_y − r_x·b_y (진영을 바꾸면 부호만 바뀜),
# 같은 팀 안 = b_x·b_y − r_x·r_y (x = y 이면 제곱 = 많을수록 효과가 줄어드는지/커지는지)
IX = list(AX) + ["range_skill", "cc_ult_n"]
PAIRS_X = list(itertools.combinations(range(len(IX)), 2))
PAIRS_W = list(itertools.combinations_with_replacement(range(len(IX)), 2))
IX_NAMES = ([f"상대: 우리 {NAMES[IX[i]]} × 상대 {NAMES[IX[j]]}" for i, j in PAIRS_X]
            + [f"같은 팀: {NAMES[IX[i]]} × {NAMES[IX[j]]}" if i != j else f"같은 팀: {NAMES[IX[i]]} 제곱" for i, j in PAIRS_W])
IX_KEYS = [f"x:{IX[i]}*{IX[j]}" for i, j in PAIRS_X] + [f"w:{IX[i]}*{IX[j]}" for i, j in PAIRS_W]


# ---------------------------------------------------------------- 자료
def oe_patch_to_ours(p: str) -> str | None:
    try:
        a, b = str(p).split(".")[:2]
        return f"{int(a) + 10}.{int(b):02d}"
    except Exception:
        return None


def pro_players() -> pd.DataFrame:
    oe = pd.read_csv(OE_FILE, low_memory=False, dtype={"patch": str})
    oe = oe[oe["league"].isin(LEAGUES) & (oe["date"].str[:10] <= END) & oe["position"].isin(H.OE_POS)]
    nm = pd.read_csv(PROCESSED / "d_champion_name_map.csv").set_index("alias")["champion_id"]
    oe["champion_id"] = oe["champion"].map(lambda s: nm.get(norm(s)))
    oe["teamPosition"] = oe["position"].map(H.OE_POS)
    oe["patch"] = oe["patch"].map(oe_patch_to_ours)
    oe["day"] = oe["date"].str[:10]
    oe["teamId"] = np.where(oe["side"] == "Blue", 100, 200)
    oe["win"] = oe["result"].astype(int)
    miss = oe["champion_id"].isna().sum()
    if miss:
        log.warning("OE 챔피언 이름 못 맞춤 %d행: %s", miss, oe.loc[oe["champion_id"].isna(), "champion"].unique()[:10])
    oe["pid"] = oe["playerid"].fillna(oe["playername"]).fillna("")
    return oe.rename(columns={"gameid": "matchId"})[["matchId", "league", "day", "date", "patch", "teamId", "teamname",
                                                     "teamPosition", "champion_id", "win", "pid"]]


def mastery(oe: pd.DataFrame) -> pd.Series:
    """대회 숙련 (2차, 선택 편향 일부): 그 경기 전까지 같은 선수가 같은 챔피언으로 한 대회 경기 수 (5리그, 2026 시즌).
    팀 5명 평균 log(1 + n), 블루 − 레드. "잘하는 챔피언을 골라서 이긴 것"을 조합 효과에서 떼어 냄."""
    d = oe.sort_values(["date", "matchId"]).copy()
    d["prior"] = d.groupby(["pid", "champion_id"]).cumcount().astype(float)
    d.loc[d["pid"] == "", "prior"] = 0.0
    t = d.assign(m=np.log1p(d["prior"])).groupby(["matchId", "teamId"])["m"].mean().unstack()
    return (t[100] - t[200]).rename("mastery_diff")


def elo(oe: pd.DataFrame, k: float = 24) -> pd.Series:
    """리그 안 경기 전 Elo 차이 (블루 − 레드) / 400. 08-25 까지 경기 순서대로."""
    g = oe.groupby(["matchId", "teamId"]).agg(league=("league", "first"), date=("date", "first"),
                                              team=("teamname", "first"), win=("win", "first")).reset_index()
    g = g.pivot_table(index=["matchId", "league", "date"], columns="teamId", values=["team", "win"], aggfunc="first")
    g.columns = [f"{a}{b}" for a, b in g.columns]
    g = g.reset_index().sort_values("date")
    rating, out = {}, {}
    for r in g.itertuples():
        a, b = (r.league, r.team100), (r.league, r.team200)
        ra, rb = rating.get(a, 1500.0), rating.get(b, 1500.0)
        out[r.matchId] = (ra - rb) / 400
        ea = 1 / (1 + 10 ** ((rb - ra) / 400))
        s = float(r.win100)
        rating[a] = ra + k * (s - ea)
        rating[b] = rb - k * (s - ea)
    return pd.Series(out, name="elo_diff")


def solo_players() -> pd.DataFrame:
    import m0_attributes as M
    raw = pd.read_csv(H.PARTS, dtype={"patch": str, "game_version": str, "matchId": str, "date_window": str})
    fr, bu, sg = M.timeline_rows()
    tp = M.tl_participants(fr, sg)
    df, _, _ = M.filter_games(raw, tp)
    df = df[df["day"] <= END]
    g = pd.read_csv(PROCESSED / "g_research_soloq_games.csv", usecols=["matchId", "teamId", "player", "suspect_account"],
                    low_memory=False)
    g = g[~g["suspect_account"].fillna(False).astype(bool)]
    pros = g.groupby(["matchId", "teamId"])["player"].nunique()
    pc = pros.unstack(fill_value=0).reindex(columns=[100, 200], fill_value=0)
    pdiff = (pc[100] - pc[200]).rename("pro_diff")
    df = df.assign(league="SOLO", date=df["day"])
    return df[["matchId", "league", "day", "date", "patch", "teamId", "teamPosition", "champion_id", "win"]], pdiff


# ---------------------------------------------------------------- 속성 붙이기
ATTR_COLS = list(AX.values()) + ["attackrange", "range_qwe_med", "melee", "magic_share", "phys_share", "dmg_share",
                                 "ult_type", "ult_teamfight"]


def load_attr(path: Path) -> pd.DataFrame:
    a = pd.read_csv(path, dtype={"patch": str}, low_memory=False)
    return a[["patch", "position", "champion_id", "n", "era"] + ATTR_COLS]


def attach(players: pd.DataFrame, at: pd.DataFrame) -> pd.DataFrame:
    """(패치, 포지션, 챔피언) 행 → 없으면 (전체 기간, 포지션, 챔피언) → 없으면 그 챔피언 판수 많은 전체 기간 행."""
    p = players.rename(columns={"teamPosition": "position"})
    patch_rows = at[~at["patch"].str.startswith("ALL")].drop(columns=["era"])
    allr = at[at["patch"] == "ALL"].drop(columns=["era", "patch"])
    x = p.merge(patch_rows, on=["patch", "position", "champion_id"], how="left")
    miss = x["n"].isna()
    if miss.any():
        f = p[miss.values].merge(allr, on=["position", "champion_id"], how="left")
        x.loc[miss.values, ATTR_COLS + ["n"]] = f[ATTR_COLS + ["n"]].values
    miss = x["n"].isna()
    if miss.any():
        top = allr.sort_values("n", ascending=False).drop_duplicates("champion_id").drop(columns=["position"])
        f = p[miss.values][["champion_id"]].merge(top, on="champion_id", how="left")
        x.loc[miss.values, ATTR_COLS + ["n"]] = f[ATTR_COLS + ["n"]].values
    x["attr_level"] = np.where(~miss, "ok", np.where(x["n"].notna(), "champ_all", "none"))
    return x


def team_features(x: pd.DataFrame) -> pd.DataFrame:
    """참가자 행(속성 붙음) → 경기 × 팀 조합 성격."""
    x = x.copy()
    for c in list(AX.values()) + ["attackrange", "range_qwe_med", "magic_share", "phys_share", "dmg_share"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x["ult_teamfight"] = pd.to_numeric(x["ult_teamfight"], errors="coerce")
    # 사일러스: 상대 팀 한타형 궁 비율로
    opp_tf = x[x["ult_type"] != "상대 궁"].groupby(["matchId", "teamId"])["ult_teamfight"].mean()
    syl = x["ult_type"] == "상대 궁"
    if syl.any():
        key = list(zip(x.loc[syl, "matchId"], 300 - x.loc[syl, "teamId"]))
        x.loc[syl, "ult_teamfight"] = [opp_tf.get(k, np.nan) for k in key]
    g = x.groupby(["matchId", "teamId"])
    f = pd.DataFrame({k: g[v].mean() for k, v in AX.items()})
    f = f.fillna(50.0)
    f["range_aa"] = g["attackrange"].mean()
    f["range_skill"] = g["range_qwe_med"].mean()
    f["melee_n"] = g["melee"].apply(lambda s: pd.to_numeric(s.map({True: 1, False: 0, "True": 1, "False": 0}), errors="coerce").sum())
    w = x["dmg_share"].fillna(0.2)
    x["_m"] = x["magic_share"].fillna(0.5) * w
    x["_p"] = x["phys_share"].fillna(0.5) * w
    x["_w"] = w
    s = x.groupby(["matchId", "teamId"])[["_m", "_p", "_w"]].sum()
    f["dmg_imbalance"] = ((s["_p"] - s["_m"]) / s["_w"]).abs()
    f["teamfight_ult_n"] = g["ult_teamfight"].sum()
    f["cc_ult_n"] = g["ult_type"].apply(lambda s: (s == "광역 CC").sum())
    f["protect_ult_n"] = g["ult_type"].apply(lambda s: (s == "아군 보호").sum())
    return f.reset_index()


def game_rows(players: pd.DataFrame, at_for, ctrl: dict) -> pd.DataFrame:
    """players → 경기 1행 (블루 − 레드). at_for: 속성표 또는 {fold: 표}(솔랭 교차)."""
    if isinstance(at_for, dict):
        fold = players["matchId"].map(lambda s: zlib.crc32(s.encode()) % 5)
        xs = [attach(players[fold == k], at_for[k]) for k in range(5)]
        x = pd.concat(xs, ignore_index=True)
    else:
        x = attach(players, at_for)
    f = team_features(x)
    b = f[f["teamId"] == 100].set_index("matchId")[FEATS]
    r = f[f["teamId"] == 200].set_index("matchId")[FEATS]
    d = (b - r).dropna(how="all")
    meta = players.groupby("matchId").agg(league=("league", "first"), day=("day", "first"), patch=("patch", "first"))
    win = players[players["teamId"] == 100].groupby("matchId")["win"].first().rename("blue_win")
    # 팀 값은 19개 성격 모두 저장 (점검 E2: M3 팀 지수용. 상호작용은 IX 만 씀)
    out = d.join(b[FEATS].add_prefix("b_")).join(r[FEATS].add_prefix("r_")).join(meta).join(win)
    for c in ("pro_diff", "elo_diff", "mastery_diff"):
        out[c] = out.index.map(ctrl.get(c, {})).astype(float)
        out[c] = out[c].fillna(0)
    full5 = x.groupby("matchId").size()
    out = out[out.index.map(full5) == 10]  # 10명이 다 있는 경기만
    lv = x.groupby("matchId")["attr_level"].apply(lambda s: (s != "ok").sum())
    out["attr_fallback"] = out.index.map(lv)
    return out.reset_index()


# ---------------------------------------------------------------- 모델
class Hier:
    """공통 + 대회 편차(×c_pro) + 리그 편차(×c_league), 엘라스틱넷 로지스틱."""

    def __init__(self, C=0.1, l1=0.5, c_pro=0.5, c_league=0.3, c_int=0.0, mastery=False):
        self.C, self.l1, self.c_pro, self.c_league = C, l1, c_pro, c_league
        self.c_int, self.mastery = c_int, mastery
        self.ctrl = ["pro_diff", "elo_diff"] + (["mastery_diff"] if mastery else [])

    def params(self) -> tuple:
        return (self.C, self.l1, self.c_pro, self.c_league, self.c_int, self.mastery)

    def _inter_raw(self, g: pd.DataFrame) -> np.ndarray:
        B = np.nan_to_num((g[[f"b_{f}" for f in IX]].to_numpy(float) - self.tmu) / self.tsd)
        R = np.nan_to_num((g[[f"r_{f}" for f in IX]].to_numpy(float) - self.tmu) / self.tsd)
        cols = [B[:, i] * R[:, j] - R[:, i] * B[:, j] for i, j in PAIRS_X]
        cols += [B[:, i] * B[:, j] - R[:, i] * R[:, j] for i, j in PAIRS_W]
        return np.column_stack(cols)

    def design(self, g: pd.DataFrame) -> np.ndarray:
        Z = (g[FEATS].to_numpy(float) - self.mu) / self.sd
        Z = np.nan_to_num(Z)
        pro = (g["league"] != "SOLO").to_numpy(float)[:, None]
        blocks = [Z, Z * pro * self.c_pro]
        lg = [((g["league"] == L).to_numpy(float))[:, None] for L in LEAGUES]
        blocks += [Z * m * self.c_league for m in lg]
        # 진영 편차(대회·리그) + 통제 (통제는 배율 3 → 사실상 덜 당김)
        blocks += [pro * self.c_pro] + [m * self.c_league for m in lg]
        if self.c_int > 0:  # 상호작용은 공통 칸만 (리그 편차까지 두면 칸이 너무 많음)
            blocks += [self._inter_raw(g) / self.isd * self.c_int]
        blocks += [(g["pro_diff"].to_numpy(float) * (1 - pro[:, 0]))[:, None] * 3,
                   (g["elo_diff"].to_numpy(float) * pro[:, 0])[:, None] * 3]
        if self.mastery:
            blocks += [(g["mastery_diff"].to_numpy(float) * pro[:, 0])[:, None] * 3]
        return np.hstack(blocks)

    def fit(self, g: pd.DataFrame, w: np.ndarray | None = None):
        self.mu = g[FEATS].mean().to_numpy(float)
        self.sd = g[FEATS].std().replace(0, 1).to_numpy(float)
        if self.c_int > 0:
            tv = np.vstack([g[[f"b_{f}" for f in IX]].to_numpy(float), g[[f"r_{f}" for f in IX]].to_numpy(float)])
            self.tmu = np.nanmean(tv, axis=0)
            self.tsd = np.nanstd(tv, axis=0)
            self.tsd[self.tsd == 0] = 1
            self.isd = self._inter_raw(g).std(axis=0)
            self.isd[self.isd == 0] = 1
        X = self.design(g)
        self.m = LogisticRegression(penalty="elasticnet", solver="saga", C=self.C, l1_ratio=self.l1, max_iter=4000,
                                    tol=1e-4).fit(X, g["blue_win"].to_numpy(int), sample_weight=w)
        return self

    def predict(self, g: pd.DataFrame) -> np.ndarray:
        return self.m.predict_proba(self.design(g))[:, 1]

    def effective(self) -> pd.DataFrame:
        """표준화 1 SD 당 로그 오즈: 솔랭(공통), 대회 공통, 리그별."""
        b = self.m.coef_[0]
        k = len(FEATS)
        base = b[:k]
        pro = b[k:2 * k] * self.c_pro
        rows = {"solo": base, "pro": base + pro}
        for i, L in enumerate(LEAGUES):
            rows[L] = base + pro + b[(2 + i) * k:(3 + i) * k] * self.c_league
        e = pd.DataFrame(rows, index=FEATS)
        e["sd"] = self.sd
        return e

    def controls(self) -> dict:
        b = self.m.coef_[0][-len(self.ctrl):] * 3
        return dict(zip(self.ctrl, b))

    def interactions(self) -> pd.Series:
        """상호작용 계수: 두 팀 값이 각각 1 SD 일 때 더해지는 로그 오즈 (곱 1 단위당)."""
        if self.c_int <= 0:
            return pd.Series(dtype=float)
        k = len(FEATS)
        s = (2 + len(LEAGUES)) * k + 1 + len(LEAGUES)
        b = self.m.coef_[0][s:s + len(IX_KEYS)]
        return pd.Series(b * self.c_int / self.isd, index=IX_KEYS)


PARAMS_FILE = OUT / "m1_params.json"


def save_params(m: "Hier") -> None:
    """모델 설정·표준화 값·계수를 JSON 으로 (pickle 은 실행한 스크립트에 묶여 다른 스크립트에서 못 읽는 문제, 점검 E4)."""
    import json
    d = {"params": [float(x) if not isinstance(x, (bool, np.bool_)) else bool(x) for x in m.params()],
         "FEATS": FEATS, "IX": IX, "ctrl": m.ctrl, "mu": m.mu.tolist(), "sd": m.sd.tolist(),
         "coef": m.m.coef_[0].tolist(), "intercept": float(m.m.intercept_[0])}
    if m.c_int > 0:
        d.update(tmu=m.tmu.tolist(), tsd=m.tsd.tolist(), isd=m.isd.tolist())
    PARAMS_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def load_model() -> "Hier":
    """m1_params.json → 예측 가능한 Hier (어느 스크립트에서나)."""
    import json
    from types import SimpleNamespace
    d = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
    m = Hier(*d["params"])
    m.mu, m.sd = np.array(d["mu"]), np.array(d["sd"])
    if "tmu" in d:
        m.tmu, m.tsd, m.isd = np.array(d["tmu"]), np.array(d["tsd"]), np.array(d["isd"])
    lr = LogisticRegression()
    lr.coef_, lr.intercept_, lr.classes_ = np.array([d["coef"]]), np.array([d["intercept"]]), np.array([0, 1])
    m.m = lr
    return m


def baseline_side(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    rate = train.groupby("league")["blue_win"].mean()
    return test["league"].map(rate).fillna(train["blue_win"].mean()).to_numpy()


def baseline_elo(train: pd.DataFrame, test: pd.DataFrame, mastery: bool = False) -> np.ndarray:
    cols = ["elo_diff"] + (["mastery_diff"] if mastery else [])
    tr = train[train["league"] != "SOLO"]
    X = np.c_[tr[cols], pd.get_dummies(tr["league"]).reindex(columns=LEAGUES, fill_value=0)]
    m = LogisticRegression(C=1e4, max_iter=2000, fit_intercept=False).fit(X, tr["blue_win"])
    Xt = np.c_[test[cols], pd.get_dummies(test["league"]).reindex(columns=LEAGUES, fill_value=0)]
    return m.predict_proba(Xt)[:, 1]


def scores(y, p) -> dict:
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return {"n": len(y), "logloss": log_loss(y, p, labels=[0, 1]), "brier": brier_score_loss(y, p),
            "auc": roc_auc_score(y, p) if len(set(y)) > 1 else np.nan}


def calib(y, p, bins=8) -> list[str]:
    q = pd.qcut(p, bins, duplicates="drop")
    t = pd.DataFrame({"y": y, "p": p, "q": q}).groupby("q", observed=True).agg(n=("y", "size"), pred=("p", "mean"), real=("y", "mean"))
    return [f"| {r.pred:.2f} | {r.real:.2f} | {r.n} |" for r in t.itertuples()]


# ---------------------------------------------------------------- main
GRID = {"C": [0.003, 0.01, 0.03, 0.1], "l1": [0.2, 0.6], "c_pro": [0.3, 1.0], "c_league": [0.2, 0.5]}
C_INT = [0.0, 0.3, 1.0]  # 상호작용 칸 배율 (0 = 주효과만)
PKEYS = ["C", "l1", "c_pro", "c_league", "c_int", "mastery"]


def fmt_ci(c, lo, hi, sg) -> str:
    t = f"{c:+.3f} [{lo:+.3f}, {hi:+.3f}]"
    return f"**{t}**" if sg else t


def refresh_team_values():
    """m1_games.csv 의 팀 값(b_·r_) 칸만 19개 성격 전부로 다시 계산 (모델·예측은 그대로). 점검 E2."""
    pro = pro_players()
    solo, pdiff = solo_players()
    ctrl = {"pro_diff": pdiff.to_dict(), "elo_diff": elo(pro).to_dict(), "mastery_diff": mastery(pro).to_dict()}
    players = pd.concat([solo, pro.drop(columns=["pid"])], ignore_index=True)
    full = load_attr(M0 / "m0_attributes.csv")
    folds = {k: load_attr(M0 / f"m0_attributes_fold{k}.csv") for k in range(5)}
    gs = game_rows(players[players["league"] == "SOLO"], folds, ctrl)
    gp = game_rows(players[players["league"] != "SOLO"], full, ctrl)
    tv = pd.concat([gs, gp], ignore_index=True)
    cols = [f"b_{f}" for f in FEATS] + [f"r_{f}" for f in FEATS]
    G = pd.read_csv(OUT / "m1_games.csv", dtype={"patch": str, "matchId": str}, low_memory=False)
    G = G.drop(columns=[c for c in cols if c in G]).merge(tv[["matchId"] + cols], on="matchId", how="left")
    H.save_csv(G, OUT / "m1_games.csv")
    log.info("m1_games.csv 팀 값 %d칸 갱신 (%d경기, 빈 값 %d)", len(cols), len(G), int(G[cols].isna().any(axis=1).sum()))


def main():
    if "--team-values" in sys.argv:
        refresh_team_values()
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=60)
    ap.add_argument("--grid-only", action="store_true", help="설정 고르기만 (최종 학습·부트스트랩 생략)")
    ap.add_argument("--reuse-selection", action="store_true", help="m1_selection.csv 를 다시 써서 그리드를 건너뜀")
    a = ap.parse_args()
    t0 = time.time()
    pro = pro_players()
    solo, pdiff = solo_players()
    mast = mastery(pro)
    ctrl = {"pro_diff": pdiff.to_dict(), "elo_diff": elo(pro).to_dict(), "mastery_diff": mast.to_dict()}
    players = pd.concat([solo, pro.drop(columns=["pid"])], ignore_index=True)
    log.info("M1 자료: 솔랭 %d경기, 대회 %s", solo["matchId"].nunique(), pro.groupby("league")["matchId"].nunique().to_dict())
    w = H.research_windows().set_index("patch")

    L = ["# M1 조합 승패 평가 (2차: 상호작용 + 대회 숙련 통제)", "",
         f"- 자료: 솔랭 {solo['matchId'].nunique():,}경기 + 대회 {pro['matchId'].nunique():,}경기 "
         f"({', '.join(f'{k} {v}' for k, v in pro.groupby('league')['matchId'].nunique().items())}), 08-25 까지",
         "- 행 = 경기 (블루 − 레드), 결과 = 블루 승. 계층 엘라스틱넷 (공통 + 대회 편차 + 리그 편차)",
         f"- 2차 추가: 상호작용 {len(IX_KEYS)}칸 (상대 사이 {len(PAIRS_X)} + 같은 팀 안 {len(PAIRS_W)}, 공통 칸만), "
         "대회 숙련 통제 (선수가 그 챔피언으로 그 경기 전까지 한 대회 경기 수, 팀 평균 log(1+n) 차이)",
         "- 1차 결과는 `m1/_v1/`. 플레이오프(08-26~)는 보지 않음", ""]

    # 1) 설정 고르기
    configs = [(C, l1, cp, cl, 0.0, False) for C, l1, cp, cl in itertools.product(*GRID.values())]
    configs += [(C, l1, cp, cl, ci, True) for C, l1, cp, cl in itertools.product(*GRID.values()) for ci in C_INT]
    sel_rows, preds, cuts = [], {}, {}
    if a.reuse_selection:
        old = pd.read_csv(OUT / "m1_selection.csv")
        old["mastery"] = old["mastery"].astype(str).str.lower().eq("true")
        av = old.groupby(PKEYS)["logloss"].mean()
        oi = av.index.to_frame(index=False)
        keep = set()
        for m_ in [(oi["c_int"] == 0) & ~oi["mastery"], (oi["c_int"] == 0) & oi["mastery"], oi["mastery"]]:
            s_ = av[m_.to_numpy()]
            keep.add(s_.index[int(np.argmin(s_.to_numpy()))])
        configs = [c for c in configs if c in keep]
        log.info("설정 고르기 다시 씀: %s", configs)
    for p in ("26.14", "26.15"):
        cut = w.loc[p, "window_from"]
        at = load_attr(M0 / f"m0_attributes_asof_{p}.csv")
        g = game_rows(players, at, ctrl)
        test = g[(g["league"] != "SOLO") & (g["patch"] == p)]
        train = g[(g["day"] < cut) & ~g["matchId"].isin(test["matchId"])]
        cuts[p] = (train, test)
        log.info("설정 고르기 %s: 학습 %d경기 (대회 %d) · 평가 %d경기 (LCK %d), 설정 %d개", p, len(train),
                 (train["league"] != "SOLO").sum(), len(test), (test["league"] == "LCK").sum(), len(configs))
        preds[(p, "진영만")] = baseline_side(train, test)
        preds[(p, "Elo")] = baseline_elo(train, test)
        preds[(p, "Elo+숙련")] = baseline_elo(train, test, mastery=True)
        for i, cfg in enumerate(configs):
            pr = Hier(*cfg).fit(train).predict(test)
            preds[(p, cfg)] = pr
            sel_rows.append(dict(zip(PKEYS, cfg), patch=p, **scores(test["blue_win"].to_numpy(), pr)))
            if (i + 1) % 32 == 0:
                log.info("  %s 설정 %d/%d (%.0f초)", p, i + 1, len(configs), time.time() - t0)
    sel = pd.DataFrame(sel_rows)
    if a.reuse_selection:
        sel = old
    else:
        H.save_csv(sel, OUT / "m1_selection.csv")
    avg = sel.groupby(PKEYS)["logloss"].mean()
    ix = avg.index.to_frame(index=False)

    def pick(mask):
        s = avg[mask.to_numpy()]
        return s.index[int(np.argmin(s.to_numpy()))]

    best_v1 = pick((ix["c_int"] == 0) & ~ix["mastery"])
    best_main = pick((ix["c_int"] == 0) & ix["mastery"])
    best = pick(ix["mastery"])
    log.info("고른 설정 %s (평균 로그 손실 %.4f) · 주효과만 %s · 1차 형태 %s", best, avg[best], best_main, best_v1)

    L += ["## 1. 설정 고르기 (26.14 · 26.15 대회 경기, 그 시점 속성표)", "",
          "| 패치 | 모델 | 경기 | 로그 손실 | 브라이어 | AUC | LCK 경기 | LCK 로그 손실 |", "|---|---|---|---|---|---|---|---|"]
    rows_show = (("진영만", "진영만"), ("Elo", "Elo"), ("Elo + 숙련", "Elo+숙련"), ("M1 1차 형태 (주효과)", best_v1),
                 ("M1 주효과 + 숙련", best_main), ("**M1 2차 (고른 설정)**", best))
    for p in ("26.14", "26.15"):
        tr, te = cuts[p]
        y = te["blue_win"].to_numpy()
        lck = (te["league"] == "LCK").to_numpy()
        for name, key in rows_show:
            pr = preds[(p, key)]
            s = scores(y, pr)
            sl = scores(y[lck], pr[lck]) if lck.sum() > 5 else {"n": lck.sum(), "logloss": np.nan}
            L.append(f"| {p} | {name} | {s['n']} | {s['logloss']:.4f} | {s['brier']:.4f} | {s['auc']:.3f} | {sl['n']} | {sl['logloss']:.4f} |")
    L += ["", "| 상호작용 배율 (숙련 통제 포함) | 두 패치 평균 로그 손실 (그 배율에서 가장 좋은 설정) |", "|---|---|"]
    for ci in C_INT:
        s = avg[((ix["c_int"] == ci) & ix["mastery"]).to_numpy()]
        L.append(f"| {ci} {'(주효과만)' if ci == 0 else ''} | {s.min():.4f} |")
    L += ["", f"- 고른 설정: C = {best[0]}, l1 비율 = {best[1]}, 대회 편차 배율 = {best[2]}, 리그 편차 배율 = {best[3]}, "
          f"상호작용 배율 = {best[4]} (설정 {len(avg)}개 중 숙련 통제를 넣은 것 가운데 두 패치 평균 로그 손실 최소)", ""]
    yy = np.concatenate([cuts[p][1]["blue_win"].to_numpy() for p in ("26.14", "26.15")])
    pp = np.concatenate([preds[(p, best)] for p in ("26.14", "26.15")])
    L += ["### 보정 곡선 (두 패치 대회 경기 합침, 고른 설정)", "", "| 예측 승률 (구간 평균) | 실제 블루 승률 | 경기 |", "|---|---|---|"]
    L += calib(yy, pp) + [""]
    if a.grid_only:
        (OUT / "m1_eval.md").write_text("\n".join(L), encoding="utf-8")
        log.info("설정 고르기만 끝 (%.0f초)", time.time() - t0)
        return

    # 2) 최종 학습 (08-25 까지 전부)
    full = load_attr(M0 / "m0_attributes.csv")
    folds = {k: load_attr(M0 / f"m0_attributes_fold{k}.csv") for k in range(5)}
    gs = game_rows(players[players["league"] == "SOLO"], folds, ctrl)
    gp = game_rows(players[players["league"] != "SOLO"], full, ctrl)
    G = pd.concat([gs, gp], ignore_index=True)
    fb = G["attr_fallback"].gt(0).mean()
    final = Hier(*best).fit(G)
    eff, inter = final.effective(), final.interactions()
    # 선택 편향 점검: 숙련 통제가 조합 계수를 얼마나 바꾸나 (같은 설정, 주효과만)
    e_no = Hier(*best_v1[:4], 0.0, False).fit(G).effective()
    m_yes = Hier(*best_v1[:4], 0.0, True).fit(G)
    e_yes = m_yes.effective()
    log.info("최종 학습 끝 (%.0f초), 부트스트랩 %d회 시작", time.time() - t0, a.boot)
    rng = np.random.default_rng(0)
    boots, iboots, cboots = [], [], []
    for b in range(a.boot):
        idx = np.concatenate([rng.choice(ix_, len(ix_), replace=True) for _, ix_ in G.groupby("league").indices.items()])
        mb = Hier(*best).fit(G.iloc[idx])
        boots.append(mb.effective())
        iboots.append(mb.interactions())
        cboots.append(mb.controls())
        if (b + 1) % 10 == 0:
            log.info("부트스트랩 %d/%d (%.0f초)", b + 1, a.boot, time.time() - t0)
    rows = []
    for f_ in FEATS:
        for lvl in ["solo", "pro", "LCK"]:
            bs = np.array([e.loc[f_, lvl] for e in boots])
            rows.append({"feature": f_, "name": NAMES[f_], "level": lvl, "coef": eff.loc[f_, lvl],
                         "lo": np.percentile(bs, 2.5), "hi": np.percentile(bs, 97.5), "sd_unit": eff.loc[f_, "sd"]})
    co = pd.DataFrame(rows)
    co["sig"] = (co["lo"] > 0) | (co["hi"] < 0)
    H.save_csv(co, OUT / "m1_coefs.csv")
    io = None
    if len(inter):
        ib = pd.DataFrame(iboots)
        io = pd.DataFrame({"key": IX_KEYS, "name": IX_NAMES, "coef": inter.to_numpy(),
                           "lo": ib.quantile(0.025).to_numpy(), "hi": ib.quantile(0.975).to_numpy(),
                           "nonzero_share": (ib.abs() > 1e-6).mean().to_numpy()})
        io["sig"] = (io["lo"] > 0) | (io["hi"] < 0)
        H.save_csv(io, OUT / "m1_interactions.csv")
    # 교차 예측 (M2 오프셋용)
    fold = G["matchId"].map(lambda s: zlib.crc32(("m1" + s).encode()) % 5)
    G["m1_pred_oof"] = np.nan
    for k in range(5):
        mk = Hier(*best).fit(G[fold != k])
        G.loc[fold == k, "m1_pred_oof"] = mk.predict(G[fold == k])
    G["m1_pred"] = final.predict(G)
    H.save_csv(G, OUT / "m1_games.csv")
    pd.to_pickle(final, OUT / "m1_model.pkl")
    save_params(final)

    L += ["## 2. 최종 주효과 계수 (08-25 까지 전부, 조합 성격 1 표준편차 차이당 블루 승 로그 오즈)", "",
          f"- 학습: {len(G):,}경기 (솔랭 {len(gs):,} · 대회 {len(gp):,}). 속성을 패치 행이 아닌 전체 기간·챔피언 행으로 대신한 선수가 있는 경기 {fb:.1%}",
          f"- 구간: 경기 단위 부트스트랩 {a.boot}회 95%. **굵게** = 구간이 0 을 포함하지 않음",
          "- 상호작용이 들어간 모델이라 주효과 = '다른 성격이 평균일 때' 그 성격의 효과", "",
          "| 성격 | 솔랭 | 대회 공통 | LCK | 1 SD 크기 |", "|---|---|---|---|---|"]
    piv = co.pivot(index="feature", columns="level")
    for f_ in piv[("coef", "solo")].abs().sort_values(ascending=False).index:
        cells = [fmt_ci(piv.loc[f_, ("coef", l)], piv.loc[f_, ("lo", l)], piv.loc[f_, ("hi", l)], piv.loc[f_, ("sig", l)])
                 for l in ["solo", "pro", "LCK"]]
        L.append(f"| {NAMES[f_]} (`{f_}`) | " + " | ".join(cells) + f" | {piv.loc[f_, ('sd_unit', 'solo')]:.2f} |")
    L += ["", "- 로그 오즈 0.1 ≈ 50% 근처에서 승률 약 2.5%p", ""]
    cc = final.controls()
    cq = pd.DataFrame(cboots)
    L += ["### 통제 변수", "", "| 통제 | 계수 | 95% 구간 | 뜻 |", "|---|---|---|---|"]
    desc = {"pro_diff": "솔랭: 리서치 프로 1명 더", "elo_diff": "대회: Elo 차이 400점", "mastery_diff": "대회: 팀 평균 log(1+이전 대회 판수) 1 차이"}
    for k_, v in cc.items():
        L.append(f"| `{k_}` | {v:+.3f} | [{cq[k_].quantile(.025):+.3f}, {cq[k_].quantile(.975):+.3f}] | {desc[k_]} |")
    pm = G.loc[G["league"] != "SOLO", "mastery_diff"]
    L += ["", f"- 숙련 차이 분포 (대회 경기): 표준편차 {pm.std():.2f}, 0 이 아닌 경기 {(pm != 0).mean():.0%}. "
          "예: 한 팀 5명이 모두 그 챔피언을 대회에서 처음, 상대 5명은 모두 3판째 → 차이 log(4) ≈ 1.39", ""]

    # 상호작용
    if io is not None:
        nz = (io["coef"].abs() > 1e-6).sum()
        L += ["## 3. 상호작용 (조합 성격 사이의 궁합)", "",
              f"- {len(io)}칸 중 엘라스틱넷이 0 이 아니게 남긴 것 {nz}개, 부트스트랩 구간이 0 을 안 포함하는 것 {io['sig'].sum()}개",
              "- 계수 = 두 팀 값이 각각 1 SD 높을 때 더해지는 로그 오즈. 상대 사이 칸은 '우리 X 가 높고 상대 Y 가 높을 때' 우리 쪽 +계수 "
              "(반대로 우리 Y · 상대 X 이면 −계수). 같은 팀 칸 + = 둘이 같이 높으면 더 좋음, 제곱 − = 많을수록 효과가 줄어듦",
              "- 여러 칸을 동시에 보므로(196) 우연히 굵게 나오는 칸이 몇 개는 있을 수 있음 (5% × 196 ≈ 10)", "",
              "| 상호작용 | 계수 [95% 구간] | 부트스트랩에서 0 이 아닌 비율 |", "|---|---|---|"]
        top = io.reindex(io["coef"].abs().sort_values(ascending=False).index)
        top = pd.concat([top[top["sig"]], top[~top["sig"]].head(max(0, 15 - top["sig"].sum()))])
        for r in top.itertuples():
            L.append(f"| {r.name} | {fmt_ci(r.coef, r.lo, r.hi, r.sig)} | {r.nonzero_share:.0%} |")
        L += [""]

    # 선택 편향 점검
    L += ["## 4. 선택 편향 점검 — 숙련 통제를 넣으면 조합 계수가 바뀌나", "",
          f"- 같은 설정(1차 형태 {best_v1[:4]}, 주효과만)에서 숙련 통제만 넣고 뺀 비교. 대회 공통 계수 기준",
          f"- 숙련 계수 (이 비교 모델): {m_yes.controls()['mastery_diff']:+.3f}",
          "- 크게 바뀐 성격 = 그 성격의 효과 일부가 '익숙한 챔피언을 골라서 이긴 것'이었음", "",
          "| 성격 | 숙련 통제 없음 | 숙련 통제 있음 | 차이 |", "|---|---|---|---|"]
    dd = (e_yes["pro"] - e_no["pro"])
    for f_ in dd.abs().sort_values(ascending=False).index:
        L.append(f"| {NAMES[f_]} | {e_no.loc[f_, 'pro']:+.3f} | {e_yes.loc[f_, 'pro']:+.3f} | {dd[f_]:+.3f} |")
    L += ["", "- 정식 IPW(③)는 M4 고를 확률이 성향 점수라 M4 뒤에 (7-7 점검 결과 ③)", ""]
    (OUT / "m1_eval.md").write_text("\n".join(L), encoding="utf-8")
    log.info("M1 끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
