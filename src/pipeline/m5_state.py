"""M5 — 드래프트 상태를 직접 입력받아 M4 특징 행을 만드는 함수 (2026-11-08)

옛 M5 인수인계 구현 순서 2번 (지금은 HANDOFF.md): m4_validate.build_rows_for 는 '기록된 경기'에서 상태를 읽음 → 여기서는 상태(지금까지의 밴·픽)를 입력으로 받아
**지금 상태에서 남은 단계 전부**의 M4 행을 한 번에 만듦.

방법
  - m4_v1.build_rows 는 경기 기록 20줄을 순서대로 읽으며 각 단계 행을 그 앞 기록으로 만듦 → 상태 이후 단계를 '유령(ghost)' 기록으로 넣음:
    유령 기록은 행(점수)은 만들지만 상태(이미 나간 챔피언·픽)에는 넣지 않음 (m4_v1 에 2026-11-08 추가). 픽 차례(순서 적합 칸)는 단계 번호로 정함
  - 다시 보기(replay): 실제 경기 기록에서 t 단계부터를 유령으로 → 유령 기록의 챔피언 = 실제 선택이라 정답 표시도 그대로
  - 실시간(live): 끝난 단계 + 유령 대상 단계 (챔피언 칸은 아무 남은 챔피언, 정답 없음)
  - C2 · 시리즈·상대 전적 특징은 m4_v6.c2_feature · m4_v7.series_features 와 같은 식을 미리 만든 색인으로 계산 (부를 때마다 표 전체를 다시 읽지 않게).
    두 함수 모두 '그 날짜 전' · '같은 매치 앞 세트'만 보므로 실제 경기 기록이 표에 있어도 결과가 같음
  - P1 은 m4_v4.p1_features 그대로 (작은 표만 넘김)
점검: python src/pipeline/m5_state.py --parity  → 리허설 LCK 경기의 실제 단계 t 행을 '상태 t' 로 다시 만들어 학습 표(m4_v6_rows.pkl + 시리즈)와 비교
"""
from __future__ import annotations

import bisect
import copy
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_features as F  # noqa: E402
import m4_v1 as V  # noqa: E402
import m4_v4 as V4  # noqa: E402
import m4_v6 as V6  # noqa: E402
import m4_v7 as V7  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log


