# stay 스킬 설정 — 설치, 텔레그램 봇 토큰과 chat id 받기

`/stay off`(부재 모드)는 텔레그램 봇으로 질문하고 답을 받는다. 그러려면 **파이썬 패키지 둘**과
**봇 토큰·chat id 두 값**이 필요하다.

## 0. 설치

프로그램은 이 스킬 폴더의 `scripts/` 에 들어 있다. 따로 받을 것은 없고 의존 패키지만 설치한다.

```bash
pip install -r .claude/skills/stay/scripts/requirements.txt
```

(파이썬 3.10 이상, `requests`, `python-dotenv`)

설정과 상태 파일은 **프로젝트가 아니라 사용자 홈**의 한 폴더에 둔다. 기본은 `~/.claude/stay/` 이고,
환경변수 `STAY_HOME` 으로 바꿀 수 있다. 홈에 두는 이유: 프로젝트마다·worktree 마다 파일이 갈라지면
감시기와 질문 스크립트가 서로의 파일을 못 본다.

| 변수 | 뜻 | 예시 형태 |
|---|---|---|
| `TELEGRAM_ASK_BOT_TOKEN` | ask 봇의 토큰 (BotFather 가 준다) | `123456789:AAH...` (숫자:영문숫자 35자 안팎) |
| `TELEGRAM_ASK_BOT_CHAT_ID` | 봇과 나 사이 개인 채팅의 id | `987654321` (개인 채팅은 양수, 그룹은 `-100...` 음수) |

> **이미 다른 프로그램이 폴링하고 있는 봇을 그대로 쓰면 안 된다.** 텔레그램 `getUpdates` 는
> 봇 하나당 소비자가 하나뿐이라, 같은 봇을 두 곳에서 폴링하면 한쪽이 다른 쪽 메시지를 삼킨다.
> 그래서 스크립트는 `TELEGRAM_ASK_BOT_TOKEN` 이 흔히 쓰는 이름인 `TELEGRAM_BOT_TOKEN` 과 같으면 돌기 전에 거부한다.
> **ask 봇은 이 용도 전용으로 하나 새로 만든다.**

## 1. 봇 토큰 받기 (@BotFather)

1. 텔레그램에서 `@BotFather` 를 검색해 대화를 연다.
2. `/newbot` 을 보낸다.
3. 봇의 **표시 이름**을 입력한다. 예: `내 비서 ask`
4. 봇의 **username** 을 입력한다. 반드시 `bot` 으로 끝나야 하고 전 세계에서 유일해야 한다.
   예: `my_ask_bot`
5. BotFather 가 `Use this token to access the HTTP API:` 다음 줄에 토큰을 준다.
   이 값이 `TELEGRAM_ASK_BOT_TOKEN` 이다.

토큰을 잃어버리거나 노출됐으면 BotFather 에서 `/mybots` → 봇 선택 → `API Token` → `Revoke current token` 으로 새로 받는다.

## 2. chat id 받기

chat id 는 "봇이 나에게 메시지를 보낼 때 쓰는 대화방 번호"다. 봇은 **내가 먼저 말을 걸기 전에는**
나에게 메시지를 보낼 수 없으므로, 먼저 봇에게 아무 말이나 보내야 한다.

### 방법 A — getUpdates 로 직접 확인 (추천, 봇 하나로 끝)

1. 텔레그램에서 방금 만든 봇을 검색해 대화를 열고 `/start` 또는 아무 글을 보낸다.
2. 브라우저 주소창에 다음을 넣는다. `<토큰>` 자리에 1 단계의 토큰을 넣는다.

   ```
   https://api.telegram.org/bot<토큰>/getUpdates
   ```

3. 돌아온 JSON 에서 `"chat":{"id":987654321, ...}` 의 `id` 값이 chat id 다.
   PowerShell 로 바로 뽑으려면:

   ```powershell
   $t = "<토큰>"
   (Invoke-RestMethod "https://api.telegram.org/bot$t/getUpdates").result | ForEach-Object { $_.message.chat.id } | Select-Object -Unique
   ```

   macOS/Linux 라면:

   ```bash
   curl -s "https://api.telegram.org/bot<토큰>/getUpdates" | python -c "import json,sys; print({u['message']['chat']['id'] for u in json.load(sys.stdin)['result'] if 'message' in u})"
   ```

