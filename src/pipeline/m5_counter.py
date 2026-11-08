"""M5 3단계 — 맞라인 상성을 챔피언 속성으로 (2026-11-08)

문제: 지금 상성 표(M2 m2_lane_counter.csv)는 쌍마다 따로 추정 → 판수 15판 이상 쌍만 칸이 있음 (2,436쌍, 쌍 판수 중앙값 솔랭 34 · 대회 1)
      표에 없는 쌍은 시험판에서 0 (예: 애니 vs 라이즈)
방법: 결과 = 경기 × 라인 15분 골드 차 (블루 − 레드, 천 단위, M2 와 같은 라인 행)
  칸 = 라인 챔피언(gm) + 맞대결 쌍(gc) + 바텀 듀오(gd) + 정글 듀오(gs)  ← M2 골드 모델 그대로
     + **속성 상호작용 (라인마다 따로)**: 블루 a · 레드 b 의 속성 축 i, j 에 대해 a_i·b_j − b_i·a_j (반대칭 → 상성의 방향)
       → 쌍 칸(gc)은 "속성으로 예측한 상성에서 벗어난 몫" 이 되고 판수가 적으면 0 쪽으로 (= 속성 예측값으로) 당겨짐
     + (시험) 정글 통제(gj): 라인 행마다 양 팀 정글 챔피언 × 그 라인 칸
  상성 값(쌍) = 속성 상호작용 몫 + 쌍 칸, 골드 → 승률 = 1,000 당 로그 오즈 0.456 (M2, M1 오프셋 위에서 맞춘 값) × 25 → %p
비교 (리허설 26.14 · 26.15, 학습 = 그 패치 창 전, 평가 = 그 패치. 속성표도 그 시점 것 m0_attributes_asof_{p}.csv)
  ① 라인 골드 설명력 R² (대회 · 솔랭) ② 승패: 통제 + k × 경기 상성 합 (k 는 출처마다 학습 마지막 15% 경기) ③ 평가 맞대결 중 쌍 칸이 있는 비율
  ④ 반분 안정성 (양쪽 30판 이상 쌍의 상성 값 상관) — 최종 자료
출력: m5/m5_counter_attr.csv (속성 상성 표, 같은 라인 모든 쌍·양방향 — **채택 안 함, 참고**), m5_counter_selection.csv, m5_counter_eval.md
결론 (2026-11-08): 속성 상호작용은 평가 패치 라인 골드를 더 못 맞힘 → 시험판은 M2 상성 표(m2_lane_counter.csv) 그대로, 표에 없는 쌍은 0
풀이: 릿지 정확해 (fit_exact). M2.fit_gauss(L-BFGS)는 이 문제에서 수렴하지 못했음 (아래 fit_exact 설명)
라인 우위 지수(CS·경험치 포함)는 솔랭 타임라인에 CS·경험치가 없어 안 함 (대회만 있음)

    python src/pipeline/m5_counter.py
"""
from __future__ import annotations

import itertools
import json
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m2_synergy as M2  # noqa: E402
import m5_value as MV  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m5"
AXES = ["ax_lane", "ax_range", "ax_poke", "ax_dive", "ax_engage", "ax_frontline", "ax_catch", "ax_protect", "ax_carry",
        "ax_split", "ax_burst", "ax_growth"]
PAIRS = list(itertools.combinations(range(len(AXES)), 2))
LANES = list(M2.LANES)
SLOPE = 0.456            # 15분 골드 1,000 → 승리 로그 오즈 (M2 2차)
PP = 25.0
BASE = {"gm": 100.0, "gc": 300.0, "gd": 300.0, "gs": 1000.0}     # M2 3차 고른 설정
GRID = {"lam_ax": [100.0, 1000.0, 10000.0, 100000.0], "lam_gc": [300.0, M2.BIG]}
JUNGLE = [False]   # 정글 통제는 1차에서 효과 없음 (R² 대회 0.063 < M2 0.074)
M2.NEED["gj"] = M2.MIN_PAIR


