"""M5 조합 맥락 시험 — M1 의 비가산 몫이 이길 가치(C 시간대 경로) 위에 더해 주는 게 있나 (2026-11-09)

배경: 이길 가치 = 챔피언 한 장 값의 합 → 조합과 무관하게 같은 챔피언(애쉬·럼블·신드라)이 1순위로 쏠림.
      맞라인 상성·듀오 쌍(M2)은 승패를 오히려 나쁘게 했고, M1 예측 전체를 더하는 w 는 0 근처 (m5_value_w).
      M1 전체에는 '챔피언 한 장 값'과 겹치는 가산 몫이 섞여 있음 → 겹치지 않는 비가산 몫만 따로 시험
맥락 = M1 예측(로그 오즈) − M1 을 챔피언 칸 합 + 통제로 근사한 값 (m5_value.m1_approx, 근사는 학습 자료로만)
      = 딜 쏠림·궁 개수·속성 상호작용처럼 같이 고른 챔피언에 따라 달라지는 부분
예측: 통제 + k × C 경로 + w × 맥락. k·w 는 출처(대회·솔랭)마다 학습 기간 마지막 15% 로 (m5_value_w 와 같은 방식)
평가: 평가 패치 5개(26.08 · 26.09 · 26.10 · 26.14 · 26.15) 대회 경기, 플레이오프 안 봄
기준 (docs/decisions.md 2026-11-09, 돌리기 전에 정함): 5개 패치 합 대회 로그 손실 차 (C + 맥락 − C) 구간 상한 < 0 그리고 최종 w_pro > 0

    python src/pipeline/m5_ctx.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m5_value as MV  # noqa: E402
import m5_value_w as VW  # noqa: E402

OUT, log = MV.OUT, MV.log
PATCHES = VW.PATCHES


def ctx_values(Gfit, Sfit, G, S):
    (apx,), _, _ = MV.m1_approx(Gfit, Sfit, [(G, S)])
    return G["m1"].to_numpy(float) - apx


def main():
    t0 = time.time()
    conf = json.loads((OUT / "m5_value_config.json").read_text(encoding="utf-8"))
    assert conf["kind"] == "C"
    c = conf["cfg"]["C"]
    G, S = MV.load_cached()
    players = MV.load_players()
    rows, P = [], {"C": [], "C+맥락": [], "lck": []}
    for p in PATCHES:
        d = MV.patch_split(G, S, players, p)
        Ga, Sa, Gh, Sh, Gtr, Str, Gte, Ste = (d[k] for k in ("Ga", "Sa", "Gh", "Sh", "Gtr", "Str", "Gte", "Ste"))
        yh, yte = Gh["blue_win"].to_numpy(float), Gte["blue_win"].to_numpy(float)
        ph, pte = (Gh["league"] != "SOLO").to_numpy(float), (Gte["league"] != "SOLO").to_numpy(float)
        ip = pte == 1
        offh, offte = MV.base_off(Gh, MV.fit_base(Ga)), MV.base_off(Gte, MV.fit_base(Gtr))
        Da, Dtr = MV.Design(Ga, Sa, c["chp"]), MV.Design(Gtr, Str, c["chp"])
        Dh, Dte = MV.Design(Gh, Sh, c["chp"], Da.coder), MV.Design(Gte, Ste, c["chp"], Dtr.coder)
        ma, mf = MV.fit_path(Ga, Da, c["lam"], c["lam_late"]), MV.fit_path(Gtr, Dtr, c["lam"], c["lam_late"])
        path_h, path_te = Dh.vdiff(ma["V"]), Dte.vdiff(mf["V"])
        ctx_h, ctx_te = ctx_values(Ga, Sa, Gh, Sh), ctx_values(Gtr, Str, Gte, Ste)
        k = MV.fit_k(path_h, yh, offh, ph)
        p_c = MV.predict(path_te, offte, k, pte)
        wk = VW.fit_wk(path_h, ctx_h, yh, offh, ph)
        p_x = VW.pred_wk(path_te, ctx_te, offte, wk, pte)
        lc, lx = VW.per_game(yte, p_c), VW.per_game(yte, p_x)
        P["C"].append(lc[ip]); P["C+맥락"].append(lx[ip]); P["lck"].append((Gte["league"] == "LCK").to_numpy()[ip])
        rows.append({"patch": p, "n_pro": int(ip.sum()), "ll_C": lc[ip].mean(), "ll_ctx": lx[ip].mean(),
                     "ll_C_solo": lc[~ip].mean(), "ll_ctx_solo": lx[~ip].mean(), "w_pro": wk[0], "w_solo": wk[1], "k_pro": wk[2],
                     "ctx_sd_pro": float(np.std(ctx_te[ip])), "ctx_sd_solo": float(np.std(ctx_te[~ip]))})
        log.info("  %s 끝 (%.0f초) C %.4f · +맥락 %.4f · w %.2f", p, time.time() - t0, lc[ip].mean(), lx[ip].mean(), wk[0])
    R = pd.DataFrame(rows)
    a, b, lck = (np.concatenate(P[x]) for x in ("C", "C+맥락", "lck"))
    dd = b - a
    rng = np.random.default_rng(0)
    idx = rng.integers(0, len(dd), (2000, len(dd)))
    lo, hi = np.percentile(dd[idx].mean(1), [2.5, 97.5])
    dl = dd[lck]
    il = rng.integers(0, len(dl), (2000, len(dl)))
    llo, lhi = np.percentile(dl[il].mean(1), [2.5, 97.5])

    # 최종 w: 08-25 까지 전부, 마지막 15% 로 (m1 은 전체 교차 예측 = 최종 표와 같은 조건)
    A = VW.holdout_split(G)
    Ga, Sa, Gh, Sh = G[A].reset_index(drop=True), S[A].reset_index(drop=True), G[~A].reset_index(drop=True), S[~A].reset_index(drop=True)
    Da = MV.Design(Ga, Sa, c["chp"]); Dh = MV.Design(Gh, Sh, c["chp"], Da.coder)
    ma = MV.fit_path(Ga, Da, c["lam"], c["lam_late"])
    ph = (Gh["league"] != "SOLO").to_numpy(float)
    wk_f = VW.fit_wk(Dh.vdiff(ma["V"]), ctx_values(Ga, Sa, Gh, Sh), Gh["blue_win"].to_numpy(float), MV.base_off(Gh, MV.fit_base(Ga)), ph)
    ok = hi < 0 and wk_f[0] > 0
    L = ["# M5 조합 맥락 시험 — M1 비가산 몫", "",
         "- 맥락 = M1 예측 − 챔피언 칸 합 근사 (같이 고른 챔피언에 따라 달라지는 몫). 예측 = 통제 + k × C 경로 + w × 맥락, k·w 는 학습 마지막 15%",
         "- 기준 (돌리기 전): 5개 패치 합 대회 로그 손실 차 구간 상한 < 0 그리고 최종 w_pro > 0", "",
         "| 패치 | 대회 경기 | C | C + 맥락 | 솔랭 C | 솔랭 + 맥락 | w 대회 | w 솔랭 | 맥락 SD 대회 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in R.itertuples():
        L.append(f"| {r.patch} | {r.n_pro} | {r.ll_C:.4f} | {r.ll_ctx:.4f} | {r.ll_C_solo:.4f} | {r.ll_ctx_solo:.4f} | {r.w_pro:+.2f} | {r.w_solo:+.2f} | {r.ctx_sd_pro:.3f} |")
    L += ["", f"- 5개 패치 합 대회 {len(dd)}판: C {a.mean():.4f} → + 맥락 {b.mean():.4f}, 차 {dd.mean():+.4f} [{lo:+.4f}, {hi:+.4f}]",
          f"- LCK 만 {len(dl)}판: 차 {dl.mean():+.4f} [{llo:+.4f}, {lhi:+.4f}] (참고)",
          f"- 최종 w (08-25 까지, 마지막 15%): 대회 {wk_f[0]:+.2f} · 솔랭 {wk_f[1]:+.2f}",
          f"- **판단: {'채택 — 굴리기 점수에 넣고 쏠림·추천 검증 다시' if ok else '채택 안 함 — 굴리기 점수에 넣지 않음'}**"]
    (OUT / "m5_ctx.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    MV.H.save_csv(R, OUT / "m5_ctx_selection.csv")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
