"""M2 시너지·카운터 (3차, 2026-11-06) — docs/PIPELINE.md 7-7

3차 추가: 정글+미드·정글+탑 듀오 15분 골드(gs), 교전 단위 시너지 모델(교전 = 15초 묶음, m2_fights.py) → 승패 모델 F 칸.

1차(모든 쌍 1만 8천 칸, 승패만)는 쌍 효과가 ±0.2%p 이하로 사라짐 → 2차는
  ① 구조 있는 쌍만:  시너지 = 원딜+서폿, 정글+미드, 정글+탑 (같은 팀)
                     카운터 = 탑·정글·미드 맞대결 + 바텀 2대2 (원딜·서폿 교차 4쌍)
  ② 맞라인 카운터를 승패 대신 **15분 라인 골드 차이**로 (정보가 많은 연속값)
     라인 = 탑 · 정글 · 미드 · 바텀(원딜 + 서폿 합). 솔랭 = 타임라인 15분 골드, 대회 = OE goldat15 (complete 만)

모델 두 개
  [골드] 라인 1행 (경기 × 라인), 결과 = 블루 − 레드 15분 골드(천 단위), 정규·릿지
         칸 = 라인 챔피언 단독(gm) + 맞대결 쌍(gc) + 바텀 듀오(gd), 통제 = 프로 수·Elo·숙련 + 출처×라인 절편
  [승패] 경기 1행, 결과 = 블루 승, 오프셋 = M1 교차 예측 로짓, 로지스틱·릿지
         칸 = 챔피언 단독(ch) + 챔피언×패치(chp) + 구조 시너지(syn) + 구조 카운터(ctr)
         + (선택) G = 골드 모델의 쌍 몫 합 (라인 상성으로 예상되는 15분 골드 우세, 천 단위, 교차 예측)
공통 + 대회 편차 칸 (대회 판수 MIN_PRO 이상). 규제 크기는 26.14·26.15 로 고름 (M1 과 같은 규칙). 플레이오프 안 봄.

    python src/pipeline/m2_synergy.py --boot 30
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
from scipy import sparse
from scipy.optimize import minimize
from sklearn.metrics import log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m2"
OUT.mkdir(exist_ok=True)
POS = H.POS
PK = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서폿"}
LANES = {"TOP": ["TOP"], "JUNGLE": ["JUNGLE"], "MIDDLE": ["MIDDLE"], "BOT": ["BOTTOM", "UTILITY"]}
LANE_KO = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOT": "바텀"}
LANE_OF = {"TOP": "TOP", "JUNGLE": "JUNGLE", "MIDDLE": "MIDDLE", "BOTTOM": "BOT", "UTILITY": "BOT"}
SYN_PAIRS = [("BOTTOM", "UTILITY"), ("JUNGLE", "MIDDLE"), ("JUNGLE", "TOP")]
CTR_PAIRS = [("TOP", "TOP"), ("JUNGLE", "JUNGLE"), ("MIDDLE", "MIDDLE"), ("BOTTOM", "BOTTOM"), ("UTILITY", "UTILITY"),
             ("BOTTOM", "UTILITY"), ("UTILITY", "BOTTOM")]
MIN_PAIR = 15        # 쌍 칸 최소 판수 (학습 자료)
MIN_PRO = 5          # 대회 편차 칸 최소 대회 판수
MIN_PATCH = 10       # 챔피언 × 패치 편차 칸 최소 판수
PATCH_MULT = 2.0
PRO_MULT = 10.0      # 대회 편차 규제 배율 (1차에서 10 이 고름)
BIG = 1e9            # "칸 없음"과 같은 규제
# 2차에서 골드 100 · 300, 승패 챔피언 100 이 골라짐 → 3차는 그 근처만
GOLD_GRID = {"lam_gm": [30.0, 100.0], "lam_gc": [100.0, 300.0, 1000.0, BIG], "lam_gs": [300.0, 1000.0, BIG]}  # gc = 맞대결·바텀 듀오, gs = 정글 듀오
FIGHT_GRID = {"lam_fm": [30.0, 100.0, 300.0], "lam_fp": [300.0, 1000.0, 3000.0, BIG]}
WIN_GRID = {"lam_ch": [100.0, 300.0], "lam_syn": [1000.0, BIG], "lam_ctr": [300.0, 3000.0, BIG],
            "use_g": [False, True], "use_f": [False, True]}
FIGHTS_MIN_KILLS = 2   # 교전 = 15초 묶음 안 킬 2개 이상, 킬 수가 다른 것 (승패가 갈림)


# ---------------------------------------------------------------- 칸 만들기
def slots(players: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """경기 × 10칸(b_TOP … r_UTILITY) 키 = 챔피언@포지션. games 순서 그대로."""
    p = players[players["matchId"].isin(games["matchId"])].copy()
    p["key"] = p["champion_id"].astype(str) + "@" + p["teamPosition"]
    p["slot"] = np.where(p["teamId"] == 100, "b_", "r_") + p["teamPosition"]
    s = p.pivot_table(index="matchId", columns="slot", values="key", aggfunc="first")
    cols = [f"b_{q}" for q in POS] + [f"r_{q}" for q in POS]
    return s.reindex(index=games["matchId"].to_numpy(), columns=cols)


def anti(kind: str, x: str, y: str) -> tuple:
    """블루 x 대 레드 y → 순서를 정한 키와 부호."""
    return (kind, f"{x}>{y}", 1.0) if x < y else (kind, f"{y}>{x}", -1.0)


def win_terms(row, patch: str) -> list:
    if pd.isna(row).any():
        return []
    b, r = dict(zip(POS, row[:5])), dict(zip(POS, row[5:]))
    out = []
    for team, v in ((b, 1.0), (r, -1.0)):
        for q in POS:
            out += [("ch", team[q], v), ("chp", f"{team[q]}|{patch}", v)]
        for p_, q in SYN_PAIRS:
            out.append(("syn", f"{team[p_]}+{team[q]}", v))
    for p_, q in CTR_PAIRS:
        out.append(anti("ctr", b[p_], r[q]))
    return out


def gold_terms(row, lane: str, pairs_only: bool = False) -> list:
    if pd.isna(row).any():
        return []
    b, r = dict(zip(POS, row[:5])), dict(zip(POS, row[5:]))
    ps = LANES[lane]
    out = [] if pairs_only else [("gm", b[q], 1.0) for q in ps] + [("gm", r[q], -1.0) for q in ps]
    for p_ in ps:
        for q in ps:
            out.append(anti("gc", b[p_], r[q]))
    if lane == "BOT":
        out += [("gd", f"{b['BOTTOM']}+{b['UTILITY']}", 1.0), ("gd", f"{r['BOTTOM']}+{r['UTILITY']}", -1.0)]
    # 3차: 정글 + 미드, 정글 + 탑 듀오 — 두 라인 행에 같은 칸 (계수 × 2 = 두 라인 합 골드 효과)
    if lane in ("JUNGLE", "MIDDLE", "TOP"):
        for team, v in ((b, 1.0), (r, -1.0)):
            mates = ["MIDDLE", "TOP"] if lane == "JUNGLE" else [lane]
            out += [("gs", f"{team['JUNGLE']}+{team[q]}", v) for q in mates]
    return out


def fight_terms(row) -> list:
    """교전 모델 칸 (경기 조합만, 교전마다 같음): 챔피언 단독 + 같은 팀 모든 쌍."""
    if pd.isna(row).any():
        return []
    b, r = list(row[:5]), list(row[5:])
    out = [("fm", k, 1.0) for k in b] + [("fm", k, -1.0) for k in r]
    for team, v in ((b, 1.0), (r, -1.0)):
        out += [("fp", f"{x}+{y}", v) for x, y in itertools.combinations(sorted(team), 2)]
    return out


NEED = {"ch": 1, "chp": MIN_PATCH, "syn": MIN_PAIR, "ctr": MIN_PAIR, "gm": 1, "gc": MIN_PAIR, "gd": MIN_PAIR,
        "gs": 2 * MIN_PAIR,  # 한 경기에 두 행
        "fm": 1, "fp": 10 * MIN_PAIR}  # 교전 행 기준 (경기당 약 9.4 교전)


class Coder:
    """학습 자료로 칸 목록을 정하고 희소 행렬로 바꿈 (공통 칸 + 대회 편차 칸)."""

    def __init__(self, T: list, is_pro: np.ndarray):
        cnt, cnt_pro = {}, {}
        for ts, pr in zip(T, is_pro):
            for kind, key, _ in ts:
                cnt[(kind, key)] = cnt.get((kind, key), 0) + 1
                if pr:
                    cnt_pro[(kind, key)] = cnt_pro.get((kind, key), 0) + 1
        self.cols = [k for k, n in cnt.items() if n >= NEED[k[0]]]
        self.cols_pro = [k for k in self.cols if k[0] != "chp" and cnt_pro.get(k, 0) >= MIN_PRO]
        self.idx = {k: i for i, k in enumerate(self.cols)}
        self.idx_pro = {k: len(self.cols) + i for i, k in enumerate(self.cols_pro)}
        self.n, self.n_pro = cnt, cnt_pro
        self.width = len(self.cols) + len(self.cols_pro)

    def matrix(self, T: list, is_pro: np.ndarray) -> sparse.csr_matrix:
        ri, ci, v = [], [], []
        for i, (ts, pr) in enumerate(zip(T, is_pro)):
            for kind, key, val in ts:
                j = self.idx.get((kind, key))
                if j is None:
                    continue
                ri.append(i); ci.append(j); v.append(val)
                if pr:
                    jp = self.idx_pro.get((kind, key))
                    if jp is not None:
                        ri.append(i); ci.append(jp); v.append(val)
        return sparse.csr_matrix((v, (ri, ci)), shape=(len(T), self.width))

    def penalty(self, lam: dict) -> np.ndarray:
        return np.array([lam[k[0]] for k in self.cols] + [lam[k[0]] * PRO_MULT for k in self.cols_pro])

    def mask(self, kinds: set) -> np.ndarray:
        return np.array([k[0] in kinds for k in self.cols] + [k[0] in kinds for k in self.cols_pro], dtype=float)


# ---------------------------------------------------------------- 적합
def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def sigm(z):
    return 1 / (1 + np.exp(-z))


def _fit(f, width, b0):
    b0 = np.zeros(width) if b0 is None else b0
    return minimize(f, b0, jac=True, method="L-BFGS-B", options={"maxiter": 1000}).x


def fit_logit(X, y, off, lam, b0=None):
    Xt = X.T.tocsr()

    def f(b):
        z = off + X @ b
        return (np.sum(np.logaddexp(0, z) - y * z) + 0.5 * np.sum(lam * b * b),
                Xt @ (sigm(z) - y) + lam * b)
    return _fit(f, X.shape[1], b0)


def fit_gauss(X, y, lam, b0=None):
    Xt = X.T.tocsr()

    def f(b):
        e = X @ b - y
        return 0.5 * np.sum(e * e) + 0.5 * np.sum(lam * b * b), Xt @ e + lam * b
    return _fit(f, X.shape[1], b0)


def ll(y, p) -> float:
    return log_loss(y, np.clip(p, 1e-4, 1 - 1e-4), labels=[0, 1])


# ---------------------------------------------------------------- 골드 자료
def solo_gold() -> pd.DataFrame:
    import m0_attributes as M0
    fr, _, _ = M0.timeline_rows()
    f = fr[(fr["minute"] == 15) & fr["teamPosition"].isin(POS)]
    return f[["matchId", "teamId", "teamPosition", "gold"]].rename(columns={"gold": "gold15"})


def pro_gold() -> pd.DataFrame:
    oe = pd.read_csv(M1.OE_FILE, usecols=["gameid", "league", "date", "datacompleteness", "side", "position", "goldat15"],
                     low_memory=False)
    oe = oe[oe["league"].isin(M1.LEAGUES) & (oe["date"].str[:10] <= M1.END) & oe["position"].isin(H.OE_POS)
            & (oe["datacompleteness"] == "complete")]
    oe["teamId"] = np.where(oe["side"] == "Blue", 100, 200)
    oe["teamPosition"] = oe["position"].map(H.OE_POS)
    return oe.rename(columns={"gameid": "matchId", "goldat15": "gold15"})[["matchId", "teamId", "teamPosition", "gold15"]]


def lane_gold(gold: pd.DataFrame) -> pd.DataFrame:
    """경기 × 라인 15분 골드 차이 (블루 − 레드, 천 단위). 그 라인 선수가 다 있어야 값."""
    g = gold.dropna(subset=["gold15"]).copy()
    g["lane"] = g["teamPosition"].map(LANE_OF)
    a = g.groupby(["matchId", "lane", "teamId"])["gold15"].agg(["sum", "count"])
    need = a.index.get_level_values("lane").map(lambda L: len(LANES[L]))
    a.loc[a["count"].to_numpy() != np.asarray(need), "sum"] = np.nan
    t = a["sum"].unstack("teamId")
    return ((t[100] - t[200]) / 1000).unstack("lane")


class GoldData:
    """라인 행 (경기 × 라인) — 칸, 결과, 통제."""

    def __init__(self, games: pd.DataFrame, S: pd.DataFrame, gd: pd.DataFrame):
        rows, T, y = [], [], []
        gd = gd.reindex(games["matchId"].to_numpy())
        for i, row in enumerate(S.to_numpy()):
            for L in LANES:
                v = gd[L].iat[i] if L in gd else np.nan
                if np.isnan(v) or pd.isna(row).any():
                    continue
                rows.append((i, L)); T.append(gold_terms(row, L)); y.append(v)
        self.gi = np.array([r[0] for r in rows], dtype=int)
        self.lane = np.array([r[1] for r in rows])
        self.T, self.y = T, np.array(y)
        lg = games["league"].to_numpy()[self.gi]
        self.is_pro = lg != "SOLO"
        self.match = games["matchId"].to_numpy()[self.gi]
        c = [games["pro_diff"].to_numpy()[self.gi] * ~self.is_pro, games["elo_diff"].to_numpy()[self.gi] * self.is_pro,
             games["mastery_diff"].to_numpy()[self.gi] * self.is_pro]
        for src in (False, True):
            for L in LANES:
                c.append(((self.is_pro == src) & (self.lane == L)).astype(float))
        self.C = sparse.csr_matrix(np.column_stack(c))

    def X(self, coder: Coder) -> sparse.csr_matrix:
        return sparse.hstack([coder.matrix(self.T, self.is_pro), self.C]).tocsr()

    def sub(self, m: np.ndarray):
        o = object.__new__(GoldData)
        o.gi, o.lane, o.y, o.is_pro, o.match, o.C = self.gi[m], self.lane[m], self.y[m], self.is_pro[m], self.match[m], self.C[m]
        o.T = [t for t, k in zip(self.T, m) if k]
        return o


class GoldModel:
    def __init__(self, lam_gm, lam_gc, lam_gs):
        self.lam = {"gm": lam_gm, "gc": lam_gc, "gd": lam_gc, "gs": lam_gs}

    def fit(self, d: GoldData, b0=None):
        self.coder = Coder(d.T, d.is_pro)
        lam = np.r_[self.coder.penalty(self.lam), np.full(d.C.shape[1], 1e-3)]
        self.b = fit_gauss(d.X(self.coder), d.y, lam, b0)
        return self

    def predict(self, d: GoldData) -> np.ndarray:
        return d.X(self.coder) @ self.b

    def pair_gold(self, S: pd.DataFrame, games: pd.DataFrame) -> np.ndarray:
        """경기마다 쌍 칸(맞대결·바텀 듀오)이 예상하는 15분 골드 우세 합 (천 단위, 블루 기준)."""
        T = [sum((gold_terms(row, L, pairs_only=True) for L in LANES), []) for row in S.to_numpy()]
        X = self.coder.matrix(T, (games["league"] != "SOLO").to_numpy())
        return X @ (self.b[:self.coder.width] * self.coder.mask({"gc", "gd", "gs"}))


def pair_gold_oof(d: GoldData, S, games, cfg, folds=5) -> np.ndarray:
    fold = games["matchId"].map(lambda s: zlib.crc32(("g" + s).encode()) % folds).to_numpy()
    out = np.zeros(len(games))
    for k in range(folds):
        m = GoldModel(*cfg).fit(d.sub(fold[d.gi] != k))
        sel = fold == k
        out[sel] = m.pair_gold(S[sel], games[sel])
    return out


# ---------------------------------------------------------------- 교전 모델 (3차)
class FightData:
    """교전 행 (솔랭만, 킬 기록이 있는 경기). 결과 = 블루가 그 교전 킬을 더 많이 냄."""

    def __init__(self, games: pd.DataFrame, S: pd.DataFrame, F: pd.DataFrame):
        pos = pd.Series(np.arange(len(games)), index=games["matchId"].to_numpy())
        gt = [fight_terms(r) for r in S.to_numpy()]
        f = F[F["matchId"].isin(pos.index)]
        gi = pos.reindex(f["matchId"]).to_numpy()
        keep = np.array([len(gt[i]) > 0 for i in gi], dtype=bool)
        f, gi = f[keep], gi[keep]
        self.gi, self.y = gi, (f["blue_k"] > f["red_k"]).to_numpy(float)
        self.T = [gt[i] for i in gi]
        self.is_pro = np.zeros(len(gi), bool)
        mn = f["t"].to_numpy() / 60
        gd = f["gold_diff"].to_numpy()
        c = [np.nan_to_num(gd), np.isnan(gd).astype(float), games["pro_diff"].to_numpy()[gi]]
        c += [((mn >= lo) & (mn < hi)).astype(float) for lo, hi in ((0, 10), (10, 20), (20, 30), (30, 1e9))]
        self.C = sparse.csr_matrix(np.column_stack(c))

    def sub(self, m):
        o = object.__new__(FightData)
        o.gi, o.y, o.is_pro, o.C = self.gi[m], self.y[m], self.is_pro[m], self.C[m]
        o.T = [t for t, k in zip(self.T, m) if k]
        return o


class FightModel:
    def __init__(self, lam_fm, lam_fp):
        self.lam = {"fm": lam_fm, "fp": lam_fp}

    def prepare(self, d: FightData):
        self.coder = Coder(d.T, d.is_pro)
        self.X = sparse.hstack([self.coder.matrix(d.T, d.is_pro), d.C]).tocsr()
        return self

    def fit(self, d: FightData, b0=None):
        if not hasattr(self, "coder"):
            self.prepare(d)
        lam = np.r_[self.coder.penalty(self.lam), np.full(d.C.shape[1], 1e-3)]
        self.b = fit_logit(self.X, d.y, np.zeros(len(d.y)), lam, b0)
        return self

    def with_lam(self, lam_fm, lam_fp):
        """같은 칸 목록·행렬로 규제만 바꾼 모델 (그리드용)."""
        m = FightModel(lam_fm, lam_fp)
        m.coder, m.X = self.coder, self.X
        return m

    def predict(self, d: FightData) -> np.ndarray:
        X = sparse.hstack([self.coder.matrix(d.T, d.is_pro), d.C]).tocsr()
        return sigm(X @ self.b)

    def pair_score(self, S: pd.DataFrame) -> np.ndarray:
        """경기마다 같은 팀 쌍 칸 합 (교전 승리 로그 오즈, 블루 기준)."""
        T = [fight_terms(r) for r in S.to_numpy()]
        X = self.coder.matrix(T, np.zeros(len(T), bool))
        return X @ (self.b[:self.coder.width] * self.coder.mask({"fp"}))


def fight_score_oof(d: FightData, S, games, cfg, folds=5) -> np.ndarray:
    fold = games["matchId"].map(lambda s: zlib.crc32(("f" + s).encode()) % folds).to_numpy()
    out = np.zeros(len(games))
    for k in range(folds):
        m = FightModel(*cfg).fit(d.sub(fold[d.gi] != k))
        sel = fold == k
        out[sel] = m.pair_score(S[sel])
    return out


# ---------------------------------------------------------------- 승패 모델
class WinModel:
    """칸 목록(coder)·기본 행렬(X)은 밖에서 한 번 만들고, 규제·추가 칸(G, F)만 바꿔 가며 적합."""

    def __init__(self, lam_ch, lam_syn, lam_ctr, use_g, use_f):
        self.lam = {"ch": lam_ch, "chp": lam_ch * PATCH_MULT, "syn": lam_syn, "ctr": lam_ctr}
        self.use_g, self.use_f = use_g, use_f

    def design(self, X, g, f):
        extra = ([g] if self.use_g else []) + ([f] if self.use_f else [])
        return sparse.hstack([X, sparse.csr_matrix(np.column_stack(extra))]).tocsr() if extra else X

    def fit(self, coder, X, y, off, g, f, b0=None):
        self.coder = coder
        lam = np.r_[coder.penalty(self.lam), np.ones(int(self.use_g) + int(self.use_f))]
        self.b = fit_logit(self.design(X, g, f), y, off, lam, b0)
        return self

    def predict(self, X, off, g, f):
        return sigm(off + self.design(X, g, f) @ self.b)

    def extra_coef(self) -> dict:
        names = (["G"] if self.use_g else []) + (["F"] if self.use_f else [])
        return dict(zip(names, self.b[self.coder.width:]))


# ---------------------------------------------------------------- 표
def ko_names() -> dict:
    d = pd.read_csv(H.ROOT / "research" / "config" / "ult_types.csv", dtype=str, encoding="utf-8-sig")
    return dict(zip(d["champion_id"], d["name_ko"]))


def label(key: str, ko: dict) -> str:
    c, p = key.split("@")
    return f"{ko.get(c, c)}({PK.get(p, p)})"


def coef_table(coder: Coder, b: np.ndarray, B: np.ndarray | None, ko: dict, scale: float) -> dict:
    """칸 계수 → 종류별 표. 대회 값 = 공통 + 대회 편차. scale 로 단위 변환 (승률 %p 또는 골드)."""
    rows = {}
    for k, j in coder.idx.items():
        kind, key = k
        jp = coder.idx_pro.get(k)
        r = {"key": key, "n": coder.n.get(k, 0), "n_pro": coder.n_pro.get(k, 0), "val": b[j] * scale,
             "val_pro": (b[j] + (b[jp] if jp is not None else 0)) * scale}
        if B is not None:
            r["lo"], r["hi"] = np.percentile(B[:, j], [2.5, 97.5]) * scale
        base = key.split("|")[0]
        if kind in ("ch", "chp", "gm", "fm"):
            r["a"] = label(base, ko)
            if kind == "chp":
                r["patch"] = key.split("|")[1]
        else:
            x, y = base.split(">") if ">" in base else base.split("+")
            r["a"], r["b"] = label(x, ko), label(y, ko)
            r["pos_a"], r["pos_b"] = x.split("@")[1], y.split("@")[1]
        rows.setdefault(kind, []).append(r)
    out = {}
    for kind, v in rows.items():
        t = pd.DataFrame(v)
        if "lo" in t:
            t["sig"] = (t["lo"] > 0) | (t["hi"] < 0)
        out[kind] = t
    return out


def md(t: pd.DataFrame, cols: list[str], unit: str, n=12, asc=False, by="val") -> list[str]:
    t = t.sort_values(by, ascending=asc).head(n)
    out = []
    for r in t.to_dict("records"):
        ci = f" [{r['lo']:+.{unit}f}, {r['hi']:+.{unit}f}]" if "lo" in r else ""
        mk = "**" if r.get("sig") else ""
        out.append("| " + " | ".join(str(r[c]) for c in cols)
                   + f" | {r['n']:,} | {r['n_pro']} | {mk}{r['val']:+.{unit}f}{ci}{mk} | {r['val_pro']:+.{unit}f} |")
    return out


def section(t, title, cols, hdr, unit, unit_name, note="") -> list[str]:
    nsig = int(t["sig"].sum()) if "sig" in t else 0
    head = f"| {hdr} | 판수 | 대회 판수 | {unit_name} (공통) [95%] | 대회 |"
    sep = "|" + "---|" * (len(cols) + 4)
    L = [f"### {title}", "", f"- 칸 {len(t):,}개, 구간이 0 을 안 포함 {nsig}개 ({nsig / max(len(t), 1):.1%}). "
         f"|값| 중앙값 {t['val'].abs().median():.{unit}f}, 상위 1% {t['val'].abs().quantile(.99):.{unit}f}{note}", ""]
    L += ["**+ 쪽**", "", head, sep] + md(t, cols, unit) + [""]
    L += ["**− 쪽**", "", head, sep] + md(t, cols, unit, asc=True) + [""]
    return L


# ---------------------------------------------------------------- main
def m1_offsets(train, test, cfg):
    fold = train["matchId"].map(lambda s: zlib.crc32(("m2" + s).encode()) % 5).to_numpy()
    oof = np.full(len(train), np.nan)
    for k in range(5):
        oof[fold == k] = M1.Hier(*cfg).fit(train[fold != k]).predict(train[fold == k])
    return oof, M1.Hier(*cfg).fit(train).predict(test)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=30)
    a = ap.parse_args()
    t0 = time.time()
    import m2_fights
    cfg1 = M1.load_model().params()  # m1_params.json (점검 E4: pickle 대신)
    pro = M1.pro_players()
    solo, pdiff = M1.solo_players()
    ctrl = {"pro_diff": pdiff.to_dict(), "elo_diff": M1.elo(pro).to_dict(), "mastery_diff": M1.mastery(pro).to_dict()}
    players = pd.concat([solo, pro.drop(columns=["pid"])], ignore_index=True)
    gd_all = lane_gold(pd.concat([solo_gold(), pro_gold()], ignore_index=True))
    FA = m2_fights.fights()
    FA = FA[(FA["n_kills"] >= FIGHTS_MIN_KILLS) & (FA["blue_k"] != FA["red_k"])]
    w = H.research_windows().set_index("patch")
    ko = ko_names()
    log.info("자료 준비 (%.0f초): 라인 골드 경기 %d, 교전 %d", time.time() - t0, gd_all.dropna(how="all").shape[0], len(FA))

    # 1) 설정 고르기 — 패치별 자료
    P = {}
    for p in ("26.14", "26.15"):
        cut = w.loc[p, "window_from"]
        g = M1.game_rows(players, M1.load_attr(M1.M0 / f"m0_attributes_asof_{p}.csv"), ctrl)
        te = g[g["patch"] == p].reset_index(drop=True)
        tr = g[(g["day"] < cut) & ~g["matchId"].isin(te["matchId"])].reset_index(drop=True)
        oof, pte = m1_offsets(tr, te, cfg1)
        Str, Ste = slots(players, tr), slots(players, te)
        P[p] = dict(tr=tr, te=te, off_tr=logit(oof), off_te=logit(pte), Str=Str, Ste=Ste,
                    gtr=GoldData(tr, Str, gd_all), gte=GoldData(te, Ste, gd_all),
                    ftr=FightData(tr, Str, FA), fte=FightData(te, Ste, FA))
        log.info("%s 준비: 학습 %d경기 · 라인 %d · 교전 %d, 평가 %d경기 · 라인 %d · 교전 %d (%.0f초)", p, len(tr),
                 len(P[p]["gtr"].y), len(P[p]["ftr"].y), len(te), len(P[p]["gte"].y), len(P[p]["fte"].y), time.time() - t0)

    # 1-a) 골드 모델
    gsel = []
    for p, d in P.items():
        for cfg in itertools.product(*GOLD_GRID.values()):
            pr = GoldModel(*cfg).fit(d["gtr"]).predict(d["gte"])
            e = (pr - d["gte"].y) ** 2
            ip = d["gte"].is_pro
            gsel.append(dict(zip(GOLD_GRID, cfg), patch=p, mse_pro=e[ip].mean(), mse_solo=e[~ip].mean(),
                             var_pro=d["gte"].y[ip].var(), var_solo=d["gte"].y[~ip].var()))
    gsel = pd.DataFrame(gsel)
    gsel["r2_pro"] = 1 - gsel["mse_pro"] / gsel["var_pro"]
    gsel["r2_solo"] = 1 - gsel["mse_solo"] / gsel["var_solo"]
    gavg = gsel.groupby(list(GOLD_GRID))[["mse_pro", "mse_solo", "r2_pro", "r2_solo"]].mean()
    gavg["score"] = gavg["mse_pro"] / gavg["mse_pro"].min() + gavg["mse_solo"] / gavg["mse_solo"].min()
    gbest = gavg["score"].idxmin()
    H.save_csv(gsel, OUT / "m2_gold_selection.csv")
    log.info("골드 모델 고른 설정 %s (%.0f초)", gbest, time.time() - t0)

    # 1-b) 교전 모델 (칸 목록·행렬은 패치마다 한 번)
    fsel = []
    for p, d in P.items():
        base = FightModel(*next(itertools.product(*FIGHT_GRID.values()))).prepare(d["ftr"])
        Xte = sparse.hstack([base.coder.matrix(d["fte"].T, d["fte"].is_pro), d["fte"].C]).tocsr()
        b_prev = None
        for cfg in itertools.product(*FIGHT_GRID.values()):
            m = base.with_lam(*cfg).fit(d["ftr"], b_prev)
            b_prev = m.b
            fsel.append(dict(zip(FIGHT_GRID, cfg), patch=p, ll=ll(d["fte"].y, sigm(Xte @ m.b))))
        log.info("  %s 교전 그리드 끝 (칸 %d, %.0f초)", p, base.coder.width, time.time() - t0)
    fsel = pd.DataFrame(fsel)
    H.save_csv(fsel, OUT / "m2_fight_selection.csv")
    favg = fsel.groupby(list(FIGHT_GRID))["ll"].mean()
    fbest = favg.idxmin()
    log.info("교전 모델 고른 설정 %s", fbest)

    # 1-c) 승패 모델 (G = 골드 쌍 몫, F = 교전 쌍 몫, 학습 행은 교차 예측)
    for p, d in P.items():
        d["G_tr"] = pair_gold_oof(d["gtr"], d["Str"], d["tr"], gbest)
        d["G_te"] = GoldModel(*gbest).fit(d["gtr"]).pair_gold(d["Ste"], d["te"])
        d["F_tr"] = fight_score_oof(d["ftr"], d["Str"], d["tr"], fbest)
        d["F_te"] = FightModel(*fbest).fit(d["ftr"]).pair_score(d["Ste"])
        Ttr = [win_terms(r, pt) for r, pt in zip(d["Str"].to_numpy(), d["tr"]["patch"].astype(str))]
        Tte = [win_terms(r, pt) for r, pt in zip(d["Ste"].to_numpy(), d["te"]["patch"].astype(str))]
        ptr, pte_ = (d["tr"]["league"] != "SOLO").to_numpy(), (d["te"]["league"] != "SOLO").to_numpy()
        d["coder"] = Coder(Ttr, ptr)
        d["Xtr"], d["Xte"] = d["coder"].matrix(Ttr, ptr), d["coder"].matrix(Tte, pte_)
        d["y"], d["is_pro"] = d["te"]["blue_win"].to_numpy(int), pte_
        log.info("  %s G·F 준비 (%.0f초)", p, time.time() - t0)
    wsel, preds = [], {}
    for p, d in P.items():
        ytr = d["tr"]["blue_win"].to_numpy(float)
        preds[(p, "M1")] = sigm(d["off_te"])
        for cfg in itertools.product(*WIN_GRID.values()):
            m = WinModel(*cfg).fit(d["coder"], d["Xtr"], ytr, d["off_tr"], d["G_tr"], d["F_tr"])
            pr = m.predict(d["Xte"], d["off_te"], d["G_te"], d["F_te"])
            preds[(p, cfg)] = pr
            ip = d["is_pro"]
            ec = m.extra_coef()
            wsel.append(dict(zip(WIN_GRID, cfg), patch=p, ll_pro=ll(d["y"][ip], pr[ip]), ll_solo=ll(d["y"][~ip], pr[~ip]),
                             g_coef=ec.get("G", np.nan), f_coef=ec.get("F", np.nan)))
        log.info("  %s 승패 그리드 끝 (%.0f초)", p, time.time() - t0)
    wsel = pd.DataFrame(wsel)
    H.save_csv(wsel, OUT / "m2_selection.csv")
    wsel["ll"] = wsel["ll_pro"] + wsel["ll_solo"]
    wavg = wsel.groupby(list(WIN_GRID))[["ll", "ll_pro", "ll_solo", "g_coef", "f_coef"]].mean()
    best = wavg["ll"].idxmin()
    ix = wavg.index.to_frame(index=False)

    def pick(mask):
        s = wavg["ll"][mask.to_numpy()]
        return s.index[int(np.argmin(s.to_numpy()))]

    none_pairs = (ix["lam_syn"] == BIG) & (ix["lam_ctr"] == BIG)
    variants = {"M1 + 챔피언 단독": pick(none_pairs & ~ix["use_g"] & ~ix["use_f"]),
                "+ 구조 시너지 (승패)": pick((ix["lam_syn"] < BIG) & (ix["lam_ctr"] == BIG) & ~ix["use_g"] & ~ix["use_f"]),
                "+ 구조 카운터 (승패)": pick((ix["lam_syn"] == BIG) & (ix["lam_ctr"] < BIG) & ~ix["use_g"] & ~ix["use_f"]),
                "+ 라인 골드 상성 G": pick(none_pairs & ix["use_g"] & ~ix["use_f"]),
                "+ 교전 시너지 F": pick(none_pairs & ~ix["use_g"] & ix["use_f"]),
                "+ G + F": pick(none_pairs & ix["use_g"] & ix["use_f"]),
                "**M2 3차 (고른 설정)**": best}
    log.info("승패 고른 설정 %s", best)

    def fmtc(c):
        return "∞" if c == BIG else f"{c:g}"

    L = ["# M2 시너지·카운터 평가 (3차: 구조 쌍 + 15분 골드 + 교전)", "",
         "- 1차(모든 쌍, 승패만) → 쌍 효과 0. 2차(구조 쌍 + 15분 라인 골드) → 맞라인 상성은 골드에서 보이나 승패 개선 미미. "
         "3차 = 2차 + **정글+미드·정글+탑 듀오 골드** + **교전 단위 시너지** (이전 결과 `m2/_v1/`, `_v1_wide/`, `_v2/`)",
         "- 골드 모델: 라인(탑·정글·미드·바텀) × 경기, 결과 = 블루 − 레드 15분 골드. 칸 = 라인 챔피언 + 맞대결 + 바텀 듀오 + 정글 듀오",
         f"- 교전 모델: 솔랭 킬 기록을 15초 묶음으로, 킬 {FIGHTS_MIN_KILLS}개 이상이고 킬 수가 갈린 교전 1행, 결과 = 블루가 교전에서 킬을 더 냄. "
         "칸 = 챔피언 단독 + 같은 팀 모든 쌍 (경기 조합), 통제 = 교전 시점 팀 골드 차이(10·15·20·25분 선형 보간)·시간대·프로 수",
         "- 승패 모델: 오프셋 = M1 교차 예측, 칸 = 챔피언 단독·패치 편차 + 구조 시너지 + 구조 카운터 + G(골드 쌍 몫 합) + F(교전 쌍 몫 합)",
         "- 플레이오프(08-26~)는 보지 않음", "",
         "## 1. 설정 고르기 (26.14 · 26.15, 학습 = 그 패치 전, 평가 = 그 패치)", "",
         "### 1-a. 골드 모델 (평가 라인 R², 두 패치 평균)", "",
         "| 챔피언 단독 규제 | 맞대결·바텀 듀오 규제 | 정글 듀오 규제 | 대회 R² | 솔랭 R² |", "|---|---|---|---|---|"]
    for (gm_, gc_, gs_), r in gavg.iterrows():
        mk = "**" if (gm_, gc_, gs_) == gbest else ""
        L.append(f"| {mk}{fmtc(gm_)}{mk} | {mk}{fmtc(gc_)}{mk} | {mk}{fmtc(gs_)}{mk} | {r['r2_pro']:.4f} | {r['r2_solo']:.4f} |")
    L += ["", "- ∞ = 그 칸 없음. 정글 듀오 ∞ 보다 낮은 규제가 골라지면 = 정글 듀오 시너지가 평가 패치 골드에서 보임", "",
          "### 1-b. 교전 모델 (평가 교전 로그 손실, 두 패치 평균 · 솔랭만)", "",
          "| 챔피언 단독 규제 | 같은 팀 쌍 규제 | 교전 로그 손실 |", "|---|---|---|"]
    for (fm_, fp_), v in favg.items():
        mk = "**" if (fm_, fp_) == fbest else ""
        L.append(f"| {mk}{fmtc(fm_)}{mk} | {mk}{fmtc(fp_)}{mk} | {v:.5f} |")
    L += ["", "- 쌍 규제 ∞ = 쌍 칸 없음. ∞ 보다 낮으면 = **같은 팀 조합 시너지가 교전 결과에 보임**", "",
          "### 1-c. 승패 모델 (로그 손실)", "",
          "| 패치 | 모델 | 대회 경기 | 대회 로그 손실 | 솔랭 경기 | 솔랭 로그 손실 |", "|---|---|---|---|---|---|"]
    for p, d in P.items():
        y, ip = d["y"], d["is_pro"]
        for name, key in [("M1 만", "M1")] + list(variants.items()):
            pr = preds[(p, key)]
            L.append(f"| {p} | {name} | {ip.sum()} | {ll(y[ip], pr[ip]):.4f} | {(~ip).sum()} | {ll(y[~ip], pr[~ip]):.4f} |")
    L += ["", "| 변형 | 규제 (챔피언 · 시너지 · 카운터) · G · F | 두 패치 평균 대회 | 솔랭 | G 계수 | F 계수 |", "|---|---|---|---|---|---|"]
    for name, key in variants.items():
        r = wavg.loc[key]
        L.append(f"| {name} | {fmtc(key[0])} · {fmtc(key[1])} · {fmtc(key[2])} · {'G' if key[3] else '−'} · {'F' if key[4] else '−'} | "
                 f"{r['ll_pro']:.4f} | {r['ll_solo']:.4f} | {r['g_coef']:+.3f} | {r['f_coef']:+.3f} |")
    L += ["", "- G 계수 = 골드 모델이 예상한 쌍 골드 우세 1,000 당 승리 로그 오즈, F 계수 = 교전 모델 쌍 몫(교전 승리 로그 오즈) 1 당 경기 승리 로그 오즈", ""]

    # 2) 최종 학습
    G = pd.read_csv(PROCESSED / "m1" / "m1_games.csv", dtype={"patch": str, "matchId": str}, low_memory=False)
    S = slots(players, G)
    is_pro = (G["league"] != "SOLO").to_numpy()
    y = G["blue_win"].to_numpy(float)
    off = logit(G["m1_pred_oof"].to_numpy())
    rng = np.random.default_rng(0)
    lg_groups = list(G.groupby("league").indices.values())

    def boot_games():
        pick_g = np.concatenate([rng.choice(ix_, len(ix_), replace=True) for ix_ in lg_groups])
        return np.bincount(pick_g, minlength=len(G))

    # 골드
    gdat = GoldData(G, S, gd_all)
    gm = GoldModel(*gbest).fit(gdat)
    Xg = gdat.X(gm.coder)
    lam_g = np.r_[gm.coder.penalty(gm.lam), np.full(gdat.C.shape[1], 1e-3)]
    GB = []
    for k in range(a.boot):
        rows = np.repeat(np.arange(len(gdat.y)), boot_games()[gdat.gi])
        GB.append(fit_gauss(Xg[rows], gdat.y[rows], lam_g, gm.b)[:gm.coder.width])
    log.info("골드 최종·부트스트랩 끝 (%.0f초)", time.time() - t0)
    GT = coef_table(gm.coder, gm.b, np.array(GB) if GB else None, ko, 1000.0)
    if "gs" in GT:  # 두 라인 행에 같은 칸 → 두 라인 합 골드 효과 = 계수 × 2
        for c in ("val", "val_pro", "lo", "hi"):
            if c in GT["gs"]:
                GT["gs"][c] *= 2
    # 교전
    fdat = FightData(G, S, FA)
    fmod = FightModel(*fbest).fit(fdat)
    lam_f = np.r_[fmod.coder.penalty(fmod.lam), np.full(fdat.C.shape[1], 1e-3)]
    FB = []
    for k in range(a.boot):
        rows = np.repeat(np.arange(len(fdat.y)), boot_games()[fdat.gi])
        FB.append(fit_logit(fmod.X[rows], fdat.y[rows], np.zeros(len(rows)), lam_f, fmod.b)[:fmod.coder.width])
        if (k + 1) % 10 == 0:
            log.info("교전 부트스트랩 %d/%d (%.0f초)", k + 1, a.boot, time.time() - t0)
    FT = coef_table(fmod.coder, fmod.b, np.array(FB) if FB else None, ko, 25.0)  # 교전 승률 %p (50% 근처)
    # 승패
    Gf = pair_gold_oof(gdat, S, G, gbest)
    Ff = fight_score_oof(fdat, S, G, fbest)
    T = [win_terms(r, pt) for r, pt in zip(S.to_numpy(), G["patch"].astype(str))]
    wc = Coder(T, is_pro)
    Xw0 = wc.matrix(T, is_pro)
    wm = WinModel(*best).fit(wc, Xw0, y, off, Gf, Ff)
    Xw = wm.design(Xw0, Gf, Ff)
    lam_w = np.r_[wc.penalty(wm.lam), np.ones(Xw.shape[1] - wc.width)]
    WB = []
    for k in range(a.boot):
        idx = np.repeat(np.arange(len(G)), boot_games())
        WB.append(fit_logit(Xw[idx], y[idx], off[idx], lam_w, wm.b))
    log.info("승패 최종·부트스트랩 끝 (%.0f초)", time.time() - t0)
    WB = np.array(WB) if WB else None
    WT = coef_table(wc, wm.b, WB[:, :wc.width] if WB is not None else None, ko, 25.0)
    ec = wm.extra_coef()
    ec_ci = {n: np.percentile(WB[:, wc.width + i], [2.5, 97.5]) for i, n in enumerate(ec)} if WB is not None else {}
    tg = gd_all.reindex(G["matchId"]).sum(axis=1, min_count=4).to_numpy()
    okg = ~np.isnan(tg)
    slope = fit_logit(sparse.csr_matrix(tg[okg][:, None]), y[okg], off[okg], np.array([1e-6]))[0]
    for kind in ("gc", "gd", "gs", "gm"):
        if kind in GT:
            GT[kind]["win_pp"] = GT[kind]["val"] / 1000 * slope * 25
    names = {"gm": "m2_lane_champion", "gc": "m2_lane_counter", "gd": "m2_bot_duo", "gs": "m2_jungle_duo",
             "fm": "m2_fight_champion", "fp": "m2_fight_synergy", "ch": "m2_champion",
             "chp": "m2_champion_patch", "syn": "m2_synergy", "ctr": "m2_counter"}
    for kind, t in list(GT.items()) + list(FT.items()) + list(WT.items()):
        H.save_csv(t, OUT / f"{names[kind]}.csv")
    fold = G["matchId"].map(lambda s: zlib.crc32(("m2" + s).encode()) % 5).to_numpy()
    G["m2_pred"] = wm.predict(Xw0, off, Gf, Ff)
    G["m2_pred_oof"] = np.nan
    for k in range(5):
        tr_ = fold != k
        mk = WinModel(*best).fit(wc, Xw0[tr_], y[tr_], off[tr_], Gf[tr_], Ff[tr_])  # 시작점 = 0 (전체 자료 해에서 출발하면 누수)
        G.loc[~tr_, "m2_pred_oof"] = mk.predict(Xw0[~tr_], off[~tr_], Gf[~tr_], Ff[~tr_])
    G["m2_pair_gold"], G["m2_fight_score"] = Gf, Ff
    H.save_csv(G[["matchId", "league", "day", "patch", "blue_win", "m1_pred_oof", "m2_pair_gold", "m2_fight_score",
                  "m2_pred_oof", "m2_pred"]], OUT / "m2_games.csv")
    pd.to_pickle({"gold": gm, "fight": fmod, "win": wm, "gold_cfg": gbest, "fight_cfg": fbest, "win_cfg": best,
                  "gold_to_logodds_per_1k": slope}, OUT / "m2_model.pkl")

    L += ["## 2. 최종 (08-25 까지 전부)", "",
          f"- 골드 모델: 라인 {len(gdat.y):,}행 (대회 {gdat.is_pro.sum():,}). 칸: 라인 챔피언 {len(GT.get('gm', []))} · 맞대결 {len(GT.get('gc', []))} · "
          f"바텀 듀오 {len(GT.get('gd', []))} · 정글 듀오 {len(GT.get('gs', []))}",
          f"- 교전 모델: 교전 {len(fdat.y):,}행 ({len(np.unique(fdat.gi)):,}경기). 칸: 챔피언 {len(FT.get('fm', []))} · 같은 팀 쌍 {len(FT.get('fp', []))}",
          f"- 승패 모델: 칸: 챔피언 {len(WT.get('ch', []))} · 챔피언×패치 {len(WT.get('chp', []))} · 시너지 {len(WT.get('syn', []))} · 카운터 {len(WT.get('ctr', []))}. "
          + ", ".join(f"{n} 계수 {v:+.3f} [{ec_ci[n][0]:+.3f}, {ec_ci[n][1]:+.3f}]" if n in ec_ci else f"{n} 계수 {v:+.3f}" for n, v in ec.items()),
          f"- 골드 → 승률: 15분 팀 골드 1,000 우세 = 로그 오즈 {slope:+.3f} (50% 근처 약 {slope * 25:+.1f}%p)",
          f"- 교차 예측 로그 손실 (학습 자료 안, 참고): 대회 M1 {ll(y[is_pro], G['m1_pred_oof'][is_pro]):.4f} → M2 {ll(y[is_pro], G['m2_pred_oof'][is_pro]):.4f} · "
          f"솔랭 {ll(y[~is_pro], G['m1_pred_oof'][~is_pro]):.4f} → {ll(y[~is_pro], G['m2_pred_oof'][~is_pro]):.4f}",
          f"- 구간: 경기 단위 부트스트랩 {a.boot}회 95%. 규제가 센 칸은 구간도 좁게 나오므로 **굵게 = 크기가 큰 것과 같은 뜻이 아님**", ""]
    if "fp" in FT:
        L += ["## 3. 교전 시너지 (같은 팀 쌍, 교전 승률 %p — 50% 근처, 골드 상황 통제)", ""]
        L += section(FT["fp"], "같은 팀 쌍", ["a", "b"], "챔피언 | 같이", 2, "교전 승률 %p")
        lee = FT["fp"][FT["fp"]["key"].str.contains("LeeSin@JUNGLE", regex=False)].sort_values("n", ascending=False).head(12)
        if len(lee):
            L += ["**예시: 리 신(정글)이 들어간 쌍 (판수 많은 순)**", "", "| 챔피언 | 같이 | 교전 수 (판수 칸도 교전 수) | 교전 승률 %p [95%] |", "|---|---|---|---|"]
            for r in lee.to_dict("records"):
                mk = "**" if r.get("sig") else ""
                L.append(f"| {r['a']} | {r['b']} | {r['n']:,} | {mk}{r['val']:+.2f} [{r['lo']:+.2f}, {r['hi']:+.2f}]{mk} |")
            L += [""]
        L += section(FT["fm"], "교전 챔피언 단독 (교전 승률 %p)", ["a"], "챔피언", 2, "교전 승률 %p")
    if "gs" in GT:
        t = GT["gs"]
        L += ["## 4. 정글 듀오 (두 라인 합 15분 골드)", ""]
        for q, nm in (("MIDDLE", "정글 + 미드"), ("TOP", "정글 + 탑")):
            tq = t[t["pos_b"] == q]
            if len(tq):
                L += section(tq, nm, ["a", "b"], "정글 | 같이", 0, "15분 골드")
    if "gc" in GT:
        t = GT["gc"]
        t["lane"] = t["pos_a"].map(LANE_OF)
        L += ["## 5. 맞라인 상성 (15분 골드, 앞 챔피언이 뒤 챔피언 상대로)", ""]
        for lane in LANES:
            tl = t[t["lane"] == lane]
            if len(tl):
                L += section(tl, f"{LANE_KO[lane]}", ["a", "b"], "챔피언 | 상대", 0, "15분 골드",
                             f". 승률 환산 상위 1% ≈ {tl['win_pp'].abs().quantile(.99):.1f}%p")
    if "gd" in GT:
        L += section(GT["gd"], "바텀 듀오 (15분 바텀 골드)", ["a", "b"], "원딜 | 서폿", 0, "15분 골드")
    if "gm" in GT:
        L += section(GT["gm"], "라인 챔피언 단독 (15분 라인 골드, 골드를 덜 먹는 성향도 음수)", ["a"], "챔피언", 0, "15분 골드")
    L += ["## 6. 승패 모델 칸", ""]
    for kind, title, cols, hdr in (("ch", "챔피언 단독 (속성으로 설명 안 되는 승리 효과)", ["a"], "챔피언"),
                                   ("syn", "구조 시너지 (승패)", ["a", "b"], "챔피언 | 같이"),
                                   ("ctr", "구조 카운터 (승패, 앞이 뒤 상대로)", ["a", "b"], "챔피언 | 상대")):
        if kind in WT:
            L += section(WT[kind], title, cols, hdr, 1, "승률 %p")
    (OUT / "m2_eval.md").write_text("\n".join(L), encoding="utf-8")
    log.info("M2 끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