# ---------------------------------------------------------------- 자료
def lane_gold():
    f = OUT / "lane_gold.pkl"
    if f.exists():
        return pd.read_pickle(f)
    gd = M2.lane_gold(pd.concat([M2.solo_gold(), M2.pro_gold()], ignore_index=True))
    pd.to_pickle(gd, f)
    return gd


def attr_map(path) -> dict:
    at = pd.read_csv(path, low_memory=False, dtype={"patch": str})
    at = at[at["patch"] == "ALL"]
    z = ((at[AXES] - 50) / 25).fillna(0.0).to_numpy()
    return {f"{c}@{p}": z[i] for i, (c, p) in enumerate(zip(at["champion_id"], at["position"]))}


def inter(a, b):
    return np.array([a[i] * b[j] - b[i] * a[j] for i, j in PAIRS])


ZERO = np.zeros(len(AXES))


class Lanes:
    """M2 GoldData (라인 행) + 속성 상호작용 칸 + 정글 통제 칸."""

    def __init__(self, G, S, gd, amap, jungle=False):
        self.d = M2.GoldData(G, S, gd)
        rows = S.to_numpy()
        k = len(PAIRS)
        A = np.zeros((len(self.d.y), k * len(LANES)))
        T = []
        for r, (gi, L, t) in enumerate(zip(self.d.gi, self.d.lane, self.d.T)):
            b, rr = dict(zip(M2.POS, rows[gi][:5])), dict(zip(M2.POS, rows[gi][5:]))
            li = LANES.index(L)
            for p in M2.LANES[L]:
                for q in M2.LANES[L]:
                    A[r, li * k:(li + 1) * k] += inter(amap.get(b[p], ZERO), amap.get(rr[q], ZERO))
            t = list(t)
            if jungle and L != "JUNGLE":
                t += [("gj", f"{b['JUNGLE']}|{L}", 1.0), ("gj", f"{rr['JUNGLE']}|{L}", -1.0)]
            T.append(t)
        self.A, self.T = A, T

    def X(self, coder, use_attr):
        parts = [coder.matrix(self.T, self.d.is_pro)]
        if use_attr:
            parts.append(sparse.csr_matrix(self.A))
        parts.append(self.d.C)
        return sparse.hstack(parts).tocsr()


def fit_exact(X, y, lam):
    """릿지 정확해 (X'X + diag(λ)) b = X'y. 성긴 챔피언 칸 + 빽빽한 속성 칸이 섞이면 M2.fit_gauss(L-BFGS 1000회)가
    수렴하지 못함 (1차 진단: 속성 칸을 더했는데 학습 R² 가 0.087 → 0.057 로 떨어짐). 칸 수가 수천이라 직접 풀 수 있음."""
    from scipy.linalg import solve
    XtX = (X.T @ X).toarray()
    XtX[np.diag_indices_from(XtX)] += lam
    return solve(XtX, X.T @ y, assume_a="pos")


class CounterModel:
    def __init__(self, lam_ax=None, lam_gc=BASE["gc"], lam_gj=None):
        self.lam_ax, self.lam_gj = lam_ax, lam_gj
        self.lam = dict(BASE, gc=lam_gc, gj=lam_gj or M2.BIG)

    def fit(self, D: Lanes):
        self.coder = M2.Coder(D.T, D.d.is_pro)
        use = self.lam_ax is not None
        pen = [self.coder.penalty(self.lam)] + ([np.full(D.A.shape[1], self.lam_ax)] if use else []) + [np.full(D.d.C.shape[1], 1e-3)]
        self.b = fit_exact(D.X(self.coder, use), D.d.y, np.concatenate(pen))
        w = self.coder.width
        self.wA = self.b[w:w + D.A.shape[1]] if use else np.zeros(D.A.shape[1])
        return self

    def predict(self, D):
        return D.X(self.coder, self.lam_ax is not None) @ self.b

    def counter_part(self, D):
        """라인 행마다 상성 몫 (속성 상호작용 + 맞대결 쌍 칸), 천 골드."""
        X = self.coder.matrix(D.T, D.d.is_pro)
        return D.A @ self.wA + X @ (self.b[:self.coder.width] * self.coder.mask({"gc"}))

    def game_counter(self, D, n_games):
        return np.bincount(D.d.gi, weights=self.counter_part(D), minlength=n_games)


