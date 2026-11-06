"""M4 입력 보완 A1~A4 + 간이 조건부 로짓 (M4 0차, 2026-11-06 — docs/PIPELINE.md "M4 준비")

드래프트 단계마다 후보 챔피언(피어리스·이미 픽·밴 제외)에 '그 경기 전날까지' 자료로 만든 특징을 붙임.
  A1 픽 순서 성향: 이 단계(픽 1번째·2~3·4~5·6~7·8~9·10번째 / 밴 1·2페이즈)에 이 챔피언이 나오는 정도(순서 적합),
     플렉스(포지션 엔트로피). 5리그 대회 픽·밴 순서 (OE pick1~5 · firstPick · ban1~5)
  A2 선수 풀: 빈 포지션 우리 선수(밴은 상대 선수)의 대회 판수, 최근 14일 솔랭 판수, 마지막으로 한 지 며칠(최근성), 상위 3 챔피언 여부
  A3 피어리스: 세트 번호 × 선수 상위 3 (뒤 세트일수록 2·3순위가 나옴), 세트 번호 × 메타
  A4 메타: 최근 21일 5리그 픽+밴 비율, LCK 비율, 빈 포지션 픽 비율, 팀원 메타 점수(meta_score·meta_lck), 리서치 선수 솔랭 14일 픽 비율
     + 팀 습관(팀이 뽑은·밴한 비율)
모델: 단계별 조건부 로짓 (후보 중 하나를 고름 = 소프트맥스), 픽·밴 따로, L2. 리허설 세트마다 그 세트 첫날 이전 LCK 경기로 학습
평가: 기준선과 같은 상위 1·3·5 · MRR (리허설: LCK 26.14 · 26.15 · Road to MSI)
26.16 메타 티어표 (A4): 08-25 기준, 포지션별 S/A/B/C

    python src/pipeline/m4_features.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m4_baseline as B  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = B.OUT
POS = M1.POS
PICK_BUCKET = {1: "p1", 2: "p23", 3: "p23", 4: "p45", 5: "p45", 6: "p67", 7: "p67", 8: "p89", 9: "p89", 10: "p10"}
FP_IDX, LP_IDX = [1, 4, 5, 8, 9], [2, 3, 6, 7, 10]   # 선픽 팀 · 후픽 팀의 전체 픽 순서
FEATS_PICK = ["meta", "meta_lck", "role_open", "order_fit", "flex", "tm_score", "tm_lck", "tm_missing", "solo_meta",
              "pl_pro", "pl_solo", "pl_recent", "pl_top3", "top3_x_game", "meta_x_game", "team_hab"]
FEATS_BAN = ["meta", "meta_lck", "order_fit", "tm_score", "tm_lck", "tm_missing", "solo_meta",
             "pl_pro", "pl_solo", "pl_recent", "pl_top3", "top3_x_game", "meta_x_game", "team_hab", "ban_ph1"]
EPS = 1e-4
ALL_FEATS = sorted(set(FEATS_PICK) | set(FEATS_BAN))


# ---------------------------------------------------------------- 자료
def pro_events(cid):
    """5리그 대회 픽(전체 순서·포지션)·밴(페이즈) 사건. 08-25 까지 (D2)."""
    cols = ["gameid", "league", "date", "side", "position", "teamname", "champion", "firstPick"] + \
           [f"pick{i}" for i in range(1, 6)] + [f"ban{i}" for i in range(1, 6)]
    oe = pd.read_csv(M1.OE_FILE, usecols=cols, low_memory=False)
    oe = oe[oe["league"].isin(M1.LEAGUES) & (oe["date"].str[:10] <= B.OTHER_END)]
    oe["day"] = oe["date"].str[:10]
    pl = oe[oe["position"].isin(H.OE_POS)].copy()
    pl["cid"] = pl["champion"].map(cid)
    role_of = {(r.gameid, r.side, r.cid): H.OE_POS[r.position] for r in pl.itertuples()}
    tm = oe[(oe["position"] == "team") & oe["firstPick"].notna()]
    ev = []
    for r in tm.itertuples():
        idx = FP_IDX if int(r.firstPick) == 1 else LP_IDX
        for i in range(5):
            c = cid(getattr(r, f"pick{i + 1}"))
            if c:
                ev.append((r.day, r.league, r.teamname, c, "pick", PICK_BUCKET[idx[i]], role_of.get((r.gameid, r.side, c))))
            b = cid(getattr(r, f"ban{i + 1}"))
            if b:
                ev.append((r.day, r.league, r.teamname, b, "ban", "b1" if i < 3 else "b2", None))
    return pd.DataFrame(ev, columns=["day", "league", "team", "cid", "kind", "bucket", "role"])


class DayStats:
    """한 경기 날짜 D 의 '전날까지' 통계 (그 날 모든 경기에 같이 씀)."""

    def __init__(self, D, ev, pro_pick, sq, tmeta, champs):
        prev = ev[ev["day"] < D]
        lo = (pd.Timestamp(D) - pd.Timedelta(days=21)).strftime("%Y-%m-%d")
        rec = prev[prev["day"] >= lo]
        if (rec["kind"] == "pick").sum() < 300:
            rec = prev
        n_tg = max(1, (rec["kind"] == "pick").sum() / 5)
        pres = Counter(rec["cid"])
        self.meta = {c: np.log((pres.get(c, 0) + 0.5) / n_tg) for c in champs}
        lck = rec[rec["league"] == "LCK"]
        n_l = max(1, (lck["kind"] == "pick").sum() / 5)
        pl_ = Counter(lck["cid"])
        self.meta_lck = {c: np.log((pl_.get(c, 0) + 0.5) / n_l) for c in champs}
        rp = rec[rec["kind"] == "pick"]
        self.role_share = {r: Counter(rp.loc[rp["role"] == r, "cid"]) for r in POS}
        self.role_n = {r: max(1, (rp["role"] == r).sum()) for r in POS}
        # 순서 적합 (시즌 전체)
        pp = prev
        bc = pp.groupby(["cid", "bucket"]).size()
        tot_b = pp.groupby("bucket").size()
        self.bucket_prior = (tot_b / tot_b.sum()).to_dict()
        self.bc = defaultdict(dict)
        for (c, b), n in bc.items():
            self.bc[c][b] = n
        self.cn = pp.groupby("cid").size().to_dict()
        # 플렉스
        rr = pp[(pp["kind"] == "pick") & pp["role"].notna()].groupby(["cid", "role"]).size().unstack(fill_value=0)
        p = rr.div(rr.sum(axis=1), axis=0)
        self.flex = (-(p * np.log(p.where(p > 0, 1))).sum(axis=1)).to_dict()
        # 밴 1페이즈 비율
        bb = pp[pp["kind"] == "ban"].groupby("cid")["bucket"].apply(lambda s: (s == "b1").mean())
        self.ban_ph1 = bb.to_dict()
        # 팀원 메타 (valid_for == D)
        t = tmeta[tmeta["valid_for"] == D].set_index("champion_id")
        self.tm_score = t["meta_score"].to_dict()
        self.tm_lck = t["meta_lck"].to_dict()
        # 리서치 선수 솔랭 14일
        so = (pd.Timestamp(D) - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
        s14 = sq[(sq["day"] < D) & (sq["day"] >= so)]
        sc = Counter(s14["cid"])
        ns = max(1, len(s14))
        self.solo_meta = {c: np.log((sc.get(c, 0) + 0.5) / ns) for c in champs}
        # 선수 (대회 + 솔랭)
        pprev = pro_pick[pro_pick["day"] < D]
        self.pl_pro = pprev.groupby(["playername", "cid"]).size().to_dict()
        self.pl_last = {k: pd.Timestamp(v).toordinal() for k, v in pprev.groupby(["playername", "cid"])["day"].max().items()}
        sprev = sq[sq["day"] < D]
        self.pl_solo14 = s14.groupby(["player", "cid"]).size().to_dict()
        self.pl_slast = {k: pd.Timestamp(v).toordinal() for k, v in sprev.groupby(["player", "cid"])["day"].max().items()}
        s30 = sprev[sprev["day"] >= (pd.Timestamp(D) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")]
        pool = Counter()
        for (p_, c), n in self.pl_pro.items():
            pool[(p_, c)] += 2 * n
        for (p_, c), n in s30.groupby(["player", "cid"]).size().items():
            pool[(p_, c)] += n
        top = defaultdict(list)
        for (p_, c), n in pool.items():
            top[p_].append((n, c))
        self.top3 = {p_: {c for _, c in sorted(v, reverse=True)[:3]} for p_, v in top.items()}
        self.D = pd.Timestamp(D).toordinal()

    def order_fit(self, c, bucket):
        prior = self.bucket_prior.get(bucket, 1e-3)
        n = self.cn.get(c, 0)
        return np.log((self.bc[c].get(bucket, 0) + 5 * prior) / (n + 5)) - np.log(prior)

    def recency(self, player, c):
        ds = [d for d in (self.pl_last.get((player, c)), self.pl_slast.get((player, c))) if d]
        if not ds:
            return 0.0
        days = self.D - max(ds)
        return float(np.exp(-days / 14))


def team_hab(a, D, team, action):
    t = a[(a["day"] < D) & (a["team"] == team) & (a["action"] == action)]
    c = Counter(t["cid"])
    n = max(1, len(t))
    return lambda x: np.log((c.get(x, 0) + 0.5) / n)


# ---------------------------------------------------------------- 단계 × 후보 행
def build_rows(a, games, ds_cache, lineup, champs):
    """games 의 모든 단계 → (단계 id, 후보, 정답 여부, 특징) 행."""
    rows = []
    sid = 0
    for gid in games:
        ga = a[a["GameId"] == gid].sort_values("step")
        if ga["cid"].isna().any() or len(ga) != 20:
            continue
        D, mid, n = ga["day"].iloc[0], ga["MatchId"].iloc[0], int(ga["N_GameInMatch"].iloc[0])
        fearless = set(a[(a["MatchId"] == mid) & (a["N_GameInMatch"] < n) & (a["action"] == "pick")]["cid"])
        ds = ds_cache[D]
        game = ga["RiotPlatformGameId"].iloc[0]
        hab = {}
        used, filled = set(), defaultdict(set)
        pick_no = 0
        for r in ga.itertuples():
            avail = [c for c in champs if c not in used and c not in fearless]
            if r.action == "pick":
                pick_no += 1
                bucket = PICK_BUCKET[pick_no]
                open_roles = [p for p in POS if p not in filled[r.team]]
                players = [_player(lineup, game, r.team, ro) for ro in open_roles]
            else:
                bucket = "b1" if r.step <= 6 else "b2"
                open_roles = []
                players = [_player(lineup, game, r.opponent, ro) for ro in POS]
            players = [p for p in players if p]
            key = (r.team, r.action)
            if key not in hab:
                hab[key] = team_hab(a, D, r.team, r.action)
            gfac = (n - 1)
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
                f["role_open"] = np.log(sum((ds.role_share[ro].get(c, 0) + 0.1) / ds.role_n[ro] for ro in open_roles) + EPS) if open_roles else 0.0
                pp = [np.log1p(ds.pl_pro.get((p, c), 0)) for p in players] or [0.0]
                ps = [np.log1p(ds.pl_solo14.get((p, c), 0)) for p in players] or [0.0]
                rc = [ds.recency(p, c) for p in players] or [0.0]
                t3 = [float(c in ds.top3.get(p, set())) for p in players] or [0.0]
                f.update(pl_pro=max(pp), pl_solo=max(ps), pl_recent=max(rc), pl_top3=max(t3))
                f["top3_x_game"] = f["pl_top3"] * gfac
                f["meta_x_game"] = f["meta"] * gfac
                Xs.append([f[k] for k in ALL_FEATS])
                ys.append(int(c == r.cid))
            rows.append((sid, gid, r.action, np.array(ys, np.int8), np.array(Xs, np.float32), int(r.step), bucket))
            used.add(r.cid)
            if r.action == "pick" and isinstance(r.role, str):
                filled[r.team].add(r.role)
            sid += 1
    return rows


def _player(lineup, game, team, role):
    try:
        p = lineup.loc[(game, team, role)]
    except KeyError:
        return None
    return p.iloc[0] if isinstance(p, pd.Series) else p


def to_arrays(rows, action, feats):
    rs = [r for r in rows if r[2] == action]
    ix = [ALL_FEATS.index(k) for k in feats]
    sid = np.concatenate([np.full(len(r[3]), r[0]) for r in rs])
    y = np.concatenate([r[3] for r in rs]).astype(float)
    X = np.vstack([r[4][:, ix] for r in rs]).astype(float)
    return sid, y, X, None


# ---------------------------------------------------------------- 조건부 로짓
def fit_clogit(sid, y, X, lam=1.0):
    _, grp = np.unique(sid, return_inverse=True)
    G = grp.max() + 1

    def f(b):
        z = X @ b
        zmax = np.zeros(G)
        np.maximum.at(zmax, grp, z)
        e = np.exp(z - zmax[grp])
        s = np.bincount(grp, e, G)
        p = e / s[grp]
        ll = np.sum(y * (z - zmax[grp] - np.log(s[grp])))
        g = X.T @ (y - p)
        return -ll + 0.5 * lam * b @ b, -g + lam * b
    r = minimize(f, np.zeros(X.shape[1]), jac=True, method="L-BFGS-B")
    sizes = np.bincount(grp)
    info = {"success": bool(r.success), "iters": int(r.nit), "nll_per_step": float(r.fun) / G,
            "null_nll_per_step": float(np.log(sizes).mean()), "train_steps": int(G)}  # 무작위(균등) 모델의 로그 손실 = log(후보 수)
    return r.x, info


def ranks(sid, y, score):
    out = []
    df = pd.DataFrame({"sid": sid, "y": y, "s": score})
    for _, g in df.groupby("sid"):  # _ = 단계 id
        if g["y"].sum() != 1:
            continue
        v = g.loc[g["y"] == 1, "s"].iloc[0]
        out.append((1 + (g["s"] > v).sum() + 0.5 * ((g["s"] == v).sum() - 1), len(g), _))
    return np.array(out)


# ---------------------------------------------------------------- 26.16 메타 티어 (A4)
def tier_table(ds: DayStats, champs, ko) -> pd.DataFrame:
    rows = []
    for ro in POS:
        for c in champs:
            share = ds.role_share[ro].get(c, 0) / ds.role_n[ro]
            if share <= 0 and np.isnan(ds.tm_score.get(c, np.nan)):
                continue
            rows.append({"position": ro, "champion_id": c, "name": ko.get(c, c), "pick_share_role": share,
                         "presence": ds.meta[c], "presence_lck": ds.meta_lck[c], "tm_score": ds.tm_score.get(c, np.nan),
                         "solo": ds.solo_meta[c], "order_early": ds.order_fit(c, "p1") if ds.cn.get(c, 0) else np.nan})
    t = pd.DataFrame(rows)
    z = lambda s: (s - s.mean()) / s.std()  # noqa: E731
    t["score"] = 0
    for col, w in (("presence", 1.0), ("presence_lck", 0.5), ("tm_score", 1.0), ("solo", 0.5)):
        t["score"] = t["score"] + w * z(t[col]).fillna(z(t[col]).min())
    t = t[t["pick_share_role"] > 0.02]  # 그 포지션에서 실제로 쓰인 챔피언만
    t["tier"] = t.groupby("position")["score"].transform(
        lambda s: pd.cut(s.rank(ascending=False, pct=True), [0, .1, .3, .6, 1.01], labels=["S", "A", "B", "C"]))
    return t.sort_values(["position", "score"], ascending=[True, False])


def main():
    t0 = time.time()
    a, pro_pick, pro_ban, lineup, sq, champs = B.load()
    cid = B.cid_map()
    ev = pro_events(cid)
    tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
    sets = B.eval_sets(a)
    days = sorted(a["day"].unique())
    log.info("자료 준비 (%.0f초): 대회 사건 %d, 날짜 %d", time.time() - t0, len(ev), len(days))
    ds_cache = {D: DayStats(D, ev, pro_pick, sq, tmeta, champs) for D in days}
    log.info("날짜별 통계 (%.0f초)", time.time() - t0)
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    all_games = sorted(gday.index, key=lambda g: gday[g])
    rows = build_rows(a, all_games, ds_cache, lineup, champs)
    log.info("단계 × 후보 행 %d (%.0f초)", len(rows), time.time() - t0)

    res, coefs, rank_rows = [], {}, []
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        train = [g for g in all_games if gday[g] < start]
        for action, feats in (("pick", FEATS_PICK), ("ban", FEATS_BAN)):
            trs = set(train)
            tr = [r for r in rows if r[1] in trs]
            te = [r for r in rows if r[1] in gids]
            sid, y, X, _ = to_arrays(tr, action, feats)
            mu, sd = X.mean(0), X.std(0) + 1e-9
            b, info = fit_clogit(sid, y, (X - mu) / sd)
            coefs[(name, action)] = dict(zip(feats, b))
            sid2, y2, X2, _ = to_arrays(te, action, feats)
            rk = ranks(sid2, y2, ((X2 - mu) / sd) @ b)
            # 학습 점검: 학습 세트 성적, 평가 세트 로그 손실, 정답을 섞은 대조 (학습할 게 없으면 무작위 수준이어야 함)
            rk_tr = ranks(sid, y, ((X - mu) / sd) @ b)
            Zt = ((X2 - mu) / sd) @ b
            _, g2 = np.unique(sid2, return_inverse=True)
            zmax = np.zeros(g2.max() + 1)
            np.maximum.at(zmax, g2, Zt)
            lse = np.log(np.bincount(g2, np.exp(Zt - zmax[g2]))) + zmax
            te_nll = float(-(Zt[y2 == 1] - lse[g2[y2 == 1]]).mean())
            _, gtr = np.unique(sid, return_inverse=True)
            first = np.r_[0, np.flatnonzero(np.diff(gtr)) + 1]
            sizes = np.diff(np.r_[first, len(gtr)])

            def nll_of(bb):
                z = ((X2 - mu) / sd) @ bb
                zm = np.zeros(g2.max() + 1)
                np.maximum.at(zm, g2, z)
                ls = np.log(np.bincount(g2, np.exp(z - zm[g2]))) + zm
                return float(-(z[y2 == 1] - ls[g2[y2 == 1]]).mean())
            sh_nll = []
            for seed in range(5):
                rng = np.random.default_rng(seed)
                ysh = np.zeros_like(y)
                ysh[first + (rng.random(len(first)) * sizes).astype(int)] = 1
                bsh, _ = fit_clogit(sid, ysh, (X - mu) / sd)
                sh_nll.append(nll_of(bsh))
            rk_sh = np.array([[np.nan]])
            res.append({"set": name, "action": action, "n_train_games": len(train), "steps": len(rk),
                        "top1": (rk[:, 0] <= 1).mean(), "top3": (rk[:, 0] <= 3).mean(), "top5": (rk[:, 0] <= 5).mean(),
                        "MRR": (1 / rk[:, 0]).mean(), "train_top5": (rk_tr[:, 0] <= 5).mean(),
                        "shuffled_nll": float(np.mean(sh_nll)), "test_nll": te_nll, **info})
            meta_ = {r[0]: (r[5], r[6]) for r in te}
            for rank_, n_, sid_ in rk:
                rank_rows.append({"set": name, "action": action, "step": meta_[sid_][0], "bucket": meta_[sid_][1],
                                  "rank": rank_, "n_avail": n_})
        log.info("  %s 끝 (%.0f초)", name, time.time() - t0)
    R = pd.DataFrame(res)
    H.save_csv(R, OUT / "m4_v0_eval.csv")
    RK = pd.DataFrame(rank_rows)
    H.save_csv(RK, OUT / "m4_v0_ranks.csv")
    C = pd.DataFrame(coefs).T
    H.save_csv(C.reset_index(), OUT / "m4_v0_coefs.csv")
    base = pd.read_csv(OUT / "m4_baseline_ranks.csv")
    best = {"pick": "B4", "ban": "B1"}

    # 26.16 메타 티어 (08-25 기준 = 08-26 의 '전날까지')
    ko = pd.read_csv(PROCESSED / "d_champions.csv", encoding="utf-8-sig").set_index("champion_id")["name_ko"].to_dict()
    ds_v = DayStats("2026-08-26", ev, pro_pick, sq, tmeta, champs)
    tier = tier_table(ds_v, champs, ko)
    H.save_csv(tier, OUT / "m4_meta_tier_26.16.csv")

    L = ["# M4 0차 — A1~A4 특징 + 간이 조건부 로짓 (리허설)", "",
         "- 특징 (그 경기 전날까지 자료): A1 순서 적합·플렉스·밴 1페이즈 비율 / A2 선수 대회·최근 14일 솔랭·최근성·상위 3 / "
         "A3 세트 번호 × 상위 3·메타 / A4 5리그·LCK 등장 비율, 빈 포지션 픽 비율, 팀원 메타 점수, 솔랭 메타 + 팀 습관",
         "- 모델: 단계마다 후보 중 하나를 고르는 조건부 로짓 (픽·밴 따로, L2). 리허설 세트마다 그 세트 첫날 이전 LCK 경기로 학습",
         "- 기준선 = 픽 '선수 풀' (B4), 밴 '메타' (B1)", "",
         "| 세트 | 종류 | 학습 경기 | 단계 | top1 | top3 | top5 | MRR | 기준선 top1 · top3 · top5 · MRR |", "|---|---|---|---|---|---|---|---|---|"]
    for r in R.itertuples():
        bb = base[(base["set"] == r.set) & (base["action"] == r.action) & (base["method"] == best[r.action])]
        L.append(f"| {r.set} | {'픽' if r.action == 'pick' else '밴'} | {r.n_train_games} | {r.steps} | **{r.top1:.1%}** | **{r.top3:.1%}** | "
                 f"**{r.top5:.1%}** | **{r.MRR:.3f}** | {(bb['rank'] <= 1).mean():.1%} · {(bb['rank'] <= 3).mean():.1%} · "
                 f"{(bb['rank'] <= 5).mean():.1%} · {(1 / bb['rank']).mean():.3f} |")
    for action in ("pick", "ban"):
        rr = R[R["action"] == action]
        w = rr["steps"] / rr["steps"].sum()
        bb = base[(base["action"] == action) & (base["method"] == best[action])]
        L += ["", f"- **{'픽' if action == 'pick' else '밴'} 세 세트 합**: top1 {(rr['top1'] * w).sum():.1%} · top3 {(rr['top3'] * w).sum():.1%} · "
              f"top5 {(rr['top5'] * w).sum():.1%} · MRR {(rr['MRR'] * w).sum():.3f} (기준선 {(bb['rank'] <= 1).mean():.1%} · "
              f"{(bb['rank'] <= 3).mean():.1%} · {(bb['rank'] <= 5).mean():.1%} · {(1 / bb['rank']).mean():.3f})"]
    L += ["", "## 학습 점검", "",
          "- 로그 손실 = 정답 후보에 준 확률의 −log 평균 (단계당). 무작위 = log(후보 수) ≈ 4.8. 낮을수록 좋음",
          "- 정답 섞기 대조 = 학습 자료의 정답을 후보 중 아무거나로 바꿔 학습(시드 5개) → 평가 로그 손실. 진짜 신호를 배웠다면 이 값은 무작위(≈ 4.8) 근처여야 함",
          "", "| 세트 | 종류 | 수렴 | 반복 | 학습 로그 손실 (무작위) | 평가 로그 손실 | 학습 top5 | 평가 top5 | 정답 섞기 대조 평가 로그 손실 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in R.itertuples():
        L.append(f"| {r.set} | {'픽' if r.action == 'pick' else '밴'} | {'예' if r.success else '아니오'} | {r.iters} | "
                 f"{r.nll_per_step:.3f} ({r.null_nll_per_step:.3f}) | {r.test_nll:.3f} | {r.train_top5:.1%} | {r.top5:.1%} | {r.shuffled_nll:.3f} |")
    ph = {"b1": "1밴 페이즈 (1~6)", "p1": "1픽 페이즈 (7~12)", "p23": "1픽 페이즈 (7~12)", "p45": "1픽 페이즈 (7~12)",
          "p67": "1픽 페이즈 (7~12)", "b2": "2밴 페이즈 (13~16)", "p89": "2픽 페이즈 (17~20)", "p10": "2픽 페이즈 (17~20)"}
    RK["phase"] = np.where(RK["step"] <= 6, "1밴 페이즈 (1~6)", np.where(RK["step"] <= 12, "1픽 페이즈 (7~12)",
                           np.where(RK["step"] <= 16, "2밴 페이즈 (13~16)", "2픽 페이즈 (17~20)")))
    bl = base[base["method"].isin(["B1", "B4"])].copy()
    bl = bl[((bl["action"] == "pick") & (bl["method"] == "B4")) | ((bl["action"] == "ban") & (bl["method"] == "B1"))]
    bl["phase"] = np.where(bl["step"] <= 6, "1밴 페이즈 (1~6)", np.where(bl["step"] <= 12, "1픽 페이즈 (7~12)",
                           np.where(bl["step"] <= 16, "2밴 페이즈 (13~16)", "2픽 페이즈 (17~20)")))
    L += ["", "## 페이즈별 (리허설 3세트 합)", "", "| 페이즈 | 단계 수 | 후보 수 | top1 | top3 | top5 | MRR | 기준선 top1 · top5 |", "|---|---|---|---|---|---|---|---|"]
    for p_, g in RK.groupby("phase"):
        bb = bl[bl["phase"] == p_]
        L.append(f"| {p_} | {len(g)} | {g['n_avail'].mean():.0f} | {(g['rank'] <= 1).mean():.1%} | {(g['rank'] <= 3).mean():.1%} | "
                 f"{(g['rank'] <= 5).mean():.1%} | {(1 / g['rank']).mean():.3f} | {(bb['rank'] <= 1).mean():.1%} · {(bb['rank'] <= 5).mean():.1%} |")
    L += ["", "## 단계별 (1~20, 리허설 3세트 합)", "", "| 단계 | 종류 | 후보 수 | top1 | top3 | top5 | 기준선 top5 |", "|---|---|---|---|---|---|---|"]
    for st, g in RK.groupby("step"):
        bb = bl[bl["step"] == st]
        L.append(f"| {st} | {'밴' if g['action'].iloc[0] == 'ban' else '픽'} | {g['n_avail'].mean():.0f} | {(g['rank'] <= 1).mean():.1%} | "
                 f"{(g['rank'] <= 3).mean():.1%} | {(g['rank'] <= 5).mean():.1%} | {(bb['rank'] <= 5).mean():.1%} |")
    L += ["", "## 계수 (표준화 1 SD 당, 세트별 학습)", "", "| 특징 | " + " | ".join(f"{s} {a_}" for s, a_ in C.index) + " |",
          "|---|" + "---|" * len(C)]
    for f_ in sorted(set(FEATS_PICK) | set(FEATS_BAN)):
        L.append(f"| {f_} | " + " | ".join("—" if pd.isna(C.loc[i].get(f_)) else f"{C.loc[i][f_]:+.2f}" for i in C.index) + " |")
    L += ["", "## A4. 26.16 메타 티어 (08-25 기준, 포지션별 상위 10% S · 30% A · 60% B · 나머지 C)", "",
          "- 점수 = 5리그 최근 21일 등장(픽+밴) + 0.5 × LCK 등장 + 팀원 메타 점수 + 0.5 × 리서치 선수 솔랭 14일 (각각 표준화). 그 포지션 픽 비율 2% 이상만", ""]
    for ro in POS:
        tt = tier[tier["position"] == ro]
        for tr_ in ("S", "A"):
            names = ", ".join(tt.loc[tt["tier"] == tr_, "name"].astype(str).head(12))
            L.append(f"- **{B.ROLE and {'TOP': '탑', 'JUNGLE': '정글', 'MIDDLE': '미드', 'BOTTOM': '원딜', 'UTILITY': '서폿'}[ro]} {tr_}**: {names}")
    (OUT / "m4_v0_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("M4 0차 끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
