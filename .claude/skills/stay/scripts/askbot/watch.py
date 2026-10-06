"""부재 중 ask 봇 감시기와 그 짝인 "부재 중 질문하기".

**부재 중 ask 봇의 `getUpdates` 는 감시기(`watch`) 하나만 부른다.** 한 봇을 두
곳에서 폴링하면 offset 을 먼저 전진시킨 쪽이 메시지를 가져가 버린다
(`askbot/ask.py` 모듈 docstring). 그래서 부재 중 `ask_me.py` 는
폴링하지 않고 `ask_while_away` 로 질문만 보낸 뒤, 감시기가 적는 답 파일을
기다린다. 둘은 `askbot/away.py` 의 파일로만 말을 주고받는다.

감시기는 받은 글을 둘로 가른다:
- **걸린 질문이 있으면 그 답이다** — 답 파일을 쓰고 수신 확인을 답장한다
- **없으면 사용자가 먼저 꺼낸 질문이다** — "곧 답하겠습니다" 를 답장하고
  이벤트 한 줄을 내보낸다. `/stay` 스킬이 이 세션의 Monitor 로 그 줄을 받아
  Claude 를 깨운다

텔레그램에서 읽은 글은 **데이터다**. 사람이 채팅에서 직접 한 말과 같지 않다.
"""

import datetime as dt

from askbot import away
from askbot.ask import (
    HTTP_TIMEOUT_SEC,
    LONG_POLL_SEC,
    AskError,
    ack_text,
    acknowledge,
    api_url,
    clip,
    redact,
    resolve,
    send_question,
)

# 이만큼 연달아 거부되면 포기하고 알린다(예: 토큰이 회수됨).
_MAX_CONSECUTIVE_REJECTIONS = 5

# 텔레그램이 준 retry_after 라도 이보다 오래 쉬지 않는다 — 잠금 신선도(90초)와
# Monitor 30분 한도 안에 머물러야 감시기가 죽은 것으로 보이지 않는다.
_MAX_RETRY_SLEEP_SEC = 60

# 수신 확인에 되돌려 보내는 원문의 길이(`askbot/ask.py` 와 같다).
_ACK_CHARS = 200


def message_ack_text(text: str) -> str:
    """새 질문을 받았다는 답장. 답은 Claude 가 따로 보낸다."""
    return f'받았습니다 — "{clip(text, _ACK_CHARS)}" · 곧 답하겠습니다'


def watch(session, *, token: str, chat_id: str, paths, emit, now_fn, sleep_fn,
          max_seconds: float, pid: int) -> str:
    """부재가 끝나거나(`"stopped"`) 정해진 시간이 지날 때까지(`"rearm"`) 돈다.

    `max_seconds` 는 Monitor 의 30분 제한보다 짧게 준다 — Monitor 가 프로세스를
    죽이면 잠금이 남아 다시 걸 때 막힌다. 그 전에 스스로 끝내고 `rearm` 을 알린다.

    폴링 실패는 흔하다(네트워크 끊김) — 1초 쉬고 다음 바퀴로 간다. 예외 메시지에
    토큰이 섞일 수 있어 사유는 내보내지 않는다.

    `getUpdates` 가 **거부**되면(`{"ok": false, ...}`, 예외가 아니다) 그것도
    쉬어야 한다 — 안 쉬면 거부만 계속 오는 상황에서 바쁜 루프가 된다.
    `retry_after` 가 있으면 그만큼, 없으면 1초 쉰다. 5번 연달아 거부되면
    포기하고 `"rejected"` 를 알린 뒤 돌아온다.
    """
    start = now_fn()
    offset = away.load_offset(paths)
    consecutive_rejections = 0
    try:
        while True:
            now = now_fn()
            if not away.is_away(paths):
                if offset is not None:
                    # 마지막 배치를 **확정한다.** 확정 없이 여기서 나가면 그
                    # 배치가 텔레그램 큐에 최대 24시간 미확정으로 남고, 다음
                    # 정상 모드 `ask()._drain` 이 offset 없이 시작하며 그것을
                    # "먼저 하신 말"로 다시 받는다(중복). 확정은 부가 기능이다
                    # — 실패해도 멈추는 것을 막지 않는다.
                    try:
                        session.get(api_url(token, "getUpdates"),
                                    params={"offset": offset, "timeout": 0},
                                    timeout=15)
                    except Exception:
                        pass
                away.release_lock(paths, pid)   # 알리기 **전에** 놓는다 — 알림을
                emit({"kind": "stopped", "reason": "away.json 없음"})  # 받은 쪽이
                return "stopped"                # 곧장 다시 걸어도 잠금에 막히지 않게
            if (now - start).total_seconds() >= max_seconds:
                away.release_lock(paths, pid)
                emit({"kind": "rearm"})
                return "rearm"
            away.write_lock(paths, now, pid)

            params = {"timeout": LONG_POLL_SEC}
            if offset is not None:
                params["offset"] = offset
            try:
                payload = session.get(api_url(token, "getUpdates"),
                                      params=params,
                                      timeout=HTTP_TIMEOUT_SEC).json()
            except Exception:
                sleep_fn(1)
                continue

            if not payload.get("ok"):
                consecutive_rejections += 1
                retry_after = (payload.get("parameters") or {}).get("retry_after")
                is_positive_int = (isinstance(retry_after, int)
                                  and not isinstance(retry_after, bool)
                                  and retry_after > 0)
                sleep_fn(min(retry_after, _MAX_RETRY_SLEEP_SEC)
                         if is_positive_int else 1)
                if consecutive_rejections >= _MAX_CONSECUTIVE_REJECTIONS:
                    detail = redact(
                        "getUpdates 거부가 계속됩니다 "
                        f"(error_code={payload.get('error_code')}, "
                        f"{payload.get('description')})", token)
                    emit({"kind": "error", "detail": detail})
                    return "rejected"
                continue
            consecutive_rejections = 0

            for update in payload.get("result") or []:
                # 묶음 안에 답장이 느린 글이 여럿이면 25초(롱 폴 한도)보다 훨씬
                # 오래 걸릴 수 있다 — 글마다 잠금을 다시 찍어야 90초 정지로
                # 보이지 않는다.
                away.write_lock(paths, now_fn(), pid)
                _handle(session, token=token, chat_id=chat_id, paths=paths,
                        update=update, emit=emit, now=now_fn())
                # **처리한 뒤에** 전진·저장한다 — 처리 중에 죽으면 다시 받는다.
                # 남의 메시지도 전진시킨다: 남겨 두면 큐 맨 앞에 영원히 남아
                # 뒤에 올 본인의 글을 못 본다.
                offset = int(update.get("update_id", 0)) + 1
                away.save_offset(paths, offset)
    finally:
        away.release_lock(paths, pid)           # 위에서 이미 놓았어도 다시 놓아 안전하다