class StateBuilder:
    """a = LCK 드래프트 표 (B.load), 08-25 까지. 날짜별 통계·누적 집계는 처음 쓸 때 만들어 둠."""

    def __init__(self, a=None, pro_pick=None, lineup=None, sq=None, champs=None):
        if a is None:
            a, pro_pick, _, lineup, sq, champs = B.load()
            a = a.assign(league="LCK", fearless=True)
        if "is_winner" not in a.columns:
            a["is_winner"] = np.nan
        self.a, self.pro_pick, self.lineup, self.sq, self.champs = a, pro_pick, lineup, sq, champs
        cid = B.cid_map()
        self.ev = F.pro_events(cid)
        self.pgames = sorted(V.pro_games(cid), key=lambda g: g["day"])
        self.ev_all, self.gm = V4.patch_events(cid)
        self.tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
        s = pd.read_csv(PROCESSED / "c_patch_champion_summary.csv", dtype={"patch": str})
        self.buff = {(r.patch, r.champion_id): (1.0 if r.label == "buff" else -1.0 if r.label == "nerf" else 0.0) for r in s.itertuples()}
        self.wins = V7.winners(a)
        self.ds, self.ex, self.cum = {}, {}, {}
        self._cum_live, self._cum_i = V.Cum(), 0
        self._index()

    # ------------------------------------------------------------ 미리 만드는 색인 (C2 · 시리즈)
    def _index(self):
        a = self.a.sort_values(["GameId", "step"]).copy()
        a["pick_no"] = a[a["action"] == "pick"].groupby("GameId").cumcount() + 1
        a["bk"] = np.where(a["action"] == "pick", a["pick_no"].map(lambda x: F.PICK_BUCKET.get(int(x), "") if pd.notna(x) else ""),
                           np.where(a["step"] <= 6, "b1", "b2"))
        a["fp"] = a["pick_order"] == "fp"
        self.c2_idx = defaultdict(list)
        for r in a.dropna(subset=["cid"]).sort_values("day").itertuples():
            self.c2_idx[(r.team, r.fp, r.bk)].append((r.day, r.cid))
        for v in self.c2_idx.values():
            v.sort()
        g = self.a.drop_duplicates("GameId").set_index("GameId")[["MatchId", "N_GameInMatch", "day"]]
        self.g = g
        self.acts = defaultdict(list)
        for r in self.a.dropna(subset=["cid"]).itertuples():
            self.acts[r.MatchId].append((int(r.N_GameInMatch), r.team, r.action, r.cid))
        self.game_of = {(m, n): gid for gid, (m, n, _) in g.iterrows()}
        self.h2h = defaultdict(list)
        for r in self.a.dropna(subset=["cid"]).sort_values("day").itertuples():
            self.h2h[(r.team, r.opponent)].append((r.day, r.action, r.cid))
        for v in self.h2h.values():
            v.sort()

    def day(self, D):
        if D not in self.ds:
            self.ds[D] = F.DayStats(D, self.ev, self.pro_pick, self.sq, self.tmeta, self.champs)
            self.ex[D] = V.day_extras(D, self.pro_pick, self.sq)
            if self._cum_i and self.pgames[self._cum_i - 1]["day"] >= D:      # 날짜가 거꾸로 오면 처음부터
                self._cum_live, self._cum_i = V.Cum(), 0
            while self._cum_i < len(self.pgames) and self.pgames[self._cum_i]["day"] < D:
                self._cum_live.add(self.pgames[self._cum_i])
                self._cum_i += 1
            self.cum[D] = copy.deepcopy(self._cum_live)
        return self.ds[D]

    # ------------------------------------------------------------ 특징 (m4_v6.c2_feature · m4_v7.series_features 와 같은 식)
    def _c2(self, rows):
        for r in rows:
            ctx = r[8]
            lst = self.c2_idx.get((r[7], ctx["fp"], ctx["bucket"]), [])
            k = bisect.bisect_left(lst, (ctx["D"], ""))
            hc = Counter(c for _, c in lst[:k])
            r.append(np.array([[np.log((hc.get(c, 0) + 0.1) / (k + 1))] for c in ctx["cands"]], np.float32))

    def _series(self, rows, mid, n):
        for r in rows:
            ctx = r[8]
            T, O, D = r[7], ctx["opp_team"], ctx["D"]
            prev = [x for x in self.acts.get(mid, []) if x[0] < n]
            ban_T = Counter(c for k, t, act, c in prev if t == T and act == "ban")
            ban_last = {c for k, t, act, c in prev if t == T and act == "ban" and k == n - 1}
            ban_O = Counter(c for k, t, act, c in prev if t == O and act == "ban")
            last_gid = self.game_of.get((mid, n - 1))
            lost = float(n > 1 and last_gid is not None and self.wins.get(last_gid) not in (None, T))
            hv = self.h2h.get((T, O), [])
            hv = hv[:bisect.bisect_left(hv, (D, "", ""))]
            hb = Counter(c for _, act, c in hv if act == "ban")
            hp = Counter(c for _, act, c in hv if act == "pick")
            ho = self.h2h.get((O, T), [])
            ho = ho[:bisect.bisect_left(ho, (D, "", ""))]
            hpo = Counter(c for _, act, c in ho if act == "pick")
            nb = max(1, sum(hb.values()))
            X = np.zeros((len(ctx["cands"]), len(V7.SER)), np.float32)
            for i, c in enumerate(ctx["cands"]):
                sl = float(c in ban_last)
                X[i] = [ban_T.get(c, 0), sl, ban_O.get(c, 0), lost * sl, np.log((hb.get(c, 0) + 0.1) / (nb + 1)),
                        np.log1p(hpo.get(c, 0)), np.log1p(hp.get(c, 0)), n]
            r.append(X)

    # ------------------------------------------------------------ 행 만들기
    def build(self, recs: pd.DataFrame, extra_a: pd.DataFrame | None = None):
        """recs = 한 경기 20줄 (ghost 칸 True = 대상 단계). 반환 = 대상 단계 행 목록 (r[5] = 단계)."""
        gid = recs["GameId"].iloc[0]
        D = recs["day"].iloc[0]
        ds = self.day(D)
        base = self.a[self.a["GameId"] != gid]
        if extra_a is not None:
            base = pd.concat([base, extra_a], ignore_index=True)
        a2 = pd.concat([base, recs], ignore_index=True)
        cum = self.cum[D]
        old = V.Cum
        V.Cum = lambda: cum
        try:
            rows = V.build_rows(a2, [gid], {D: ds}, {D: self.ex[D]}, self.lineup, self.champs, [], {}, None)
        finally:
            V.Cum = old
        ghost_steps = set(recs.loc[recs["ghost"] == True, "step"].astype(int))  # noqa: E712
        rows = [list(r) for r in rows if r[5] in ghost_steps]
        self._c2(rows)
        gm = self.gm
        rpg = recs["RiotPlatformGameId"].iloc[0]
        if rpg not in set(gm["gameid"]):
            gm = pd.concat([gm, pd.DataFrame([{"gameid": rpg, "league": "LCK", "day": D, "patch": self.lck_patch(D)}])], ignore_index=True)
        rows = V4.p1_features(rows, recs, self.ev_all, gm, self.buff, five_only=True)
        for r in rows:
            r.append("LCK")
        mid, n = recs["MatchId"].iloc[0], int(recs["N_GameInMatch"].iloc[0])
        self._series(rows, mid, n)
        return rows

    def add_records(self, recs: pd.DataFrame):
        """같은 시리즈 앞 세트 기록(20줄, is_winner 포함)을 기록 표에 더함 → 피어리스 제외 · 시리즈 특징 · 승패가 학습 때와 같은 방식으로 계산됨."""
        self.a = pd.concat([self.a, recs.drop(columns=[c for c in ("ghost",) if c in recs.columns])], ignore_index=True)
        self.wins = V7.winners(self.a)
        self._index()

    def lck_patch(self, D):
        g = self.gm[(self.gm["league"] == "LCK") & (self.gm["day"] < D)].sort_values("day")
        return g["patch"].iloc[-1]

    def replay(self, gid, t):
        """실제 경기 gid 의 '단계 t 직전 상태' → 단계 t..20 행 (유령 = 실제 선택, 정답 표시 있음)."""
        recs = self.a[self.a["GameId"] == gid].sort_values("step").copy()
        recs["ghost"] = recs["step"] >= t
        return self.build(recs)


