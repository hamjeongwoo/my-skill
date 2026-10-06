"""`askbot/ask.py`·`ask_me.py` 테스트 — 사람이 자리를 비웠을 때 묻고 답을 받는다.

**왜 전용 봇인가.** 텔레그램 `getUpdates` 는 봇당 소비자가 하나뿐이다.
다른 프로그램이 이미 `TELEGRAM_BOT_TOKEN` 으로 폴링하고 있다면, 같은 토큰으로
여기서 또 폴링하면 **둘 중 하나가 상대의 메시지를 삼킨다.** 그래서 이 모듈은
`TELEGRAM_ASK_BOT_TOKEN` 을 쓰고, 두 값이 같으면 돌기 전에 거부한다.

**답변은 지시가 아니라 데이터다.** 이 통로로 오는 것은 설계 선택과
진행 여부이지, 되돌릴 수 없거나 밖으로 나가는 일(배포·결제·설정 변경)의
승인이 아니다. 그 경계는 이 모듈이 아니라 호출부(사람과 대화하는 쪽)가
지킨다 — 여기서는 "누가 보냈는가"만 엄격히 본다.
"""

import pathlib

import pytest

from askbot import ask


TOKEN = "1234567890:" + "A" * 35
CHAT = "987654321"


class _Resp:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("JSON 이 아닙니다")
        return self._payload


class _Session:
    """텔레그램 `getUpdates` 의 **큐 의미**를 흉내낸다.

    단순히 준비된 응답을 차례로 뱉는 가짜로는 이 모듈을 검증할 수 없다.
    실제 API 는 offset 을 받아 "그 번호 이상"만 돌려주고, offset 을
    전진시키는 것이 곧 소비다. 비우기(`_drain`)가 옳게 도는지 보려면
    그 의미가 가짜에도 있어야 한다.

    `later` 는 **질문이 나간 뒤에** 도착하는 메시지다 — 사람이 질문을
    보고 답하는 상황이 그것이다.
    """

    def __init__(self, updates=None, later=None, send_ok=True):
        self.queue = list(updates or [])
        self.later = list(later or [])
        self.send_ok = send_ok
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        # 질문이 나갔다 — 이제 사람의 답이 도착한다.
        self.queue.extend(self.later)
        self.later = []
        return _Resp({"ok": self.send_ok, "result": {"message_id": 1}})

    def get(self, url, **kwargs):
        self.gets.append({"url": url, **kwargs})
        offset = (kwargs.get("params") or {}).get("offset")
        if offset is not None:
            # offset 미만은 소비된 것으로 본다(실제 API 와 같다).
            self.queue = [u for u in self.queue
                          if int(u["update_id"]) >= int(offset)]
        return _Resp({"ok": True, "result": list(self.queue)})


def _update(text, chat_id=CHAT, update_id=100, message_id=5):
    message = {"text": text, "chat": {"id": int(chat_id)}}
    if message_id is not None:
        message["message_id"] = message_id
    return {"update_id": update_id, "message": message}


# --- 발신 -------------------------------------------------------------


def test_question_is_sent_to_the_configured_chat():
    session = _Session(later=[_update("네")])

    ask.ask(session, token=TOKEN, chat_id=CHAT, question="배포할까요?",
            sleep_fn=lambda s: None, deadline_ticks=3)

    post = session.posts[0]
    assert "sendMessage" in post["url"]
    assert post["json"]["chat_id"] == CHAT
    assert "배포할까요?" in post["json"]["text"]


def test_options_are_listed_in_the_message():
    """선택지가 있으면 본문에 번호로 싣는다 — 휴대폰에서 길게 쓰지 않고
    숫자 하나로 답할 수 있어야 한다."""
    session = _Session(later=[_update("1")])

    ask.ask(session, token=TOKEN, chat_id=CHAT, question="어느 쪽?",
            options=["지금 배포", "내일 아침"],
            sleep_fn=lambda s: None, deadline_ticks=3)

    text = session.posts[0]["json"]["text"]
    assert "1" in text and "지금 배포" in text
    assert "2" in text and "내일 아침" in text


def test_a_numeric_reply_is_resolved_to_the_option():
    session = _Session(later=[_update("2")])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="어느 쪽?",
                     options=["지금 배포", "내일 아침"],
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "내일 아침"


