"""G-3. 리서치 대상 솔랭 상세 — g_soloq_research.py 가 받은 경기에서 뽑을 수 있는 것 전부 + 타임라인(10·15분).

단계
  timeline : 대상 선수 본인 경기마다 match-v5 timeline 1번 (키 필요, 호출 = 본인 경기 수).
             원본은 크기가 커서 저장하지 않고 필요한 것만 뽑아 data/raw/riot/timeline_lite/<matchId>.json.gz 로 캐시
  build    : 캐시(경기 상세 + timeline_lite)만 읽어 CSV 를 만듦 (API 호출 없음, 키 불필요)

경기 상세에서 뽑는 것 (선수 본인 1행, g_research_soloq_games.csv)
  기본: 패치, 리서치 패치 창, 경기 시간, 진영, 포지션, 챔피언, 승패, 항복 여부
  전투: K/D/A, 멀티킬, 최대 연속 킬, 솔로킬, 킬 관여, 퍼블, 첫 포탑, 분당 딜, 딜 비중, 받은 피해 비중, CC 시간, 회복·보호막
  성장: 골드·분당 골드, CS·분당 CS, 라인 CS 10분, 정글 CS 10분, 레벨, 라인전 골드·경험치 우위, 최대 CS 우위
  시야: 시야 점수·분당, 와드 설치·제거, 제어 와드
  오브젝트: 포탑·억제기·드래곤·바론·전령·공허 유충·아타칸 (본인 관여 + 팀 처치 수 + 첫 처치)
  빌드: 최종 아이템 7칸, 소환사 주문 2개, 룬 (핵심 룬·주 빌드·보조 빌드·세부 룬 6개·능력치 3개) — 이름 포함 (Data Dragon ko_KR)
  상대: 같은 포지션 상대 챔피언·계정·LCK 선수 여부, 골드·CS·딜·경험치·레벨·시야 차이 (경기 끝 기준)
  팀: 아군·적 5명 챔피언 (포지션 순), 양 팀 밴, 같은 편·상대 편 LCK 선수
  그 외 challenges 항목 전부는 ch_* 칸 (아레나·칼바람·스웜 전용 항목은 뺌)
타임라인에서 뽑는 것: 10·15분 골드·경험치·CS·레벨과 라인 상대와의 차이, 10·15분까지 킬·데스·어시,
  스킬 선마 순서(예: Q>E>W), 완성 아이템 순서·시각(처음 3개), 첫 귀환 전 구매 아이템

집계 (data/processed/2026/)
  g_research_soloq_games.csv      위 1행 (다시하기 포함, is_remake·suspect_account 로 거를 것)
  g_research_soloq_champs.csv     선수 × 챔피언: 판수·승률·KDA·평균 지표·주 룬·주 주문·자주 가는 아이템·10/15분 차이
  g_research_soloq_players.csv    선수: 판수·승률·챔피언 폭·주 포지션 비율·상위 챔피언·라인전 지표·패치 창별 판수
  g_research_soloq_patch.csv      선수 × 리서치 패치 창: 판수·승률·KDA·상위 챔피언
  g_research_soloq_matchups.csv   선수 × 챔피언 × 상대 챔피언: 판수·승률·평균 골드 차
  g_research_soloq_vs_pros.csv    선수 × 맞라인 LCK 선수: 판수·승률·평균 15분 골드 차

사용
  python g_soloq_detail.py --steps build              # 키 없이 지금 캐시로
  python g_soloq_detail.py --steps timeline,build     # 타임라인까지 (본인 경기 수만큼 호출, 개발 키로 시간당 약 3,000개)
"""
from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter

import pandas as pd

from common import PROCESSED, RAW, ensure_dir, get_logger, http_get, save_csv
from g_riot_soloq import KST, REGION, RIOT, Riot, load_key
from g_soloq_research import MIN_CHECK_GAMES, MIN_ROLE_SHARE, POS, label_patch, period
from datetime import datetime

log = get_logger("G3")

