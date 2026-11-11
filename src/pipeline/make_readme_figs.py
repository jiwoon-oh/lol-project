"""README 그래프 7장 (2026-11-11, 글자는 영어) — 결과 파일의 집계 숫자만 읽어 docs/img/*.png 로 저장 (원문 없음)

    python src/pipeline/make_readme_figs.py      (matplotlib 필요. 이 PC 에서는 anaconda 파이썬)

읽는 파일 (data/processed/2026/, git 제외)
  1·2  m4/validation/m4_validation_ranks.csv      플레이오프 55경기 단계별 실제 선택 순위 (M4 최종 · 기준선)
  3    m5/m5_replay_raw_26.08_26.09_26.10.pkl      리허설 밖 재생: "팀 X 가 이번 페이즈 남은 픽에서 c" 예측과 실제
  4    m5/m5_value_w.md, m5/m5_ctx.md               이길 가치 변형별 평가 패치 5개 대회 로그 손실
  5    m5/m5_value_champion.csv                     챔피언 이길 가치 (대회 %p, 95% 구간)
  6    m5/rec_check/m5_rec_check.md                 조합 가치 4분위별 기대 vs 실제 블루 승률
  7    m5/rec_check/*.md, m5/m5_skill_eval.md, m5/m5_value_w.md, m5/m5_ctx.md   시험별 효과와 95% 구간
"""
from __future__ import annotations

import re
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
P = ROOT / "data" / "processed" / "2026"
OUT = ROOT / "docs" / "img"

# 포트폴리오 케이스 스터디 팔레트 (2026-11-11, 따뜻한 회색 배경 + 테라코타 강조)
# 변수 이름은 예전 그대로: BLUE = 이 프로젝트 방법(강조, 테라코타), ORANGE = 예전 방식(진한 회색), GRAY = 기준선·미통과
BLUE, ORANGE, GRAY = "#B0472E", "#3D3C39", "#A8A399"
INK, INK2, GRID, SURF = "#262626", "#666460", "#D6D3CC", "#E7E5E0"
plt.rcParams.update({
    "font.family": ["Segoe UI", "Malgun Gothic", "DejaVu Sans"], "axes.unicode_minus": False, "font.size": 11,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.titlesize": 14, "axes.titleweight": "bold", "axes.titlecolor": INK, "axes.titlelocation": "left",
    "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
})
POS_EN = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
NAME_EN = {"MonkeyKing": "Wukong", "KSante": "K'Sante", "LeeSin": "Lee Sin", "JarvanIV": "Jarvan IV", "TwistedFate": "Twisted Fate",
           "MissFortune": "Miss Fortune", "KogMaw": "Kog'Maw", "RekSai": "Rek'Sai", "XinZhao": "Xin Zhao", "DrMundo": "Dr. Mundo",
           "AurelionSol": "Aurelion Sol", "TahmKench": "Tahm Kench", "Belveth": "Bel'Veth", "Chogath": "Cho'Gath",
           "Kaisa": "Kai'Sa", "Khazix": "Kha'Zix", "Velkoz": "Vel'Koz", "Nunu": "Nunu & Willump", "Renata": "Renata Glasc"}