# ---------------------------------------------------------------- 점검
def parity(n_games=12):
    """리허설 LCK 경기에서 상태 t 의 단계 t 행 = 학습 표 단계 t 행 인가."""
    t0 = time.time()
    sb = StateBuilder()
    cache, a_full, *_ = pd.read_pickle(B.OUT / "m4_v6_rows.pkl")
    if "is_winner" not in a_full.columns:
        a_full["is_winner"] = np.nan
    sets = B.eval_sets(sb.a)
    games = sorted(set().union(*sets.values()))
    rng = np.random.default_rng(0)
    games = list(rng.choice(games, min(n_games, len(games)), replace=False))
    ref = [list(r) for r in cache if r[1] in set(games)]
    ref = V7.series_features(ref, a_full, V7.winners(a_full))
    ref = {(r[1], r[5]): r for r in ref}
    used = [V.ALL.index(f) for f in set(V7.PICK_FEATS) | set(V7.BAN_FEATS)]
    worst = {"공통": 0.0, "C2": 0.0, "P1": 0.0, "시리즈": 0.0}
    n_cmp, cand_diff, t_build = 0, 0, []
    for gid in games:
        for t in range(1, 21):
            t1 = time.time()
            rows = sb.replay(gid, t)
            t_build.append(time.time() - t1)
            r = next((x for x in rows if x[5] == t), None)
            q = ref.get((gid, t))
            if r is None or q is None:
                continue
            n_cmp += 1
            if r[8]["cands"] != q[8]["cands"]:
                cand_diff += 1
                continue
            worst["공통"] = max(worst["공통"], float(np.abs(r[4][:, used] - q[4][:, used]).max()))
            worst["C2"] = max(worst["C2"], float(np.abs(r[9] - q[9]).max()))
            worst["P1"] = max(worst["P1"], float(np.abs(r[10] - q[10]).max()))
            worst["시리즈"] = max(worst["시리즈"], float(np.abs(r[12] - q[12]).max()))
    log.info("점검: %d경기 · 비교 단계 %d · 후보 목록 다름 %d · 최대 차이 %s · 상태 한 번 %.2f초 (중앙값) · 전체 %.0f초",
             len(games), n_cmp, cand_diff, {k: round(v, 6) for k, v in worst.items()}, float(np.median(t_build)), time.time() - t0)
    return n_cmp, cand_diff, worst


if __name__ == "__main__":
    if "--parity" in sys.argv:
        parity()


