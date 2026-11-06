"""M2 시너지·카운터 — 조합 성격(M1)으로 설명되지 않는 챔피언 · 챔피언 쌍 효과 (docs/PIPELINE.md 7-7, 2026-11-06 1차)

행 = 경기 1개 (블루 기준), 결과 = 블루 승, **오프셋 = M1 예측 승률의 로짓** (M1 이 이미 설명한 부분은 고정).
칸 (모두 진영 반대칭: 블루에 있으면 +1, 레드에 있으면 −1). 키 = 챔피언@포지션
  - 챔피언 단독: 속성으로 설명 안 되는 그 챔피언 자체의 승리 효과 (+ 패치별 편차)
  - 시너지: 같은 팀 챔피언 쌍 (팀당 10쌍)
  - 카운터: 상대 팀 챔피언 쌍 (25쌍, 같은 포지션 = 맞라인 표시). 계수 = 앞 챔피언이 뒤 챔피언 상대로 얻는 몫
계층: 공통(솔랭이 대부분) + 대회 편차. 릿지 (판수가 적을수록 0 쪽으로). 칸별 규제 크기는 26.14 · 26.15 로 고름.
최소 판수: 쌍은 학습 자료에 MIN_PAIR 판 이상 나온 것만 칸을 만듦 (나머지는 0 = 효과 없음으로 둠).

절차 (7-7 검증 규칙, M1 과 같음):
  1) 설정 고르기: 26.14 · 26.15 마다 cut 이전 경기로 M1(고른 설정)을 다시 학습 → 학습 행 = 5묶음 교차 예측, 평가 행 = 예측을 오프셋으로.
     M2 를 학습해 그 패치 대회 경기(주) + 솔랭 경기(보조)에서 로그 손실 비교 (M1 만 vs M1 + M2)
  2) 고른 설정으로 08-25 까지 전부 재학습 (오프셋 = m1_games.csv 의 m1_pred_oof), 경기 단위 부트스트랩 구간
  플레이오프(08-26~)는 보지 않음

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
MIN_PAIR = 15        # 쌍 칸을 만드는 최소 판수 (학습 자료)
MIN_PRO = 5          # 대회 편차 칸을 만드는 최소 대회 판수
MIN_PATCH = 10       # 챔피언 × 패치 편차 칸 최소 판수
PATCH_MULT = 2.0     # 패치 편차 규제 = 챔피언 규제 × 이 배율
GRID = {"lam_ch": [3.0, 10.0, 30.0], "lam_syn": [10.0, 30.0, 100.0], "lam_ctr": [10.0, 30.0, 100.0], "pro_mult": [3.0, 10.0]}


# ---------------------------------------------------------------- 자료
def slots(players: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """경기 × 10칸(b_TOP … r_UTILITY) 키 = 챔피언@포지션. games 순서 그대로."""
    p = players[players["matchId"].isin(games["matchId"])].copy()
    p["key"] = p["champion_id"].astype(str) + "@" + p["teamPosition"]
    p["slot"] = np.where(p["teamId"] == 100, "b_", "r_") + p["teamPosition"]
    s = p.pivot_table(index="matchId", columns="slot", values="key", aggfunc="first")
    cols = [f"b_{q}" for q in POS] + [f"r_{q}" for q in POS]
    return s.reindex(index=games["matchId"].to_numpy(), columns=cols)


def terms(row: np.ndarray, patch: str):
    """한 경기 → [(종류, 키, 값)]."""
    b, r = row[:5], row[5:]
    out = []
    for k in b:
        out += [("ch", k, 1.0), ("chp", f"{k}|{patch}", 1.0)]
    for k in r:
        out += [("ch", k, -1.0), ("chp", f"{k}|{patch}", -1.0)]
    for team, v in ((b, 1.0), (r, -1.0)):
        for x, y in itertools.combinations(sorted(team), 2):
            out.append(("syn", f"{x}+{y}", v))
    for x in b:
        for y in r:
            out.append(("ctr", f"{x}>{y}", 1.0) if x < y else ("ctr", f"{y}>{x}", -1.0))
    return out


class Coder:
    """학습 자료로 칸 목록을 정하고, 경기들을 희소 행렬로 바꿈 (공통 칸 + 대회 편차 칸)."""

    def __init__(self, S: pd.DataFrame, games: pd.DataFrame):
        T = self._all_terms(S, games)
        pro = (games["league"] != "SOLO").to_numpy()
        cnt, cnt_pro = {}, {}
        for i, ts in enumerate(T):
            for kind, key, _ in ts:
                cnt[(kind, key)] = cnt.get((kind, key), 0) + 1
                if pro[i]:
                    cnt_pro[(kind, key)] = cnt_pro.get((kind, key), 0) + 1
        need = {"ch": 1, "chp": MIN_PATCH, "syn": MIN_PAIR, "ctr": MIN_PAIR}
        self.cols = [k for k, n in cnt.items() if n >= need[k[0]]]
        self.cols_pro = [k for k in self.cols if k[0] != "chp" and cnt_pro.get(k, 0) >= MIN_PRO]
        self.idx = {k: i for i, k in enumerate(self.cols)}
        self.idx_pro = {k: len(self.cols) + i for i, k in enumerate(self.cols_pro)}
        self.n, self.n_pro = cnt, cnt_pro
        self.width = len(self.cols) + len(self.cols_pro)

    @staticmethod
    def _all_terms(S, games):
        return [terms(row, p) if not pd.isna(row).any() else [] for row, p in zip(S.to_numpy(), games["patch"].astype(str))]

    def matrix(self, S: pd.DataFrame, games: pd.DataFrame) -> sparse.csr_matrix:
        T = self._all_terms(S, games)
        pro = (games["league"] != "SOLO").to_numpy()
        ri, ci, v = [], [], []
        for i, ts in enumerate(T):
            for kind, key, val in ts:
                j = self.idx.get((kind, key))
                if j is not None:
                    ri.append(i); ci.append(j); v.append(val)
                    if pro[i]:
                        jp = self.idx_pro.get((kind, key))
                        if jp is not None:
                            ri.append(i); ci.append(jp); v.append(val)
        return sparse.csr_matrix((v, (ri, ci)), shape=(len(T), self.width))

    def penalty(self, lam_ch, lam_syn, lam_ctr, pro_mult) -> np.ndarray:
        base = {"ch": lam_ch, "chp": lam_ch * PATCH_MULT, "syn": lam_syn, "ctr": lam_ctr}
        lam = [base[k[0]] for k in self.cols] + [base[k[0]] * pro_mult for k in self.cols_pro]
        return np.array(lam)


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def fit_ridge(X, y, off, lam, w=None, b0=None):
    """릿지 로지스틱, 오프셋 있음. 손실 = Σ w·로그 손실 + ½ Σ λ b²."""
    w = np.ones(len(y)) if w is None else w
    Xt = X.T.tocsr()

    def f(b):
        z = off + X @ b
        pz = 1 / (1 + np.exp(-z))
        loss = np.sum(w * (np.logaddexp(0, z) - y * z)) + 0.5 * np.sum(lam * b * b)
        grad = Xt @ (w * (pz - y)) + lam * b
        return loss, grad

    b0 = np.zeros(X.shape[1]) if b0 is None else b0
    r = minimize(f, b0, jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return r.x


def scores(y, p) -> dict:
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return {"n": len(y), "logloss": log_loss(y, p, labels=[0, 1])}


# ---------------------------------------------------------------- M1 오프셋 (설정 고르기용)
def m1_offsets(train: pd.DataFrame, test: pd.DataFrame, cfg: tuple) -> tuple[np.ndarray, np.ndarray]:
    fold = train["matchId"].map(lambda s: zlib.crc32(("m2" + s).encode()) % 5).to_numpy()
    oof = np.full(len(train), np.nan)
    for k in range(5):
        m = M1.Hier(*cfg).fit(train[fold != k])
        oof[fold == k] = m.predict(train[fold == k])
    return oof, M1.Hier(*cfg).fit(train).predict(test)


# ---------------------------------------------------------------- 결과 표
def ko_names() -> dict:
    f = H.ROOT / "research" / "config" / "ult_types.csv"
    d = pd.read_csv(f, dtype=str, encoding="utf-8-sig")
    return dict(zip(d["champion_id"], d["name_ko"]))


def label(key: str, ko: dict) -> str:
    c, p = key.split("@")
    return f"{ko.get(c, c)}({PK.get(p, p)})"


def tables(coder: Coder, b: np.ndarray, B: np.ndarray | None, ko: dict) -> dict[str, pd.DataFrame]:
    """칸 계수 → 표. 대회 값 = 공통 + 대회 편차."""
    pro_add = {k: b[j] for k, j in coder.idx_pro.items()}
    pro_add_b = ({k: B[:, j] for k, j in coder.idx_pro.items()} if B is not None else {})
    rows = {"ch": [], "chp": [], "syn": [], "ctr": []}
    for k, j in coder.idx.items():
        kind, key = k
        r = {"key": key, "n": coder.n.get(k, 0), "n_pro": coder.n_pro.get(k, 0), "coef": b[j],
             "coef_pro": b[j] + pro_add.get(k, 0.0)}
        if B is not None:
            bs = B[:, j]
            bp = bs + pro_add_b.get(k, 0.0)
            r.update(lo=np.percentile(bs, 2.5), hi=np.percentile(bs, 97.5),
                     lo_pro=np.percentile(bp, 2.5), hi_pro=np.percentile(bp, 97.5))
        if kind in ("ch", "chp"):
            ck = key.split("|")[0]
            r["champion"] = label(ck, ko)
            if kind == "chp":
                r["patch"] = key.split("|")[1]
        elif kind == "syn":
            x, y = key.split("+")
            r.update(a=label(x, ko), b=label(y, ko))
        else:
            x, y = key.split(">")
            r.update(a=label(x, ko), b=label(y, ko), lane=x.split("@")[1] == y.split("@")[1])
        rows[kind].append(r)
    out = {k: pd.DataFrame(v) for k, v in rows.items()}
    for t in out.values():
        if "lo" in t:
            t["sig"] = (t["lo"] > 0) | (t["hi"] < 0)
        t["pp"] = t["coef"] * 25  # 50% 근처 승률 %p
        t["pp_pro"] = t["coef_pro"] * 25
    return out


def md_rows(t: pd.DataFrame, cols: list[str], n=15, by="coef", asc=False) -> list[str]:
    t = t.sort_values(by, ascending=asc).head(n)
    out = []
    for r in t.to_dict("records"):
        ci = f" [{r['lo']*25:+.1f}, {r['hi']*25:+.1f}]" if "lo" in r else ""
        mark = "**" if r.get("sig") else ""
        out.append("| " + " | ".join(str(r[c]) for c in cols) + f" | {r['n']:,} | {r['n_pro']} | {mark}{r['pp']:+.1f}{ci}{mark} | {r['pp_pro']:+.1f} |")
    return out


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=30)
    a = ap.parse_args()
    t0 = time.time()
    m1 = pd.read_pickle(PROCESSED / "m1" / "m1_model.pkl")
    cfg1 = m1.params()
    log.info("M1 설정 %s", cfg1)
    pro = M1.pro_players()
    solo, pdiff = M1.solo_players()
    ctrl = {"pro_diff": pdiff.to_dict(), "elo_diff": M1.elo(pro).to_dict(), "mastery_diff": M1.mastery(pro).to_dict()}
    players = pd.concat([solo, pro.drop(columns=["pid"])], ignore_index=True)
    w = H.research_windows().set_index("patch")
    ko = ko_names()

    L = ["# M2 시너지·카운터 평가 (1차)", "",
         "- 행 = 경기 (블루 기준), 결과 = 블루 승, 오프셋 = M1 예측 로짓 (M1 이 설명한 조합 성격은 고정)",
         "- 칸: 챔피언 단독(+패치 편차), 같은 팀 쌍(시너지), 상대 쌍(카운터). 키 = 챔피언@포지션. 공통 + 대회 편차, 릿지",
         f"- 쌍 칸은 학습 자료 {MIN_PAIR}판 이상만, 대회 편차 칸은 대회 {MIN_PRO}판 이상만. 플레이오프(08-26~)는 보지 않음",
         f"- M1 설정: {cfg1}", ""]

    # 1) 설정 고르기
    sel, res = [], {}
    for p in ("26.14", "26.15"):
        cut = w.loc[p, "window_from"]
        at = M1.load_attr(M1.M0 / f"m0_attributes_asof_{p}.csv")
        g = M1.game_rows(players, at, ctrl)
        test = g[g["patch"] == p]
        train = g[(g["day"] < cut) & ~g["matchId"].isin(test["matchId"])]
        oof, ptest = m1_offsets(train, test, cfg1)
        log.info("%s: 학습 %d · 평가 %d (대회 %d) · M1 오프셋 끝 (%.0f초)", p, len(train), len(test),
                 (test["league"] != "SOLO").sum(), time.time() - t0)
        Str, Ste = slots(players, train), slots(players, test)
        coder = Coder(Str, train)
        Xtr, Xte = coder.matrix(Str, train), coder.matrix(Ste, test)
        log.info("  칸 %d (대회 편차 %d)", len(coder.cols), len(coder.cols_pro))
        ytr, yte = train["blue_win"].to_numpy(float), test["blue_win"].to_numpy(int)
        offtr, offte = logit(oof), logit(ptest)
        is_pro = (test["league"] != "SOLO").to_numpy()
        res[(p, "M1")] = ptest
        b_prev = None
        for cfg in itertools.product(*GRID.values()):
            lam = coder.penalty(*cfg)
            b = fit_ridge(Xtr, ytr, offtr, lam, b0=b_prev)
            b_prev = b
            pr = 1 / (1 + np.exp(-(offte + Xte @ b)))
            res[(p, cfg)] = pr
            sel.append(dict(zip(GRID, cfg), patch=p, ll_pro=scores(yte[is_pro], pr[is_pro])["logloss"],
                            ll_solo=scores(yte[~is_pro], pr[~is_pro])["logloss"]))
        res[(p, "y")], res[(p, "pro")] = yte, is_pro
        log.info("  그리드 끝 (%.0f초)", time.time() - t0)
    sel = pd.DataFrame(sel)
    H.save_csv(sel, OUT / "m2_selection.csv")
    sel["ll"] = sel["ll_pro"] + sel["ll_solo"]  # 대회(주) + 솔랭(보조) 같은 비중
    avg = sel.groupby(list(GRID))[["ll", "ll_pro", "ll_solo"]].mean()
    best = avg["ll"].idxmin()
    log.info("고른 설정 %s", best)
    L += ["## 1. 설정 고르기 (26.14 · 26.15, 학습 = 그 패치 전, 평가 = 그 패치)", "",
          "| 패치 | 모델 | 대회 경기 | 대회 로그 손실 | 솔랭 경기 | 솔랭 로그 손실 |", "|---|---|---|---|---|---|"]
    for p in ("26.14", "26.15"):
        y, ip = res[(p, "y")], res[(p, "pro")]
        for name, key in (("M1 만", "M1"), ("M1 + M2 (고른 설정)", best)):
            pr = res[(p, key)]
            L.append(f"| {p} | {name} | {ip.sum()} | {scores(y[ip], pr[ip])['logloss']:.4f} | {(~ip).sum()} | "
                     f"{scores(y[~ip], pr[~ip])['logloss']:.4f} |")
    L += ["", f"- 고른 설정: 챔피언 규제 {best[0]}, 시너지 규제 {best[1]}, 카운터 규제 {best[2]}, 대회 편차 배율 {best[3]} "
          f"(그리드 {len(avg)}개 중 대회 + 솔랭 평균 로그 손실 최소). 규제가 클수록 0 쪽으로 세게 당김", "",
          "| 시너지 규제 | 카운터 규제 | 두 패치 평균 대회 로그 손실 | 솔랭 로그 손실 (나머지 설정은 고른 값) |", "|---|---|---|---|"]
    for (ls, lc), r in avg.xs((best[0], best[3]), level=["lam_ch", "pro_mult"]).iterrows():
        L.append(f"| {ls} | {lc} | {r['ll_pro']:.4f} | {r['ll_solo']:.4f} |")
    L += [""]

    # 2) 최종 학습
    G = pd.read_csv(PROCESSED / "m1" / "m1_games.csv", dtype={"patch": str, "matchId": str}, low_memory=False)
    S = slots(players, G)
    coder = Coder(S, G)
    X = coder.matrix(S, G)
    y = G["blue_win"].to_numpy(float)
    off = logit(G["m1_pred_oof"].to_numpy())
    lam = coder.penalty(*best)
    b = fit_ridge(X, y, off, lam)
    log.info("최종 학습: 칸 %d (대회 편차 %d) (%.0f초)", len(coder.cols), len(coder.cols_pro), time.time() - t0)
    rng = np.random.default_rng(0)
    B = []
    groups = list(G.groupby("league").indices.values())
    for k in range(a.boot):
        idx = np.concatenate([rng.choice(ix, len(ix), replace=True) for ix in groups])
        B.append(fit_ridge(X[idx], y[idx], off[idx], lam, b0=b))
        if (k + 1) % 10 == 0:
            log.info("부트스트랩 %d/%d (%.0f초)", k + 1, a.boot, time.time() - t0)
    B = np.array(B) if B else None
    T = tables(coder, b, B, ko)
    for kind, name in (("ch", "m2_champion"), ("chp", "m2_champion_patch"), ("syn", "m2_synergy"), ("ctr", "m2_counter")):
        H.save_csv(T[kind], OUT / f"{name}.csv")
    # 경기별 예측 (M1 + M2), 5묶음 교차
    fold = G["matchId"].map(lambda s: zlib.crc32(("m2" + s).encode()) % 5).to_numpy()
    G["m2_pred"] = 1 / (1 + np.exp(-(off + X @ b)))
    G["m2_pred_oof"] = np.nan
    for k in range(5):
        bk = fit_ridge(X[fold != k], y[fold != k], off[fold != k], lam, b0=b)
        G.loc[fold == k, "m2_pred_oof"] = 1 / (1 + np.exp(-(off[fold == k] + X[fold == k] @ bk)))
    H.save_csv(G[["matchId", "league", "day", "patch", "blue_win", "m1_pred_oof", "m2_pred_oof", "m2_pred"]], OUT / "m2_games.csv")
    pd.to_pickle({"coder": coder, "b": b, "cfg": best}, OUT / "m2_model.pkl")
    ipro = (G["league"] != "SOLO").to_numpy()
    L += ["## 2. 최종 (08-25 까지 전부, 오프셋 = M1 교차 예측)", "",
          f"- 칸: 챔피언 {len(T['ch'])} · 챔피언×패치 {len(T['chp'])} · 시너지 {len(T['syn'])} · 카운터 {len(T['ctr'])} "
          f"(대회 편차 {len(coder.cols_pro)})",
          f"- 구간: 경기 단위 부트스트랩 {a.boot}회 95%. 값 = 50% 근처 승률 %p (로그 오즈 × 25). **굵게** = 구간이 0 을 안 포함",
          f"- 교차 예측 로그 손실 (학습 자료 안, 참고): M1 {scores(y[ipro], G['m1_pred_oof'][ipro])['logloss']:.4f} → "
          f"M1+M2 {scores(y[ipro], G['m2_pred_oof'][ipro])['logloss']:.4f} (대회) · "
          f"M1 {scores(y[~ipro], G['m1_pred_oof'][~ipro])['logloss']:.4f} → M1+M2 {scores(y[~ipro], G['m2_pred_oof'][~ipro])['logloss']:.4f} (솔랭)", ""]
    for kind, title, cols, hdr in (
            ("ch", "챔피언 단독 (속성으로 설명 안 되는 승리 효과)", ["champion"], "| 챔피언 "),
            ("syn", "시너지 (같은 팀 쌍)", ["a", "b"], "| 챔피언 | 같이 "),
            ("ctr", "카운터 (앞 챔피언이 뒤 챔피언 상대로)", ["a", "b"], "| 챔피언 | 상대 ")):
        t = T[kind]
        nsig = int(t["sig"].sum()) if "sig" in t else 0
        L += [f"### {title}", "", f"- 칸 {len(t)}개, 구간이 0 을 안 포함 {nsig}개 ({nsig / max(len(t), 1):.1%})", ""]
        head = hdr + "| 판수 | 대회 판수 | 승률 %p (공통) [95%] | 대회 %p |"
        sep = "|" + "---|" * (len(cols) + 4)
        L += ["**+ 쪽 상위**", "", head, sep] + md_rows(t, cols) + [""]
        L += ["**− 쪽 상위**", "", head, sep] + md_rows(t, cols, asc=True) + [""]
        if kind == "ctr":
            tl = t[t["lane"]]
            L += ["**맞라인만 (같은 포지션), 절댓값 상위**", "", head, sep]
            L += md_rows(tl.assign(abs_=tl["coef"].abs()), cols, by="abs_") + [""]
    L += ["- 쌍 효과는 M1 조합 성격과 챔피언 단독 효과를 뺀 나머지. 판수가 적은 쌍은 0 쪽으로 당겨져 작게 나옴",
          "- 여러 칸을 동시에 보므로 굵은 칸 중 일부(약 5%)는 우연일 수 있음", ""]
    (OUT / "m2_eval.md").write_text("\n".join(L), encoding="utf-8")
    log.info("M2 끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
