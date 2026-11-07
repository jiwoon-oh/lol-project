"""M4 P2 · P3 · 두 단계 포지션 (2026-11-07, docs/PIPELINE.md "M4")

P2 — 학습 자료를 LCK 에서 5리그(LCK·LCKC·LPL·LEC·LCS) 드래프트로 늘림 (LCK 447 → 2,055경기).
     41개 리그가 아니라 5리그인 이유: 선수 풀·메타·P1 특징이 5리그 대회 기록으로 정의돼 있어 다른 리그는 특징 절반이 빔.
     다른 리그 드래프트 20단계는 OE 팀 행(firstPick · pick1~5 · ban1~5)으로 표준 순서대로 다시 만듦, 포지션은 선수 행.
     시리즈 = 같은 리그·같은 두 팀의 연속 경기 (OE game 번호가 1 이면 새 시리즈). 피어리스 여부는 리그마다 데이터로 판정
     (같은 시리즈 안에 같은 챔피언 픽이 다시 나오면 피어리스 아님 → 그 리그는 앞 세트 제외 규칙 안 씀)
     LCK 가중치 w (다른 리그 단계 1 대비): 1 · 3 · 10 → LCK 리허설로 고름. 'LCK 만' = 지금 최종 모델
     조건부 로짓은 단계 안 후보끼리만 비교 → 다른 리그에 없는 칸(리서치 선수 솔랭 등)이 0 이어도 그 단계엔 영향 없음
P3 — LightGBM 순위 모델 (lambdarank, 단계 = 묶음, LCK 표시 칸 + 가중치). 선형과 앙상블(단계 안 표준화 점수 평균)
두 단계 포지션 — 다음에 어느 포지션을 뽑나 (빈 포지션 중 하나, 조건부 로짓: 그 픽 순서의 포지션 비율 · 팀 습관 · 상대가 이미 채웠나 · 포지션)
     → 픽 칸 role_w = log Σ_r P(다음 포지션 r) × 그 챔피언의 r 포지션 비율 (기존 '빈 포지션' 칸을 확률 가중으로)
평가 = LCK 리허설(26.14 · 26.15 · RTM). 지금 최종 모델보다 분명히 좋을 때만 채택 (1%p 안은 우연 범위)
특징 표 캐시 m4/m4_v6_rows.pkl

    python src/pipeline/m4_v6.py
"""
from __future__ import annotations

import bisect
import sys
import time
from collections import Counter, defaultdict
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
import m4_v4 as V4  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = B.OUT
POS = M1.POS
OTHER = ["LCKC", "LPL", "LEC", "LCS"]
STRONG = ["T1", "Gen.G", "Hanwha Life Esports", "Dplus Kia", "KT Rolster"]
ORDER = [("ban", "fp", 1), ("ban", "lp", 1), ("ban", "fp", 2), ("ban", "lp", 2), ("ban", "fp", 3), ("ban", "lp", 3),
         ("pick", "fp", 1), ("pick", "lp", 1), ("pick", "lp", 2), ("pick", "fp", 2), ("pick", "fp", 3), ("pick", "lp", 3),
         ("ban", "lp", 4), ("ban", "fp", 4), ("ban", "lp", 5), ("ban", "fp", 5),
         ("pick", "lp", 4), ("pick", "fp", 4), ("pick", "fp", 5), ("pick", "lp", 5)]