def save(fig, name, note):
    fig.text(0.01, 0.01, note, fontsize=9, color=INK2, ha="left", va="bottom")
    fig.savefig(OUT / name, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print("→", OUT / name)


def num(s):
    return float(s.replace("−", "-").replace("%", "").replace("p", "").replace("+", ""))


def ci(text):
    """'-0.0196 [-0.0347, -0.0046]' → (점, 하한, 상한)."""
    text = text.replace("*", "").replace("95% ", "")
    m = re.search(r"([+\-−]?\d+\.\d+)\s*\[\s*([+\-−]?\d+\.\d+),\s*([+\-−]?\d+\.\d+)\s*\]", text)
    return tuple(num(x) for x in m.groups())


def champ_label(key):
    c, p = key.split("@")
    return f"{NAME_EN.get(c, c)} ({POS_EN.get(p, p)})"


# ---------------------------------------------------------------- 1 · 2 상대 예측 (플레이오프)
def fig_m4():
    r = pd.read_csv(P / "m4" / "validation" / "m4_validation_ranks.csv")
    m4 = r[(r["condition"] == "A") & (r["model"] == "M4 최종")]
    bl = r[r["model"] == "기준선"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), sharey=True)
    for ax, act, en in zip(axes, ("pick", "ban"), ("Picks", "Bans")):
        ks = [1, 3, 5]
        a = [100 * (m4.loc[m4["action"] == act, "rank"] <= k).mean() for k in ks]
        b = [100 * (bl.loc[bl["action"] == act, "rank"] <= k).mean() for k in ks]
        x = np.arange(3)
        ax.bar(x - 0.19, b, 0.36, color=GRAY, label="Baseline (meta + open roles + player pools)", zorder=3)
        ax.bar(x + 0.19, a, 0.36, color=BLUE, label="M4 (in-draft)", zorder=3)
        for xi, v in zip(x - 0.19, b):
            ax.text(xi, v + 1.5, f"{v:.1f}", ha="center", color=INK2, fontsize=10)
        for xi, v in zip(x + 0.19, a):
            ax.text(xi, v + 1.5, f"{v:.1f}", ha="center", color=INK, fontsize=10, fontweight="bold")
        ax.set_xticks(x, [f"Top {k}" for k in ks])
        ax.set_title(en, loc="center", fontsize=12)
        ax.set_ylim(0, 80)
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("Actual choice within predicted top k (%)")
    axes[0].legend(loc="upper left", frameon=False, fontsize=9.5)
    fig.suptitle("Predicting the opponent's next pick / ban — LCK Play-In & Playoffs, 55 unseen games (evaluated once)",
                 x=0.01, ha="left", fontsize=13, fontweight="bold", color=INK)
    fig.tight_layout(rect=(0, 0.04, 1, 0.95))
    save(fig, "m4_topk.png", "Random guessing puts the actual choice in the top 5 about 4% of the time. Source: m4/validation/m4_validation_ranks.csv")

    ph = {"Ban phase 1": (1, 6), "Pick phase 1": (7, 12), "Ban phase 2": (13, 16), "Pick phase 2": (17, 20)}
    rows = []
    for k, (lo, hi) in ph.items():
        f = lambda d: 100 * (d.loc[d["step"].between(lo, hi), "rank"] <= 5).mean()  # noqa: E731
        rows.append((k, f(bl), f(m4)))
    fig, ax = plt.subplots(figsize=(9, 3.6))
    for i, (k, b, a) in enumerate(rows):
        ax.plot([b, a], [i, i], color=GRID, lw=6, solid_capstyle="round", zorder=2)
        ax.scatter([b], [i], s=90, color=GRAY, zorder=3, edgecolor=SURF, linewidth=2)
        ax.scatter([a], [i], s=110, color=BLUE, zorder=4, edgecolor=SURF, linewidth=2)
        ax.text(b - 1.5, i, f"{b:.1f}", ha="right", va="center", color=INK2, fontsize=10)
        ax.text(a + 1.5, i, f"{a:.1f}  (+{a - b:.1f} pp)", ha="left", va="center", color=INK, fontsize=10, fontweight="bold")
    ax.set_yticks(range(4), [r[0] for r in rows])
    ax.invert_yaxis()
    ax.set_xlim(10, 95)
    ax.set_xlabel("Actual choice within top 5 (%)")
    ax.grid(axis="y", visible=False)
    ax.scatter([], [], s=90, color=GRAY, label="Baseline")
    ax.scatter([], [], s=110, color=BLUE, label="M4")
    ax.legend(loc="lower right", frameon=False, fontsize=9.5)
    ax.set_title("Top-5 accuracy by draft phase — the biggest gain is in reactive bans (ban phase 2)")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    save(fig, "m4_phase.png", "Playoffs, 55 games. Ban phase 2 benefits from reactive features: opponent's open roles, blocking counters to our picks, blocking combos. Source: m4_validation_ranks.csv")


