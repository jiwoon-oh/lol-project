"""H. 챔피언 속성표 (패치 × 챔피언 × 포지션) — 딜 구성·앞라인·이니시·캐치·보호·성장 곡선.

자료
  - 솔랭 경기 원본 data/raw/riot/matches/*.json.gz (G1·G2가 받은 것, 10명 전원)
    · 첫 리서치 창 시작일 ~ 마지막 리서치 창 끝(08-25) 사이 경기 전부 (비시즌·MSI 기간 솔랭 포함, 2026-11-05 변경)
    · 패치 이름표 = 경기의 실제 버전 gameVersion (16.15 → 26.15). 솔랭은 대회보다 한 패치 앞서 가므로
      날짜 창으로 붙이면 LCK 패치와 어긋남 (예: 날짜 창 26.14 솔랭 744경기 중 742경기가 실제 16.15)
    · 다시하기(5분 미만, 조기 항복) 제외
  - 자체 수치: Data Dragon 한국어 championFull (패치별 툴팁의 <status> CC 태그·광역·돌진기, 기본 스탯)
    · Meraki 는 25.15 이후 갱신이 없어 역할 이름표·attributeRatings(평가·보조용)만 사용
  - 대회 대조: Oracle's Elixir 원본 (2026-08-25까지)

실행
  python h_champion_attributes.py                 # 참가자 표 + 속성표 + 평가
  python h_champion_attributes.py --reuse         # 참가자 표를 다시 읽지 않고 속성표·평가만

산출 (data/processed/2026/)
  h_attr_participants.csv.gz   경기 × 참가자 (속성 계산용 칸만)
  h_champion_static.csv        챔피언 자체 수치 (Meraki·Data Dragon)
  h_champion_attributes.csv    패치(26.xx 또는 ALL) × 챔피언 × 포지션
  h_champion_attributes_eval.md  평가 (타당성·안정성·대회 일치)
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import re
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from common import PROCESSED, RAW, ROOT, get_logger, save_csv
from d_champion_meta import norm

log = get_logger("H")
KST = timezone(timedelta(hours=9))
PATCHES_CSV = ROOT / "research" / "config" / "tournament_patches.csv"
MATCH_DIR = RAW / "riot" / "matches"
MERAKI = RAW / "meraki" / "champions.json"
PARTS = PROCESSED / "h_attr_participants.csv.gz"
POS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
OE_POS = {"top": "TOP", "jng": "JUNGLE", "mid": "MIDDLE", "bot": "BOTTOM", "sup": "UTILITY"}
K_SHRINK = 30      # 판수가 이보다 적으면 상위 값(전체 패치 → 포지션 평균) 쪽으로 당김
MIN_RELIABLE = 30  # 이 미만은 reliable = False ("추정")
EARLY, LATE = 25, 32  # 성장 곡선: 25분 전에 끝난 판 vs 32분 넘은 판

# ---------------------------------------------------------------- 자체 수치
# Data Dragon 한국어 툴팁: CC는 <status>…</status> 태그로 표시됨 (패치마다 갱신, 로크·자헨 포함)
DD_DIR = RAW / "ddragon"
HARD_CC = re.compile(r"기절|속박|공중|띄워|띄웁|띄우|공포|달아나|도망|이동\s*불가|제압|매혹|도발|수면|광란|변이|"
                     r"밀어|밀려|밀치|날려|당기|당겨|끌어|끌려|잡아당")
SOFT_CC = re.compile(r"둔화|느려|침묵|실명|방해|이동\s*스킬|공격\s*불가|졸음")
AOE = re.compile(r"주변|근처|모든\s*적|범위|전부|원뿔|경로에\s*있는|일직선|구역|영역|원형|부채꼴|적들|전원|"
                 r"다른\s*적|나머지\s*적|챔피언들")
DASH = re.compile(r"돌진|도약|순간이동|점멸|뛰어|날아올|미끄러|질주|달려")
R_25000_GLOBAL = {"Ashe", "Ezreal", "Jinx", "Senna", "Soraka"}  # 표기 25000 중 실제 전역·장거리 궁
R_LONG_NOT = {"Hecarim"}
# 툴팁 사거리: 100 이하(자기 자신·근접 판정)와 3000 이상(25000 = 자신 시전·미정 표기가 섞임)은 "조준 사거리"가 아님
RANGE_LO, RANGE_HI = 100, 3000
STATUS = re.compile(r"<status>(.*?)</status>", re.S)
TAG = re.compile(r"<[^>]+>|\{\{[^}]*\}\}")


def dd_version(oe_patch: str) -> str:
    """16.01 → 16.1.1"""
    a, b = oe_patch.split(".")
    return f"{int(a)}.{int(b)}.1"


def gv_patch(gv: str) -> str | None:
    """경기 gameVersion 앞 두 자리 → 패치 이름표 (16.1 → 26.01, 16.15 → 26.15)."""
    try:
        a, b = gv.split(".")[:2]
        return f"{int(a) + 10}.{int(b):02d}"
    except Exception:
        return None


def oe_of(patch: str) -> str:
    """26.15 → 16.15 (Data Dragon·기본 스탯 표기)"""
    a, b = patch.split(".")
    return f"{int(a) - 10}.{b}"


def research_windows() -> pd.DataFrame:
    w = pd.read_csv(PATCHES_CSV, encoding="utf-8-sig", dtype=str)
    return w[w["role"] == "research"].reset_index(drop=True)


def static_patch(oe_patch: str, champs: pd.DataFrame) -> pd.DataFrame:
    """한 패치의 Data Dragon 한국어 툴팁 → 스킬별 CC·광역·돌진기 개수."""
    f = DD_DIR / dd_version(oe_patch) / "championFull_ko_KR.json"
    if not f.exists():  # 리서치 창 밖 패치(비시즌·MSI)는 처음 쓸 때 받음
        from common import http_get
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(http_get(f"https://ddragon.leagueoflegends.com/cdn/{dd_version(oe_patch)}/data/ko_KR/championFull.json").text,
                     encoding="utf-8")
    data = json.loads(f.read_text(encoding="utf-8"))["data"]
    rows = []
    for cid, x in data.items():
        hard = aoe = single = soft = dash = 0
        r_hard = r_aoe = False
        r_range, r_cd1, r_cd3, r_rawrange = np.nan, np.nan, np.nan, 0
        qwe = []
        spells = [("P", x["passive"])] + list(zip("QWER", x["spells"]))
        for slot, sp in spells:
            if slot in "QWER":
                rg = max(sp.get("range") or [0])
                ok_rg = RANGE_LO < rg < RANGE_HI
                if slot == "R":
                    r_range = rg if ok_rg else np.nan
                    cds = sp.get("cooldown") or []
                    r_cd1 = cds[0] if cds else np.nan
                    r_cd3 = cds[min(2, len(cds) - 1)] if cds else np.nan
                    r_rawrange = rg
                elif ok_rg:
                    qwe.append(rg)
            raw = sp.get("tooltip") or sp.get("description") or ""
            st_tags = " ".join(STATUS.findall(raw))
            text = TAG.sub(" ", raw)
            h = bool(HARD_CC.search(st_tags))
            a = h and bool(AOE.search(text))
            hard += h; aoe += a; single += h and not a
            soft += bool(SOFT_CC.search(st_tags)) and not h
            dash += bool(DASH.search(text))
            if slot == "R":
                r_hard, r_aoe = h, a
        # 장거리·전역 궁: 표기 사거리 3000 이상. 25000 은 자신 시전과 섞여 있어 손으로 고른 전역 궁만 인정,
        # 잘못된 표기(헤카림 50000 = 돌진 궁)는 뺌
        r_global = ((RANGE_HI <= r_rawrange and r_rawrange != 25000 and cid not in R_LONG_NOT)
                    or (r_rawrange == 25000 and cid in R_25000_GLOBAL))
        rows.append(dict(champion_id=cid, st_hard_cc=hard, st_aoe_cc=aoe, st_single_cc=single, st_soft_cc=soft,
                         st_dash=dash, st_r_hard_cc=r_hard, st_r_aoe_cc=r_aoe,
                         range_qwe_max=max(qwe) if qwe else np.nan,
                         range_qwe_med=float(np.median(qwe)) if qwe else np.nan,
                         range_r=r_range, r_global=r_global, r_cd1=r_cd1, r_cd3=r_cd3))
    st = pd.DataFrame(rows)
    bs = pd.read_csv(PROCESSED / "c_champion_base_stats.csv", dtype={"patch": str})
    bs = bs[bs["patch"] == oe_patch]
    bs = bs.assign(hp18=bs.hp + 17 * bs.hpperlevel, armor18=bs.armor + 17 * bs.armorperlevel,
                   mr18=bs.spellblock + 17 * bs.spellblockperlevel)
    st = st.merge(bs[["champion_id", "attackrange", "movespeed", "hp", "hp18", "armor18", "mr18"]],
                  on="champion_id", how="left")
    st["melee"] = st["attackrange"] <= 250
    return st


def static_table(patches: list[str]) -> pd.DataFrame:
    """패치(26.xx, 마지막 패치 값 = ALL)별 자체 수치 + Meraki 이름표·attributeRatings(보조, 25.15 이후 갱신 없음)."""
    champs = pd.read_csv(PROCESSED / "d_champions.csv")
    parts = [static_patch(oe_of(p), champs).assign(patch=p) for p in sorted(patches)]
    parts.append(parts[-1].assign(patch="ALL"))
    st = pd.concat(parts, ignore_index=True)
    mer = json.loads(MERAKI.read_text(encoding="utf-8"))
    by_key = {str(v["id"]): v for v in mer.values()}
    info = []
    for _, c in champs.iterrows():
        m = by_key.get(str(c["champion_key"]))
        r = {"champion_id": c["champion_id"], "name_ko": c["name_ko"], "meraki_roles": c["meraki_roles"],
             "dd_damage_type": c["damage_type"], "meraki": m is not None}
        for k, v in ((m or {}).get("attributeRatings") or {}).items():
            r[f"rating_{k}"] = v
        info.append(r)
    st = st.merge(pd.DataFrame(info), on="champion_id", how="left")
    first = ["patch", "champion_id", "name_ko"]
    return st[first + [c for c in st.columns if c not in first]]


# ---------------------------------------------------------------- 참가자 표
def window_of(day: str, w: pd.DataFrame) -> str | None:
    hit = w[(w["window_from"] <= day) & (w["window_to"] >= day)]
    return hit["patch"].iloc[0] if len(hit) else None


CH = ["damageTakenOnTeamPercentage", "teamDamagePercentage", "effectiveHealAndShielding",
      "enemyChampionImmobilizations", "immobilizeAndKillWithAlly", "knockEnemyIntoTeamAndKill",
      "pickKillWithAlly", "killParticipation",
      # 2차 추가 (다이브·스플릿·라인전·포킹)
      "outnumberedKills", "killsNearEnemyTurret", "multikills", "soloKills", "soloTurretsLategame",
      "turretPlatesTaken", "laningPhaseGoldExpAdvantage", "maxCsAdvantageOnLaneOpponent", "skillshotsHit",
      # 3차 (궁극기, 2026-11-05)
      "multiKillOneSpell"]
PF = ["teamId", "win", "teamPosition", "championId", "kills", "deaths", "assists", "goldEarned",
      "totalDamageDealtToChampions", "physicalDamageDealtToChampions", "magicDamageDealtToChampions",
      "trueDamageDealtToChampions", "totalDamageTaken", "physicalDamageTaken", "magicDamageTaken",
      "trueDamageTaken", "damageSelfMitigated", "timeCCingOthers", "totalHealsOnTeammates",
      "totalDamageShieldedOnTeammates", "damageDealtToBuildings", "spell4Casts", "puuid"]


def participants() -> pd.DataFrame:
    w = research_windows()
    lo, hi = w["window_from"].min(), w["window_to"].max()
    files = sorted(glob.glob(str(MATCH_DIR / "*.json.gz")))
    rows, bad, skipped = [], 0, 0
    for i, f in enumerate(files, 1):
        try:
            m = json.load(gzip.open(f))
        except Exception:
            bad += 1  # 수집 중 쓰이고 있는 파일 등
            continue
        info = m.get("info") or {}
        if info.get("queueId") != 420 or not info.get("participants"):
            skipped += 1; continue
        day = datetime.fromtimestamp(info["gameStartTimestamp"] / 1000, KST).strftime("%Y-%m-%d")
        if day < lo or day > hi:
            skipped += 1; continue
        dur = info.get("gameDuration", 0)
        dur = dur / 1000 if dur > 100000 else dur  # 옛 형식(ms) 방어
        ps = info["participants"]
        if dur < 300 or any(p.get("gameEndedInEarlySurrender") for p in ps):
            skipped += 1; continue
        gv = ".".join(info.get("gameVersion", "").split(".")[:2])
        patch = gv_patch(gv)
        if patch is None:
            skipped += 1; continue
        for p in ps:
            ch = p.get("challenges") or {}
            r = {k: p.get(k) for k in PF}
            r.update({f"ch_{k}": ch.get(k) for k in CH})
            r.update(matchId=m["metadata"]["matchId"], day=day, patch=patch, game_version=gv, dur_min=dur / 60,
                     date_window=window_of(day, w))
            rows.append(r)
        if i % 5000 == 0:
            log.info("경기 원본 %d/%d 읽음", i, len(files))
    df = pd.DataFrame(rows)
    log.info("파일 %d개 · 사용 %d경기 · 제외 %d · 읽기 실패 %d", len(files), df["matchId"].nunique(), skipped, bad)
    # 팀 내 몫 (막은 피해)
    tm = df.groupby(["matchId", "teamId"])["damageSelfMitigated"].transform("sum")
    df["mitig_share"] = df["damageSelfMitigated"] / tm.replace(0, np.nan)
    key2id = pd.read_csv(PROCESSED / "d_champions.csv").set_index("champion_key")["champion_id"]
    df["champion_id"] = df["championId"].map(key2id)
    df = df[df["teamPosition"].isin(POS)]
    return df


# ---------------------------------------------------------------- 속성
def per_row(df: pd.DataFrame) -> pd.DataFrame:
    d = pd.DataFrame(index=df.index)
    mn = df["dur_min"]
    d["phys"] = df["physicalDamageDealtToChampions"]; d["magic"] = df["magicDamageDealtToChampions"]
    d["true"] = df["trueDamageDealtToChampions"]; d["dmg"] = df["totalDamageDealtToChampions"]
    d["dmg_share"] = df["ch_teamDamagePercentage"]
    d["taken_share"] = df["ch_damageTakenOnTeamPercentage"]
    d["mitig_share"] = df["mitig_share"]
    d["taken_per_death"] = df["totalDamageTaken"] / (df["deaths"] + 1)
    d["taken_phys_share"] = df["physicalDamageTaken"] / df["totalDamageTaken"].replace(0, np.nan)
    d["deaths_pm"] = df["deaths"] / mn
    d["immob_pm"] = df["ch_enemyChampionImmobilizations"] / mn
    d["cc_time_pm"] = df["timeCCingOthers"] / mn
    d["cc_kill_pm"] = (df["ch_immobilizeAndKillWithAlly"].fillna(0) + df["ch_knockEnemyIntoTeamAndKill"].fillna(0)) / mn
    d["pick_pm"] = df["ch_pickKillWithAlly"] / mn
    d["ally_heal_shield_pm"] = (df["totalHealsOnTeammates"] + df["totalDamageShieldedOnTeammates"]) / mn
    d["eff_heal_shield_pm"] = df["ch_effectiveHealAndShielding"] / mn
    d["taken_pm"] = df["totalDamageTaken"] / mn
    d["mitig_pm"] = df["damageSelfMitigated"] / mn
    # 다이브·암살
    d["outnumbered_pm"] = df["ch_outnumberedKills"] / mn
    d["kills_near_turret_pm"] = df["ch_killsNearEnemyTurret"] / mn
    d["multikills_pm"] = df["ch_multikills"] / mn
    d["solokills_pm"] = df["ch_soloKills"] / mn
    # 사이드·스플릿
    d["solo_turrets_late_pm"] = df["ch_soloTurretsLategame"] / mn
    d["bldg_pm"] = df["damageDealtToBuildings"] / mn
    d["plates_pm"] = df["ch_turretPlatesTaken"] / mn
    d["solo_kill_share"] = df["ch_soloKills"] / df["kills"].replace(0, np.nan)
    # 라인전 (맞라인 상대 대비)
    d["lane_adv"] = df["ch_laningPhaseGoldExpAdvantage"]
    d["max_cs_adv"] = df["ch_maxCsAdvantageOnLaneOpponent"]
    # 포킹
    d["skillshots_pm"] = df["ch_skillshotsHit"] / mn
    # 궁극기 (관측): 분당 사용 횟수, 스킬 한 번으로 여러 명 처치, 아군 쪽으로 밀어 넣고 처치
    col = lambda c: df[c] if c in df else np.nan  # noqa: E731  (옛 참가자 표 --reuse 대비)
    d["r_casts_pm"] = col("spell4Casts") / mn
    d["one_spell_mk_pm"] = col("ch_multiKillOneSpell") / mn
    d["knock_kill_pm"] = df["ch_knockEnemyIntoTeamAndKill"] / mn
    d["win"] = df["win"].astype(float)
    d["early"] = (mn < EARLY).astype(float); d["late"] = (mn >= LATE).astype(float)
    d["win_early"] = d["win"] * d["early"]; d["win_late"] = d["win"] * d["late"]
    return d


MEAN_COLS = ["dmg_share", "taken_share", "mitig_share", "taken_per_death", "taken_phys_share", "deaths_pm",
             "immob_pm", "cc_time_pm", "cc_kill_pm", "pick_pm", "ally_heal_shield_pm", "eff_heal_shield_pm",
             "taken_pm", "mitig_pm", "outnumbered_pm", "kills_near_turret_pm", "multikills_pm", "solokills_pm",
             "solo_turrets_late_pm", "bldg_pm", "plates_pm", "solo_kill_share", "lane_adv", "max_cs_adv",
             "skillshots_pm", "r_casts_pm", "one_spell_mk_pm", "knock_kill_pm", "win"]
SUM_COLS = ["phys", "magic", "true", "dmg", "early", "late", "win_early", "win_late"]


def aggregate(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    d = per_row(df)
    for k in keys:
        d[k] = df[k]
    g = d.groupby(keys)
    out = g[MEAN_COLS].mean()
    out = out.join(g[SUM_COLS].sum())
    out["n"] = g.size()
    out["phys_share"] = out["phys"] / out["dmg"]
    out["magic_share"] = out["magic"] / out["dmg"]
    out["true_share"] = out["true"] / out["dmg"]
    out["wr_early"] = out["win_early"] / out["early"].replace(0, np.nan)
    out["wr_late"] = out["win_late"] / out["late"].replace(0, np.nan)
    return out.drop(columns=["phys", "magic", "true", "dmg", "win_early", "win_late"]).reset_index()


VALUE_COLS = MEAN_COLS + ["phys_share", "magic_share", "true_share"]


def shrink(x: pd.DataFrame, prior: pd.DataFrame, on: list[str], cols: list[str], n="n") -> pd.DataFrame:
    p = x[on].merge(prior[on + cols], on=on, how="left")
    for c in cols:
        pv = p[c].to_numpy()
        xv = x[c].to_numpy()
        nn = x[n].to_numpy()
        est = (nn * xv + K_SHRINK * pv) / (nn + K_SHRINK)
        x[c] = np.where(np.isnan(pv), xv, np.where(np.isnan(xv), pv, est))
    return x


def pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True) * 100


def attributes(df: pd.DataFrame, st: pd.DataFrame) -> pd.DataFrame:
    by_patch = aggregate(df, ["patch", "teamPosition", "champion_id"])
    all_ = aggregate(df, ["teamPosition", "champion_id"]).assign(patch="ALL")
    pos_mean = aggregate(df, ["teamPosition"])
    # 전체 패치 → 포지션 평균으로, 패치별 → 전체 패치 값으로 당김
    all_ = shrink(all_, pos_mean, ["teamPosition"], VALUE_COLS)
    by_patch = shrink(by_patch, all_, ["teamPosition", "champion_id"], VALUE_COLS)
    out = pd.concat([by_patch, all_], ignore_index=True)

    # 성장 곡선: (늦게 끝난 판 승률 − 일찍 끝난 판 승률), 포지션 평균 대비, 판수로 당김.
    # 패치별로는 인접 패치 상관이 0 근처라(1차 평가) 전체 패치 값을 패치 행에도 그대로 씀.
    sc = out["wr_late"] - out["wr_early"]
    m = np.minimum(out["early"], out["late"]).fillna(0)
    sc = (m * sc.fillna(0)) / (m + K_SHRINK)
    out["scaling"] = sc - sc.groupby([out["patch"], out["teamPosition"]]).transform("mean")
    allsc = out.loc[out["patch"] == "ALL"].set_index(["teamPosition", "champion_id"])["scaling"]
    out["scaling"] = pd.MultiIndex.from_frame(out[["teamPosition", "champion_id"]]).map(allsc.to_dict().get)
    out["scaling"] = out["scaling"].astype(float)

    out = out.merge(st[["patch", "champion_id", "name_ko", "meraki_roles", "dd_damage_type", "st_hard_cc", "st_aoe_cc",
                        "st_single_cc", "st_soft_cc", "st_dash", "st_r_hard_cc", "st_r_aoe_cc", "attackrange",
                        "melee", "range_qwe_max", "range_qwe_med", "range_r", "r_global", "r_cd1", "r_cd3"]],
                    on=["patch", "champion_id"], how="left")
    # 같은 (패치, 포지션) 안 백분위 → 축 점수
    g = out.groupby(["patch", "teamPosition"])
    P = lambda c: g[c].transform(pct)  # noqa: E731
    out["ax_frontline"] = (P("taken_share") + P("mitig_share") + P("taken_per_death")) / 3
    out["ax_engage"] = (P("immob_pm") + P("cc_time_pm") + P("cc_kill_pm")) / 3
    # 캐치: ch_pickKillWithAlly 는 1차 평가에서 CATCHER 를 못 가려내(AUC 0.48) 제외.
    # 단일 대상 단단한 CC(툴팁) + 적을 묶은 횟수(관측)
    out["ax_catch"] = (P("st_single_cc") + P("immob_pm")) / 2
    out["ax_protect"] = (P("ally_heal_shield_pm") + P("eff_heal_shield_pm")) / 2
    # 성장 축: 승률 차(scaling)는 인접 패치 상관이 0 근처라 축에서 뺌 (2026-11-05).
    # 대신 타임라인 25분 − 10분 골드 차이(ax_growth, timeline_features)를 씀. scaling 값은 참고로만 남김.
    out["ax_carry"] = P("dmg_share")
    # 2차 추가 축 (2026-11-04 시험: 이름표 AUC·대회 골드 차이로 고름)
    out["ax_dive"] = (P("outnumbered_pm") + P("kills_near_turret_pm") + P("multikills_pm") + P("solokills_pm")
                      + P("st_dash")) / 5
    out["ax_split"] = (P("solo_turrets_late_pm") + P("bldg_pm") + P("plates_pm") + P("solo_kill_share")) / 4
    # 라인전: 맞라인 1:1 인 탑·미드·서폿만 (원딜·정글은 대회 15분 골드 차이와 맞지 않아 비움)
    lane = (P("lane_adv") + P("max_cs_adv")) / 2
    out["ax_lane"] = lane.where(out["teamPosition"].isin(["TOP", "MIDDLE", "UTILITY"]))
    out["ax_poke"] = (P("attackrange") + P("skillshots_pm")) / 2
    # 사거리 (툴팁, 2026-11-05): 기본 공격 + 일반 스킬 중간 사거리. 카운터(사거리 차이)·조합(팀 평균)에는 원래 값(range_*)을 씀
    out["ax_range"] = (P("attackrange") + P("range_qwe_med")) / 2
    # 궁극기 (2026-11-05): 한타형 = 궁 광역 단단한 CC(툴팁) + 한 스킬 다중 처치 + 밀어 넣고 처치 (관측)
    out["ax_ult_fight"] = (out.assign(_r=out["st_r_aoe_cc"].astype(float)).groupby(["patch", "teamPosition"])["_r"].transform(pct)
                           + P("one_spell_mk_pm") + P("knock_kill_pm")) / 3
    out["ax_ult_uptime"] = P("r_casts_pm")  # 궁을 자주 쓰나 (재사용 대기시간·사용 습관)
    out["damage_label"] = np.select([out["magic_share"] >= 0.6, out["phys_share"] >= 0.6], ["AP", "AD"], "Hybrid")
    out["reliable"] = out["n"] >= MIN_RELIABLE
    out = out.rename(columns={"teamPosition": "position"})
    first = ["patch", "position", "champion_id", "name_ko", "n", "reliable", "damage_label", "phys_share",
             "magic_share", "true_share", "ax_frontline", "ax_engage", "ax_catch", "ax_protect", "ax_carry",
             "ax_dive", "ax_split", "ax_lane", "ax_poke", "ax_range", "ax_ult_fight", "ax_ult_uptime"]
    return out[first + [c for c in out.columns if c not in first]]


# ---------------------------------------------------------------- 타임라인 (v3)
LITE = RAW / "riot" / "timeline_lite"
TL_MIN = (10, 15, 20, 25)
BURST_SHARE = 0.7  # 킬러가 희생자 피해의 70% 이상을 준 킬만 순간 딜 계산에 씀
END_MIN = 30       # 25분–끝 구간은 30분 이상 판만 (구간이 5분 이상)
LATE_DIR = RAW / "riot" / "timeline_late"  # 30분 이상 판의 25분~끝 1분 프레임 (g_soloq_detail --steps late)


def timeline_table() -> pd.DataFrame:
    """타임라인 v3 (리서치 선수 58명이 있었던 판, 10명 전원) → 경기 × 참가자 × 분 기록 + 킬별 순간 딜."""
    w = research_windows()
    key2id = pd.read_csv(PROCESSED / "d_champions.csv").set_index("champion_key")["champion_id"].to_dict()
    rows, burst, seg = [], [], []
    for f in glob.glob(str(LITE / "*.json.gz")):
        try:
            tl = json.loads(gzip.decompress(Path(f).read_bytes()))
        except Exception:
            continue
        if tl.get("v", 1) < 3:
            continue
        mid = Path(f).name[:-8]
        mf = MATCH_DIR / f"{mid}.json.gz"
        if not mf.exists():
            continue
        info = json.load(gzip.open(mf))["info"]
        if info.get("queueId") != 420:
            continue
        day = datetime.fromtimestamp(info["gameStartTimestamp"] / 1000, KST).strftime("%Y-%m-%d")
        patch = gv_patch(info.get("gameVersion", ""))
        ps = info["participants"]
        if not (w["window_from"].min() <= day <= w["window_to"].max()):
            continue
        if patch is None or info["gameDuration"] < 300 or any(p.get("gameEndedInEarlySurrender") for p in ps):
            continue
        by_pu = {p["puuid"]: p for p in ps}
        pid = {int(k): v for k, v in tl["pid"].items()}
        opp = {}
        for p in ps:
            for q in ps:
                if q["teamId"] != p["teamId"] and q["teamPosition"] == p["teamPosition"]:
                    opp[p["puuid"]] = q["puuid"]
        for m in TL_MIN:
            fr = tl["frames"].get(str(m))
            if not fr:
                continue
            team_dmg = {}
            for pu, v in fr.items():
                t = by_pu[pu]["teamId"]
                team_dmg[t] = team_dmg.get(t, 0) + (v.get("dmg") or 0)
            for pu, v in fr.items():
                p = by_pu[pu]
                o = fr.get(opp.get(pu)) if opp.get(pu) else None
                rows.append({"matchId": mid, "teamId": p["teamId"], "patch": patch, "minute": m, "teamPosition": p["teamPosition"],
                             "champion_id": key2id.get(p["championId"]), "gold": v["gold"], "dmg": v.get("dmg"),
                             "dmg_share": (v.get("dmg") or 0) / team_dmg[p["teamId"]] if team_dmg[p["teamId"]] else np.nan,
                             "gold_diff": v["gold"] - o["gold"] if o else np.nan})
        # 구간 딜 (2026-11-05): 0–10분, 20–25분, 25분–끝(경기 원본의 최종 딜·골드, 30분 이상 판만)
        f10, f20, f25 = (tl["frames"].get(str(m)) for m in (10, 20, 25))
        # 30분 이후 프레임 (g_soloq_detail --steps late, 30분 이상 판만)
        f30 = f35 = None
        lf = LATE_DIR / f"{mid}.json.gz"
        if lf.exists():
            try:
                lt = json.loads(gzip.decompress(lf.read_bytes()))["frames"]
                f30, f35 = lt.get("30"), lt.get("35")
            except Exception:
                pass
        if f10 and f25:
            end_ok = info["gameDuration"] >= END_MIN * 60
            for p in ps:
                a, b, c = f10.get(p["puuid"]), (f20 or {}).get(p["puuid"]), f25.get(p["puuid"])
                if not a or not c or a.get("dmg") is None or c.get("dmg") is None:
                    continue
                b_ok = bool(b) and b.get("dmg") is not None
                seg.append({"matchId": mid, "teamId": p["teamId"], "teamPosition": p["teamPosition"],
                            "champion_id": key2id.get(p["championId"]),
                            "d0_10": a["dmg"], "g0_10": a["gold"],
                            "d20_25": c["dmg"] - b["dmg"] if b_ok else np.nan,
                            "g20_25": c["gold"] - b["gold"] if b_ok else np.nan,
                            "d25_end": p["totalDamageDealtToChampions"] - c["dmg"] if end_ok else np.nan,
                            "g25_end": p["goldEarned"] - c["gold"] if end_ok else np.nan,
                            # 30분 이후: 25–30분, 30–35분, 35분–끝 (각 구간이 있는 판만)
                            **late_seg_cols(p, c, f30, f35, info["gameDuration"])})
        for t, killer, victim, assists, x, y, dmg, n_inst in tl["kills"]:
            tot = sum(sum(v) for v in dmg.values())
            kd = dmg.get(str(killer))
            if not killer or not kd or tot <= 0 or n_inst <= 0:
                continue
            share = sum(kd) / tot
            if share >= BURST_SHARE:
                p = by_pu[pid[killer]]
                burst.append({"matchId": mid, "teamId": p["teamId"], "teamPosition": p["teamPosition"], "champion_id": key2id.get(p["championId"]),
                              "burst_per_hit": sum(kd) / n_inst, "kill_dmg": sum(kd)})
    return pd.DataFrame(rows), pd.DataFrame(burst), pd.DataFrame(seg)


def late_seg_cols(p: dict, c25: dict, f30: dict | None, f35: dict | None, dur: int) -> dict:
    """30분 이후 프레임으로 구간 딜·골드 (25–30, 30–35, 35–끝). 없으면 NaN."""
    nan = {k: np.nan for k in ("d25_30", "d30_35", "d35_end", "gold30", "gold35")}
    a = (f30 or {}).get(p["puuid"])
    if not a or a.get("dmg") is None:
        return nan
    out = dict(nan, d25_30=a["dmg"] - c25["dmg"], gold30=a["gold"])
    b = (f35 or {}).get(p["puuid"])
    if b and b.get("dmg") is not None:
        out.update(d30_35=b["dmg"] - a["dmg"], gold35=b["gold"])
        if dur >= 40 * 60:  # 35분–끝 구간이 5분 이상
            out["d35_end"] = p["totalDamageDealtToChampions"] - b["dmg"]
    return out


def segment_features(seg: pd.DataFrame) -> pd.DataFrame:
    """구간 딜 몫 (그 구간에 팀이 넣은 딜 중 몫)과 골드당 딜 → (포지션, 챔피언) 평균."""
    s = seg[seg["teamPosition"].isin(POS)].copy()
    for c in ("d0_10", "d20_25", "d25_end", "d25_30", "d30_35", "d35_end"):
        if c not in s:
            s[c] = np.nan
        team = s.groupby(["matchId", "teamId"])[c].transform("sum")
        s[c.replace("d", "sh", 1)] = s[c] / team.where(team > 0)
    s["late_seg"] = s["sh20_25"] - s["sh0_10"]     # 20–25분 딜 몫 − 0–10분 딜 몫
    s["late_end"] = s["sh25_end"] - s["sh0_10"]    # 25분–끝 딜 몫 − 0–10분 딜 몫 (30분 이상 판)
    s["late_30_35"] = s["sh30_35"] - s["sh0_10"]   # 30–35분 (35분 이상 판)
    s["late_35_end"] = s["sh35_end"] - s["sh0_10"]  # 35분–끝 (40분 이상 판)
    dpg_e = s["d0_10"] / s["g0_10"].where(s["g0_10"] > 0)
    dpg_l = s["d25_end"] / s["g25_end"].where(s["g25_end"] > 0)
    s["gold_eff"] = np.log(dpg_l.where(dpg_l > 0) / dpg_e.where(dpg_e > 0))  # 골드당 딜이 후반에 몇 배 커지나 (로그)
    g = s.groupby(["teamPosition", "champion_id"])
    out = g[["sh0_10", "sh20_25", "sh25_end", "sh25_30", "sh30_35", "sh35_end", "late_seg", "late_end",
             "late_30_35", "late_35_end", "gold_eff"]].mean()
    out["n_seg"] = g["late_seg"].count()
    out["n_end"] = g["late_end"].count()
    out["n_30_35"] = g["late_30_35"].count()
    out["n_35_end"] = g["late_35_end"].count()
    out = out.reset_index()
    gp = out.groupby("teamPosition")
    out["ax_late_seg"] = gp["late_seg"].transform(pct).where(out["n_seg"] >= MIN_RELIABLE)
    out["ax_late_end"] = gp["late_end"].transform(pct).where(out["n_end"] >= MIN_RELIABLE)
    out["ax_gold_eff"] = gp["gold_eff"].transform(pct).where(out["n_end"] >= MIN_RELIABLE)
    out["ax_late_30_35"] = gp["late_30_35"].transform(pct).where(out["n_30_35"] >= MIN_RELIABLE)
    # 안정성 (경기 반분)
    from scipy.stats import spearmanr
    h = s["matchId"].map(lambda m: zlib.crc32(m.encode()) % 2)
    for c in ("late_seg", "late_end", "late_30_35", "gold_eff"):
        if s[c].notna().sum() == 0:
            continue
        ab = [s[h == k].groupby(["teamPosition", "champion_id"])[c].agg(["mean", "count"]) for k in (0, 1)]
        j = ab[0].join(ab[1], lsuffix="_a", rsuffix="_b", how="inner")
        j = j[(j["count_a"] >= MIN_RELIABLE) & (j["count_b"] >= MIN_RELIABLE)].reset_index()
        TL_STAB[c] = (len(j), spearmanr(j["mean_a"], j["mean_b"]).statistic,
                      np.nanmedian([spearmanr(x["mean_a"], x["mean_b"]).statistic for _, x in j.groupby("teamPosition") if len(x) >= 8]))
    return out


def timeline_features() -> pd.DataFrame | None:
    """(포지션, 챔피언) 전체 기간: 분별 골드 차이·딜 몫, 성장(25분 − 10분 골드 차이), 순간 딜."""
    fr, bu, sg = timeline_table()
    if fr.empty:
        return None
    fr = fr[fr["teamPosition"].isin(POS)]
    piv = fr.pivot_table(index=["matchId", "teamPosition", "champion_id"], columns="minute",
                         values=["gold_diff", "dmg_share"]).reset_index()
    piv.columns = ["matchId", "teamPosition", "champion_id"] + [f"{a}{b}" for a, b in piv.columns[3:]]
    piv["growth"] = piv.get("gold_diff25") - piv.get("gold_diff10")
    piv["dmg_curve"] = piv.get("dmg_share25") - piv.get("dmg_share10")
    g = piv.groupby(["teamPosition", "champion_id"])
    out = g[[c for c in piv.columns if c not in ("matchId", "teamPosition", "champion_id")]].mean()
    out["n_tl"] = g.size()
    out["n_tl25"] = g["growth"].count()
    if not bu.empty:
        b = bu.groupby(["teamPosition", "champion_id"]).agg(burst_per_hit=("burst_per_hit", "median"),
                                                             n_burst=("burst_per_hit", "size"))
        out = out.join(b)
    out = out.reset_index()
    ok = out["n_tl25"] >= MIN_RELIABLE
    gp = out.groupby("teamPosition")
    out["ax_growth"] = gp["growth"].transform(pct).where(ok)
    out["ax_dmg_late"] = gp["dmg_curve"].transform(pct).where(ok)
    if "burst_per_hit" in out:
        out["ax_burst"] = gp["burst_per_hit"].transform(pct).where(out["n_burst"] >= MIN_RELIABLE)
    # 안정성: 경기를 반으로 나눠 계산한 값끼리 순위 상관 (평가 2-3)
    from scipy.stats import spearmanr
    h = piv["matchId"].map(lambda s: zlib.crc32(s.encode()) % 2)
    TL_STAB.clear()
    for c in ("growth", "dmg_curve", "gold_diff15"):
        if c not in piv:
            continue
        ab = [piv[h == k].groupby(["teamPosition", "champion_id"])[c].agg(["mean", "count"]) for k in (0, 1)]
        j = ab[0].join(ab[1], lsuffix="_a", rsuffix="_b", how="inner")
        j = j[(j["count_a"] >= MIN_RELIABLE) & (j["count_b"] >= MIN_RELIABLE)].reset_index()
        TL_STAB[c] = (len(j), spearmanr(j["mean_a"], j["mean_b"]).statistic,
                      np.nanmedian([spearmanr(g["mean_a"], g["mean_b"]).statistic for _, g in j.groupby("teamPosition") if len(g) >= 8]))
    if not bu.empty:
        hb = pd.Series(np.arange(len(bu)) % 2)  # 킬 단위로 반 나눔
        ab = [bu[hb == k].groupby(["teamPosition", "champion_id"])["burst_per_hit"].agg(["median", "count"]) for k in (0, 1)]
        j = ab[0].join(ab[1], lsuffix="_a", rsuffix="_b", how="inner")
        j = j[(j["count_a"] >= MIN_RELIABLE) & (j["count_b"] >= MIN_RELIABLE)].reset_index()
        TL_STAB["burst_per_hit"] = (len(j), spearmanr(j["median_a"], j["median_b"]).statistic,
                                    np.nanmedian([spearmanr(g["median_a"], g["median_b"]).statistic for _, g in j.groupby("teamPosition") if len(g) >= 8]))
    if not sg.empty:
        out = out.merge(segment_features(sg), on=["teamPosition", "champion_id"], how="left")
    log.info("타임라인 %d경기 · 챔피언×포지션 %d (25분까지 간 판 30 이상 %d)", fr["matchId"].nunique(), len(out), ok.sum())
    return out


TL_STAB: dict = {}


# ---------------------------------------------------------------- 평가
def auc(y: np.ndarray, s: np.ndarray) -> float:
    from scipy.stats import mannwhitneyu
    ok = ~np.isnan(s.astype(float))
    y, s = y[ok], s[ok]
    pos, neg = s[y], s[~y]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    return mannwhitneyu(pos, neg).statistic / (len(pos) * len(neg))


def evaluate(df: pd.DataFrame, at: pd.DataFrame, st: pd.DataFrame) -> str:
    from scipy.stats import spearmanr
    L = ["# 챔피언 속성표 평가", "",
         f"- 자료: 솔랭 {df['matchId'].nunique():,}경기 · 참가자 {len(df):,}행 · 리서치 패치 창 안 · 생성 {datetime.now(KST):%Y-%m-%d %H:%M}",
         f"- 단위: 패치 × 챔피언 × 포지션. 판수 {MIN_RELIABLE} 미만은 `reliable = False`", ""]
    a = at[(at["patch"] == "ALL") & at["reliable"]].copy()
    roles = a["meraki_roles"].fillna("")

    # 1. 타당성
    L += ["## 1. 타당성 — Meraki 이름표를 점수가 가려내나 (전체 패치, 판수 30 이상)", "",
          "| 축 | 이름표 | 범위 | 이름표 있음 | AUC |", "|---|---|---|---|---|"]
    checks = [("ax_frontline", "TANK", None), ("ax_engage", "VANGUARD", None),
              ("ax_engage", "VANGUARD", ["JUNGLE", "UTILITY", "TOP"]),
              ("ax_catch", "CATCHER", ["UTILITY"]), ("ax_protect", "ENCHANTER", ["UTILITY"]),
              ("ax_carry", "MARKSMAN", None),
              ("ax_dive", "ASSASSIN", ["MIDDLE", "JUNGLE"]), ("ax_dive", "DIVER", ["TOP", "JUNGLE"]),
              ("ax_split", "JUGGERNAUT", ["TOP"]), ("ax_poke", "ARTILLERY", ["MIDDLE", "UTILITY"])]
    for ax, lab, pos in checks:
        b = a if pos is None else a[a["position"].isin(pos)]
        y = b["meraki_roles"].fillna("").str.contains(lab).to_numpy()
        L.append(f"| {ax} | {lab} | {'전체' if pos is None else '·'.join(pos)} | {y.sum()}/{len(b)} | {auc(y, b[ax].to_numpy()):.3f} |")
    # 딜 타입 일치
    dd = a[a["dd_damage_type"].isin(["AD", "AP"]) & (a["damage_label"] != "Hybrid")]
    agree = (dd["dd_damage_type"] == dd["damage_label"]).mean()
    L += ["", f"- 딜 타입: 실제 비중 60% 기준 AD/AP 라벨과 기존 `d_champions.damage_type` 일치 {agree:.1%} "
          f"({len(dd)}행, Hybrid {int((a['damage_label'] == 'Hybrid').sum())}행 제외)", ""]
    mis = a[a["dd_damage_type"].isin(["AD", "AP"]) & (a["damage_label"] != a["dd_damage_type"])]
    mis = mis.sort_values("n", ascending=False).head(15)
    L += ["어긋난 행 (판수 많은 순 15개):", "", "| 포지션 | 챔피언 | 기존 | 실제 | 물리 | 마법 | 고정 | 판수 |", "|---|---|---|---|---|---|---|---|"]
    for _, r in mis.iterrows():
        L.append(f"| {r.position} | {r.name_ko} | {r.dd_damage_type} | {r.damage_label} | {r.phys_share:.0%} | "
                 f"{r.magic_share:.0%} | {r.true_share:.0%} | {r.n} |")
    # 이름표 밖인데 이니시 높음
    L += ["", "이니시 점수 상위인데 VANGUARD 이름표가 없는 챔피언 (정글·서폿·탑, 상위 12):", ""]
    b = a[a["position"].isin(["JUNGLE", "UTILITY", "TOP"]) & ~roles.str.contains("VANGUARD")]
    L.append(", ".join(f"{r.name_ko}({r.position[:3]} {r.ax_engage:.0f})" for _, r in b.nlargest(12, "ax_engage").iterrows()))
    L.append("")

    # 2. 안정성: 경기를 반으로 나눠 두 번 계산
    L += ["## 2. 안정성", "", "### 2-1. 경기를 반으로 나눠 계산한 값끼리 순위 상관 (전체 패치, 양쪽 판수 30 이상)", "",
          "| 값 | 행 | Spearman |", "|---|---|---|"]
    h = df["matchId"].map(lambda s: zlib.crc32(s.encode()) % 2)
    halves = [aggregate(df[h == k], ["teamPosition", "champion_id"]) for k in (0, 1)]
    j = halves[0].merge(halves[1], on=["teamPosition", "champion_id"], suffixes=("_a", "_b"))
    j = j[(j["n_a"] >= 30) & (j["n_b"] >= 30)]
    for c in ["magic_share", "taken_share", "mitig_share", "immob_pm", "cc_kill_pm", "pick_pm", "ally_heal_shield_pm", "dmg_share",
              "r_casts_pm", "one_spell_mk_pm", "knock_kill_pm"]:
        if j[f"{c}_a"].notna().sum() < 10:
            continue
        r = spearmanr(j[f"{c}_a"], j[f"{c}_b"], nan_policy="omit").statistic
        L.append(f"| {c} | {len(j)} | {r:.3f} |")
    L += ["", "### 2-2. 인접 리서치 패치끼리 순위 상관 — 당기기 전 값 (양쪽 판수 30 이상)", "",
          "| 패치 쌍 | 행 | 마법 비중 | 받은 피해 몫 | 묶은 횟수 | 아군 회복·보호막 | 성장(승률 차) |", "|---|---|---|---|---|---|---|"]
    bp = aggregate(df, ["patch", "teamPosition", "champion_id"])
    bp["scaling"] = bp["wr_late"] - bp["wr_early"]
    ps = sorted(set(bp["patch"]) - {"ALL"})
    for p1, p2 in zip(ps, ps[1:]):
        x = bp[(bp["patch"] == p1) & (bp["n"] >= 30)].merge(bp[(bp["patch"] == p2) & (bp["n"] >= 30)],
                                                             on=["teamPosition", "champion_id"], suffixes=("_a", "_b"))
        cs = [spearmanr(x[f"{c}_a"], x[f"{c}_b"]).statistic for c in
              ["magic_share", "taken_share", "immob_pm", "ally_heal_shield_pm", "scaling"]]
        L.append(f"| {p1}→{p2} | {len(x)} | " + " | ".join(f"{v:.2f}" for v in cs) + " |")
    L += ["", "- 성장(승률 차)은 패치별로 안정적이지 않아 축에서 뺐음 (2026-11-05). 성장 축은 타임라인 골드 차이(2-3)로 교체", ""]
    if TL_STAB:
        names = {"growth": "성장 (25분 − 10분 골드 차이)", "dmg_curve": "후반 딜 몫 (25분 − 10분 딜 몫)",
                 "gold_diff15": "15분 골드 차이", "burst_per_hit": "순간 딜 (킬당 한 방 피해, 중앙값)",
                 "late_seg": "구간 딜 몫 (20–25분 − 0–10분)", "late_end": "구간 딜 몫 (25분–끝 − 0–10분, 30분 이상 판)",
                 "gold_eff": "골드당 딜 증가 (25분–끝 ÷ 0–10분, 로그)",
                 "late_30_35": "구간 딜 몫 (30–35분 − 0–10분, 35분 이상 판)"}
        L += ["### 2-3. 타임라인 축 — 반으로 나눠 계산한 값끼리 순위 상관 (전체 패치, 양쪽 30 이상)", "",
              "| 값 | 행 | Spearman (전체) | Spearman (포지션 안, 중앙값) |", "|---|---|---|---|"]
        for c, (nn, r_all, r_pos) in TL_STAB.items():
            L.append(f"| {names.get(c, c)} | {nn} | {r_all:.3f} | {r_pos:.3f} |")
        L += ["", "- 타임라인 축은 패치별로 나누지 않고 전체 기간 값을 모든 패치 행에 씀 (패치당 25분까지 간 판이 적음)", ""]

    # 3. 대회 일치 (OE)
    oe = pd.read_csv(PROCESSED / "a_oe_lck_2026_raw.csv",
                     usecols=["date", "position", "champion", "damagetakenperminute", "damagemitigatedperminute", "damageshare",
                              "golddiffat10", "golddiffat15", "golddiffat25"])
    oe = oe[(oe["position"].isin(OE_POS)) & (oe["date"].str[:10] <= research_windows()["window_to"].max())]
    nm = pd.read_csv(PROCESSED / "d_champion_name_map.csv").set_index("alias")["champion_id"]
    oe["champion_id"] = oe["champion"].map(lambda s: nm.get(norm(s)))
    oe["position"] = oe["position"].map(OE_POS)
    og = oe.groupby(["position", "champion_id"]).agg(n_oe=("damageshare", "size"), oe_taken=("damagetakenperminute", "mean"),
                                                    oe_mitig=("damagemitigatedperminute", "mean"), oe_share=("damageshare", "mean"),
                                                    oe_gd15=("golddiffat15", "mean"),
                                                    oe_growth=("golddiffat25", "mean")).reset_index()
    og["oe_growth"] = og["oe_growth"] - oe.groupby(["position", "champion_id"])["golddiffat10"].mean().values
    x = a.merge(og[og["n_oe"] >= 5], on=["position", "champion_id"])
    L += ["## 3. 대회 일치 — 솔랭 값 vs OE (LCK, 08-25까지, 대회 5판 이상 · 솔랭 30판 이상)", "",
          "| 솔랭 | OE | 행 | Spearman |", "|---|---|---|---|"]
    for s, o in [("taken_pm", "oe_taken"), ("mitig_pm", "oe_mitig"), ("dmg_share", "oe_share")]:
        L.append(f"| {s} | {o} | {len(x)} | {spearmanr(x[s], x[o]).statistic:.3f} |")
    L.append("")
    L.append(f"- 같은 포지션 안에서만 보면 (포지션 차이로 상관이 부풀지 않게):")
    for s, o in [("taken_pm", "oe_taken"), ("mitig_pm", "oe_mitig"), ("dmg_share", "oe_share")]:
        rs = [f"{p[:3]} {spearmanr(g[s], g[o]).statistic:.2f}" for p, g in x.groupby("position") if len(g) >= 8]
        L.append(f"  - {s}: " + ", ".join(rs))
    L.append("")
    L.append("- 라인전 축 ↔ OE 15분 골드 차이 (같은 포지션 안): " + ", ".join(
        f"{p[:3]} {spearmanr(g['ax_lane'], g['oe_gd15']).statistic:.2f}"
        for p, g in x.dropna(subset=["ax_lane"]).groupby("position") if len(g) >= 8))
    if "ax_growth" in x and x["ax_growth"].notna().sum() >= 20:
        L.append("- 성장 축(타임라인) ↔ OE (25분 − 10분 골드 차이) (같은 포지션 안): " + ", ".join(
            f"{p[:3]} {spearmanr(g['ax_growth'], g['oe_growth']).statistic:.2f}"
            for p, g in x.dropna(subset=["ax_growth", "oe_growth"]).groupby("position") if len(g) >= 8))
    L.append("")

    # 4. 자체 수치 ↔ 관측
    L += ["## 4. 자체 수치(Data Dragon 한국어 툴팁 CC 태그) ↔ 관측 이니시 점수", ""]
    y = a.merge(st.loc[st["patch"] == "ALL", ["champion_id", "rating_control"]], on="champion_id", how="left")
    for c in ["st_hard_cc", "st_aoe_cc", "st_single_cc", "st_r_aoe_cc", "rating_control"]:
        v = y[[c, "ax_engage"]].dropna()
        L.append(f"- {c} ↔ ax_engage: Spearman {spearmanr(v[c], v['ax_engage']).statistic:.3f} ({len(v)}행)")
    # 5. 사거리·궁극기 (2026-11-05)
    L += ["", "## 5. 사거리·궁극기", ""]
    for ax, lab, pos in [("ax_range", "MARKSMAN|ARTILLERY", None), ("ax_range", "ARTILLERY", ["MIDDLE", "UTILITY"])]:
        b = a if pos is None else a[a["position"].isin(pos)]
        y = b["meraki_roles"].fillna("").str.contains(lab).to_numpy()
        L.append(f"- {ax} ↔ {lab} ({'전체' if pos is None else '·'.join(pos)}): AUC {auc(y, b[ax].to_numpy()):.3f} ({y.sum()}/{len(b)})")
    v = a[["r_cd3", "r_casts_pm"]].dropna()
    v = v[v["r_cd3"] > 0]
    if len(v) > 20:
        L.append(f"- 궁 재사용 대기(3레벨, 툴팁) ↔ 분당 궁 사용(관측): Spearman {spearmanr(v['r_cd3'], v['r_casts_pm']).statistic:.3f} "
                 f"({len(v)}행, 음수면 맞음)")
    if a["ax_ult_fight"].notna().any():
        y = a["meraki_roles"].fillna("").str.contains("VANGUARD").to_numpy()
        L.append(f"- ax_ult_fight ↔ VANGUARD (전체): AUC {auc(y, a['ax_ult_fight'].to_numpy()):.3f}")
        L += ["", "한타형 궁 점수 상위 (포지션별 8개):", ""]
        for p, g in a.dropna(subset=["ax_ult_fight"]).groupby("position"):
            L.append(f"- {p}: " + ", ".join(f"{r.name_ko} {r.ax_ult_fight:.0f}" for _, r in g.nlargest(8, "ax_ult_fight").iterrows()))
    gl = st.loc[(st["patch"] == "ALL") & st["r_global"].fillna(False).astype(bool), "name_ko"].dropna()
    L += ["", f"- 장거리·전역 궁 판정 (사거리 3000 이상, {len(gl)}명): {', '.join(gl)}"]
    ml = st.loc[(st["patch"] == "ALL") & st["melee"].fillna(False).astype(bool)]
    L.append(f"- 근접(기본 공격 사거리 250 이하): {len(ml)}명 / {int((st['patch'] == 'ALL').sum())}명")
    L += ["", f"- Meraki 자료가 없는 챔피언: {', '.join(st.loc[(st['patch'] == 'ALL') & ~st['meraki'].fillna(False).astype(bool), 'name_ko'])} (Meraki 마지막 갱신 25.15, 이름표·rating_* 만 비어 있음)", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse", action="store_true", help="참가자 표를 다시 읽지 않음")
    a = ap.parse_args()
    if a.reuse and PARTS.exists():
        df = pd.read_csv(PARTS, dtype={"patch": str, "game_version": str, "matchId": str, "date_window": str})
    else:
        df = participants()
        df.drop(columns=["puuid"]).to_csv(PARTS, index=False, compression="gzip")
        df = df.drop(columns=["puuid"])
    st = static_table(df["patch"].dropna().unique().tolist())
    save_csv(st, PROCESSED / "h_champion_static.csv")
    at = attributes(df, st)
    tf = timeline_features()
    if tf is not None:
        save_csv(tf, PROCESSED / "h_champion_timeline.csv")
        tf = tf.rename(columns={"teamPosition": "position"})
        keep = ["position", "champion_id", "n_tl", "n_tl25"] + [c for c in tf.columns if c.startswith(("gold_diff", "dmg_share", "ax_"))]             + [c for c in ("growth", "dmg_curve", "burst_per_hit", "n_burst", "sh0_10", "sh20_25", "sh25_end",
                          "sh25_30", "sh30_35", "sh35_end", "late_seg", "late_end", "late_30_35", "late_35_end",
                          "gold_eff", "n_seg", "n_end", "n_30_35", "n_35_end") if c in tf]
        at = at.merge(tf[keep].rename(columns={c: f"tl_{c}" for c in keep if c.startswith(("gold_diff", "dmg_share"))}),
                      on=["position", "champion_id"], how="left")
        front = [c for c in at.columns if c.startswith("ax_")]
        lead = [c for c in at.columns[:at.columns.get_loc("ax_ult_uptime") + 1] if c not in front]
        at = at[lead + front + [c for c in at.columns if c not in lead and c not in front]]
    save_csv(at, PROCESSED / "h_champion_attributes.csv")
    rep = evaluate(df, at, st)
    (PROCESSED / "h_champion_attributes_eval.md").write_text(rep, encoding="utf-8")
    log.info("속성표 %d행 (패치별 %d, 전체 %d) 저장", len(at), (at.patch != "ALL").sum(), (at.patch == "ALL").sum())


if __name__ == "__main__":
    main()
