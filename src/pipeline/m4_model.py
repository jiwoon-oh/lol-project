"""M4 1차 — 공통 모델 + 이길 가치 + 팀별 편차 (2026-11-06, docs/PIPELINE.md "M4 1차")

공통 모델 = m4_v1 에서 고른 설정: 픽 하나의 모델, 밴 1·2페이즈 따로. A2·A3 보강·메타 순위·플레이오프 가중은 뺌
  + 이길 가치 (win_value): 후보를 빈 포지션(밴은 상대 빈 포지션)에 넣었을 때의 승리 몫 (로그 오즈)
    = M2 챔피언 단독 효과(대회 값) + M1 조합 성격 기여 (M0 축 12개 × M1 LCK 계수, 팀 평균이라 ÷5)
    주의: M1·M2·M0 는 08-25 까지 전체로 학습한 값이라 리허설 경기 결과가 조금 섞임 (리허설 성적이 약간 낙관적일 수 있음)
팀별 편차 (설계 2026-11-06): 특징을 4그룹(메타 · 숙련 · 카운터 · 조합)으로 묶어 그룹 점수 = Σ 공통 계수 × 특징,
  팀 t 의 점수 = 공통 점수 + Σ_g δ[t, g] × 그룹 점수 (픽·밴 따로 → 팀당 8개). δ 는 L2 로 0(리그 평균) 쪽으로 당김.
  공통 점수를 고정(오프셋)하고 팀마다 따로 적합 (팀끼리 공유하는 칸이 없어 독립)
규제 세기 λ 는 리허설(LCK 26.14 · 26.15 · RTM)로 고름: 없음 / 100 / 30 / 10 / 3
최종: 08-26 이전 LCK 전부로 학습 → m4/m4_model.json (최종 검증·M5 용), 팀 성향표 (δ, 경기 묶음 부트스트랩 구간)

    python src/pipeline/m4_model.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_features as F  # noqa: E402
import m4_v1 as V  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = B.OUT
DROP = {"pool_rank", "solo_surge", "meta_rank"}
PICK_FEATS = [f for f in F.FEATS_PICK + V.NEW_PICK if f not in DROP] + ["win_value"]
BAN_FEATS = [f for f in F.FEATS_BAN + V.NEW_BAN if f not in DROP] + ["win_value"]
GROUPS = {"메타": ["meta", "meta_lck", "tm_score", "tm_lck", "tm_missing", "solo_meta", "order_fit", "meta_x_game"],
          "숙련": ["pl_pro", "pl_recent", "pl_solo", "pl_top3", "top3_x_game"],
          "카운터": ["counter_resp", "block_counter", "ban_resp", "target", "sub_lift"],
          "조합": ["syn_own", "block_combo"]}
GNAMES = list(GROUPS)
LAMBDAS = [None, 100.0, 30.0, 10.0, 3.0]
SEGMENTS = {"pick": [(7, 20)], "ban": [(1, 6), (13, 16)]}


# ---------------------------------------------------------------- 이길 가치
def win_value_table() -> dict:
    ch = pd.read_csv(PROCESSED / "m2" / "m2_champion.csv")
    wv = {}
    for r in ch.itertuples():
        c, p = r.key.split("@")
        wv[(c, p)] = r.val_pro / 25.0  # %p → 로그 오즈 (50% 근처)
    co = pd.read_csv(PROCESSED / "m1" / "m1_coefs.csv")
    co = co[co["level"] == "LCK"].set_index("feature")
    at = pd.read_csv(PROCESSED / "m0" / "m0_attributes.csv", low_memory=False, dtype={"patch": str})
    at = at[at["patch"] == "ALL"]
    for r in at.itertuples(index=False):
        d = r._asdict()
        v = 0.0
        for f, col in M1.AX.items():
            if f in co.index and pd.notna(d.get(col)):
                v += co.loc[f, "coef"] * ((d[col] - 50) / 5) / co.loc[f, "sd_unit"]
        k = (d["champion_id"], d["position"])
        wv[k] = wv.get(k, 0.0) + v
    return wv


# ---------------------------------------------------------------- 적합
def fit_off(sid, y, X, off, lam, w=None):
    """조건부 로짓 (오프셋, L2). lam 은 칸마다 같은 값."""
    _, grp = np.unique(sid, return_inverse=True)
    G = grp.max() + 1
    w = np.ones(len(y)) if w is None else w
    wg = np.bincount(grp, w * y, G)

    def f(b):
        z = off + X @ b
        zm = np.zeros(G)
        np.maximum.at(zm, grp, z)
        e = np.exp(z - zm[grp])
        s = np.bincount(grp, e, G)
        p = e / s[grp]
        ll = np.sum(w * y * (z - zm[grp] - np.log(s[grp])))
        g = X.T @ (w * y - wg[grp] * p)
        return -ll + 0.5 * lam * b @ b, -g + lam * b
    return minimize(f, np.zeros(X.shape[1]), jac=True, method="L-BFGS-B").x


def seg_rows(rows, action, lo, hi):
    return [r for r in rows if r[2] == action and lo <= r[5] <= hi]


class Common:
    """공통 모델 (구간별). 점수와 그룹 점수를 돌려줌."""

    def __init__(self, rows, action):
        self.action = action
        self.feats = list(PICK_FEATS if action == "pick" else BAN_FEATS)
        self.parts = []
        for lo, hi in SEGMENTS[action]:
            sid, y, X, _ = V.arrays(seg_rows(rows, action, lo, hi), self.feats)
            mu, sd = X.mean(0), X.std(0) + 1e-9
            b = fit_off(sid, y, (X - mu) / sd, np.zeros(len(y)), 1.0)
            self.parts.append((lo, hi, mu, sd, b))

    def score(self, rows):
        """rows (한 구간 섞여도 됨) → sid, y, 공통 점수, 그룹 점수(n×4), 팀"""
        out = []
        for lo, hi, mu, sd, b in self.parts:
            rr = seg_rows(rows, self.action, lo, hi)
            if not rr:
                continue
            sid, y, X, _ = V.arrays(rr, self.feats)
            Z = (X - mu) / sd
            s = Z @ b
            Gs = np.column_stack([Z[:, [self.feats.index(f) for f in GROUPS[g] if f in self.feats]]
                                  @ b[[self.feats.index(f) for f in GROUPS[g] if f in self.feats]] for g in GNAMES])
            team = np.concatenate([np.full(len(r[3]), r[7], dtype=object) for r in rr])
            out.append((sid, y, s, Gs, team))
        return [np.concatenate(x) for x in zip(*out)]

    def coefs(self):
        return {f"{self.action} {lo}-{hi}": dict(zip(self.feats, b)) for lo, hi, mu, sd, b in self.parts}


def fit_team(sid, y, s, Gs, team, lam, gsd):
    """팀마다 δ (4개). 공통 점수는 오프셋."""
    d = {}
    for t in np.unique(team):
        m = team == t
        d[t] = fit_off(sid[m], y[m], Gs[m] / gsd, s[m], lam) / gsd
    return d


def apply_team(s, Gs, team, delta):
    out = s.copy()
    for t, dl in delta.items():
        m = team == t
        out[m] += Gs[m] @ dl
    return out


def rank_list(sid, y, score):
    df = pd.DataFrame({"sid": sid, "y": y, "s": score})
    res = []
    for k, g in df.groupby("sid"):
        if g["y"].sum() != 1:
            continue
        v = g.loc[g["y"] == 1, "s"].iloc[0]
        res.append((k, 1 + (g["s"] > v).sum() + 0.5 * ((g["s"] == v).sum() - 1)))
    return res


# ---------------------------------------------------------------- main
def main():
    t0 = time.time()
    a, pro_pick, pro_ban, lineup, sq, champs = B.load()
    cid = B.cid_map()
    ev = F.pro_events(cid)
    pgames = V.pro_games(cid)
    tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
    sets = B.eval_sets(a)
    days = sorted(a["day"].unique())
    ds_cache = {D: F.DayStats(D, ev, pro_pick, sq, tmeta, champs) for D in days}
    ex_cache = {D: V.day_extras(D, pro_pick, sq) for D in days}
    wv = win_value_table()
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    all_games = sorted(gday.index, key=lambda x: gday[x])
    rows = V.build_rows(a, all_games, ds_cache, ex_cache, lineup, champs, pgames, {}, wv)
    step_of = {r[0]: r[5] for r in rows}
    log.info("단계 × 후보 (%.0f초)", time.time() - t0)

    # 리허설: 이길 가치 유무 × λ
    res = []
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        train = [r for r in rows if gday[r[1]] < start]
        test = [r for r in rows if r[1] in gids]
        for action in ("pick", "ban"):
            for with_wv in (False, True):
                if not with_wv:
                    saved = (PICK_FEATS[:], BAN_FEATS[:])
                    PICK_FEATS.remove("win_value")
                    BAN_FEATS.remove("win_value")
                cm = Common(train, action)
                sid, y, s, Gs, team = cm.score(train)
                gsd = Gs.std(0) + 1e-9
                s2, y2, sc2, G2, team2 = cm.score(test)
                for lam in (LAMBDAS if with_wv else [None]):
                    score = sc2 if lam is None else apply_team(sc2, G2, team2, fit_team(sid, y, s, Gs, team, lam, gsd))
                    for k, rk in rank_list(s2, y2, score):
                        res.append({"set": name, "action": action, "win_value": with_wv, "lam": "없음" if lam is None else lam,
                                    "step": step_of[k], "rank": rk, "team": team2[s2 == k][0]})
                if not with_wv:
                    PICK_FEATS[:], BAN_FEATS[:] = saved
        log.info("  %s 끝 (%.0f초)", name, time.time() - t0)
    R = pd.DataFrame(res)
    H.save_csv(R, OUT / "m4_model_ranks.csv")

    def tops(g):
        return {"top1": (g["rank"] <= 1).mean(), "top3": (g["rank"] <= 3).mean(), "top5": (g["rank"] <= 5).mean(), "MRR": (1 / g["rank"]).mean()}
    summ = R.groupby(["action", "win_value", "lam"]).apply(lambda g: pd.Series(tops(g))).reset_index()
    best = {}
    for action in ("pick", "ban"):
        x = summ[(summ["action"] == action) & summ["win_value"]]
        x = x.assign(obj=x["top5"] + x["MRR"])
        best[action] = x.sort_values("obj", ascending=False)["lam"].iloc[0]
    log.info("고른 λ %s", best)

    # 최종: 08-26 이전 LCK 전부
    final, team_tab = {}, []
    rng = np.random.default_rng(0)
    for action in ("pick", "ban"):
        cm = Common(rows, action)
        sid, y, s, Gs, team = cm.score(rows)
        gsd = Gs.std(0) + 1e-9
        lam = best[action]
        delta = {} if lam == "없음" else fit_team(sid, y, s, Gs, team, float(lam), gsd)
        # 부트스트랩 (경기 묶음, 공통 고정)
        gid_of = {r[0]: r[1] for r in rows}
        game = np.array([gid_of[k] for k in sid])
        boots = []
        if delta:
            for _ in range(30):
                gs_ = rng.choice(np.unique(game), len(np.unique(game)), replace=True)
                cnt = pd.Series(gs_).value_counts()
                wrow = pd.Series(game).map(cnt).fillna(0).to_numpy()
                m = wrow > 0
                d = {}
                for t in np.unique(team):
                    mm = m & (team == t)
                    d[t] = fit_off(sid[mm], y[mm], Gs[mm] / gsd, s[mm], float(lam), wrow[mm]) / gsd
                boots.append(d)
        for t in sorted(np.unique(team)):
            for i, g in enumerate(GNAMES):
                v = delta.get(t, np.zeros(4))[i] if delta else 0.0
                bs = [b_[t][i] for b_ in boots] if boots else [0.0]
                team_tab.append({"team": t, "action": action, "group": g, "delta": v,
                                 "lo": np.percentile(bs, 2.5), "hi": np.percentile(bs, 97.5)})
        final[action] = {"features": cm.feats, "segments": [{"steps": [lo, hi], "mu": mu.tolist(), "sd": sd.tolist(), "coef": b.tolist()}
                                                          for lo, hi, mu, sd, b in cm.parts],
                         "groups": {g: [f for f in GROUPS[g] if f in cm.feats] for g in GNAMES},
                         "lambda": None if lam == "없음" else float(lam), "group_sd": gsd.tolist(),
                         "team_delta": {t: v.tolist() for t, v in delta.items()}, "common_coefs": cm.coefs()}
    (OUT / "m4_model.json").write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")
    TT = pd.DataFrame(team_tab)
    H.save_csv(TT, OUT / "m4_team_style.csv")

    # 보고서
    def fmt(x):
        return f"{x['top1']:.1%} · {x['top3']:.1%} · {x['top5']:.1%} · {x['MRR']:.3f}"
    L = ["# M4 1차 — 공통 + 이길 가치 + 팀별 편차", "",
         "- 공통 = M4 1차 특징 보강 설정 (픽 하나 · 밴 1·2페이즈 따로, A2·A3 보강·메타 순위·플레이오프 가중 뺌)",
         "- 이길 가치 = M2 챔피언 단독(대회) + M1 조합 성격 기여 (빈 포지션 기준). M0~M2 가 08-25 까지 전체로 학습돼 리허설이 조금 낙관적일 수 있음",
         "- 팀 편차 = 4그룹(메타·숙련·카운터·조합) × 픽·밴, L2 λ (작을수록 팀 차이를 많이 허용)", "",
         "## 리허설 (3세트 합, top1 · top3 · top5 · MRR)", "", "| 설정 | 픽 | 밴 |", "|---|---|---|"]
    v1 = pd.read_csv(OUT / "m4_v1_ranks.csv")
    for cfg_name, cfg in (("M4 1차 특징 보강 (전 단계 결과)", "1차 전체"),):
        g = v1[v1["config"] == cfg]
        L.append(f"| {cfg_name} | {fmt(tops(g[g['action'] == 'pick']))} | {fmt(tops(g[g['action'] == 'ban']))} |")
    for (wv_, lam), _g in summ.groupby(["win_value", "lam"], sort=False):
        p_ = summ[(summ["action"] == "pick") & (summ["win_value"] == wv_) & (summ["lam"] == lam)]
        b_ = summ[(summ["action"] == "ban") & (summ["win_value"] == wv_) & (summ["lam"] == lam)]
        if len(p_) and len(b_):
            nm = f"{'이길 가치 있음' if wv_ else '이길 가치 없음'} · 팀 편차 {'없음' if lam == '없음' else f'λ {lam:g}'}"
            L.append(f"| {nm} | {fmt(p_.iloc[0])} | {fmt(b_.iloc[0])} |")
    L += ["", f"- 고른 λ: 픽 {best['pick']}, 밴 {best['ban']} (리허설 top5 + MRR 최대)", ""]
    sel = R[(R["win_value"]) & (R["lam"].astype(str) == str(best["pick"])) & (R["action"] == "pick")]
    sel0 = R[(R["win_value"]) & (R["lam"] == "없음") & (R["action"] == "pick")]
    L += ["## 팀별 픽 top5 (리허설 합, 팀 편차 없음 → 고른 설정)", "", "| 팀 | 단계 | 없음 | 고른 설정 |", "|---|---|---|---|"]
    for t, g in sel0.groupby("team"):
        g1 = sel[sel["team"] == t]
        L.append(f"| {t} | {len(g)} | {(g['rank'] <= 5).mean():.1%} | {(g1['rank'] <= 5).mean():.1%} |")
    L += ["", "## 팀 성향표 (08-25 까지 전부로 학습, δ = 그룹을 리그 평균보다 더 따르는 정도, [95% 부트스트랩])", "",
          "| 팀 | 픽 메타 | 픽 숙련 | 픽 카운터 | 픽 조합 | 밴 메타 | 밴 숙련 | 밴 카운터 | 밴 조합 |", "|---|---|---|---|---|---|---|---|---|"]
    for t, g in TT.groupby("team"):
        cells = []
        for action in ("pick", "ban"):
            for gn in GNAMES:
                r = g[(g["action"] == action) & (g["group"] == gn)].iloc[0]
                sig = (r["lo"] > 0) or (r["hi"] < 0)
                v = f"{r['delta']:+.2f} [{r['lo']:+.2f}, {r['hi']:+.2f}]"
                cells.append(f"**{v}**" if sig else v)
        L.append(f"| {t} | " + " | ".join(cells) + " |")
    L += ["", "- 굵게 = 구간이 0 을 안 포함. +0.3 = 그 그룹을 리그 평균보다 30% 더 따름", ""]
    C = {}
    for action in ("pick", "ban"):
        C.update(final[action]["common_coefs"])
    Cd = pd.DataFrame(C)
    L += ["## 공통 계수 (08-25 까지 전부, 1 SD 당)", "", "| 특징 | " + " | ".join(Cd.columns) + " |", "|---|" + "---|" * len(Cd.columns)]
    for f_ in Cd.index:
        L.append(f"| {f_} | " + " | ".join("—" if pd.isna(Cd.loc[f_, c]) else f"{Cd.loc[f_, c]:+.2f}" for c in Cd.columns) + " |")
    (OUT / "m4_model_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("M4 1차 끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
