"""M5 2단계 보강 — M1 조합 성격 예측을 얼마나 믿을지(w)를 자료로 정하기 (2026-11-08)

배경: E-C 는 M1 예측을 가중치 1 로 깔고 경로가 나머지만 배움 → M1 이 약한 시즌 초(26.08·26.09)에 C(M1 없음)보다 나쁨,
      경기가 쌓인 26.14·26.15 에서는 E-C 가 나음 (m5_value_eval.md 1-1)
방법: 예측 = 통제 + w × M1 + k × 경로. w·k 는 출처(대회·솔랭)마다 학습 기간 마지막 15% 경기로 함께 맞춤 (지금 k 맞추는 방식과 같음)
      → M1 이 약한 시기엔 w 가 0 쪽(C 에 가까움), 강해지면 1 쪽(E-C 에 가까움)
변형: W-M1 (경로 없이 통제 + w × M1) · W-C (C 경로 + w × M1) · W-EC (E-C 경로 + w × M1)
      경로 설정은 m5_value 가 26.14 · 26.15 로 고른 그대로 (m5_value_config.json)
평가: 평가 패치 5개(26.08 · 26.09 · 26.10 · 26.14 · 26.15) 대회 970판. 고르는 기준 = 5개 패치 합 대회 + 솔랭 (2026-11-08 결정:
      두 패치로 고르면 운이 섞임). 플레이오프 안 봄
최종: 고른 변형으로 08-25 까지 전부 → 학습 마지막 15% 로 w·k → m5_value_champion.csv 를 바꿔 씀 (예전 E-C 표는 m5_value_champion_ec.csv)

    python src/pipeline/m5_value_w.py [--boot 50]
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m2_synergy as M2  # noqa: E402
import m5_value as MV  # noqa: E402

OUT, log = MV.OUT, MV.log
PATCHES = ["26.08", "26.09", "26.10", "26.14", "26.15"]
OLD = {"A": ("A 통제만", ""), "M1": ("M1 성격만", "")}


def fit_wk(path, m1, y, off, isp):
    cols = [m1 * isp, m1 * (1 - isp)] + ([path * isp, path * (1 - isp)] if path is not None else [])
    return M2.fit_logit(sparse.csr_matrix(np.column_stack(cols)), y, off, np.full(len(cols), 1e-3))


def pred_wk(path, m1, off, wk, isp):
    z = off + wk[0] * m1 * isp + wk[1] * m1 * (1 - isp)
    if path is not None:
        z = z + wk[2] * path * isp + wk[3] * path * (1 - isp)
    return M2.sigm(z)


def holdout_split(G):
    """출처마다 마지막 15% 경기 = 맞춤 구간 (m5_value 리허설과 같은 규칙)."""
    A = np.ones(len(G), bool)
    for m_ in ((G["league"] == "SOLO").to_numpy(), (G["league"] != "SOLO").to_numpy()):
        d = np.sort(G.loc[m_, "day"].to_numpy())
        A &= ~(m_ & (G["day"] >= d[int(len(d) * 0.85)]).to_numpy())
    return A


def per_game(y, p):
    q = np.clip(p, 1e-4, 1 - 1e-4)
    return -(y * np.log(q) + (1 - y) * np.log(1 - q))


def main():
    t0 = time.time()
    conf = json.loads((OUT / "m5_value_config.json").read_text(encoding="utf-8"))
    if conf.get("chosen") != "E-C":       # 이 스크립트가 이미 바꿔 쓴 경우 → 예전 설정 보관본
        conf = json.loads((OUT / "m5_value_config_ec.json").read_text(encoding="utf-8"))
    cfgs = conf["cfg"]
    lab = {k: f"lam {cfgs[k]['lam']:g} · 후반 {cfgs[k]['lam_late']:g} · 패치 {'O' if cfgs[k]['chp'] else 'X'}" for k in ("C", "EC")}
    OLD["C"], OLD["EC"] = (MV.NAMES["C"], lab["C"]), (MV.NAMES["EC"], lab["EC"])
    LOSS = pd.read_pickle(OUT / "m5_value_losses.pkl")
    G, S = MV.load_cached()
    players = MV.load_players()
    rows, L = [], {}
    for p in PATCHES:
        d = MV.patch_split(G, S, players, p)
        Ga, Sa, Gh, Sh, Gtr, Str, Gte, Ste = (d[k] for k in ("Ga", "Sa", "Gh", "Sh", "Gtr", "Str", "Gte", "Ste"))
        yh, yte = Gh["blue_win"].to_numpy(float), Gte["blue_win"].to_numpy(float)
        ph, pte = (Gh["league"] != "SOLO").to_numpy(float), (Gte["league"] != "SOLO").to_numpy(float)
        ip = pte == 1
        offh, offte = MV.base_off(Gh, MV.fit_base(Ga)), MV.base_off(Gte, MV.fit_base(Gtr))
        mh, mte = Gh["m1"].to_numpy(), Gte["m1"].to_numpy()

        def rec(name, pr, wk):
            per = per_game(yte, pr)
            L[(p, name)] = (per[ip], per[~ip])
            rows.append({"patch": p, "variant": name, "n_pro": int(ip.sum()), "ll_pro": per[ip].mean(), "ll_solo": per[~ip].mean(),
                         "w_pro": wk[0], "w_solo": wk[1], "k_pro": wk[2] if len(wk) > 2 else np.nan, "k_solo": wk[3] if len(wk) > 2 else np.nan})
        # 맞춤 점검: 통제만 다시 계산 = 저장된 A 와 같아야 (평가 경기 순서가 같은지)
        a_old = LOSS[(p, *OLD["A"])][0]
        a_new = per_game(yte, MV.M2.sigm(offte))[ip]
        assert len(a_old) == len(a_new) and np.allclose(a_old, a_new, atol=1e-6), f"{p} 평가 경기 순서가 다름"
        wk = fit_wk(None, mh, yh, offh, ph)
        rec("W-M1 통제 + w × M1", pred_wk(None, mte, offte, wk, pte), wk)
        for kind, use_m1 in (("C", False), ("EC", True)):
            c = cfgs[kind]
            Da, Dtr = MV.Design(Ga, Sa, c["chp"], use_m1=use_m1), MV.Design(Gtr, Str, c["chp"], use_m1=use_m1)
            Dh, Dte = MV.Design(Gh, Sh, c["chp"], Da.coder, use_m1=use_m1), MV.Design(Gte, Ste, c["chp"], Dtr.coder, use_m1=use_m1)
            ma, mf = MV.fit_path(Ga, Da, c["lam"], c["lam_late"]), MV.fit_path(Gtr, Dtr, c["lam"], c["lam_late"])
            path_h, path_te = Dh.vdiff(ma["V"]), Dte.vdiff(mf["V"])
            wk = fit_wk(path_h, mh, yh, offh, ph)
            rec(f"W-{kind} {'E-C' if kind == 'EC' else 'C'} 경로 + w × M1", pred_wk(path_te, mte, offte, wk, pte), wk)
        log.info("  %s 끝 (%.0f초)", p, time.time() - t0)
    R = pd.DataFrame(rows)
    MV.H.save_csv(R, OUT / "m5_value_w_selection.csv")

    # 5개 패치 합 + 짝 비교
    names = {"A 통제만": OLD["A"], "M1 성격만": OLD["M1"], "C 시간대 경로 (지금 비교 기준)": OLD["C"], "E-C (지금 시험판)": OLD["EC"]}
    pro = {k: np.concatenate([LOSS[(p, *v)][0] for p in PATCHES]) for k, v in names.items()}
    solo = {k: np.concatenate([LOSS[(p, *v)][1] for p in PATCHES]) for k, v in names.items()}
    new = sorted(R["variant"].unique())
    for v in new:
        pro[v] = np.concatenate([L[(p, v)][0] for p in PATCHES])
        solo[v] = np.concatenate([L[(p, v)][1] for p in PATCHES])
    # 후보 = C(M1 없음, w 0 고정) · W-C · W-EC. 같은 기준(5개 패치 합 대회 + 솔랭)
    cands = [v for v in new if not v.startswith("W-M1")] + ["C 시간대 경로 (지금 비교 기준)"]
    score = {v: pro[v].mean() + solo[v].mean() for v in cands}
    pick = min(score, key=score.get)
    kind = "EC" if pick.startswith("W-EC") else "C"
    no_m1 = not pick.startswith("W-")
    rng = np.random.default_rng(0)
    n = len(pro["A 통제만"])
    idx = rng.integers(0, n, (2000, n))

    def ci(a, b):
        dd = pro[a] - pro[b]
        lo, hi = np.percentile(dd[idx].mean(1), [2.5, 97.5])
        return f"{dd.mean():+.4f} [{lo:+.4f}, {hi:+.4f}]"

    # 최종: 08-25 까지 전부, 마지막 15% 로 w·k
    c = cfgs[kind]
    use_m1 = kind == "EC"
    A = holdout_split(G)
    Ga, Sa, Gh, Sh = G[A].reset_index(drop=True), S[A].reset_index(drop=True), G[~A].reset_index(drop=True), S[~A].reset_index(drop=True)
    Da = MV.Design(Ga, Sa, c["chp"], use_m1=use_m1)
    Dh = MV.Design(Gh, Sh, c["chp"], Da.coder, use_m1=use_m1)
    ma = MV.fit_path(Ga, Da, c["lam"], c["lam_late"])
    ph = (Gh["league"] != "SOLO").to_numpy(float)
    yh, offh, path_h = Gh["blue_win"].to_numpy(float), MV.base_off(Gh, MV.fit_base(Ga)), Dh.vdiff(ma["V"])
    wk_free = fit_wk(path_h, Gh["m1"].to_numpy(), yh, offh, ph)          # 참고: 제약 없는 w
    if no_m1 or wk_free[0] < 0:     # w 는 0~1 로 봄 (음수 = M1 조합 성격을 거꾸로 쓰는 것 → 해석 안 됨) → w 0 (M1 없음)
        k2 = MV.fit_k(path_h, yh, offh, ph)
        wk = np.array([0.0, 0.0, k2[0], k2[1]])
    else:
        wk = np.minimum(wk_free, [1.0, 1.0, np.inf, np.inf])
    log.info("고른 변형 %s · 최종 w 대회 %.2f · k 대회 %.2f (%.0f초)", pick, wk[0], wk[2], time.time() - t0)
    boot = int(sys.argv[sys.argv.index("--boot") + 1]) if "--boot" in sys.argv else 50
    T, m, D = MV.champion_table(G, S, c, MV.M2.ko_names(), float(wk[2]), boot, w_pro=float(wk[0]), use_m1=use_m1)
    f_ch = OUT / "m5_value_champion.csv"
    if not (OUT / "m5_value_champion_ec.csv").exists():
        shutil.copy(f_ch, OUT / "m5_value_champion_ec.csv")
        shutil.copy(OUT / "m5_value_config.json", OUT / "m5_value_config_ec.json")
    MV.H.save_csv(T, f_ch)
    (OUT / "m5_value_config.json").write_text(json.dumps(
        {"chosen": pick, "kind": kind, "w_free_pro": float(wk_free[0]), "cfg": cfgs, "w_pro": float(wk[0]), "w_solo": float(wk[1]), "k_pro": float(wk[2]), "k_solo": float(wk[3]),
         "K_END": MV.K_END, "conversion": {f"{s}_{mm}": v for (s, mm), v in m["conv"].items()}}, ensure_ascii=False, indent=1), encoding="utf-8")

    # 보고서
    O = ["# M5 2단계 보강 — M1 을 얼마나 믿을지(w) 자료로 정하기", "",
         "- 예측 = 통제 + **w × M1 조합 성격 예측** + k × 시간대 경로. w·k 는 출처마다 학습 기간 마지막 15% 경기로 함께 맞춤 (평가 경기 안 봄)",
         f"- 경로 설정은 m5_value 가 26.14 · 26.15 로 고른 그대로: C {lab['C']} · E-C {lab['EC']}",
         "- 고르는 기준 = 평가 패치 5개 합 대회 + 솔랭 로그 손실 (두 패치로 고르면 운이 섞임 — 2026-11-08). 플레이오프 안 봄", "",
         f"## 1. 평가 패치 5개 합 (대회 {n}판 · 솔랭 {len(solo['A 통제만'])}판)", "",
         "| 변형 | 대회 | 솔랭 | 대회 차이 − E-C [95%] | 대회 차이 − C [95%] |", "|---|---|---|---|---|"]
    for v in list(names) + new:
        mk = "**" if v == pick else ""
        O.append(f"| {mk}{v}{mk} | {pro[v].mean():.4f} | {solo[v].mean():.4f} | {ci(v, 'E-C (지금 시험판)')} | {ci(v, 'C 시간대 경로 (지금 비교 기준)')} |")
    O += ["", "## 2. 패치별 (대회 로그 손실 · 학습 마지막 15% 로 맞춘 대회 w · k)", "",
          "| 패치 | 대회 경기 | 통제만 | C | E-C | W-M1 | W-C | W-EC | w (W-C) | w (W-EC) | k (W-C) |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in PATCHES:
        r = R[R["patch"] == p].set_index("variant")
        old = {k: LOSS[(p, *OLD[k])][0].mean() for k in ("A", "C", "EC")}
        wc, we, wm = (r.loc[[v for v in r.index if v.startswith(x)][0]] for x in ("W-C", "W-EC", "W-M1"))
        O.append(f"| {p} | {int(wc['n_pro'])} | {old['A']:.4f} | {old['C']:.4f} | {old['EC']:.4f} | {wm['ll_pro']:.4f} | {wc['ll_pro']:.4f} | {we['ll_pro']:.4f} "
                 f"| {wc['w_pro']:.2f} | {we['w_pro']:.2f} | {wc['k_pro']:.2f} |")
    O += ["", f"- **채택: {pick}** (후보 C · W-C · W-EC 중 5개 패치 합 대회 + 솔랭 최소). 최종(08-25 까지 전부, 마지막 15% 로 맞춤) "
          f"w 대회 {wk[0]:.2f} · 솔랭 {wk[1]:.2f}, k 대회 {wk[2]:.2f} · 솔랭 {wk[3]:.2f} (제약 없이 맞춘 w 대회 {wk_free[0]:.2f} · 솔랭 {wk_free[1]:.2f}, 참고)",
          "- 리허설 w 가 다섯 패치 모두 0 근처(−0.43 ~ 0.22) → 시간대 경로 위에 M1 조합 성격 예측이 더해 주는 게 거의 없음",
          f"- 최종 표 value_pro = w × 조합 성격 몫(M1 챔피언 합 근사) + k × 경로 몫 → m5_value_champion.csv (예전 E-C 표 = m5_value_champion_ec.csv)",
          "- 최종 w 는 M1 전체 교차 예측(m1_pred_oof)으로 맞춤 → 리허설 w 보다 조금 낙관적일 수 있음", "",
          "## 3. 최종 표 상위 · 하위 (대회 %p, 판수 200 이상)", "",
          "| 챔피언 | 판수 (대회) | 조합 성격 × w | 경로 × k | 합 [95% 구간] |", "|---|---|---|---|---|"]
    big = T[T["n"] >= 200]
    for r in pd.concat([big.head(10), big.tail(5)]).itertuples():
        mk = "**" if r.sig else ""
        O.append(f"| {r.a} | {r.n:,} ({r.n_pro}) | {r.m1_part_pro:+.1f} | {r.path_pro:+.1f} | {mk}{r.value_pro:+.1f}{mk} [{r.lo:+.1f}, {r.hi:+.1f}] |")
    O.append(f"\n- 구간이 0 을 안 걸침: 판수 200 이상 {len(big)}개 중 {int(big['sig'].sum())}개")
    (OUT / "m5_value_w.md").write_text("\n".join(O) + "\n", encoding="utf-8")
    print("\n".join(O))
    log.info("끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
