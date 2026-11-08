"""리서치 추가 검증 — 프로필 주장 ↔ 경기 기록 대조 (2026-11-08)

"이 출처들이 이렇게 본다"(S4~S5 주장)가 실제 기록과 같은 방향인지 숫자로 봄. 판정 기준이 아니라 결과 해석용.
  A 라인전   : 선수별 '라인' 측면 주장의 긍정 − 부정 ↔ ① 선수 평소 라인 골드 수준 (m5_skill: 15분 라인 골드에서 챔피언·상대 챔피언·상대 선수·팀 Elo 를 뺀 몫)
                                                ② 대회 LCK 15분 골드 차 평균 (OE golddiffat15, 08-25 까지)
  B 챔피언   : 주장 문장에 챔피언 이름이 하나 나오는 (선수, 챔피언)의 긍정 − 부정 ↔ 선수 × 챔피언 숙련 편차 δ (m5_skill, 15분 골드)
  C 팀 운영  : 팀 주장 중 '초반' 측면 ↔ M3 초반 수행 (실제 15분 골드 − 조합 기대), '후반 · 운영 · 한타' 측면 ↔ M3 굳히기 (실제 승률 − 15분 골드 기대)
  D 전반 평가: 선수별 전체 긍정 − 부정 ↔ 선수 라인 골드 수준, 팀 승률
  - 주장: research/verified (통과 + 검토 필요). 입장 positive = +1, negative · sarcastic = −1, mixed · neutral_fact 제외
  - 출처 묶음: 기자·본인 (article · article_en · interview · interview_en) / 커뮤니티 (inven · opgg) / 유튜브 (lck · team · analysis)
  - 지표: 스피어만 순위 상관 (선수·팀 사이), 방향 일치율 (양쪽이 뚜렷한 경우만), 순열 p (5,000번)
  - 기간: 주장·기록 모두 리서치 기간 (08-25 까지). 원문은 결과에 넣지 않음 (집계만)
출력: research/eval/record_check.md, record_check_players.csv, record_check_champions.csv

    python src/research/record_check.py
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "pipeline"))
import m1_comp as M1  # noqa: E402

OUT = ROOT / "research" / "eval"
POS_KO = {"탑": "TOP", "정글": "JUNGLE", "미드": "MIDDLE", "원딜": "BOTTOM", "서폿": "UTILITY"}
GROUP = {"article": "기자·본인", "article_en": "기자·본인", "interview": "기자·본인", "interview_en": "기자·본인",
         "inven": "커뮤니티", "opgg": "커뮤니티", "youtube_lck": "유튜브", "youtube_team": "유튜브", "youtube_analysis": "유튜브"}
SIGN = {"positive": 1, "negative": -1, "sarcastic": -1}
RNG = np.random.default_rng(0)


def load_claims():
    rows = []
    for f in glob.glob(str(ROOT / "research" / "verified" / "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            r = json.loads(line)
            if r.get("status") in ("passed", "needs_review") and r.get("stance") in SIGN:
                rows.append({"target": r["player"], "src": GROUP.get(r["source_type"], r["source_type"]), "aspect": r.get("aspect") or "",
                             "claim": r.get("claim") or "", "s": SIGN[r["stance"]]})
    return pd.DataFrame(rows)


def net(g, min_n=3):
    """(긍정 − 부정) / 개수, 개수 min_n 미만이면 NaN."""
    n = len(g)
    return (g["s"].sum() / n) if n >= min_n else np.nan


def perm_p(x, y, n=5000):
    x, y = np.asarray(x, float), np.asarray(y, float)
    r0 = spearmanr(x, y).statistic
    rs = np.array([spearmanr(x, RNG.permutation(y)).statistic for _ in range(n)])
    return float(r0), float((np.abs(rs) >= abs(r0)).mean())


def corr_row(name, df, a, b, clear_a=0.34, clear_b=None):
    d = df[[a, b]].dropna()
    if len(d) < 5:
        return f"| {name} | {len(d)} | — | — | — |"
    r, p = perm_p(d[a], d[b])
    cb = clear_b if clear_b is not None else d[b].abs().median()
    m = (d[a].abs() >= clear_a) & (d[b].abs() >= cb)
    agree = (np.sign(d.loc[m, a]) == np.sign(d.loc[m, b])).mean() if m.sum() else np.nan
    return f"| {name} | {len(d)} | {r:+.2f} (p {p:.3f}) | {agree:.0%} ({int(m.sum())}) | |" if m.sum() else f"| {name} | {len(d)} | {r:+.2f} (p {p:.3f}) | — | |"


def main():
    OUT.mkdir(exist_ok=True)
    P = yaml.safe_load(open(ROOT / "research" / "config" / "players.yaml", encoding="utf-8"))["players"]
    T = yaml.safe_load(open(ROOT / "research" / "config" / "teams.yaml", encoding="utf-8"))["teams"]
    cl = load_claims()
    sk = pd.read_csv(ROOT / "data" / "processed" / "2026" / "m5" / "m5_skill.csv")
    level = sk.drop_duplicates("player").set_index("player")["player_level_gold"]
    oe = pd.read_csv(M1.OE_FILE, low_memory=False, usecols=["league", "date", "playername", "teamname", "position", "golddiffat15", "result"])
    oe = oe[(oe["league"] == "LCK") & (oe["date"].str[:10] <= "2026-08-25")]
    gd15 = oe[oe["position"] != "team"].groupby("playername")["golddiffat15"].mean()
    team_wr = oe[oe["position"] == "team"].groupby("teamname")["result"].mean()
    m3 = pd.read_csv(ROOT / "data" / "processed" / "2026" / "m3" / "m3_team_execution.csv").set_index("team")

    pl = cl[cl["target"].isin(P)]
    L = ["# 리서치 주장 ↔ 경기 기록 대조", "",
         f"- 주장 {len(cl):,}건 (통과 + 검토 필요, 입장 긍정·부정·비꼼만. 혼합·사실 제외), 선수 대상 {len(pl):,}건",
         "- 순위 상관 = 스피어만 (선수·팀 사이), p = 순열 5,000번. 방향 일치 = 양쪽 값이 뚜렷한 경우(주장 |순 평가| ≥ 0.34, 기록 |값| ≥ 중앙값)만, 괄호 = 개수",
         "- 결과는 \"출처들의 시각이 기록과 같은 방향인가\" — 다르다고 주장이 틀렸다는 뜻은 아님 (출처가 보는 것과 기록이 재는 것이 다를 수 있음)", ""]

    # ---------------- A 라인전
    lane = pl[pl["aspect"].str.contains("라인")]
    rows = []
    for p_, info in P.items():
        g = lane[lane["target"] == p_]
        r = {"player": p_, "position": info.get("position"), "team": info.get("team"), "lane_n": len(g), "lane_net": net(g),
             "level_gold": level.get(p_, np.nan), "gd15": gd15.get(p_, np.nan), "all_n": int((pl["target"] == p_).sum()),
             "all_net": net(pl[pl["target"] == p_], 10), "team_wr": team_wr.get(info.get("team"), np.nan)}
        for grp in ("기자·본인", "커뮤니티", "유튜브"):
            r[f"lane_net_{grp}"] = net(g[g["src"] == grp])
            r[f"all_net_{grp}"] = net(pl[(pl["target"] == p_) & (pl["src"] == grp)], 10)
        rows.append(r)
    PD = pd.DataFrame(rows)
    PD.to_csv(OUT / "record_check_players.csv", index=False, encoding="utf-8-sig")
    L += ["## A. 라인전 — 선수별 '라인' 측면 주장 ↔ 라인 골드", "",
          f"- 라인 측면 주장 {len(lane):,}건, 3건 이상인 선수 {int(PD['lane_net'].notna().sum())}명", "",
          "| 비교 | 선수 수 | 순위 상관 | 방향 일치 | |", "|---|---|---|---|---|",
          corr_row("라인전 평가 ↔ 선수 평소 라인 골드 (통제)", PD, "lane_net", "level_gold"),
          corr_row("라인전 평가 ↔ 대회 15분 골드 차 (통제 없음)", PD, "lane_net", "gd15")]
    for grp in ("기자·본인", "커뮤니티", "유튜브"):
        L.append(corr_row(f"  {grp}만 ↔ 평소 라인 골드", PD, f"lane_net_{grp}", "level_gold"))
    # ---------------- D 전반 평가
    PD["team_wr_c"] = PD["team_wr"] - 0.5      # 방향 = 승률 50% 위·아래
    L += ["", "## D. 전반 평가 — 선수별 전체 주장(10건 이상)의 긍정 − 부정", "",
          "- 방향 일치: 전반 평가 |순 평가| ≥ 0.2, 팀 승률은 50% 에서 5%p 이상 떨어진 경우", "",
          "| 비교 | 선수 수 | 순위 상관 | 방향 일치 | |", "|---|---|---|---|---|",
          corr_row("전반 평가 ↔ 선수 평소 라인 골드", PD, "all_net", "level_gold", clear_a=0.2),
          corr_row("전반 평가 ↔ 팀 승률", PD, "all_net", "team_wr_c", clear_a=0.2, clear_b=0.05)]
    for grp in ("기자·본인", "커뮤니티", "유튜브"):
        L.append(corr_row(f"  {grp}만 ↔ 팀 승률", PD, f"all_net_{grp}", "team_wr_c", clear_a=0.2, clear_b=0.05))

    # ---------------- B 챔피언
    al = pd.read_csv(ROOT / "research" / "config" / "champion_aliases.csv")
    al = al[al["alias"].str.len() >= 2]
    pats = [(r.champion_id, re.compile(r"(?<![가-힣A-Za-z])" + re.escape(r.alias) + (r"(?![A-Za-z])" if r.kind == "en" else ""))) for r in al.itertuples()]
    ch_rows = []
    for r in pl.itertuples():
        hit = {cid for cid, pt in pats if pt.search(r.claim)}
        if len(hit) == 1:
            ch_rows.append({"player": r.target, "champ": hit.pop(), "s": r.s, "src": r.src})
    CH = pd.DataFrame(ch_rows)
    sk_i = sk.set_index(["player", "key"])
    agg = []
    for (p_, c), g in CH.groupby(["player", "champ"]):
        pos = POS_KO.get(P[p_].get("position"))
        k = (p_, f"{c}@{pos}")
        x = sk_i.loc[k] if k in sk_i.index else None
        agg.append({"player": p_, "champ": c, "n": len(g), "net": g["s"].mean(), "delta_gold": (x["delta_gold"] if x is not None else np.nan),
                    "games": (int(x["n_pro"] + x["n_solo"]) if x is not None else 0), "n_pro": (int(x["n_pro"]) if x is not None else 0)})
    CA = pd.DataFrame(agg)
    CA.to_csv(OUT / "record_check_champions.csv", index=False, encoding="utf-8-sig")
    L += ["", "## B. 챔피언 — 주장 문장에 챔피언이 하나 나온 (선수, 챔피언) ↔ 숙련 편차 δ (15분 골드)", "",
          f"- 챔피언 하나가 나온 선수 주장 {len(CH):,}건 → (선수, 챔피언) {len(CA)}쌍, 그중 δ 가 있는 쌍 {int(CA['delta_gold'].notna().sum())}", "",
          "| 범위 | 쌍 | 순위 상관 | 방향 일치 (주장 뚜렷 · |δ| ≥ 100골드) |", "|---|---|---|---|"]
    for nm, m in (("전체 (δ 있음)", CA["delta_gold"].notna()),
                  ("주장 2건 이상", CA["delta_gold"].notna() & (CA["n"] >= 2)),
                  ("판수 10 이상 (대회 + 솔랭)", CA["delta_gold"].notna() & (CA["games"] >= 10)),
                  ("주장 2건 이상 · 판수 10 이상", CA["delta_gold"].notna() & (CA["n"] >= 2) & (CA["games"] >= 10))):
        d = CA[m]
        if len(d) < 5:
            L.append(f"| {nm} | {len(d)} | — | — |")
            continue
        r, p = perm_p(d["net"], d["delta_gold"])
        mm = (d["net"].abs() >= 0.34) & (d["delta_gold"].abs() >= 100)
        ag = (np.sign(d.loc[mm, "net"]) == np.sign(d.loc[mm, "delta_gold"])).mean() if mm.sum() else np.nan
        L.append(f"| {nm} | {len(d)} | {r:+.2f} (p {p:.3f}) | {ag:.0%} ({int(mm.sum())}) |")
    for grp in ("기자·본인", "커뮤니티", "유튜브"):
        g2 = CH[CH["src"] == grp].groupby(["player", "champ"])["s"].mean().rename("net").reset_index().merge(CA[["player", "champ", "delta_gold", "games"]], on=["player", "champ"])
        g2 = g2[g2["delta_gold"].notna() & (g2["games"] >= 10)]
        if len(g2) >= 5:
            r, p = perm_p(g2["net"], g2["delta_gold"])
            mm = (g2["net"].abs() >= 0.34) & (g2["delta_gold"].abs() >= 100)
            ag = (np.sign(g2.loc[mm, "net"]) == np.sign(g2.loc[mm, "delta_gold"])).mean() if mm.sum() else np.nan
            L.append(f"| {grp}만 (판수 10 이상) | {len(g2)} | {r:+.2f} (p {p:.3f}) | {ag:.0%} ({int(mm.sum())}) |")

    # ---------------- C 팀 운영
    tm = cl[~cl["target"].isin(P)].copy()
    tname = {k.replace(" ", "_"): k for k in T}
    tm["team"] = tm["target"].map(lambda x: x if x in T else tname.get(x, x))
    trows = []
    for t in T:
        g = tm[tm["team"] == t]
        e = g[g["aspect"].str.contains("초반")]
        lt = g[g["aspect"].str.contains("후반|운영|한타")]
        trows.append({"team": t, "early_n": len(e), "early_net": net(e, 2), "late_n": len(lt), "late_net": net(lt, 2),
                      "all_net": net(g, 10), "early_exec": m3["early_exec"].get(t, np.nan), "conv": m3["conv"].get(t, np.nan),
                      "win": team_wr.get(t, np.nan)})
    TD = pd.DataFrame(trows)
    L += ["", "## C. 팀 — 팀 주장 ↔ M3 팀 수행력 (10팀, 표본이 작아 참고)", "",
          "| 팀 | '초반' 주장 (순 평가) | M3 초반 수행 (천 골드) | '후반·운영·한타' 주장 (순 평가) | M3 굳히기·역전 (%p) | 전체 순 평가 | 승률 |", "|---|---|---|---|---|---|---|"]
    for r in TD.sort_values("win", ascending=False).itertuples():
        f = lambda v, fmt: ("—" if pd.isna(v) else format(v, fmt))  # noqa: E731
        L.append(f"| {r.team} | {r.early_n}건 {f(r.early_net, '+.2f')} | {f(r.early_exec, '+.2f')} | {r.late_n}건 {f(r.late_net, '+.2f')} | "
                 f"{f(100 * r.conv, '+.1f')} | {f(r.all_net, '+.2f')} | {f(r.win, '.0%')} |")
    L += ["", "| 비교 (10팀) | 팀 수 | 순위 상관 |", "|---|---|---|"]
    for nm, a, b in (("'초반' 평가 ↔ M3 초반 수행", "early_net", "early_exec"), ("'후반·운영·한타' 평가 ↔ M3 굳히기·역전", "late_net", "conv"),
                     ("전체 평가 ↔ 승률", "all_net", "win")):
        d = TD[[a, b]].dropna()
        if len(d) >= 5:
            r, p = perm_p(d[a], d[b])
            L.append(f"| {nm} | {len(d)} | {r:+.2f} (p {p:.3f}) |")
    (OUT / "record_check.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