DD = "https://ddragon.leagueoflegends.com"
DD_VERSION = "16.16.1"  # 리서치 마지막 패치 (아이템·룬 이름용)
LITE = ensure_dir(RIOT / "timeline_lite")
ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
SKIP_CH = ("SWARM_", "poro", "snowball", "Aram", "ARAM", "Alcove", "fistBump", "dancedWith", "completeSupportQuest",
           "mejais", "blastCone", "InfernalScale", "HealFromMapSources", "twoWardsOneSweeper")
DROP_P = {"PlayerScore", "playerAugment", "playerSubteamId", "subteamPlacement", "placement", "profileIcon",
          "summonerId", "summonerName", "summonerLevel", "eligibleForProgression", "missions", "perks", "challenges",
          "riotIdGameName", "riotIdTagline", "championTransform", "unrealKills", "participantId"}


# ---------------------------------------------------------------- 이름표 (Data Dragon)
def dd(name: str) -> dict:
    f = ensure_dir(RAW / "ddragon" / DD_VERSION) / f"{name}_ko_KR.json"
    if not f.exists():
        f.write_text(http_get(f"{DD}/cdn/{DD_VERSION}/data/ko_KR/{name}.json").text, encoding="utf-8")
    return json.loads(f.read_text(encoding="utf-8"))


def names() -> dict:
    items = dd("item")["data"]
    spells = {int(v["key"]): v["name"] for v in dd("summoner")["data"].values()}
    runes = {}
    for style in dd("runesReforged"):
        runes[style["id"]] = style["name"]
        for slot in style["slots"]:
            for r in slot["runes"]:
                runes[r["id"]] = r["name"]
    # 완성 아이템 = 상위 아이템이 없고(into 없음) 하위 재료가 있는 것 + 신발 완성품 (소모품·장신구 제외)
    done = {int(k) for k, v in items.items()
            if not v.get("into") and v.get("from") and v.get("gold", {}).get("total", 0) >= 900
            and "Consumable" not in v.get("tags", []) and "Trinket" not in v.get("tags", [])}
    return {"item": {int(k): v["name"] for k, v in items.items()}, "spell": spells, "rune": runes, "done": done}


# ---------------------------------------------------------------- 타임라인
LITE_VERSION = 3  # 3: 프레임에 챔피언 딜·받은 피해 추가
LITE_MINUTES = (10, 15, 20, 25)


def lite(tl: dict, puuids: set[str]) -> dict:
    """타임라인에서 10·15·20·25분 프레임(10명 전부), 대상 선수 이벤트, 모든 킬 이벤트만 남김.

    kills (v2, 챔피언 속성표의 캐치·폭딜용): [초, 킬러 pid, 희생자 pid, [어시스트 pid], x, y,
    {피해 준 pid: [물리, 마법, 고정]}, 희생자에게 피해를 준 스킬 수]
    """
    info = tl["info"]
    pid = {p["participantId"]: p["puuid"] for p in info["participants"]}
    frames = {}
    for minute in LITE_MINUTES:
        if minute < len(info["frames"]):
            fr = info["frames"][minute]["participantFrames"]
            frames[minute] = {pid[int(k)]: {"gold": v["totalGold"], "xp": v["xp"], "level": v["level"],
                                            "cs": v["minionsKilled"] + v["jungleMinionsKilled"],
                                            "dmg": (v.get("damageStats") or {}).get("totalDamageDoneToChampions"),
                                            "taken": (v.get("damageStats") or {}).get("totalDamageTaken")}
                              for k, v in fr.items()}
    ev = {pu: {"skills": [], "items": [], "kda": []} for pu in pid.values() if pu in puuids}
    kills = []
    for f in info["frames"]:
        for e in f["events"]:
            t = e.get("timestamp", 0) // 1000
            if e["type"] == "SKILL_LEVEL_UP" and pid.get(e.get("participantId")) in ev and e.get("levelUpType") == "NORMAL":
                ev[pid[e["participantId"]]]["skills"].append(e["skillSlot"])
            elif e["type"] in ("ITEM_PURCHASED", "ITEM_UNDO", "ITEM_SOLD") and pid.get(e.get("participantId")) in ev:
                ev[pid[e["participantId"]]]["items"].append([t, e["type"][5], e.get("itemId") or e.get("beforeId")])
            elif e["type"] == "CHAMPION_KILL":
                dmg = {}
                for d in e.get("victimDamageReceived") or []:
                    q = d.get("participantId")
                    if q and 1 <= q <= 10:
                        x = dmg.setdefault(q, [0, 0, 0])
                        x[0] += d.get("physicalDamage", 0); x[1] += d.get("magicDamage", 0); x[2] += d.get("trueDamage", 0)
                pos = e.get("position") or {}
                kills.append([t, e.get("killerId"), e.get("victimId"), e.get("assistingParticipantIds", []),
                              pos.get("x"), pos.get("y"), dmg, len(e.get("victimDamageReceived") or [])])
                for who, kind in [(e.get("killerId"), "K"), (e.get("victimId"), "D")] + \
                                 [(a, "A") for a in e.get("assistingParticipantIds", [])]:
                    if pid.get(who) in ev:
                        ev[pid[who]]["kda"].append([t, kind])
    return {"v": LITE_VERSION, "pid": pid, "frames": frames, "events": ev, "kills": kills}


