"""M2 조합(콤보) 찾기 — 인식(프로 밴픽) × 효과(솔랭 교전) (2026-11-06, docs/PIPELINE.md 7-7)

M2 3차에서 "같은 팀에 있다"만으로 본 교전 시너지는 0 → 쌍마다 따로, 경기 안에서 비교하는 방식으로 다시 봄.

[1] 인식 — 프로가 조합으로 여기나 (OE 5리그 08-25 까지, 같은 팀 챔피언@포지션 쌍)
    동시 픽 lift  = 같이 나온 팀-경기 수 ÷ 기대 (패치마다 두 챔피언 픽률 곱 × 팀-경기 수), 축소 (n + A) / (e + A)
    같은 차례 비율 = 두 장 동시 픽(선픽 팀 2·3, 4·5번째 / 후픽 팀 1·2, 3·4번째)에 같이 뽑힌 비율 ÷ 같은 포지션 쌍 평균
    밴 반응 lift  = 상대가 A 를 1페이즈에 뽑은 뒤 2페이즈 밴에 B 를 넣은 수 ÷ 기대 (그 패치 2페이즈 밴률, B 가 남아 있을 때)
    검증: 26.13 까지로 계산한 lift ↔ 26.14·26.15 대회의 실제 동시 픽 (순위 상관, 후보 쌍의 평가 기간 lift)
[2] 효과 — 실제로 같이 있을 때 더 되나 (솔랭 킬 기록, 같은 팀에 그 쌍이 있던 판만)
    ① 공동 딜 lift = 두 챔피언이 같은 희생자에 같이 딜을 넣은 킬 ÷ 기대(경기마다 각자 킬 관여 수 곱 ÷ 팀 킬 수)
    ② 경기 안 교전 비교 = 같은 경기 안에서 "둘 다 낀 교전" vs "하나만 낀 교전" 의 교전 승률 차 (선형 확률, 경기×쌍 고정효과,
       통제 = 양 팀 참여 인원·교전 시점 골드 차이·시간대). 같은 포지션 쌍 평균을 빼서(차이의 차이) 그 쌍만의 몫, 경험적 베이즈 축소
    낌 = 그 교전에서 적 희생자에게 딜을 넣었거나 죽음. 신뢰도: 경기 반분 상관
[4] 조합 표 = 인식 × 효과 4분류 (+ M2 3차의 15분 듀오 골드)

    python src/pipeline/m2_combos.py
"""
from __future__ import annotations

import glob
import gzip
import json
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
from d_champion_meta import norm  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m2" / "combos"
OUT.mkdir(parents=True, exist_ok=True)
EV_CACHE = PROCESSED / "m2" / "m2_combo_events.pkl"
POS = H.POS
PK = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서폿"}
FIGHT_GAP = 15
A_LIFT = 3.0         # 동시 픽 lift 축소 (가상 판수)
A_JOINT = 5.0        # 공동 딜 lift 축소
PERCEIVED = (5, 1.5)  # 인식 = 대회 동시 픽 5회 이상 & 축소 lift 1.5 이상
EFFECT_Z = 2.0       # (참고) 경기 안 교전 비교 z — 반분 재현이 안 돼 분류에 쓰지 않음
JOINT_PLAYS = (1.05, 3.0)
COMM_MIN = 3         # 커뮤니티 조합 언급 = 조합 단어와 한 문장에 3회 이상  # 같이 놂 = 공동 딜 lift(포지션 대비) 1.05 이상 & z 3 이상
MIN_STRATA = 30      # 효과 판단에 필요한 솔랭 팀-경기 수
EXAMPLES = [("LeeSin", "Ahri"), ("Camille", "Galio"), ("Ashe", "Seraphine"), ("LeeSin", "Orianna"), ("LeeSin", "JarvanIV")]


# ================================================================ [1] 프로 밴픽
def pro_drafts() -> pd.DataFrame:
    """팀-경기 1행: 픽 5개(챔피언@포지션, 순서), 밴 5개(챔피언), 선픽 여부, 상대 픽·밴."""
    cols = ["gameid", "league", "date", "patch", "side", "position", "champion", "firstPick"] + \
           [f"pick{i}" for i in range(1, 6)] + [f"ban{i}" for i in range(1, 6)]
    oe = pd.read_csv(M1.OE_FILE, usecols=cols, low_memory=False, dtype={"patch": str})
    oe = oe[oe["league"].isin(M1.LEAGUES) & (oe["date"].str[:10] <= M1.END)]
    nm = pd.read_csv(PROCESSED / "d_champion_name_map.csv").set_index("alias")["champion_id"]
    cid = lambda s: nm.get(norm(s)) if isinstance(s, str) else None  # noqa: E731
    pl = oe[oe["position"].isin(H.OE_POS)].copy()
    pl["cid"] = pl["champion"].map(cid)
    pos_of = {(r.gameid, r.side, r.cid): H.OE_POS[r.position] for r in pl.itertuples()}
    tm = oe[(oe["position"] == "team") & oe["firstPick"].notna()].copy()
    rows = []
    for r in tm.itertuples():
        picks = [cid(getattr(r, f"pick{i}")) for i in range(1, 6)]
        bans = [cid(getattr(r, f"ban{i}")) for i in range(1, 6)]
        if any(p is None for p in picks):
            continue
        keys = [f"{p}@{pos_of.get((r.gameid, r.side, p), '?')}" for p in picks]
        if any(k.endswith("@?") for k in keys):
            continue
        rows.append({"gameid": r.gameid, "league": r.league, "day": r.date[:10], "patch": M1.oe_patch_to_ours(r.patch),
                     "side": r.side, "first": int(r.firstPick) == 1, "picks": keys, "bans": bans})
    d = pd.DataFrame(rows)
    opp = d.set_index(["gameid", "side"])
    d["opp_picks"] = [opp.loc[(g, "Red" if s == "Blue" else "Blue"), "picks"] if (g, "Red" if s == "Blue" else "Blue") in opp.index else None
                      for g, s in zip(d["gameid"], d["side"])]
    d["opp_bans"] = [opp.loc[(g, "Red" if s == "Blue" else "Blue"), "bans"] if (g, "Red" if s == "Blue" else "Blue") in opp.index else None
                     for g, s in zip(d["gameid"], d["side"])]
    return d[d["opp_picks"].notna()].reset_index(drop=True)


