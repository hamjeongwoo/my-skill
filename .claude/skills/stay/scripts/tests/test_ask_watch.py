"""`askbot/watch.py`·`ask_watch.py` 테스트 — 부재 중 ask 봇 감시기.

부재 중 ask 봇의 `getUpdates` 는 **감시기 하나만** 부른다. 걸린 질문이 있으면
받은 글을 답 파일로 넘기고, 없으면 한 줄 JSON 이벤트로 Claude 를 깨운다.
여기서 틀리면 사용자의 질문이 조용히 사라지거나, 엉뚱한 글이 답이 된다.

모든 테스트는 가짜 세션과 `tmp_path` 만 쓴다 — 네트워크도 실제 `data/` 도 없다.
"""

import datetime as dt
import json

import pytest

from askbot import away
from askbot import watch as ask_watch
from askbot.ask import AskError

TOKEN = "1234567890:" + "A" * 35
OTHER_TOKEN = "9999999999:" + "B" * 35
CHAT = "987654321"
KST = dt.timezone(dt.timedelta(hours=9))
T0 = dt.datetime(2026, 9, 30, 20, 0, tzinfo=KST)


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class _Session:
    """`getUpdates` 가 부를 때마다 `batches` 에서 한 묶음씩 돌려준다.

    묶음이 떨어지면 `away.json` 을 지워 감시기를 끝낸다(사용자가 `off` 한 것과 같다).
    `fail_gets` 만큼은 먼저 예외를 낸다(네트워크 끊김).
    """

    def __init__(self, p, batches, fail_gets=0, post_ok=True):
        self.p = p
        self.batches = list(batches)
        self.fail_gets = fail_gets
        self.post_ok = post_ok
        self.gets = []
        self.posts = []

    def get(self, url, **kwargs):
        self.gets.append({"url": url, **kwargs})
        if self.fail_gets:
            self.fail_gets -= 1
            raise RuntimeError(f"끊김 {url}")
        if not self.batches:
            self.p.away.unlink(missing_ok=True)
            return _Resp({"ok": True, "result": []})
        item = self.batches.pop(0)
        if isinstance(item, dict):
            return _Resp(item)              # 거부 응답 등 완성된 페이로드 그대로
        return _Resp({"ok": True, "result": item})

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return _Resp({"ok": self.post_ok, "result": {"message_id": 9}})


def _update(text, chat_id=CHAT, update_id=100, message_id=5, date=None):
    message = {"message_id": message_id, "text": text,
               "chat": {"id": int(chat_id)}}
    if date is not None:
        message["date"] = date
    return {"update_id": update_id, "message": message}


@pytest.fixture
def p(tmp_path):
    paths = away.paths(tmp_path / "data")
    away.set_away(paths, T0)
    return paths


def _run(session, p, *, now_fn=lambda: T0, max_seconds=3600):
    events = []
    reason = ask_watch.watch(session, token=TOKEN, chat_id=CHAT, paths=p,
                             emit=events.append, now_fn=now_fn,
                             sleep_fn=lambda s: None, max_seconds=max_seconds,
                             pid=4242)
    return reason, events


def _messages(events):
    return [e for e in events if e["kind"] == "message"]


# --- 새 질문 -----------------------------------------------------------


def test_a_new_message_becomes_one_event_and_is_acknowledged(p):
    s = _Session(p, [[_update("오늘 손익 어때?", message_id=42)]])

    reason, events = _run(s, p)

    assert reason == "stopped"
    assert _messages(events) == [
        {"kind": "message", "text": "오늘 손익 어때?", "at": T0.isoformat()}]
    assert events[-1] == {"kind": "stopped", "reason": "away.json 없음"}
    ack = s.posts[0]["json"]
    assert ack["chat_id"] == CHAT
    assert ack["text"] == '받았습니다 — "오늘 손익 어때?" · 곧 답하겠습니다'
    assert ack["reply_parameters"]["message_id"] == 42