def test_free_text_reply_is_returned_as_is():
    session = _Session(later=[_update("둘 다 말고 다음 주에")])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="어느 쪽?",
                     options=["지금", "내일"],
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "둘 다 말고 다음 주에"


# --- 발신자 검증 -------------------------------------------------------


def test_replies_from_other_chats_are_ignored():
    """다른 채팅에서 온 메시지는 사용자의 답이 아니다. 이 검사가 없으면
    봇을 아는 누구나 이 시스템의 결정에 답할 수 있다."""
    session = _Session(later=[
        _update("남이 보낸 승인", chat_id="111111111", update_id=100),
        _update("본인 답", chat_id=CHAT, update_id=101),
    ])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     sleep_fn=lambda s: None, deadline_ticks=5)

    assert answer == "본인 답"


def test_offset_advances_past_messages_we_ignore():
    """`getUpdates` 는 offset 으로 소비를 확정한다. 남의 메시지를
    건너뛰기만 하고 offset 을 전진시키지 않으면, 그것이 큐 맨 앞에
    영원히 남아 뒤에 올 본인의 답을 영영 못 본다."""
    session = _Session(later=[_update("남", chat_id="111111111", update_id=500)])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer is None                      # 남의 말은 답이 아니다
    assert session.gets[-1]["params"]["offset"] == 501   # 그래도 지나갔다


# --- 묵은 메시지 ---------------------------------------------------------
#
# 이 통로는 질문을 던지는 동안에만 폴링한다. 그 밖의 시간에 사용자가
# 보낸 말은 큐에 남아 있다가 **다음 질문의 답으로 잡힌다** — 한 시간 전
# "네"가 전혀 다른 질문의 승인이 되는 것이다. 질문을 보내기 전에 큐를
# 비워, **질문 이후에 온 것만** 답으로 친다.


def test_messages_sent_before_the_question_are_not_taken_as_the_answer():
    session = _Session(updates=[_update("한 시간 전에 한 말", update_id=10)],
                       later=[_update("질문에 대한 진짜 답", update_id=11)])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "질문에 대한 진짜 답"


def test_the_queue_is_drained_before_the_question_is_sent():
    """비우기가 **발신보다 먼저**여야 한다. 질문을 먼저 보내면 그 사이에
    도착한 답을 비우기가 지워 버린다."""
    session = _Session(updates=[_update("묵은 것", update_id=7)])

    ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
            sleep_fn=lambda s: None, deadline_ticks=2)

    # 첫 동작은 getUpdates(비우기)이고, sendMessage 는 그 뒤다.
    assert session.gets, "비우기 없이 바로 질문을 보냈다"
    assert session.gets[0]["params"]["timeout"] == 0, "비우기는 롱 폴링이 아니다"


# --- 시간 초과 ---------------------------------------------------------


def test_returns_none_when_nobody_answers():
    """답이 없으면 None. 예외가 아니다 — 호출부는 "사람이 지금 없다"를
    정상적인 결과로 다뤄야 한다."""
    session = _Session(updates=[])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer is None
    # 비우기 1회 + 본 폴링 3회.
    assert len(session.gets) == 4


# --- 비밀 ---------------------------------------------------------------


def test_the_token_never_appears_in_an_error():
    session = _Session(send_ok=False)

    with pytest.raises(ask.AskError) as exc:
        ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                sleep_fn=lambda s: None, deadline_ticks=1)

    assert TOKEN not in str(exc.value)


def test_transport_failure_never_leaks_the_token():
    class _Boom(_Session):
        def post(self, url, **kwargs):
            raise RuntimeError(f"요청 실패 {url}")

    with pytest.raises(ask.AskError) as exc:
        ask.ask(_Boom(), token=TOKEN, chat_id=CHAT, question="?",
                sleep_fn=lambda s: None, deadline_ticks=1)

    assert TOKEN not in str(exc.value)


