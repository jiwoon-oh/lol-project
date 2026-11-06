"""대시보드 만들기: research 프로필·기록표 + 경기 기록 → dashboard/data.json → dashboard/t1_scouting.html

    python dashboard/build.py

원문 인용은 넣지 않음 (프로필 요약 문장·기록 수치만). 로스터는 2026-08-25 기준 경기 기록으로 정함.
화면 틀은 dashboard/template.html (__DATA__ 자리에 data.json 이 들어감).
"""
import base64, json, re
from pathlib import Path
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
R = ROOT / "research"
OUT = ROOT / "dashboard/data.json"

players_cfg = yaml.safe_load(open(R / "config/players.yaml", encoding="utf-8"))["players"]
teams_cfg = yaml.safe_load(open(R / "config/teams.yaml", encoding="utf-8"))["teams"]

REF = re.compile(r"\s*\[#\d+†?\](\s*\[#\d+†?\])*")


def clean(s):
    s = REF.sub("", s or "")
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"\s+\(\d+\)", "", s)
    return re.sub(r"\s{2,}", " ", s).strip(" ,·")


def sections(md):
    out, cur = {}, None
    for line in md.splitlines():
        m = re.match(r"^## (.+)$", line)
        if m:
            cur = m.group(1).strip()
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return out


def find(secs, prefix):
    for k, v in secs.items():
        if k.startswith(prefix):
            return v
    return []


def bullets(lines):
    return [clean(l[2:]) for l in lines if l.startswith("- ")]


def table(lines):
    rows = [l for l in lines if l.startswith("|")]
    if len(rows) < 3:
        return []
    head = [c.strip() for c in rows[0].strip("|").split("|")]
    res = []
    for r in rows[2:]:
        cells = [c.strip() for c in r.strip("|").split("|")]
        res.append(dict(zip(head, cells)))
    return res


def file_key(name):
    return name.replace(".", "").replace(" ", "_")


# ---------- standings (regular season, research period) ----------
ms = pd.read_csv(ROOT / "data/processed/2026/f_match_schedule.csv")
ros = pd.read_csv(ROOT / "data/processed/2026/f_rosters.csv")
name_std = dict(zip(ros.Team, ros.team_std))
reg = ms[ms.OverviewPage.isin(["LCK/2026 Season/Rounds 1-2", "LCK/2026 Season/Rounds 3-4"])]
reg = reg[reg.date_kst <= "2026-08-25 23:59"]
tab = {}
for _, m in reg.iterrows():
    t1, t2 = name_std.get(m.Team1, m.Team1), name_std.get(m.Team2, m.Team2)
    w = name_std.get(m.winner_team, m.winner_team)
    for t, s, o in ((t1, m.Team1Score, m.Team2Score), (t2, m.Team2Score, m.Team1Score)):
        d = tab.setdefault(t, {"w": 0, "l": 0, "sw": 0, "sl": 0})
        d["w" if w == t else "l"] += 1
        d["sw"] += int(s); d["sl"] += int(o)
standings = sorted(
    [{"team": t, **d, "diff": d["sw"] - d["sl"]} for t, d in tab.items()],
    key=lambda x: (-x["w"], -x["diff"]),
)
for i, s in enumerate(standings):
    s["rank"] = i + 1

# ---------- players ----------
birth = ros.drop_duplicates("ID").set_index("ID")[["NativeName", "Country", "Birthdate"]]
POS_EN = {"탑": "TOP", "정글": "JGL", "미드": "MID", "원딜": "ADC", "바텀": "ADC", "서폿": "SUP", "서포터": "SUP"}
players = {}
for pid, p in players_cfg.items():
    md = (R / f"profiles/{pid}.md").read_text(encoding="utf-8")
    secs = sections(md)
    st = json.load(open(R / f"profiles/stats/{pid}.json", encoding="utf-8"))
    attrs = []
    for k, v in st.get("position_z", {}).items():
        z = v["z"] if k != "deaths" else -v["z"]
        attrs.append({"key": k, "label": v["label"], "value": v["value"], "pos_mean": v["pos_mean"],
                      "rank": v["rank"], "of": v["of"], "score": max(1, min(20, round(10.5 + z * 4.5)))})
    play = [{"claim": clean(r.get("주장", "")), "conf": clean(r.get("신뢰도", "")),
             "has_src": clean(r.get("근거", "")) not in ("", "-")}
            for r in table(find(secs, "1."))]
    champ_claims = [{"claim": clean(r.get("주장", "")), "conf": clean(r.get("신뢰도", ""))}
                    for r in table(find(secs, "2."))]
    weak = []
    for b in bullets(find(secs, "4.")):
        head = b.split(" — ")[0]
        conf = b.rsplit(" — ", 1)[-1] if " — " in b else ""
        weak.append({"claim": head, "conf": conf})
    form_txt = {}
    for r in table(find(secs, "3.")):
        form_txt[clean(r.get("패치", "")).split(" ")[0]] = clean(r.get("내용", ""))
    b = birth.loc[pid] if pid in birth.index else None
    players[pid] = {
        "id": pid, "name_ko": p.get("name_ko"), "real_name": p.get("real_name"), "team": p["team"],
        "position": p["position"], "pos": POS_EN.get(p["position"], p["position"]),
        "birth": None if b is None or pd.isna(b.Birthdate) else b.Birthdate,
        "country": None if b is None or pd.isna(b.Country) else b.Country,
        "overall": st["overall"], "pick_slot": st.get("pick_slot_mean"), "phase1": st.get("phase1_share"),
        "attrs": attrs,
        "champions": [{k: c[k] for k in ("champion", "games", "winrate", "kda", "golddiffat15", "phase1_share") if k in c}
                      for c in st.get("champions", [])[:10]],
        "patches": [{**x, "note": form_txt.get(x["patch"], "")} for x in st.get("patches", [])],
        "summary": bullets(find(secs, "요약")),
        "playstyle": play, "champ_claims": champ_claims, "weak": weak,
        "banpick": bullets(find(secs, "5.")),
        "views": bullets(find(secs, "6.")),
        "reviewed": "S7 검토 반영" in md.splitlines()[0],
    }