# ---------------------------------------------------------------- 리허설
def split(G, S, p):
    w = H.research_windows().set_index("patch")
    cut = w.loc[p, "window_from"]
    te = (G["patch"] == p).to_numpy()
    tr = ((G["day"] < cut) & ~te).to_numpy()
    Gtr, Str, Gte, Ste = G[tr].reset_index(drop=True), S[tr].reset_index(drop=True), G[te].reset_index(drop=True), S[te].reset_index(drop=True)
    A = np.ones(len(Gtr), bool)
    for m_ in ((Gtr["league"] == "SOLO").to_numpy(), (Gtr["league"] != "SOLO").to_numpy()):
        d = np.sort(Gtr.loc[m_, "day"].to_numpy())
        A &= ~(m_ & (Gtr["day"] >= d[int(len(d) * 0.85)]).to_numpy())
    return Gtr, Str, Gte, Ste, A


def r2(y, p):
    return float(1 - np.sum((y - p) ** 2) / np.sum((y - y.mean()) ** 2))


def rehearsal(G, S, gd):
    sel, t0 = [], time.time()
    for p in ("26.14", "26.15"):
        amap = attr_map(MV.M1.M0 / f"m0_attributes_asof_{p}.csv")
        Gtr, Str, Gte, Ste, A = split(G, S, p)
        Ga, Sa, Gh, Sh = Gtr[A].reset_index(drop=True), Str[A].reset_index(drop=True), Gtr[~A].reset_index(drop=True), Str[~A].reset_index(drop=True)
        yh, yte = Gh["blue_win"].to_numpy(float), Gte["blue_win"].to_numpy(float)
        ph, pte = (Gh["league"] != "SOLO").to_numpy(float), (Gte["league"] != "SOLO").to_numpy(float)
        offh, offte = MV.base_off(Gh, MV.fit_base(Ga)), MV.base_off(Gte, MV.fit_base(Gtr))
        for jg in JUNGLE:
            Dtr, Dte, Da, Dh = (Lanes(g, s, gd, amap, jg) for g, s in ((Gtr, Str), (Gte, Ste), (Ga, Sa), (Gh, Sh)))
            cfgs = [("M2 그대로", None, BASE["gc"]), ("쌍 칸 없음 (라인 챔피언 + 듀오만)", None, M2.BIG)] if not jg else []
            cfgs += [("속성 상호작용", la, lg) for la, lg in itertools.product(GRID["lam_ax"], GRID["lam_gc"])]
            if jg:
                cfgs = [(n_, la, lg) for n_, la, lg in cfgs if lg in (1000.0, 3000.0)]
            for name, la, lg in cfgs:
                for lgj in ([None] if not jg else [300.0, 1000.0]):
                    mf = CounterModel(la, lg, lgj).fit(Dtr)
                    ma = CounterModel(la, lg, lgj).fit(Da)
                    pr = mf.predict(Dte)
                    ip = Dte.d.is_pro
                    k = MV.fit_k(ma.game_counter(Dh, len(Gh)) * SLOPE, yh, offh, ph)
                    pw = MV.predict(mf.game_counter(Dte, len(Gte)) * SLOPE, offte, k, pte)
                    gc_keys = {key for (kind, key) in mf.coder.idx if kind == "gc"}
                    te_pairs = [key for t in Dte.T for kind, key, _ in t if kind == "gc"]
                    tag = ("+ 정글 통제 " if jg else "") + name
                    cfg = "" if la is None else f"속성 {la:g} · 쌍 {'∞' if lg >= M2.BIG else f'{lg:g}'}" + (f" · 정글 {lgj:g}" if lgj else "")
                    sel.append({"patch": p, "variant": tag, "cfg": cfg, "r2_pro": r2(Dte.d.y[ip], pr[ip]), "r2_solo": r2(Dte.d.y[~ip], pr[~ip]),
                                "ll_pro": M2.ll(yte[pte == 1], pw[pte == 1]), "ll_solo": M2.ll(yte[pte == 0], pw[pte == 0]),
                                "k_pro": k[0], "coverage": np.mean([q in gc_keys for q in te_pairs])})
            log.info("  %s 정글 %s 끝 (%.0f초)", p, jg, time.time() - t0)
        base = MV.predict(np.zeros(len(Gte)), offte, (0.0, 0.0), pte)
        sel.append({"patch": p, "variant": "통제만 (승패 기준선)", "cfg": "", "r2_pro": np.nan, "r2_solo": np.nan,
                    "ll_pro": M2.ll(yte[pte == 1], base[pte == 1]), "ll_solo": M2.ll(yte[pte == 0], base[pte == 0]), "k_pro": 0.0, "coverage": np.nan})
    return pd.DataFrame(sel)


