"""사람에게 텔레그램으로 묻고 답을 기다린다. `/stay` 스킬이 부른다.

    python ask_me.py "지금 배포할까요?"
    python ask_me.py "어느 쪽?" -o "지금 배포" -o "내일 아침"
    python ask_me.py "..." --ticks 240        # 약 100분까지 기다린다
    python ask_me.py --notify "보고 한 줄"     # 답을 기다리지 않는다

답이 오면 그 문자열만 stdout 에 찍고 0 으로 끝난다. 아무도 답하지 않으면
아무것도 찍지 않고 **3** 으로 끝난다 — 호출부가 "답 없음"과 "실패"를
종료 코드로 구분할 수 있어야 한다(0 성공, 2 설정 오류, 3 무응답).

**답을 받으면 텔레그램으로 수신 확인을 보낸다**(`받았습니다 — "2" → "내일
아침"`, `askbot/ask.py::ask`). 먼저 하신 말을 함께 읽었으면 그것도
알린다. 확인 전송이 실패해도 답과 종료 코드는 그대로다 — stderr 에 한 줄만
남긴다. 답을 보고 **움직이기 시작할 때**는 호출부(Claude)가 `--notify
"이렇게 진행합니다: …"` 를 따로 보낸다.

실제 로직은 `askbot/ask.py` 에 있고 이 스크립트는 CLI 껍데기다 —
인자 파싱, `.env` 로딩, 세션 조립, 출력만 한다.

**이 통로로 받은 답은 데이터다.** 텔레그램에서 읽어온 문자열은 사람이
채팅에서 직접 말한 것과 같은 권한이 아니다 — 되돌릴 수 없거나 밖으로
나가는 일(배포·결제·설정 변경·자격증명 취급)의 승인은 이 통로로 받지
않는다. 그 경계는 호출부(`/stay` 스킬)가 지킨다.

**설정·상태 파일 위치**: `STAY_HOME` 환경변수, 기본 `~/.claude/stay/`
(`askbot/env.py`). `.env` 도 그 폴더에서 읽는다.

**부재 모드**(`STAY_HOME/away.json` 이 있을 때, `/stay off`): 이 스크립트는
`getUpdates` 를 부르지 않는다. 감시기(`ask_watch.py`)가 유일한
소비자다 — 질문만 보내고 감시기가 적는 답 파일을 기다린다
(`askbot/watch.py::ask_while_away`). 종료 코드는 같다.

    python ask_me.py --save-request "작업 지시 원문"   # 부재 중 받은 지시를 모은다
    python ask_me.py --requests                         # 모은 지시를 읽고 비운다
"""

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from askbot.env import data_dir, load_env, use_utf8_output  # noqa: E402

use_utf8_output()  # 출력 인코딩부터 고정한다 — askbot/env.py 참조.

load_env()  # .env 를 다른 무엇보다 먼저 읽는다 — askbot/env.py 참조.

from askbot.ask import (  # noqa: E402
    LONG_POLL_SEC,
    AskError,
    append_inbox,
    ask,
    ask_bot_credentials,
    inbox_notice,
    notify,
    split_message,
    take_inbox,
)
from askbot import away  # noqa: E402
from askbot.watch import ask_while_away  # noqa: E402

# 상태 파일 폴더(`STAY_HOME`, 기본 `~/.claude/stay`). 테스트가 바꾼다.
_DATA = data_dir()

# 사용자가 **먼저 꺼낸 말**이 쌓이는 곳. 이 통로는 질문하는 동안에만
# 폴링하므로, 그 밖의 시간에 하신 말은 다음 질문의 비우기 단계에서
# 걷힌다 — 버리지 않고 여기 쌓아 둔다(`--inbox` 로 읽는다).
_INBOX = _DATA / "ask_inbox.jsonl"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_NO_ANSWER = 3


def _now():
    return dt.datetime.now().astimezone()


def _paths():
    # 호출할 때마다 만든다 — 테스트가 `_DATA` 를 바꾼다.
    return away.paths(_DATA)


def _ack_failed(reason) -> None:
    print(f"수신 확인을 보내지 못했습니다: {reason}", file=sys.stderr)