def test_it_long_polls(p):
    s = _Session(p, [[]])

    _run(s, p)

    assert s.gets[0]["params"]["timeout"] == 25


def test_messages_from_other_chats_are_ignored_but_consumed(p):
    s = _Session(p, [[_update("남", chat_id="111111111", update_id=500)]])

    _reason, events = _run(s, p)

    assert _messages(events) == []
    assert s.posts == []                      # 남에게 답장하지 않는다
    assert away.load_offset(p) == 501         # 그래도 지나간다


def test_offset_is_persisted_and_used_after_a_restart(p):
    """감시기 자체의 재시작(프로세스만 새로 뜬다, 부재는 계속 켜진 채)은
    offset 파일을 그대로 쓴다 — `rearm` 으로 끝나면 `set_away` 를 다시 부르지
    않는다."""
    clock = iter([T0 + dt.timedelta(seconds=10 * i) for i in range(1000)])
    s = _Session(p, [[_update("a", update_id=7)]] + [[] for _ in range(50)])

    reason, _events = _run(s, p, now_fn=lambda: next(clock), max_seconds=30)

    assert reason == "rearm"
    assert away.load_offset(p) == 8
    assert p.away.exists()                    # 부재 모드는 그대로 — set_away 를 다시 안 부른다

    s2 = _Session(p, [])
    _run(s2, p)

    assert s2.gets[0]["params"]["offset"] == 8


def test_re_arming_away_mode_resets_the_offset(p):
    """`set_away` 를 다시 부르는 것은 재시작이 아니라 **새 부재 시작**이다 —
    묵은 offset 이 새 update_id 를 걸러낼 수 있어(`askbot/away.py`)
    이번에는 지운다."""
    _run(_Session(p, [[_update("a", update_id=7)]]), p)
    assert away.load_offset(p) == 8

    away.set_away(p, T0)
    s2 = _Session(p, [])
    _run(s2, p)

    assert "offset" not in s2.gets[0]["params"]


def test_a_polling_failure_does_not_stop_the_watcher(p):
    s = _Session(p, [[_update("살아 있니")]], fail_gets=2)

    reason, events = _run(s, p)

    assert reason == "stopped"
    assert [e["text"] for e in _messages(events)] == ["살아 있니"]
    assert TOKEN not in json.dumps(events, ensure_ascii=False)


# --- getUpdates 거부 -----------------------------------------------------


_REJECTED = {"ok": False, "error_code": 401, "description": "Unauthorized"}


def test_five_consecutive_rejections_emit_one_error_and_return_rejected(p):
    s = _Session(p, [_REJECTED] * 5)
    sleeps = []
    events = []

    reason = ask_watch.watch(s, token=TOKEN, chat_id=CHAT, paths=p,
                             emit=events.append, now_fn=lambda: T0,
                             sleep_fn=sleeps.append, max_seconds=3600, pid=4242)

    assert reason == "rejected"
    errors = [e for e in events if e["kind"] == "error"]
    assert len(errors) == 1
    assert "error_code=401" in errors[0]["detail"]
    assert "Unauthorized" in errors[0]["detail"]
    assert TOKEN not in errors[0]["detail"]
    assert sleeps == [1, 1, 1, 1, 1]


def test_a_rejection_with_retry_after_sleeps_that_long(p):
    rejected = {"ok": False, "error_code": 429, "description": "Too Many Requests",
                "parameters": {"retry_after": 7}}
    s = _Session(p, [rejected])
    sleeps = []

    ask_watch.watch(s, token=TOKEN, chat_id=CHAT, paths=p, emit=lambda e: None,
                    now_fn=lambda: T0, sleep_fn=sleeps.append, max_seconds=3600,
                    pid=4242)

    assert sleeps[0] == 7


