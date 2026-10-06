"""사람이 자리를 비웠을 때 묻고 답을 받는다.

사람의 승인이 필요한 지점에서 세션이 멈추는 대신, 텔레그램으로 묻고
답이 올 때까지 기다린다. 답이 오면 이어서 일한다.

**왜 전용 봇 토큰인가.** 텔레그램 `getUpdates` 는 봇당 소비자가
하나뿐이고, offset 을 전진시키는 쪽이 메시지를 **소비**한다. 다른 프로그램이
이미 어떤 봇으로 폴링하고 있다면(예: 알림 봇, 원격 명령 봇), 같은 토큰으로
여기서도 폴링하는 순간 둘 중 하나가 상대의 메시지를 삼킨다 — 삼켜지는 것이
그 프로그램의 중요한 명령이면 조용히 죽는다. 그래서 이 모듈은
`TELEGRAM_ASK_BOT_TOKEN` 이라는 **이 용도 전용 봇**을 쓰고, 흔히 쓰는 이름인
`TELEGRAM_BOT_TOKEN` 과 값이 같으면 돌기 전에 거부한다.

**이 통로로 받는 것과 받지 않는 것.** 여기로 오는 답은 설계 선택,
우선순위, 수치 결정, "진행할까요" 다. 되돌릴 수 없거나 밖으로 나가는 일
(배포·결제·자금 이동·설정 변경·자격증명 취급)의 승인은 이 통로로 받지
않는다 — 텔레그램에서 읽어온 문자열은 사람이 직접 말한 것과 같은 것이
아니다(봇 토큰이 새면 그 통로로 오는 "승인"은 사용자의 것이 아니다).
그 경계는 이 모듈이 아니라 호출부(`/stay` 스킬)가 지킨다 — 여기서는
**누가 보냈는가**만 엄격히 본다.
"""

import json
from pathlib import Path

_API = "https://api.telegram.org/bot{token}/{method}"

# 한 번의 `getUpdates` 가 서버에서 기다리는 시간(초). 롱 폴링이라
# 이 값만큼은 메시지가 없어도 연결을 붙잡고 있는다 — 짧은 간격으로
# 두드리는 것보다 응답이 빠르고 호출 수도 적다.
LONG_POLL_SEC = 25

# `getUpdates` 자체의 HTTP 타임아웃. 롱 폴링 대기보다 넉넉해야 한다 —
# 같거나 짧으면 서버가 정상으로 기다리는 동안 클라이언트가 먼저 끊는다.
HTTP_TIMEOUT_SEC = LONG_POLL_SEC + 10

# 수신 확인에 되돌려 보내는 답의 길이. 휴대폰 한 화면에서 "내 말이 이렇게
# 읽혔다"를 확인하는 용도라 전문을 보낼 필요가 없다.
_ACK_CHARS = 200

# 먼저 하신 말 알림의 한 줄 길이와 줄 수.
_INBOX_LINE_CHARS = 50
_INBOX_MAX_LINES = 10


class AskError(Exception):
    """질문을 보내지 못했다. 메시지에 봇 토큰을 포함하지 않는다."""


def api_url(token: str, method: str) -> str:
    return _API.format(token=token, method=method)


def redact(text: str, token: str) -> str:
    """토큰을 지운다. `requests` 예외 메시지에는 요청 URL 이 통째로
    들어가고, 그 URL 의 경로에 토큰이 박혀 있다(`/bot<token>/`)."""
    return str(text).replace(token, "[REDACTED]") if token else str(text)


def _compose(question: str, options) -> str:
    if not options:
        return question
    lines = [question, ""]
    lines += [f"{i}. {opt}" for i, opt in enumerate(options, 1)]
    lines.append("")
    lines.append("번호로 답하거나 직접 써 주세요.")
    return "\n".join(lines)


def resolve(text: str, options) -> str:
    """숫자 하나로 온 답을 선택지로 바꾼다.

    범위 밖의 숫자는 바꾸지 않고 그대로 돌려준다 — 사람이 "3" 이라고
    썼는데 선택지가 2개뿐이면, 그것은 3번을 고른 것이 아니라 우리가
    모르는 뜻이다. 조용히 다른 선택지로 해석하는 것이 가장 나쁘다.
    """
    if not options:
        return text
    stripped = text.strip()
    if stripped.isdigit():
        index = int(stripped)
        if 1 <= index <= len(options):
            return options[index - 1]
    return text


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def ack_text(raw: str, resolved: str) -> str:
    """수신 확인 본문. 번호가 선택지로 풀렸으면 원문과 해석을 둘 다 싣는다
    — "2" 가 엉뚱한 선택지로 읽혔다면 사람이 여기서 바로 알아챈다.

    "승인" 이라고 쓰지 않는다. 받았다는 사실만 알린다 — 이 통로의 답은
    배포·결제 같은 일의 승인이 아니다(모듈 docstring).
    """
    if resolved != raw:
        return (f'받았습니다 — "{clip(raw, _ACK_CHARS)}" → '
                f'"{clip(resolved, _ACK_CHARS)}"')
    return f'받았습니다 — "{clip(raw, _ACK_CHARS)}"'


