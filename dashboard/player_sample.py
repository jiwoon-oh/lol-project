"""FM식 선수 페이지 샘플 (레이아웃 시험용, 지금은 도란만)

    python dashboard/build.py            # data.json 먼저
    python dashboard/player_sample.py    # → dashboard/player_sample_Doran.html

본 대시보드(report.html)는 건드리지 않음. 레이아웃이 확정되면 template.html 의 renderPlayer 로 옮길 것.
- 사진: Leaguepedia 이미지 주소를 그대로 불러옴 (파일로 받지 않음, 인터넷 연결 필요, 실패하면 실루엣)
- 커리어: 2018~2025 = Leaguepedia 선수 통계 페이지(대회별 경기·승, career/{선수}.csv), 2026 = OE 경기 기록 08-25까지
"""
import json, sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PID = sys.argv[1] if len(sys.argv) > 1 else "Doran"
AS_OF = "2026-08-25 23:59"

# 손으로 옮긴 자료 (Leaguepedia, 2026-11-05 확인). 대회별 경기·승은 dashboard/career/{선수}.csv
EXTRA = {
    "Doran": {
        "photo": "https://static.wikia.nocookie.net/lolesports_gamepedia_en/images/d/de/T1_Doran_2026_LCK_Cup.png/revision/latest/scale-to-width-down/440",
        "photo_credit": "Leaguepedia",
        "career": [  # (기간, 표시 이름, csv 팀 코드, OE 팀 이름 = 2026 경기 합산용) 최신순
            ("2024.11 ~ 현재", "T1", "T1", "T1"),
            ("2023.11 ~ 2024.11", "한화생명e스포츠", "HLE", None),
            ("2021.11 ~ 2023.11", "젠지", "GEN", None),
            ("2020.11 ~ 2021.11", "KT 롤스터", "KT", None),
            ("2019.12 ~ 2020.11", "DRX", "DRX", None),
            ("2018.12 ~ 2019.11", "그리핀", "GRF", None),
            ("2018 KeSPA컵", "KSL", "KSL", None),
        ],
    }
}

data = json.loads((ROOT / "dashboard/data.json").read_text(encoding="utf-8"))
p = data["players"][PID]
team = data["teams"][p["team"]]

ros = pd.read_csv(ROOT / "data/processed/2026/f_rosters.csv")
r = ros[ros.ID == PID].drop_duplicates("ID")
birth = None if r.empty or pd.isna(r.iloc[0].Birthdate) else r.iloc[0].Birthdate
country = None if r.empty or pd.isna(r.iloc[0].Country) else r.iloc[0].Country

g = pd.read_csv(ROOT / "data/processed/2026/a_player_games.csv", dtype={"patch": str})
g = g[(g.playername == PID) & (g.date <= AS_OF)]
ex = EXTRA.get(PID, {})
cpath = ROOT / f"dashboard/career/{PID}.csv"
cdf = pd.read_csv(cpath) if cpath.exists() else pd.DataFrame(columns=["team", "games", "wins"])
career = []
for period, name, code, oe_team in ex.get("career", []):
    c = cdf[cdf.team == code]
    gm, w = int(c.games.sum()), int(c.wins.sum())
    if oe_team:  # 2026 = OE 경기 기록, 리서치 기간(08-25)까지만
        x = g[g.teamname == oe_team]
        gm += len(x); w += int(x.result.sum())
    career.append({"period": period, "team": name, "games": gm, "winrate": round(w / gm, 3) if gm else None})

# 레이더 축 범위 = 같은 포지션 리서치 대상의 최소~최대
peers = [q for q in data["players"].values() if q["position"] == p["position"]]
ranges = {}
for a in p["attrs"]:
    vals = [x["value"] for q in peers for x in q["attrs"] if x["key"] == a["key"]]
    ranges[a["key"]] = [min(vals), max(vals)]
page = {
    "p": p,
    "team": {"name": team["name"], "id": team["id"]},
    "birth": birth, "country": country,
    "photo": ex.get("photo"), "photo_credit": ex.get("photo_credit"),
    "career": career, "ranges": ranges, "n_peers": len(peers),
}
tpl = (ROOT / "dashboard/player_sample_template.html").read_text(encoding="utf-8")
blob = json.dumps(page, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
out = ROOT / f"dashboard/player_sample_{PID}.html"  # 스크롤 한 번 버전 (2026-11-05 결정)
out.write_text(tpl.replace("__DATA__", blob), encoding="utf-8")
print("wrote", out.relative_to(ROOT))
for c in career: print(c)