def lite_ok(mid: str) -> bool:
    """받아 둔 타임라인이 현재 형식(v2)인가. 옛 형식·깨진 파일은 다시 받음."""
    f = LITE / f"{mid}.json.gz"
    if not f.exists():
        return False
    try:
        return json.loads(gzip.decompress(f.read_bytes())).get("v") == LITE_VERSION
    except Exception:
        return False


def timeline(riot: Riot, mid: str, puuids: set[str]) -> dict | None:
    f = LITE / f"{mid}.json.gz"
    if f.exists():
        try:
            d = json.loads(gzip.decompress(f.read_bytes()))
            if d.get("v") == LITE_VERSION:
                return d
        except Exception:  # 중간에 끊겨 깨진 파일 → 다시 받음
            pass
        f.unlink()  # 깨졌거나 옛 형식(v1) → 다시 받음
    tl = riot.get(f"{REGION}/lol/match/v5/matches/{mid}/timeline")
    if not tl:
        return None
    out = lite(tl, puuids)
    tmp = f.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(json.dumps(out).encode()))
    tmp.replace(f)  # 다 쓴 뒤에 이름을 바꿔서, 끊겨도 반쯤 쓴 파일이 남지 않게
    return out


# ---------------------------------------------------------------- 30분 이후 프레임 (2026-11-05)
# timeline_lite 는 25분까지만 남겨서, 30분 이상 간 판만 25분부터 끝까지 1분마다 10명 프레임을 따로 받음.
# 대상 = 챔피언 속성표 참가자 표(h_attr_participants: 리서치 창·솔랭·조기 항복 제외)의 30분 이상 판.
LATE = ensure_dir(RIOT / "timeline_late")
LATE_VERSION = 1
LATE_FROM, LATE_MIN_DUR = 25, 30


def late_frames(tl: dict) -> dict:
    info = tl["info"]
    pid = {p["participantId"]: p["puuid"] for p in info["participants"]}
    frames = {}
    for minute in range(LATE_FROM, len(info["frames"])):
        fr = info["frames"][minute]
        frames[minute] = {pid[int(k)]: {"gold": v["totalGold"], "xp": v["xp"], "level": v["level"],
                                        "cs": v["minionsKilled"] + v["jungleMinionsKilled"],
                                        "dmg": (v.get("damageStats") or {}).get("totalDamageDoneToChampions"),
                                        "taken": (v.get("damageStats") or {}).get("totalDamageTaken")}
                          for k, v in fr["participantFrames"].items()}
    return {"v": LATE_VERSION, "pid": pid, "frames": frames}


def late_ok(mid: str) -> bool:
    f = LATE / f"{mid}.json.gz"
    if not f.exists():
        return False
    try:
        return json.loads(gzip.decompress(f.read_bytes())).get("v") == LATE_VERSION
    except Exception:
        return False


