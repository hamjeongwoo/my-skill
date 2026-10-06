"""프로세스 시작 준비 — 설정 폴더, `.env` 로딩, 출력 인코딩.

**설정과 상태 파일은 한 곳에 둔다.** 기본은 사용자 홈의 `~/.claude/stay/` 이고,
환경변수 `STAY_HOME` 으로 바꿀 수 있다. 프로젝트 폴더가 아니라 홈에 두는 이유:
git worktree 를 여러 개 쓰면 프로젝트 안의 `data/` 가 worktree 마다 갈라져
감시기(`ask_watch.py`)와 `ask_me.py` 가 서로의 파일을 못 본다. 홈은 하나뿐이다.

`.env` 는 두 곳에서 읽는다 — `STAY_HOME/.env` 먼저, 그다음 현재 폴더의 `.env`.
둘 다 `override=False` 라 쉘에서 export 한 값이 파일보다 우선하고, 먼저 읽은
파일이 나중 파일보다 우선한다. 파일이 없어도 예외를 내지 않는다.

모든 진입점(`ask_me.py`, `ask_watch.py`)이 `use_utf8_output()` 과 `load_env()` 를
가장 먼저 부른다.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ENV_HOME = "STAY_HOME"


def home_dir() -> Path:
    """설정(`.env`)과 상태 파일(`away.json` 등)이 사는 폴더."""
    override = os.environ.get(ENV_HOME, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "stay"


def data_dir() -> Path:
    """상태 파일 폴더. 지금은 `home_dir()` 와 같다 — 따로 둔 것은 호출부가
    설정과 상태를 구분해 읽을 수 있게 하기 위해서다."""
    return home_dir()


def load_env() -> None:
    """`STAY_HOME/.env` 와 현재 폴더의 `.env` 를 읽는다. 값은 어디에도 찍지 않는다."""
    load_dotenv(home_dir() / ".env")
    load_dotenv(Path.cwd() / ".env")


def use_utf8_output() -> None:
    """표준 출력·오류를 UTF-8 로 고정한다.

    이 프로그램의 출력은 전부 한국어다. 윈도우에서 출력을 파일로 리다이렉트하면
    파이썬이 cp949 를 골라 첫 줄에서 `UnicodeEncodeError` 로 죽는다. 콘솔에서
    직접 돌릴 때는 보이지 않다가 로그를 남기려는 순간 나타나는 문제라, 실행하는
    쪽이 `PYTHONIOENCODING` 을 기억하게 하지 않고 여기서 고정한다.

    `errors="replace"`: 어떤 환경에서 UTF-8 설정이 안 먹어도 글자 하나 때문에
    프로그램이 죽어선 안 된다.
    """
    for stream in (sys.stdout, sys.stderr):
        # pythonw 등에서는 None 일 수 있고, 테스트에서는 파이테스트의
        # 캡처 객체라 reconfigure 가 없을 수 있다. 둘 다 정상이다.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