# ---------- career + photo (dashboard/career.py → careers.json, Leaguepedia) ----------
cpath = ROOT / "dashboard/career/careers.json"
careers = json.loads(cpath.read_text(encoding="utf-8")) if cpath.exists() else {}
for pid, p in players.items():
    c = careers.get(pid, {})
    p["career"] = c.get("career", [])
    p["photo"] = c.get("photo")
    # dashboard/photos/{선수}.png|jpg|webp 가 있으면 페이지에 넣음 (아티팩트는 외부 이미지를 막음). 없으면 Leaguepedia 주소
    for ext, mime in (("webp", "image/webp"), ("jpg", "image/jpeg"), ("jpeg", "image/jpeg"), ("png", "image/png")):
        fp = ROOT / f"dashboard/photos/{pid}.{ext}"
        if fp.exists():
            p["photo"] = f"data:{mime};base64," + base64.b64encode(fp.read_bytes()).decode()
            break

# ---------- rosters as of 2026-08-25 (from OE game records) ----------
AS_OF = "2026-08-25 23:59"
pg = pd.read_csv(ROOT / "data/processed/2026/a_player_games.csv", dtype={"patch": str})
pg = pg[pg.date <= AS_OF].copy()
pg["d"] = pg.date.str[:10]
hist = (pg.groupby(["playername", "teamname"])
          .agg(games=("gameid", "size"), first=("d", "min"), last=("d", "max"))
          .reset_index().sort_values(["playername", "first"]))
OE_POS = {"top": 0, "jng": 1, "mid": 2, "bot": 3, "sup": 4}
lineup = {}  # team -> {slot: player}  (most games over the team's last 4 match days)
for t, g in pg.groupby("teamname"):
    days = sorted(g.d.unique())[-4:]
    rec = g[g.d.isin(days)].groupby(["position", "playername"]).size().reset_index(name="n")
    lineup[t] = {OE_POS[pos]: grp.sort_values("n").iloc[-1].playername for pos, grp in rec.groupby("position")}
for pid, p in players.items():
    h = hist[hist.playername == pid]
    p["team_cfg"] = p["team"]
    if len(h):
        p["team"] = h.sort_values("last").iloc[-1].teamname
        p["history"] = [{"team": r.teamname, "games": int(r.games), "first": r["first"], "last": r["last"]}
                        for _, r in h.iterrows()]
    else:
        p["history"] = []

# ---------- teams ----------
teams = {}
for tname, t in teams_cfg.items():
    key = file_key(tname)
    md = (R / f"profiles/{key}.md").read_text(encoding="utf-8")
    secs = sections(md)
    ts = json.load(open(R / f"profiles/team_stats/{key}.json", encoding="utf-8"))
    teams[tname] = {
        "id": key, "name": tname, "name_ko": t.get("name_ko"),
        "players": [pid for pid, p in players.items() if p["team"] == tname],
        "lineup": {str(k): v for k, v in lineup.get(tname, {}).items()},
        "summary": bullets(find(secs, "요약")),
        "vsT1": bullets(find(secs, "5.")),
        "games": ts["games"], "overall": ts["overall"], "first_selection": ts.get("first_selection"),
        "bans": ts.get("bans"), "picks_phase1": ts.get("picks", {}).get("phase1", [])[:10],
        "by_role": ts.get("picks", {}).get("by_role", {}),
        "duos": ts.get("duos", {}), "archetypes": ts.get("archetypes", [])[:5],
        "macro": ts.get("macro"), "by_patch": ts.get("by_patch", []),
    }
# map standings team names to config names
cfg_by_std = {}
for tname in teams:
    cfg_by_std[tname] = tname
alias = {"Gen.G": "Gen.G"}
for s in standings:
    if s["team"] not in teams:
        cand = [k for k in teams if k.lower().replace(" ", "") in s["team"].lower().replace(" ", "") or s["team"].lower().replace(" ", "") in k.lower().replace(" ", "")]
        s["team_cfg"] = cand[0] if cand else None
    else:
        s["team_cfg"] = s["team"]

OUT.write_text(json.dumps({"standings": standings, "teams": teams, "players": players},
                          ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("standings", [(s["rank"], s["team"], s["team_cfg"], s["w"], s["l"]) for s in standings])
print("players", len(players), "teams", len(teams), "bytes", OUT.stat().st_size)
for k, v in teams.items():
    print(k, "lineup", [v["lineup"].get(str(i)) for i in range(5)], "| research", v["players"])
print("moved", {pid: [(h["team"], h["games"], h["first"], h["last"]) for h in p["history"]]
                for pid, p in players.items() if len(p["history"]) > 1})

# ---------- HTML: template + data ----------
tpl = (ROOT / "dashboard/template.html").read_text(encoding="utf-8")
data = OUT.read_text(encoding="utf-8").replace("</", "<" + "\\" + "/")
(ROOT / "dashboard/t1_scouting.html").write_text(tpl.replace("__DATA__", data), encoding="utf-8")
print("wrote dashboard/t1_scouting.html")