# ---------------------------------------------------------------- 최종 표
def pair_values(m: CounterModel, amap, keys_by_lane):
    """같은 라인 모든 쌍 (양방향) 의 상성 값 = 속성 몫 + 쌍 칸."""
    k = len(PAIRS)
    rows = []
    for L, keys in keys_by_lane.items():
        li = LANES.index(L)
        w = m.wA[li * k:(li + 1) * k]
        for x in keys:
            for y in keys:
                if x == y or x.split("@")[1] not in M2.LANES[L] or y.split("@")[1] not in M2.LANES[L]:
                    continue
                attr = float(inter(amap.get(x, ZERO), amap.get(y, ZERO)) @ w)
                kind, key, s = M2.anti("gc", x, y)
                j = m.coder.idx.get((kind, key))
                res = s * m.b[j] if j is not None else 0.0
                n = m.coder.n.get((kind, key), 0)
                rows.append({"key": f"{x}>{y}", "a": x, "b": y, "lane": L, "attr_k": attr, "pair_k": res, "gold_k": attr + res,
                             "win_pp": (attr + res) * SLOPE * PP, "n_pair": n, "has_pair": j is not None})
    return pd.DataFrame(rows)


def split_half(G, S, gd, amap, cfg):
    half = G["matchId"].map(lambda s: zlib.crc32(("half" + s).encode()) % 2).to_numpy()
    out = {}
    for name, c in (("M2 그대로", (None, BASE["gc"], None)), ("속성 상호작용 (가장 나은 속성 설정, 채택 안 함)", cfg)):
        vals = []
        for h in (0, 1):
            g, s = G[half == h].reset_index(drop=True), S[half == h].reset_index(drop=True)
            D = Lanes(g, s, gd, amap, c[2] is not None)
            m = CounterModel(*c).fit(D)
            v = {}
            for (kind, key), j in m.coder.idx.items():
                if kind != "gc":
                    continue
                x, y = key.split(">")
                L = M2.LANE_OF[x.split("@")[1]]
                li = LANES.index(L)
                attr = float(inter(amap.get(x, ZERO), amap.get(y, ZERO)) @ m.wA[li * len(PAIRS):(li + 1) * len(PAIRS)])
                v[key] = (attr + m.b[j], m.coder.n.get((kind, key), 0))
            vals.append(v)
        ks = [k for k in vals[0] if k in vals[1] and min(vals[0][k][1], vals[1][k][1]) >= 30]
        out[name] = (len(ks), float(np.corrcoef([vals[0][k][0] for k in ks], [vals[1][k][0] for k in ks])[0, 1]))
    return out