def same_turn(first: bool, i: int, j: int) -> bool:
    """팀 안 픽 순서 i, j (0부터) 가 두 장 동시 픽 차례인가."""
    turns = [(1, 2), (3, 4)] if first else [(0, 1), (2, 3)]
    return tuple(sorted((i, j))) in turns


def pair_key(x: str, y: str) -> str:
    return "+".join(sorted((x, y)))


def draft_metrics(d: pd.DataFrame) -> pd.DataFrame:
    """쌍마다 동시 픽 lift · 같은 차례 비율 · 밴 반응 lift."""
    # 픽률 (패치 × 챔피언@포지션, 팀-경기 기준)
    ex = d[["patch", "picks"]].explode("picks")
    n_tg = d.groupby("patch").size()
    rate = ex.groupby(["patch", "picks"]).size().div(n_tg, level="patch").rename("r")
    co, st, role_st, role_n = {}, {}, {}, {}
    for r in d.itertuples():
        for i in range(5):
            for j in range(i + 1, 5):
                k = pair_key(r.picks[i], r.picks[j])
                co[k] = co.get(k, 0) + 1
                s = same_turn(r.first, i, j)
                st[k] = st.get(k, 0) + int(s)
                rp = "+".join(sorted((r.picks[i].split("@")[1], r.picks[j].split("@")[1])))
                role_st[rp] = role_st.get(rp, 0) + int(s)
                role_n[rp] = role_n.get(rp, 0) + 1
    # 기대 동시 픽: Σ_patch n × r(A) × r(B)
    rp_ = rate.reset_index()
    by_patch = {p: dict(zip(g["picks"], g["r"])) for p, g in rp_.groupby("patch")}
    exp = {}
    for k in co:
        x, y = k.split("+")
        exp[k] = sum(n_tg[p] * by_patch[p].get(x, 0) * by_patch[p].get(y, 0) for p in n_tg.index)
    # 밴 반응 (챔피언 단위): 상대가 1페이즈(픽 1~3)에 A → 우리 2페이즈 밴(4·5)에 B. 기대 = 그 패치 2페이즈 밴률 (B 가 남아 있을 때)
    ban2 = d[["patch"]].assign(b2=d["bans"].map(lambda b: [x for x in b[3:5] if x])).explode("b2").dropna()
    b2rate = ban2.groupby(["patch", "b2"]).size().div(n_tg, level="patch")
    q = {p: g.droplevel(0).to_dict() for p, g in b2rate.groupby(level=0)}
    champs = sorted(set(k.split("@")[0] for ks in d["picks"] for k in ks))
    bo, be = {}, {}
    for r in d.itertuples():
        mine2 = set(x for x in r.bans[3:5] if x)
        gone = set(c.split("@")[0] for c in r.picks[:3] + r.opp_picks[:3]) | set(x for x in r.bans[:3] + r.opp_bans[:3] if x)
        qp = q.get(r.patch, {})
        for a_key in r.opp_picks[:3]:
            a = a_key.split("@")[0]
            for b in champs:
                if b in gone or b == a:
                    continue
                e = qp.get(b, 0.0)
                if e == 0 and b not in mine2:
                    continue
                bo[(a, b)] = bo.get((a, b), 0) + int(b in mine2)
                be[(a, b)] = be.get((a, b), 0.0) + e
    out = pd.DataFrame({"key": list(co)})
    out["n_co"] = out["key"].map(co)
    out["exp_co"] = out["key"].map(exp)
    out["lift"] = (out["n_co"] + A_LIFT) / (out["exp_co"] + A_LIFT)
    out["raw_lift"] = out["n_co"] / out["exp_co"].where(out["exp_co"] > 0)
    out["role"] = out["key"].map(lambda k: "+".join(sorted(x.split("@")[1] for x in k.split("+"))))
    base = {rp: role_st[rp] / role_n[rp] for rp in role_n}
    out["same_turn"] = out["key"].map(st)
    out["same_turn_ratio"] = (out["same_turn"] + 2) / (out["n_co"] * out["role"].map(base) + 2)
    ca = out["key"].map(lambda k: tuple(x.split("@")[0] for x in k.split("+")))
    out["ban_obs"] = [bo.get((x, y), 0) + bo.get((y, x), 0) for x, y in ca]
    out["ban_exp"] = [be.get((x, y), 0.0) + be.get((y, x), 0.0) for x, y in ca]
    out["ban_lift"] = (out["ban_obs"] + 1) / (out["ban_exp"] + 1)
    return out