# ---------------------------------------------------------------- 3 확률 보정 곡선
def fig_calib():
    _, ev, _ = pd.read_pickle(P / "m5" / "m5_replay_raw_26.08_26.09_26.10.pkl")
    items = ev["페이즈"]
    bins = [0, 0.03, 0.1, 0.2, 0.35, 0.5, 0.7, 1.01]
    fig, ax = plt.subplots(figsize=(6.4, 6))
    ax.plot([0, 100], [0, 100], color=INK2, lw=1, ls=(0, (4, 3)), zorder=1)
    ax.text(83, 89, "perfect calibration", color=INK2, fontsize=9.5, rotation=45, ha="center")
    for idx, col, lab in ((0, ORANGE, "Before: per-step probabilities combined independently"), (1, BLUE, "Monte Carlo draft rollouts")):
        Pp = np.concatenate([x[idx] for x in items])
        Y = np.concatenate([x[2] for x in items])
        xs, ys, ns = [], [], []
        for lo, hi in zip(bins[:-1], bins[1:]):
            m = (Pp >= lo) & (Pp < hi)
            if m.sum() >= 30:
                xs.append(100 * Pp[m].mean()); ys.append(100 * Y[m].mean()); ns.append(m.sum())
        ax.plot(xs, ys, color=col, lw=2, zorder=3, label=lab)
        ax.scatter(xs, ys, s=[max(18, min(160, np.sqrt(n) * 2.2)) for n in ns], color=col, zorder=4, edgecolor=SURF, linewidth=2)
        ax.annotate(f"{ys[-1]:.0f}%", (xs[-1], ys[-1]), xytext=(8, -4), textcoords="offset points", color=INK, fontsize=10, fontweight="bold")
    ax.set_xlim(0, 100); ax.set_ylim(0, 100)
    ax.set_xlabel("Predicted probability (%)"); ax.set_ylabel("Observed frequency (%)")
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    ax.set_title("Calibration: \"the opponent picks champion c\nlater in this phase\"", fontsize=12.5)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    save(fig, "calibration.png", "26.08–26.10 LCK, 138 games not used for any tuning; every draft rewound to each step. Marker size = number of events.\n"
         "The old method double-counted a champion across steps and was overconfident at high probabilities.\nSource: m5_replay_raw_26.08_26.09_26.10.pkl")


# ---------------------------------------------------------------- 4 이길 가치 변형
def fig_value_variants():
    w = (P / "m5" / "m5_value_w.md").read_text(encoding="utf-8")
    rows = {}
    for line in w.splitlines():
        m = re.match(r"\|\s*\**([^|*]+?)\**\s*\|\s*(0\.\d+)\s*\|", line)
        if m:
            rows[m.group(1).strip()] = float(m.group(2))
    ctx = re.search(r"C (0\.\d+) → \+ 맥락 (0\.\d+)", (P / "m5" / "m5_ctx.md").read_text(encoding="utf-8"))
    show = [("Team strength controls only (Elo, league, mastery)", rows["A 통제만"], GRAY),
            ("M1 composition-trait model", rows["M1 성격만"], GRAY),
            ("E-C: M1 + time-window paths", rows["E-C (지금 시험판)"], GRAY),
            ("C + composition context (non-additive M1 part)", float(ctx.group(2)), GRAY),
            ("C + learned weight × M1", rows["W-C C 경로 + w × M1"], GRAY),
            ("C: time-window gold paths (adopted)", rows["C 시간대 경로 (지금 비교 기준)"], BLUE)]
    show.sort(key=lambda t: -t[1])
    fig, ax = plt.subplots(figsize=(9.5, 4))
    y = np.arange(len(show))
    for i, (lab, v, c) in enumerate(show):
        ax.barh(i, v - 0.62, left=0.62, color=c, height=0.6, zorder=3)
        ax.text(v + 0.0004, i, f"{v:.4f}", va="center", color=INK if c == BLUE else INK2, fontsize=10, fontweight="bold" if c == BLUE else "normal")
    ax.set_yticks(y, [s[0] for s in show])
    ax.set_xlim(0.62, 0.6465)
    ax.set_xlabel("Pro-game log loss (lower is better; axis starts at 0.62)")
    ax.grid(axis="y", visible=False)
    ax.set_title("Win-value variants — time-window gold paths alone (C) predict best")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    save(fig, "value_variants.png", "5 evaluation patches (26.08, 26.09, 26.10, 26.14, 26.15), 970 pro games; each patch trained only on earlier data. Coin flip = 0.693.\n"
         "Source: m5_value_w.md, m5_ctx.md")