def main():
    t0 = time.time()
    G, S = MV.load_cached()
    gd = lane_gold()
    log.info("자료 (%.0f초)", time.time() - t0)
    sel = rehearsal(G, S, gd)
    H.save_csv(sel, OUT / "m5_counter_selection.csv")
    avg = sel.groupby(["variant", "cfg"], sort=False)[["r2_pro", "r2_solo", "ll_pro", "ll_solo", "k_pro", "coverage"]].mean()
    avg["score"] = avg["r2_pro"] / avg["r2_pro"].max() + avg["r2_solo"] / avg["r2_solo"].max()   # M2 와 같이 대회 · 솔랭 R² 를 같이
    cand = avg[avg.index.get_level_values(0).str.contains("속성")]
    best = cand["score"].idxmax()
    p = best[1].split(" · ")
    cfg = (float(p[0].split(" ")[1]), M2.BIG if p[1].split(" ")[1] == "∞" else float(p[1].split(" ")[1]),
           float(p[2].split(" ")[1]) if len(p) > 2 else None)
    log.info("고른 설정 %s %s (%.0f초)", best, cfg, time.time() - t0)
    amap = attr_map(MV.M1.M0 / "m0_attributes.csv")
    stab = split_half(G, S, gd, amap, cfg)
    log.info("안정성 %s", stab)
    D = Lanes(G, S, gd, amap, cfg[2] is not None)
    m = CounterModel(*cfg).fit(D)
    n_ch = pd.Series([k for t in D.T for kind, k, _ in t if kind == "gm"]).value_counts()
    keys_by_lane = {L: sorted(k for k, n in n_ch.items() if n >= 50 * 2 and M2.LANE_OF[k.split("@")[1]] == L) for L in LANES}
    T = pair_values(m, amap, keys_by_lane)
    ko = M2.ko_names()
    T["a_ko"], T["b_ko"] = T["a"].map(lambda k: M2.label(k, ko)), T["b"].map(lambda k: M2.label(k, ko))
    H.save_csv(T, OUT / "m5_counter_attr.csv")
    (OUT / "m5_counter_config.json").write_text(json.dumps({"lam_ax": cfg[0], "lam_gc": cfg[1], "lam_gj": cfg[2], "base": BASE,
                                                            "slope": SLOPE, "axes": AXES}, ensure_ascii=False, indent=1), encoding="utf-8")
    report(sel, avg, best, stab, T, m)
    log.info("끝 (%.0f초)", time.time() - t0)