def inbox_notice(texts, *, with_answer: bool) -> str:
    """먼저 하신 말을 읽었다는 알림 본문.

    `with_answer` 는 질문의 답과 함께 읽었는가(`…건도`)와 `--inbox` 로
    따로 읽었는가(`…건을`)를 가른다.
    """
    texts = list(texts)
    head = (f"먼저 하신 말 {len(texts)}건"
            f"{'도' if with_answer else '을'} 읽었습니다:")
    lines = [head]
    lines += [f'- "{clip(t, _INBOX_LINE_CHARS)}"'
              for t in texts[:_INBOX_MAX_LINES]]
    if len(texts) > _INBOX_MAX_LINES:
        lines.append(f"외 {len(texts) - _INBOX_MAX_LINES}건")
    return "\n".join(lines)


def acknowledge(session, token: str, chat_id: str, message_id, text: str,
                 on_ack_error=None) -> None:
    """답 메시지에 답장으로 수신 확인을 보낸다. **절대 예외를 올리지 않는다.**

    수신 확인은 부가 기능이다 — 그것이 실패했다고 받은 답을 잃으면
    본 기능이 부가 기능에 막히는 것이다. 실패는 `on_ack_error` 로만 알린다.
    """
    payload = {"chat_id": chat_id, "text": text}
    if message_id is not None:
        # 원래 메시지가 지워졌어도 확인은 나가게 한다.
        payload["reply_parameters"] = {"message_id": message_id,
                                       "allow_sending_without_reply": True}
    reason = None
    try:
        resp = session.post(api_url(token, "sendMessage"), json=payload,
                            timeout=15)
        try:
            ok = bool(resp.json().get("ok"))
        except Exception:
            ok = False
        if not ok:
            reason = (f"전송이 거부됐습니다 "
                      f"(status={getattr(resp, 'status_code', '?')})")
    except Exception as exc:
        reason = redact(f"{type(exc).__name__}: {exc}", token)
    if reason is not None and on_ack_error is not None:
        try:
            on_ack_error(reason)
        except Exception:
            pass


