"""선수 커리어(팀별 경기·승률)와 사진 주소 — Leaguepedia Cargo API

    python dashboard/career.py            # → dashboard/career/careers.json
    python dashboard/build.py             # data.json·t1_scouting.html 에 반영

- 경기: ScoreboardPlayers (Leaguepedia 선수 Statistics 페이지와 같은 원천). 공식 대회 전체(정규·플레이오프·국제전·KeSPA컵·CL 등)
- 기간 끝 = 리서치 기간 끝 2026-08-25 (KST 23:59 = UTC 14:59)
- 개명한 선수는 PlayerRedirects 로 묶음
- 같은 구단이 이름만 바뀐 경우(DAMWON → DWG KIA → Dplus Kia 등)는 연속이면 한 줄로 합침
- 사진: Players.Image (Leaguepedia 대표 사진) → static.wikia.nocookie.net 주소. 파일로 받지 않고 주소만 저장
- API 응답은 data/raw/leaguepedia/ 에 캐시 (src/pipeline/leaguepedia.py 의 Cargo)
"""
import hashlib, json, re, sys
from pathlib import Path
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/pipeline"))
from leaguepedia import Cargo, in_clause  # noqa: E402

UNTIL_UTC = "2026-08-25 14:59:59"
OUT = ROOT / "dashboard/career/careers.json"

# 같은 구단(이름 변경). 키 = 구단 묶음 이름
ORG = {}
for org, names in {
    "T1": ["SK Telecom T1", "SK Telecom T1 K", "SK Telecom T1 S", "SK Telecom T1 2", "T1"],
    "GEN": ["Samsung Galaxy", "KSV Esports", "Gen.G"],
    "DK": ["DAMWON Gaming", "DWG KIA", "Dplus KIA", "Dplus Kia"],
    "DNS": ["Afreeca Freecs", "Kwangdong Freecs", "DN Freecs", "DN SOOPers"],
    "BFX": ["SANDBOX Gaming", "Liiv SANDBOX", "FearX", "FEARX", "BNK FearX", "BNK FEARX"],
    "DRX": ["Longzhu Gaming", "Kingzone DragonX", "DragonX", "DRX", "Kiwoom DRX"],
    "BRO": ["BRION", "Fredit BRION", "OKSavingsBank BRION", "OK BRION", "HANJIN BRION"],
    "NS": ["Team Dynamics", "Nongshim RedForce"],
    "HLE": ["Hanwha Life Esports"],
    "KT": ["KT Rolster"],
}.items():
    for n in names:
        ORG[n.lower()] = org
# 지금 LCK 구단 이름은 한국어로 표시, 그 외는 Leaguepedia 이름 그대로
KO = {"T1": "T1", "Gen.G": "젠지", "Hanwha Life Esports": "한화생명e스포츠", "KT Rolster": "KT 롤스터",
      "Dplus Kia": "디플러스 기아", "Dplus KIA": "디플러스 기아", "Nongshim RedForce": "농심 레드포스",
      "BNK FEARX": "BNK 피어엑스", "BNK FearX": "BNK 피어엑스", "HANJIN BRION": "한진 브리온", "BRION": "브리온",
      "DRX": "DRX", "Kiwoom DRX": "키움 DRX", "DN SOOPers": "DN 수퍼스", "DN Freecs": "DN 프릭스"}


# 구단이 아닌 팀(국가대표·올스타)은 커리어 표에서 뺌
NOT_CLUB = ("national team", "allstars", "all-stars", "all stars", "team fire", "team ice")


def org(team):
    return ORG.get(team.lower(), team)


def photo_url(fn):
    if not fn:
        return None
    fn = fn.strip().replace(" ", "_")
    h = hashlib.md5(fn.encode("utf-8")).hexdigest()
    return f"https://static.wikia.nocookie.net/lolesports_gamepedia_en/images/{h[0]}/{h[:2]}/{fn}/revision/latest/scale-to-width-down/440"