# ---------------------------------------------------------------- 다른 리그 드래프트 표
def other_actions(cid):
    cols = ["gameid", "league", "date", "patch", "side", "position", "teamname", "champion", "firstPick", "game", "playoffs", "split"] + \
           [f"pick{i}" for i in range(1, 6)] + [f"ban{i}" for i in range(1, 6)]
    oe = pd.read_csv(M1.OE_FILE, usecols=cols, low_memory=False, dtype={"patch": str})
    oe = oe[oe["league"].isin(OTHER) & (oe["date"].str[:10] <= B.OTHER_END)]
    pl = oe[oe["position"].isin(H.OE_POS)].copy()
    pl["cid"] = pl["champion"].map(cid)
    role_of = {(r.gameid, r.teamname, r.cid): H.OE_POS[r.position] for r in pl.itertuples()}
    tm = oe[(oe["position"] == "team") & oe["firstPick"].notna()].copy()
    tm = tm.sort_values("date")
    rows = []
    series = {}
    for gid, g in tm.groupby("gameid", sort=False):
        if len(g) != 2:
            continue
        fp = g[g["firstPick"] == 1]
        lp = g[g["firstPick"] == 0]
        if len(fp) != 1 or len(lp) != 1:
            continue
        fp, lp = fp.iloc[0], lp.iloc[0]
        key = (fp["league"], tuple(sorted([fp["teamname"], lp["teamname"]])))
        if int(fp["game"]) == 1 or key not in series:
            series[key] = f"{fp['league']}_{fp['date'][:10]}_{'_'.join(key[1])}"
        mid = series[key]
        side = {"fp": fp, "lp": lp}
        acts = []
        for step, (act, who, k) in enumerate(ORDER, 1):
            t, o = side[who], side["lp" if who == "fp" else "fp"]
            name = t[f"{act}{k}"]
            c = cid(name) if isinstance(name, str) else None
            acts.append({"GameId": f"OE_{gid}", "date_kst": t["date"][:16], "day": t["date"][:10],
                         "Patch": "26." + str(t["patch"]).split(".")[1] if isinstance(t["patch"], str) else None,
                         "step": step, "phase": ("ban1" if step <= 6 else "pick1" if step <= 12 else "ban2" if step <= 16 else "pick2"),
                         "pick_order": who, "side": t["side"], "action": act, "team": t["teamname"], "opponent": o["teamname"],
                         "champion": name, "cid": c, "role": role_of.get((gid, t["teamname"], c)) if act == "pick" else None,
                         "MatchId": mid, "N_GameInMatch": int(t["game"]),
                         "OverviewPage": f"{t['league']}/{t['split']}", "Tab": "Playoffs" if int(t["playoffs"] or 0) == 1 else "Season",
                         "RiotPlatformGameId": gid, "league": t["league"]})
        rows += acts
    d = pd.DataFrame(rows)
    # 피어리스 판정 (리그마다): 같은 시리즈 안 픽 중복이 있으면 피어리스 아님
    p = d[d["action"] == "pick"]
    rep = p.groupby(["league", "MatchId"])["cid"].apply(lambda s: s.duplicated().any())
    fear = (~rep.groupby("league").any()).to_dict()
    d["fearless"] = d["league"].map(fear)
    return d, fear


# ---------------------------------------------------------------- C2 (빠른 판)
def c2_feature(rows, a):
    a = a.sort_values(["GameId", "step"]).copy()
    a["pick_no"] = a[a["action"] == "pick"].groupby("GameId").cumcount() + 1
    a["bk"] = np.where(a["action"] == "pick", a["pick_no"].map(lambda x: F.PICK_BUCKET.get(int(x), "") if pd.notna(x) else ""),
                       np.where(a["step"] <= 6, "b1", "b2"))
    a["fp"] = a["pick_order"] == "fp"
    idx = defaultdict(list)
    for r in a.dropna(subset=["cid"]).sort_values("day").itertuples():
        idx[(r.team, r.fp, r.bk)].append((r.day, r.cid))
    for r in rows:
        ctx = r[8]
        lst = idx.get((r[7], ctx["fp"], ctx["bucket"]), [])
        k = bisect.bisect_left(lst, (ctx["D"], ""))
        hc = Counter(c for _, c in lst[:k])
        r.append(np.array([[np.log((hc.get(c, 0) + 0.1) / (k + 1))] for c in ctx["cands"]], np.float32))   # r[9]
    return rows