# --- inbox: 먼저 하신 말을 버리지 않는다 --------------------------------
#
# 비우기는 묵은 답이 엉뚱한 질문에 붙는 것을 막지만, 그대로 두면 **사용자가
# 먼저 꺼낸 말까지 같이 버린다.** 이 통로는 질문하는 동안에만 폴링하므로
# 그 밖의 시간에 하신 말은 전부 그 경로로 사라진다.
#
# 버리지 말고 받아둔다. 답으로 쓰지는 않되(질문 이후에 온 것만 답이다)
# 나중에 읽을 수 있게 남긴다.


def test_stale_messages_from_the_user_are_handed_over_not_dropped():
    seen = []
    session = _Session(updates=[_update("아까 하려던 말", update_id=10)],
                       later=[_update("지금 질문의 답", update_id=11)])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     on_stale=seen.append,
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "지금 질문의 답"
    assert seen == ["아까 하려던 말"]


def test_stale_messages_from_other_chats_are_not_kept():
    """남이 보낸 것은 사용자의 말이 아니다. inbox 에도 넣지 않는다."""
    seen = []
    session = _Session(updates=[_update("남", chat_id="111111111", update_id=10)],
                       later=[_update("답", update_id=11)])

    ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
            on_stale=seen.append, sleep_fn=lambda s: None, deadline_ticks=3)

    assert seen == []


def test_a_failing_inbox_does_not_break_the_question():
    """받아두기가 실패해도 질문은 나가야 한다 — 부가 기능이 본 기능을
    막으면 안 된다."""
    def boom(_text):
        raise OSError("디스크 가득참")

    session = _Session(updates=[_update("아까 말", update_id=10)],
                       later=[_update("답", update_id=11)])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     on_stale=boom, sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "답"


def test_inbox_file_round_trip(tmp_path):
    path = tmp_path / "inbox.jsonl"

    ask.append_inbox(path, "첫 번째")
    ask.append_inbox(path, "두 번째")

    assert ask.take_inbox(path) == ["첫 번째", "두 번째"]
    # 한 번 읽으면 비워진다 — 같은 말을 매번 다시 보고하지 않는다.
    assert ask.take_inbox(path) == []


def test_take_inbox_on_a_missing_file_is_empty():
    assert ask.take_inbox(pathlib.Path("없는경로") / "inbox.jsonl") == []


# --- 수신 확인: 답을 읽었다고 알린다 -----------------------------------
#
# 사용자는 자리를 비운 채 휴대폰으로 답한다. 답이 닿았는지, "1" 이 어느
# 선택지로 읽혔는지 알 길이 없으면 같은 답을 다시 보내거나, 잘못 읽힌 것을
# 모른 채 넘어간다. 답을 받는 순간 그 메시지에 답장으로
# 원문과 해석을 되돌려 보낸다.


def test_the_answer_is_acknowledged_as_a_reply():
    session = _Session(later=[_update("다음 주에", update_id=11, message_id=42)])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "다음 주에"
    assert len(session.posts) == 2, "질문 뒤에 수신 확인이 나가야 한다"
    ack = session.posts[1]
    assert "sendMessage" in ack["url"]
    assert ack["json"]["chat_id"] == CHAT
    assert ack["json"]["text"] == '받았습니다 — "다음 주에"'
    assert ack["json"]["reply_parameters"] == {
        "message_id": 42, "allow_sending_without_reply": True}


def test_a_numeric_answer_is_acknowledged_with_raw_and_resolved():
    """번호 답은 원문과 해석을 둘 다 보인다 — 잘못 읽혔으면 사람이 바로 안다."""
    session = _Session(later=[_update("2")])

    ask.ask(session, token=TOKEN, chat_id=CHAT, question="어느 쪽?",
            options=["지금 배포", "내일 아침"],
            sleep_fn=lambda s: None, deadline_ticks=3)

    assert session.posts[1]["json"]["text"] == '받았습니다 — "2" → "내일 아침"'


def test_a_long_answer_is_clipped_in_the_acknowledgement():
    session = _Session(later=[_update("가" * 300)])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "가" * 300               # 답 자체는 자르지 않는다
    assert session.posts[1]["json"]["text"] == '받았습니다 — "' + "가" * 200 + '…"'


def test_an_answer_of_exactly_the_limit_is_not_clipped():
    session = _Session(later=[_update("가" * 200)])

    ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
            sleep_fn=lambda s: None, deadline_ticks=3)

    assert session.posts[1]["json"]["text"] == '받았습니다 — "' + "가" * 200 + '"'