def main():
    players = yaml.safe_load(open(ROOT / "research/config/players.yaml", encoding="utf-8"))["players"]
    ros = pd.read_csv(ROOT / "data/processed/2026/f_rosters.csv").drop_duplicates("ID").set_index("ID")
    link = {pid: ros.loc[pid, "Player"] for pid in players}
    links = sorted(set(link.values()))
    cargo = Cargo()
    common = dict(tables="ScoreboardPlayers=SP, PlayerRedirects=PR", join_on="SP.Link=PR.AllName",
                  group_by="PR.OverviewPage, SP.Team, SP.OverviewPage")
    where = f'{in_clause("PR.OverviewPage", links)} AND SP.DateTime_UTC <= "{UNTIL_UTC}"'
    tot = cargo.query("career_games", fields="PR.OverviewPage=Player, SP.Team=Team, SP.OverviewPage=Tournament, "
                      "COUNT(SP.GameId)=Games, MIN(SP.DateTime_UTC)=First, MAX(SP.DateTime_UTC)=Last",
                      where=where, **common)
    win = cargo.query("career_wins", fields="PR.OverviewPage=Player, SP.Team=Team, SP.OverviewPage=Tournament, COUNT(SP.GameId)=Wins",
                      where=where + ' AND SP.PlayerWin="Yes"', **common)
    img = cargo.query("career_images", tables="PlayerImages", fields="FileName, Link, Tournament",
                      where=in_clause("Link", links))
    t = pd.DataFrame(tot)
    w = pd.DataFrame(win)
    t["Games"] = t.Games.astype(int)
    w["Wins"] = w.Wins.astype(int)
    t = t.merge(w, on=["Player", "Team", "Tournament"], how="left").fillna({"Wins": 0})
    # 가장 최근 대회 사진 (파일 이름의 연도 → 대회 이름 순)
    best = {}
    for r in img:
        y = max([int(x) for x in re.findall(r"20\d\d", r["FileName"] + " " + (r["Tournament"] or ""))] or [0])
        k = (y, r["Tournament"] or "", r["FileName"])
        lk_ = r["Link"].lower()
        if lk_ not in best or k > best[lk_][0]:
            best[lk_] = (k, r["FileName"])
    img = {k: v[1] for k, v in best.items()}
    t = t[~t.Team.str.lower().str.contains("|".join(NOT_CLUB))]
    t["key"] = t.Player.str.lower()

    out = {}
    for pid, lk in link.items():
        d = t[t.key == lk.lower()].sort_values("First")
        cur = players[pid]["team"]
        stints = []
        for _, r in d.iterrows():
            o = org(r.Team)
            if stints and stints[-1]["org"] == o:
                s = stints[-1]
                s["games"] += int(r.Games); s["wins"] += int(r.Wins); s["last"] = max(s["last"], r.Last); s["team_lp"] = r.Team
            else:
                stints.append({"org": o, "team_lp": r.Team, "games": int(r.Games), "wins": int(r.Wins),
                               "first": r.First, "last": r.Last})
        rows = []
        for i, s in enumerate(stints):
            now = i == len(stints) - 1 and org(s["team_lp"]) == org(cur)
            rows.append({"team": KO.get(s["team_lp"], re.sub(r"\s*\(.*?\)$", "", s["team_lp"])), "team_lp": s["team_lp"],
                         "period": f'{s["first"][:7].replace("-", ".")} ~ {"현재" if now else s["last"][:7].replace("-", ".")}',
                         "games": s["games"], "winrate": round(s["wins"] / s["games"], 3) if s["games"] else None})
        out[pid] = {"link": lk, "photo": photo_url(img.get(lk.lower())), "photo_file": img.get(lk.lower()), "career": rows[::-1]}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", OUT.relative_to(ROOT), "players", len(out),
          "no career", [p for p, v in out.items() if not v["career"]], "no photo", [p for p, v in out.items() if not v["photo"]])


if __name__ == "__main__":
    main()