def late_all(riot: Riot) -> None:
    parts = PROCESSED / "h_attr_participants.csv.gz"
    g = pd.read_csv(parts, usecols=["matchId", "dur_min"], dtype={"matchId": str}).drop_duplicates("matchId")
    ids = g.loc[g["dur_min"] >= LATE_MIN_DUR, "matchId"].tolist()
    todo = [m for m in ids if not late_ok(m)]
    log.info("30분 이후 프레임: 30분 이상 판 %d 중 새로 받을 것 %d (시간당 약 3,000)", len(ids), len(todo))
    for i, mid in enumerate(todo, 1):
        tl = riot.get(f"{REGION}/lol/match/v5/matches/{mid}/timeline")
        if tl:
            f = LATE / f"{mid}.json.gz"
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(json.dumps(late_frames(tl)).encode()))
            tmp.replace(f)
        if i % 200 == 0 or i == len(todo):
            log.info("30분 이후 프레임: %d/%d", i, len(todo))


# ---------------------------------------------------------------- 킬 피해 스킬별 (2026-11-05)
# 궁극기가 킬 딜의 몇 %인지 보려고, 킬 이벤트의 victimDamageReceived 를 스킬 단위로 남김 (timeline_lite 는 참가자 합계만).
# 대상 = 챔피언 속성표 참가자 표(h_attr_participants)의 경기 전부. 받은 파일은 건너뛰므로 끊겨도 다시 실행하면 이어받음.
KDMG = ensure_dir(RIOT / "timeline_killdmg")
KDMG_VERSION = 1