def test_a_huge_retry_after_is_capped(p):
    """retry_after 가 크면 쉬는 동안 잠금(90초)이 낡아 보이고 Monitor 30분도 넘긴다."""
    rejected = {"ok": False, "error_code": 429, "description": "Too Many Requests",
                "parameters": {"retry_after": 3600}}
    s = _Session(p, [rejected])
    sleeps = []

    ask_watch.watch(s, token=TOKEN, chat_id=CHAT, paths=p, emit=lambda e: None,
                    now_fn=lambda: T0, sleep_fn=sleeps.append, max_seconds=3600,
                    pid=4242)

    assert sleeps[0] == 60


def test_an_ok_response_between_rejections_resets_the_count(p):
    s = _Session(p, [_REJECTED, _REJECTED, [], _REJECTED, _REJECTED])
    events = []

    reason = ask_watch.watch(s, token=TOKEN, chat_id=CHAT, paths=p,
                             emit=events.append, now_fn=lambda: T0,
                             sleep_fn=lambda s: None, max_seconds=3600, pid=4242)

    assert reason == "stopped"                # 5번 연속이 아니므로 거부로 끝나지 않는다
    assert [e for e in events if e["kind"] == "error"] == []


# --- 걸린 질문의 답 -----------------------------------------------------


def test_an_answer_to_a_pending_question_goes_to_the_answer_file(p):
    away.write_pending(p, qid="q1", options=["지금", "내일"],
                       expires_at=T0 + dt.timedelta(minutes=5))
    s = _Session(p, [[_update("2")]])

    _reason, events = _run(s, p)

    assert _messages(events) == []            # 답은 이벤트가 아니다
    assert away.take_answer(p, "q1") == {"qid": "q1", "raw": "2", "answer": "내일"}
    assert not p.pending.exists()
    assert s.posts[0]["json"]["text"] == '받았습니다 — "2" → "내일"'


def test_only_the_first_message_answers_the_question(p):
    away.write_pending(p, qid="q1", options=["지금", "내일"],
                       expires_at=T0 + dt.timedelta(minutes=5))
    s = _Session(p, [[_update("1", update_id=100),
                      _update("그리고 하나 더", update_id=101)]])

    _reason, events = _run(s, p)

    assert away.take_answer(p, "q1")["answer"] == "지금"
    assert [e["text"] for e in _messages(events)] == ["그리고 하나 더"]


# --- 질문 전에 온 글은 답이 아니다 --------------------------------------
#
# 걸린 질문이 있어도, 텔레그램의 `date`(unix seconds) 가 질문을 던진 시각보다
# 이르면 그것은 질문에 대한 답이 아니라 그 전에 이미 와 있던 글이다(질문
# 전 대화의 일부, 또는 감시기 재시작 전에 도착한 것). 답으로 잡지 않고
# 새 메시지로 다뤄야 하고, 걸린 질문은 그대로 둬야 한다.


def test_a_message_dated_before_the_question_was_asked_is_a_new_message(p):
    away.write_pending(p, qid="q1", options=["지금", "내일"],
                       expires_at=T0 + dt.timedelta(minutes=5), asked_at=T0)
    earlier = int(T0.timestamp()) - 10
    s = _Session(p, [[_update("1", date=earlier)]])

    _reason, events = _run(s, p)

    assert [e["text"] for e in _messages(events)] == ["1"]
    assert away.read_pending(p, T0)["qid"] == "q1"   # 걸린 질문은 그대로
    assert away.take_answer(p, "q1") is None


def test_a_message_dated_after_the_question_was_asked_answers_it(p):
    away.write_pending(p, qid="q1", options=["지금", "내일"],
                       expires_at=T0 + dt.timedelta(minutes=5), asked_at=T0)
    later = int(T0.timestamp()) + 10
    s = _Session(p, [[_update("1", date=later)]])

    _reason, events = _run(s, p)

    assert _messages(events) == []
    assert away.take_answer(p, "q1") == {"qid": "q1", "raw": "1", "answer": "지금"}