def test_an_answer_without_message_id_is_acknowledged_without_reply():
    session = _Session(later=[_update("네", message_id=None)])

    ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
            sleep_fn=lambda s: None, deadline_ticks=3)

    ack = session.posts[1]["json"]
    assert ack["text"] == '받았습니다 — "네"'
    assert "reply_parameters" not in ack


def test_messages_from_other_chats_are_not_acknowledged():
    """남의 메시지에 답장하면 그 사람에게 이 봇이 살아 있다고 알리는 셈이다."""
    session = _Session(later=[
        _update("남", chat_id="111111111", update_id=100),
        _update("본인 답", update_id=101),
    ])

    ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
            sleep_fn=lambda s: None, deadline_ticks=3)

    assert len(session.posts) == 2
    assert session.posts[1]["json"]["chat_id"] == CHAT
    assert session.posts[1]["json"]["text"] == '받았습니다 — "본인 답"'


# --- 확정: 답을 받으면 텔레그램 큐에서도 지운다 --------------------------
#
# `getUpdates` 는 나중에 그보다 큰 offset 으로 다시 부를 때만 이전 update 를
# 큐에서 지운다(확정한다). 답을 받고 나서 곧장 멈추면 그 update 가 24시간
# 동안 확정되지 않은 채 남고, offset 없이 새로 시작하는 다음 소비자(예:
# 재부팅 뒤의 감시기)가 그것을 다시 받는다.


def test_the_answer_is_confirmed_with_a_zero_timeout_getupdates():
    session = _Session(later=[_update("네", update_id=55)])

    ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
            sleep_fn=lambda s: None, deadline_ticks=3)

    confirm = session.gets[-1]
    assert confirm["params"] == {"offset": 56, "timeout": 0}
    assert confirm["timeout"] == 15


class _ConfirmBoom(_Session):
    """확정 호출만 실패한다(비우기·본 폴링은 정상)."""

    def get(self, url, **kwargs):
        params = kwargs.get("params") or {}
        if params.get("timeout") == 0 and self.posts:
            raise RuntimeError("확정 실패")
        return super().get(url, **kwargs)


def test_a_failing_confirmation_still_returns_the_answer():
    """확정은 부가 기능이다 — 실패해도 답은 그대로 돌려줘야 한다."""
    session = _ConfirmBoom(later=[_update("네")])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "네"


def test_nothing_is_acknowledged_when_nobody_answers():
    session = _Session(later=[_update("남", chat_id="111111111")])

    assert ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                   sleep_fn=lambda s: None, deadline_ticks=3) is None
    assert len(session.posts) == 1


class _AckFails(_Session):
    """질문은 나가고 **두 번째 발신(수신 확인)부터** 실패한다."""

    def __init__(self, *a, mode="raise", **kw):
        super().__init__(*a, **kw)
        self.mode = mode

    def post(self, url, **kwargs):
        if self.posts:
            self.posts.append({"url": url, **kwargs})
            if self.mode == "raise":
                raise RuntimeError(f"요청 실패 {url}")
            return _Resp({"ok": False})
        return super().post(url, **kwargs)


def test_a_failed_acknowledgement_still_returns_the_answer():
    """수신 확인은 부가 기능이다 — 그것 때문에 답을 잃으면 안 된다."""
    errors = []
    session = _AckFails(later=[_update("답")])

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     on_ack_error=errors.append,
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "답"
    assert len(errors) == 1
    assert TOKEN not in errors[0]              # 사유에 토큰을 흘리지 않는다
    assert "RuntimeError" in errors[0]


