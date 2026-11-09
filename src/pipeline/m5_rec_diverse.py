"""M5 추천 쏠림 줄이기 — 순위 규칙 비교 (2026-11-09)

배경: 이길 가치 95% 구간 반폭(중앙 약 1.9%p)이 챔피언 간 값 차이(표준편차 0.84%p)보다 커서
      점추정 1순위가 잡음에 끌려 같은 챔피언으로 몰림 (학습 밖 1순위 상위 5 비중 39% vs 실제 픽 15%)
규칙 (docs/decisions.md 2026-11-09, 돌리기 전에 정함)
  R0 지금: 굴리기 예상 승률 최대
  R1 채택 후보: 1위와 차이가 두 후보 이길 가치 불확실성 합성 1σ 안 = 동점권 → 동점권 안에서 평소 고를 확률(M4) 순
  (참고) R2 = 2σ 동점권 + 평소 확률 순, R3 = 하한(예상 승률 − 1σ) 순
불확실성 σ_c = (hi − lo) / 3.92 (m5_value_champion.csv 부트스트랩 구간, 챔피언@포지션). 세트별 시점 표에는 구간이 없어 최종 표 폭을 씀 (폭은 주로 판수로 정해짐)
자료: m5_rec_check.py 의 raw_{세트}.pkl (픽 차례마다 후보 전부의 굴리기 예상 승률 · 평소 확률 · 포지션). 학습 밖 301경기
판단: 쏠림이 줄고, 새 1순위 기준 놓친 몫 기울기가 음수로 남으면 채택

    python src/pipeline/m5_rec_diverse.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m5_rec_check as RC  # noqa: E402

OUT = RC.OUT


def load():
    G, T = [], []
    for name in RC.ALL_SETS:
        f = OUT / f"raw_{name}.pkl"
        if f.exists():
            g, t = pd.read_pickle(f)
            G += g
            T += t
    G = sorted(G, key=lambda x: x["gid"])
    return pd.DataFrame(G), T


def pick(cands, sd, rule):
    """cands = [(cid, wp, p_habit, role)] → 고른 cid."""
    wp = np.array([c[1] for c in cands]) * 100
    hb = np.array([c[2] for c in cands])
    s = np.array([sd.get(f"{c[0]}@{c[3]}", MED) for c in cands])
    i0 = int(np.argmax(wp))
    if rule == "R0":
        return cands[i0][0]
    if rule == "R3":
        return cands[int(np.argmax(wp - s))][0]
    z = 1.0 if rule == "R1" else 2.0
    tie = (wp[i0] - wp) <= z * np.sqrt(s[i0] ** 2 + s ** 2)
    idx = np.where(tie)[0]
    return cands[int(idx[np.argmax(hb[idx])])][0]


def main():
    global MED
    val = pd.read_csv(RC.MV.OUT / "m5_value_champion.csv")
    sd = dict(zip(val["key"], (val["hi"] - val["lo"]) / 3.92))
    MED = float(np.nanmedian(list(sd.values())))
    gall, T = load()
    g = gall.dropna(subset=["blue_win", "elo_diff"]).reset_index(drop=True)
    if not T or "cands" not in T[0]:
        raise SystemExit("raw_*.pkl 에 cands 가 없음 — m5_rec_check.py 를 다시 돌리세요")
    gi = g.set_index("gid")
    act_cnt = Counter()
    for r in g.itertuples():
        for s, c, tm, ac, ro in r.acts:
            if ac == "pick":
                act_cnt[c] += 1
    L = ["# M5 추천 쏠림 — 순위 규칙 비교 (학습 밖)", "",
         f"- LCK {len(g)}경기 · 픽 차례 {len(T)}번. 규칙은 돌리기 전에 정함 (decisions 2026-11-09): **R1 = 1σ 동점권 + 평소 확률 순** 이 채택 후보, R2·R3 는 참고",
         f"- σ = 이길 가치 95% 구간 반폭 / 1.96 (최종 표, 중앙 {MED:.2f}%p). 동점권 = 1위와 차이 ≤ z × √(σ_1위² + σ_c²)", "",
         "| 규칙 | 1순위 종류 | 1순위 상위 5 비중 | 가장 많은 1순위 | 실제 픽 = 1순위 | 모델이 보는 비용 (R0 1위 대비 %p) | 놓친 몫 기울기 [95%] | p |",
         "|---|---|---|---|---|---|---|---|"]
    tot = sum(act_cnt.values())
    L_act = f"| (참고) 실제 프로 픽 | {len(act_cnt)} | {100 * sum(v for _, v in act_cnt.most_common(5)) / tot:.1f}% | " + \
            " · ".join(f"{c} {100 * v / tot:.1f}%" for c, v in act_cnt.most_common(3)) + " | — | — | — | — |"
    res = {}
    for rule, lab in (("R0", "R0 지금 (예상 승률 최대)"), ("R1", "**R1 1σ 동점권 + 평소 확률**"), ("R2", "R2 2σ 동점권 + 평소 확률"), ("R3", "R3 하한 순")):
        top, cost, miss_rows, hit = [], [], [], []
        for t in T:
            cands = t["cands"]
            if not cands:
                continue
            c = pick(cands, sd, rule)
            wpd = {x[0]: x[1] for x in cands}
            top.append(c)
            cost.append((max(wpd.values()) - wpd[c]) * 100)
            hit.append(c == t["top"][0][0] if rule == "R0" else None)
            a = t["wp_actual"]
            miss_rows.append((t["gid"], t["is_blue"], (wpd[c] - a) * 100, c))
        cnt = Counter(top)
        m = pd.DataFrame(miss_rows, columns=["gid", "is_blue", "miss", "cid"])
        per = m.groupby(["gid", "is_blue"])["miss"].sum().unstack()
        g2 = gi.join(per.rename(columns={True: "mb", False: "mr"}), how="inner")
        g2["d"] = g2["mb"] - g2["mr"]
        fit = RC.logit_fit(np.column_stack([g2["elo_diff"], g2["d"] / RC.PP]), g2["blue_win"].to_numpy(float))
        ci = fit.conf_int()
        # 실제 픽 = 새 1순위
        acts = {}
        for r in gall.itertuples():
            for s, c_, tm, ac, ro in r.acts:
                acts[(r.gid, s)] = c_
        agree = np.mean([acts[(t["gid"], t["step"])] == c for t, c in zip([t for t in T if t["cands"]], top)])
        n = len(top)
        L.append(f"| {lab} | {len(cnt)} | {100 * sum(v for _, v in cnt.most_common(5)) / n:.1f}% | "
                 + " · ".join(f"{c} {100 * v / n:.1f}%" for c, v in cnt.most_common(3))
                 + f" | {agree:.1%} | {np.mean(cost):.2f} | {fit.params[2]:.2f} [{ci[2][0]:.2f}, {ci[2][1]:.2f}] | {fit.pvalues[2]:.3f} |")
        res[rule] = {"conc": sum(v for _, v in cnt.most_common(5)) / n, "slope": fit.params[2], "ties": None}
    L.append(L_act)
    # 동점권 크기
    sizes = []
    for t in T:
        c = t["cands"]
        if not c:
            continue
        wp = np.array([x[1] for x in c]) * 100
        s = np.array([sd.get(f"{x[0]}@{x[3]}", MED) for x in c])
        i0 = int(np.argmax(wp))
        sizes.append(int(((wp[i0] - wp) <= np.sqrt(s[i0] ** 2 + s ** 2)).sum()))
    sizes = np.array(sizes)
    ok = res["R1"]["conc"] < res["R0"]["conc"] and res["R1"]["slope"] < 0
    L += ["", f"- R1 동점권 크기: 평균 {sizes.mean():.1f}개 · 1개(혼자 1위) {np.mean(sizes == 1):.0%} · 3개 이상 {np.mean(sizes >= 3):.0%} (후보 평균 {np.mean([len(t['cands']) for t in T if t['cands']]):.1f}개)",
          "- 놓친 몫 = 그 규칙의 1순위 예상 승률 − 실제 픽, 경기마다 팀 합 → 블루 승 ~ Elo + (블루 − 레드)/25. 음수 = 그 1순위에서 멀수록 실제로 더 짐",
          "- 모델이 보는 비용 = R0 1위 대비 그 규칙 1순위의 굴리기 예상 승률 손해 (모델 안 숫자, 동점권이면 불확실성 안)",
          f"- **판단: {'채택' if ok else '채택 안 함'}** (기준: 상위 5 비중 감소 {res['R0']['conc']:.1%} → {res['R1']['conc']:.1%}, 기울기 음수 {res['R1']['slope']:.2f})"]
    (OUT / "m5_rec_diverse.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
