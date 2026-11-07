"""M4 1차 특징 보강 — A2·A3 보강 + A5·A6·C1 + 페이즈별 계수 (2026-11-06, docs/PIPELINE.md "M4 준비")

0차(m4_features.py) 특징 위에 더함. 모두 그 경기 '전날까지' 자료 (다른 리그 08-25 까지).
  반응형 (2밴 페이즈·밴 1순위 겨냥) — 5리그 대회 드래프트 누적 집계
    A5 응수 lift      : 상대가 같은 포지션에 c 를 먼저 뽑은 뒤 d 를 응수로 뽑은 정도 (픽 = 우리 후보가 상대 픽의 응수인가,
                        밴 = 후보가 '우리 픽'에 대한 상대의 응수인가 → 미리 막는 밴)
    조합 lift         : 같은 팀 동시 픽 lift (픽 = 우리 이미 뽑은 챔피언과, 밴 = 상대가 이미 뽑은 챔피언과 = 조합 완성 막기)
    밴 반응 lift      : 상대가 1페이즈에 c 를 뽑았을 때 2페이즈에 b 를 밴한 정도
    A6 대체 lift      : 이번 경기에 이미 밴된 챔피언이 있을 때 이 챔피언이 뽑히는 정도
    C1 타깃           : 상대 팀 아직 안 뽑은 포지션 선수의 풀 비율 (대회 ×2 + 솔랭 30일)
    밴 대상 포지션    : 상대가 아직 안 채운 포지션에서 그 챔피언이 쓰이는 비율
  A2·A3 보강
    남은 풀 순위      : 선수 풀(대회 ×2 + 솔랭 30일)에서 이번 매치에 이미 쓴·이번 경기에 픽·밴된 것을 뺀 순위 (1/순위, 상위 10만)
    연습 급증         : 최근 14일 솔랭 ÷ 직전 60일 평소 속도
  메타 순위           : 5리그 최근 21일 등장 순위 (−log 순위)
모델: 페이즈별(1밴·2밴·1픽·2픽) 조건부 로짓, 플레이오프형 경기(LCK Cup 플레이인·플레이오프·결승, RTM 은 평가 세트라 학습엔 없음) 가중 2
비교: 0차 재현 · 1차 전체 · 하나씩 빼 보기 (페이즈 분리 / 반응형 / A2·A3 보강 / 플레이오프 가중 / 메타 순위)

    python src/pipeline/m4_v1.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_features as F  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = B.OUT
POS = M1.POS
A_SHRINK = 5.0
NEW_PICK = ["syn_own", "counter_resp", "sub_lift", "pool_rank", "solo_surge", "meta_rank"]
NEW_BAN = ["block_combo", "block_counter", "ban_resp", "target", "opp_open_role", "sub_lift", "pool_rank", "solo_surge", "meta_rank"]
REACTIVE = {"syn_own", "counter_resp", "sub_lift", "block_combo", "block_counter", "ban_resp", "target", "opp_open_role"}
A23 = {"pool_rank", "solo_surge"}
ALL = sorted(set(F.ALL_FEATS) | set(NEW_PICK) | set(NEW_BAN) | {"win_value"})  # win_value = M4 1차 (m4_model.py)
PHASE = lambda step: "ban1" if step <= 6 else ("pick1" if step <= 12 else ("ban2" if step <= 16 else "pick2"))  # noqa: E731


# ---------------------------------------------------------------- 5리그 대회 드래프트 (순서·포지션)
def pro_games(cid):
    cols = ["gameid", "league", "date", "side", "position", "champion", "firstPick"] + \
           [f"pick{i}" for i in range(1, 6)] + [f"ban{i}" for i in range(1, 6)]
    oe = pd.read_csv(M1.OE_FILE, usecols=cols, low_memory=False)
    oe = oe[oe["league"].isin(M1.LEAGUES) & (oe["date"].str[:10] <= B.OTHER_END)]
    pl = oe[oe["position"].isin(H.OE_POS)].copy()
    pl["cid"] = pl["champion"].map(cid)
    role_of = {(r.gameid, r.side, r.cid): H.OE_POS[r.position] for r in pl.itertuples()}
    tm = oe[(oe["position"] == "team") & oe["firstPick"].notna()]
    games = {}
    for r in tm.itertuples():
        idx = F.FP_IDX if int(r.firstPick) == 1 else F.LP_IDX
        picks = []
        for i in range(5):
            c = cid(getattr(r, f"pick{i + 1}"))
            if c:
                picks.append((idx[i], c, role_of.get((r.gameid, r.side, c))))
        bans = [(1 if i < 3 else 2, cid(getattr(r, f"ban{i + 1}"))) for i in range(5)]
        g = games.setdefault(r.gameid, {"day": r.date[:10], "sides": {}})
        g["sides"][r.side] = {"picks": picks, "bans": [(ph, b) for ph, b in bans if b]}
    out = [g for g in games.values() if len(g["sides"]) == 2]
    return sorted(out, key=lambda g: g["day"])


class Cum:
    """대회 드래프트 누적 집계 (날짜 순으로 경기를 더해 감 → 항상 '그 날 전까지')."""

    def __init__(self):
        self.n_tg = self.n_games = 0
        self.pick, self.gpick, self.co = Counter(), Counter(), Counter()
        self.role_pick, self.role_tot = Counter(), Counter()
        self.resp, self.resp_base = Counter(), Counter()
        self.b2, self.banresp, self.banresp_base = Counter(), Counter(), Counter()
        self.sub, self.bang = Counter(), Counter()

    def add(self, g):
        self.n_games += 1
        s = g["sides"]
        allp, allb = set(), set()
        for side, t in s.items():
            o = s["Red" if side == "Blue" else "Blue"]
            P = [c for _, c, _ in t["picks"]]
            self.n_tg += 1
            for c in P:
                self.pick[c] += 1
                allp.add(c)
            for x, y in combinations(sorted(set(P)), 2):
                self.co[(x, y)] += 1
            for _, c, r in t["picks"]:
                if r:
                    self.role_pick[(c, r)] += 1
                    self.role_tot[r] += 1
            for ph, b in t["bans"]:
                allb.add(b)
                if ph == 2:
                    self.b2[b] += 1
            o_first3 = [c for _, c, _ in sorted(o["picks"])[:3]]
            my_b2 = [b for ph, b in t["bans"] if ph == 2]
            for c in o_first3:
                self.banresp_base[c] += 1
                for b in my_b2:
                    self.banresp[(c, b)] += 1
        # 같은 포지션 응수: 먼저 뽑힌 쪽 → 나중 쪽
        roles = defaultdict(dict)
        for side, t in s.items():
            for gi, c, r in t["picks"]:
                if r:
                    roles[r][side] = (gi, c)
        for r, d in roles.items():
            if len(d) == 2:
                (g1, c1), (g2, c2) = sorted(d.values())
                self.resp[(c1, c2)] += 1
                self.resp_base[c1] += 1
        for b in allb:
            self.bang[b] += 1
            for c in allp:
                self.sub[(b, c)] += 1
        for c in allp:
            self.gpick[c] += 1

    def co_lift(self, x, y):
        if self.n_tg == 0:
            return 0.0
        e = self.pick[x] * self.pick[y] / self.n_tg
        return float(np.log((self.co[tuple(sorted((x, y)))] + 1) / (e + 1)))

    def resp_lift(self, c, d, r):
        p = (self.role_pick[(d, r)] + 0.5) / (self.role_tot[r] + 50)
        return float(np.log((self.resp[(c, d)] + A_SHRINK * p) / (self.resp_base[c] + A_SHRINK) / p))

    def banresp_lift(self, c, b):
        q = (self.b2[b] + 0.5) / (self.n_tg + 50)
        return float(np.log((self.banresp[(c, b)] + A_SHRINK * q) / (self.banresp_base[c] + A_SHRINK) / q))

    def sub_lift(self, b, c):
        base = (self.gpick[c] + 0.5) / (self.n_games + 50)
        return float(np.log((self.sub[(b, c)] + A_SHRINK * base) / (self.bang[b] + A_SHRINK) / base))


# ---------------------------------------------------------------- 날짜별 선수 풀·연습 (0차 DayStats 에 더함)
def day_extras(D, pro_pick, sq):
    pprev = pro_pick[pro_pick["day"] < D]
    s30 = sq[(sq["day"] < D) & (sq["day"] >= (pd.Timestamp(D) - pd.Timedelta(days=30)).strftime("%Y-%m-%d"))]
    s74 = sq[(sq["day"] < (pd.Timestamp(D) - pd.Timedelta(days=14)).strftime("%Y-%m-%d"))
             & (sq["day"] >= (pd.Timestamp(D) - pd.Timedelta(days=74)).strftime("%Y-%m-%d"))]
    pool = Counter()
    for k, n in pprev.groupby(["playername", "cid"]).size().items():
        pool[k] += 2 * n
    for k, n in s30.groupby(["player", "cid"]).size().items():
        pool[k] += n
    lists = defaultdict(list)
    for (p, c), n in pool.items():
        lists[p].append((n, c))
    pool_sorted = {p: [c for _, c in sorted(v, reverse=True)] for p, v in lists.items()}
    pool_tot = {p: sum(n for n, _ in v) for p, v in lists.items()}
    pool_share = {k: n / pool_tot[k[0]] for k, n in pool.items()}
    base60 = s74.groupby(["player", "cid"]).size().to_dict()  # 14일 전 ~ 74일 전 = 60일
    return pool_sorted, pool_share, base60


# ---------------------------------------------------------------- 단계 × 후보 행
def build_rows(a, games, ds_cache, ex_cache, lineup, champs, pgames, weight_of, wv=None):
    """wv: (챔피언, 포지션) → 이길 가치 (로그 오즈). 없으면 0. 행 = (단계 id, 경기, 밴/픽, 정답, 특징, 단계, 가중, 팀)."""
    wv = wv or {}
    cum = Cum()
    pi = 0
    rows, sid = [], 0
    for gid in games:
        ga = a[a["GameId"] == gid].sort_values("step")
        if ga["cid"].isna().any() or len(ga) != 20:
            continue
        D, mid, n = ga["day"].iloc[0], ga["MatchId"].iloc[0], int(ga["N_GameInMatch"].iloc[0])
        while pi < len(pgames) and pgames[pi]["day"] < D:
            cum.add(pgames[pi])
            pi += 1
        fearless = set(a[(a["MatchId"] == mid) & (a["N_GameInMatch"] < n) & (a["action"] == "pick")]["cid"])
        if "fearless" in ga.columns and not bool(ga["fearless"].iloc[0]):
            fearless = set()  # 피어리스가 아닌 리그 (P2, m4_v6.py)
        ds = ds_cache[D]
        pool_sorted, pool_share, base60 = ex_cache[D]
        meta_rank = {c: i + 1 for i, c in enumerate(sorted(champs, key=lambda c: -ds.meta[c]))}
        game = ga["RiotPlatformGameId"].iloc[0]
        w = weight_of.get(gid, 1.0)
        hab = {}
        used, banned = set(), []
        picks = defaultdict(list)          # 팀 → [(cid, role)]
        pick_no = 0
        for r in ga.itertuples():
            avail = [c for c in champs if c not in used and c not in fearless]
            T, O = r.team, r.opponent
            my_open = [p for p in POS if p not in {ro for _, ro in picks[T]}]
            op_open = [p for p in POS if p not in {ro for _, ro in picks[O]}]
            if r.action == "pick":
                pick_no += 1
                bucket = F.PICK_BUCKET[pick_no]
                players = [F._player(lineup, game, T, ro) for ro in my_open]
            else:
                bucket = "b1" if r.step <= 6 else "b2"
                players = [F._player(lineup, game, O, ro) for ro in POS]
            players = [p for p in players if p]
            opp_open_players = [p for p in (F._player(lineup, game, O, ro) for ro in op_open) if p]
            # 남은 풀 순위: 이번 매치에 쓴 것·이번 경기 픽·밴 제외
            gone = used | fearless
            rem_rank = {}
            for p in (players if r.action == "pick" else opp_open_players):
                k = 0
                for c in pool_sorted.get(p, []):
                    if c in gone:
                        continue
                    k += 1
                    if k > 10:
                        break
                    rem_rank[(p, c)] = k
            key = (T, r.action)
            if key not in hab:
                hab[key] = F.team_hab(a, D, T, r.action)
            gfac = n - 1
            Xs, ys = [], []
            for c in avail:
                f = {"meta": ds.meta[c], "meta_lck": ds.meta_lck[c], "order_fit": ds.order_fit(c, bucket),
                     "flex": ds.flex.get(c, 0.0), "solo_meta": ds.solo_meta[c], "team_hab": hab[key](c),
                     "ban_ph1": ds.ban_ph1.get(c, 0.5)}
                ts = ds.tm_score.get(c, np.nan)
                f["tm_missing"] = float(np.isnan(ts)) if isinstance(ts, float) else 0.0
                f["tm_score"] = 0.0 if f["tm_missing"] else float(ts)
                tl = ds.tm_lck.get(c, np.nan)
                f["tm_lck"] = 0.0 if (isinstance(tl, float) and np.isnan(tl)) else float(tl)
                f["role_open"] = (np.log(sum((ds.role_share[ro].get(c, 0) + 0.1) / ds.role_n[ro] for ro in my_open) + F.EPS)
                                  if (r.action == "pick" and my_open) else 0.0)
                pp = [np.log1p(ds.pl_pro.get((p, c), 0)) for p in players] or [0.0]
                ps = [np.log1p(ds.pl_solo14.get((p, c), 0)) for p in players] or [0.0]
                rc = [ds.recency(p, c) for p in players] or [0.0]
                t3 = [float(c in ds.top3.get(p, set())) for p in players] or [0.0]
                f.update(pl_pro=max(pp), pl_solo=max(ps), pl_recent=max(rc), pl_top3=max(t3))
                f["top3_x_game"] = f["pl_top3"] * gfac
                f["meta_x_game"] = f["meta"] * gfac
                # --- 1차 추가
                f["meta_rank"] = -np.log(meta_rank[c])
                roles_wv = my_open if r.action == "pick" else op_open
                f["win_value"] = max([wv.get((c, ro), 0.0) for ro in roles_wv] or [0.0])
                f["sub_lift"] = float(np.mean([cum.sub_lift(b, c) for b in banned])) if banned else 0.0
                pl_for = players if r.action == "pick" else opp_open_players
                f["pool_rank"] = max([1.0 / rem_rank[(p, c)] for p in pl_for if (p, c) in rem_rank] or [0.0])
                f["solo_surge"] = max([np.log((ds.pl_solo14.get((p, c), 0) + 1) / (base60.get((p, c), 0) * 14 / 60 + 1))
                                       for p in pl_for] or [0.0])
                if r.action == "pick":
                    f["syn_own"] = float(np.mean([cum.co_lift(c, q) for q, _ in picks[T]])) if picks[T] else 0.0
                    cr = [cum.resp_lift(q, c, ro) for q, ro in picks[O] if ro in my_open]
                    f["counter_resp"] = max(cr) if cr else 0.0
                else:
                    f["block_combo"] = max([cum.co_lift(c, q) for q, _ in picks[O]] or [0.0])
                    bc = [cum.resp_lift(q, c, ro) for q, ro in picks[T] if ro in op_open]
                    f["block_counter"] = max(bc) if bc else 0.0
                    f["ban_resp"] = float(np.mean([cum.banresp_lift(q, c) for q, _ in picks[O]])) if picks[O] else 0.0
                    f["target"] = max([np.log(pool_share.get((p, c), 0) + 0.01) for p in opp_open_players] or [np.log(0.01)])
                    f["opp_open_role"] = (np.log(sum((ds.role_share[ro].get(c, 0) + 0.1) / ds.role_n[ro] for ro in op_open) + F.EPS)
                                          if op_open else np.log(F.EPS))
                Xs.append([f.get(k, 0.0) for k in ALL])
                ys.append(int(c == r.cid))
            ctx = {"cands": avail, "own": list(picks[T]), "opp": list(picks[O]), "bans": list(banned), "D": D,
                   "fp": getattr(r, "pick_order", "") == "fp", "bucket": bucket, "my_open": my_open, "op_open": op_open, "opp_team": O}
            rows.append((sid, gid, r.action, np.array(ys, np.int8), np.array(Xs, np.float32), int(r.step), w, T, ctx))
            used.add(r.cid)
            if r.action == "pick":
                picks[T].append((r.cid, r.role if isinstance(r.role, str) else None))
            else:
                banned.append(r.cid)
            sid += 1
    return rows


# ---------------------------------------------------------------- 적합·평가
def arrays(rows, feats):
    ix = [ALL.index(k) for k in feats]
    sid = np.concatenate([np.full(len(r[3]), r[0]) for r in rows])
    y = np.concatenate([r[3] for r in rows]).astype(float)
    X = np.vstack([r[4][:, ix] for r in rows]).astype(float)
    w = np.concatenate([np.full(len(r[3]), r[6]) for r in rows])
    return sid, y, X, w


def fit(sid, y, X, w, lam=1.0):
    _, grp = np.unique(sid, return_inverse=True)
    G = grp.max() + 1
    wg = np.bincount(grp, w * y, G)  # 단계 가중 (정답 행의 가중)

    def f(b):
        z = X @ b
        zm = np.zeros(G)
        np.maximum.at(zm, grp, z)
        e = np.exp(z - zm[grp])
        s = np.bincount(grp, e, G)
        p = e / s[grp]
        ll = np.sum(w * y * (z - zm[grp] - np.log(s[grp])))
        g = X.T @ (w * y - wg[grp] * p)
        return -ll + 0.5 * lam * b @ b, -g + lam * b
    return minimize(f, np.zeros(X.shape[1]), jac=True, method="L-BFGS-B").x


def rank_of(sid, y, s):
    order = np.lexsort((-s, sid))
    out = []
    df = pd.DataFrame({"sid": sid, "y": y, "s": s})
    for k, g in df.groupby("sid"):
        if g["y"].sum() != 1:
            continue
        v = g.loc[g["y"] == 1, "s"].iloc[0]
        out.append((k, 1 + (g["s"] > v).sum() + 0.5 * ((g["s"] == v).sum() - 1), len(g)))
    return out


CONFIGS = {
    "0차 재현": dict(new=False, phase=False, weight=False, drop=set()),
    "1차 전체": dict(new=True, phase=True, weight=True, drop=set()),
    "− 페이즈 분리": dict(new=True, phase=False, weight=True, drop=set()),
    "− 반응형 (A5·A6·조합·타깃·밴 포지션)": dict(new=True, phase=True, weight=True, drop=REACTIVE),
    "− A2·A3 보강 (남은 풀·연습 급증)": dict(new=True, phase=True, weight=True, drop=A23),
    "− 메타 순위": dict(new=True, phase=True, weight=True, drop={"meta_rank"}),
    "− 플레이오프 가중": dict(new=True, phase=True, weight=False, drop=set()),
}


def feats_for(action, cfg):
    base = F.FEATS_PICK if action == "pick" else F.FEATS_BAN
    new = (NEW_PICK if action == "pick" else NEW_BAN) if cfg["new"] else []
    return [f for f in base + new if f not in cfg["drop"]]


def evaluate(rows, sets, gday, all_games):
    step_of = {r[0]: r[5] for r in rows}
    res, coefs = [], {}
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        train = {g for g in all_games if gday[g] < start}
        for cname, cfg in CONFIGS.items():
            for action in ("pick", "ban"):
                feats = feats_for(action, cfg)
                tr = [r for r in rows if r[1] in train and r[2] == action]
                te = [r for r in rows if r[1] in gids and r[2] == action]
                groups = ({"1": (1, 6), "2": (13, 16)} if action == "ban" else {"1": (7, 12), "2": (17, 20)}) if cfg["phase"] \
                    else {"all": (1, 20)}
                for gname, (lo, hi) in groups.items():
                    trg = [r for r in tr if lo <= r[5] <= hi]
                    teg = [r for r in te if lo <= r[5] <= hi]
                    sid, y, X, w = arrays(trg, feats)
                    if not cfg["weight"]:
                        w = np.ones_like(w)
                    mu, sd = X.mean(0), X.std(0) + 1e-9
                    b = fit(sid, y, (X - mu) / sd, w)
                    if cname == "1차 전체":
                        coefs[(name, action, gname)] = dict(zip(feats, b))
                    s2, y2, X2, _ = arrays(teg, feats)
                    for k, rk, nn in rank_of(s2, y2, ((X2 - mu) / sd) @ b):
                        res.append({"set": name, "config": cname, "action": action, "step": step_of[k], "rank": rk, "n": nn})
        log.info("  %s 끝", name)
    return pd.DataFrame(res), coefs


def report(R, coefs, base) -> str:
    def tops(g):
        return f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 3).mean():.1%} · {(g['rank'] <= 5).mean():.1%} · {(1 / g['rank']).mean():.3f}"
    L = ["# M4 1차 특징 보강 — 리허설 비교", "",
         "- 0차 + 반응형(A5 응수 · 조합 막기 · 밴 반응 · A6 대체 · C1 타깃 · 밴 대상 포지션) + A2·A3 보강(남은 풀 순위 · 연습 급증) + 메타 순위,"
         " 페이즈별(1밴·2밴·1픽·2픽) 계수, 플레이오프형 경기 가중 2", "- 값 = top1 · top3 · top5 · MRR (리허설 3세트 합: LCK 26.14 · 26.15 · RTM)", "",
         "## 설정별 (하나씩 빼 보기)", "", "| 설정 | 픽 | 밴 |", "|---|---|---|"]
    bp = base[(base["action"] == "pick") & (base["method"] == "B4")]
    bb = base[(base["action"] == "ban") & (base["method"] == "B1")]
    L.append(f"| 기준선 (픽 선수 풀 · 밴 메타) | {tops(bp)} | {tops(bb)} |")
    for c in CONFIGS:
        g = R[R["config"] == c]
        L.append(f"| {c} | {tops(g[g['action'] == 'pick'])} | {tops(g[g['action'] == 'ban'])} |")
    ph = lambda s: "1밴 (1~6)" if s <= 6 else ("1픽 (7~12)" if s <= 12 else ("2밴 (13~16)" if s <= 16 else "2픽 (17~20)"))  # noqa: E731
    R = R.assign(phase=R["step"].map(ph))
    base = base.assign(phase=base["step"].map(ph))
    L += ["", "## 페이즈별 top1 · top5", "", "| 페이즈 | 기준선 | 0차 | 1차 전체 |", "|---|---|---|---|"]
    for p_ in ["1밴 (1~6)", "1픽 (7~12)", "2밴 (13~16)", "2픽 (17~20)"]:
        act = "ban" if "밴" in p_ else "pick"
        b_ = base[(base["phase"] == p_) & (base["action"] == act) & (base["method"] == ("B1" if act == "ban" else "B4"))]
        r0 = R[(R["phase"] == p_) & (R["config"] == "0차 재현")]
        r1 = R[(R["phase"] == p_) & (R["config"] == "1차 전체")]
        f2 = lambda g: f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 5).mean():.1%}"  # noqa: E731
        L.append(f"| {p_} | {f2(b_)} | {f2(r0)} | **{f2(r1)}** |")
    L += ["", "## 세트별 1차 전체 (픽 · 밴 top5)", "", "| 세트 | 0차 픽 | 1차 픽 | 0차 밴 | 1차 밴 |", "|---|---|---|---|---|"]
    for s_, g in R.groupby("set"):
        f5 = lambda c, a_: f"{(g[(g['config'] == c) & (g['action'] == a_)]['rank'] <= 5).mean():.1%}"  # noqa: E731
        L.append(f"| {s_} | {f5('0차 재현', 'pick')} | {f5('1차 전체', 'pick')} | {f5('0차 재현', 'ban')} | {f5('1차 전체', 'ban')} |")
    C = pd.DataFrame(coefs)
    sel = [c for c in C.columns if c[0] == "26.15"]
    L += ["", "## 1차 전체 계수 (26.15 세트 학습, 페이즈별, 표준화 1 SD 당)", "",
          "| 특징 | " + " | ".join(f"{a_} {g_}" for _, a_, g_ in sel) + " |", "|---|" + "---|" * len(sel)]
    for f_ in C.index:
        L.append(f"| {f_} | " + " | ".join("—" if pd.isna(C.loc[f_, c]) else f"{C.loc[f_, c]:+.2f}" for c in sel) + " |")
    return "\n".join(L) + "\n"


def main():
    t0 = time.time()
    a, pro_pick, pro_ban, lineup, sq, champs = B.load()
    cid = B.cid_map()
    ev = F.pro_events(cid)
    pgames = pro_games(cid)
    tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
    sets = B.eval_sets(a)
    days = sorted(a["day"].unique())
    ds_cache = {D: F.DayStats(D, ev, pro_pick, sq, tmeta, champs) for D in days}
    ex_cache = {D: day_extras(D, pro_pick, sq) for D in days}
    log.info("날짜별 통계 (%.0f초), 대회 경기 %d", time.time() - t0, len(pgames))
    g = pd.read_csv(PROCESSED / "b_draft_games.csv")
    po = g[g["Tab"].astype(str).str.contains("Play|Final") | g["OverviewPage"].astype(str).str.contains("Road to MSI")]["GameId"]
    weight_of = {k: 2.0 for k in po}
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    all_games = sorted(gday.index, key=lambda x: gday[x])
    rows = build_rows(a, all_games, ds_cache, ex_cache, lineup, champs, pgames, weight_of)
    log.info("단계 × 후보 (%.0f초)", time.time() - t0)
    R, coefs = evaluate(rows, sets, gday, all_games)
    H.save_csv(R, OUT / "m4_v1_ranks.csv")
    base = pd.read_csv(OUT / "m4_baseline_ranks.csv")
    (OUT / "m4_v1_eval.md").write_text(report(R, coefs, base), encoding="utf-8")
    log.info("M4 1차 특징 끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