def test_a_message_without_a_date_keeps_answering_the_pending_question(p):
    """`date` 가 없는 글은 기존 동작 그대로다 — 이르다고 볼 근거가 없다."""
    away.write_pending(p, qid="q1", options=["지금", "내일"],
                       expires_at=T0 + dt.timedelta(minutes=5), asked_at=T0)
    s = _Session(p, [[_update("1")]])

    _reason, events = _run(s, p)

    assert _messages(events) == []
    assert away.take_answer(p, "q1") == {"qid": "q1", "raw": "1", "answer": "지금"}


def test_an_expired_question_is_dropped_and_the_message_is_new(p):
    away.write_pending(p, qid="q1", options=[],
                       expires_at=T0 - dt.timedelta(seconds=1))
    s = _Session(p, [[_update("늦은 답")]])

    _reason, events = _run(s, p)

    assert [e["text"] for e in _messages(events)] == ["늦은 답"]
    assert away.take_answer(p, "q1") is None
    assert not p.pending.exists()


# --- 끝내기와 잠금 -------------------------------------------------------


def test_it_rearms_before_the_monitor_deadline_and_releases_the_lock(p):
    """Monitor 는 30분에 프로세스를 죽인다. 죽으면 잠금이 남아 다시 걸 때 막힌다 —
    그 전에 스스로 끝낸다."""
    clock = iter([T0 + dt.timedelta(seconds=10 * i) for i in range(1000)])
    s = _Session(p, [[] for _ in range(50)])

    reason, events = _run(s, p, now_fn=lambda: next(clock), max_seconds=30)

    assert reason == "rearm"
    assert events[-1] == {"kind": "rearm"}
    assert not p.lock.exists()
    assert p.away.exists()                    # 부재 모드는 그대로


def test_the_lock_is_released_before_rearm_is_emitted(p):
    """재무장을 감시하는 쪽(다시 거는 컨트롤러)이 이벤트를 받자마자 다시 걸 때,
    아직 신선한 잠금이 남아 있으면 자기가 건 감시기를 막아 버린다."""
    clock = iter([T0 + dt.timedelta(seconds=10 * i) for i in range(1000)])
    s = _Session(p, [[] for _ in range(50)])

    def emit(event):
        if event["kind"] == "rearm":
            assert not p.lock.exists()

    reason = ask_watch.watch(s, token=TOKEN, chat_id=CHAT, paths=p, emit=emit,
                             now_fn=lambda: next(clock), sleep_fn=lambda s: None,
                             max_seconds=30, pid=4242)

    assert reason == "rearm"


def test_the_lock_is_beaten_while_running_and_released_after(p):
    seen = []

    class S(_Session):
        def get(self, url, **kwargs):
            seen.append(json.loads(self.p.lock.read_text(encoding="utf-8")))
            return super().get(url, **kwargs)

    _run(S(p, [[]]), p)

    assert seen[0] == {"pid": 4242, "beat": T0.isoformat()}
    assert not p.lock.exists()


def test_the_lock_beat_is_refreshed_between_updates_in_a_batch(p):
    """묶음 안에 답장이 느린 글이 여럿이면 90초를 넘길 수 있다 — 글마다 잠금을
    다시 찍어야 감시기가 죽은 것으로 보이지 않는다."""
    ticks = iter(range(1000))

    def now_fn():
        return T0 + dt.timedelta(seconds=60 * next(ticks))

    class S(_Session):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.before_poll = None
            self.acks = []

        def get(self, url, **kwargs):
            if self.before_poll is None:          # 배치를 도는 첫 폴만 본다
                self.before_poll = json.loads(self.p.lock.read_text(encoding="utf-8"))
            return super().get(url, **kwargs)

        def post(self, url, **kwargs):
            self.acks.append(json.loads(self.p.lock.read_text(encoding="utf-8")))
            return super().post(url, **kwargs)

    updates = [_update(f"글{i}", update_id=300 + i) for i in range(3)]
    s = S(p, [updates])

    _run(s, p, now_fn=now_fn)

    assert len(s.acks) == 3
    before_poll_beat = dt.datetime.fromisoformat(s.before_poll["beat"])
    third_ack_beat = dt.datetime.fromisoformat(s.acks[2]["beat"])
    assert third_ack_beat > before_poll_beat


