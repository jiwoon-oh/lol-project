"""M5 시험판 — 고정 M4 + 이길 가치로 가상 드래프트를 한 단계씩 진행하며 T1 밴·픽 추천 (2026-11-07, 시험용)

목적: "지금 있는 모델로 어디까지 되나" 확인. M5 정식 구현이 아님 (docs/M5_HANDOFF.md).
  - 자료 범위 = 리서치 기간 (2026-08-25 까지). 가상 경기일 D = 2026-08-26 → 특징은 모두 '전날까지'. 플레이오프 기록은 안 씀
  - 가상 경기를 드래프트 기록 표에 한 판 끼워 넣고 m4_v1.build_rows 등 기존 특징 계산을 그대로 씀
  - 상대 차례는 M4 1순위 예측을 '실제 선택'으로 가정 (--given 으로 직접 넣을 수 있음)
  - T1 차례는 아래 추천 점수 1순위를 고른다고 가정

추천 (2026-11-08 기본 = 몬테카를로 + 조합 단위 승률, --mc 0 이면 아래 예전 점수)
  후보 행동마다 남은 드래프트를 --mc 번(기본 2000) 굴림: 남은 단계는 지금 상태에서 만든 M4 확률(상대 = 상대 예측, T1 = 평소 습관)로
  이미 나간 챔피언 빼고 · 빈 포지션만 뽑음 (중복 없이 → 예전 '독립 합' 부풀림 없음). 사이 단계의 가상 선택은 M4 특징에 반영 못 함
  완성 조합 승률 = σ((T1 이길 가치 합 − 상대 합) / 25), 팀 실력 차 0 가정. 같은 난수를 모든 후보에 씀 → 평소 대비 차이와 표준오차
  뺏기·밴 효과는 굴리기 안에서 저절로 계산 (상대가 그 챔피언을 못 가져감). T1 이 남은 자리를 평소 습관으로 채운다는 가정이라
  "지금 이 포지션을 채워 두면 나중에 습관 픽(값이 낮은)을 피함" 효과도 들어감

예전 점수 (--mc 0, 시험용, 가중치는 임시)
  밴 = P(상대가 이번 페이즈 남은 픽에서 c 를 고름) × (기본 2%p + max(이길 가치, 0))
  픽 = 이길 가치(m5_value.py: M1 조합 성격 근사 + 시간대 경로 나머지, 대회 %p. 없으면 예전 M1·M2 챔피언 단독) + 뺏기(P(상대 다음 픽 c) × (2%p + 이길 가치))
  맞라인 상성(M2 15분 골드 → %p)은 **점수에 안 넣고 근거 칸에만** (2026-11-08 m5_counter 리허설: 승패 예측에 더하면 대회 로그 손실 0.6552 → 0.6613)
       후보 = T1 이 이 상황에서 고를 확률 3% 이상 (최소 3개, 최대 15. M4 를 T1 입장으로 돌린 확률 — 선수 숙련·풀 반영)
확률 = M4 점수 소프트맥스. 온도 τ·바닥 ε 은 m5_calibrate.py 결과(리허설 학습 밖 점수로 맞춘 페이즈별 값, m5/calibration.json)
      없으면 08-19~08-25 LCK 경기로 맞춘 학습 안 τ (과신 쪽, 예전 방식)

2026-11-08 추가
  - 1수 앞보기: 후보마다 '그걸 고른 직후 상태'로 M4 를 다시 계산(m5_state, 후보당 약 0.2초)한 뒤 굴림 → 상대가 그 선택에 반응하는 것(응수·조합 막기 등)이 반영됨.
    '평소 대비' = 후보 안에서 T1 평소 확률로 가중한 평균 대비. '앞보기 없이' 칸 = 지금 상태 확률로만 굴린 값 (비교용)
  - 팀·선수별 이길 가치: 이길 가치 + K_DELTA × 그 팀 그 포지션 선수의 챔피언 숙련 편차(m5_skill δ, 골드 → %p).
    K_DELTA 0.32 = 숙련 편차를 승패 예측에 넣어 맞춘 배율 k 의 5개 평가 패치 평균 (0.01 · 0.43 · 0.12 · 0.84 · 0.18, 모두 양수, 각 패치 전 자료로 맞춤).
    로그 손실 개선은 확인 안 됨(+0.0003 [−0.0044, +0.0052]) → 작게 반영. 밴에서 '상대 선수 주력 챔피언'의 무게가 이 값으로 들어감
  - 밴 효과 = 상대 조합 가치를 낮춘 만큼만 (T1 쪽 값은 평소 기댓값으로 고정 — T1 남은 픽을 습관으로 채우는 가정 때문에
    'T1 이 습관적으로 고르는 값 낮은 챔피언을 밴'하면 이득처럼 보이는 착시를 막음)
  - 밴 동점권: 밴은 굴리기를 3배(기본 6000번) 하고, 1순위와 차이가 표준오차 2배 안이면 '동점권'. 동점권 안은 위협도
    (상대가 남은 픽에서 고를 확률 × 상대에게 주는 가치, 계산식이라 잡음 없음)로 순서
  - 피어리스 시리즈: --sets N 이면 같은 상대와 N 세트를 이어서 (앞 세트 = 시험판 진행 결과, 승자는 --winners, 다음 세트 선픽 = 앞 세트 진 팀으로 가정).
    --prev 파일로 실제 앞 세트 기록을 넣을 수도 있음. 앞 세트 기록을 상태 입력 표에 더해 피어리스 제외 · 시리즈 특징을 학습 때와 같은 방식으로

사용
  python src/pipeline/m5_sim.py --opp "Gen.G" --opp-first
  python src/pipeline/m5_sim.py --opp "Gen.G" --opp-first --sets 3 --winners "Gen.G,T1"
  python src/pipeline/m5_sim.py --opp "Gen.G" --prev prev.json   (앞 세트 실제 기록, 형식은 load_prev 설명)
  --given "7:Ahri,10:Vi"   상대(또는 T1) 단계별 실제 선택을 직접 지정 (DDragon id)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_features as F  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v1 as V  # noqa: E402
import m4_v4 as V4  # noqa: E402
import m4_v6 as V6  # noqa: E402
import m4_v7 as V7  # noqa: E402
import m4_validate as MV  # noqa: E402
import m5_state as ST  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m5"
D = "2026-08-26"
US = "T1"
POS = M1.POS
ROLE_KO = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서폿"}
BASE_PP = 2.0          # 상대가 원하는 픽을 빼는 기본 가치 (%p, 임시)
CAND_PICK = 15
PHASE = lambda s: "1밴" if s <= 6 else ("1픽" if s <= 12 else ("2밴" if s <= 16 else "2픽"))  # noqa: E731
K_DELTA = 0.32         # 숙련 편차(δ, %p) 반영 배율 — m5_skill 승패 시험의 패치별 k 평균
BAN_R_MULT = 3         # 밴은 차이가 작아 굴리기를 더 많이
# 2026-11-09 추천 쏠림: 픽 후보끼리 예상 승률 차이가 이길 가치 불확실성(구간 반폭 / 1.96) 합성 PICK_TIE_Z σ 안이면 동점권 → 동점권 안은 T1 평소 고를 확률 순
# (m5_rec_diverse.py 학습 밖 비교. 0 이면 예전처럼 예상 승률 순)
PICK_TIE_Z = 1.0
SD_MED = 1.0           # 구간이 없는 챔피언의 σ (표의 중앙값으로 바뀜)
MIN_HABIT = 0.03       # 픽 후보 = T1 이 이 상황에서 고를 확률 3% 이상 (최소 3개) — 안 쓰는 챔피언을 이길 가치만으로 올리지 않게


class Sim:
    def __init__(self):
        self.a, self.pro_pick, _, self.lineup, self.sq, self.champs = B.load()
        self.a = self.a.assign(league="LCK", fearless=True)
        cid = B.cid_map()
        ev = F.pro_events(cid)
        tmeta = pd.read_csv(PROCESSED / "i_meta_score_daily.csv", usecols=["valid_for", "champion_id", "meta_score", "meta_lck"])
        self.ds = F.DayStats(D, ev, self.pro_pick, self.sq, tmeta, self.champs)
        self.ex = V.day_extras(D, self.pro_pick, self.sq)
        self.ev_all, self.gm = V4.patch_events(cid)
        s = pd.read_csv(PROCESSED / "c_patch_champion_summary.csv", dtype={"patch": str})
        self.buff = {(r.patch, r.champion_id): (1.0 if r.label == "buff" else -1.0 if r.label == "nerf" else 0.0) for r in s.itertuples()}
        self.wins = V7.winners(self.a)
        self.spec, self.pick_m, self.ban_m = MV.load_final()
        self.tau = self.fit_tau()   # V.Cum 을 바꾸기 전에 (보정은 원래 build_rows 그대로)
        self.calib = self.load_calib()
        # 2026-11-08: 상태 입력 특징 계산 (m5_state) — 지금 상태에서 남은 단계 행을 한 번에 (유령 기록)
        self.sb = ST.StateBuilder(self.a, self.pro_pick, self.lineup, self.sq, self.champs)
        self._rows = {}
        self.wv = MM.win_value_table()   # 예전 이길 가치 (새 표가 없을 때만)
        f = OUT / "m5_value_champion.csv"  # m5_value.py: M1 조합 성격 근사 + 시간대 경로 나머지 (대회 %p)
        self.val = pd.read_csv(f).set_index("key") if f.exists() else None
        global SD_MED
        if self.val is not None and "lo" in self.val.columns:
            SD_MED = float(((self.val["hi"] - self.val["lo"]) / 3.92).median())
        if self.val is None:
            log.warning("m5_value_champion.csv 없음 → 예전 이길 가치(win_value_table) 사용")
        lc = pd.read_csv(PROCESSED / "m2" / "m2_lane_counter.csv")
        self.counter = {}
        for r in lc.itertuples():
            x, y = r.key.split(">")
            self.counter[(x, y)] = r.win_pp
        self.lck_patch = self.gm[self.gm["league"] == "LCK"].sort_values("day")["patch"].iloc[-1]
        ko = json.loads((H.ROOT / "dashboard" / "career" / "champ_ko.json").read_text(encoding="utf-8"))["names"]
        self.ko = lambda c: ko.get(c, c)
        self.mc = 0   # 몬테카를로 굴리기 횟수 (0 = 예전 점수)
        sk = pd.read_csv(OUT / "m5_skill.csv") if (OUT / "m5_skill.csv").exists() else pd.DataFrame(columns=["player", "key"])
        self.skill = {(r.player, r.key): r for r in sk.itertuples()}
        self.mid, self.set_no, self.fearless = None, 1, set()
        self.base = 0.0     # T1 기준 승리 로그 오즈 기본값 (팀 실력 차·진영, m5_draft 가 정함). 0 = 실력 같음
        self.resp = {}      # 1수 앞보기에서 후보마다 상대 다음 행동 예측 상위 (화면용)

    # ------------------------------------------------------------ 라인업 (08-25 이전 마지막 LCK 경기)
    def team_lineup(self, team):
        p = self.pro_pick[(self.pro_pick["league"] == "LCK") & (self.pro_pick["teamname"] == team)]
        last = p.sort_values("day")["gameid"].iloc[-1]
        return p[p["gameid"] == last].set_index("role")["playername"].to_dict()

    # ------------------------------------------------------------ 가상 경기 → M4 행
    def state_rows(self, gid, fp, lp, done):
        """done = [(단계, 챔피언, 포지션)] (1 단계부터 이어짐) → 남은 단계 전부의 M4 행 {단계: 행} (m5_state 유령 기록, 한 번 계산)."""
        key = (gid, tuple(done))
        if key not in self._rows:
            used = {c for _, c, _ in done}
            filler = next(c for c in self.champs if c not in used)
            dmap = {st: (c, ro) for st, c, ro in done}
            rpg = f"SIM_{gid}"
            recs = []
            for st in range(1, 21):
                ac, po, _ = V6.ORDER[st - 1]
                c, ro = dmap.get(st, (filler, None))
                recs.append({"GameId": gid, "MatchId": self.mid or gid, "N_GameInMatch": self.set_no, "RiotPlatformGameId": rpg, "day": D,
                             "date_kst": D + " 17:00", "Patch": "26.16", "step": st, "action": ac, "pick_order": po,
                             "team": fp if po == "fp" else lp, "opponent": lp if po == "fp" else fp, "cid": c, "role": ro,
                             "league": "LCK", "fearless": True, "is_winner": np.nan, "ghost": st not in dmap})
            rows = self.sb.build(pd.DataFrame(recs))
            self._rows = {key: {r[5]: r for r in rows}}
        return self._rows[key]

    def row_at(self, gid, fp, lp, done, target):
        """target 단계의 M4 행 (지금 상태 done 에서). 사이 단계는 아직 안 일어난 것으로 봄."""
        return self.state_rows(gid, fp, lp, done)[target]

    def set_lineup(self, gid, fp, lp):
        rpg = f"SIM_{gid}"
        add = {}
        for team in (fp, lp):
            for ro, pl in self.team_lineup(team).items():
                add[(rpg, team, ro)] = pl
        s = pd.Series(add)
        s.index.names = self.lineup.index.names
        self.lineup = pd.concat([self.lineup, s])
        self.sb.lineup = self.lineup

    # ------------------------------------------------------------ 점수 · 확률
    def raw_scores(self, row):
        if row[2] == "pick":
            _, _, X, _ = V7.mats([row], "pick", False)
            return self.pick_m.predict(np.hstack([X, np.ones((len(X), 1))]))
        for seg, gbm in self.ban_m:
            lo, hi = seg["steps"]
            if lo <= row[5] <= hi:
                sid, _, X, _ = V7.mats([row], "ban", True)
                lin = ((X - np.array(seg["linear"]["mu"])) / np.array(seg["linear"]["sd"])) @ np.array(seg["linear"]["coef"])
                gb = gbm.predict(np.hstack([X, np.ones((len(X), 1))]))
                return V6.zscore_within(sid, lin) + V6.zscore_within(sid, gb)
        raise ValueError(row[5])

    def probs(self, row):
        s = self.raw_scores(row)
        c = self.calib[PHASE(row[5])] if self.calib else {"tau": self.tau[row[2]], "eps": 0.0}
        z = s / c["tau"]
        p = np.exp(z - z.max())
        p = (1 - c["eps"]) * p / p.sum() + c["eps"] / len(p)
        return dict(zip(row[8]["cands"], p))

    def load_calib(self):
        """m5_calibrate.py 결과 (리허설 학습 밖 점수로 맞춘 페이즈별 τ·ε). 없으면 None → 학습 안 τ (과신)."""
        f = OUT / "calibration.json"
        if not f.exists():
            log.warning("calibration.json 없음 → 08-19~25 학습 안 τ 사용 (과신 쪽). 먼저 m5_calibrate.py 실행")
            return None
        return json.loads(f.read_text(encoding="utf-8"))["phase"]

    def fit_tau(self):
        f = OUT / "tau.json"
        if f.exists():
            return json.loads(f.read_text())
        OUT.mkdir(exist_ok=True)
        games = sorted(self.a.loc[(self.a["day"] >= "2026-08-19") & (self.a["day"] <= "2026-08-25"), "GameId"].unique())
        rows, _, _ = MV.build_rows_for(games, self.a, self.pro_pick, self.lineup, self.sq, self.champs)
        tau = {}
        for act in ("pick", "ban"):
            rr = [r for r in rows if r[2] == act and (act == "pick" or r[5] <= 6 or 13 <= r[5] <= 16)]
            S = [(self.raw_scores(r), r[3]) for r in rr]
            best = None
            for t in np.exp(np.linspace(np.log(0.05), np.log(5), 60)):
                nll = 0.0
                for s, y in S:
                    z = s / t
                    z = z - z.max()
                    nll -= z[y.argmax()] - np.log(np.exp(z).sum())
                nll /= len(S)
                if best is None or nll < best[0]:
                    best = (nll, t)
            tau[act] = float(best[1])
            log.info("τ %s = %.3f (평균 로그 손실 %.3f, %d단계)", act, best[1], best[0], len(S))
        f.write_text(json.dumps(tau))
        return tau

    # ------------------------------------------------------------ 포지션 · 이길 가치
    def role_for(self, c, open_roles):
        sh = {r: self.ds.role_share[r].get(c, 0) for r in POS}
        tot = sum(sh.values())
        if not open_roles:
            return None, 0.0
        r = max(open_roles, key=lambda r: sh[r])
        return r, (sh[r] / tot if tot else 0.0)

    def wv_pp(self, c, r):
        if not r:
            return 0.0
        if self.val is not None:
            k = f"{c}@{r}"
            return float(self.val.at[k, "value_pro"]) if k in self.val.index else 0.0
        return 25.0 * self.wv.get((c, r), 0.0)

    def timing(self, c, r):
        """(초반 ~15분, 후반 25분~·같은 상태) 나머지 몫 %p — 설명용."""
        k = f"{c}@{r}"
        if self.val is None or not r or k not in self.val.index:
            return np.nan, np.nan
        v = self.val.loc[k]
        return float(v["early10_pro"] + v["m15_pro"]), float(v["late_pro"])

    def ci(self, c, r):
        """이길 가치 95% 구간 (부트스트랩, m5_value.py)."""
        k = f"{c}@{r}"
        if self.val is None or not r or k not in self.val.index or "lo" not in self.val.columns:
            return np.nan, np.nan
        return float(self.val.at[k, "lo"]), float(self.val.at[k, "hi"])

    # ------------------------------------------------------------ 시리즈 (피어리스)
    def begin_match(self, opp):
        self.mid, self.set_no, self.fearless = f"SIM/{US}_vs_{opp}", 1, set()
        self._rows = {}

    def finish_set(self, gid, fp, lp, done, winner):
        """끝난 세트 기록을 상태 입력 표에 더함 (피어리스 · 시리즈 특징 · 승패)."""
        rpg = f"SIM_{gid}"
        recs = []
        for st, c, ro in done:
            ac, po, _ = V6.ORDER[st - 1]
            tm = fp if po == "fp" else lp
            recs.append({"GameId": gid, "MatchId": self.mid, "N_GameInMatch": self.set_no, "RiotPlatformGameId": rpg, "day": D,
                         "date_kst": D + " 17:00", "Patch": "26.16", "step": st, "action": ac, "pick_order": po, "team": tm,
                         "opponent": lp if tm == fp else fp, "cid": c, "role": ro, "league": "LCK", "fearless": True,
                         "is_winner": tm == winner})
        self.sb.add_records(pd.DataFrame(recs))
        self.fearless |= {c for st, c, ro in done if V6.ORDER[st - 1][0] == "pick"}
        self.set_no += 1
        self._rows = {}

    def load_prev(self, path, opp):
        """실제 앞 세트 기록. JSON: {"sets": [{"fp": 팀, "lp": 팀, "winner": 팀,
        "steps": [20개, 단계 순서, 밴 = "Bard", 픽 = "Cassiopeia@MIDDLE"]}]} (챔피언 = DDragon id, 포지션 = TOP·JUNGLE·MIDDLE·BOTTOM·UTILITY)."""
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        for st_ in d["sets"]:
            gid = f"{self.mid}/{self.set_no}"
            self.set_lineup(gid, st_["fp"], st_["lp"])
            done = []
            for k, x in enumerate(st_["steps"], start=1):
                c, _, ro = x.partition("@")
                done.append((k, c, ro or None))
            self.finish_set(gid, st_["fp"], st_["lp"], done, st_["winner"])
        return d["sets"]

    # ------------------------------------------------------------ 한 판 진행
    def play(self, opp, opp_first=True, given=None):
        given = given or {}
        if self.mid is None:
            self.begin_match(opp)
        if self.mc and not hasattr(self, "VAL"):
            self.mc_setup()
        fp, lp = (opp, US) if opp_first else (US, opp)
        gid = f"{self.mid}/{self.set_no}"
        self.set_lineup(gid, fp, lp)
        if self.mc:
            self.team_values(fp, lp)
        done, log_ = [], []
        for step in range(1, 21):
            act, po, _ = V6.ORDER[step - 1]
            team = fp if po == "fp" else lp
            other = lp if team == fp else fp
            picks = defaultdict(list)
            for st, c, ro in done:
                picks[fp if V6.ORDER[st - 1][1] == "fp" else lp].append((c, ro))
            open_ = {t: [r for r in POS if r not in {ro for _, ro in picks[t]}] for t in (fp, lp)}
            row = self.row_at(gid, fp, lp, done, step)
            p_self = self.probs(row)
            entry = {"step": step, "team": team, "action": act}
            if team == US:
                rec = self.recommend(gid, fp, lp, done, step, act, other, open_, picks, p_self)
                if self.mc:
                    rec = self.mc_rank(gid, fp, lp, done, step, act, rec, p_self, self.mc, open_)
                choice = given.get(step, rec.iloc[0]["cid"])
                entry["rec"] = rec
            else:
                top = sorted(p_self.items(), key=lambda kv: -kv[1])
                if act == "pick":
                    top = [(c, p) for c, p in top if self.role_for(c, open_[team])[1] >= 0.1] or top
                entry["pred"] = top[:5]
                choice = given.get(step, top[0][0])
            ro = self.role_for(choice, open_[team])[0] if act == "pick" else None
            entry["choice"], entry["role"] = choice, ro
            done.append((step, choice, ro))
            log_.append(entry)
            log.info("%d세트 %2d %s %s → %s %s", self.set_no, step, team, act, choice, ro or "")
        return fp, lp, log_, gid, done

    def opp_next_pick_steps(self, step, opp_po):
        phase = range(7, 13) if step <= 12 else range(17, 21)
        return [s for s in phase if s > step and V6.ORDER[s - 1][0] == "pick" and V6.ORDER[s - 1][1] == opp_po]

    def recommend(self, gid, fp, lp, done, step, act, opp, open_, picks, p_us):
        opp_po = "fp" if opp == fp else "lp"
        nxt = self.opp_next_pick_steps(step, opp_po)
        # 상대가 남은 픽에서 c 를 고를 확률 (단계마다 따로 계산해 합침, 그 사이 단계는 건너뛴 근사)
        p_any, p_next = defaultdict(float), {}
        miss = defaultdict(lambda: 1.0)
        for i, s in enumerate(nxt):
            ps = self.probs(self.row_at(gid, fp, lp, done, s))
            for c, p in ps.items():
                miss[c] *= (1 - p)
            if i == 0:
                p_next = ps
        for c, m in miss.items():
            p_any[c] = 1 - m
        rows = []
        if act == "ban":
            for c in p_us:
                ro, _ = self.role_for(c, open_[opp])
                w = self.wv_pp(c, ro)
                rows.append({"cid": c, "name": self.ko(c), "score": p_any[c] * (BASE_PP + max(w, 0)),
                             "p_opp": p_any[c], "wv_opp": w, "p_us_habit": p_us[c]})
        else:
            cand = sorted(p_us, key=lambda c: -p_us[c])
            cand = [c for c in cand if self.role_for(c, open_[US])[1] >= 0.1]
            cand = [c for i, c in enumerate(cand) if i < 3 or p_us[c] >= MIN_HABIT][:CAND_PICK]
            for c in cand:
                ro, _ = self.role_for(c, open_[US])
                w = self.wv_pp(c, ro)
                cnt, vs = 0.0, ""
                for q, qr in picks[opp]:
                    if qr == ro:   # 상성 표는 쌍마다 한 방향만 저장 → 반대 방향은 부호를 바꿈
                        x, y = f"{c}@{ro}", f"{q}@{qr}"
                        cnt = self.counter.get((x, y), -self.counter.get((y, x), 0.0))
                        vs = self.ko(q)
                ro_o, _ = self.role_for(c, open_[opp])
                deny = p_next.get(c, 0.0) * (BASE_PP + max(self.wv_pp(c, ro_o), 0))
                early, late = self.timing(c, ro)
                lo, hi = self.ci(c, ro)
                rows.append({"cid": c, "name": self.ko(c), "role": ROLE_KO.get(ro, ""), "score": w + deny,   # 상성은 점수에 안 넣음 (근거 칸만, m5_counter 리허설: 승패에 더하면 대회 예측이 나빠짐)
                             "wv": w, "counter": cnt, "vs": vs, "deny": deny, "p_opp_next": p_next.get(c, 0.0),
                             "p_us_habit": p_us[c], "early": early, "late": late, "lo": lo, "hi": hi})
        return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)

    # ------------------------------------------------------------ 몬테카를로 + 조합 단위 승률 (2026-11-08)
    def mc_setup(self):
        """챔피언 순서 고정 배열: 포지션 비중·가능 (m5_state.role_arrays), 이길 가치(대회 %p, 포지션별)."""
        self.cl = list(self.champs)
        self.ci_ = {c: i for i, c in enumerate(self.cl)}
        self.share, self.role_ok, self.role_any = ST.role_arrays(self.ds, self.cl)
        self.VAL = np.array([[self.wv_pp(c, r) for r in POS] for c in self.cl], float)

    def skill_of(self, team, c, r):
        """그 팀 그 포지션 선수의 c 숙련 편차 (m5_skill): (선수, δ 골드, δ %p, 대회 판수, 솔랭 판수) 또는 None."""
        pl = self.team_lineup(team).get(r)
        x = self.skill.get((pl, f"{c}@{r}")) if pl else None
        return (pl, x.delta_gold, x.delta_win_pp, int(x.n_pro), int(x.n_solo)) if x is not None else None

    def team_values(self, fp, lp):
        """팀·선수별 값 = 이길 가치 + K_DELTA × 숙련 편차 δ (%p)."""
        self.VALt = {}
        for tm in (fp, lp):
            M = self.VAL.copy()
            lu = self.team_lineup(tm)
            for j, r in enumerate(POS):
                pl = lu.get(r)
                if not pl:
                    continue
                for i, c in enumerate(self.cl):
                    x = self.skill.get((pl, f"{c}@{r}"))
                    if x is not None:
                        M[i, j] += K_DELTA * x.delta_win_pp
            self.VALt[tm] = M

    def future_probs(self, gid, fp, lp, done, step, p_now=None):
        """상태 done 에서 단계 step..20 의 M4 확률 (벡터). p_now = step 의 확률을 이미 알면 그대로."""
        out = {}
        for s in range(step, 21):
            ps = p_now if (s == step and p_now is not None) else self.probs(self.row_at(gid, fp, lp, done, s))
            v = np.zeros(len(self.cl))
            for c, p in ps.items():
                if c in self.ci_:
                    v[self.ci_[c]] = p
            out[s] = v
        return out

    def state_arrays(self, fp, lp, done):
        n = len(self.cl)
        taken0 = np.zeros(n, bool)
        for c in self.fearless:          # 앞 세트 픽 (피어리스) — M4 후보에서도 빠지지만 굴리기 대체 뽑기에서도 빠지게
            if c in self.ci_:
                taken0[self.ci_[c]] = True
        open0 = {fp: np.ones(5, bool), lp: np.ones(5, bool)}
        val0 = {fp: 0.0, lp: 0.0}
        for st, c, ro in done:
            if c in self.ci_:
                taken0[self.ci_[c]] = True
            if ro:
                t = fp if V6.ORDER[st - 1][1] == "fp" else lp
                open0[t][POS.index(ro)] = False
                val0[t] += self.VALt[t][self.ci_[c], POS.index(ro)] if c in self.ci_ else 0.0
        return taken0, open0, val0

    def roll(self, FP, start, fp, lp, done, R, seed, forced=None):
        taken0, open0, val0 = self.state_arrays(fp, lp, done)
        opp = lp if US == fp else fp
        if start > 20:
            return np.full(R, 1 / (1 + np.exp(-(self.base + (val0[US] - val0[opp]) / 25.0)))), np.zeros(len(self.cl)), np.full(R, val0[US]), np.full(R, val0[opp])
        _, val, E = ST.rollout(FP, start, fp, lp, taken0, open0, self.share, self.role_ok, self.role_any, R, seed,
                               forced=forced, VAL=self.VALt, val0=val0)
        opp_steps = [s for s in range(start, 21) if V6.ORDER[s - 1][0] == "pick" and (fp if V6.ORDER[s - 1][1] == "fp" else lp) == opp]
        return 1 / (1 + np.exp(-(self.base + (val[US] - val[opp]) / 25.0))), E[opp_steps].sum(0), val[US], val[opp]

    def mc_rank(self, gid, fp, lp, done, step, act, rec, p_us, R, open_):
        opp = lp if US == fp else fp
        if act == "ban":
            cand = list(dict.fromkeys(list(rec["cid"].head(12)) + sorted(p_us, key=lambda c: -p_us[c])[:3]))
            R = R * BAN_R_MULT
        else:
            cand = list(rec["cid"])
        cand = [c for c in cand if c in self.ci_]
        # 앞보기 없이 (지금 상태 확률로) — 비교용 + 상대가 남은 픽에서 고를 확률
        FP0 = self.future_probs(gid, fp, lp, done, step, p_us)
        _, p_opp, *_ = self.roll(FP0, step, fp, lp, done, R, 0)
        o0 = {c: self.roll(FP0, step, fp, lp, done, R, 0, forced=self.ci_[c]) for c in cand}
        # 1수 앞보기: 후보를 고른 직후 상태로 M4 를 다시 계산해 굴림 (같은 난수)
        r1 = {}
        for c in cand:
            ro = self.role_for(c, open_[US])[0] if act == "pick" else None
            dc = done + [(step, c, ro)]
            FPc = self.future_probs(gid, fp, lp, dc, step + 1) if step < 20 else {}
            r1[c] = self.roll(FPc, step + 1, fp, lp, dc, R, 0)
            nxt = next((s_ for s_ in range(step + 1, 21) if (fp if V6.ORDER[s_ - 1][1] == "fp" else lp) == opp), None)
            if nxt is not None:    # 상대 다음 행동 예측 (c 를 고른 직후 상태 기준, 화면용)
                v = FPc[nxt]
                top = np.argsort(-v)[:3]
                self.resp[c] = {"step": nxt, "action": V6.ORDER[nxt - 1][0], "top": [(self.cl[j], float(v[j])) for j in top]}
        w = np.array([p_us.get(c, 0.0) for c in cand])
        w = w / w.sum() if w.sum() > 0 else np.full(len(cand), 1 / len(cand))
        if act == "ban":
            # 밴 효과 = 상대 조합 가치를 얼마나 낮추나만. T1 쪽 값은 평소 기댓값으로 고정
            # (T1 남은 픽을 습관 확률로 채우므로, T1 이 습관적으로 고르는 값 낮은 챔피언을 밴하면 이득처럼 보이는 착시를 막음)
            us1 = sum(wi * r1[c][2].mean() for wi, c in zip(w, cand))
            us0 = sum(wi * o0[c][2].mean() for wi, c in zip(w, cand))
            r1 = {c: 1 / (1 + np.exp(-(self.base + (us1 - r1[c][3]) / 25.0))) for c in cand}
            r0 = {c: 1 / (1 + np.exp(-(self.base + (us0 - o0[c][3]) / 25.0))) for c in cand}
        else:
            r1 = {c: r1[c][0] for c in cand}
            r0 = {c: o0[c][0] for c in cand}
        base1 = sum(wi * r1[c] for wi, c in zip(w, cand))
        base0 = sum(wi * r0[c] for wi, c in zip(w, cand))
        rec = rec[rec["cid"].isin(cand)].copy()
        rec["old_rank"] = np.arange(1, len(rec) + 1)
        rec["mc_wp"] = rec["cid"].map(lambda c: 100 * r1[c].mean())
        rec["mc_d"] = rec["cid"].map(lambda c: 100 * (r1[c] - base1).mean())
        rec["mc_se"] = rec["cid"].map(lambda c: 100 * np.std(r1[c] - base1) / np.sqrt(R))
        rec["mc_d0"] = rec["cid"].map(lambda c: 100 * (r0[c] - base0).mean())
        rec["p_opp_mc"] = rec["cid"].map(lambda c: p_opp[self.ci_[c]])
        if act == "ban":
            def threat(c):    # 상대가 남은 픽에서 c 를 고를 확률 × 상대에게 주는 가치 (빈 포지션 중 가장 맞는 곳, 선수 숙련 포함). 음수 = 상대가 가져가도 손해 아님
                ro_o = self.role_for(c, open_[opp])[0]
                v = self.VALt[opp][self.ci_[c], POS.index(ro_o)] if ro_o else 0.0
                return p_opp[self.ci_[c]] * v
            rec["threat"] = rec["cid"].map(threat)
            rec["skill_opp"] = rec["cid"].map(lambda c: self.skill_of(opp, c, self.role_for(c, open_[opp])[0]) if open_[opp] else None)
            top = max(cand, key=lambda c: r1[c].mean())
            rec["tie"] = rec["cid"].map(lambda c: c == top or 100 * (r1[top] - r1[c]).mean() < 2 * 100 * np.std(r1[top] - r1[c]) / np.sqrt(R))
            rec["_k1"] = np.where(rec["tie"], 1, 0)
            rec["_k2"] = np.where(rec["tie"], rec["threat"], rec["mc_d"])
            out = rec.sort_values(["_k1", "_k2"], ascending=False).drop(columns=["_k1", "_k2"]).reset_index(drop=True)
        else:
            rec["skill_us"] = rec["cid"].map(lambda c: self.skill_of(US, c, self.role_for(c, open_[US])[0]))
            sdv = ((rec["hi"] - rec["lo"]) / 3.92).fillna(SD_MED)
            it = rec["mc_d"].idxmax()
            rec["value_best"] = rec.index == it
            rec["tie_sd"] = np.sqrt(sdv[it] ** 2 + sdv ** 2)
            rec["tie"] = (rec.at[it, "mc_d"] - rec["mc_d"]) <= PICK_TIE_Z * rec["tie_sd"] if PICK_TIE_Z > 0 else rec["value_best"]
            rec["_k1"] = np.where(rec["tie"], 1, 0)
            rec["_k2"] = np.where(rec["tie"], rec["p_us_habit"], rec["mc_d"])
            out = rec.sort_values(["_k1", "_k2"], ascending=False).drop(columns=["_k1", "_k2"]).reset_index(drop=True)
        out.attrs["mc_base"] = 100 * base1.mean()
        out.attrs["R"] = R
        return out


def fmt_skill(x):
    if not x:
        return "—"
    return f"{x[0]} {x[1]:+.0f}골드 ({x[3]}+{x[4]}판)"


def report(sim, opp, fp, lp, lg, set_no=1, fear=()):
    L = [f"## {US} vs {opp} ({set_no}세트, 선픽 {fp} · 후픽 {lp}, 가상 경기일 {D})", ""]
    if fear:
        L.append(f"피어리스로 못 쓰는 챔피언 {len(fear)}개: " + " · ".join(sorted(sim.ko(c) for c in fear)))
        L.append("")
    L.append(f"라인업 {US}: " + " · ".join(f"{ROLE_KO[r]} {p}" for r, p in sim.team_lineup(US).items()))
    L.append(f"라인업 {opp}: " + " · ".join(f"{ROLE_KO[r]} {p}" for r, p in sim.team_lineup(opp).items()))
    L.append("")
    for e in lg:
        ph = "1밴" if e["step"] <= 6 else ("1픽" if e["step"] <= 12 else ("2밴" if e["step"] <= 16 else "2픽"))
        what = "밴" if e["action"] == "ban" else "픽"
        ch = sim.ko(e["choice"]) + (f"({ROLE_KO.get(e['role'], '')})" if e["role"] else "")
        if e["team"] == US:
            L.append(f"### {e['step']}단계 · {ph} · **{US} {what} 추천** → 선택 {ch}")
            r = e["rec"]
            if "mc_wp" in r:
                L.append(f"- 몬테카를로 {r.attrs.get('R', sim.mc)}번 · 1수 앞보기: T1 이 평소대로(후보 안 평소 확률 가중) 할 때 완성 조합 예상 승률 "
                         f"{r.attrs.get('mc_base', np.nan):.1f}% (팀 실력 차 0 가정, 이길 가치 + 숙련 편차 × {K_DELTA})")
                if e["action"] == "ban":
                    L += ["| 순위 | 챔피언 | 평소 대비 %p (± 표준오차) | 앞보기 없이 | 동점권 | 위협도 %p (고를 확률 × 상대 가치) | 상대 선수 숙련 (15분 골드) "
                          "| 상대가 남은 픽에서 고를 확률 | T1 평소 밴 확률 |", "|---|---|---|---|---|---|---|---|---|"]
                    for i, x in r.head(6).iterrows():
                        L.append(f"| {i + 1} | {x['name']} | {x['mc_d']:+.2f} (±{x['mc_se']:.2f}) | {x['mc_d0']:+.2f} | {'○' if x['tie'] else ''} "
                                 f"| {x['threat']:+.2f} | {fmt_skill(x['skill_opp'])} | {x['p_opp_mc']:.0%} | {x['p_us_habit']:.0%} |")
                else:
                    L += ["| 순위 | 챔피언 | 포지션 | 예상 승률 % | 평소 대비 %p (± 표준오차) | 앞보기 없이 | 이길 가치 [95% 구간] (초반 · 후반) | 우리 선수 숙련 (15분 골드) "
                          "| 맞라인 상성 (근거) | 상대 다음 픽 확률 | T1 평소 고를 확률 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
                    for i, x in r.head(6).iterrows():
                        vs = f"{x['counter']:+.1f} (vs {x['vs']})" if x["vs"] else "—"
                        tm = f" [{x['lo']:+.1f}, {x['hi']:+.1f}] ({x['early']:+.1f} · {x['late']:+.1f})" if pd.notna(x.get("early")) else ""
                        L.append(f"| {i + 1} | {x['name']} | {x['role']} | {x['mc_wp']:.1f} | {x['mc_d']:+.2f} (±{x['mc_se']:.2f}) | {x['mc_d0']:+.2f} "
                                 f"| {x['wv']:+.1f}{tm} | {fmt_skill(x['skill_us'])} | {vs} | {x['p_opp_next']:.0%} | {x['p_us_habit']:.0%} |")
            elif e["action"] == "ban":
                L += ["| 순위 | 챔피언 | 점수 | 상대가 남은 픽에서 고를 확률 | 상대 기준 이길 가치 %p | T1 평소 밴 확률 |", "|---|---|---|---|---|---|"]
                for i, x in r.head(5).iterrows():
                    L.append(f"| {i + 1} | {x['name']} | {x['score']:.2f} | {x['p_opp']:.0%} | {x['wv_opp']:+.1f} | {x['p_us_habit']:.0%} |")
            else:
                L += ["| 순위 | 챔피언 | 포지션 | 점수 %p | 이길 가치 [95% 구간] (초반 · 후반) | 맞라인 상성 (근거, 점수 밖) | 뺏기 | 상대 다음 픽 확률 | T1 평소 고를 확률 |", "|---|---|---|---|---|---|---|---|---|"]
                for i, x in r.head(5).iterrows():
                    vs = f"{x['counter']:+.1f} (vs {x['vs']})" if x["vs"] else "—"
                    tm = f" [{x['lo']:+.1f}, {x['hi']:+.1f}] ({x['early']:+.1f} · {x['late']:+.1f})" if pd.notna(x.get("early")) else ""
                    L.append(f"| {i + 1} | {x['name']} | {x['role']} | {x['score']:+.1f} | {x['wv']:+.1f}{tm} | {vs} | {x['deny']:+.1f} | {x['p_opp_next']:.0%} | {x['p_us_habit']:.0%} |")
        else:
            pr = " · ".join(f"{sim.ko(c)} {p:.0%}" for c, p in e["pred"])
            L.append(f"### {e['step']}단계 · {ph} · {e['team']} {what} → {ch}  (M4 예측: {pr})")
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--opp", action="append", required=True)
    ap.add_argument("--opp-first", action="store_true")
    ap.add_argument("--given", default="")
    ap.add_argument("--mc", type=int, default=2000, help="몬테카를로 굴리기 횟수. 0 = 예전 점수 (상대 확률 독립 합 + 뺏기 가산)")
    ap.add_argument("--out", default="m5_sim.md")
    ap.add_argument("--sets", type=int, default=1, help="같은 상대와 이어서 진행할 세트 수 (피어리스)")
    ap.add_argument("--winners", default="", help="앞 세트 승자 (쉼표, 세트 순서). 비면 상대 · T1 번갈아")
    ap.add_argument("--prev", default="", help="실제 앞 세트 기록 JSON (Sim.load_prev 설명)")
    args = ap.parse_args()
    given = {int(k): v for k, v in (x.split(":") for x in args.given.split(",") if x)}
    sim = Sim()
    sim.mc = args.mc
    OUT.mkdir(exist_ok=True)
    if sim.calib:
        cal = " · ".join(f"{ph} τ {c['tau']:.2f} ε {c['eps']}" for ph, c in sim.calib.items())
        head = f"보정: 리허설 학습 밖 ({cal})"
    else:
        head = f"보정: 학습 안 τ 픽 {sim.tau['pick']:.2f} · 밴 {sim.tau['ban']:.2f} (과신 쪽)"
    parts = [f"# M5 시험판 — 가상 드래프트 ({head})", ""]
    for opp in args.opp:
        sim.begin_match(opp)
        opp_first = args.opp_first
        if args.prev:
            prev = sim.load_prev(args.prev, opp)
            last = prev[-1]
            opp_first = (last["winner"] == US)      # 앞 세트 진 팀이 선픽
            parts.append(f"## {US} vs {opp} — 앞 세트 {len(prev)}개 입력 (승자: {', '.join(p['winner'] for p in prev)})\n")
        winners = [w for w in args.winners.split(",") if w] or [opp if k % 2 == 0 else US for k in range(args.sets)]
        for k in range(args.sets):
            fp, lp, lg, gid, done = sim.play(opp, opp_first, given)
            parts.append(report(sim, opp, fp, lp, lg, sim.set_no, sim.fearless))
            if k < args.sets - 1:
                win = winners[k] if k < len(winners) else opp
                if win not in (US, opp):      # 다른 상대 이름 (여러 상대를 한 번에 돌릴 때) → 이 시리즈의 상대 승리로
                    win = opp
                parts.append(f"> {sim.set_no}세트 승자(가정): **{win}** → 다음 세트 선픽 = 진 팀\n")
                sim.finish_set(gid, fp, lp, done, win)
                opp_first = (win == US)
        sim.mid = None
    (OUT / args.out).write_text("\n".join(parts), encoding="utf-8")
    log.info("→ %s", OUT / args.out)


if __name__ == "__main__":
    main()
