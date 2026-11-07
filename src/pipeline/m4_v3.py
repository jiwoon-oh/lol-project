"""M4 2차 시험 — C2 반영 + 강팀 보강 (2026-11-07, docs/PIPELINE.md "M4")

1) C2(선픽·후픽 팀 밴 습관)를 밴 모델에 넣음 (m4_v2 에서 효과 확인)
2) 강팀(T1 · Gen.G · 한화생명 · DK · KT) 픽 예측 보강 시험
   R1 리서치 선호: 선수의 리서치 주장(인용 확인 통과 + 검토 필요)에서 그 챔피언이 언급된 수 (그 경기 전날까지 날짜)
       픽 = 우리 빈 포지션 선수, 밴 = 상대 빈 포지션 선수. 챔피언 이름 사전 = research/config/champion_aliases.csv
   국제·컵 대회 포함 변형: KeSPA Cup · EWC · MSI · FST 의 대회 기록(08-25 까지)을 메타·응수·선수 풀·대체 집계에 더함
       (CLAUDE.md 0장의 대회 자료 범위 'LCK·LCKC·LPL·LEC·LCS' 를 넓히는 것 → 채택은 사용자 결정)
3) 강팀 오답 진단: 강팀이 실제로 고른 챔피언이 그 선수의 대회 첫 판이었나, 메타 순위 밖이었나 등
리허설 = LCK 26.14 · 26.15 · RTM. 최종 모델은 범위를 넓히지 않은 변형 + C2 → m4/m4_model.json 갱신 (넓힌 변형은 m4_model_intl.json)

    python src/pipeline/m4_v3.py
"""
from __future__ import annotations

import glob
import json
import sys
import time
from collections import defaultdict
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
import m4_v2 as V2  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = B.OUT
STRONG = ["T1", "Gen.G", "Hanwha Life Esports", "Dplus Kia", "KT Rolster"]
EXTRA_LEAGUES = ["KeSPA Cup", "EWC", "MSI", "FST"]
BASE_LEAGUES = list(M1.LEAGUES)


