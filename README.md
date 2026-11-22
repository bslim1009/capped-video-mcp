# capped-video-mcp

영상 생성 모델을 에이전트(Claude Code, Claude Desktop 등)에서 부르는 로컬 MCP 서버. 생성은 **Replicate**와 **fal**을 통해 호출하고, API 키는 각자 자기 것을 쓴다.

지원 모델: `seedance-2.5`(Replicate, 기본), `seedance-2.5-fal`, `kling-3.0`, `wan-2.5`, `veo-3.1-fast`, `veo-3.1-lite`, `hailuo-2.3`, `gemini-omni-1.2-flash`. 모델마다 허용 길이·해상도·가격이 다르다(`list_models` 또는 [기본 config.toml](src/capped_video_mcp/config.toml)).

비용 상한(모델 허용 목록, 해상도·길이, 일일·월간 예산, 동시 작업 수)과 자동 다운로드를 서버가 강제한다.

> 가격표는 서버가 예산을 계산하는 **예상값**이다. 실제 청구액은 Replicate·fal 대시보드에서 확인한다. `seedance-2.5`(Replicate) 가격은 아직 확인 전(`price_checked = "unverified"`)이다.

## 설치와 등록

필요한 것: [uv](https://docs.astral.sh/uv/), Replicate 토큰 또는 fal 키 (둘 중 하나만 있어도 된다. 키가 없는 쪽 모델은 꺼진다). 선택: [ffmpeg](https://ffmpeg.org/) (결과 미리보기 `preview_video`에만 쓴다).

### Claude Code

```bash
claude mcp add capped-video --scope user \
  -e FAL_KEY=fal-xxxx \
  -e REPLICATE_API_TOKEN=r8_xxxx \
  -e CAPPED_VIDEO_DEFAULT_MODEL=kling-3.0 \
  -e CAPPED_VIDEO_DAILY_MAX_USD=3 \
  -- uvx capped-video-mcp
```

`--scope user`는 모든 프로젝트에서 쓴다. 팀 저장소에 `.mcp.json`으로 공유할 때는 키를 적지 말고 각자의 환경 변수를 참조한다.

```json
{
  "mcpServers": {
    "capped-video": {
      "command": "uvx",
      "args": ["capped-video-mcp"],
      "env": {
        "FAL_KEY": "${FAL_KEY:-}",
        "REPLICATE_API_TOKEN": "${REPLICATE_API_TOKEN:-}",
        "CAPPED_VIDEO_DEFAULT_MODEL": "kling-3.0"
      }
    }
  }
}
```

`/mcp`로 연결을 확인한다. `generate_video`는 과금되므로 자동 허용 목록에 넣지 말고 매번 승인하는 것을 권한다.

`uvx`는 한 번 받은 버전을 계속 쓴다. 새 버전으로 바꾸려면 `uvx capped-video-mcp@latest`로 등록하거나 `uvx --refresh capped-video-mcp`를 한 번 실행한다.

### Claude Desktop 등 다른 클라이언트

`claude_desktop_config.json`(또는 각 클라이언트의 MCP 설정)의 `mcpServers`에 위 JSON과 같은 형식으로 넣는다. 키는 `${...}` 대신 값을 직접 쓴다.

### 키 없이 시험

`CAPPED_VIDEO_PROVIDER=mock`을 함께 넘기면 실제 API를 부르지 않고 가짜 파일을 만든다. 기록도 실제와 분리된다(`data/usage-mock.json`, `<출력 폴더>/mock/`).

## 도구

| 도구 | 과금 | 하는 일 |
|---|---|---|
| `generate_video` | **예** | 생성 작업을 만들고 `job_id`를 돌려준다. `model`, `resolution`, `duration_sec`, `aspect_ratio`를 요청마다 고를 수 있다. **최근 10분 안의 같은 요청은 새 작업을 만들지 않고 기존 `job_id`를 `duplicate: true`로 돌려준다**(과금 없음). `request_id`로도 중복을 잡고, 같은 영상을 하나 더 원할 때만 `force: true` |
| `get_video_status` | 아니오 | 상태 확인. 최대 60초 기다린다. 완료면 파일 경로 |
| `download_video` | 아니오 | 자동 다운로드가 실패했을 때 다시 시도 |
| `get_usage` | 아니오 | 오늘·이번 달 사용량과 상한 |
| `estimate_video` | 아니오 | 조건(길이·해상도·비율)에 맞는 모델별 예상 비용을 싼 순서로, 남은 예산과 함께. 예산 안에서 가장 싼 모델(`cheapest`), 조건을 못 맞추는 모델과 이유(`unavailable`), 최근 같은 요청(`duplicate_of`) |
| `preview_video` | 아니오 | 완성된 영상의 프레임 6장을 한 이미지로 붙여 **이미지로** 돌려준다. 에이전트가 결과를 보고 재생성 여부를 판단할 수 있다. ffmpeg 필요(없으면 이유를 돌려줌) |
| `list_models` | 아니오 | 허용 모델, 가격, 길이 범위, 현재 기본값 |

결과 수거는 서버(클라이언트 세션)가 켜져 있는 동안에만 일어난다. 생성 중에는 세션을 닫지 않는다.

### 중복 요청 감지

에이전트는 응답이 늦거나 애매하면 같은 요청을 다시 보내기 쉽다. 서버는 프롬프트(앞뒤·연속 공백만 정리)·모델·해상도·길이·화면 비율이 같은 요청이 `duplicate_window_minutes`(기본 10분) 안에 다시 오면 제공처를 부르지 않고 기존 작업의 상태를 돌려준다. 과금되지 않은 거부(`create_failed`)와 확정 실패(`failed`)는 다시 시도할 수 있으므로 중복으로 보지 않는다. 검사와 예약이 같은 파일 잠금 안에서 일어나서 두 클라이언트가 동시에 같은 요청을 보내도 하나만 만들어진다. `CAPPED_VIDEO_DUPLICATE_WINDOW_MINUTES=0`으로 끌 수 있다.

### 생성 전 비용 확인 (선택)

`CAPPED_VIDEO_CONFIRM_ABOVE_USD=0.5`처럼 임계값을 주면, 예상 비용이 그 이상일 때 **서버가** 클라이언트에 "약 $0.63이 과금됩니다. 진행할까요?"를 직접 묻는다(MCP elicitation). 클라이언트의 도구 승인 설정과 무관하게 동작하므로, 자동 승인 모드에서도 비싼 생성은 한 번 더 확인받을 수 있다. 클라이언트가 이 요청을 지원하지 않으면 `CAPPED_VIDEO_CONFIRM_FALLBACK`(`allow` 기본: 그냥 진행, `deny`: 거부)을 따른다. 기본값 0은 묻지 않는다.

### 제공처 전환

같은 모델이 두 제공처에 있으면(`seedance-2.5` ↔ `seedance-2.5-fal`) 1차 제공처가 요청을 **확정 거부**(4xx, 점검 등)했을 때 남은 예산을 다시 확인하고 대체 모델로 만든다. 응답에 `failover_from`이 붙는다. 결과를 알 수 없는 오류(타임아웃, 5xx)에서는 과금됐을 수 있으므로 전환하지 않는다. `CAPPED_VIDEO_FAILOVER=false`로 끈다. 대체 모델은 설정의 `fallback_model`로 바꿀 수 있다.

### 결과 미리보기

`preview_video`는 ffmpeg로 프레임 6장(가로 3장 × 2줄, 시간 순서)을 한 JPEG로 붙여 이미지 콘텐츠로 돌려준다. 결과는 영상 옆에 `<job_id>.preview.jpg`로 저장해 다시 만들지 않는다. ffmpeg는 PATH 또는 `CAPPED_VIDEO_FFMPEG`로 찾고, 없으면 영상 경로와 함께 이유만 돌려준다. 장수·가로 크기는 설정 `[preview]`에서 바꾼다.

## 설정

설정은 아래 순서로 덮어쓴다. 뒤에 있는 것이 이긴다.

1. 패키지 기본값 [src/capped_video_mcp/config.toml](src/capped_video_mcp/config.toml)
2. 데이터 폴더의 `config.toml` (있으면. 바꿀 항목만 쓴다)
3. `CAPPED_VIDEO_*` 환경 변수 (등록 때 `-e`/`env`로 넘기거나 데이터 폴더의 `.env`)

### 환경 변수

| 변수 | 설정 위치 | 예 |
|---|---|---|
| `FAL_KEY` | fal API 키 (API 범위, ADMIN 범위 쓰지 않기) | |
| `REPLICATE_API_TOKEN` | Replicate 토큰 | |
| `CAPPED_VIDEO_DEFAULT_MODEL` | `defaults.model` | `kling-3.0` |
| `CAPPED_VIDEO_DEFAULT_RESOLUTION` | `defaults.resolution` | `480p` |
| `CAPPED_VIDEO_DEFAULT_DURATION_SEC` | `defaults.duration_sec` | `5` |
| `CAPPED_VIDEO_DEFAULT_ASPECT_RATIO` | `defaults.aspect_ratio` | `9:16` |
| `CAPPED_VIDEO_MAX_RESOLUTION` | `limits.max_resolution` | `720p` |
| `CAPPED_VIDEO_MAX_DURATION_SEC` | `limits.max_duration_sec` | `10` |
| `CAPPED_VIDEO_DAILY_MAX_COUNT` | `limits.daily_max_count` | `10` |
| `CAPPED_VIDEO_DAILY_MAX_USD` | `limits.daily_max_usd` | `5` |
| `CAPPED_VIDEO_MONTHLY_MAX_USD` | `limits.monthly_max_usd` | `30` |
| `CAPPED_VIDEO_MAX_PENDING_JOBS` | `limits.max_pending_jobs` | `3` |
| `CAPPED_VIDEO_DUPLICATE_WINDOW_MINUTES` | `limits.duplicate_window_minutes` (같은 요청을 중복으로 보는 시간. `0`이면 끔) | `10` |
| `CAPPED_VIDEO_CONFIRM_ABOVE_USD` | `limits.confirm_above_usd` (이 금액 이상이면 생성 전 사용자 확인. `0`이면 끔) | `0.5` |
| `CAPPED_VIDEO_CONFIRM_FALLBACK` | `limits.confirm_fallback` (확인을 지원하지 않는 클라이언트: `allow`/`deny`) | `allow` |
| `CAPPED_VIDEO_FAILOVER` | `limits.failover` (확정 거부 시 `fallback_model`로 전환) | `true` |
| `CAPPED_VIDEO_FFMPEG` | `preview.ffmpeg` (미리보기용 ffmpeg 경로. 비우면 PATH) | `C:/ffmpeg/bin/ffmpeg.exe` |
| `CAPPED_VIDEO_OUTPUT_DIR` | `download.dir` (영상 저장 폴더. 상대 경로면 데이터 폴더 기준) | `~/Videos/capped-video` |
| `CAPPED_VIDEO_HOME` | 데이터 폴더 위치. **절대 경로**, 등록 env로만 (`.env`에 쓰면 효과 없음) | `D:/capped-video` |
| `CAPPED_VIDEO_PROVIDER` | `mock`이면 실제 API를 부르지 않는다. 다른 값은 오류 | `mock` |

빈 값은 없는 것으로 본다(`.env`에 값이 있으면 그 값을 쓴다). 값이 잘못되면(없는 모델, 모르는 해상도, 숫자가 아닌 예산 등) 서버가 뜨지 않고 어떤 변수·파일이 문제인지 알려 준다. 경로에는 `~`와 환경 변수(`%USERPROFILE%`, `$HOME`)를 쓸 수 있다. API 키를 도구 인자나 대화로 넘기지 않는다(대화 기록과 로그에 남는다).

### 데이터 폴더

| 실행 방식 | 위치 |
|---|---|
| `CAPPED_VIDEO_HOME` 지정 | 그 폴더 |
| `uvx`·`pip` 설치 | `~/.capped-video-mcp` (Windows: `C:\Users\<이름>\.capped-video-mcp`) |
| 이 저장소에서 `uv sync` 후 실행 | 저장소 루트 |

안에 생기는 것: `config.toml`·`.env`(선택, 직접 만든다), `output/`(영상), `data/usage.json`(사용량 기록), `logs/`.

데이터 폴더 `config.toml` 예. 표(`[limits]` 등)는 쓴 키만 바뀌고, 목록(`[[models]]`, `allowed_hosts`)은 통째로 바뀐다.

```toml
[defaults]
model = "veo-3.1-lite"

[limits]
daily_max_usd = 2.0

[download]
dir = 'D:\Videos\capped-video'   # Windows 경로는 작은따옴표 또는 / 로 쓴다
```

## 개발

```bash
uv sync
cp .env.example .env        # 키를 채운다. 키 없이 시험하려면 CAPPED_VIDEO_PROVIDER=mock
uv run pytest               # 실제 API 를 부르지 않는다
```

저장소에서 띄우려면 `.mcp.json`(git 제외)에 등록한다.

```json
{
  "mcpServers": {
    "capped-video": {
      "command": "uv",
      "args": ["run", "--directory", "<저장소 경로>", "python", "-m", "capped_video_mcp"]
    }
  }
}
```

배포 절차는 [doc/배포가이드.md](doc/배포가이드.md), 작업 경과는 [doc/작업진행.md](doc/작업진행.md)에 있다.

## 라이선스

MIT