def test_the_last_offset_is_confirmed_on_stop(p):
    """`stopped` 로 나갈 때 마지막으로 저장한 offset 을 `timeout=0` 으로 한 번
    더 확정한다 — 안 하면 그 배치가 다음 정상 모드 `ask()._drain` 에 "먼저
    하신 말"로 다시 배달된다(중복)."""
    s = _Session(p, [[_update("글", update_id=77)]])

    reason, _events = _run(s, p)

    assert reason == "stopped"
    confirm = s.gets[-1]
    assert confirm["params"] == {"offset": 78, "timeout": 0}


def test_no_confirmation_when_nothing_was_ever_received(p):
    """offset 이 아직 `None` 이면(한 번도 못 받았으면) 확정할 것이 없다."""
    s = _Session(p, [[]])

    reason, _events = _run(s, p)

    assert reason == "stopped"
    assert all((g.get("params") or {}).get("timeout") != 0 for g in s.gets)


def test_it_stops_at_once_when_not_away(p):
    away.clear_away(p)
    s = _Session(p, [[_update("x")]])

    reason, events = _run(s, p)

    assert reason == "stopped"
    assert s.gets == []


# --- 부재 중 질문하기(ask_me.py 쪽) --------------------------------------


class _Sender:
    """발신만 한다. 부재 중 `ask_me.py` 는 `getUpdates` 를 부르면 안 된다."""

    def __init__(self, p, ok=True):
        self.p = p
        self.ok = ok
        self.posts = []

    def post(self, url, **kwargs):
        pending = json.loads(self.p.pending.read_text(encoding="utf-8")) if self.p.pending.exists() else None
        self.posts.append({"url": url, "pending_existed": self.p.pending.exists(),
                           "pending": pending,
                           **kwargs})
        return _Resp({"ok": self.ok})

    def get(self, url, **kwargs):
        raise AssertionError("부재 중에 getUpdates 를 불렀다")


def test_ask_while_away_writes_the_question_before_sending_and_returns_the_answer(p):
    s = _Sender(p)

    def sleep_fn(_sec):
        qid = away.read_pending(p, T0)["qid"]
        away.write_answer(p, qid=qid, raw="1", answer="지금")

    answer = ask_watch.ask_while_away(
        s, token=TOKEN, chat_id=CHAT, question="어느 쪽?", options=["지금", "내일"],
        paths=p, deadline_sec=60, now_fn=lambda: T0, sleep_fn=sleep_fn)

    assert answer == "지금"
    assert s.posts[0]["pending_existed"] is True   # 먼저 적고 보낸다
    # 질문 시각을 적어야 질문 전에 보낸 글을 답으로 착각하지 않는다.
    assert s.posts[0]["pending"]["asked_at"] == T0.isoformat()
    assert "1. 지금" in s.posts[0]["json"]["text"]
    assert not p.pending.exists()
    assert not list(p.root.glob("ask_answer-*"))


def test_ask_while_away_times_out_and_cleans_up(p):
    clock = iter([T0, T0 + dt.timedelta(seconds=30),
                  T0 + dt.timedelta(seconds=61)] + [T0 + dt.timedelta(seconds=99)] * 10)

    answer = ask_watch.ask_while_away(
        _Sender(p), token=TOKEN, chat_id=CHAT, question="?", options=[],
        paths=p, deadline_sec=60, now_fn=lambda: next(clock),
        sleep_fn=lambda s: None)

    assert answer is None
    assert not p.pending.exists()


