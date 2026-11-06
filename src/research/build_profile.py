"""S6. 대조·프로필 초안 — Claude Code 구독(하위 에이전트 1개)으로 실행하는 하네스.

  prepare  S5 결과(passed + needs_review)에 번호를 매겨 claims_used.csv 를 만들고,
           선수 정보 + 기록 기준표 + 주장 표 + 기존 스타일 카드를 입력 파일 하나로 묶음
           (에이전트가 한 번 읽고 한 번 쓰도록 → 도구 호출·토큰 최소)
  (에이전트) research/config/s6_prompt.md 를 따라 work/{player}_s6_input.md → {player}_draft.md
  check    초안의 [#n] 이 claims_used.csv 에 있는지, † 표시가 needs_review 와 맞는지,
           근거 칸에 커뮤니티 번호가 섞이지 않았는지 확인

출력  research/profiles/{player}_claims_used.csv, work/{player}_s6_input.md, {player}_draft.md, {player}_check.txt

사용
  python build_profile.py prepare --player Oner
  python build_profile.py check --player Oner
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter

import pandas as pd

from common import DATA, RESEARCH, get_logger, players, read_jsonl, write_csv, slug, targets
from patch_windows import windows

log = get_logger("S6")
PROFILES = RESEARCH / "profiles"
WORK = PROFILES / "work"
QUOTE_CHARS = 80  # 기사·인터뷰 인용만 이 길이로 잘라 넣음 (커뮤니티는 주장 요약만 → 토큰 절약)

EXPERT = {"사실", "본인", "기자·전문가"}
COMMUNITY_GRADE = {"inven": "인벤", "youtube_lck": "일반 시청자", "youtube_team": "팬", "youtube_analysis": "분석 채널",
                   "opgg": "OP.GG"}
FIELDS = ["n", "claim_id", "status", "grade", "source_type", "speaker", "stance", "date", "patch", "aspect", "claim",
          "quote", "source_url"]
MIN_CELL = 3  # 패치×등급 칸의 주장이 이보다 적으면 "표본 부족"
SIGN = {"positive": "+", "negative": "−", "mixed": "±", "sarcastic": "~"}


def grade(c: dict) -> str:
    """CLAUDE.md 6-1 출처 등급."""
    if c["source_type"] in COMMUNITY_GRADE:
        return COMMUNITY_GRADE[c["source_type"]]
    if c["speaker"] == "self":
        return "본인"
    if c["stance"] == "neutral_fact":
        return "사실"
    return "기자·전문가"


def claims_used(pid: str) -> list[dict]:
    rows = [c for c in read_jsonl(RESEARCH / "verified" / f"{slug(pid)}.jsonl") if c["status"] in ("passed", "needs_review")]
    order = list(COMMUNITY_GRADE.values())
    rows.sort(key=lambda c: (grade(c) in COMMUNITY_GRADE.values(),
                             order.index(grade(c)) if grade(c) in order else 0, c["date"]))
    for i, c in enumerate(rows, 1):
        c["n"] = i
        c["grade"] = grade(c)
    return rows


TEAM_KO = {"Gen.G": "젠지", "Hanwha Life Esports": "한화생명", "T1": "T1", "KT Rolster": "KT", "Dplus Kia": "디플러스기아",
           "Nongshim RedForce": "농심", "BNK FEARX": "피어엑스", "HANJIN BRION": "브리온", "Kiwoom DRX": "DRX",
           "DN SOOPers": "DN수퍼스"}


def team_cards(team: str) -> str:
    out = []
    for src, label in (("inven_2026", "인벤"), ("youtube_2026", "유튜브")):
        d = pd.read_csv(DATA / src / "exports" / "team_cards.csv")
        r = d[d["team"] == TEAM_KO.get(team, team)]
        if not r.empty:
            r = r.iloc[0].fillna("-")
            out.append(f"- {label}: 잘함 {r['good']} / 못함 {r['bad']} / 화제 {r['hot']} / 핵심 선수 {r['key_players']}")
    return "\n".join(out) or "- (없음)"


def stats_path(pid: str, ext: str):
    kind = targets()[pid]["kind"]
    return PROFILES / ("team_stats" if kind == "team" else "stats") / f"{slug(pid)}.{ext}"


def style_cards(pid: str) -> str:
    if targets()[pid]["kind"] == "team":
        return team_cards(pid)
    out = []
    for src, label in (("inven_2026", "인벤"), ("youtube_2026", "유튜브")):
        d = pd.read_csv(DATA / src / "exports" / "style_cards.csv")
        r = d[d["player"] == pid]
        if r.empty:
            continue
        r = r.iloc[0].fillna("-")
        out.append(f"- {label}: 강점 {r['strengths']} / 약점 {r['weaknesses']} / 화제 {r['hot']} / "
                   f"대표 챔피언 {r['signature']} / 최고 {r['best_pick']} / 최저 {r['worst_pick']}")
    return "\n".join(out) or "- (없음)"


def form_table(pid: str, rows: list[dict]) -> str:
    """3. 폼 섹션용 패치별 집계 (코드가 셈, LLM 은 옮겨 쓰기만). 다른 섹션은 패치로 나누지 않음."""
    st = json.loads(stats_path(pid, "json").read_text(encoding="utf-8"))
    rec = {p["patch"]: p for p in (st.get("patches") or st.get("by_patch") or [])}
    grades = ["사실", "본인", "기자·전문가", *COMMUNITY_GRADE.values()]
    L = ["| 패치 | 기간(KST) | 경기 | 승률 | KDA | 15분 골드 차 | 주장 수 | 등급별 입장 (+긍정 −부정 ±혼합 ~비꼼) |",
         "|---|---|---|---|---|---|---|---|"]
    for patch, a, b in windows("research"):
        r = rec.get(patch)
        cs = [c for c in rows if c.get("patch") == patch and c["stance"] != "neutral_fact"]
        parts = []
        for g in grades:
            cnt = Counter(c["stance"] for c in cs if c["grade"] == g)
            n = sum(cnt.values())
            if n:
                txt = " ".join(f"{SIGN[k]}{v}" for k, v in cnt.items() if k in SIGN)
                parts.append(f"{g} {txt}" + (" (표본 부족)" if n < MIN_CELL else ""))
        if r and "kda" in r:  # 선수
            rec_txt = f"{r['games']} | {r['winrate']:.0%} | {r['kda']} | {r['golddiffat15']:+d}"
        elif r:  # 팀 (승률은 이미 문자열, KDA·골드 차 대신 그 패치 많이 한 픽)
            rec_txt = f"{r['games']} | {r['winrate']} | - | 픽: {r['top_picks']}"
        else:
            rec_txt = "0 | - | - | -"
        L.append(f"| {patch} | {a[5:]}~{b[5:]} | {rec_txt} | {len(cs)} | {' · '.join(parts) or '-'} |")
    return "\n".join(L)


def cmd_prepare(pid: str):
    pcfg = targets()[pid]
    rows = claims_used(pid)
    write_csv(PROFILES / f"{slug(pid)}_claims_used.csv", rows, FIELDS)
    lines = ["| # | 등급 | 입장 | 날짜 | 패치 | 측면 | 주장 | 인용(기사·인터뷰만, 앞부분) |",
             "|---|---|---|---|---|---|---|---|"]
    for c in rows:
        mark = " (검토필요)" if c["status"] == "needs_review" else ""
        q = c["quote"].replace("\n", " ").replace("|", "/")
        q = (q[:QUOTE_CHARS] + "…" if len(q) > QUOTE_CHARS else q) if c["grade"] in EXPERT else ""
        g = c["grade"] + ("(영문)" if c["source_type"].endswith("_en") else "")
        lines.append(f"| {c['n']}{mark} | {g} | {c['stance']} | {c['date']} | {c.get('patch') or '-'} | {c['aspect']} | "
                     f"{c['claim'].replace('|', '/')} | {q} |")
    stats = stats_path(pid, "md").read_text(encoding="utf-8")
    prompt = "research/config/" + ("s6_team_prompt.md" if pcfg["kind"] == "team" else "s6_prompt.md")
    bundle = "\n".join([
        f"# S6 입력 — {pcfg['name_ko']} ({pid})", "",
        f"- 대상: {pcfg['name_ko']} / {pid} / 본명 {pcfg['real_name']} / {pcfg['team']} {pcfg['position']}",
        f"- 출력 경로: {PROFILES / f'{slug(pid)}_draft.md'}",
        f"- 지시문: {prompt}",
        f"- 주장 {len(rows)}개 (검토필요 {sum(c['status'] == 'needs_review' for c in rows)}개)", "",
        "## A. 기록 기준표", "", stats, "",
        "## A2. 패치별 폼·여론 집계 (코드가 셈 — 3. 폼 섹션에만 사용)", "", form_table(pid, rows), "",
        "## B. 기존 스타일 카드 (사전 기반 파이프라인, 비교 대상)", "", style_cards(pid), "",
        "## C. 주장 표 (S5 통과 + 검토필요)", "", *lines, ""])
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / f"{slug(pid)}_s6_input.md"
    path.write_text(bundle, encoding="utf-8")
    log.info("%s: 주장 %d개 → %s (%s자)", pid, len(rows), path, f"{len(bundle):,}")


def cmd_check(pid: str):
    rows = {c["n"]: c for c in claims_used(pid)}
    path = PROFILES / f"{slug(pid)}_draft.md"
    draft = path.read_text(encoding="utf-8")
    # † 표시는 S5 status 로 기계적으로 정해지므로 코드가 맞춰 줌 (LLM 이 빠뜨리는 경우가 많음, 다시 돌리지 않음)
    fixed = re.sub(r"\[#(\d+)†?\]", lambda m: f"[#{m.group(1)}†]" if rows.get(int(m.group(1)), {}).get("status") == "needs_review"
                   else f"[#{m.group(1)}]", draft)
    if fixed != draft:
        path.write_text(fixed, encoding="utf-8")
        draft = fixed
    refs = re.findall(r"\[#(\d+)(†?)\]", draft)
    problems = []
    for n, dag in refs:
        c = rows.get(int(n))
        if c is None:
            problems.append(f"[#{n}] 없는 번호")
        elif (c["status"] == "needs_review") != bool(dag):
            problems.append(f"[#{n}] † 표시 불일치 (status={c['status']})")
    # 근거 칸(표의 둘째 칸)에 커뮤니티 등급 번호가 섞였는지
    for line in draft.splitlines():
        cells = [x.strip() for x in line.split("|")]
        if len(cells) >= 6 and not set(cells[1]) <= set("-: "):
            for n in re.findall(r"\[#(\d+)", cells[2]):
                c = rows.get(int(n))
                if c and c["grade"] not in EXPERT and not line.startswith("| 시기"):
                    problems.append(f"[#{n}] 근거 칸에 커뮤니티({c['grade']}) 번호")
    used = {int(n) for n, _ in refs}
    report = [f"{pid} 초안 점검", f"- 인용 번호 {len(refs)}개 (서로 다른 {len(used)}개) / 주장 표 {len(rows)}개 중 사용 {len(used) / len(rows):.0%}",
              f"- 문제 {len(problems)}건", *[f"  - {p}" for p in problems]]
    (PROFILES / f"{slug(pid)}_check.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    log.info("\n%s", "\n".join(report))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "check"):
        sub.add_parser(name).add_argument("--player", required=True)
    a = ap.parse_args()
    {"prepare": cmd_prepare, "check": cmd_check}[a.cmd](a.player)


if __name__ == "__main__":
    main()
