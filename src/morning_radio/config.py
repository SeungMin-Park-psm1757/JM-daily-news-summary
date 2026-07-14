from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class AppConfig:
    gemini_api_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    telegram_thread_id: str | None
    telegram_silent: bool
    hours_back: int
    output_dir: Path
    editor_model: str
    triage_model: str
    tts_model: str
    enable_tts: bool
    timezone_name: str
    host_name: str
    analyst_name: str
    host_voice: str
    analyst_voice: str
    tts_quality_mode: str
    tts_speed_multiplier: float
    tts_turn_pause_multiplier: float
    tts_retry_count: int
    tts_retry_delay_seconds: int
    archive_limit: int
    public_archive_base_url: str | None
    per_query_limit: int
    max_story_count: int
    score_threshold: float
    max_output_tokens: int
    skip_llm: bool
    skip_tts: bool

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.gemini_api_key) and not self.skip_llm

    @property
    def tts_enabled(self) -> bool:
        return self.llm_enabled and self.enable_tts and not self.skip_tts

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def tts_bitrate_kbps(self) -> int:
        if self.tts_quality_mode.lower() == "manual":
            return 64
        return 48


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a Korean morning radio news briefing from the last 24 hours.",
    )
    parser.add_argument(
        "--hours-back",
        type=int,
        default=int(os.getenv("MORNING_RADIO_HOURS_BACK", "24")),
        help="How many past hours of news to consider.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("MORNING_RADIO_OUTPUT_DIR", "output"),
        help="Directory where generated files will be written.",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip Gemini calls and use fallback heuristic summaries instead.",
    )
    parser.add_argument(
        "--skip-tts",
        action="store_true",
        help="Skip audio generation even if TTS is enabled in the environment.",
    )
    return parser


def load_config(args: argparse.Namespace) -> AppConfig:
    _load_dotenv(Path(".env"))
    return AppConfig(
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        telegram_thread_id=os.getenv("TELEGRAM_THREAD_ID"),
        telegram_silent=_env_bool("MORNING_RADIO_TELEGRAM_SILENT", False),
        hours_back=args.hours_back,
        output_dir=Path(args.output_dir),
        editor_model=os.getenv("MORNING_RADIO_EDITOR_MODEL", "gemini-2.5-flash"),
        triage_model=os.getenv("MORNING_RADIO_TRIAGE_MODEL", "gemini-2.5-flash-lite"),
        tts_model=os.getenv("MORNING_RADIO_TTS_MODEL", "gemini-2.5-flash-preview-tts"),
        enable_tts=_env_bool("MORNING_RADIO_ENABLE_TTS", False),
        timezone_name=os.getenv("MORNING_RADIO_TIMEZONE", "Asia/Seoul"),
        host_name=os.getenv("MORNING_RADIO_HOST_NAME", "HOST"),
        analyst_name=os.getenv("MORNING_RADIO_ANALYST_NAME", "ANALYST"),
        host_voice=os.getenv("MORNING_RADIO_HOST_VOICE", "Charon"),
        analyst_voice=os.getenv("MORNING_RADIO_ANALYST_VOICE", "Leda"),
        tts_quality_mode=os.getenv("MORNING_RADIO_TTS_MODE", "daily"),
        tts_speed_multiplier=float(os.getenv("MORNING_RADIO_TTS_SPEED", "0.95")),
        tts_turn_pause_multiplier=float(os.getenv("MORNING_RADIO_TTS_TURN_PAUSE", "1.5")),
        tts_retry_count=int(os.getenv("MORNING_RADIO_TTS_RETRY_COUNT", "1")),
        tts_retry_delay_seconds=int(os.getenv("MORNING_RADIO_TTS_RETRY_DELAY_SECONDS", "40")),
        archive_limit=int(os.getenv("MORNING_RADIO_ARCHIVE_LIMIT", "20")),
        public_archive_base_url=os.getenv("MORNING_RADIO_PUBLIC_ARCHIVE_BASE_URL"),
        per_query_limit=int(os.getenv("MORNING_RADIO_PER_QUERY_LIMIT", "12")),
        max_story_count=int(os.getenv("MORNING_RADIO_MAX_STORY_COUNT", "4")),
        score_threshold=float(os.getenv("MORNING_RADIO_SCORE_THRESHOLD", "40")),
        max_output_tokens=int(os.getenv("MORNING_RADIO_MAX_OUTPUT_TOKENS", "5120")),
        skip_llm=args.skip_llm,
        skip_tts=args.skip_tts,
    )
