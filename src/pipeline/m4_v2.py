"""M4 추가 특징 B1·B3·C2·C3 시험 (2026-11-06, docs/PIPELINE.md "M4 준비" 아이디어 목록)

M4 1차 공통 모델(픽 하나 · 밴 1·2페이즈, 이길 가치 포함, 팀 편차 없음) 위에 하나씩 더해 리허설로 비교.
  B3 블라인드 픽 안전도: 그 챔피언(포지션)의 맞라인 15분 골드 상성이 상대에 따라 얼마나 흔들리나 (M2 골드 모델, 판수 가중 표준편차의 음수).
     픽 1~3번째(상대 응수 전)에 곱한 칸도 같이
  C2 선픽·후픽 팀 습관: 그 팀이 같은 선픽/후픽 처지에서 같은 픽 순서(밴은 같은 페이즈)에 이 챔피언을 고른 비율 (LCK, 전날까지)
  C3 팀 유형 일치: 챔피언의 초반·후반 지수(M3 경로 계수 × M0 축) × 팀의 초반·후반 성향(M3 팀 유형 z). 밴은 상대 팀 유형
  B1 챔피언 임베딩: 5리그 같은 팀 동시 픽 PPMI → SVD 16차원, 달마다 그 달 이전 경기로 다시 만듦.
     픽 = 우리 픽과의 평균 유사도, 밴 = 상대 픽과의 평균 유사도, 공통 = 이미 밴된 챔피언과의 최대 유사도
주의: B3·C3 는 08-25 까지 전체로 만든 M2·M3 값을 씀 (리허설이 조금 낙관적일 수 있음)

    python src/pipeline/m4_v2.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict
from itertools import combinations
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
EXTRA = ["b3_safe", "b3_safe_early", "c2_hab", "c3_align", "b1_team", "b1_ban"]
GROUPS = {"B3 블라인드 안전도": ["b3_safe", "b3_safe_early"], "C2 선픽·후픽 습관": ["c2_hab"],
          "C3 팀 유형 일치": ["c3_align"], "B1 임베딩": ["b1_team", "b1_ban"]}


# ---------------------------------------------------------------- 정적 표 (B3, C3)
def blind_safety() -> dict:
    t = pd.read_csv(PROCESSED / "m2" / "m2_lane_counter.csv")
    acc = defaultdict(lambda: [0.0, 0.0])
    for r in t.itertuples():
        x, y = r.key.split(">")
        v = r.val / 1000
        for k, s in ((x, v), (y, -v)):
            acc[k][0] += r.n * s * s
            acc[k][1] += r.n
    return {tuple(k.split("@")): -np.sqrt(a / n) for k, (a, n) in acc.items() if n > 0}


def early_late() -> tuple[dict, dict]:
    p = pd.read_csv(PROCESSED / "m3" / "m3_paths.csv").set_index("feature")
    at = pd.read_csv(PROCESSED / "m0" / "m0_attributes.csv", low_memory=False, dtype={"patch": str})
    at = at[at["patch"] == "ALL"]
    el = {}
    for r in at.itertuples(index=False):
        d = r._asdict()
        e = l_ = 0.0
        for f, col in M1.AX.items():
            if f in p.index and pd.notna(d.get(col)):
                z = (d[col] - 50) / 28.87
                e += p.loc[f, "gold15_pro"] * z
                l_ += p.loc[f, "win_direct_pro"] * z
        el[(d["champion_id"], d["position"])] = (e, l_)
    ts = pd.read_csv(PROCESSED / "m3" / "m3_team_style.csv").set_index("team")
    return el, {t: (r.early_z, r.late_z) for t, r in ts.iterrows()}


# ---------------------------------------------------------------- B1 임베딩 (달마다)
def embeddings(pgames, months, k=16) -> dict:
    out = {}
    for m in months:
        P = [g for g in pgames if g["day"] < m]
        cnt, co = Counter(), Counter()
        for g in P:
            for t in g["sides"].values():
                cs = sorted({c for _, c, _ in t["picks"]})
                for c in cs:
                    cnt[c] += 1
                for x, y in combinations(cs, 2):
                    co[(x, y)] += 1
        champs = sorted(cnt)
        if len(champs) < k + 2:
            out[m] = {}
            continue
        ix = {c: i for i, c in enumerate(champs)}
        M = np.zeros((len(champs), len(champs)))
        for (x, y), n in co.items():
            M[ix[x], ix[y]] = M[ix[y], ix[x]] = n
        tot = M.sum()
        row = M.sum(1, keepdims=True) + 1e-9
        pmi = np.log((M * tot) / (row @ row.T) + 1e-12)
        ppmi = np.maximum(pmi, 0)
        U, S, _ = np.linalg.svd(ppmi, full_matrices=False)
        E = U[:, :k] * np.sqrt(S[:k])
        E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9
        out[m] = {c: E[ix[c]] for c in champs}
    return out


# ---------------------------------------------------------------- 추가 특징 계산
def add_extra(rows, a, safety, el, tstyle, emb):
    # C2: 팀 × 선픽/후픽 × 순서 버킷 기록 (LCK)
    a = a.sort_values(["GameId", "step"]).copy()
    a["pick_no"] = a[a["action"] == "pick"].groupby("GameId").cumcount() + 1
    a["bk"] = np.where(a["action"] == "pick", a["pick_no"].map(lambda x: F.PICK_BUCKET.get(int(x), "") if pd.notna(x) else ""),
                       np.where(a["step"] <= 6, "b1", "b2"))
    a["fp"] = a["pick_order"] == "fp"
    hist = a[["day", "team", "fp", "bk", "cid"]].dropna()
    for r in rows:
        ctx = r[8]
        D, T, fp, bk = ctx["D"], r[7], ctx["fp"], ctx["bucket"]
        h = hist[(hist["day"] < D) & (hist["team"] == T) & (hist["fp"] == fp) & (hist["bk"] == bk)]
        hc = Counter(h["cid"])
        nh = len(h)
        E = emb.get(D[:7] + "-01", {})
        is_pick = r[2] == "pick"
        roles = ctx["my_open"] if is_pick else ctx["op_open"]
        style_team = T if is_pick else ctx["opp_team"]
        te, tl = tstyle.get(style_team, (0.0, 0.0))
        early_pick = is_pick and bk in ("p1", "p23")
        ref = [c for c, _ in (ctx["own"] if is_pick else ctx["opp"])]
        refE = [E[c] for c in ref if c in E]
        banE = [E[c] for c in ctx["bans"] if c in E]
        X = np.zeros((len(ctx["cands"]), len(EXTRA)), np.float32)
        for i, c in enumerate(ctx["cands"]):
            sf = max([safety.get((c, ro), -0.15) for ro in roles] or [-0.15])
            X[i, 0] = sf if is_pick else 0.0
            X[i, 1] = sf if early_pick else 0.0
            X[i, 2] = np.log((hc.get(c, 0) + 0.1) / (nh + 1))
            X[i, 3] = max([el.get((c, ro), (0.0, 0.0))[0] * te + el.get((c, ro), (0.0, 0.0))[1] * tl for ro in roles] or [0.0])
            if c in E:
                X[i, 4] = float(np.mean([E[c] @ v for v in refE])) if refE else 0.0
                X[i, 5] = float(max([E[c] @ v for v in banE])) if banE else 0.0
        r[9] = X
    return rows


def arrays(rows, feats, extra):
    ix = [V.ALL.index(k) for k in feats]
    ex = [EXTRA.index(k) for k in extra]
    sid = np.concatenate([np.full(len(r[3]), r[0]) for r in rows])
    y = np.concatenate([r[3] for r in rows]).astype(float)
    X = np.vstack([np.hstack([r[4][:, ix], r[9][:, ex]]) for r in rows]).astype(float)
    return sid, y, X


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
    wv = MM.win_value_table()
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    all_games = sorted(gday.index, key=lambda x: gday[x])
    rows = V.build_rows(a, all_games, ds_cache, ex_cache, lineup, champs, pgames, {}, wv)
    rows = [list(r) + [None] for r in rows]
    log.info("단계 × 후보 (%.0f초)", time.time() - t0)
    months = sorted({d[:7] + "-01" for d in days})
    emb = embeddings(pgames, months)
    el, tstyle = early_late()
    add_extra(rows, a, blind_safety(), el, tstyle, emb)
    log.info("추가 특징 (%.0f초)", time.time() - t0)

    configs = {"M4 1차 공통 (기준)": []}
    for g, fs in GROUPS.items():
        configs[f"+ {g}"] = fs
    configs["+ 넷 다"] = EXTRA
    res, coefs = [], {}
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        tr = [r for r in rows if gday[r[1]] < start]
        te = [r for r in rows if r[1] in gids]
        for cname, extra in configs.items():
            for action in ("pick", "ban"):
                feats = MM.PICK_FEATS if action == "pick" else MM.BAN_FEATS
                ex = [e for e in extra if not (action == "ban" and e in ("b3_safe", "b3_safe_early"))]
                for lo, hi in MM.SEGMENTS[action]:
                    trg = [r for r in tr if r[2] == action and lo <= r[5] <= hi]
                    teg = [r for r in te if r[2] == action and lo <= r[5] <= hi]
                    sid, y, X = arrays(trg, feats, ex)
                    mu, sd = X.mean(0), X.std(0) + 1e-9
                    b = MM.fit_off(sid, y, (X - mu) / sd, np.zeros(len(y)), 1.0)
                    if cname == "+ 넷 다" and name == "26.15":
                        coefs[f"{action} {lo}-{hi}"] = dict(zip(feats + ex, b))
                    s2, y2, X2 = arrays(teg, feats, ex)
                    team_of = {r[0]: r[7] for r in teg}
                    for k, rk in MM.rank_list(s2, y2, ((X2 - mu) / sd) @ b):
                        res.append({"set": name, "config": cname, "action": action, "step": [r[5] for r in teg if r[0] == k][0],
                                    "team": team_of[k], "rank": rk})
        log.info("  %s 끝 (%.0f초)", name, time.time() - t0)
    R = pd.DataFrame(res)
    H.save_csv(R, OUT / "m4_v2_ranks.csv")

    def t(g):
        return f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 3).mean():.1%} · {(g['rank'] <= 5).mean():.1%} · {(1 / g['rank']).mean():.3f}"
    L = ["# M4 추가 특징 B1 · B3 · C2 · C3 (리허설)", "",
         "- 기준 = M4 1차 공통 (이길 가치 포함, 팀 편차 없음). 하나씩 더해 비교. 값 = top1 · top3 · top5 · MRR (3세트 합)",
         "- B3·C3 는 08-25 까지 전체로 만든 M2·M3 값 사용 (조금 낙관적일 수 있음). B1 은 달마다 그 달 이전 경기로", "",
         "| 설정 | 픽 | 밴 |", "|---|---|---|"]
    for c in configs:
        g = R[R["config"] == c]
        L.append(f"| {c} | {t(g[g['action'] == 'pick'])} | {t(g[g['action'] == 'ban'])} |")
    L += ["", "## 팀별 픽 top5 (기준 → 넷 다)", "", "| 팀 | 기준 | 넷 다 |", "|---|---|---|"]
    for tm, g in R[R["action"] == "pick"].groupby("team"):
        f5 = lambda c: f"{(g[g['config'] == c]['rank'] <= 5).mean():.1%}"  # noqa: E731
        L.append(f"| {tm} | {f5('M4 1차 공통 (기준)')} | {f5('+ 넷 다')} |")
    C = pd.DataFrame(coefs)
    L += ["", "## 넷 다 계수 (26.15 세트 학습, 추가 특징만)", "", "| 특징 | " + " | ".join(C.columns) + " |", "|---|" + "---|" * len(C.columns)]
    for f_ in EXTRA:
        if f_ in C.index:
            L.append(f"| {f_} | " + " | ".join("—" if pd.isna(C.loc[f_, c]) else f"{C.loc[f_, c]:+.2f}" for c in C.columns) + " |")
    (OUT / "m4_v2_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
