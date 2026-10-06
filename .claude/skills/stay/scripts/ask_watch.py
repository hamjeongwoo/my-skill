"""부재 중 ask 봇 감시기. `/stay` 스킬이 부른다(`/stay off` 가 켜고 `/stay on` 이 끈다).

    python ask_watch.py --on          # 부재 모드 켜기(STAY_HOME/away.json)
    python ask_watch.py --off         # 끄기(감시기도 곧 스스로 멈춘다)
    python ask_watch.py               # 감시(Monitor 로 건다)
    python ask_watch.py --takeover    # 죽은 감시기의 잠금을 무시하고 감시

감시하는 동안 stdout 한 줄이 이벤트 하나다(JSON):
`message`(사용자가 먼저 꺼낸 글) · `rearm`(29분이 지나 스스로 끝남 — 다시 건다) ·
`stopped`(부재 모드가 꺼짐) · `error`(예상 못 한 예외이거나 `getUpdates` 거부가
5번 연달아 옴, 종료 1).

종료 코드: 0 정상 · 1 예외 또는 `getUpdates` 거부 반복 ·
2 설정 오류(봇 설정, 부재 아님, 이미 도는 감시기).

상태 파일은 `STAY_HOME`(기본 `~/.claude/stay/`)에 있다 — 프로젝트 폴더가 아니라서
어느 폴더·worktree 에서 돌려도 `ask_me.py` 와 같은 파일을 본다(`askbot/env.py`).
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from askbot.env import data_dir, load_env, use_utf8_output  # noqa: E402

use_utf8_output()  # 출력 인코딩부터 고정한다 — askbot/env.py 참조.

load_env()  # .env 를 다른 무엇보다 먼저 읽는다 — askbot/env.py 참조.

from askbot import away  # noqa: E402
from askbot.ask import ask_bot_credentials, redact  # noqa: E402
from askbot.watch import watch  # noqa: E402

# 상태 파일 폴더(`STAY_HOME`, 기본 `~/.claude/stay`). 테스트가 바꾼다.
_DATA = data_dir()

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2


def _now():
    return dt.datetime.now().astimezone()


def _emit(event) -> None:
    """이벤트 한 줄. `ensure_ascii=False` 여도 json.dumps 는 줄바꿈을 `\\n` 으로
    적으므로 글 안의 줄바꿈이 이벤트를 쪼개지 않는다."""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="부재 중 ask 봇 감시기")
    parser.add_argument("--on", action="store_true", help="부재 모드를 켠다")
    parser.add_argument("--off", action="store_true", help="부재 모드를 끈다")
    parser.add_argument("--takeover", action="store_true",
                        help="살아 있어 보이는 잠금을 무시한다(죽은 감시기 복구용)")
    parser.add_argument("--max-minutes", type=float, default=29,
                        help="이 시간이 지나면 스스로 끝내고 rearm 을 알린다 "
                             "(Monitor 30분 제한보다 짧게)")
    args = parser.parse_args(argv)
    p = away.paths(_DATA)

    if args.on:
        away.set_away(p, _now())
        print(f"부재 모드를 켰습니다 ({p.away})")
        return EXIT_OK
    if args.off:
        away.clear_away(p)
        print("부재 모드를 껐습니다")
        return EXIT_OK

    token, chat_id, refusal = ask_bot_credentials(os.environ)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return EXIT_CONFIG
    if not away.is_away(p):
        print("부재 모드가 아닙니다 — 먼저 --on 을 하십시오. 부재가 아닐 때 "
              "폴링하면 ask_me.py 와 같은 봇을 두고 메시지를 빼앗습니다.",
              file=sys.stderr)
        return EXIT_CONFIG
    if away.lock_is_fresh(p, _now()) and not args.takeover:
        print("감시기가 이미 돌고 있습니다(잠금이 90초 안에 갱신됨). "
              "확실히 죽었으면 --takeover 로 다시 거십시오.", file=sys.stderr)
        return EXIT_CONFIG

    import requests

    try:
        reason = watch(requests.Session(), token=token, chat_id=chat_id, paths=p,
                       emit=_emit, now_fn=_now, sleep_fn=time.sleep,
                       max_seconds=args.max_minutes * 60, pid=os.getpid())
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as exc:
        _emit({"kind": "error",
               "detail": redact(f"{type(exc).__name__}: {exc}", token)})
        return EXIT_FAILED
    return EXIT_FAILED if reason == "rejected" else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