# ================================================================ [2] 솔랭 이벤트
def solo_events():
    """교전 표 (참여 위치 비트) + 킬 표 (킬 낸 팀의 딜 넣은 위치 비트). 캐시."""
    if EV_CACHE.exists():
        return pd.read_pickle(EV_CACHE)
    import m2_fights
    gold = m2_fights.team_gold()
    mins = np.array([0] + list(gold.columns), dtype=float) * 60
    F, K = [], []
    files = sorted(glob.glob(str(m2_fights.KDMG_DIR / "*.json.gz")))
    for n, f in enumerate(files, 1):
        mid = Path(f).name[:-8]
        try:
            kills = sorted(json.loads(gzip.decompress(Path(f).read_bytes()))["kills"], key=lambda x: x[0])
            info = json.load(gzip.open(H.MATCH_DIR / f"{mid}.json.gz"))["info"]
        except Exception:
            continue
        where = {p["participantId"]: (p["teamId"], POS.index(p["teamPosition"])) for p in info["participants"]
                 if p.get("teamPosition") in POS}
        if len(where) != 10:
            continue
        g = gold.loc[mid].to_numpy(float) if mid in gold.index else None
        fights, cur, last = [], [], None
        for kl in kills:
            if last is not None and kl[0] - last > FIGHT_GAP:
                fights.append(cur)
                cur = []
            cur.append(kl)
            last = kl[0]
        if cur:
            fights.append(cur)
        for c in fights:
            k = {100: 0, 200: 0}
            pres = {100: 0, 200: 0}
            for sec, killer, victim, hits in c:
                if victim not in where:
                    continue
                vt, vp = where[victim]
                kt = 300 - vt
                k[kt] += 1
                pres[vt] |= 1 << vp
                m = 0
                if killer in where and where[killer][0] == kt:
                    m |= 1 << where[killer][1]
                for h in hits:
                    pid, typ = h[0], h[7]
                    if typ == "OTHER" and pid in where and where[pid][0] == kt:
                        m |= 1 << where[pid][1]
                pres[kt] |= m
                K.append((mid, kt, m))
            if len(c) >= 2 and k[100] != k[200]:
                gd = np.nan
                if g is not None:
                    ok = ~np.isnan(np.r_[0.0, g])
                    gd = np.interp(c[0][0], mins[ok], np.r_[0.0, g][ok])
                F.append((mid, c[0][0], k[100], k[200], pres[100], pres[200], gd))
        if n % 5000 == 0:
            log.info("조합 이벤트 %d/%d", n, len(files))
    F = pd.DataFrame(F, columns=["matchId", "t", "blue_k", "red_k", "pres_b", "pres_r", "gold_diff"])
    K = pd.DataFrame(K, columns=["matchId", "team", "mask"])
    pd.to_pickle((F, K), EV_CACHE)
    return F, K


EV_CACHE_LOC = PROCESSED / "m2" / "m2_combo_events_loc.pkl"
FIGHT_RADIUS = 2500  # 같은 교전 = 15초 안 & 교전 중심에서 이 거리 안 (맵 약 15,000 × 15,000)


def solo_events_loc():
    """위치 기반 (2026-11-06 2차): timeline_lite 킬 기록 [초, 킬러, 희생자, [어시스트], x, y, {딜 준 id: …}, n].
    교전 = 시간(15초) + 장소(중심에서 FIGHT_RADIUS) 로 묶음 → 같은 시각 다른 라인 싸움은 따로.
    낌 = 킬러 · 어시스트(딜·CC·보호막 등 라이엇 기준) · 딜 준 사람 · 희생자."""
    if EV_CACHE_LOC.exists():
        return pd.read_pickle(EV_CACHE_LOC)
    import m2_fights
    gold = m2_fights.team_gold()
    mins = np.array([0] + list(gold.columns), dtype=float) * 60
    F, K = [], []
    files = sorted(glob.glob(str(H.LITE / "*.json.gz")))
    for n, f in enumerate(files, 1):
        mid = Path(f).name[:-8]
        try:
            tl = json.loads(gzip.decompress(Path(f).read_bytes()))
            info = json.load(gzip.open(H.MATCH_DIR / f"{mid}.json.gz"))["info"]
        except Exception:
            continue
        if tl.get("v", 1) < 3 or info.get("queueId") != 420:
            continue
        where = {p["participantId"]: (p["teamId"], POS.index(p["teamPosition"])) for p in info["participants"]
                 if p.get("teamPosition") in POS}
        if len(where) != 10:
            continue
        g = gold.loc[mid].to_numpy(float) if mid in gold.index else None
        open_f, done = [], []
        for kl in sorted(tl["kills"], key=lambda x: x[0]):
            t, killer, victim, assists, x, y, dmg = kl[0], kl[1], kl[2], kl[3] or [], kl[4], kl[5], kl[6] or {}
            if victim not in where or x is None:
                continue
            still = []
            for fi in open_f:
                (done if t - fi["last"] > FIGHT_GAP else still).append(fi)
            open_f = still
            best = None
            for fi in open_f:
                d = np.hypot(x - fi["cx"], y - fi["cy"])
                if d <= FIGHT_RADIUS and (best is None or d < best[0]):
                    best = (d, fi)
            if best is None:
                fi = {"t0": t, "last": t, "cx": x, "cy": y, "n": 0, "k": {100: 0, 200: 0}, "pres": {100: 0, 200: 0}}
                open_f.append(fi)
            else:
                fi = best[1]
            vt, vp = where[victim]
            kt = 300 - vt
            m = 0
            for q in [killer] + list(assists) + [int(k) for k in dmg]:
                if q in where and where[q][0] == kt:
                    m |= 1 << where[q][1]
            fi["k"][kt] += 1
            fi["pres"][kt] |= m
            fi["pres"][vt] |= 1 << vp
            fi["cx"] = (fi["cx"] * fi["n"] + x) / (fi["n"] + 1)
            fi["cy"] = (fi["cy"] * fi["n"] + y) / (fi["n"] + 1)
            fi["n"] += 1
            fi["last"] = t
            K.append((mid, kt, m))
        for fi in done + open_f:
            if fi["n"] >= 2 and fi["k"][100] != fi["k"][200]:
                gd = np.nan
                if g is not None:
                    ok = ~np.isnan(np.r_[0.0, g])
                    gd = np.interp(fi["t0"], mins[ok], np.r_[0.0, g][ok])
                F.append((mid, fi["t0"], fi["k"][100], fi["k"][200], fi["pres"][100], fi["pres"][200], gd))
        if n % 5000 == 0:
            log.info("조합 이벤트 (위치) %d/%d", n, len(files))
    F = pd.DataFrame(F, columns=["matchId", "t", "blue_k", "red_k", "pres_b", "pres_r", "gold_diff"])
    K = pd.DataFrame(K, columns=["matchId", "team", "mask"])
    pd.to_pickle((F, K), EV_CACHE_LOC)
    return F, K