# ---------------------------------------------------------------- 5 챔피언 이길 가치
def fig_champions():
    t = pd.read_csv(P / "m5" / "m5_value_champion.csv")
    t = t[t["n"] >= 200].sort_values("value_pro")
    sel = pd.concat([t.head(10), t.tail(10)])
    fig, ax = plt.subplots(figsize=(8.5, 7.2))
    y = np.arange(len(sel))
    for i, r in enumerate(sel.itertuples()):
        c = BLUE if r.value_pro > 0 else ORANGE
        ax.plot([r.lo, r.hi], [i, i], color=c, lw=2, alpha=0.45, solid_capstyle="round", zorder=2)
        ax.scatter([r.value_pro], [i], s=60, color=c, zorder=3, edgecolor=SURF, linewidth=1.5)
    ax.axvline(0, color=INK2, lw=1)
    ax.axhline(9.5, color=GRID, lw=1, ls=(0, (3, 3)))
    ax.set_yticks(y, [f"{champ_label(k)}  ({n:,} games)" for k, n in zip(sel["key"], sel["n"])], fontsize=9.5)
    ax.set_xlabel("Win value (pro win-rate pp; dot = estimate, line = 95% interval)")
    ax.grid(axis="y", visible=False)
    ax.set_title("Champion win value — top and bottom 10 (200+ solo-queue + pro games)")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    save(fig, "champion_value.png", "Gold-lead paths at 10/15/20/25 min + the share of wins at equal gold. All data to 08-25; interval = 50 within-league bootstrap resamples.\n"
         "Source: m5_value_champion.csv")


# ---------------------------------------------------------------- 6 추천 검증 4분위
def fig_rec_quartile():
    md = (P / "m5" / "rec_check" / "m5_rec_check.md").read_text(encoding="utf-8")
    sec = md[md.index("| 조합 가치 차 4분위"):]
    rows = []
    for line in sec.splitlines()[2:6]:
        c = [x.strip() for x in line.strip("|").split("|")]
        rows.append((c[0], int(c[1]), c[2].replace("%p", " pp"), num(c[3]), num(c[4])))
    labs = ["Blue much worse\n(bottom 25%)", "Blue slightly worse", "Blue slightly better", "Blue much better\n(top 25%)"]
    fig, ax = plt.subplots(figsize=(8.5, 4.4))
    x = np.arange(4)
    ax.bar(x - 0.19, [r[3] for r in rows], 0.36, color=GRAY, label="Expected blue win rate (team strength + side only)", zorder=3)
    ax.bar(x + 0.19, [r[4] for r in rows], 0.36, color=BLUE, label="Actual blue win rate", zorder=3)
    for i, r in enumerate(rows):
        ax.text(i - 0.19, r[3] + 1.2, f"{r[3]:.0f}", ha="center", color=INK2, fontsize=10)
        ax.text(i + 0.19, r[4] + 1.2, f"{r[4]:.0f}", ha="center", color=INK, fontsize=10, fontweight="bold")
    ax.set_xticks(x, [f"{l}\nmean {r[2]} · {r[1]} games" for l, r in zip(labs, rows)], fontsize=9.5)
    ax.set_ylim(0, 80)
    ax.set_ylabel("Blue win rate (%)")
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper left", frameon=False, fontsize=9.5)
    ax.set_title("Draft win-value gap vs actual results (301 out-of-sample LCK games)")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    save(fig, "rec_check_quartiles.png", "Win-value tables rebuilt from data before each set. The effect shows mostly for clearly worse drafts (bottom 25%).\n"
         "Log loss 0.6261 → 0.6065. Observational data, not causal. Source: rec_check/m5_rec_check.md")