def test_a_rejected_acknowledgement_still_returns_the_answer():
    errors = []
    session = _AckFails(later=[_update("답")], mode="reject")

    answer = ask.ask(session, token=TOKEN, chat_id=CHAT, question="?",
                     on_ack_error=errors.append,
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "답"
    assert len(errors) == 1


def test_a_failing_ack_error_callback_is_swallowed():
    def boom(_reason):
        raise OSError("stderr 닫힘")

    answer = ask.ask(_AckFails(later=[_update("답")]), token=TOKEN,
                     chat_id=CHAT, question="?", on_ack_error=boom,
                     sleep_fn=lambda s: None, deadline_ticks=3)

    assert answer == "답"


def test_inbox_notice_lists_what_was_read():
    assert ask.inbox_notice(["아까 한 말", "또 한 말"], with_answer=True) == (
        '먼저 하신 말 2건도 읽었습니다:\n- "아까 한 말"\n- "또 한 말"')
    assert ask.inbox_notice(["하나"], with_answer=False) == (
        '먼저 하신 말 1건을 읽었습니다:\n- "하나"')


def test_inbox_notice_is_capped():
    texts = [f"{i}번 " + "나" * 60 for i in range(12)]

    lines = ask.inbox_notice(texts, with_answer=False).split("\n")

    assert lines[0] == "먼저 하신 말 12건을 읽었습니다:"
    assert len(lines) == 1 + 10 + 1
    assert lines[1] == '- "' + ("0번 " + "나" * 60)[:50] + '…"'
    assert lines[-1] == "외 2건"


# --- CLI 가드 ----------------------------------------------------------
#
# 실제로 당했던 일이다. `.env` 의 TELEGRAM_ASK_BOT_TOKEN 이 다른 프로그램의
# TELEGRAM_BOT_TOKEN 과 **같은 값**이었는데 스크립트가 그대로 돌아, 그
# 프로그램의 getUpdates 를 폴링했다. 그 사이에 온 명령을 이 스크립트가
# "답변"으로 가져가면 원래 프로그램은 영영 못 본다.
#
# 불변식을 docstring 에만 적어 두면 지켜지지 않는다. 검사로 옮긴다.
#
# **이 절의 모든 테스트는 `cli.ask` 를 반드시 가짜로 바꾼다.** 가드를
# 짜는 중에 같은 사고를 또 냈다: 가드가 없는 상태로 `main()` 을 부르니
# 진짜 `ask()` 가 진짜 텔레그램을 폴링했다. 테스트가 네트워크에 닿을 수
# 있으면 언젠가 닿는다.

import ask_me as cli  # noqa: E402


def _env(monkeypatch, **values):
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "TELEGRAM_ASK_BOT_TOKEN", "TELEGRAM_ASK_BOT_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _never_called(monkeypatch):
    """`ask` 가 불리면 테스트를 실패시킨다 — 네트워크 접근 금지."""
    def _boom(*a, **kw):
        raise AssertionError("가드가 막았어야 하는데 ask() 가 불렸다")
    monkeypatch.setattr(cli, "ask", _boom)


OTHER_TOKEN = "9999999999:" + "B" * 35


@pytest.fixture(autouse=True)
def _inbox(tmp_path, monkeypatch):
    """CLI 가 실제 `data/ask_inbox.jsonl` 을 읽고 비우지 않게 한다 — 테스트가
    사용자가 먼저 한 말을 삼키면 안 된다."""
    path = tmp_path / "ask_inbox.jsonl"
    monkeypatch.setattr(cli, "_INBOX", path)
    return path


def test_cli_refuses_when_the_ask_bot_is_another_programs_bot(monkeypatch, capsys):
    """같은 봇이면 돌기 전에 막는다 — 한 번 폴링하는 순간 이미 상대의
    메시지를 삼켰을 수 있다."""
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT,
         TELEGRAM_ASK_BOT_TOKEN=TOKEN, TELEGRAM_ASK_BOT_CHAT_ID=CHAT)
    _never_called(monkeypatch)

    code = cli.main(["질문"])

    assert code == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in err   # 왜 막았는지 말해야 한다
    assert TOKEN not in err        # 이유를 말하면서 토큰을 흘리면 안 된다


def test_cli_refuses_when_the_ask_token_is_missing(monkeypatch):
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT)
    _never_called(monkeypatch)

    assert cli.main(["질문"]) == cli.EXIT_CONFIG