PAIRS = [(i, j) for i in range(5) for j in range(i + 1, 5)]


def team_keys(players: pd.DataFrame) -> pd.DataFrame:
    """(matchId, teamId) × 5 포지션 챔피언@포지션."""
    p = players[players["teamPosition"].isin(POS)].copy()
    p["key"] = p["champion_id"].astype(str) + "@" + p["teamPosition"]
    return p.pivot_table(index=["matchId", "teamId"], columns="teamPosition", values="key", aggfunc="first")[POS]


def joint_damage(K: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    """① 공동 딜: 팀-경기 × 위치 쌍 → 관측 공동 킬, 기대."""
    K = K.copy()
    for b in range(5):
        K[f"b{b}"] = (K["mask"].to_numpy(np.int64) >> b) & 1  # pandas Series 는 >> 를 지원하지 않음
    g = K.groupby(["matchId", "team"])
    n = g.size().rename("n")
    s = g[[f"b{b}" for b in range(5)]].sum()
    rows = []
    for i, j in PAIRS:
        both = ((K[f"b{i}"] & K[f"b{j}"]).groupby([K["matchId"], K["team"]]).sum())
        t = pd.DataFrame({"obs": both, "exp": s[f"b{i}"] * s[f"b{j}"] / n}).reset_index()
        t["i"], t["j"] = i, j
        rows.append(t)
    t = pd.concat(rows, ignore_index=True).rename(columns={"team": "teamId"})
    kk = keys.dropna().reset_index()
    t = t.merge(kk, on=["matchId", "teamId"], how="inner")
    t["key"] = [pair_key(r[POS[i]], r[POS[j]]) for r, i, j in zip(t[POS].to_dict("records"), t["i"], t["j"])]
    t["role"] = [f"{POS[i]}+{POS[j]}" for i, j in zip(t["i"], t["j"])]
    a = t.groupby(["key", "role"]).agg(obs=("obs", "sum"), exp=("exp", "sum"), n_tg=("obs", "size")).reset_index()
    role = a.groupby("role")[["obs", "exp"]].sum()
    a["role_lift"] = a["role"].map(role["obs"] / role["exp"])
    a["joint_lift"] = (a["obs"] + A_JOINT * a["role_lift"]) / (a["exp"] + A_JOINT) / a["role_lift"]  # 같은 포지션 쌍 대비
    a["joint_z"] = (a["obs"] - a["exp"] * a["role_lift"]) / np.sqrt(a["exp"] * a["role_lift"]).clip(lower=1e-9)
    return a


def fight_rows(F: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    """② 교전 × 팀 × 위치 쌍 (둘 중 하나라도 낀 교전만) 긴 표."""
    out = []
    md = F["t"].to_numpy() / 60
    mb = np.digitize(md, [10, 20, 30])
    for team, mine, opp, sign in ((100, "pres_b", "pres_r", 1.0), (200, "pres_r", "pres_b", -1.0)):
        pm, po = F[mine].to_numpy(), F[opp].to_numpy()
        win = ((F["blue_k"] > F["red_k"]).to_numpy() == (team == 100)).astype(np.float32)
        nT = np.array([bin(x).count("1") for x in pm], dtype=np.float32)
        nO = np.array([bin(x).count("1") for x in po], dtype=np.float32)
        gd = F["gold_diff"].to_numpy() * sign
        for i, j in PAIRS:
            a, b = (pm >> i) & 1, (pm >> j) & 1
            sel = (a | b) == 1
            out.append(pd.DataFrame({"matchId": F["matchId"].to_numpy()[sel], "teamId": team, "i": i, "j": j,
                                     "y": win[sel], "both": (a & b)[sel].astype(np.float32), "nT": nT[sel], "nO": nO[sel],
                                     "gd": np.nan_to_num(gd[sel]).astype(np.float32), "gmiss": np.isnan(gd[sel]).astype(np.float32),
                                     "mb": mb[sel]}))
    R = pd.concat(out, ignore_index=True)
    kk = keys.dropna().reset_index()
    R = R.merge(kk, on=["matchId", "teamId"], how="inner")
    R["key"] = [pair_key(a_, b_) for a_, b_ in zip(R[POS].to_numpy()[np.arange(len(R)), R["i"]],
                                                   R[POS].to_numpy()[np.arange(len(R)), R["j"]])]
    R["role"] = [f"{POS[i]}+{POS[j]}" for i, j in zip(R["i"], R["j"])]
    return R.drop(columns=POS)


def within_effect(R: pd.DataFrame) -> pd.DataFrame:
    """경기 × 팀 × 쌍 고정효과 선형 확률. 통제 계수는 모든 쌍 공통(먼저 추정), 쌍마다 '둘 다 낌' 계수 + 경기 묶음 표준오차."""
    R = R.copy()
    for m in (1, 2, 3):
        R[f"mb{m}"] = (R["mb"] == m).astype(np.float32)
    ctrl = ["nT", "nO", "gd", "gmiss", "mb1", "mb2", "mb3"]
    grp = R.groupby(["matchId", "teamId", "i", "j"], sort=False)
    cols = ["y", "both"] + ctrl
    D = R[cols] - grp[cols].transform("mean")
    # 1) 공통 통제 계수 (둘 다 낌 포함 공동 회귀)
    X = D[["both"] + ctrl].to_numpy(float)
    gam = np.linalg.lstsq(X, D["y"].to_numpy(float), rcond=None)[0]
    ys = D["y"].to_numpy(float) - D[ctrl].to_numpy(float) @ gam[1:]
    xb = D["both"].to_numpy(float)
    R["_xy"], R["_xx"] = xb * ys, xb * xb
    a = R.groupby(["key", "role"]).agg(sxy=("_xy", "sum"), sxx=("_xx", "sum"), n_fight=("y", "size"),
                                       n_both=("both", "sum")).reset_index()
    a["beta"] = a["sxy"] / a["sxx"].where(a["sxx"] > 0)
    # 경기 묶음 표준오차
    R["_beta"] = R["key"].map(a.set_index("key")["beta"])
    R["_s"] = xb * (ys - R["_beta"].to_numpy() * xb)
    sg = R.groupby(["key", "matchId"])["_s"].sum()
    a["se"] = np.sqrt((sg ** 2).groupby("key").sum().reindex(a["key"]).to_numpy()) / a["sxx"]
    a["n_tg"] = R.groupby("key")["matchId"].nunique().reindex(a["key"]).to_numpy()
    a = a[a["sxx"] > 0].copy()
    # 같은 포지션 쌍 평균 (정밀도 가중) 을 빼고 경험적 베이즈 축소
    a["w"] = 1 / a["se"].clip(lower=1e-3) ** 2
    rb = a.groupby("role").apply(lambda g: np.average(g["beta"], weights=g["w"]))
    a["role_beta"] = a["role"].map(rb)
    a["delta"] = a["beta"] - a["role_beta"]
    tau2 = a.groupby("role").apply(lambda g: max(np.average(g["delta"] ** 2, weights=g["w"]) - np.average(g["se"] ** 2, weights=g["w"]), 1e-6))
    a["tau2"] = a["role"].map(tau2)
    a["delta_shrunk"] = a["delta"] * a["tau2"] / (a["tau2"] + a["se"] ** 2)
    a["z"] = a["delta"] / a["se"]
    return a.drop(columns=["w"]), gam


# ================================================================ 표
def ko_names() -> dict:
    d = pd.read_csv(H.ROOT / "research" / "config" / "ult_types.csv", dtype=str, encoding="utf-8-sig")
    return dict(zip(d["champion_id"], d["name_ko"]))


def lab(key: str, ko: dict) -> str:
    c, p = key.split("@")
    return f"{ko.get(c, c)}({PK.get(p, p)})"


def duo_gold() -> pd.DataFrame:
    rows = []
    for f in ("m2_jungle_duo.csv", "m2_bot_duo.csv"):
        p = PROCESSED / "m2" / f
        if p.exists():
            t = pd.read_csv(p)
            t["key"] = t["key"].map(lambda k: pair_key(*k.split("+")))
            rows.append(t[["key", "val", "lo", "hi"]].rename(columns={"val": "duo_gold", "lo": "duo_lo", "hi": "duo_hi"}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["key", "duo_gold", "duo_lo", "duo_hi"])


def main():
    t0 = time.time()
    ko = ko_names()
    w = H.research_windows().set_index("patch")
    # [1]
    d = pro_drafts()
    dm = draft_metrics(d)
    log.info("[1] 대회 팀-경기 %d, 쌍 %d (%.0f초)", len(d), len(dm), time.time() - t0)
    cut = w.loc["26.14", "window_from"]
    pre, post = d[d["day"] < cut], d[d["patch"].isin(["26.14", "26.15"])]
    dpre, dpost = draft_metrics(pre), draft_metrics(post)
    v = dpre.merge(dpost[["key", "n_co", "exp_co"]], on="key", suffixes=("", "_post"))
    v = v[v["exp_co_post"] >= 0.5]
    rho = spearmanr(np.log(v["lift"]), np.log((v["n_co_post"] + 0.5) / (v["exp_co_post"] + 0.5))).correlation
    flag_pre = (v["n_co"] >= PERCEIVED[0]) & (v["lift"] >= PERCEIVED[1])
    post_lift = lambda m: v.loc[m, "n_co_post"].sum() / v.loc[m, "exp_co_post"].sum()  # noqa: E731
    # [2]
    F, K = solo_events_loc() if "--dmg" not in sys.argv else solo_events()  # 기본 = 위치 기반 (2차)
    solo, _ = M1.solo_players()
    keys = team_keys(solo)
    jd = joint_damage(K, keys)
    R = fight_rows(F, keys)
    log.info("[2] 교전 %d · 킬 %d · 교전×쌍 행 %d (%.0f초)", len(F), len(K), len(R), time.time() - t0)
    we, gam = within_effect(R)
    half = R["matchId"].map(lambda s: zlib.crc32(s.encode()) % 2)
    h0, _ = within_effect(R[half == 0])
    h1, _ = within_effect(R[half == 1])
    hh = h0[["key", "delta", "n_tg"]].merge(h1[["key", "delta", "n_tg"]], on="key", suffixes=("_0", "_1"))
    hh = hh[(hh["n_tg_0"] >= MIN_STRATA) & (hh["n_tg_1"] >= MIN_STRATA)]
    rel = spearmanr(hh["delta_0"], hh["delta_1"]).correlation if len(hh) > 10 else np.nan
    # 공동 딜 반분
    jh = []
    for h in (0, 1):
        mids = set(R.loc[half == h, "matchId"])
        jh.append(joint_damage(K[K["matchId"].isin(mids)], keys))
    jj = jh[0][["key", "joint_lift", "n_tg"]].merge(jh[1][["key", "joint_lift", "n_tg"]], on="key", suffixes=("_0", "_1"))
    jj = jj[(jj["n_tg_0"] >= MIN_STRATA) & (jj["n_tg_1"] >= MIN_STRATA)]
    jrel = spearmanr(jj["joint_lift_0"], jj["joint_lift_1"]).correlation if len(jj) > 10 else np.nan
    log.info("[2] 끝 (%.0f초), 반분 상관 교전 %.3f · 공동 딜 %.3f", time.time() - t0, rel, jrel)

    # [4] 표
    T = dm.merge(we[["key", "beta", "role_beta", "delta", "delta_shrunk", "se", "z", "n_tg", "n_fight", "n_both"]], on="key", how="outer")
    T = T.merge(jd[["key", "joint_lift", "joint_z", "obs", "exp"]].rename(columns={"obs": "joint_obs", "exp": "joint_exp"}), on="key", how="left")
    T = T.merge(duo_gold(), on="key", how="left")
    # 커뮤니티 동시 언급 (m2_community.py, 챔피언 쌍 단위 → 포지션 붙은 쌍 모두에)
    cfile = OUT / "m2_combo_community.csv"
    if cfile.exists():
        cm = pd.read_csv(cfile)
        cm["ck"] = [pair_key(x, y) for x, y in zip(cm["a"], cm["b"])]
        T["ck"] = T["key"].map(lambda k: pair_key(*[x.split("@")[0] for x in k.split("+")]))
        T = T.merge(cm[["ck", "n_both", "n_combo", "comm_lift"]].rename(columns={"n_both": "comm_both", "n_combo": "comm_combo"}),
                    on="ck", how="left")
    T["role"] = T["key"].map(lambda k: "+".join(sorted(x.split("@")[1] for x in k.split("+"))))
    T[["n_co", "exp_co"]] = T[["n_co", "exp_co"]].fillna(0)
    T["lift"] = T["lift"].fillna((T["n_co"] + A_LIFT) / (T["exp_co"] + A_LIFT))
    T["a"] = T["key"].map(lambda k: lab(k.split("+")[0], ko))
    T["b"] = T["key"].map(lambda k: lab(k.split("+")[1], ko))
    # 분류 (1차 실행 뒤 수정): 경기 안 교전 비교는 반분 상관 0.02 로 재현이 안 돼 분류에 쓰지 않음 (참고 칸만).
    # 효과 = 평가 패치로 검증된 15분 듀오 골드(정글+미드·정글+탑·원딜+서폿) 구간 > 0. 같이 놂 = 공동 딜 lift (반분 0.65)
    perceived = (T["n_co"] >= PERCEIVED[0]) & (T["lift"] >= PERCEIVED[1])
    enough = T["n_tg"].fillna(0) >= MIN_STRATA
    T.loc[~enough, ["delta", "delta_shrunk", "z"]] = np.nan  # 판수 적은 쌍은 표준오차가 0 에 가까워 z 가 터짐
    gold_pos = T["duo_lo"].fillna(-1) > 0
    gold_neg = T["duo_hi"].fillna(1) < 0
    plays = enough & (T["joint_lift"] >= JOINT_PLAYS[0]) & (T["joint_z"] >= JOINT_PLAYS[1])
    T["class"] = np.select(
        [perceived & gold_pos, perceived & gold_neg, perceived & plays, perceived & enough, perceived & ~enough, gold_pos],
        ["검증된 콤보 (골드)", "인식 · 골드 손해", "인식 · 같이 놂", "인식만", "인식 (솔랭 근거 부족)", "숨은 콤보 (골드)"], "")
    T["fight_pp"] = T["delta_shrunk"] * 100
    keep = perceived | gold_pos
    # 의미 있는 시너지 = 서로 다른 근거 4가지 중 2개 이상 (프로 인식 · 커뮤니티 조합 언급 · 15분 듀오 골드 · 같이 놂)
    comm = T.get("comm_combo", pd.Series(0, index=T.index)).fillna(0) >= COMM_MIN
    sig = pd.DataFrame({"프로 인식": perceived, "커뮤니티 언급": comm, "듀오 골드": gold_pos, "같이 놂": plays})
    T["n_signals"] = sig.sum(axis=1)
    T["signals"] = sig.apply(lambda r: " · ".join(c for c, v in r.items() if v), axis=1)
    syn = T[(T["n_signals"] >= 2) & ~gold_neg].sort_values(["n_signals", "n_co"], ascending=False)
    H.save_csv(syn, OUT / "m2_combo_synergy.csv")
    if cfile.exists():
        both_ok = T.dropna(subset=["comm_lift"])
        both_ok = both_ok[(both_ok["exp_co"] >= 1) & (both_ok["comm_both"] >= 3)]
        rho_c = spearmanr(np.log(both_ok["lift"]), np.log(both_ok["comm_lift"])).correlation if len(both_ok) > 10 else np.nan
    else:
        rho_c = np.nan
    H.save_csv(T.sort_values(["class", "n_co"], ascending=[True, False]), OUT / "m2_combo_table.csv")
    H.save_csv(dm, OUT / "m2_combo_draft.csv")
    H.save_csv(we, OUT / "m2_combo_fight.csv")
    H.save_csv(jd, OUT / "m2_combo_joint.csv")

    def row(r):
        duo = f"{r['duo_gold']:+.0f}" if pd.notna(r.get("duo_gold")) else "—"
        fz = f"{r['fight_pp']:+.1f} (z {r['z']:+.1f}, {int(r['n_tg']):,}판)" if pd.notna(r.get("fight_pp")) else "—"
        jl = f"{r['joint_lift']:.2f}" if pd.notna(r.get("joint_lift")) else "—"
        return (f"| {r['a']} | {r['b']} | {int(r['n_co'])} | {r['lift']:.2f} | {r['same_turn_ratio']:.2f} | {r['ban_lift']:.2f} | "
                f"{jl} | {fz} | {duo} |" if pd.notna(r.get("same_turn_ratio")) else
                f"| {r['a']} | {r['b']} | {int(r['n_co'])} | {r['lift']:.2f} | — | — | {jl} | {fz} | {duo} |")

    head = ["| 챔피언 | 같이 | 대회 동시 픽 | 동시 픽 lift | 같은 차례 비율 | 밴 반응 lift | 공동 딜 lift (포지션 대비) | 교전 승률 %p (경기 안 비교) | 15분 듀오 골드 |",
            "|---|---|---|---|---|---|---|---|---|"]
    L = ["# M2 조합 찾기 — 인식(프로 밴픽) × 효과(솔랭 교전)", "",
         f"- [1] 대회 OE 5리그 팀-경기 {len(d):,} (08-25 까지), 같은 팀 챔피언@포지션 쌍 {len(dm):,}",
         f"- [2] 솔랭 교전 {len(F):,} (킬 2개 이상 · 킬 수 갈림) · 킬 {len(K):,}, 교전 × 쌍 행 {len(R):,}. "
         + ("교전 = 15초 + 딜 기준 낌 (1차, `--dmg`)" if "--dmg" in sys.argv else
            f"**위치 기반 (2차)**: 교전 = 15초 안 & 중심에서 {FIGHT_RADIUS} 안, 낌 = 킬러·어시스트·딜·희생자. 1차(딜 기준) 결과 `combos/_v1_dmg/`"),
         f"- 인식 = 대회 동시 픽 {PERCEIVED[0]}회 이상 & 축소 lift {PERCEIVED[1]} 이상. 효과 = 15분 듀오 골드(M2 3차, 평가 패치 검증) 구간 > 0. "
         f"같이 놂 = 공동 딜 lift {JOINT_PLAYS[0]} 이상 & z {JOINT_PLAYS[1]} 이상 (솔랭 팀-경기 {MIN_STRATA} 이상)",
         "- **경기 안 교전 비교는 경기 반분 재현이 안 됨(아래 [2]) → 분류에 쓰지 않고 참고 칸으로만 둠**", "",
         "## [1] 인식 지표 검증 (26.13 까지로 계산 → 26.14·26.15 대회)", "",
         f"- 평가 기간 기대 동시 픽 0.5 이상인 쌍 {len(v):,}개: 이전 lift ↔ 평가 기간 lift 순위 상관 **{rho:.3f}**",
         f"- 이전 기간 '인식' 쌍 {int(flag_pre.sum())}개의 평가 기간 lift {post_lift(flag_pre):.2f} vs 나머지 {post_lift(~flag_pre):.2f} "
         "(1 보다 크면 = 이전에 조합으로 보던 쌍을 다음 패치에도 묶어 뽑음)", "",
         "## [2] 효과 지표 신뢰도", "",
         f"- 경기 안 교전 비교 (포지션 대비 차이) 경기 반분 순위 상관: **{rel:.3f}** (양쪽 다 {MIN_STRATA}판 이상인 {len(hh)}쌍)",
         f"- 공동 딜 lift 경기 반분 순위 상관: **{jrel:.3f}** ({len(jj)}쌍)",
         f"- 공통 통제 계수 (교전 승리 확률): 둘 다 낌 {gam[0]:+.3f}, 우리 인원 {gam[1]:+.3f}, 상대 인원 {gam[2]:+.3f}, 골드 1,000 {gam[3]:+.3f}",
         "- 같은 포지션 쌍 평균 '둘 다 낌' 효과 (교전 승률 %p): " + ", ".join(
             f"{r} {v_ * 100:+.1f}" for r, v_ in we.groupby("role")["role_beta"].first().items()), "",
         "## [4] 분류", ""]
    for c, n in T.loc[keep, "class"].value_counts().items():
        L.append(f"- {c or '(효과만 음수)'}: {n}")
    L += [""]
    for c in ["검증된 콤보 (골드)", "인식 · 같이 놂", "인식만", "인식 · 골드 손해", "인식 (솔랭 근거 부족)", "숨은 콤보 (골드)"]:
        t = T[T["class"] == c]
        if not len(t):
            continue
        by = "duo_gold" if c.startswith("숨은") else "n_co"
        L += [f"### {c} ({len(t)}쌍, {('15분 듀오 골드' if by == 'duo_gold' else '대회 동시 픽')} 많은 순 상위 20)", ""] + head
        L += [row(r) for r in t.sort_values(by, ascending=False).head(20).to_dict("records")] + [""]
    L += ["### 예시 (요청한 조합)", ""] + head
    for x, y in EXAMPLES:
        m = T["key"].str.contains(f"{x}@", regex=False) & T["key"].str.contains(f"{y}@", regex=False)
        for r in T[m].sort_values("n_co", ascending=False).head(3).to_dict("records"):
            L.append(row(r))
    L += ["", "- 동시 픽 lift = 우연보다 몇 배 같이 뽑나. 같은 차례 비율 = 두 장 동시 픽에 같이 뽑힌 비율 ÷ 같은 포지션 쌍 평균. "
          "밴 반응 lift = 상대가 한쪽을 뽑은 뒤 2페이즈에 다른 쪽을 밴한 비율 ÷ 기대",
          "- 공동 딜 lift = 둘이 같은 희생자에 같이 딜을 넣은 킬 ÷ 기대, 같은 포지션 쌍 평균으로 나눔 (1 보다 크면 같이 노는 편)",
          "- 교전 승률 %p = 같은 경기 안에서 둘 다 낀 교전이 하나만 낀 교전보다 더 이기는 몫 − 같은 포지션 쌍 평균, 축소 값. z = 축소 전 값 ÷ 표준오차",
          "- 솔랭은 호흡을 맞추지 않는 판이라 대회 콤보 효과보다 작게 나올 수 있음. 낌 = 딜 또는 죽음 (도움만 준 경우는 못 셈)", ""]
    L += ["## [4-c] 의미 있는 시너지 (근거 2개 이상)", "",
          f"- 근거 4가지: 프로 인식 (동시 픽 {PERCEIVED[0]}회·lift {PERCEIVED[1]} 이상) · 커뮤니티 조합 언급 (조합 단어와 한 문장 {COMM_MIN}회 이상) · "
          "15분 듀오 골드 (구간 > 0) · 같이 놂 (공동 딜). 골드 손해 쌍은 뺌",
          f"- 프로 동시 픽 lift ↔ 커뮤니티 동시 언급 lift 순위 상관 {rho_c:.3f} (둘 다 있는 쌍) — 두 인식 근거가 서로 맞는 정도",
          f"- {len(syn)}쌍 (근거 3개 이상 {int((syn['n_signals'] >= 3).sum())})", "",
          "| 챔피언 | 같이 | 근거 | 대회 동시 픽 (lift) | 같은 차례 · 밴 반응 | 커뮤니티 조합 언급 | 15분 듀오 골드 [95%] | 공동 딜 lift |",
          "|---|---|---|---|---|---|---|---|"]
    for r in syn.to_dict("records"):
        f2 = lambda v: "—" if pd.isna(v) else f"{v:.2f}"  # noqa: E731
        duo = "—" if pd.isna(r.get("duo_gold")) else f"{r['duo_gold']:+.0f} [{r['duo_lo']:+.0f}, {r['duo_hi']:+.0f}]"
        cc = "—" if pd.isna(r.get("comm_combo")) else f"{int(r['comm_combo'])}회 (같이 언급 {int(r['comm_both'])})"
        L.append(f"| {r['a']} | {r['b']} | {r['signals']} | {int(r['n_co'])} ({r['lift']:.1f}배) | "
                 f"{f2(r.get('same_turn_ratio'))} · {f2(r.get('ban_lift'))} | {cc} | {duo} | {f2(r.get('joint_lift'))} |")
    L += [""]
    L += role_sections(T)
    (OUT / "m2_combo_eval.md").write_text("\n".join(L), encoding="utf-8")
    log.info("조합 끝 (%.0f초)", time.time() - t0)


ROLE_KO = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서폿"}


def role_sections(T: pd.DataFrame, n: int = 8) -> list[str]:
    """포지션 쌍 10가지마다: 프로가 묶는 조합(인식) 상위 + 공동 딜(같이 노는 정도) 상위 + 교전 효과 상위·하위."""
    head = ["| 챔피언 | 같이 | 대회 동시 픽 | 동시 픽 lift | 같은 차례 | 밴 반응 | 공동 딜 lift | 교전 승률 %p (z, 판수) | 15분 듀오 골드 | 분류 |",
            "|---|---|---|---|---|---|---|---|---|---|"]

    def f(v, d=2):
        return "—" if pd.isna(v) else f"{v:.{d}f}"

    def fmt(r):
        fz = "—" if pd.isna(r.get("fight_pp")) else f"{r['fight_pp']:+.1f} (z {r['z']:+.1f}, {int(r['n_tg']):,})"
        duo = "—" if pd.isna(r.get("duo_gold")) else f"{r['duo_gold']:+.0f}"
        cls = r.get("class")
        return (f"| {r['a']} | {r['b']} | {int(r['n_co'])} | {f(r['lift'])} | {f(r.get('same_turn_ratio'))} | {f(r.get('ban_lift'))} | "
                f"{f(r.get('joint_lift'))} | {fz} | {duo} | {cls if isinstance(cls, str) else ''} |")

    L = ["## [4-b] 포지션 쌍별 조합", "",
         "- 포지션 쌍마다 ① 프로가 묶어 뽑는 조합 (대회 동시 픽 5회 이상, lift 높은 순) ② 솔랭에서 같이 노는 조합 (공동 딜 lift, 팀-경기 30 이상) "
         "③ 15분 듀오 골드 상위·하위 (정글+미드·정글+탑·원딜+서폿만 있음). 교전 칸은 재현이 안 돼 참고만", ""]
    for i in range(5):
        for j in range(i + 1, 5):
            role = "+".join(sorted((POS[i], POS[j])))
            t = T[T["role"] == role]
            if not len(t):
                continue
            L += [f"### {ROLE_KO[POS[i]]} + {ROLE_KO[POS[j]]}", ""]
            per = t[t["n_co"] >= 5].sort_values("lift", ascending=False).head(n)
            if len(per):
                L += ["**① 프로가 묶어 뽑는 조합**", ""] + head + [fmt(r) for r in per.to_dict("records")] + [""]
            ok = t[t["n_tg"].fillna(0) >= MIN_STRATA]
            jd = ok.dropna(subset=["joint_lift"])
            if len(jd):
                L += ["**② 솔랭에서 같이 노는 조합 (공동 딜 lift 상위)**", ""] + head
                L += [fmt(r) for r in jd.sort_values("joint_lift", ascending=False).head(n // 2 + 1).to_dict("records")] + [""]
            dg = t.dropna(subset=["duo_gold"])
            if len(dg):
                L += ["**③ 15분 듀오 골드 상위 / 하위 (M2 3차 골드 모델)**", ""] + head
                L += [fmt(r) for r in dg.sort_values("duo_gold", ascending=False).head(4).to_dict("records")]
                L += [fmt(r) for r in dg.sort_values("duo_gold").head(3).to_dict("records")] + [""]
    return L


def report_only():
    """저장된 표로 보고서의 포지션 쌍 절만 다시 만듦."""
    T = pd.read_csv(OUT / "m2_combo_table.csv", low_memory=False)
    md = (OUT / "m2_combo_eval.md").read_text(encoding="utf-8")
    md = md.split("## [4-b]")[0].rstrip() + "\n\n" + "\n".join(role_sections(T))
    (OUT / "m2_combo_eval.md").write_text(md, encoding="utf-8")


if __name__ == "__main__":
    if "--report-only" in sys.argv:
        report_only()
    else:
        main()
