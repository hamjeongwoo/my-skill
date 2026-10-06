"""테스트가 `askbot` 패키지와 `ask_me.py`·`ask_watch.py` 를 import 할 수 있게
스크립트 폴더를 sys.path 에 넣는다. 실제 `STAY_HOME` 을 보지 않도록 임시 폴더로 돌린다."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _isolated_stay_home(tmp_path, monkeypatch):
    monkeypatch.setenv("STAY_HOME", str(tmp_path / "stay-home"))
