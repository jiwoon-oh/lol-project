"""M4 P5 — 경기 전 포지션별 예측 (분석관용 평가 + 팀 × 포지션 티어표) (2026-11-07)

드래프트 상태 없이 경기 전에 알 수 있는 정보만으로 "이 팀의 이 포지션 선수가 무엇을 고를까" 를 포지션별로 순위 매김.
  단위 = (경기, 팀, 포지션) — 정답 = 그 포지션에서 실제로 고른 챔피언. 후보 = 전체 챔피언 (그 매치 앞 세트 픽은 피어리스로 제외)
  특징 (그 경기 전날까지): 포지션 픽 비율(5리그 최근 21일), 메타 등장, 팀원 메타 점수(meta_score·meta_lck),
       그 포지션 선수의 풀 비율(대회 ×2 + 솔랭 30일), 대회 판수, 최근성
  모델 = 조건부 로짓 (LCK 만). 비교 = 포지션 메타 순위 / 선수 풀 순위
평가 (리허설 26.14 · 26.15 · RTM, 그 세트 첫날 이전으로 학습): 포지션별 상위 3·5 안에 실제 픽이 있나
티어표: 08-25 까지 전부로 학습 → 08-26 기준(정규시즌까지 정보), 각 팀의 08-25 이전 3주간 주 라인업으로 팀 × 포지션 상위 8
  (플레이오프 경기 자료는 쓰지 않음)

    python src/pipeline/m4_p5.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_features as F  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v1 as V  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = B.OUT
POS = M1.POS
FEATS = ["role_share", "meta", "tm_score", "tm_lck", "tm_missing", "pool_share", "pl_pro", "pl_recent"]
STRONG = ["T1", "Gen.G", "Hanwha Life Esports", "Dplus Kia", "KT Rolster"]
KO_POS = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서폿"}


def unit_rows(units, ds_cache, ex_cache, champs):
    """units: [(키, 날짜, 팀, 포지션, 선수, 정답 cid, 피어리스 제외 집합)] → [(키, 후보, 정답 표시, X)]."""
    out = []
    for key, D, team, ro, p, true, gone in units:
        ds = ds_cache[D]
        _, pool_share, _ = ex_cache[D]
        cands = [c for c in champs if c not in gone]
        X = np.zeros((len(cands), len(FEATS)), np.float32)
        for i, c in enumerate(cands):
            ts = ds.tm_score.get(c, np.nan)
            miss = float(isinstance(ts, float) and np.isnan(ts))
            tl = ds.tm_lck.get(c, np.nan)
            X[i] = [np.log((ds.role_share[ro].get(c, 0) + 0.1) / ds.role_n[ro]), ds.meta[c],
                    0.0 if miss else float(ts), 0.0 if (isinstance(tl, float) and np.isnan(tl)) else float(tl), miss,
                    np.log(pool_share.get((p, c), 0) + 0.005) if p else np.log(0.005),
                    np.log1p(ds.pl_pro.get((p, c), 0)) if p else 0.0, ds.recency(p, c) if p else 0.0]
        y = np.array([int(c == true) for c in cands], np.int8)
        out.append((key, cands, y, X))
    return out


def arrays(rr):
    sid = np.concatenate([np.full(len(r[1]), i) for i, r in enumerate(rr)])
    y = np.concatenate([r[2] for r in rr]).astype(float)
    X = np.vstack([r[3] for r in rr]).astype(float)
    return sid, y, X


def main():
    t0 = time.time()
    a, pro_pick, pro_ban, lineup, sq, champs = B.load()
    cid = B.cid_map()
    ev = F.pro_events(cid)
    tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
    picks = a[(a["action"] == "pick") & a["role"].notna() & a["cid"].notna()]
    days = sorted(picks["day"].unique()) + ["2026-08-26"]
    ds_cache = {D: F.DayStats(D, ev, pro_pick, sq, tmeta, champs) for D in days}
    ex_cache = {D: V.day_extras(D, pro_pick, sq) for D in days}
    log.info("날짜별 통계 (%.0f초)", time.time() - t0)
    units = []
    for r in picks.itertuples():
        earlier = a[(a["MatchId"] == r.MatchId) & (a["N_GameInMatch"] < r.N_GameInMatch) & (a["action"] == "pick")]["cid"]
        p = F._player(lineup, r.RiotPlatformGameId, r.team, r.role)
        units.append(((r.GameId, r.team, r.role), r.day, r.team, r.role, p, r.cid, set(earlier)))
    U = unit_rows(units, ds_cache, ex_cache, champs)
    log.info("단위 %d (%.0f초)", len(U), time.time() - t0)
    sets = B.eval_sets(a)
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    res = []
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        tr = [u for u in U if gday[u[0][0]] < start]
        te = [u for u in U if u[0][0] in gids]
        sid, y, X = arrays(tr)
        mu, sd = X.mean(0), X.std(0) + 1e-9
        b = MM.fit_off(sid, y, (X - mu) / sd, np.zeros(len(y)), 1.0)
        for u in te:
            Z = (u[3] - mu) / sd
            sc = {"모델 (경기 전 포지션)": Z @ b, "포지션 메타 순위": u[3][:, 0], "선수 풀 순위": u[3][:, 5] + 1e-3 * u[3][:, 0]}
            t = int(np.argmax(u[2]))
            for m, s in sc.items():
                rank = 1 + (s > s[t]).sum() + 0.5 * ((s == s[t]).sum() - 1)
                res.append({"set": name, "method": m, "team": u[0][1], "role": u[0][2], "rank": rank})
        log.info("  %s 끝", name)
    R = pd.DataFrame(res)
    H.save_csv(R, OUT / "m4_p5_ranks.csv")

    # 최종 경기 전 포지션 모델 (08-25 까지 전부) + 티어표
    sid, y, X = arrays(U)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    b = MM.fit_off(sid, y, (X - mu) / sd, np.zeros(len(y)), 1.0)
    # 각 팀 주 라인업: 08-05 ~ 08-25 LCK 경기에서 포지션마다 가장 많이 나온 선수
    recent = a[(a["action"] == "pick") & (a["day"] >= "2026-08-05")]
    lineups = Counter()
    for r in recent.drop_duplicates(["GameId", "team", "role"]).itertuples():
        p = F._player(lineup, r.RiotPlatformGameId, r.team, r.role)
        if p:
            lineups[(r.team, r.role, p)] += 1
    main_p = {}
    for (tm, ro, p), n in lineups.items():
        if n > main_p.get((tm, ro), ("", 0))[1]:
            main_p[(tm, ro)] = (p, n)
    ko = pd.read_csv(PROCESSED / "d_champions.csv", encoding="utf-8-sig").set_index("champion_id")["name_ko"].to_dict()
    tier = []
    for (tm, ro), (p, _) in sorted(main_p.items()):
        rr = unit_rows([((None, tm, ro), "2026-08-26", tm, ro, p, None, set())], ds_cache, ex_cache, champs)[0]
        s = ((rr[3] - mu) / sd) @ b
        pr = np.exp(s - s.max())
        pr /= pr.sum()
        for k, i in enumerate(np.argsort(-s)[:8], 1):
            tier.append({"team": tm, "position": ro, "player": p, "rank": k, "champion_id": rr[1][i], "name": ko.get(rr[1][i], rr[1][i]),
                         "prob": float(pr[i])})
    T = pd.DataFrame(tier)
    H.save_csv(T, OUT / "m4_p5_team_position_tier.csv")
    import json
    (OUT / "m4_p5_pregame_model.json").write_text(json.dumps({"features": FEATS, "mu": mu.tolist(), "sd": sd.tolist(), "coef": b.tolist(),
                                                               "note": "경기 전 포지션 모델, LCK 08-25 까지 전부"}, ensure_ascii=False), encoding="utf-8")

    def t(g):
        return f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 3).mean():.1%} · {(g['rank'] <= 5).mean():.1%}"
    L = ["# M4 P5 — 경기 전 포지션별 예측 (리허설)", "",
         "- 단위 = (경기, 팀, 포지션). 드래프트 상태 없이 경기 전 정보만. 값 = 그 포지션 실제 픽이 상위 1 · 3 · 5 안에 든 비율",
         "- 모델 = 조건부 로짓 (포지션 픽 비율 · 메타 · 팀원 메타 · 선수 풀 비율 · 대회 판수 · 최근성), 리허설 세트 첫날 이전 LCK 로 학습", "",
         "| 방법 | 전체 | 강팀 | 나머지 | RTM |", "|---|---|---|---|---|"]
    for m, g in R.groupby("method", sort=False):
        L.append(f"| {m} | {t(g)} | {t(g[g['team'].isin(STRONG)])} | {t(g[~g['team'].isin(STRONG)])} | {t(g[g['set'] == 'RTM'])} |")
    L += ["", "## 포지션별 (모델, 상위 3 · 5)", "", "| 포지션 | 상위 1 · 3 · 5 |", "|---|---|"]
    gm = R[R["method"] == "모델 (경기 전 포지션)"]
    for ro in POS:
        L.append(f"| {KO_POS[ro]} | {t(gm[gm['role'] == ro])} |")
    L += ["", f"- 계수 (08-25 까지 전부, 1 SD 당): " + ", ".join(f"{f} {c:+.2f}" for f, c in zip(FEATS, b)), "",
          "## 플레이오프 참고용 팀 × 포지션 상위 5 (08-25 까지 정보, 08-05 이후 주 라인업)", ""]
    for tm, g in T.groupby("team"):
        L.append(f"**{tm}**")
        L.append("")
        for ro in POS:
            gg = g[(g["position"] == ro) & (g["rank"] <= 5)]
            if len(gg):
                L.append(f"- {KO_POS[ro]} ({gg['player'].iloc[0]}): " + ", ".join(f"{r.name} {r.prob:.0%}" for r in gg.itertuples()))
        L.append("")
    (OUT / "m4_p5_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("P5 끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