# ---------------------------------------------------------------- 다음 포지션 모델
def role_rows(rows, a, ds_cache):
    """픽 단계마다 (빈 포지션 후보 × 특징, 정답 포지션)."""
    role_of = a.set_index(["GameId", "step"])["role"].to_dict()
    p = a[a["action"] == "pick"].sort_values(["GameId", "step"]).copy()
    p["pick_no"] = p.groupby("GameId").cumcount() + 1
    hist = defaultdict(list)          # (팀, 픽 순서) → [(날짜, 포지션)]
    glob_ = defaultdict(list)         # 픽 순서 → [(날짜, 포지션)]
    for r in p.dropna(subset=["role"]).sort_values("day").itertuples():
        hist[(r.team, r.pick_no)].append((r.day, r.role))
        glob_[r.pick_no].append((r.day, r.role))
    out = {}
    pick_no_of = p.set_index(["GameId", "step"])["pick_no"].to_dict()
    for r in rows:
        if r[2] != "pick":
            continue
        ctx = r[8]
        D, opens = ctx["D"], ctx["my_open"]
        n = pick_no_of.get((r[1], r[5]))
        true = role_of.get((r[1], r[5]))
        if not opens or n is None or true not in opens:
            continue
        g = glob_[n][:bisect.bisect_left(glob_[n], (D, ""))]
        h = hist[(r[7], n)][:bisect.bisect_left(hist[(r[7], n)], (D, ""))]
        gc, hc = Counter(x for _, x in g), Counter(x for _, x in h)
        opp_filled = {ro for _, ro in ctx["opp"] if ro}
        X = []
        for ro in opens:
            X.append([np.log((gc.get(ro, 0) + 1) / (len(g) + 5)), np.log((hc.get(ro, 0) + 0.5) / (len(h) + 2.5)),
                      float(ro in opp_filled)] + [float(ro == q) for q in POS])
        out[r[0]] = (opens, np.array(X), opens.index(true))
    return out


def fit_role(rr, keys):
    sid, y, X = [], [], []
    for k in keys:
        if k in rr:
            opens, Xk, t = rr[k]
            sid += [k] * len(opens)
            y += [1.0 if i == t else 0.0 for i in range(len(opens))]
            X.append(Xk)
    X = np.vstack(X)
    return MM.fit_off(np.array(sid), np.array(y), X, np.zeros(len(y)), 0.1)


def role_w(rows, rr, theta, ds_cache):
    """픽 행마다 후보별 log Σ_r P(r) × r 포지션 비율."""
    out = {}
    for r in rows:
        if r[2] != "pick" or r[0] not in rr:
            continue
        opens, Xk, _ = rr[r[0]]
        z = Xk @ theta
        pr = np.exp(z - z.max())
        pr /= pr.sum()
        ds = ds_cache[r[8]["D"]]
        out[r[0]] = np.array([[np.log(sum(pr[i] * (ds.role_share[ro].get(c, 0) + 0.1) / ds.role_n[ro] for i, ro in enumerate(opens)) + F.EPS)]
                              for c in r[8]["cands"]], np.float32)
    return out


# ---------------------------------------------------------------- 평가 도우미
def mats(rr, action, extra_role=None):
    feats = MM.PICK_FEATS if action == "pick" else MM.BAN_FEATS
    ix = [V.ALL.index(k) for k in feats]
    sid, y, X, lck = [], [], [], []
    for r in rr:
        parts = [r[4][:, ix]]
        if action == "ban":
            parts += [r[9], r[10]]                     # C2, P1(5리그)
        if extra_role is not None:
            parts.append(extra_role.get(r[0], np.zeros((len(r[3]), 1), np.float32)))
        X.append(np.hstack(parts))
        sid.append(np.full(len(r[3]), r[0]))
        y.append(r[3])
        lck.append(np.full(len(r[3]), float(r[11] == "LCK")))
    return np.concatenate(sid), np.concatenate(y).astype(float), np.vstack(X).astype(float), np.concatenate(lck)