def research_mentions():
    """선수 → [(날짜, 챔피언)] (리서치 주장 인용·요약에 나온 챔피언)."""
    import m2_community as MC
    find = MC.build_matcher(MC.aliases())
    out = defaultdict(list)
    for f in glob.glob(str(H.ROOT / "research" / "verified" / "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            c = json.loads(line)
            if not (c.get("passed") or c.get("status") == "needs_review"):
                continue
            for ch in find(f"{c.get('quote', '')} {c.get('claim', '')}"):
                out[c["player"]].append((str(c.get("date", ""))[:10], ch))
    return out


def build(leagues, wv, rm):
    M1.LEAGUES = leagues  # 대회 자료 범위 (불러오는 함수들이 실행 때 읽음)
    a, pro_pick, pro_ban, lineup, sq, champs = B.load()
    cid = B.cid_map()
    ev = F.pro_events(cid)
    pgames = V.pro_games(cid)
    tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
    days = sorted(a["day"].unique())
    ds_cache = {D: F.DayStats(D, ev, pro_pick, sq, tmeta, champs) for D in days}
    ex_cache = {D: V.day_extras(D, pro_pick, sq) for D in days}
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    all_games = sorted(gday.index, key=lambda x: gday[x])
    rows = V.build_rows(a, all_games, ds_cache, ex_cache, lineup, champs, pgames, {}, wv)
    rows = [list(r) + [None] for r in rows]
    V2.add_extra(rows, a, {}, {}, {}, {})          # C2 만 쓰므로 나머지 표는 비움
    # R1 리서치 선호 + 진단용 칸
    for r in rows:
        ctx = r[8]
        D = ctx["D"]
        game = a.loc[a["GameId"] == r[1], "RiotPlatformGameId"].iloc[0]
        team = r[7] if r[2] == "pick" else ctx["opp_team"]
        roles = ctx["my_open"] if r[2] == "pick" else ctx["op_open"]
        players = [p for p in (F._player(lineup, game, team, ro) for ro in roles) if p]
        cnt = defaultdict(int)
        for p in players:
            for d, ch in rm.get(p, []):
                if d and d < D:
                    cnt[ch] += 1
        r1 = np.array([[np.log1p(cnt.get(c, 0))] for c in ctx["cands"]], np.float32)
        r[9] = np.hstack([r[9], r1])
    M1.LEAGUES = BASE_LEAGUES
    return rows, a, gday, all_games


EX_NAMES = V2.EXTRA + ["r1_research"]


def arrays(rows, feats, extra):
    ix = [V.ALL.index(k) for k in feats]
    ex = [EX_NAMES.index(k) for k in extra]
    sid = np.concatenate([np.full(len(r[3]), r[0]) for r in rows])
    y = np.concatenate([r[3] for r in rows]).astype(float)
    X = np.vstack([np.hstack([r[4][:, ix], r[9][:, ex]]) for r in rows]).astype(float)
    return sid, y, X


CONFIGS = {"M4 1차 + C2": {"pick": [], "ban": ["c2_hab"]},
           "+ R1 리서치 선호": {"pick": ["r1_research"], "ban": ["c2_hab", "r1_research"]}}


def evaluate(rows, sets, gday, variant):
    res, models = [], {}
    for name, gids in list(sets.items()) + [("final", None)]:
        if gids is None:
            tr, te = rows, []
        else:
            start = min(gday[g] for g in gids)
            tr = [r for r in rows if gday[r[1]] < start]
            te = [r for r in rows if r[1] in gids]
        for cname, ex in CONFIGS.items():
            for action in ("pick", "ban"):
                feats = MM.PICK_FEATS if action == "pick" else MM.BAN_FEATS
                for lo, hi in MM.SEGMENTS[action]:
                    trg = [r for r in tr if r[2] == action and lo <= r[5] <= hi]
                    sid, y, X = arrays(trg, feats, ex[action])
                    mu, sd = X.mean(0), X.std(0) + 1e-9
                    b = MM.fit_off(sid, y, (X - mu) / sd, np.zeros(len(y)), 1.0)
                    if gids is None:
                        models.setdefault(cname, {}).setdefault(action, []).append(
                            {"steps": [lo, hi], "features": feats + ex[action], "mu": mu.tolist(), "sd": sd.tolist(), "coef": b.tolist()})
                        continue
                    teg = [r for r in te if r[2] == action and lo <= r[5] <= hi]
                    s2, y2, X2 = arrays(teg, feats, ex[action])
                    meta = {r[0]: r for r in teg}
                    Xr = {}
                    for k, rk in MM.rank_list(s2, y2, ((X2 - mu) / sd) @ b):
                        r = meta[k]
                        yi = int(np.argmax(r[3]))
                        f = dict(zip(V.ALL, r[4][yi]))
                        res.append({"variant": variant, "set": name, "config": cname, "action": action, "step": r[5], "team": r[7],
                                    "rank": rk, "pl_pro": f["pl_pro"], "pl_recent": f["pl_recent"], "meta": f["meta"],
                                    "meta_rank": f["meta_rank"], "r1": r[9][yi, -1]})
        log.info("  %s %s 끝", variant, name)
    return pd.DataFrame(res), models


def main():
    t0 = time.time()
    wv = MM.win_value_table()
    rm = research_mentions()
    log.info("리서치 언급 선수 %d (%.0f초)", len(rm), time.time() - t0)
    out, models = [], {}
    for variant, leagues in (("5리그", BASE_LEAGUES), ("+ 국제·컵", BASE_LEAGUES + EXTRA_LEAGUES)):
        rows, a, gday, all_games = build(leagues, wv, rm)
        sets = B.eval_sets(a)
        R, m = evaluate(rows, sets, gday, variant)
        out.append(R)
        models[variant] = m
        log.info("%s 끝 (%.0f초)", variant, time.time() - t0)
    R = pd.concat(out, ignore_index=True)
    H.save_csv(R, OUT / "m4_v3_ranks.csv")
    # 최종 모델 저장 (5리그 + C2, R1 은 결과 보고 결정 → 둘 다 저장)
    (OUT / "m4_model_c2.json").write_text(json.dumps(models["5리그"], ensure_ascii=False), encoding="utf-8")
    (OUT / "m4_model_intl.json").write_text(json.dumps(models["+ 국제·컵"], ensure_ascii=False), encoding="utf-8")

    def t(g):
        return f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 3).mean():.1%} · {(g['rank'] <= 5).mean():.1%} · {(1 / g['rank']).mean():.3f}"
    L = ["# M4 2차 시험 — C2 반영 + 강팀 보강", "",
         "- 기준 = M4 1차 공통 + 이길 가치, 밴에 C2. R1 = 리서치 주장 속 챔피언 언급, '+ 국제·컵' = KeSPA Cup·EWC·MSI·FST 대회 기록 포함",
         "- 값 = top1 · top3 · top5 · MRR (리허설 3세트 합)", "", "| 자료 범위 | 설정 | 픽 | 밴 | 강팀 픽 | 나머지 픽 |", "|---|---|---|---|---|---|"]
    for (v, c), g in R.groupby(["variant", "config"], sort=False):
        gp = g[g["action"] == "pick"]
        L.append(f"| {v} | {c} | {t(gp)} | {t(g[g['action'] == 'ban'])} | {t(gp[gp['team'].isin(STRONG)])} | {t(gp[~gp['team'].isin(STRONG)])} |")
    L += ["", "## 팀별 픽 top5", "", "| 팀 | " + " | ".join(f"{v} · {c}" for v, c in R.groupby(["variant", "config"], sort=False).groups) + " |",
          "|---|" + "---|" * R.groupby(["variant", "config"]).ngroups]
    for tm, g in R[R["action"] == "pick"].groupby("team"):
        L.append(f"| {tm}{' (강팀)' if tm in STRONG else ''} | " + " | ".join(
            f"{(gg['rank'] <= 5).mean():.1%}" for _, gg in g.groupby(["variant", "config"], sort=False)) + " |")
    base = R[(R["variant"] == "5리그") & (R["config"] == "M4 1차 + C2") & (R["action"] == "pick")]
    L += ["", "## 강팀 오답 진단 (5리그 · M4 1차 + C2, 픽)", "",
          "- 실제로 고른 챔피언의 성격: 그 선수의 대회 첫 판(그 전 대회 판수 0) 비율, 메타 상위 20 밖 비율, 리서치에서 언급된 적 있는 비율", "",
          "| 집단 | 단계 | top5 | 대회 첫 판 | 메타 20위 밖 | 리서치 언급 있음 | 첫 판일 때 top5 | 첫 판 아닐 때 top5 |", "|---|---|---|---|---|---|---|---|"]
    base = base.assign(first=base["pl_pro"] <= 0, offmeta=base["meta_rank"] < -np.log(20), r1pos=base["r1"] > 0)
    for nm, g in (("강팀", base[base["team"].isin(STRONG)]), ("나머지", base[~base["team"].isin(STRONG)])):
        L.append(f"| {nm} | {len(g)} | {(g['rank'] <= 5).mean():.1%} | {g['first'].mean():.1%} | {g['offmeta'].mean():.1%} | {g['r1pos'].mean():.1%} | "
                 f"{(g.loc[g['first'], 'rank'] <= 5).mean():.1%} | {(g.loc[~g['first'], 'rank'] <= 5).mean():.1%} |")
    (OUT / "m4_v3_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