def test_ask_while_away_refuses_a_second_question(p):
    away.write_pending(p, qid="other", options=[],
                       expires_at=T0 + dt.timedelta(minutes=5))
    s = _Sender(p)

    with pytest.raises(AskError):
        ask_watch.ask_while_away(s, token=TOKEN, chat_id=CHAT, question="?",
                                 options=[], paths=p, deadline_sec=60,
                                 now_fn=lambda: T0, sleep_fn=lambda s: None)

    assert s.posts == []
    assert away.read_pending(p, T0)["qid"] == "other"   # 남의 질문은 그대로


def test_ask_while_away_clears_its_question_when_sending_fails(p):
    with pytest.raises(AskError):
        ask_watch.ask_while_away(_Sender(p, ok=False), token=TOKEN, chat_id=CHAT,
                                 question="?", options=[], paths=p,
                                 deadline_sec=60, now_fn=lambda: T0,
                                 sleep_fn=lambda s: None)

    assert not p.pending.exists()


def test_ask_while_away_clears_pending_when_the_wait_is_interrupted(p):
    """기다리다 죽으면(예외·Ctrl-C) 걸린 질문을 지운다 — 안 지우면 감시기가
    사용자의 다음 아무 글이나 죽은 질문의 답으로 잡는다."""
    s = _Sender(p)

    def sleep_fn(_sec):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        ask_watch.ask_while_away(s, token=TOKEN, chat_id=CHAT, question="?",
                                 options=[], paths=p, deadline_sec=60,
                                 now_fn=lambda: T0, sleep_fn=sleep_fn)

    assert not p.pending.exists()


def test_ask_while_away_returns_an_answer_written_exactly_at_expiry(p):
    """감시기가 만료 확인 바로 그 틈에 답을 적는 경쟁을 흉내 낸다 — 마지막으로
    한 번 더 답 파일을 확인하지 않으면 답을 놓치고 `None` 을 돌려준다."""
    s = _Sender(p)
    calls = {"n": 0}

    def now_fn():
        calls["n"] += 1
        if calls["n"] == 1:
            return T0                              # 시작 시각(만료 계산용)
        qid = away.read_pending(p, T0)["qid"]
        away.write_answer(p, qid=qid, raw="1", answer="지금")
        return T0 + dt.timedelta(seconds=61)       # 만료 확인 시점

    answer = ask_watch.ask_while_away(
        s, token=TOKEN, chat_id=CHAT, question="?", options=["지금", "내일"],
        paths=p, deadline_sec=60, now_fn=now_fn, sleep_fn=lambda s: None)

    assert answer == "지금"
    assert not p.pending.exists()


# --- CLI ----------------------------------------------------------------
#
# **이 절의 모든 테스트는 `cli.watch` 를 가짜로 바꾸거나 기동 전에 막히는 경로만
# 탄다.** 진짜 `watch` 가 불리면 진짜 텔레그램을 폴링한다.

import ask_watch as cli  # noqa: E402


@pytest.fixture(autouse=True)
def _data(tmp_path, monkeypatch):
    d = tmp_path / "cli-data"
    monkeypatch.setattr(cli, "_DATA", d)
    return d


def _env(monkeypatch, **values):
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "TELEGRAM_ASK_BOT_TOKEN", "TELEGRAM_ASK_BOT_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _separate_bots(monkeypatch):
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT,
         TELEGRAM_ASK_BOT_TOKEN=OTHER_TOKEN, TELEGRAM_ASK_BOT_CHAT_ID=CHAT)


def _never_watch(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("기동 전에 막았어야 하는데 watch() 가 불렸다")
    monkeypatch.setattr(cli, "watch", _boom)


def _record_watch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "watch",
                        lambda session, **kw: calls.append(kw) or "stopped")
    return calls


def test_on_and_off(_data):
    assert cli.main(["--on"]) == cli.EXIT_OK
    assert (_data / "away.json").exists()

    assert cli.main(["--off"]) == cli.EXIT_OK
    assert not (_data / "away.json").exists()


