"""부재 모드(`/stay off|on`)의 상태 파일.

감시기(`ask_watch.py`), `ask_me.py`, `/stay` 스킬이 **파일로만**
말을 주고받는다. 부재 중 ask 봇의 `getUpdates` 는 감시기 하나만 부르고,
`ask_me.py` 는 질문을 보낸 뒤 감시기가 적는 답 파일을 기다린다 — 한 봇을 두 곳에서
폴링하면 서로의 메시지를 삼킨다(`askbot/ask.py` 모듈 docstring).

모든 파일은 `STAY_HOME`(기본 `~/.claude/stay/`)에 있다(`askbot/env.py`). 쓰기는
임시 파일 뒤 `os.replace` — 반쯤 쓴 JSON 을 상대가 읽지 않게 한다.
"""

import datetime as dt
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from askbot.ask import append_inbox, take_inbox

# 감시기는 롱 폴링 한 바퀴(최대 25초 + 여유)마다 잠금의 beat 를 갱신한다.
# 이보다 오래 갱신이 없으면 죽은 것으로 본다.
LOCK_STALE_SEC = 90


@dataclass(frozen=True)
class AwayPaths:
    root: Path

    @property
    def away(self) -> Path:
        return self.root / "away.json"

    @property
    def lock(self) -> Path:
        return self.root / "ask_watch.lock"

    @property
    def offset(self) -> Path:
        return self.root / "ask_watch.offset"

    @property
    def pending(self) -> Path:
        return self.root / "ask_pending.json"

    @property
    def requests(self) -> Path:
        return self.root / "away_requests.jsonl"

    def answer(self, qid: str) -> Path:
        return self.root / f"ask_answer-{qid}.json"


def paths(data_dir) -> AwayPaths:
    return AwayPaths(Path(data_dir))


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    _retry_locked(lambda: os.replace(tmp, path))


def _unlink(path: Path) -> None:
    _retry_locked(lambda: path.unlink(missing_ok=True))


def _retry_locked(action) -> None:
    """Windows 는 다른 프로세스가 그 순간 파일을 열고 있으면 교체·삭제를
    거부한다(`PermissionError`, WinError 32). 감시기와 `ask_me.py` 는 별개
    프로세스라 실제로 겹칠 수 있다 — 잠깐 뒤 다시 한다."""
    for attempt in range(5):
        try:
            action()
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse(value):
    """ISO 시각. 형식이 틀렸거나 시간대가 없으면 `None`."""
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


# --- 부재 표시 --------------------------------------------------------


def set_away(p: AwayPaths, now) -> None:
    """부재를 켠다. offset 도 지운다 — 지난 부재의 offset 이 일주일 묵어
    있으면 텔레그램이 새로 매기는 update_id 가 그보다 작을 수 있고, 그러면
    `getUpdates` 가 그 offset 미만을 전부 걸러내 새 메시지가 조용히
    사라진다. 감시기 재시작(같은 부재가 켜진 채)은 이 함수를 다시 부르지
    않으므로 offset 이 지워지지 않는다."""
    _write_json(p.away, {"since": now.isoformat()})
    _unlink(p.offset)


def is_away(p: AwayPaths) -> bool:
    return p.away.exists()


def clear_away(p: AwayPaths) -> None:
    """부재를 끈다. 감시기 잠금과 걸린 질문도 지운다 — 남은 잠금이 다음
    `on` 을 막지 않게, 남은 질문이 다음 부재의 첫 메시지를 이미 끝난
    질문의 답으로 가로채지 않게."""
    _unlink(p.away)
    _unlink(p.lock)
    _unlink(p.pending)


# --- 감시기 잠금 --------------------------------------------------------


def write_lock(p: AwayPaths, now, pid: int) -> None:
    _write_json(p.lock, {"pid": pid, "beat": now.isoformat()})


def lock_is_fresh(p: AwayPaths, now) -> bool:
    data = _read_json(p.lock)
    beat = _parse(data.get("beat")) if isinstance(data, dict) else None
    return (beat is not None
            and abs((now - beat).total_seconds()) <= LOCK_STALE_SEC)


def release_lock(p: AwayPaths, pid: int) -> None:
    """자기 잠금일 때만 지운다 — `--takeover` 로 넘겨준 잠금을 지우지 않게."""
    data = _read_json(p.lock)
    if isinstance(data, dict) and data.get("pid") == pid:
        _unlink(p.lock)


# --- getUpdates offset --------------------------------------------------


def load_offset(p: AwayPaths) -> int | None:
    data = _read_json(p.offset)
    return data if isinstance(data, int) and not isinstance(data, bool) else None


def save_offset(p: AwayPaths, offset: int) -> None:
    _write_json(p.offset, int(offset))


# --- 걸린 질문과 답 -----------------------------------------------------


def new_qid() -> str:
    return uuid.uuid4().hex[:12]


def write_pending(p: AwayPaths, *, qid: str, options, expires_at,
                  asked_at=None) -> None:
    """`asked_at` 은 질문을 던진 시각이다(주면 ISO 로 적는다). 그보다 먼저
    온 글은 이 질문의 답이 아니다 — 질문 전 대화의 일부이거나 감시기
    재시작 전에 이미 와 있던 글이다(`askbot/watch.py::_handle`)."""
    data = {"qid": qid, "options": list(options),
            "expires_at": expires_at.isoformat()}
    if asked_at is not None:
        data["asked_at"] = asked_at.isoformat()
    _write_json(p.pending, data)


def read_pending(p: AwayPaths, now) -> dict | None:
    """만료 전인 걸린 질문. 만료됐거나 깨진 파일은 **지우고** `None`.

    남겨 두면 다음에 온 글이 이미 포기한 질문의 답으로 잡힌다.
    """
    data = _read_json(p.pending)
    if data is None:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("qid"), str):
        _unlink(p.pending)
        return None
    expires = _parse(data.get("expires_at"))
    if expires is None or now >= expires:
        clear_pending(p, data["qid"])
        return None
    return data


def clear_pending(p: AwayPaths, qid) -> None:
    data = _read_json(p.pending)
    if isinstance(data, dict) and data.get("qid") == qid:
        _unlink(p.pending)


def write_answer(p: AwayPaths, *, qid: str, raw: str, answer: str) -> None:
    _write_json(p.answer(qid), {"qid": qid, "raw": raw, "answer": answer})


def take_answer(p: AwayPaths, qid: str) -> dict | None:
    path = p.answer(qid)
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    _unlink(path)
    return data


# --- 부재 중 받은 작업 지시 --------------------------------------------


def save_request(p: AwayPaths, text: str) -> None:
    append_inbox(p.requests, text)


def take_requests(p: AwayPaths) -> list[str]:
    return take_inbox(p.requests)