def report(sel, avg, best, stab, T, m):
    L = ["# M5 3단계 — 맞라인 상성을 챔피언 속성으로", "",
         "- 결과 = 경기 × 라인 15분 골드 차. M2 골드 모델(라인 챔피언 + 맞대결 쌍 + 바텀·정글 듀오)에 **속성 상호작용**(라인마다, 12축 반대칭 66칸)을 더함",
         "- 쌍 칸은 '속성으로 예측한 상성에서 벗어난 몫' → 판수가 적으면 속성 예측값으로 당겨짐. 쌍 ∞ = 쌍 칸 없이 속성만",
         "- 리허설 26.14 · 26.15 (학습 = 그 패치 창 전, 속성표도 그 시점). 승패 = 통제 + k × 경기 상성 합(골드 × 0.456). 플레이오프 안 봄", "",
         "## 1. 리허설 (두 패치 평균)", "",
         "| 변형 | 설정 | 라인 골드 R² 대회 | R² 솔랭 | 승패 로그 손실 대회 | 솔랭 | 평가 맞대결 중 쌍 칸 있음 |", "|---|---|---|---|---|---|---|"]
    rows = [("통제만 (승패 기준선)", ""), ("쌍 칸 없음 (라인 챔피언 + 듀오만)", ""), ("M2 그대로", "")]
    for v in avg.index.get_level_values(0).unique():
        if "속성" in v:
            rows += [(v, c) for c in avg.loc[v].sort_values("score", ascending=False).index[:3]]
    for v, c in rows:
        r = avg.loc[(v, c)]
        mk = "**" if (v, c) == best else ""
        f = lambda x, d=4: "—" if pd.isna(x) else f"{x:.{d}f}"  # noqa: E731
        L.append(f"| {mk}{v}{mk} | {c} | {f(r['r2_pro'])} | {f(r['r2_solo'])} | {f(r['ll_pro'])} | {f(r['ll_solo'])} | "
                 f"{'—' if pd.isna(r['coverage']) else f'{r.coverage:.0%}'} |")
    L += ["", "## 2. 안정성 (경기를 반으로 나눠 두 번, 양쪽 30판 이상 맞대결 쌍의 상성 값 상관)", "", "| 방식 | 쌍 수 | r |", "|---|---|---|"]
    for k, (n, r) in stab.items():
        L.append(f"| {k} | {n} | {r:.2f} |")
    k = len(PAIRS)
    L += ["", "## 3. 라인별 속성 상호작용이 큰 칸 (블루 축 i × 레드 축 j, 천 골드)", ""]
    for li, Ln in enumerate(LANES):
        w = m.wA[li * k:(li + 1) * k]
        top = np.argsort(-np.abs(w))[:4]
        L.append(f"- **{M2.LANE_KO[Ln]}**: " + ", ".join(
            f"우리 {AXES[PAIRS[t][0]][3:]} × 상대 {AXES[PAIRS[t][1]][3:]} {w[t] * 1000:+.0f}골드" for t in top))
    big = T[T["n_pair"] >= 0]
    L += ["", "## 4. 상성 표 (같은 포지션, 각 라인 승률 환산 상위, 판수 = 학습 자료 맞대결 수)", ""]
    for Ln in LANES:
        t = big[(big["lane"] == Ln) & (big["a"].str.split("@").str[1] == big["b"].str.split("@").str[1])].sort_values("win_pp", ascending=False).head(6)
        L += [f"### {M2.LANE_KO[Ln]}", "", "| 챔피언 | 상대 | 15분 골드 | 속성 몫 | 쌍 몫 | 승률 %p | 쌍 판수 |", "|---|---|---|---|---|---|---|"]
        for r in t.itertuples():
            L.append(f"| {r.a_ko} | {r.b_ko} | {r.gold_k * 1000:+.0f} | {r.attr_k * 1000:+.0f} | {r.pair_k * 1000:+.0f} | {r.win_pp:+.1f} | {r.n_pair} |")
        L.append("")
    old = pd.read_csv(PROCESSED / "m2" / "m2_lane_counter.csv")
    old_d = {}
    for r in old.itertuples():
        x, y = r.key.split(">")
        old_d[(x, y)], old_d[(y, x)] = r.win_pp, -r.win_pp
    same = T[T["a"].str.split("@").str[1] == T["b"].str.split("@").str[1]]
    have = same.apply(lambda r: (r["a"], r["b"]) in old_d, axis=1)
    L += [f"- 같은 포지션 쌍 {len(same):,}개 (양방향) 중 예전 표에 있던 것 {have.mean():.0%} → 새 표는 전부 값이 있음", ""]
    chk = [("Annie@MIDDLE", "Ryze@MIDDLE"), ("Seraphine@UTILITY", "Karma@UTILITY"), ("Ashe@BOTTOM", "Ezreal@BOTTOM"),
           ("Caitlyn@BOTTOM", "Lucian@BOTTOM"), ("Vayne@TOP", "KSante@TOP"), ("Gnar@TOP", "KSante@TOP"), ("Syndra@MIDDLE", "Ryze@MIDDLE")]
    L += ["### 시험판에서 나온 맞대결 (예전 → 새, 승률 %p)", "", "| 챔피언 | 상대 | 예전 표 | 새 값 (속성 + 쌍) | 쌍 판수 |", "|---|---|---|---|---|"]
    for x, y in chk:
        t = T[(T["a"] == x) & (T["b"] == y)]
        if len(t):
            r = t.iloc[0]
            o = old_d.get((x, y))
            L.append(f"| {r['a_ko']} | {r['b_ko']} | {'없음' if o is None else f'{o:+.1f}'} | {r['win_pp']:+.1f} ({r['attr_k'] * 1000:+.0f} + {r['pair_k'] * 1000:+.0f}골드) | {r['n_pair']} |")
    (OUT / "m5_counter_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