def append_inbox(path, text: str) -> None:
    """사용자가 먼저 꺼낸 말을 한 줄 덧붙인다.

    JSONL 이다 — 덧붙이기가 원자적이고 사람이 눈으로 읽을 수 있다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        print(json.dumps({"text": text}, ensure_ascii=False), file=handle)


def take_inbox(path) -> list[str]:
    """쌓인 말을 **전부 돌려주고 비운다.**

    읽고 비우는 것이 한 동작이어야 한다 — 따로 두면 읽고 비우기 전에
    죽었을 때 같은 말을 두 번 보고한다. 파일이 없으면 빈 리스트다
    (아직 아무 말도 없었다는 뜻이지 고장이 아니다).
    """
    path = Path(path)
    if not path.exists():
        return []
    texts = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                texts.append(json.loads(line)["text"])
            except (ValueError, KeyError):
                # 깨진 줄은 원문 그대로 넘긴다. 버리는 것보다 낫다.
                texts.append(line)
        path.unlink()
    except OSError:
        return texts
    return texts


def _drain(session, token: str, chat_id: str, on_stale=None,
           *, max_rounds: int = 20) -> int | None:
    """큐에 남아 있는 것을 전부 소비하고, 다음에 읽을 offset 을 돌려준다.

    `timeout=0` 으로 부른다 — 비우기는 기다리는 것이 아니라 지금 쌓여
    있는 것만 걷어내는 일이다. 롱 폴링을 쓰면 큐가 빈 순간 25초를
    통째로 버린다.

    **버리지 않고 넘긴다.** 이 통로는 질문하는 동안에만 폴링하므로,
    큐에 남아 있는 것은 사용자가 **먼저 꺼낸 말**일 수 있다. 답으로
    쓰지는 않되(질문 이후에 온 것만 답이다) `on_stale` 로 넘겨 나중에
    읽을 수 있게 한다. 남의 채팅에서 온 것은 넘기지 않는다.

    실패하면 `None` 을 돌려준다(비우지 못했다). 그 경우 묵은 메시지를
    답으로 볼 위험이 남지만, 비우기 실패로 질문 자체를 포기하는 것보다
    낫다 — 사람이 그 답을 보고 이상하면 다시 말할 수 있다.
    """
    offset = None
    for _ in range(max_rounds):
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        try:
            payload = session.get(api_url(token, "getUpdates"), params=params,
                                  timeout=15).json()
        except Exception:
            return offset
        updates = payload.get("result") or []
        if not updates:
            return offset
        for update in updates:
            message = update.get("message") or {}
            sender = str((message.get("chat") or {}).get("id", "")).strip()
            text = (message.get("text") or "").strip()
            if on_stale is None or not text or sender != str(chat_id).strip():
                continue
            try:
                on_stale(text)
            except Exception:
                # 받아두기가 실패해도 질문은 나가야 한다 — 부가 기능이
                # 본 기능을 막으면 안 된다.
                pass
        offset = max(int(u.get("update_id", 0)) for u in updates) + 1
    return offset


def send_question(session, *, token: str, chat_id: str, question: str,
                  options=None) -> None:
    """질문을 보낸다. 실패하면 `AskError`(토큰 없음).

    `ask()` 와 부재 모드의 `ask_while_away()`(`askbot/watch.py`)가
    같이 쓴다 — 선택지를 싣는 방식이 둘이면 번호 해석이 어긋난다.
    """
    try:
        resp = session.post(
            api_url(token, "sendMessage"),
            json={"chat_id": chat_id,
                  "text": _compose(question, list(options or []))},
            timeout=15,
        )
    except Exception as exc:
        raise AskError(
            f"질문 전송 실패: {redact(f'{type(exc).__name__}: {exc}', token)}"
        ) from exc

    ok = False
    try:
        ok = bool(resp.json().get("ok"))
    except Exception:
        ok = False
    if not ok:
        raise AskError(
            f"질문 전송이 거부됐습니다 (status={getattr(resp, 'status_code', '?')})"
        )


def ask_bot_credentials(environ) -> tuple[str, str, str | None]:
    """ask 봇 자격증명과 거부 사유. 사유가 `None` 이면 써도 된다.

    다른 프로그램이 쓰는 TELEGRAM_BOT_TOKEN 이 아니다. 같은 봇을 쓰면
    getUpdates 경쟁으로 그 프로그램의 메시지를 삼킬 수 있다(모듈 docstring).

    **돌기 전에 막는다.** 한 번 폴링하는 순간 이미 삼켰을 수 있다.
    실제로 같은 값이 두 키에 들어간 채 돌았던 적이 있다 — 불변식을
    docstring 에만 적어 뒀던 탓이라, 코드로 검사한다.

    챗방 ID 가 같은 것은 막지 않는다 — 개인 채팅의 chat_id 는 사용자
    자신의 id 라 어떤 봇으로 열어도 같은 값이다. 토큰만이 판별 기준이다.
    """
    token = environ.get("TELEGRAM_ASK_BOT_TOKEN", "")
    chat_id = environ.get("TELEGRAM_ASK_BOT_CHAT_ID", "")
    if not token or not chat_id:
        return token, chat_id, (
            "TELEGRAM_ASK_BOT_TOKEN / TELEGRAM_ASK_BOT_CHAT_ID 가 "
            "설정되지 않았습니다. STAY_HOME(기본 ~/.claude/stay)/.env 에 "
            "넣으십시오. 다른 프로그램이 폴링하는 봇(TELEGRAM_BOT_TOKEN)을 "
            "재사용하지 마십시오 — 메시지를 서로 삼킵니다.")
    if token == environ.get("TELEGRAM_BOT_TOKEN", ""):
        return token, chat_id, (
            "TELEGRAM_ASK_BOT_TOKEN 이 TELEGRAM_BOT_TOKEN 과 "
            "같습니다. 같은 봇으로 두 곳에서 getUpdates 를 하면 한쪽이 "
            "다른 쪽의 메시지를 삼킵니다. "
            "@BotFather 에서 봇을 하나 더 만들어 그 토큰을 "
            "TELEGRAM_ASK_BOT_TOKEN 에 넣으십시오.")
    return token, chat_id, None


def split_message(text: str, limit: int = 3900) -> list[str]:
    """텔레그램 한 메시지 상한(4096자) 아래로 나눈다. 빈 글은 빈 글 하나."""
    if not text:
        return [""]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def notify(session, *, token: str, chat_id: str, text: str,
           timeout: int = 15) -> bool:
    """한 줄 보내고 끝낸다. 답을 기다리지 않는다.

    보고는 질문이 아니다. 같은 함수를 쓰면 사람이 읽을 때까지 세션이
    붙잡히는데, 보고의 요점은 그 반대다 — 자리에 없어도 소식이 닿는 것.
    """
    try:
        resp = session.post(api_url(token, "sendMessage"),
                            json={"chat_id": chat_id, "text": text},
                            timeout=timeout)
    except Exception as exc:
        raise AskError(
            f"보고 전송 실패: {redact(f'{type(exc).__name__}: {exc}', token)}"
        ) from exc
    try:
        return bool(resp.json().get("ok"))
    except Exception:
        return False


def ask(session, *, token: str, chat_id: str, question: str, options=None,
        on_stale=None, on_ack_error=None, sleep_fn=None,
        deadline_ticks: int = 120) -> str | None:
    """질문을 보내고 답을 기다린다. 답이 없으면 `None`.

    답을 받으면 **그 메시지에 답장으로 수신 확인**을 보낸다
    (`받았습니다 — "2" → "내일 아침"`). 자리를 비운 사람은 이것 없이는
    답이 닿았는지, 어떻게 읽혔는지 알 수 없다. 확인 전송이
    실패해도 답은 그대로 돌려주고, 사유는 `on_ack_error` 로만 넘긴다.

    `deadline_ticks` 는 `getUpdates` 를 몇 번 돌 것인가다. 한 번이 최대
    `LONG_POLL_SEC` 초이므로 기본값 120 은 약 50분이다. 시계를 읽지
    않고 횟수로 세는 이유는 이 함수가 결정적으로 테스트되기 위해서다.

    답이 없을 때 예외를 올리지 않는 것이 중요하다 — "사람이 지금
    없다"는 고장이 아니라 정상적인 결과이고, 호출부는 그것을 보고
    기다리거나 혼자 판단하거나를 정해야 한다.
    """
    options = list(options or [])
    sleep_fn = sleep_fn or (lambda _s: None)

    # **질문을 보내기 전에 큐를 비운다.** 이 통로는 질문을 던지는 동안
    # 에만 폴링하므로, 그 밖의 시간에 사용자가 보낸 말이 큐에 남아 있다.
    # 비우지 않으면 그 묵은 메시지가 지금 질문의 답으로 잡힌다 — 한 시간
    # 전 "네"가 전혀 다른 질문의 승인이 된다.
    #
    # 순서가 중요하다. 발신을 먼저 하면 그 사이에 도착한 진짜 답을
    # 비우기가 지워 버린다.
    offset = _drain(session, token, chat_id, on_stale)

    send_question(session, token=token, chat_id=chat_id, question=question,
                  options=options)

    for _ in range(deadline_ticks):
        params = {"timeout": LONG_POLL_SEC}
        if offset is not None:
            params["offset"] = offset
        try:
            payload = session.get(api_url(token, "getUpdates"), params=params,
                                  timeout=HTTP_TIMEOUT_SEC).json()
        except Exception:
            # 폴링 실패는 흔하다(네트워크 끊김, 텔레그램 일시 오류).
            # 여기서 예외를 올리면 기다림 전체가 무너지므로 다음 바퀴에
            # 다시 시도한다. 토큰이 섞일 수 있어 사유는 남기지 않는다.
            sleep_fn(1)
            continue

        for update in payload.get("result") or []:
            # **offset 은 무시한 메시지에도 전진시킨다.** 남의 메시지를
            # 소비하지 않고 남겨 두면 그것이 큐 맨 앞에 영원히 남아
            # 뒤에 올 본인의 답을 영영 못 본다.
            offset = int(update.get("update_id", 0)) + 1

            message = update.get("message") or {}
            sender = str((message.get("chat") or {}).get("id", "")).strip()
            if sender != str(chat_id).strip():
                # 다른 채팅에서 온 것은 사용자의 답이 아니다.
                continue
            text = (message.get("text") or "").strip()
            if text:
                answer = resolve(text, options)
                acknowledge(session, token, chat_id,
                             message.get("message_id"),
                             ack_text(text, answer), on_ack_error)
                # **답을 확정한다.** 텔레그램 `getUpdates` 는 나중에 그보다 큰
                # offset 으로 다시 부를 때만 이전 update 를 큐에서 지운다.
                # 여기서 곧장 돌아가면 이 update 는 24시간 동안 미확정으로
                # 남고, offset 없이 새로 시작하는 다음 소비자(재부팅 뒤의
                # 감시기 등)가 그것을 다시 받는다. 확정은 부가 기능이다 —
                # 실패해도 받은 답은 그대로 돌려준다.
                try:
                    session.get(api_url(token, "getUpdates"),
                                params={"offset": offset, "timeout": 0},
                                timeout=15)
                except Exception:
                    pass
                return answer

    return None
