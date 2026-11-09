"""T1 밴픽 대시보드 서버 (2026-11-08) — 로컬 전용

    python dashboard/draft_server.py [--port 8765]   → http://localhost:8765

화면 dashboard/draft.html + JSON API. 계산은 src/pipeline/m5_draft.py (M4 고정 모델 + 몬테카를로 추천).
시작에 약 20초 (모델·자료 불러오기). 한 사람이 쓰는 로컬 도구라 요청은 하나씩 처리.
자료 범위는 시험판과 같음 (2026-08-25 까지, 가상 경기일 08-26). 챔피언 그림은 브라우저가 Data Dragon 에서 불러옴.

API
  GET  /api/meta                     팀·챔피언·Data Dragon 버전
  POST /api/new   {opp, t1_first, t1_blue}
  GET  /api/state
  POST /api/act   {cid, role?}       지금 차례에 c (T1 이든 상대든)
  POST /api/auto                     상대 차례 = M4 1순위
  POST /api/undo                     직전 T1 행동 전으로
  GET  /api/rec                      T1 차례 추천 (몇 초)
  POST /api/next_set {winner}        피어리스 다음 세트
  GET  /api/replay_list              실제 경기 대조 목록 (m5_rec_check.py → replay.json)
  GET  /api/replay?gid=...           실제 경기 하나 (드래프트 · 픽 차례별 그 시점 추천 · 실제 승패)
"""
from __future__ import annotations

import json
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src" / "pipeline"))
import m5_draft as MD  # noqa: E402

LOCK = threading.Lock()
D = None
REPLAY_F = HERE.parent / "data" / "processed" / "2026" / "m5" / "rec_check" / "replay.json"
_replay = {"mtime": None, "games": []}


def replay_games():
    """실제 경기 대조 자료 (src/pipeline/m5_rec_check.py 가 만듦). 세트마다 그 전 자료로만 만든 M4·이길 가치 → 학습 밖."""
    if not REPLAY_F.exists():
        raise ValueError("replay.json 없음 — python src/pipeline/m5_rec_check.py 를 먼저 돌리세요")
    m = REPLAY_F.stat().st_mtime
    if _replay["mtime"] != m:
        _replay["games"] = json.loads(REPLAY_F.read_text(encoding="utf-8"))
        _replay["mtime"] = m
    return _replay["games"]


def replay_list():
    keys = ("gid", "set", "day", "n_game", "blue", "red", "fp", "winner", "p_elo_blue", "p_draft_blue", "miss")
    return [{k: g[k] for k in keys} for g in replay_games()]


def replay_one(gid):
    for g in replay_games():
        if g["gid"] == gid:
            return g
    raise ValueError(f"경기 없음: {gid}")


def meta():
    sim = D.sim
    ko = json.loads((HERE / "career" / "champ_ko.json").read_text(encoding="utf-8"))
    logos = json.loads((HERE / "career" / "team_logos.json").read_text(encoding="utf-8"))
    champs = []
    for c in sim.champs:
        sh = {r: sim.ds.role_share[r].get(c, 0) for r in MD.POS}
        tot = sum(sh.values()) or 1
        roles = [r for r, v in sorted(sh.items(), key=lambda kv: -kv[1]) if v / tot >= 0.1]
        champs.append({"id": c, "ko": sim.ko(c), "roles": roles})
    champs.sort(key=lambda x: x["ko"])
    teams = [{"name": t, "logo": logos.get(t, {}).get("url"), "elo": round(D.elo.get(t, 1500))} for t in [MD.US] + MD.LCK_TEAMS]
    return {"ddragon": ko.get("ddragon_version"), "champs": champs, "teams": teams, "us": MD.US,
            "pos": MD.POS, "pos_ko": MD.ROLE_KO, "day": "2026-08-26", "R": sim.mc}


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _api(self, fn):
        try:
            with LOCK:
                self._send(200, fn())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(400, {"error": str(e)})

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/draft.html"):
            return self._send(200, (HERE / "draft.html").read_bytes(), "text/html; charset=utf-8")
        if p.startswith("/fonts/"):
            f = HERE / "fonts" / Path(p).name
            if f.exists():
                return self._send(200, f.read_bytes(), "font/ttf")
        if p == "/api/meta":
            return self._api(meta)
        if p == "/api/state":
            return self._api(lambda: D.state() if D.opp else {"empty": True})
        if p == "/api/rec":
            return self._api(D.recommend)
        if p == "/api/replay_list":
            return self._api(replay_list)
        if p == "/api/replay":
            from urllib.parse import parse_qs, urlsplit
            gid = parse_qs(urlsplit(self.path).query).get("gid", [""])[0]
            return self._api(lambda: replay_one(gid))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        b = self._body()
        if p == "/api/new":
            return self._api(lambda: (D.new(b["opp"], bool(b.get("t1_first")), bool(b.get("t1_blue", True))), D.state())[1])
        if p == "/api/act":
            return self._api(lambda: (D.act(b["cid"], b.get("role")), D.state())[1])
        if p == "/api/auto":
            return self._api(lambda: (D.auto(), D.state())[1])
        if p == "/api/undo":
            return self._api(lambda: (D.undo(), D.state())[1])
        if p == "/api/next_set":
            return self._api(lambda: (D.next_set(b["winner"]), D.state())[1])
        self._send(404, {"error": "not found"})


def main():
    global D
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
    D = MD.Draft()
    print(f"밴픽 대시보드: http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main()