def zscore_within(sid, s):
    df = pd.DataFrame({"sid": sid, "s": s})
    g = df.groupby("sid")["s"]
    return ((df["s"] - g.transform("mean")) / (g.transform("std") + 1e-9)).to_numpy()


def main():
    import lightgbm as lgb
    t0 = time.time()
    cache = OUT / "m4_v6_rows.pkl"
    cid = B.cid_map()
    a_lck, pro_pick, pro_ban, lineup, sq, champs = B.load()
    a_lck = a_lck.assign(league="LCK", fearless=True)
    ev_all, games = V4.patch_events(cid)
    if cache.exists():
        rows, a, fear, ds_days = pd.read_pickle(cache)
    else:
        a_oth, fear = other_actions(cid)
        log.info("다른 리그 드래프트 %d경기, 피어리스 %s", a_oth["GameId"].nunique(), fear)
        a = pd.concat([a_lck, a_oth], ignore_index=True)
        ev = F.pro_events(cid)
        pgames = V.pro_games(cid)
        tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
        ds_days = sorted(a["day"].unique())
        ds_cache = {D: F.DayStats(D, ev, pro_pick, sq, tmeta, champs) for D in ds_days}
        ex_cache = {D: V.day_extras(D, pro_pick, sq) for D in ds_days}
        log.info("날짜별 통계 %d일 (%.0f초)", len(ds_days), time.time() - t0)
        gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
        all_games = sorted(gday.index, key=lambda x: gday[x])
        rows = V.build_rows(a, all_games, ds_cache, ex_cache, lineup, champs, pgames, {}, MM.win_value_table())
        rows = [list(r) for r in rows]
        log.info("단계 × 후보 %d단계 (%.0f초)", len(rows), time.time() - t0)
        rows = c2_feature(rows, a)                                                   # r[9]
        s = pd.read_csv(PROCESSED / "c_patch_champion_summary.csv", dtype={"patch": str})
        buff = {(r.patch, r.champion_id): (1.0 if r.label == "buff" else -1.0 if r.label == "nerf" else 0.0) for r in s.itertuples()}
        rows = V4.p1_features(rows, a, ev_all, games, buff, five_only=True)          # r[10]
        lg = a.drop_duplicates("GameId").set_index("GameId")["league"].to_dict()
        for r in rows:
            r.append(lg[r[1]])                                                       # r[11]
        pd.to_pickle((rows, a, fear, ds_days), cache)
        del ds_cache, ex_cache
    log.info("특징 표 준비 (%.0f초)", time.time() - t0)
    tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
    ev = F.pro_events(cid)
    ds_cache = {D: F.DayStats(D, ev, pro_pick, sq, tmeta, champs) for D in ds_days}   # role_w 용 (포지션 비율)
    rr_role = role_rows(rows, a, ds_cache)
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    sets = B.eval_sets(a_lck)
    res = []
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        tr = [r for r in rows if gday[r[1]] < start]
        te = [r for r in rows if r[1] in gids]
        theta = fit_role(rr_role, [r[0] for r in tr if r[2] == "pick"])
        rw = role_w(tr + te, rr_role, theta, ds_cache)
        # 다음 포지션 정확도 (참고)
        acc = [int(np.argmax(rr_role[r[0]][1] @ theta) == rr_role[r[0]][2]) for r in te if r[0] in rr_role]
        log.info("  %s 다음 포지션 맞힘 %.1f%% (%d)", name, 100 * np.mean(acc), len(acc))
        for action in ("pick", "ban"):
            for lo, hi in MM.SEGMENTS[action]:
                trg = [r for r in tr if r[2] == action and lo <= r[5] <= hi]
                teg = [r for r in te if r[2] == action and lo <= r[5] <= hi]
                variants = {}
                for use_role in ([False, True] if action == "pick" else [False]):
                    er = rw if use_role else None
                    sid, y, X, lck = mats(trg, action, er)
                    s2, y2, X2, _ = mats(teg, action, er)
                    mu, sd = X.mean(0), X.std(0) + 1e-9
                    tag = " + 두 단계 포지션" if use_role else ""
                    for w in ("LCK 만", 1.0, 3.0, 10.0):
                        if w == "LCK 만":
                            m = lck == 1
                            b = MM.fit_off(sid[m], y[m], (X[m] - mu) / sd, np.zeros(m.sum()), 1.0)
                        else:
                            ww = np.where(lck == 1, w, 1.0)
                            b = MM.fit_off(sid, y, (X - mu) / sd, np.zeros(len(y)), 1.0, ww)
                        variants[f"선형 · {w if w == 'LCK 만' else f'5리그 w={w:g}'}{tag}"] = ((X2 - mu) / sd) @ b
                    if not use_role:
                        # P3 LightGBM (LCK 가중 3, LCK 표시 칸)
                        order = np.argsort(sid, kind="stable")
                        Xl = np.hstack([X, lck[:, None]])[order]
                        _, cnt = np.unique(sid[order], return_counts=True)
                        wq = np.where(lck[order] == 1, 3.0, 1.0)
                        ds_ = lgb.Dataset(Xl, label=y[order], group=cnt, weight=wq)
                        gbm = lgb.train({"objective": "lambdarank", "metric": "ndcg", "ndcg_eval_at": [5], "learning_rate": 0.05,
                                         "num_leaves": 31, "min_data_in_leaf": 100, "feature_fraction": 0.8, "verbose": -1,
                                         "lambdarank_truncation_level": 10}, ds_, num_boost_round=300)
                        sg = gbm.predict(np.hstack([X2, np.ones((len(X2), 1))]))
                        variants["LightGBM · 5리그 (LCK w=3)"] = sg
                        variants["앙상블 (선형 5리그 w=3 + LightGBM)"] = zscore_within(s2, variants["선형 · 5리그 w=3"]) + zscore_within(s2, sg)
                meta = {r[0]: r for r in teg}
                for vname, sc in variants.items():
                    for k, rk in MM.rank_list(s2, y2, sc):
                        res.append({"set": name, "model": vname, "action": action, "step": meta[k][5], "team": meta[k][7], "rank": rk})
        log.info("  %s 끝 (%.0f초)", name, time.time() - t0)
    R = pd.DataFrame(res)
    H.save_csv(R, OUT / "m4_v6_ranks.csv")

    def t(g):
        return f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 3).mean():.1%} · {(g['rank'] <= 5).mean():.1%} · {(1 / g['rank']).mean():.3f}"
    L = ["# M4 P2 · P3 · 두 단계 포지션 (LCK 리허설)", "",
         f"- 학습 자료: LCK + LCKC·LPL·LEC·LCS 드래프트 (다른 리그는 OE 로 20단계 재구성). 리그별 피어리스 판정: {fear}",
         "- 선형 = M4 최종과 같은 특징 (픽 공통 + 이길 가치, 밴 + C2 + P1). LCK 만 = 지금 최종 모델. w = LCK 단계 가중",
         "- 값 = top1 · top3 · top5 · MRR (LCK 26.14 · 26.15 · RTM 합)", "",
         "| 모델 | 픽 | 밴 | 강팀 픽 | RTM 픽 |", "|---|---|---|---|---|"]
    for mname in R["model"].unique():
        g = R[R["model"] == mname]
        gp = g[g["action"] == "pick"]
        gb = g[g["action"] == "ban"]
        L.append(f"| {mname} | {t(gp)} | {t(gb) if len(gb) else '—'} | {(gp[gp['team'].isin(STRONG)]['rank'] <= 5).mean():.1%} | "
                 f"{(gp[gp['set'] == 'RTM']['rank'] <= 5).mean():.1%} |")
    L += ["", "- '+ 두 단계 포지션' 은 픽만 (밴은 같은 이름의 모델 값과 동일)", ""]
    (OUT / "m4_v6_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
