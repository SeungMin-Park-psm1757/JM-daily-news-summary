# 청양 농업 데일리 브리핑

청양 농업 데일리 브리핑은 매일 다음 내용을 수집·요약해 전송합니다.

- 충남 청양의 오늘 날씨와 7일 날씨 개요(Open-Meteo)
- 최근 농사·농업 뉴스와 비료 관련 새 소식
- 날짜에 따라 바뀌는 쉬운 비료 공부 주제

기존의 정치·국제·국방·AI·경제 중심 카테고리는 위 주제로 교체되었습니다.

실행 결과는 다음으로 제공됩니다:

- a ranked article digest
- a two-speaker radio script
- an optional MP3 audio file
- a Telegram summary and audio delivery
- a simple HTML archive page

## What It Does

- Collects recent news for Korea politics, global affairs, military strategy, weapon systems, AI, quantum, and the economy.
- Scores articles by recency, source reliability, signal terms, and low-signal penalties.
- Clusters near-duplicate coverage so one event is represented once, with extra signal matching for finance and geopolitical follow-ups.
- Produces a messenger digest with a short summary and a short "why it matters" line.
- Generates a two-speaker Korean radio script with a lively host and an energetic analyst voice.
- Uses Gemini TTS to create an MP3 when enabled.
- Supports Telegram private chats, groups, channels, and topic threads.
- Writes per-run output plus an HTML archive index.

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
```

Set `GEMINI_API_KEY` in `.env`, then run:

```bash
morning-radio
```

For a no-API smoke test:

```bash
morning-radio --skip-llm --skip-tts
```

## Main Outputs

Each run writes to `output/YYYYMMDD-HHMMSS/`.

- `news_items.json`: all collected items
- `selected_items.json`: clustered and selected representatives
- `category_briefs.json`: category-level brief objects
- `radio_show.json`: final radio show metadata
- `radio_script.md`: markdown radio script
- `radio_script.txt`: plain text transcript for TTS
- `message_digest.md`: Telegram-friendly digest
- `summary.md`: run summary and quota log
- `index.html`: run-level archive page
- `audio.mp3`: generated TTS audio when available
- `run_metadata.json`: machine-readable run metadata

The root `output/index.html` file lists recent runs as a lightweight archive page.

## Key Environment Variables

- `GEMINI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_THREAD_ID`
- `MORNING_RADIO_TELEGRAM_SILENT`
- `MORNING_RADIO_PUBLIC_ARCHIVE_BASE_URL`
- `MORNING_RADIO_ENABLE_TTS`
- `MORNING_RADIO_TTS_MODE`
  - `daily`: lighter weekday mode
  - `manual`: higher-bitrate manual mode
- `MORNING_RADIO_HOST_VOICE`
- `MORNING_RADIO_ANALYST_VOICE`
- `MORNING_RADIO_TTS_SPEED`
- `MORNING_RADIO_TTS_TURN_PAUSE`
- `MORNING_RADIO_TTS_RETRY_COUNT`
- `MORNING_RADIO_TTS_RETRY_DELAY_SECONDS`
- `MORNING_RADIO_SCORE_THRESHOLD`
- `MORNING_RADIO_MAX_STORY_COUNT`
- `MORNING_RADIO_ARCHIVE_LIMIT`

Current weekday defaults are tuned for a denser daily brief:

- score threshold `40`
- up to `4` stories per populated category
- TTS pace `1.27x`

## GitHub Actions

The workflow is defined in `.github/workflows/daily-radio.yml`.

- Schedule: weekday `06:00 KST`
- The workflow uses `daily` TTS mode by default
- The workflow pins score threshold `40`, max story count `4`, and TTS speed `1.27`
- Telegram delivery is enabled when the Telegram secrets are present
- Optional repository variables:
  - `MORNING_RADIO_TELEGRAM_SILENT` for quiet group or channel delivery
  - `MORNING_RADIO_PUBLIC_ARCHIVE_BASE_URL` to append archive links in Telegram

Required secrets:

- `GEMINI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_THREAD_ID` only for topic-based groups

Group and channel notes:

- Use a group or channel `chat_id` to broadcast the same digest to everyone in that destination.
- Use `TELEGRAM_THREAD_ID` only for topic-enabled supergroups.
- When `MORNING_RADIO_PUBLIC_ARCHIVE_BASE_URL` is set, Telegram messages include direct links to the run archive, digest, summary, and audio file when available.

## Notes

- Text generation and TTS share the same Gemini API key but use different models.
- The main free-tier bottleneck is TTS, not text generation.
- If TTS fails, the pipeline still delivers the text digest and preserves run metadata.
- If LLM generation fails, the package falls back to heuristic summaries so the pipeline still completes.