def kill_damage(tl: dict) -> dict:
    """kills: [초, 킬러 pid, 희생자 pid, [[피해 준 pid, spellSlot, basic(0/1), 물리, 마법, 고정, spellName, type]]]"""
    info = tl["info"]
    pid = {p["participantId"]: p["puuid"] for p in info["participants"]}
    kills = []
    for f in info["frames"]:
        for e in f["events"]:
            if e["type"] != "CHAMPION_KILL":
                continue
            hits = [[d.get("participantId"), d.get("spellSlot"), int(bool(d.get("basic"))),
                     d.get("physicalDamage", 0), d.get("magicDamage", 0), d.get("trueDamage", 0),
                     d.get("spellName"), d.get("type")]
                    for d in e.get("victimDamageReceived") or []]
            kills.append([e.get("timestamp", 0) // 1000, e.get("killerId"), e.get("victimId"), hits])
    return {"v": KDMG_VERSION, "pid": pid, "kills": kills}


def kdmg_ok(mid: str) -> bool:
    f = KDMG / f"{mid}.json.gz"
    if not f.exists():
        return False
    try:
        return json.loads(gzip.decompress(f.read_bytes())).get("v") == KDMG_VERSION
    except Exception:
        return False


def kdmg_all(riot: Riot) -> None:
    g = pd.read_csv(PROCESSED / "h_attr_participants.csv.gz", usecols=["matchId"], dtype=str).drop_duplicates()
    ids = g["matchId"].tolist()
    todo = [m for m in ids if not kdmg_ok(m)]
    log.info("킬 피해 스킬별: 경기 %d 중 새로 받을 것 %d (시간당 약 3,000)", len(ids), len(todo))
    for i, mid in enumerate(todo, 1):
        tl = riot.get(f"{REGION}/lol/match/v5/matches/{mid}/timeline")
        if tl:
            f = KDMG / f"{mid}.json.gz"
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(json.dumps(kill_damage(tl)).encode()))
            tmp.replace(f)
        if i % 200 == 0 or i == len(todo):
            log.info("킬 피해 스킬별: %d/%d", i, len(todo))


def skill_order(slots: list[int]) -> str:
    """선마 순서: 궁(4) 빼고 Q/W/E 중 5레벨을 먼저 찍은 순서. 18렙 전에 끝난 경기는 레벨이 높은 순."""
    lv, done = Counter(), []
    for s in slots:
        lv[s] += 1
        if s != 4 and lv[s] == 5:
            done.append(s)
    rest = sorted((s for s in (1, 2, 3) if s not in done), key=lambda s: -lv[s])
    return ">".join("QWE"[s - 1] for s in done + rest) if slots else None


def item_path(items: list, nm: dict) -> dict:
    bought, finished, first_back = [], [], []
    for t, kind, it in items:
        if kind == "P":
            bought.append((t, it))
        elif kind == "U" and bought:  # 되돌리기
            bought.pop()
    for t, it in bought:
        if it in nm["done"] and it not in [x for _, x in finished]:
            finished.append((t, it))
        if t < 90:
            first_back.append(it)
    out = {"start_items": "|".join(nm["item"].get(i, str(i)) for i in first_back) or None}
    for k, (t, it) in enumerate(finished[:3], 1):
        out[f"core{k}"] = nm["item"].get(it, str(it))
        out[f"core{k}_min"] = round(t / 60, 1)
    return out


# ---------------------------------------------------------------- 경기 1행
def rows_of(m: dict, pro_map: dict[str, str], tl: dict | None, nm: dict, key2id: dict) -> list[dict]:
    info, mid = m["info"], m["metadata"]["matchId"]
    ver = info.get("gameVersion", "")
    patch = ".".join(f"{int(x):02d}" if i else x for i, x in enumerate(ver.split(".")[:2])) if ver else None
    start = datetime.fromtimestamp(info["gameStartTimestamp"] / 1000, KST).strftime("%Y-%m-%d %H:%M")
    dur = info["gameDuration"]
    ps = info["participants"]
    champ = lambda p: key2id.get(p["championId"], p["championName"])
    team = {t["teamId"]: t for t in info["teams"]}
    tot = {tid: {"kills": sum(p["kills"] for p in ps if p["teamId"] == tid),
                 "gold": sum(p["goldEarned"] for p in ps if p["teamId"] == tid),
                 "dmg": sum(p["totalDamageDealtToChampions"] for p in ps if p["teamId"] == tid)} for tid in team}
    lineup = {tid: sorted((p for p in ps if p["teamId"] == tid),
                          key=lambda p: ORDER.index(p["teamPosition"]) if p["teamPosition"] in ORDER else 9) for tid in team}
    out = []
    for p in ps:
        player = pro_map.get(p["puuid"])
        if not player:
            continue
        mine, foe = p["teamId"], 200 if p["teamId"] == 100 else 100
        opp = next((q for q in ps if q["teamId"] == foe and q["teamPosition"] == p["teamPosition"] and p["teamPosition"]), None)
        cs = p["totalMinionsKilled"] + p["neutralMinionsKilled"]
        r = {"matchId": mid, "game_start_kst": start, "patch": patch, "gameVersion": ver, "game_min": round(dur / 60, 1),
             "is_remake": dur < 300, "player": player, "riot_id": f"{p.get('riotIdGameName')}#{p.get('riotIdTagline')}",
             "side": "Blue" if mine == 100 else "Red", "champion": champ(p),
             "cs": cs, "cspm": round(cs / max(dur / 60, 1), 2), "kda": round((p["kills"] + p["assists"]) / max(p["deaths"], 1), 2),
             "kill_share_team": round((p["kills"] + p["assists"]) / max(tot[mine]["kills"], 1), 3),
             "gold_share": round(p["goldEarned"] / max(tot[mine]["gold"], 1), 3),
             "damage_share": round(p["totalDamageDealtToChampions"] / max(tot[mine]["dmg"], 1), 3)}
        r.update({k: v for k, v in p.items() if not any(k.startswith(d) for d in DROP_P) and not isinstance(v, (dict, list))})
        r.update({f"ch_{k}": v for k, v in (p.get("challenges") or {}).items()
                  if not isinstance(v, (dict, list)) and not any(s in k for s in SKIP_CH)})
        for i in range(7):
            r[f"item{i}_name"] = nm["item"].get(p[f"item{i}"]) if p[f"item{i}"] else None
        r["spells"] = "|".join(sorted(nm["spell"].get(p[f"summoner{i}Id"], str(p[f"summoner{i}Id"])) for i in (1, 2)))
        st = p["perks"]["styles"]
        sel = [s["perk"] for s in st[0]["selections"]] + [s["perk"] for s in st[1]["selections"]]
        r.update({"keystone": nm["rune"].get(sel[0]), "rune_primary": nm["rune"].get(st[0]["style"]),
                  "rune_secondary": nm["rune"].get(st[1]["style"]),
                  "runes": "|".join(nm["rune"].get(x, str(x)) for x in sel),
                  "stat_perks": "|".join(str(p["perks"]["statPerks"][k]) for k in ("offense", "flex", "defense"))})
        for o, d in (("dragon", "dragon"), ("baron", "baron"), ("riftHerald", "herald"), ("horde", "grubs"),
                     ("atakhan", "atakhan"), ("tower", "tower"), ("inhibitor", "inhib")):
            for tid, pre in ((mine, "team"), (foe, "enemy")):
                ob = team[tid].get("objectives", {}).get(o, {})
                r[f"{pre}_{d}"] = ob.get("kills")
                r[f"{pre}_{d}_first"] = ob.get("first")
        r["team_kills"], r["enemy_kills"] = tot[mine]["kills"], tot[foe]["kills"]
        for tid, pre in ((mine, "ally"), (foe, "enemy")):
            r[f"{pre}_champs"] = "|".join(str(champ(q)) for q in lineup[tid])
            r[f"{pre}_bans"] = "|".join(str(key2id.get(b["championId"], b["championId"]))
                                        for b in team[tid].get("bans", []) if b["championId"] > 0)
            r[f"{pre}_pros"] = "|".join(pro_map[q["puuid"]] for q in lineup[tid] if q["puuid"] in pro_map and q is not p) or None
        if opp:
            ocs = opp["totalMinionsKilled"] + opp["neutralMinionsKilled"]
            r.update({"opp_champion": champ(opp), "opp_riot_id": f"{opp.get('riotIdGameName')}#{opp.get('riotIdTagline')}",
                      "opp_pro": pro_map.get(opp["puuid"]),
                      "gold_diff_end": p["goldEarned"] - opp["goldEarned"], "cs_diff_end": cs - ocs,
                      "dmg_diff_end": p["totalDamageDealtToChampions"] - opp["totalDamageDealtToChampions"],
                      "xp_diff_end": p["champExperience"] - opp["champExperience"],
                      "level_diff_end": p["champLevel"] - opp["champLevel"],
                      "vision_diff_end": p["visionScore"] - opp["visionScore"],
                      "opp_kills": opp["kills"], "opp_deaths": opp["deaths"], "opp_assists": opp["assists"]})
        if tl:
            for minute, fr in tl["frames"].items():
                me, op = fr.get(p["puuid"]), fr.get(opp["puuid"]) if opp else None
                if not me:
                    continue
                for k in ("gold", "xp", "cs", "level", "dmg"):
                    if me.get(k) is None:
                        continue
                    r[f"{k}{minute}"] = me[k]
                    if op and op.get(k) is not None:
                        r[f"{k}_diff{minute}"] = me[k] - op[k]
            ev = tl["events"].get(p["puuid"])
            if ev:
                for minute in (10, 15):
                    c = Counter(k for t, k in ev["kda"] if t < minute * 60)
                    r.update({f"kills{minute}": c["K"], f"deaths{minute}": c["D"], f"assists{minute}": c["A"]})
                r["skill_order"] = skill_order(ev["skills"])
                r.update(item_path(ev["items"], nm))
        out.append(r)
    return out


# ---------------------------------------------------------------- 집계
def top(s: pd.Series, n: int = 3) -> str | None:
    s = s.dropna()
    return " / ".join(f"{k} {v}" for k, v in s.value_counts().head(n).items()) if len(s) else None


def top_items(g: pd.DataFrame, n: int = 4) -> str | None:
    """최종 아이템 중 자주 산 완성템 (신발·장신구 포함 안 함은 이름 기준 거르지 않고 그대로, 빈도순)."""
    c = Counter(x for col in [f"item{i}_name" for i in range(6)] for x in g[col].dropna())
    return " / ".join(f"{k} {v}" for k, v in c.most_common(n)) or None


def mean(g: pd.DataFrame, col: str, nd: int = 2):
    return round(g[col].mean(), nd) if col in g and g[col].notna().any() else None


def summarize(g: pd.DataFrame) -> dict:
    d = {"games": len(g), "wins": int(g["win"].sum()), "win_rate": round(g["win"].mean(), 3),
         "kills": mean(g, "kills"), "deaths": mean(g, "deaths"), "assists": mean(g, "assists"),
         "kda": round((g["kills"].sum() + g["assists"].sum()) / max(g["deaths"].sum(), 1), 2)}
    for col, nd in (("ch_killParticipation", 3), ("ch_damagePerMinute", 0), ("damage_share", 3), ("ch_goldPerMinute", 0),
                    ("gold_share", 3), ("cspm", 2), ("ch_visionScorePerMinute", 2), ("ch_soloKills", 2),
                    ("ch_laningPhaseGoldExpAdvantage", 3), ("ch_maxCsAdvantageOnLaneOpponent", 1),
                    ("ch_laneMinionsFirst10Minutes", 1), ("ch_jungleCsBefore10Minutes", 1),
                    ("gold_diff10", 0), ("gold_diff15", 0), ("cs_diff10", 1), ("cs_diff15", 1), ("xp_diff15", 0),
                    ("deaths10", 2), ("gold_diff_end", 0), ("game_min", 1)):
        d[col] = mean(g, col, nd)
    d["on_role_share"] = round(g["on_role"].mean(), 3)  # 선수 본 포지션으로 한 판 비율 (나머지는 다른 라인 연습)
    d["first_blood_rate"] =round((g["firstBloodKill"] | g["firstBloodAssist"]).mean(), 3)
    d["surrender_rate"] = round(g["gameEndedInSurrender"].mean(), 3)
    d["top_keystone"] = top(g["keystone"], 2)
    d["top_spells"] = top(g["spells"], 2)
    d["top_items"] = top_items(g)
    if "skill_order" in g:
        d["top_skill_order"] = top(g["skill_order"], 2)
        d["top_core1"] = top(g["core1"], 3)
    return d


def build(pro_map: dict, accts: pd.DataFrame):
    nm = names()
    champs = pd.read_csv(PROCESSED / "d_champions.csv")
    key2id = dict(zip(champs["champion_key"], champs["champion_id"]))
    start, end = period()
    ids = []
    for f in (RIOT / "matchids_research").glob(f"*_{start}_{end}.json"):
        ids += json.loads(f.read_text())
    ids = list(dict.fromkeys(ids))
    rows, n_tl = [], 0
    for i, mid in enumerate(ids, 1):
        f = RIOT / "matches" / f"{mid}.json.gz"
        if not f.exists():
            continue
        m = json.loads(gzip.decompress(f.read_bytes()))
        if not m["info"].get("participants"):
            continue
        lf = LITE / f"{mid}.json.gz"
        tl = json.loads(gzip.decompress(lf.read_bytes())) if lf.exists() else None
        n_tl += tl is not None
        rows += rows_of(m, pro_map, tl, nm, key2id)
        if i % 5000 == 0:
            log.info("build: %d/%d", i, len(ids))
    df = pd.DataFrame(rows)
    a, b = (datetime.fromtimestamp(x, KST).strftime("%Y-%m-%d") for x in (start, end))
    df = df[(df["game_start_kst"] >= a) & (df["game_start_kst"] < b)].sort_values(["player", "game_start_kst"])
    df.insert(3, "research_patch", label_patch(df["game_start_kst"]))
    # 계정 검사 (포지션 일치율)
    pos = dict(zip(accts["player"], accts["position"].map(POS)))
    ok = df[~df["is_remake"]]
    share = ok.groupby("puuid").apply(lambda g: (g["teamPosition"] == pos[g["player"].iat[0]]).mean())
    n = ok.groupby("puuid").size()
    bad = set(share[(share < MIN_ROLE_SHARE) & (n >= MIN_CHECK_GAMES)].index)
    df["suspect_account"] = df["puuid"].isin(bad)
    df["on_role"] = df["teamPosition"] == df["player"].map(pos)
    save_csv(df, PROCESSED / "g_research_soloq_games.csv")
    log.info("경기 행 %d (선수 %d명, 타임라인 있는 경기 %d), 의심 계정 %d", len(df), df["player"].nunique(), n_tl, len(bad))

    g = df[~df["is_remake"] & ~df["suspect_account"]]
    rows = [{"player": p, "champion": c, **summarize(x)} for (p, c), x in g.groupby(["player", "champion"])]
    ch = pd.DataFrame(rows)
    ch["share"] = (ch["games"] / ch.groupby("player")["games"].transform("sum")).round(3)
    save_csv(ch.sort_values(["player", "games"], ascending=[True, False]), PROCESSED / "g_research_soloq_champs.csv")

    prow = []
    for p, x in g.groupby("player"):
        vc = x["champion"].value_counts()
        prow.append({"player": p, **summarize(x),
                     "champions": len(vc), "champions_5plus": int((vc >= 5).sum()),
                     "top_champions": " / ".join(f"{k} {v}" for k, v in vc.head(8).items()),
                     "first_game": x["game_start_kst"].min(), "last_game": x["game_start_kst"].max(),
                     "games_offseason": int(x["research_patch"].isna().sum()),
                     **{f"games_{w}": int((x["research_patch"] == w).sum()) for w in sorted(x["research_patch"].dropna().unique())}})
    save_csv(pd.DataFrame(prow), PROCESSED / "g_research_soloq_players.csv")

    pt = [{"player": p, "research_patch": w, **summarize(x), "top_champions": top(x["champion"], 5)}
          for (p, w), x in g.dropna(subset=["research_patch"]).groupby(["player", "research_patch"])]
    save_csv(pd.DataFrame(pt), PROCESSED / "g_research_soloq_patch.csv")

    on = g[g["on_role"] & g["opp_champion"].notna()]
    mu = [{"player": p, "champion": c, "opp_champion": o, "games": len(x), "win_rate": round(x["win"].mean(), 3),
           "kda": round((x["kills"].sum() + x["assists"].sum()) / max(x["deaths"].sum(), 1), 2),
           "gold_diff15": mean(x, "gold_diff15", 0), "gold_diff_end": mean(x, "gold_diff_end", 0)}
          for (p, c, o), x in on.groupby(["player", "champion", "opp_champion"])]
    save_csv(pd.DataFrame(mu).sort_values(["player", "games"], ascending=[True, False]), PROCESSED / "g_research_soloq_matchups.csv")

    vp = [{"player": p, "opp_pro": o, "games": len(x), "win_rate": round(x["win"].mean(), 3),
           "kda": round((x["kills"].sum() + x["assists"].sum()) / max(x["deaths"].sum(), 1), 2),
           "gold_diff15": mean(x, "gold_diff15", 0), "champions": top(x["champion"], 3), "opp_champions": top(x["opp_champion"], 3)}
          for (p, o), x in on.dropna(subset=["opp_pro"]).groupby(["player", "opp_pro"])]
    save_csv(pd.DataFrame(vp).sort_values(["player", "games"], ascending=[True, False]), PROCESSED / "g_research_soloq_vs_pros.csv")
    log.info("집계: 챔피언 %d행, 선수 %d행, 패치 %d행, 상대 챔피언 %d행, LCK 맞라인 %d행",
             len(ch), len(prow), len(pt), len(mu), len(vp))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="build", help="timeline,late,killdmg,build")
    args = ap.parse_args()
    steps = set(args.steps.split(","))
    accts = pd.read_csv(PROCESSED / "g_research_accounts.csv").dropna(subset=["puuid"])
    shared = set(accts.loc[accts.duplicated("puuid", keep=False), "puuid"])
    pro_map = {p: pl for p, pl in zip(accts["puuid"], accts["player"]) if p not in shared}

    if "timeline" in steps:
        riot = Riot(load_key())
        start, end = period()
        ids = []
        for f in (RIOT / "matchids_research").glob(f"*_{start}_{end}.json"):
            ids += json.loads(f.read_text())
        ids = list(dict.fromkeys(ids))
        todo = [m for m in ids if (RIOT / "matches" / f"{m}.json.gz").exists() and not lite_ok(m)]
        log.info("타임라인: 경기 %d 중 새로 받을 것 %d (시간당 약 3,000)", len(ids), len(todo))
        for i, mid in enumerate(todo, 1):
            m = json.loads(gzip.decompress((RIOT / "matches" / f"{mid}.json.gz").read_bytes()))
            mine = {p["puuid"] for p in m["info"].get("participants", []) if p["puuid"] in pro_map}
            if mine and m["info"]["gameDuration"] >= 300:
                timeline(riot, mid, mine)
            if i % 200 == 0:
                log.info("타임라인: %d/%d", i, len(todo))
    if "late" in steps:
        late_all(Riot(load_key()))
    if "killdmg" in steps:
        kdmg_all(Riot(load_key()))
    if "build" in steps:
        build(pro_map, accts)


if __name__ == "__main__":
    main()
