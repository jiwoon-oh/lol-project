"""대시보드 만들기: research 프로필·기록표 + 경기 기록 → dashboard/data.json → dashboard/report.html

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

# ---------- position metrics (dashboard only; research stats/*.json 는 그대로 둠) ----------
# 비교군·기간은 stats_baseline.position_z 와 같음: 리서치 창 안 LCK 경기, 같은 포지션 20경기 이상 (+ 대상 선수)
import sys
sys.path.insert(0, str(ROOT / "src/research"))
from patch_windows import patch_of, windows  # noqa: E402

MIN_GAMES = 20
OE_POS_KO = {"top": "탑", "jng": "정글", "mid": "미드", "bot": "원딜", "sup": "서폿"}
METRICS = {  # key: (설명, 높을수록 좋은가)
    "kda": ("KDA", True), "deaths": ("경기당 데스", False), "kp": ("킬 관여율", True),
    "dpm": ("분당 딜", True), "damageshare": ("팀 내 딜 비중", True), "earnedgoldshare": ("팀 내 골드 비중", True),
    "dmg_eff": ("딜 효율 (딜 비중 ÷ 골드 비중)", True), "cspm": ("분당 CS", True), "apg": ("경기당 어시스트", True),
    "vspm": ("분당 시야 점수", True), "kaat15": ("15분 킬+어시스트", True),
    "golddiffat10": ("10분 골드 차", True), "csdiffat10": ("10분 CS 차", True), "xpdiffat10": ("10분 경험치 차", True),
    "golddiffat15": ("15분 골드 차", True), "csdiffat15": ("15분 CS 차", True), "xpdiffat15": ("15분 경험치 차", True),
}
POS_METRICS = {  # 공통 3개(kda·deaths·kp) + 포지션별
    "탑": ["golddiffat10", "csdiffat10", "golddiffat15", "xpdiffat15", "cspm", "damageshare"],
    # 상대 정글 몬스터 처치(monsterkillsenemyjungle)는 OE 2026 LCK 에 값이 없음 (LPL 일부만) → 못 넣음
    "정글": ["kaat15", "vspm", "golddiffat10", "xpdiffat15", "damageshare", "earnedgoldshare"],
    "미드": ["dpm", "damageshare", "cspm", "csdiffat15", "golddiffat15", "xpdiffat15"],
    "원딜": ["dpm", "damageshare", "dmg_eff", "cspm", "earnedgoldshare", "golddiffat15"],
    "서폿": ["vspm", "apg", "xpdiffat10", "golddiffat15"],
}
COMMON = ["kda", "deaths", "kp"]

wins = windows("research")
gm = pd.read_csv(ROOT / "data/processed/2026/a_player_games.csv", dtype={"patch": str})
gm["date_kst"] = (pd.to_datetime(gm["date"]) + pd.Timedelta(hours=9)).dt.date.astype(str)
gm = gm[gm["date_kst"].map(lambda d: patch_of(d, wins) is not None)].copy()
gm["team_kills"] = gm.groupby(["gameid", "teamname"])["kills"].transform("sum")
gm["kp"] = (gm["kills"] + gm["assists"]) / gm["team_kills"].where(gm["team_kills"] > 0)
gm["cspm"] = gm["total cs"] / (gm["gamelength"] / 60)
gm["pos_ko"] = gm["position"].map(OE_POS_KO)
# OE 원본에서 15분 킬·어시스트 (data/inven_2026/games/ 에 CSV 하나만)
oe_files = sorted((ROOT / "data/inven_2026/games").glob("*.csv"))
if len(oe_files) == 1:
    oe = pd.read_csv(oe_files[0], usecols=["gameid", "league", "playername", "killsat15", "assistsat15"], low_memory=False)
    oe = oe[(oe["league"] == "LCK") & oe["playername"].notna()].drop(columns="league")
    gm = gm.merge(oe, on=["gameid", "playername"], how="left")
    gm["kaat15"] = gm["killsat15"] + gm["assistsat15"]
else:
    print("OE 원본 CSV 가 0개 또는 2개 이상 → 15분 킬+어시스트 생략", [f.name for f in oe_files])
    gm["kaat15"] = float("nan")
    POS_METRICS["정글"].remove("kaat15")
MEAN_COLS = ["kp", "kaat15","dpm", "damageshare", "earnedgoldshare", "cspm", "vspm",
             "golddiffat10", "csdiffat10", "xpdiffat10", "golddiffat15", "csdiffat15", "xpdiffat15"]
pos_agg = {}
for pos, d in gm.groupby("pos_ko"):
    a = d.groupby("playername").agg(games=("gameid", "count"), k=("kills", "sum"), a=("assists", "sum"),
                                    dsum=("deaths", "sum"), **{c: (c, "mean") for c in MEAN_COLS})
    a["kda"] = (a["k"] + a["a"]) / a["dsum"].clip(lower=1)
    a["deaths"] = a["dsum"] / a["games"]
    a["apg"] = a["a"] / a["games"]
    a["dmg_eff"] = a["damageshare"] / a["earnedgoldshare"]
    pos_agg[pos] = a


def position_attrs(pid, pos):
    a = pos_agg[pos]
    if pid not in a.index:
        return []
    a = a[(a["games"] >= MIN_GAMES) | (a.index == pid)]
    out = []
    for k in COMMON + POS_METRICS[pos]:
        label, higher = METRICS[k]
        col = a[k]
        z = (col[pid] - col.mean()) / col.std()
        z = z if higher else -z
        out.append({"key": k, "label": label, "value": round(float(col[pid]), 3), "pos_mean": round(float(col.mean()), 3),
                    "rank": int(col.rank(ascending=not higher)[pid]), "of": len(a),
                    "z": round(float(z), 2),  # 좋은 방향이 + (데스는 뒤집음). 레이더·막대 위치에 씀
                    "score": max(1, min(20, round(10.5 + z * 4.5)))})
    return out


# 챔피언 한글 이름 (Data Dragon ko_KR, dashboard/career/champ_ko.json)
_cp = ROOT / "dashboard/career/champ_ko.json"
CHAMP_KO = json.loads(_cp.read_text(encoding="utf-8"))["names"] if _cp.exists() else {}
_CHAMP_RE = re.compile(r"(?<![A-Za-z'])(" + "|".join(re.escape(n) for n in sorted(CHAMP_KO, key=len, reverse=True)) + r")(?![A-Za-z'])") if CHAMP_KO else None


def ko_champ(x):
    """프로필 문장 속 영어 챔피언 이름 → 한글 (문자열·리스트·딕셔너리 안 문자열 모두)."""
    if isinstance(x, str):
        return _CHAMP_RE.sub(lambda m: CHAMP_KO[m.group(1)], x) if _CHAMP_RE else x
    if isinstance(x, list):
        return [ko_champ(v) for v in x]
    if isinstance(x, dict):
        return {k: ko_champ(v) for k, v in x.items()}
    return x

# ---------- players ----------
birth =ros.drop_duplicates("ID").set_index("ID")[["NativeName", "Country", "Birthdate"]]
POS_EN = {"탑": "TOP", "정글": "JGL", "미드": "MID", "원딜": "ADC", "바텀": "ADC", "서폿": "SUP", "서포터": "SUP"}
players = {}
for pid, p in players_cfg.items():
    md = (R / f"profiles/{pid}.md").read_text(encoding="utf-8")
    secs = sections(md)
    st = json.load(open(R / f"profiles/stats/{pid}.json", encoding="utf-8"))
    pos_ko = "원딜" if p["position"] == "바텀" else "서폿" if p["position"] == "서포터" else p["position"]
    attrs = position_attrs(pid, pos_ko)
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
        "champions": [{**{k: c[k] for k in ("champion", "games", "winrate", "kda", "golddiffat15", "phase1_share") if k in c},
                       "champion_ko": CHAMP_KO.get(c["champion"], c["champion"])}
                      for c in st.get("champions", [])[:10]],
        "patches": [{**x, "note": ko_champ(form_txt.get(x["patch"], ""))} for x in st.get("patches", [])],
        "summary": ko_champ(bullets(find(secs, "요약"))),
        "playstyle": ko_champ(play), "champ_claims": ko_champ(champ_claims), "weak": ko_champ(weak),
        "banpick": ko_champ(bullets(find(secs, "5."))),
        "views": ko_champ(bullets(find(secs, "6."))),
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
        "summary": ko_champ(bullets(find(secs, "요약"))),
        "vsT1": ko_champ(bullets(find(secs, "5."))),
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
(ROOT / "dashboard/report.html").write_text(tpl.replace("__DATA__", data), encoding="utf-8")
print("wrote dashboard/report.html")

# ---------- cover: dashboard/cover_template.html → dashboard/index.html ----------
from datetime import date
pt = pd.read_csv(R / "eval/per_target.csv")
# 팀 로고: Leaguepedia Teams.Image 파일 주소 (dashboard/career/team_logos.json). 파일로 받지 않고 주소만 — 열 때 인터넷 필요
lp = ROOT / "dashboard/career/team_logos.json"
logos = json.loads(lp.read_text(encoding="utf-8")) if lp.exists() else {}
cover = {
    "period_from": wins[0][1], "period_to": "2026-08-25", "as_of": AS_OF[:10],
    "patches": f"{wins[0][0]}~{wins[-1][0]}", "n_patch_windows": len(wins),
    "n_players": len(players), "n_games": int(gm["gameid"].nunique()),
    "n_claims": int(pt["passed"].sum() + pt["needs_review"].sum()),  # 프로필에 쓰인 주장 (S5 통과 + 검토필요)
    "built": date.today().isoformat(),
    "t1_hero": logos.get("T1", {}).get("hero"),  # 표지 3D 로고 (T1logo square, 날개 T1만)
    "teams": [{"name": s["team_cfg"], "id": teams[s["team_cfg"]]["id"], "name_ko": teams[s["team_cfg"]]["name_ko"],
               "rank": s["rank"], "w": s["w"], "l": s["l"], "sw": s["sw"], "sl": s["sl"], "diff": s["diff"],
               "logo": logos.get(s["team_cfg"], {}).get("url")}
              for s in standings if s.get("team_cfg") in teams],
}
ctpl = (ROOT / "dashboard/cover_template.html").read_text(encoding="utf-8")
cdata = json.dumps(cover, ensure_ascii=False).replace("</", "<" + "\\" + "/")
(ROOT / "dashboard/index.html").write_text(ctpl.replace("__DATA__", cdata), encoding="utf-8")
print("wrote dashboard/index.html", {k: v for k, v in cover.items() if k != "teams"})