def test_cli_allows_a_genuinely_separate_bot(monkeypatch):
    """토큰이 다르면 통과시킨다. **챗방 ID 가 같은 것은 정상이다** —
    개인 채팅의 chat_id 는 사용자 자신의 id 라서 어떤 봇으로 열어도 같은
    값이다. 문제가 되는 것은 토큰이 같은 경우뿐이다."""
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT,
         TELEGRAM_ASK_BOT_TOKEN=OTHER_TOKEN, TELEGRAM_ASK_BOT_CHAT_ID=CHAT)

    captured = {}

    def fake_ask(session, **kwargs):
        captured.update(kwargs)
        return "답"

    monkeypatch.setattr(cli, "ask", fake_ask)

    assert cli.main(["질문"]) == cli.EXIT_OK
    assert captured["token"] == OTHER_TOKEN


def test_cli_reports_no_answer_with_its_own_exit_code(monkeypatch):
    """무응답(3)과 설정 오류(2)를 구분한다 — 호출부가 "사람이 자리에
    없다"와 "설정이 틀렸다"에 다르게 반응해야 한다."""
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT,
         TELEGRAM_ASK_BOT_TOKEN=OTHER_TOKEN, TELEGRAM_ASK_BOT_CHAT_ID=CHAT)
    monkeypatch.setattr(cli, "ask", lambda session, **kw: None)

    assert cli.main(["질문"]) == cli.EXIT_NO_ANSWER


def test_notify_sends_without_waiting(monkeypatch):
    """보고는 답을 기다리지 않는다. 질문과 같은 함수를 쓰면 사람이
    읽을 때까지 세션이 붙잡힌다 — 보고의 요점은 그 반대다."""
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT,
         TELEGRAM_ASK_BOT_TOKEN=OTHER_TOKEN, TELEGRAM_ASK_BOT_CHAT_ID=CHAT)
    _never_called(monkeypatch)      # ask() 는 불리면 안 된다

    sent = []
    monkeypatch.setattr(cli, "notify",
                        lambda session, **kw: sent.append(kw) or True)

    assert cli.main(["--notify", "배포 끝났습니다"]) == cli.EXIT_OK
    assert sent[0]["text"] == "배포 끝났습니다"


def test_notify_still_refuses_the_trading_bot(monkeypatch):
    """알림이라고 가드를 비켜 가지 않는다 — 같은 봇이면 폴링은 안 해도
    사람이 그 방을 답변용으로 착각하게 된다."""
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT,
         TELEGRAM_ASK_BOT_TOKEN=TOKEN, TELEGRAM_ASK_BOT_CHAT_ID=CHAT)
    _never_called(monkeypatch)
    monkeypatch.setattr(cli, "notify",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("가드를 지나쳤다")))

    assert cli.main(["--notify", "x"]) == cli.EXIT_CONFIG


# --- CLI 수신 확인 ------------------------------------------------------


def _separate_bots(monkeypatch):
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT,
         TELEGRAM_ASK_BOT_TOKEN=OTHER_TOKEN, TELEGRAM_ASK_BOT_CHAT_ID=CHAT)


def _record_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "notify",
                        lambda session, **kw: sent.append(kw) or True)
    return sent


def _refuse_notify(monkeypatch, why):
    def _boom(*a, **kw):
        raise AssertionError(why)
    monkeypatch.setattr(cli, "notify", _boom)


def test_cli_acknowledges_earlier_messages_read_with_the_answer(
        monkeypatch, _inbox):
    _separate_bots(monkeypatch)
    ask.append_inbox(_inbox, "아까 한 말")
    monkeypatch.setattr(cli, "ask", lambda session, **kw: "답")
    sent = _record_notify(monkeypatch)

    assert cli.main(["질문"]) == cli.EXIT_OK

    assert len(sent) == 1
    assert sent[0]["token"] == OTHER_TOKEN
    assert sent[0]["text"] == '먼저 하신 말 1건도 읽었습니다:\n- "아까 한 말"'


def test_cli_sends_no_extra_notice_without_earlier_messages(monkeypatch):
    _separate_bots(monkeypatch)
    monkeypatch.setattr(cli, "ask", lambda session, **kw: "답")
    _refuse_notify(monkeypatch, "먼저 하신 말이 없는데 보냈다")

    assert cli.main(["질문"]) == cli.EXIT_OK