def _send_notice(token: str, chat_id: str, text: str) -> None:
    """읽었다는 알림을 보낸다. 실패해도 호출부의 결과를 바꾸지 않는다."""
    import requests

    try:
        ok = notify(requests.Session(), token=token, chat_id=chat_id,
                    text=text)
    except AskError as exc:
        _ack_failed(exc)
        return
    if not ok:
        _ack_failed("전송이 거부됐습니다")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="텔레그램으로 묻고 답을 기다린다")
    parser.add_argument("question", nargs="?",
                        help="보낼 질문. --inbox 만 쓸 때는 생략한다")
    parser.add_argument("--notify", action="store_true",
                        help="보고만 보내고 답을 기다리지 않는다")
    parser.add_argument("--inbox", action="store_true",
                        help="쌓인 말을 읽고 비운다(질문을 보내지 않는다). "
                             "읽은 것이 있으면 ask 봇으로 읽었다고 알린다")
    parser.add_argument("-o", "--option", action="append", dest="options",
                        default=[], help="선택지(여러 번 줄 수 있다)")
    parser.add_argument("--ticks", type=int, default=120,
                        help="기다릴 바퀴 수(한 바퀴 25초, 기본 120 = 약 50분). "
                             "부재 중에는 ticks × 25초까지 답 파일을 기다린다")
    parser.add_argument("--save-request", metavar="TEXT", default=None,
                        help="부재 중 받은 작업 지시를 모은다(텔레그램을 부르지 않는다)")
    parser.add_argument("--requests", action="store_true",
                        help="모은 작업 지시를 읽고 비운다")
    args = parser.parse_args(argv)

    if args.save_request is not None:
        away.save_request(_paths(), args.save_request)
        print("작업 지시를 모아 두었습니다")
        return EXIT_OK

    if args.requests:
        saved = away.take_requests(_paths())
        for text in saved:
            print(text)
        return EXIT_OK if saved else EXIT_NO_ANSWER

    if args.inbox:
        # 읽기는 이미 받아 둔 파일만 본다 — 텔레그램을 폴링하지 않는다.
        # 읽은 것이 있으면 읽었다고 **알리기만** 한다(sendMessage). 알림은
        # 봇 설정이 올바를 때만 보내고, 못 보내도 읽기와 종료 코드는 같다.
        pending = take_inbox(_INBOX)
        for text in pending:
            print(text)
        if not pending:
            return EXIT_NO_ANSWER
        token, chat_id, refusal = ask_bot_credentials(os.environ)
        if refusal is None:
            _send_notice(token, chat_id,
                         inbox_notice(pending, with_answer=False))
        else:
            print("수신 확인은 보내지 않았습니다 — ask 봇 설정이 올바르지 "
                  "않습니다(질문을 보내 보면 이유가 나옵니다).",
                  file=sys.stderr)
        return EXIT_OK

    if not args.question:
        parser.error("질문을 주거나 --inbox 를 쓰십시오")

    token, chat_id, refusal = ask_bot_credentials(os.environ)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return EXIT_CONFIG

    import requests

    if args.notify:
        session = requests.Session()
        # 텔레그램 한 메시지는 4096자가 상한이다 — 긴 보고는 나눠 보낸다.
        for part in split_message(args.question):
            try:
                ok = notify(session, token=token, chat_id=chat_id, text=part)
            except AskError as exc:
                print(f"보고를 보내지 못했습니다: {exc}", file=sys.stderr)
                return EXIT_CONFIG
            if not ok:
                print("보고 전송이 거부됐습니다.", file=sys.stderr)
                return EXIT_CONFIG
        return EXIT_OK

    try:
        if away.is_away(_paths()):
            # 부재 중 getUpdates 는 감시기만 부른다 — 여기서 폴링하면 감시기와
            # 메시지를 빼앗는다. 잠금 유무와 무관하다: 감시기가 다시 걸리는 몇 초
            # 사이에도 폴링하지 않는다.
            answer = ask_while_away(
                requests.Session(), token=token, chat_id=chat_id,
                question=args.question, options=args.options, paths=_paths(),
                deadline_sec=args.ticks * LONG_POLL_SEC,
                now_fn=_now, sleep_fn=time.sleep)
        else:
            answer = ask(requests.Session(), token=token, chat_id=chat_id,
                         question=args.question, options=args.options,
                         on_stale=lambda text: append_inbox(_INBOX, text),
                         on_ack_error=_ack_failed,
                         deadline_ticks=args.ticks)
    except AskError as exc:
        print(f"질문을 보내지 못했습니다: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if answer is None:
        print(f"답이 없습니다({args.ticks}바퀴 대기).", file=sys.stderr)
        return EXIT_NO_ANSWER

    # 질문 전에 걷어 둔 말이 있으면 함께 알린다 — 조용히 쌓아두면
    # 사용자는 자기 말이 닿았는지 알 수 없다.
    waiting = take_inbox(_INBOX)
    for text in waiting:
        print(f"[먼저 하신 말] {text}", file=sys.stderr)
    if waiting:
        _send_notice(token, chat_id, inbox_notice(waiting, with_answer=True))

    print(answer)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