4. `result` 가 빈 배열(`[]`)이면 봇에게 메시지를 아직 안 보냈거나, 다른 곳에서 이미 폴링해 가져간 것이다.
   메시지를 한 번 더 보내고 다시 연다. (감시기 `ask_watch.py` 가 돌고 있으면 그것이 먼저 가져가므로 `/stay on` 으로 끈 뒤 확인한다.)

### 방법 B — @userinfobot 으로 내 id 확인

`@userinfobot` 에게 아무 말이나 보내면 `Id: 987654321` 로 내 사용자 id 를 알려 준다.
**개인 채팅의 chat id 는 사용자 본인의 id 와 같다.** 그래서 어떤 봇으로 열어도 같은 값이 나온다.

### 그룹 방을 쓰고 싶다면

1. 봇을 그룹에 초대한다.
2. BotFather 에서 `/setprivacy` → 봇 선택 → `Disable` 로 두어야 봇이 그룹의 일반 메시지를 읽는다.
3. 그룹에서 아무 글을 보내고 방법 A 의 getUpdates 를 열면 `chat.id` 가 `-100...` 으로 시작하는 음수로 나온다. 그 값을 그대로 쓴다.

## 3. `.env` 에 넣기

`~/.claude/stay/.env`(또는 `STAY_HOME/.env`)를 만들어 적는다. 폴더가 없으면 만든다.

```dotenv
TELEGRAM_ASK_BOT_TOKEN=123456789:AAH...
TELEGRAM_ASK_BOT_CHAT_ID=987654321
```

- 스크립트는 `STAY_HOME/.env` 를 먼저 읽고, 그다음 현재 폴더의 `.env` 도 읽는다(이미 프로젝트 `.env` 에 두었다면 그대로 동작한다).
  쉘에서 export 한 값이 파일보다 우선한다.
- 이 파일은 git 에 올리지 않는다.
- 토큰·chat id 를 채팅이나 텔레그램 메시지로 되풀이해 보내지 않는다 (`stay` 스킬도 비밀값의 **내용**은 텔레그램에 보내지 않도록 되어 있다).

## 4. 동작 확인

```bash
python .claude/skills/stay/scripts/ask_me.py --notify "ask 봇 설정 확인"
```

텔레그램의 ask 봇 대화방에 이 글이 오면 끝이다. 오지 않으면 순서대로 본다.

| 증상 | 원인 | 조치 |
|---|---|---|
| `TELEGRAM_ASK_BOT_TOKEN / TELEGRAM_ASK_BOT_CHAT_ID 가 설정되지 않았습니다` | `.env` 가 `STAY_HOME` 에 없거나 변수 이름 오타 | 파일 위치와 변수 이름 두 개를 확인한다 |
| `TELEGRAM_BOT_TOKEN 과 같습니다` | 다른 프로그램의 봇 토큰을 재사용했다 | BotFather 에서 봇을 하나 더 만들어 그 토큰을 넣는다 |
| `ModuleNotFoundError: requests` / `dotenv` | 의존 패키지 미설치 | 0 단계의 `pip install` |
| HTTP 401 `Unauthorized` | 토큰이 틀렸거나 revoke 됐다 | BotFather `/mybots` 에서 토큰을 다시 확인한다 |
| HTTP 400 `chat not found` | chat id 가 틀렸거나 봇에게 먼저 말을 건 적이 없다 | 봇에게 `/start` 를 보낸 뒤 getUpdates 로 id 를 다시 확인한다 |
| HTTP 403 `bot was blocked by the user` | 봇을 차단했다 | 텔레그램에서 봇 차단을 푼다 |

## 5. 테스트 돌리기 (선택)

```bash
python -m pytest .claude/skills/stay/scripts/tests -q
```

네트워크와 실제 `STAY_HOME` 을 쓰지 않는 단위 테스트다.