# ---------------------------------------------------------------- 남은 드래프트 굴리기 (m5_sim · m5_replay 공용)
def role_arrays(ds, champs):
    """챔피언 × 포지션 비중(행 합 1), 가능(0.1 이상), 나온 적 있음."""
    sh = np.array([[ds.role_share[r].get(c, 0) for r in V.POS] for c in champs], float)
    tot = sh.sum(1, keepdims=True)
    share = np.divide(sh, tot, out=np.zeros_like(sh), where=tot > 0)
    return share, (share >= 0.1).astype(float), (share > 0).astype(float)


def rollout(FP, t, fp, lp, taken0, open0, share, role_ok, role_any, R, seed=0, forced=None, VAL=None, val0=None, role_mode="ratio"):
    """남은 단계 t..20 을 R 번 굴림. FP[s] = 단계 s 확률 벡터(챔피언 순서), taken0 = 이미 나간 챔피언(불리언),
    open0 = {팀: 빈 포지션 불리언 5}. 뽑을 때 이미 나간 챔피언 제외 + 픽은 '포지션 비율 보정':
      FP[s] 는 지금 상태(open0)의 빈 포지션을 이미 반영한 M4 확률 → 굴리는 중 그 팀 포지션이 더 채워진 만큼만 낮춤
      가중 = (지금 굴리기의 빈 포지션 비중 합 + 0.02) / (지금 상태 빈 포지션 비중 합 + 0.02). 다음 단계(s = t)는 M4 확률 그대로
      (2026-11-08 재생 검증: '비중 0.1 이상만' 거르기는 다음 단계 로그 손실을 2.540 → 2.591 로 나쁘게 함 — 실제 비주류·플렉스 픽을 지움)
    forced = 단계 t 에 강제로 할 챔피언 번호 (None = FP[t] 로 뽑음). 같은 seed = 같은 난수 (후보 간 공통 난수).
    VAL = 챔피언 × 포지션 값 행렬, 또는 {팀: 행렬} (팀·선수별 값 — 2026-11-08 숙련 편차 반영)
    반환: C (21 × 챔피언) 단계별 선택 횟수, val {팀: R 배열} (VAL 이 있으면 픽한 챔피언 값 합),
          E (21 × 챔피언) 단계별 '그 순간 뽑기 분포'의 굴리기 평균 = P(단계 s 에 c) 의 매끄러운 추정 (횟수보다 잡음이 적음, 기댓값은 같음)."""
    rng = np.random.default_rng(seed)
    U = rng.random((R, 21))
    n, ar = len(taken0), np.arange(R)
    avail = np.tile(~taken0, (R, 1))
    op = {k: np.tile(open0[k], (R, 1)) for k in (fp, lp)}
    val = {k: np.full(R, (val0 or {}).get(k, 0.0)) for k in (fp, lp)}
    C = np.zeros((21, n))
    E = np.zeros((21, n))
    base = {k: share @ open0[k].astype(float) + 0.02 for k in (fp, lp)}       # 챔피언별 지금 상태 빈 포지션 비중 합
    for s in range(t, 21):
        act, po, _ = V6.ORDER[s - 1]
        tm = fp if po == "fp" else lp
        if s == t and forced is not None:
            idx = np.full(R, forced)
            E[s, forced] = 1.0
        else:
            w = FP[s][None, :] * avail
            if act == "pick":
                if role_mode == "hard":       # 예전 방식 (비교용): 그 팀 빈 포지션 비중 0.1 이상만
                    w2 = w * ((op[tm].astype(float) @ role_ok.T) > 0)
                else:
                    w2 = w * ((op[tm].astype(float) @ share.T + 0.02) / base[tm][None, :])
                bad = w2.sum(1) == 0
                if bad.any():
                    w2[bad] = avail[bad] * ((op[tm][bad].astype(float) @ role_any.T) > 0) + 1e-12 * avail[bad]
                w = w2
            else:
                w = np.where(w.sum(1, keepdims=True) > 0, w, avail.astype(float))
            cs = np.cumsum(w, 1)
            E[s] = (w / cs[:, -1:]).mean(0)
            idx = np.minimum((cs < U[:, s:s + 1] * cs[:, -1:]).sum(1), n - 1)
        avail[ar, idx] = False
        np.add.at(C[s], idx, 1)
        if act == "pick":
            ro = np.argmax(np.where(op[tm], share[idx] + 1e-9, -1), 1)
            op[tm][ar, ro] = False
            if VAL is not None:
                val[tm] += (VAL[tm] if isinstance(VAL, dict) else VAL)[idx, ro]
    return C, val, E