def test_off_also_removes_a_left_over_lock(_data):
    p = away.paths(_data)
    away.set_away(p, T0)
    away.write_lock(p, T0, 1)

    cli.main(["--off"])

    assert not p.lock.exists()


def test_it_refuses_the_trading_bot(monkeypatch, capsys, _data):
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT,
         TELEGRAM_ASK_BOT_TOKEN=TOKEN, TELEGRAM_ASK_BOT_CHAT_ID=CHAT)
    away.set_away(away.paths(_data), T0)
    _never_watch(monkeypatch)

    assert cli.main([]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in err and TOKEN not in err


def test_it_refuses_to_poll_when_not_away(monkeypatch, capsys):
    """부재가 아닌데 폴링하면 `ask_me.py` 와 같은 봇을 두고 경쟁한다."""
    _separate_bots(monkeypatch)
    _never_watch(monkeypatch)

    assert cli.main([]) == cli.EXIT_CONFIG
    assert "부재" in capsys.readouterr().err


def test_a_live_lock_blocks_a_second_watcher(monkeypatch, _data):
    _separate_bots(monkeypatch)
    p = away.paths(_data)
    away.set_away(p, T0)
    away.write_lock(p, cli._now(), 1)
    _never_watch(monkeypatch)

    assert cli.main([]) == cli.EXIT_CONFIG


def test_takeover_starts_despite_a_live_lock(monkeypatch, _data):
    _separate_bots(monkeypatch)
    p = away.paths(_data)
    away.set_away(p, T0)
    away.write_lock(p, cli._now(), 1)
    calls = _record_watch(monkeypatch)

    assert cli.main(["--takeover"]) == cli.EXIT_OK
    assert len(calls) == 1


def test_a_stale_lock_does_not_block(monkeypatch, _data):
    _separate_bots(monkeypatch)
    p = away.paths(_data)
    away.set_away(p, T0)
    away.write_lock(p, cli._now() - dt.timedelta(seconds=200), 1)
    calls = _record_watch(monkeypatch)

    assert cli.main([]) == cli.EXIT_OK
    assert calls[0]["token"] == OTHER_TOKEN
    assert calls[0]["chat_id"] == CHAT
    assert calls[0]["paths"] == p
    assert calls[0]["max_seconds"] == 29 * 60


def test_max_minutes_is_passed_in_seconds(monkeypatch, _data):
    _separate_bots(monkeypatch)
    away.set_away(away.paths(_data), T0)
    calls = _record_watch(monkeypatch)

    cli.main(["--max-minutes", "1"])

    assert calls[0]["max_seconds"] == 60


def test_cli_fails_when_watch_returns_rejected(monkeypatch, _data):
    _separate_bots(monkeypatch)
    away.set_away(away.paths(_data), T0)
    monkeypatch.setattr(cli, "watch", lambda session, **kw: "rejected")

    assert cli.main([]) == cli.EXIT_FAILED


def test_an_unexpected_error_is_one_redacted_event(monkeypatch, capsys, _data):
    _separate_bots(monkeypatch)
    away.set_away(away.paths(_data), T0)

    def boom(session, **kw):
        raise RuntimeError(f"요청 실패 https://api.telegram.org/bot{OTHER_TOKEN}/getUpdates")

    monkeypatch.setattr(cli, "watch", boom)

    assert cli.main([]) == cli.EXIT_FAILED
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    event = json.loads(out)
    assert event["kind"] == "error"
    assert OTHER_TOKEN not in out


def test_emit_writes_one_line_per_event(capsys):
    """글 안의 줄바꿈이 이벤트를 쪼개면 Monitor 가 한 질문을 두 번 깨운다."""
    cli._emit({"kind": "message", "text": "첫 줄\n둘째 줄"})

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert json.loads(out)["text"] == "첫 줄\n둘째 줄"