def test_cli_reports_a_failed_acknowledgement_on_stderr(monkeypatch, capsys):
    _separate_bots(monkeypatch)

    def fake_ask(session, **kw):
        kw["on_ack_error"]("RuntimeError: 끊김")
        return "답"

    monkeypatch.setattr(cli, "ask", fake_ask)

    assert cli.main(["질문"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.strip() == "답"
    assert "수신 확인을 보내지 못했습니다" in captured.err


def test_cli_a_failed_inbox_notice_is_not_fatal(monkeypatch, capsys, _inbox):
    _separate_bots(monkeypatch)
    ask.append_inbox(_inbox, "아까 한 말")
    monkeypatch.setattr(cli, "ask", lambda session, **kw: "답")

    def boom(session, **kw):
        raise cli.AskError("보고 전송 실패: 끊김")

    monkeypatch.setattr(cli, "notify", boom)

    assert cli.main(["질문"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.strip() == "답"
    assert "수신 확인을 보내지 못했습니다" in captured.err


def test_cli_inbox_acknowledges_what_it_read(monkeypatch, capsys, _inbox):
    _separate_bots(monkeypatch)
    ask.append_inbox(_inbox, "아까 한 말")
    sent = _record_notify(monkeypatch)

    assert cli.main(["--inbox"]) == cli.EXIT_OK

    assert capsys.readouterr().out.strip() == "아까 한 말"
    assert [kw["text"] for kw in sent] == [
        '먼저 하신 말 1건을 읽었습니다:\n- "아까 한 말"']
    assert sent[0]["token"] == OTHER_TOKEN


def test_cli_inbox_never_notifies_through_another_programs_bot(
        monkeypatch, capsys, _inbox):
    """같은 토큰이면 읽기는 하되 알리지 않는다 — 남의 봇으로 보내면 안 된다."""
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT,
         TELEGRAM_ASK_BOT_TOKEN=TOKEN, TELEGRAM_ASK_BOT_CHAT_ID=CHAT)
    ask.append_inbox(_inbox, "아까 한 말")
    _refuse_notify(monkeypatch, "남의 봇으로 보냈다")

    assert cli.main(["--inbox"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.strip() == "아까 한 말"
    assert "수신 확인" in captured.err
    assert TOKEN not in captured.err


def test_cli_inbox_without_an_ask_bot_still_reads(monkeypatch, capsys, _inbox):
    _env(monkeypatch)
    ask.append_inbox(_inbox, "아까 한 말")
    _refuse_notify(monkeypatch, "설정 없이 보냈다")

    assert cli.main(["--inbox"]) == cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "아까 한 말"


def test_cli_empty_inbox_sends_nothing(monkeypatch):
    _separate_bots(monkeypatch)
    _refuse_notify(monkeypatch, "읽은 것이 없는데 보냈다")

    assert cli.main(["--inbox"]) == cli.EXIT_NO_ANSWER


# --- 공개 API: 감시기와 함께 쓴다 ----------------------------------------


def test_ask_bot_credentials_accepts_a_separate_bot():
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ASK_BOT_TOKEN": OTHER_TOKEN,
           "TELEGRAM_ASK_BOT_CHAT_ID": CHAT}

    assert ask.ask_bot_credentials(env) == (OTHER_TOKEN, CHAT, None)


def test_ask_bot_credentials_refuses_the_trading_bot():
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ASK_BOT_TOKEN": TOKEN,
           "TELEGRAM_ASK_BOT_CHAT_ID": CHAT}

    _token, _chat, refusal = ask.ask_bot_credentials(env)

    assert refusal is not None and "TELEGRAM_BOT_TOKEN" in refusal
    assert TOKEN not in refusal


def test_ask_bot_credentials_refuses_missing_values():
    assert ask.ask_bot_credentials({})[2] is not None
    assert ask.ask_bot_credentials({"TELEGRAM_ASK_BOT_TOKEN": OTHER_TOKEN})[2] is not None


def test_split_message_keeps_every_character():
    text = "가" * 8000

    parts = ask.split_message(text)

    assert [len(p) for p in parts] == [3900, 3900, 200]
    assert "".join(parts) == text
    assert ask.split_message("가" * 3900) == ["가" * 3900]
    assert ask.split_message("") == [""]


def test_cli_notify_splits_a_long_report(monkeypatch):
    _separate_bots(monkeypatch)
    sent = []
    monkeypatch.setattr(cli, "notify",
                        lambda session, **kw: sent.append(kw["text"]) or True)

    assert cli.main(["--notify", "나" * 5000]) == cli.EXIT_OK
    assert [len(t) for t in sent] == [3900, 1100]


def test_cli_notify_stops_at_a_rejected_part(monkeypatch):
    _separate_bots(monkeypatch)
    sent = []
    monkeypatch.setattr(cli, "notify",
                        lambda session, **kw: sent.append(kw["text"]) and False)

    assert cli.main(["--notify", "나" * 5000]) == cli.EXIT_CONFIG
    assert len(sent) == 1


# --- 부재 모드 ----------------------------------------------------------
#
# `data/away.json` 이 있으면 `ask_me.py` 는 **폴링하지 않는다** — 감시기가 유일한
# 소비자다. 이 PC 가 실제로 부재 모드일 때 테스트를 돌려도 실제 `data/` 를 보지
# 않도록 `_DATA` 를 바꾼다.

import datetime as _dt  # noqa: E402

from askbot import away  # noqa: E402


@pytest.fixture(autouse=True)
def _data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setattr(cli, "_DATA", d)
    return d


def _go_away(data_dir):
    away.set_away(away.paths(data_dir), _dt.datetime.now().astimezone())


def _never_ask_directly(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("부재 중에 직접 폴링하는 ask() 를 불렀다")
    monkeypatch.setattr(cli, "ask", _boom)


def test_cli_while_away_asks_through_the_watcher(monkeypatch, capsys, _data):
    _separate_bots(monkeypatch)
    _go_away(_data)
    _never_ask_directly(monkeypatch)
    captured = {}

    def fake(session, **kw):
        captured.update(kw)
        return "내일"

    monkeypatch.setattr(cli, "ask_while_away", fake)

    code = cli.main(["어느 쪽?", "-o", "지금", "-o", "내일", "--ticks", "4"])

    assert code == cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "내일"
    assert captured["token"] == OTHER_TOKEN
    assert captured["question"] == "어느 쪽?"
    assert captured["options"] == ["지금", "내일"]
    assert captured["paths"] == away.paths(_data)
    assert captured["deadline_sec"] == 4 * 25


def test_cli_while_away_reports_no_answer(monkeypatch, _data):
    _separate_bots(monkeypatch)
    _go_away(_data)
    _never_ask_directly(monkeypatch)
    monkeypatch.setattr(cli, "ask_while_away", lambda session, **kw: None)

    assert cli.main(["질문"]) == cli.EXIT_NO_ANSWER


def test_cli_while_away_a_busy_question_is_a_config_error(monkeypatch, capsys, _data):
    _separate_bots(monkeypatch)
    _go_away(_data)
    _never_ask_directly(monkeypatch)

    def busy(session, **kw):
        raise cli.AskError("이미 답을 기다리는 질문이 있습니다")

    monkeypatch.setattr(cli, "ask_while_away", busy)

    assert cli.main(["질문"]) == cli.EXIT_CONFIG
    assert "이미 답을 기다리는" in capsys.readouterr().err


def test_cli_not_away_polls_directly(monkeypatch):
    _separate_bots(monkeypatch)

    def _boom(*a, **kw):
        raise AssertionError("부재가 아닌데 감시기 경로를 탔다")

    monkeypatch.setattr(cli, "ask_while_away", _boom)
    monkeypatch.setattr(cli, "ask", lambda session, **kw: "답")

    assert cli.main(["질문"]) == cli.EXIT_OK


def test_cli_saves_and_takes_requests_without_a_bot(monkeypatch, capsys):
    _env(monkeypatch)                        # 봇 설정 없이도 된다(텔레그램을 안 부른다)
    _refuse_notify(monkeypatch, "요청 저장에 텔레그램을 불렀다")

    assert cli.main(["--save-request", "배포해줘"]) == cli.EXIT_OK
    assert cli.main(["--save-request", "손절선 바꿔줘"]) == cli.EXIT_OK
    capsys.readouterr()

    assert cli.main(["--requests"]) == cli.EXIT_OK
    assert capsys.readouterr().out.splitlines() == ["배포해줘", "손절선 바꿔줘"]
    assert cli.main(["--requests"]) == cli.EXIT_NO_ANSWER