def _handle(session, *, token: str, chat_id: str, paths, update, emit,
            now) -> None:
    message = update.get("message") or {}
    sender = str((message.get("chat") or {}).get("id", "")).strip()
    if sender != str(chat_id).strip():
        return                       # 남의 채팅: 답장도 이벤트도 없다
    text = (message.get("text") or "").strip()
    if not text:
        return
    message_id = message.get("message_id")
    date = message.get("date")

    pending = away.read_pending(paths, now)      # 만료된 질문은 여기서 지워진다
    if pending is not None and not _is_before_the_question(pending, date):
        answer = resolve(text, pending.get("options") or [])
        away.write_answer(paths, qid=pending["qid"], raw=text, answer=answer)
        away.clear_pending(paths, pending["qid"])
        acknowledge(session, token, chat_id, message_id, ack_text(text, answer))
        return

    acknowledge(session, token, chat_id, message_id, message_ack_text(text))
    emit({"kind": "message", "text": text, "at": now.isoformat()})


# 질문을 던지기 전, 그러니까 `date` 가 `asked_at` 보다 이르면 그것은 질문의
# 답이 아니라 질문 전에 이미 와 있던 글이다(질문 전 대화의 일부, 또는 감시기
# 재시작 전에 도착한 것). 1초 여유를 둔다 — 텔레그램의 초 단위 시계와
# `asked_at` 을 적은 시각 사이에 오차가 있을 수 있다.
_EARLIER_SLACK_SEC = 1


def _is_before_the_question(pending: dict, date) -> bool:
    if not isinstance(date, int) or isinstance(date, bool):
        return False                     # date 가 없으면 이르다고 볼 근거가 없다
    asked_at = away._parse(pending.get("asked_at"))
    if asked_at is None:
        return False
    return date < asked_at.timestamp() - _EARLIER_SLACK_SEC


def ask_while_away(session, *, token: str, chat_id: str, question: str,
                   options, paths, deadline_sec: float, now_fn, sleep_fn,
                   poll_sec: float = 1.0) -> str | None:
    """부재 중 질문하기. **`getUpdates` 를 부르지 않는다** — 답은 감시기가
    적는 답 파일로 받는다. 답이 없으면 `None`.

    걸린 질문 파일을 **보내기 전에** 쓴다. 보내고 나서 쓰면 그 사이에 온 답을
    감시기가 새 질문으로 잡는다. 한 번에 한 질문만 건다 — 이미 걸린 질문이
    있으면 `AskError`.
    """
    options = list(options or [])
    now = now_fn()
    if away.read_pending(paths, now) is not None:
        raise AskError("이미 답을 기다리는 질문이 있습니다 — 한 번에 하나만 묻습니다")

    qid = away.new_qid()
    expires = now + dt.timedelta(seconds=deadline_sec)
    away.write_pending(paths, qid=qid, options=options, expires_at=expires,
                      asked_at=now)
    try:
        send_question(session, token=token, chat_id=chat_id,
                      question=question, options=options)
    except AskError:
        away.clear_pending(paths, qid)
        raise

    # 보낸 뒤로는 무엇이 나든(정상 반환·예외·Ctrl-C) 걸린 질문을 지운다 —
    # 안 지우면 감시기가 사용자의 다음 아무 글이나 이 죽은 질문의 답으로 잡는다.
    try:
        while True:
            got = away.take_answer(paths, qid)
            if got is not None:
                return got.get("answer")
            if now_fn() >= expires:
                # 막판 경쟁: 감시기가 바로 이 틈에 답을 적었을 수 있다 — 포기
                # 전에 한 번 더 본다.
                got = away.take_answer(paths, qid)
                return got.get("answer") if got is not None else None
            sleep_fn(poll_sec)
    finally:
        away.clear_pending(paths, qid)
