"""M4 최종 검증 모드 (2026-11-07) — 고정 모델(m4/final/)로 LCK 플레이인·플레이오프(08-26~09-13, 55경기) 평가

자료 범위 (decisions 2026-11-06 D2 · 2026-11-07 정보 범위 A)
  - LCK 대회 기록·드래프트: 경기 전날까지 (플레이인·플레이오프 앞 경기 포함) + 같은 시리즈 앞 세트
  - 다른 리그(LCKC·LPL·LEC·LCS) 대회 기록: 08-25 까지
  - 리서치 선수 솔랭: 경기 전날까지
  - 팀원 메타 점수: valid_for == 경기일 행 (플레이오프 날짜 행은 정규시즌까지 정보로 만들어짐)
  - 모델은 다시 학습하지 않음 (m4/final/ 그대로)
조건
  A (드래프트 중): 같은 시리즈 앞 세트 밴·픽·승패까지 씀 (고정 모델 그대로)
  B (경기 전날까지만): 밴 모델의 같은 시리즈 칸(앞 세트 밴 횟수·바로 앞 세트 밴·상대 앞 세트 밴·패배 × 밴)을 0 으로. 피어리스 제외는 그대로
비교: 기준선 (픽 = 메타 + 빈 포지션 + 팀 습관 + 선수 풀, 밴 = 메타), P5 경기 전 포지션 모델
사용
  python src/pipeline/m4_validate.py --parity     코드 점검: 08-25 이전 LCK 경기 특징을 검증 모드로 다시 계산 → 학습 표(m4_v6_rows.pkl)와 비교 (플레이오프 안 봄)
  python src/pipeline/m4_validate.py --run --once  최종 검증 (한 번만. 결과를 보고 모델을 바꾸지 않음) → m4/validation/
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_features as F  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_p5 as P5  # noqa: E402
import m4_v1 as V  # noqa: E402
import m4_v4 as V4  # noqa: E402
import m4_v6 as V6  # noqa: E402
import m4_v7 as V7  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
FINAL = B.OUT / "final"
VOUT = B.OUT / "validation"
END = "2026-09-13"
SAME_SERIES = ["s_ban_same", "s_ban_last", "s_ban_opp", "s_lost_x_ban"]


def scoped_oe() -> Path:
    """LCK 는 09-13 까지, 다른 리그는 08-25 까지 남긴 OE 사본 (불러오는 함수들이 이 파일을 읽게 함)."""
    p = B.OUT / "validation_oe_scope.csv"
    if not p.exists():
        oe = pd.read_csv(M1.OE_FILE, low_memory=False, dtype={"patch": str})
        d = oe["date"].str[:10]
        keep = ((oe["league"] == "LCK") & (d <= END)) | (d <= B.OTHER_END)
        oe[keep].to_csv(p, index=False)
    return p


def setup_scope():
    M1.OE_FILE = scoped_oe()
    B.OTHER_END = END            # 사본이 이미 리그별로 잘려 있으므로 날짜 자르기는 끝 날로
    B.VALID_START = "2026-09-14"  # LCK 드래프트·솔랭을 플레이오프까지 불러옴 (특징은 모두 '전날까지'로만 씀)


def build_rows_for(games, a, pro_pick, lineup, sq, champs):
    cid = B.cid_map()
    ev = F.pro_events(cid)
    pgames = V.pro_games(cid)
    ev_all, gm = V4.patch_events(cid)
    tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    days = sorted({gday[g] for g in games})
    ds_cache = {D: F.DayStats(D, ev, pro_pick, sq, tmeta, champs) for D in days}
    ex_cache = {D: V.day_extras(D, pro_pick, sq) for D in days}
    rows = V.build_rows(a, sorted(games, key=lambda g: gday[g]), ds_cache, ex_cache, lineup, champs, pgames, {}, None)
    rows = [list(r) for r in rows]
    rows = V6.c2_feature(rows, a)
    s = pd.read_csv(PROCESSED / "c_patch_champion_summary.csv", dtype={"patch": str})
    buff = {(r.patch, r.champion_id): (1.0 if r.label == "buff" else -1.0 if r.label == "nerf" else 0.0) for r in s.itertuples()}
    rows = V4.p1_features(rows, a, ev_all, gm, buff, five_only=True)
    for r in rows:
        r.append("LCK")
    if "is_winner" not in a.columns:
        a["is_winner"] = np.nan
    rows = V7.series_features(rows, a, V7.winners(a))
    return rows, ds_cache, ex_cache


def load_final():
    import lightgbm as lgb
    spec = json.loads((FINAL / "model_spec.json").read_text(encoding="utf-8"))
    pick = lgb.Booster(model_file=str(FINAL / spec["pick"]["file"]))
    bans = [(seg, lgb.Booster(model_file=str(FINAL / seg["lgbm_file"]))) for seg in spec["ban"]["segments"]]
    return spec, pick, bans


def score_rows(rows, spec, pick, bans, condition="A"):
    """단계마다 (sid, 순위). condition B = 같은 시리즈 칸 0."""
    out = []
    pr = [r for r in rows if r[2] == "pick"]
    if pr:
        sid, y, X, _ = V7.mats(pr, "pick", False)
        sc = pick.predict(np.hstack([X, np.ones((len(X), 1))]))
        out += MM.rank_list(sid, y, sc)
    for seg, gbm in bans:
        lo, hi = seg["steps"]
        br = [r for r in rows if r[2] == "ban" and lo <= r[5] <= hi]
        if not br:
            continue
        if condition == "B":
            br = [list(r) for r in br]
            for r in br:
                X12 = r[12].copy()
                for f in SAME_SERIES:
                    X12[:, V7.SER.index(f)] = 0
                r[12] = X12
        sid, y, X, _ = V7.mats(br, "ban", True)
        lin = ((X - np.array(seg["linear"]["mu"])) / np.array(seg["linear"]["sd"])) @ np.array(seg["linear"]["coef"])
        gb = gbm.predict(np.hstack([X, np.ones((len(X), 1))]))
        sc = V6.zscore_within(sid, lin) + V6.zscore_within(sid, gb)
        out += MM.rank_list(sid, y, sc)
    return out


def parity():
    """코드 점검: 08-19~08-23 LCK 경기 특징을 검증 모드로 다시 계산 → 학습 표와 비교."""
    setup_scope()
    a, pro_pick, pro_ban, lineup, sq, champs = B.load()
    a = a.assign(league="LCK", fearless=True)
    games = sorted(a.loc[(a["day"] >= "2026-08-19") & (a["day"] <= "2026-08-25"), "GameId"].unique())
    rows, _, _ = build_rows_for(games, a, pro_pick, lineup, sq, champs)
    cache, *_ = pd.read_pickle(B.OUT / "m4_v6_rows.pkl")
    ref = {(r[1], r[5]): r for r in cache if r[1] in set(games)}
    a_full = pd.read_pickle(B.OUT / "m4_v6_rows.pkl")[1]
    if "is_winner" not in a_full.columns:
        a_full["is_winner"] = np.nan
    ref_rows = V7.series_features([list(r) for r in ref.values()], a_full, V7.winners(a_full))
    ref = {(r[1], r[5]): r for r in ref_rows}
    diffs = {"공통 특징": [], "C2": [], "P1": [], "시리즈": [], "후보 목록 다름": 0, "비교 단계": 0}
    col_diff = {}
    for r in rows:
        k = (r[1], r[5])
        if k not in ref:
            continue
        q = ref[k]
        diffs["비교 단계"] += 1
        if r[8]["cands"] != q[8]["cands"]:
            diffs["후보 목록 다름"] += 1
            continue
        dcol = np.abs(r[4] - q[4]).max(axis=0)
        for j, v in enumerate(dcol):
            if v > 1e-5:
                col_diff[V.ALL[j]] = max(col_diff.get(V.ALL[j], 0.0), float(v))
        used = [V.ALL.index(f) for f in set(V7.PICK_FEATS) | set(V7.BAN_FEATS)]   # 최종 모델이 쓰는 칸만
        diffs["공통 특징"].append(float(dcol[used].max()))
        diffs["C2"].append(float(np.abs(r[9] - q[9]).max()))
        diffs["P1"].append(float(np.abs(r[10] - q[10]).max()))
        diffs["시리즈"].append(float(np.abs(r[12] - q[12]).max()))
    rep = {k: (max(v) if isinstance(v, list) and v else v) for k, v in diffs.items()}
    log.info("코드 점검 (08-19~25, %d경기): %s", len(games), rep)
    log.info("차이 나는 칸 (최종 모델이 안 쓰는 칸 포함): %s", col_diff)
    # 고정 모델로 점수를 매길 수 있는지 (형식 점검, 성적 아님)
    spec, pick, bans = load_final()
    n = len(score_rows(rows, spec, pick, bans))
    log.info("고정 모델 점수 계산 단계 %d (형식 점검)", n)
    return rep, len(games), n


def run():
    setup_scope()
    VOUT.mkdir(exist_ok=True)
    t0 = time.time()
    a, pro_pick, pro_ban, lineup, sq, champs = B.load()
    a = a.assign(league="LCK", fearless=True)
    g = pd.read_csv(PROCESSED / "b_draft_games.csv")
    po = g[g["OverviewPage"].astype(str).str.contains("Season Play-In|Season Playoffs")]
    games = sorted(set(po["GameId"]) & set(a["GameId"]))
    stage = po.set_index("GameId")["OverviewPage"].map(lambda s: "플레이인" if "Play-In" in s else "플레이오프").to_dict()
    log.info("검증 경기 %d (플레이인 %d · 플레이오프 %d)", len(games), sum(stage[x] == "플레이인" for x in games),
             sum(stage[x] == "플레이오프" for x in games))
    rows, ds_cache, ex_cache = build_rows_for(games, a, pro_pick, lineup, sq, champs)
    spec, pick, bans = load_final()
    meta = {r[0]: r for r in rows}
    res = []
    for cond in ("A", "B"):
        for k, rk in score_rows(rows, spec, pick, bans, cond):
            r = meta[k]
            res.append({"condition": cond, "model": "M4 최종", "action": r[2], "step": r[5], "team": r[7], "game": r[1],
                        "stage": stage[r[1]], "game_n": int(r[12][0, -1]), "rank": rk})
    # 기준선 (같은 자료 범위)
    for gid in games:
        ga = a[a["GameId"] == gid].sort_values("step")
        if ga["cid"].isna().any() or len(ga) != 20:
            continue
        D, mid, n = ga["day"].iloc[0], ga["MatchId"].iloc[0], ga["N_GameInMatch"].iloc[0]
        fearless = set(a[(a["MatchId"] == mid) & (a["N_GameInMatch"] < n) & (a["action"] == "pick")]["cid"])
        ctx, used, filled = {}, set(), {}
        for r in ga.itertuples():
            if r.team not in ctx:
                ctx[r.team] = B.Context(D, r.team, r.opponent, ga["RiotPlatformGameId"].iloc[0], pro_pick, pro_ban, lineup, sq, a, champs)
            avail = [c for c in champs if c not in used and c not in fearless]
            open_roles = [p for p in M1.POS if p not in filled.get(r.team, set())]
            m = "B4" if r.action == "pick" else "B1"
            s = B.scores(m, r.action, ctx[r.team], open_roles)
            v = s[r.cid]
            rank = 1 + sum(1 for c in avail if s[c] > v) + 0.5 * sum(1 for c in avail if s[c] == v and c != r.cid)
            res.append({"condition": "기준선", "model": "기준선", "action": r.action, "step": r.step, "team": r.team, "game": gid,
                        "stage": stage[gid], "game_n": int(n), "rank": rank})
            used.add(r.cid)
            if r.action == "pick" and isinstance(r.role, str):
                filled.setdefault(r.team, set()).add(r.role)
    R = pd.DataFrame(res)
    H.save_csv(R, VOUT / "m4_validation_ranks.csv")
    # P5 경기 전 포지션 (고정 경기 전 모델)
    pm = json.loads((B.OUT / "m4_p5_pregame_model.json").read_text(encoding="utf-8"))
    picks = a[a["GameId"].isin(games) & (a["action"] == "pick") & a["role"].notna()]
    units = []
    for r in picks.itertuples():
        earlier = a[(a["MatchId"] == r.MatchId) & (a["N_GameInMatch"] < r.N_GameInMatch) & (a["action"] == "pick")]["cid"]
        units.append(((r.GameId, r.team, r.role), r.day, r.team, r.role, F._player(lineup, r.RiotPlatformGameId, r.team, r.role), r.cid, set(earlier)))
    U = P5.unit_rows(units, ds_cache, ex_cache, champs)
    p5 = []
    for u in U:
        s = ((u[3] - np.array(pm["mu"])) / np.array(pm["sd"])) @ np.array(pm["coef"])
        t = int(np.argmax(u[2]))
        p5.append({"team": u[0][1], "role": u[0][2], "stage": stage[u[0][0]], "rank": 1 + (s > s[t]).sum() + 0.5 * ((s == s[t]).sum() - 1)})
    P = pd.DataFrame(p5)
    H.save_csv(P, VOUT / "m4_validation_p5_ranks.csv")

    def t(g):
        return f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 3).mean():.1%} · {(g['rank'] <= 5).mean():.1%} · {(1 / g['rank']).mean():.3f}" if len(g) else "—"
    L = ["# M4 최종 검증 — LCK 플레이인 · 플레이오프 (08-26 ~ 09-13)", "",
         f"- 경기 {len(games)} (플레이인 {sum(stage[x] == '플레이인' for x in games)} · 플레이오프 {sum(stage[x] == '플레이오프' for x in games)}). 고정 모델 `m4/final/` (만든 시각 {spec['created']}, 코드 {spec['code']['git_head']})",
         "- A = 드래프트 중 (같은 시리즈 앞 세트 정보 씀), B = 경기 전날까지만 (밴 모델의 같은 시리즈 칸 0). 기준선 = 픽 메타 + 빈 포지션 + 팀 습관 + 선수 풀, 밴 메타",
         "- 값 = top1 · top3 · top5 · MRR. **이 결과를 보고 모델을 바꾸지 않음**", "",
         "| 모델 · 조건 | 픽 | 밴 |", "|---|---|---|"]
    for cname, g in R.groupby("condition", sort=False):
        L.append(f"| {g['model'].iloc[0]} · {cname} | {t(g[g['action'] == 'pick'])} | {t(g[g['action'] == 'ban'])} |")
    L += ["", f"- 리허설 (참고): 픽 top5 {spec['rehearsal']['pick_top5']:.1%} · 밴 {spec['rehearsal']['ban_top5']:.1%} / 기준선 픽 {spec['rehearsal']['baseline_pick_top5']:.1%} · 밴 {spec['rehearsal']['baseline_ban_top5']:.1%}", "",
          "## 단계별 나눔 (A 조건)", "", "| 나눔 | 픽 | 밴 |", "|---|---|---|"]
    A = R[R["condition"] == "A"]
    for nm, m_ in (("플레이인", A["stage"] == "플레이인"), ("플레이오프", A["stage"] == "플레이오프"),
                   ("1세트", A["game_n"] == 1), ("2세트 이상", A["game_n"] >= 2),
                   ("강팀 (T1·Gen.G·한화·DK·KT)", A["team"].isin(V6.STRONG)), ("T1", A["team"] == "T1")):
        g = A[m_]
        L.append(f"| {nm} | {t(g[g['action'] == 'pick'])} | {t(g[g['action'] == 'ban'])} |")
    ph = lambda s: "1밴" if s <= 6 else ("1픽" if s <= 12 else ("2밴" if s <= 16 else "2픽"))  # noqa: E731
    L += ["", "## 페이즈별 top5 (A 조건 / 기준선)", "", "| 페이즈 | M4 | 기준선 |", "|---|---|---|"]
    for p_ in ("1밴", "1픽", "2밴", "2픽"):
        g1 = A[A["step"].map(ph) == p_]
        g0 = R[(R["condition"] == "기준선") & (R["step"].map(ph) == p_)]
        L.append(f"| {p_} | {(g1['rank'] <= 5).mean():.1%} | {(g0['rank'] <= 5).mean():.1%} |")
    L += ["", "## 팀별 픽 top5 (A 조건)", "", "| 팀 | 픽 단계 | top5 |", "|---|---|---|"]
    for tm, g in A[A["action"] == "pick"].groupby("team"):
        L.append(f"| {tm} | {len(g)} | {(g['rank'] <= 5).mean():.1%} |")
    L += ["", "## P5 경기 전 포지션 (상위 1 · 3 · 5)", "", f"- 전체 {t(P)}  (리허설 19.5 · 49.3 · 66.5)", ""]
    (VOUT / "m4_validation.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("검증 끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    if "--parity" in sys.argv:
        parity()
    elif "--run" in sys.argv and "--once" in sys.argv:
        if (VOUT / "m4_validation.md").exists():
            raise SystemExit("이미 검증했음 (m4/validation/m4_validation.md). 최종 검증은 한 번만 — 다시 돌리지 않음")
        run()
    else:
        print(__doc__)