# ---------------------------------------------------------------- 7 시험 결과 숲 그림
def fig_forest():
    rd = lambda p: (P / p).read_text(encoding="utf-8")  # noqa: E731
    rec = rd("m5/rec_check/m5_rec_check.md")
    ban = rd("m5/rec_check/m5_ban_check.md")
    ctxm = rd("m5/m5_ctx.md")
    sk = rd("m5/m5_skill_eval.md")
    w = rd("m5/m5_value_w.md")

    def line_of(text, key):
        return next(l for l in text.splitlines() if key in l)
    wc = ci(line_of(w, "| W-C C 경로 + w × M1").split("|")[5])
    panel_a = [("Win value of the full draft\n(on top of team strength + side, 301 games)", ci(line_of(rec, "| 전체 |"))),
               ("M1 composition traits (weight × M1, on top of C)", wc),
               ("Composition context (non-additive M1, on top of C)", ci(line_of(ctxm, "5개 패치 합 대회 970판"))),
               ("Player × champion mastery δ (on top of controls)", ci(line_of(sk, "| + 숙련 편차 δ | 927 | 0.6317")))]
    panel_b = [("Picks: deviation from recommendation", ci(line_of(rec, "놓친 몫 기울기"))),
               ("Bans: deviation from recommendation", ci(line_of(ban, "기울기 **")))]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.2), gridspec_kw={"width_ratios": [1.5, 1], "wspace": 1.0})
    for ax, rows, xlab, ttl in ((axes[0], panel_a, "Log-loss difference (negative = better at predicting results)", "Does it help predict results?"),
                                (axes[1], panel_b, "Slope of missed value (negative = teams that\ndeviated more lost more)", "Did following recommendations win more?")):
        for i, (lab, (pt, lo, hi)) in enumerate(rows):
            ok = hi < 0
            c = BLUE if ok else GRAY
            ax.plot([lo, hi], [i, i], color=c, lw=2.2, solid_capstyle="round", zorder=2)
            ax.scatter([pt], [i], s=70, color=c, zorder=3, edgecolor=SURF, linewidth=1.5)
            ax.text(1.02, i, "pass" if ok else "fail", transform=ax.get_yaxis_transform(), va="center", ha="left",
                    color=INK if ok else INK2, fontsize=10, fontweight="bold" if ok else "normal")
        ax.axvline(0, color=INK2, lw=1)
        ax.set_yticks(range(len(rows)), [r[0] for r in rows], fontsize=9.5)
        ax.set_xlabel(xlab, fontsize=10)
        ax.grid(axis="y", visible=False)
        ax.set_title(ttl, fontsize=12.5)
        lo_all = min(r[1][1] for r in rows); hi_all = max(r[1][2] for r in rows)
        pad = (hi_all - lo_all) * 0.1
        ax.set_xlim(lo_all - pad, hi_all + pad)
        ax.set_ylim(len(rows) - 0.5, -0.5)
    fig.suptitle("Test results at a glance — dot = estimate, line = 95% interval; pass = whole interval left of 0", x=0.01, ha="left",
                 fontsize=13, fontweight="bold", color=INK)
    fig.subplots_adjust(left=0.25, right=0.95, top=0.82, bottom=0.22)
    save(fig, "tests_forest.png", "Pass = upper bound of the 95% interval < 0. Evaluated on games not used for training. Source: rec_check/*.md, m5_value_w.md, m5_ctx.md, m5_skill_eval.md")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    fig_m4()
    fig_calib()
    fig_value_variants()
    fig_champions()
    fig_rec_quartile()
    fig_forest()
