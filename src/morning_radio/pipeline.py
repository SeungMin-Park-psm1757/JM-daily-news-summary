from __future__ import annotations

import array
import html
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from morning_radio.config import AppConfig
from morning_radio.gemini import GeminiEditor
from morning_radio.models import CategoryBrief, NewsItem, RadioShow
from morning_radio.news_sources import CATEGORIES, collect_news, enrich_articles, flatten_news
from morning_radio.telegram import send_digest_and_audio

OPENING_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "안녕하세요. 청양 농업 브리핑입니다.",
        "청양 날씨와 비료 정보를 전해드리겠습니다.",
    ),
)

logger = logging.getLogger(__name__)

TITLE_STOPWORDS = {
    "속보",
    "단독",
    "상보",
    "뉴스특보",
    "종합",
    "오늘",
    "관련",
    "브리핑",
    "이란",
    "대한",
    "위한",
    "통해",
}

DEDUP_STEMS = (
    "우원식",
    "개헌",
    "지방선거",
    "국민투표",
    "한동훈",
    "중동",
    "유가",
    "전쟁",
    "휴전",
    "트럼프",
    "김여정",
    "미사일",
    "금리",
    "환율",
    "ai",
    "양자",
)

TOKEN_ALIASES = {
    "한미훈련": "joint_drill",
    "군사훈련": "joint_drill",
    "연합훈련": "joint_drill",
    "훈련": "joint_drill",
    "멈춰라": "halt_action",
    "중단하라": "halt_action",
    "중단": "halt_action",
    "공격": "conflict_action",
    "전쟁": "conflict_action",
    "공습": "conflict_action",
    "관세": "tariff_policy",
    "관세전쟁": "tariff_policy",
    "무역전쟁": "tariff_policy",
    "환율": "fx_market",
    "환율마감": "fx_market",
    "원달러": "fx_market",
    "달러원": "fx_market",
    "유가": "oil_market",
    "국제유가": "oil_market",
    "원유": "oil_market",
}


def run_pipeline(config: AppConfig) -> Path:
    now_utc = datetime.now(tz=UTC)
    start_utc = now_utc - timedelta(hours=config.hours_back)
    run_dir = config.output_dir / now_utc.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    news_by_category = collect_news(
        hours_back=config.hours_back,
        per_query_limit=config.per_query_limit,
        now=now_utc,
    )
    selected_by_category = {
        category.key: _select_top_articles(news_by_category.get(category.key, []), config)
        for category in CATEGORIES
    }
    enrich_articles(
        [
            article
            for category in CATEGORIES
            for article in selected_by_category.get(category.key, [])
        ]
    )

    local_now = now_utc.astimezone(config.timezone)
    monthly_weather_active = (
        (local_now.weekday() == 0 and local_now.day <= 7)
        or (local_now.weekday() == 4 and local_now.day >= 15)
    )
    quiet_categories = [
        category.label
        for category in CATEGORIES
        if not selected_by_category.get(category.key)
        and (category.key != "cheongyang_weather_month" or monthly_weather_active)
    ]
    opening_pair = _opening_pair(now_utc.astimezone(config.timezone))
    quota_log = _quota_log(config, selected_by_category)

    briefs = _build_briefs(config, selected_by_category)
    show = _build_show(config, briefs, [], opening_pair, start_utc, now_utc)
    message_digest = _render_message_digest(show.show_title, briefs, [])

    _write_json(
        run_dir / "news_items.json",
        {
            "generated_at": now_utc.isoformat(),
            "articles": [item.to_dict() for item in flatten_news(news_by_category)],
        },
    )
    _write_json(
        run_dir / "selected_items.json",
        {
            category.label: [item.to_dict() for item in selected_by_category.get(category.key, [])]
            for category in CATEGORIES
        },
    )
    _write_json(run_dir / "category_briefs.json", [brief.to_dict() for brief in briefs])
    _write_json(run_dir / "radio_show.json", show.to_dict())
    (run_dir / "radio_script.md").write_text(show.script_markdown + "\n", encoding="utf-8")
    (run_dir / "radio_script.txt").write_text(show.script_plaintext + "\n", encoding="utf-8")
    (run_dir / "message_digest.md").write_text(message_digest, encoding="utf-8")

    audio_metadata: dict[str, Any] = {"generated": False}
    audio_path: Path | None = None
    if config.tts_enabled:
        try:
            editor = GeminiEditor(config)
            audio_bytes, mime_type = editor.generate_audio(show.script_plaintext)
            audio_path = _write_audio_output(run_dir, audio_bytes, mime_type, config)
            audio_metadata = {"generated": True, "mime_type": mime_type, "path": audio_path.name}
        except Exception as exc:  # pragma: no cover - resilience path
            audio_metadata = {"generated": False, "error": str(exc)}

    telegram_metadata: dict[str, Any] = {"sent": False}
    if config.telegram_enabled:
        try:
            public_links = _public_links(config, run_dir, audio_path)
            telegram_metadata = send_digest_and_audio(
                config=config,
                digest_markdown=message_digest,
                title=show.show_title,
                audio_path=audio_path,
                public_links=public_links,
            )
        except Exception as exc:  # pragma: no cover - resilience path
            telegram_metadata = {"sent": False, "error": str(exc)}

    summary = _render_summary(
        config=config,
        run_dir=run_dir,
        start_utc=start_utc,
        end_utc=now_utc,
        news_by_category=news_by_category,
        selected_by_category=selected_by_category,
        briefs=briefs,
        show=show,
        audio_metadata=audio_metadata,
        telegram_metadata=telegram_metadata,
        quota_log=quota_log,
    )
    (run_dir / "summary.md").write_text(summary, encoding="utf-8")
    _write_run_archive_page(run_dir, show, briefs, audio_metadata)
    _write_archive_index(config.output_dir, config.archive_limit)
    _write_json(
        run_dir / "run_metadata.json",
        {
            "generated_at": now_utc.isoformat(),
            "start_utc": start_utc.isoformat(),
            "end_utc": now_utc.isoformat(),
            "llm_enabled": config.llm_enabled,
            "tts_enabled": config.tts_enabled,
            "audio": audio_metadata,
            "telegram": telegram_metadata,
            "quota_log": quota_log,
            "quiet_categories": quiet_categories,
            "run_dir": str(run_dir),
        },
    )
    return run_dir


def _opening_pair(local_dt: datetime) -> tuple[str, str]:
    index = local_dt.toordinal() % len(OPENING_PATTERNS)
    return OPENING_PATTERNS[index]


def _select_top_articles(items: list[NewsItem], config: AppConfig) -> list[NewsItem]:
    ranked = sorted(items, key=lambda article: (article.score, article.published_at), reverse=True)
    clusters = _cluster_articles(ranked)
    category = ranked[0].category if ranked else ""
    threshold = min(config.score_threshold, 28.0) if category in {"agriculture_news", "fertilizer_news"} else config.score_threshold
    selected: list[NewsItem] = []
    for cluster_index, cluster in enumerate(clusters, start=1):
        article = _cluster_representative(cluster)
        if article.score < threshold:
            continue
        article.cluster_id = f"{article.category}-{cluster_index:02d}"
        article.cluster_size = len(cluster)
        selected.append(article)
        if len(selected) >= config.max_story_count:
            break
    return selected


def _cluster_articles(items: list[NewsItem]) -> list[list[NewsItem]]:
    clusters: list[list[NewsItem]] = []
    for article in items:
        placed = False
        for cluster in clusters:
            if any(_cluster_similarity(article, existing) >= 0.34 for existing in cluster):
                cluster.append(article)
                placed = True
                break
        if not placed:
            clusters.append([article])

    return sorted(clusters, key=_cluster_rank, reverse=True)


def _cluster_rank(cluster: list[NewsItem]) -> tuple[float, datetime]:
    representative = _cluster_representative(cluster)
    cluster_bonus = min(len(cluster) * 1.5, 6.0)
    return (representative.score + representative.source_weight + cluster_bonus, representative.published_at)


def _cluster_representative(cluster: list[NewsItem]) -> NewsItem:
    return max(
        cluster,
        key=lambda article: (
            article.score + article.source_weight + min(len(cluster) * 1.2, 5.0),
            article.published_at,
        ),
    )


def _cluster_similarity(left: NewsItem, right: NewsItem) -> float:
    if _is_duplicate_story(left, right):
        return 1.0

    left_tokens = _story_tokens(left)
    right_tokens = _story_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0

    overlap = left_tokens & right_tokens
    return len(overlap) / len(left_tokens | right_tokens)


def _is_duplicate_story(left: NewsItem, right: NewsItem) -> bool:
    left_tokens = _title_tokens(left.title)
    right_tokens = _title_tokens(right.title)
    overlap = left_tokens & right_tokens
    if len(overlap) >= 3:
        return True

    if left_tokens and right_tokens:
        jaccard = len(overlap) / len(left_tokens | right_tokens)
        if jaccard >= 0.45:
            return True

        if len(_signal_tokens(left_tokens) & _signal_tokens(right_tokens)) >= 2:
            return True

    left_subject = _headline_subject(left.title)
    right_subject = _headline_subject(right.title)
    stem_overlap = {
        stem for stem in DEDUP_STEMS if stem.lower() in left.title.lower() and stem.lower() in right.title.lower()
    }
    if left_subject and left_subject == right_subject and len(stem_overlap) >= 2:
        return True
    return False


def _signal_tokens(tokens: set[str]) -> set[str]:
    return {
        token
        for token in tokens
        if token in TOKEN_ALIASES.values() or any(char.isdigit() for char in token)
    }


def _title_tokens(title: str) -> set[str]:
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", title)
    cleaned = cleaned.replace("“", " ").replace("”", " ").replace('"', " ")
    tokens = set()
    for token in re.findall(r"[0-9A-Za-z가-힣]+", cleaned):
        lowered = token.lower()
        if len(lowered) <= 1 or lowered in TITLE_STOPWORDS:
            continue
        tokens.add(lowered)
        alias = TOKEN_ALIASES.get(lowered)
        if alias:
            tokens.add(alias)
        if lowered.endswith("훈련"):
            tokens.add("joint_drill")
        if lowered.endswith("전쟁") or lowered.endswith("공격") or lowered.endswith("공습"):
            tokens.add("conflict_action")
        if "환율" in lowered or "원달러" in lowered or "달러원" in lowered or "달러" in lowered:
            tokens.add("fx_market")
        if "유가" in lowered or "원유" in lowered:
            tokens.add("oil_market")
        if "관세" in lowered or "tariff" in lowered:
            tokens.add("tariff_policy")
        if "무역" in lowered and ("갈등" in lowered or "전쟁" in lowered):
            tokens.add("tariff_policy")
        digits = re.sub(r"\D", "", lowered)
        if len(digits) >= 2:
            tokens.add(f"num_{digits}")
    return tokens


def _story_tokens(article: NewsItem) -> set[str]:
    summary_tokens = {
        token.lower()
        for token in re.findall(r"[0-9A-Za-z가-힣]{2,}", article.summary)
        if len(token) > 2
    }
    return _title_tokens(article.title) | summary_tokens


def _headline_subject(title: str) -> str:
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", title).strip()
    cleaned = cleaned.replace("“", '"').replace("”", '"')
    match = re.match(r'(.+?)\s*"(.+?)"', cleaned)
    if match:
        return match.group(1).strip()
    return cleaned.split(" ", 1)[0].strip()


def _build_briefs(
    config: AppConfig,
    selected_by_category: dict[str, list[NewsItem]],
) -> list[CategoryBrief]:
    if config.llm_enabled:
        editor = GeminiEditor(config)
        briefs: list[CategoryBrief] = []
        for category in CATEGORIES:
            selected = selected_by_category.get(category.key, [])
            if not selected:
                briefs.append(_fallback_brief(category.key, category.label, []))
                continue
            try:
                briefs.append(
                    editor.create_category_brief(
                        category=category.key,
                        label=category.label,
                        articles=selected,
                        max_story_count=config.max_story_count,
                    ),
                )
            except Exception:
                logger.warning("Gemini category brief failed for %s; using deterministic brief.", category.key, exc_info=True)
                briefs.append(_fallback_brief(category.key, category.label, selected))
        return briefs

    return [
        _fallback_brief(category.key, category.label, selected_by_category.get(category.key, []))
        for category in CATEGORIES
    ]


def _build_show(
    config: AppConfig,
    briefs: list[CategoryBrief],
    quiet_categories: list[str],
    opening_pair: tuple[str, str],
    start_utc: datetime,
    end_utc: datetime,
) -> RadioShow:
    if config.llm_enabled:
        try:
            editor = GeminiEditor(config)
            candidate = editor.create_radio_show(
                briefs=briefs,
                quiet_categories=quiet_categories,
                opening_pair=opening_pair,
                start_iso=start_utc.isoformat(),
                end_iso=end_utc.isoformat(),
            )
            return _validate_radio_show(candidate, config)
        except Exception:
            logger.warning("Gemini radio script failed validation; using deterministic script.", exc_info=True)
            return _fallback_show(config, briefs, quiet_categories, opening_pair, start_utc, end_utc)
    return _fallback_show(config, briefs, quiet_categories, opening_pair, start_utc, end_utc)


def _validate_radio_show(show: RadioShow, config: AppConfig) -> RadioShow:
    script = show.script_markdown.strip()
    if not script:
        raise ValueError("Gemini returned an empty radio script.")

    dialogue_lines = [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    host_prefix = f"{config.host_name}:"
    analyst_prefix = f"{config.analyst_name}:"
    if not any(line.startswith(host_prefix) for line in dialogue_lines):
        raise ValueError("Radio script is missing the host speaker label.")
    if not any(line.startswith(analyst_prefix) for line in dialogue_lines):
        raise ValueError("Radio script is missing the analyst speaker label.")
    if len(dialogue_lines) < 8:
        raise ValueError("Radio script is too short for a two-person broadcast.")
    if any(not line.startswith((host_prefix, analyst_prefix)) for line in dialogue_lines):
        raise ValueError("Radio script contains an unlabeled dialogue line.")
    if any(token in script for token in ("http://", "https://", "왜 중요한가", "we picked the top")):
        raise ValueError("Radio script contains operational or non-broadcast text.")

    estimated_minutes = max(5, min(int(show.estimated_minutes), 7))
    plaintext = re.sub(r"^#+\s*", "", script, flags=re.MULTILINE)
    plaintext = re.sub(r"\*\*(.*?)\*\*", r"\1", plaintext)
    return RadioShow(
        show_title=show.show_title or "아침 뉴스 라디오",
        show_summary=show.show_summary,
        estimated_minutes=estimated_minutes,
        script_markdown=script,
        script_plaintext=plaintext,
        quiet_categories=show.quiet_categories,
    )


def _fallback_brief(category: str, label: str, items: list[NewsItem]) -> CategoryBrief:
    if not items:
        return CategoryBrief(
            category=category,
            label=label,
            lead=f"오늘은 {label} 분야에서 기준 점수를 넘는 뚜렷한 기사가 많지 않았습니다.",
            stories=[],
            watch="후속 보도가 더 쌓이면 다음 실행에서 다시 포착하겠습니다.",
        )

    stories = [
        {
            "headline": article.title,
            "angle": _condense_article(article),
            "message_summary": _fallback_message_summary(article),
            "article_summary": _article_summary(category, article),
            "why_it_matters": _article_summary(category, article),
            "verification_note": _verification_note(article.verification_flags or []),
            "source_urls": [article.resolved_url or article.url],
            "score": article.score,
            "source": article.source,
            "source_domain": article.source_domain,
            "source_weight": article.source_weight,
            "cluster_size": article.cluster_size,
            "verification_flags": article.verification_flags or [],
            "fallback_story": True,
            "category": category,
        }
        for article in items
    ]

    return CategoryBrief(
        category=category,
        label=label,
        lead=_compose_lead(category, label, stories),
        stories=stories,
        watch=_compose_follow_up_answer(category, label, stories),
    )


def _fallback_show(
    config: AppConfig,
    briefs: list[CategoryBrief],
    quiet_categories: list[str],
    opening_pair: tuple[str, str],
    start_utc: datetime,
    end_utc: datetime,
) -> RadioShow:
    local_date = end_utc.astimezone(config.timezone).strftime("%m월 %d일")
    lines = [
        f"# {local_date} 청양 농업 브리핑",
        "",
        f"{config.host_name}: {opening_pair[0]}",
        f"{config.analyst_name}: {opening_pair[1]}",
    ]

    for brief in briefs:
        if not brief.stories:
            continue
        lines.append("")
        setup = _fallback_radio_prompt(brief)
        lines.append(f"{config.host_name}: {setup}")
        lines.append(f"{config.analyst_name}: {brief.lead}")

    lines.append("")
    lines.append(f"{config.host_name}: 여기까지 {local_date} 청양 농업 브리핑이었습니다.")
    script_markdown = "\n".join(lines).strip()

    return RadioShow(
        show_title=f"{local_date} 청양 농업 브리핑",
        show_summary=f"{start_utc.isoformat()}부터 {end_utc.isoformat()}까지의 확인된 날씨와 농업·비료 정보를 정리했습니다.",
        estimated_minutes=6,
        script_markdown=script_markdown,
        script_plaintext=script_markdown.replace("# ", ""),
        quiet_categories=quiet_categories,
    )


def _fallback_radio_prompt(brief: CategoryBrief) -> str:
    prompts = {
        "cheongyang_weather_today": "먼저 오늘 청양 날씨입니다.",
        "cheongyang_weather_week": "내일과 모레 청양 날씨 전망입니다.",
        "cheongyang_weather_month": "청양의 월간 기상 전망입니다.",
        "fertilizer_learning": "오늘의 비료 공부입니다.",
    }
    if brief.category in prompts:
        return prompts[brief.category]
    return f"{brief.label}입니다."


def _compose_lead(category: str, label: str, stories: list[dict[str, Any]]) -> str:
    if category.startswith("cheongyang_weather_") or category == "fertilizer_learning":
        return _ensure_sentence(str(stories[0].get("article_summary") or stories[0].get("angle") or ""))
    summaries = [
        _first_sentence(_ensure_sentence(str(story.get("article_summary") or story.get("angle") or "")))
        for story in stories[:4]
    ]
    if len(summaries) == 1:
        return summaries[0]
    if len(summaries) == 2:
        return f"{summaries[0]} {summaries[1]}"
    return " ".join(summary for summary in summaries if summary)


def _compose_follow_up_answer(category: str, label: str, stories: list[dict[str, Any]]) -> str:
    if category == "cheongyang_weather_today":
        return "비나 눈이 예보된 시간대에는 방제와 야외 작업을 피하고, 강수량에 맞춰 배수와 시설물 점검을 준비하면 좋겠습니다."
    if category in {"cheongyang_weather_week", "cheongyang_weather_month"}:
        return "전망은 변동될 수 있으므로 실제 작업 전에는 새로 발표된 단기 예보를 다시 확인해야 합니다."
    if category == "fertilizer_learning":
        return "실제 적용 전에는 작물, 토양검정, 시비량과 시기를 함께 확인해야 합니다."
    if category == "agriculture_news":
        return "후속 발표와 실제 농가 적용 여부를 확인하겠습니다."
    if category == "fertilizer_news":
        return "지원 규모, 제품·자재 변화와 현장 적용 조건을 원문에서 확인하겠습니다."
    return _ensure_sentence(_watch_message(label))


def _follow_up_question(label: str, stories: list[dict[str, Any]]) -> str:
    headline = stories[0]["headline"]
    subject = _headline_subject(headline)

    topic_hints = {
        "한국정치": f"{subject} 관련 주장 가운데 핵심 쟁점은 뭔가요?",
        "세계정세": "이 흐름이 국제 정세와 시장에 어떤 의미를 주나요?",
        "군사학": "군사적으로 보면 가장 먼저 읽어야 할 포인트는 뭔가요?",
        "무기체계": "실제 전력 변화로 이어질 가능성은 어떻게 보세요?",
        "AI": "이 변화가 산업 현장에 주는 신호는 뭔가요?",
        "양자": "이게 기술 전환 관점에서 왜 눈에 띄는 건가요?",
        "경제": "시장과 정책 측면에서 어디를 가장 먼저 봐야 할까요?",
    }
    return topic_hints.get(label, "이 가운데 먼저 짚어볼 지점은 뭔가요?")


def _fallback_message_summary(article: NewsItem) -> str:
    if article.category.startswith("cheongyang_weather_"):
        return _ensure_sentence(article.summary or article.title)
    condensed = _first_sentence(_condense_article(article))
    if _headline_overlap_ratio(article.title, condensed) >= 0.82 and article.summary:
        condensed = _first_sentence(_ensure_sentence(article.summary))
    return condensed


def _article_summary(category: str, article: NewsItem) -> str:
    if category == "fertilizer_learning":
        topic = article.title.split(":", 1)[-1].strip()
        return _limit_sentences(_learning_summary(topic, article.summary), 8)
    condensed = _condense_article(article)
    if category.startswith("cheongyang_weather_"):
        return _ensure_sentence(article.summary or condensed)
    if _is_title_only_article(article):
        return _ensure_sentence(article.title)
    return condensed


def _learning_summary(topic: str, source_summary: str) -> str:
    if "논문" in topic or "fertilizer" in topic:
        return _ensure_sentence(source_summary or "최근 비료 연구 논문을 바탕으로 학습합니다.") + " 연구 대상과 비교한 처리 조건을 먼저 확인해야 합니다. 실험 결과는 작물과 토양 조건에 따라 달라질 수 있습니다. 수량뿐 아니라 품질과 토양 양분 변화도 함께 봐야 합니다. 논문의 시비량을 청양 밭에 그대로 적용해서는 안 됩니다. 토양검정과 작물별 표준 시비량을 우선 확인해야 합니다. 효과와 함께 비용, 노동력, 환경 부담도 비교해야 합니다. 작은 면적에서 시험한 뒤 생육과 수확 결과를 기록하는 것이 안전합니다."
    return _ensure_sentence(source_summary or f"오늘은 {topic}을 공부합니다.") + " 비료의 역할은 작물의 생육 단계와 토양 상태에 따라 달라집니다. 같은 제품도 작물과 시기에 따라 필요한 양이 다를 수 있습니다. 토양검정 결과를 기준으로 부족한 성분을 보충해야 합니다. 한 번에 많이 주기보다 밑거름과 웃거름으로 나누는 편이 안전합니다. 과다 시비는 웃자람과 병해, 염류 집적을 일으킬 수 있습니다. 비가 오기 직전이나 한낮의 고온에는 시비를 피하는 것이 좋습니다. 사용 전 제품 표시와 작물별 권장량을 확인해야 합니다."


def _limit_sentences(text: str, limit: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentence for sentence in sentences[:limit] if sentence).strip()


def _verification_note(flags: list[str]) -> str:
    if not flags:
        return ""

    notes: list[str] = []
    if "numeric_claim" in flags:
        notes.append("숫자는 원문 확인 권장")
    if "quoted_claim" in flags:
        notes.append("인용은 원문 확인 권장")
    if "breaking_update" in flags:
        notes.append("속보성 이슈")
    if "sensitive_geopolitics" in flags:
        notes.append("민감 분야")
    return ", ".join(notes[:2])


def _condense_article(article: NewsItem) -> str:
    text = article.summary or ""
    text = _strip_repetition(text, article.title)
    text = _strip_repetition(text, article.source)
    text = re.sub(r"^\[[^\]]*기자\]\s*", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\b[\w.-]+\.[a-z]{2,}\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -:;,.")
    if len(text) < 18:
        return _headline_fallback(article.title)

    sentences = re.split(r"(?<=[.!?])\s*", text)
    summary = " ".join(sentence.strip() for sentence in sentences[:2] if sentence.strip())
    summary = re.sub(r"\s+", " ", summary).strip(" -:;,.")
    if (
        len(summary) < 18
        or summary.count("'") % 2 == 1
        or summary.count("“") != summary.count("”")
    ):
        return _headline_fallback(article.title)
    return _ensure_sentence(summary)


def _is_title_only_article(article: NewsItem) -> bool:
    detail = _strip_repetition(article.summary or "", article.title)
    detail = _strip_repetition(detail, article.source)
    return len(detail.strip(" -:;,.")) < 25


def _strip_repetition(text: str, fragment: str) -> str:
    if not text or not fragment:
        return text
    pattern = re.escape(fragment)
    stripped = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return stripped.strip()


def _headline_fallback(title: str) -> str:
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", title).strip()
    cleaned = cleaned.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")

    return _ensure_sentence(cleaned)


def _normalize_speaker(raw: str) -> str:
    speaker = raw.strip(" ,.-")
    speaker = re.sub(r",.*$", "", speaker).strip()
    if "…" in speaker:
        speaker = speaker.split("…")[-1].strip() or speaker
    if "·" in speaker and len(speaker) > 12:
        speaker = speaker.split("·")[0].strip()
    speaker = speaker.rstrip("에은는이가를을")
    return speaker or "관계자"


def _ensure_sentence(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""
    if cleaned[-1] not in {".", "!", "?"}:
        cleaned += "."
    return cleaned


def _why_it_matters(category: str, label: str) -> str:
    category_reasons = {
        "cheongyang_weather_today": "오늘의 기온·강수·바람은 작업 시간과 병해 관리, 관수 계획을 정하는 데 직접 영향을 줍니다.",
        "cheongyang_weather_week": "이번 주 기상 흐름을 알면 파종·정식·방제·수확 일정을 미리 조정할 수 있습니다.",
        "agriculture_news": "농가의 재배 일정, 판로, 농업 정책과 지역 농산물 수익성에 영향을 줄 수 있는 소식입니다.",
        "fertilizer_news": "비료 가격·공급·정책 변화는 생산비와 적정 시비 계획에 직접 영향을 줄 수 있습니다.",
        "fertilizer_learning": "비료의 원리와 사용 시기를 알면 과다 시비를 줄이고 작물 생육과 토양 상태를 함께 관리할 수 있습니다.",
    }
    if category in category_reasons:
        return category_reasons[category]
    reasons = {
        "한국정치": "정책 일정과 정치권의 힘의 균형에 직접 영향을 줄 수 있습니다.",
        "세계정세": "외교, 에너지, 공급망 흐름에 연쇄적으로 연결될 수 있습니다.",
        "군사학": "안보 환경과 군사적 긴장 수준을 판단하는 데 중요합니다.",
        "무기체계": "전력 변화와 방산 수요 방향을 가늠하는 신호가 됩니다.",
        "AI": "모델 경쟁과 산업 적용 속도를 판단하는 데 중요합니다.",
        "양자": "상용화 속도와 보안 기술 전환 시점을 가늠하는 재료가 됩니다.",
        "경제": "시장 심리와 정책 대응 전망에 바로 영향을 줄 수 있습니다.",
    }
    return reasons.get(label, "분야 흐름을 읽는 데 의미가 있습니다.")


def _watch_message(label: str) -> str:
    follow_ups = {
        "한국정치": "추가 공식 발표나 일정 확정 여부를 확인해보는 것이 좋겠습니다.",
        "세계정세": "후속 외교 일정과 국제 시장 반응을 함께 볼 필요가 있습니다.",
        "군사학": "현장 동향과 각국 공식 발표의 간극을 계속 확인해야 합니다.",
        "무기체계": "실전 배치 여부와 실제 계약 규모가 뒤따르는지 보겠습니다.",
        "AI": "실제 제품화와 기업 간 제휴가 이어지는지 지켜보겠습니다.",
        "양자": "기술 발표가 실사용 단계로 이어지는지 확인이 필요합니다.",
        "경제": "정책 대응과 시장 가격 변화를 함께 봐야 합니다.",
    }
    return follow_ups.get(label, "후속 기사에서 구체성이 더 붙는지 보겠습니다.")


def _render_message_digest(
    show_title: str,
    briefs: list[CategoryBrief],
    quiet_categories: list[str],
) -> str:
    lines = [
        f"# {show_title} 요약",
        "",
        "필요한 기사만 빠르게 찾아볼 수 있도록 핵심 제목과 육하원칙 중심의 기사 요약을 정리했습니다.",
    ]

    for brief in briefs:
        if not brief.stories:
            continue
        lines.append("")
        lines.append(f"## {brief.label}")
        for story in brief.stories:
            summary = _message_summary(story)
            meta = _message_meta(story)
            lines.append(f"- **{story['headline']}**")
            summary_label = "요약" if story.get("category", "").startswith("cheongyang_weather_") or story.get("category") == "fertilizer_learning" else "기사 요약"
            lines.append(f"  {summary_label}: {summary}")
            if meta:
                lines.append(f"  메모: {meta}")
            lines.append("")

    if quiet_categories:
        lines.append("")
        lines.append("## 저신호 분야")
        lines.append(f"- 오늘은 {', '.join(quiet_categories)} 분야에서 기준 점수를 넘는 특정 기사가 많지 않았습니다.")

    return "\n".join(lines).strip() + "\n"


def _message_summary(story: dict[str, Any]) -> str:
    if story.get("category") in {"cheongyang_weather_today", "cheongyang_weather_week", "cheongyang_weather_month", "fertilizer_learning"}:
        detailed = _ensure_sentence(str(story.get("article_summary") or story.get("message_summary") or ""))
        if detailed:
            return _emphasize_summary(detailed)
    if story.get("fallback_story"):
        candidate = _ensure_sentence(str(story.get("article_summary") or story.get("message_summary") or story.get("angle") or ""))
        return _emphasize_summary(candidate)

    headline = str(story.get("headline", ""))
    preferred_candidates = [
        ("message_summary", story.get("message_summary")),
        ("angle", _first_sentence(str(story.get("angle", "")))),
        ("why_it_matters", story.get("why_it_matters")),
    ]

    for source, raw_candidate in preferred_candidates:
        candidate = _ensure_sentence(str(raw_candidate or ""))
        if not candidate:
            continue
        if source == "message_summary" and _headline_overlap_ratio(headline, candidate) >= 0.72:
            continue
        if source == "angle" and _is_generic_digest_sentence(candidate):
            continue
        return _emphasize_summary(candidate)

    fallback = _ensure_sentence(
        str(
            story.get("message_summary")
            or story.get("angle")
            or story.get("why_it_matters")
            or ""
        )
    )
    return _emphasize_summary(fallback)


def _message_why(story: dict[str, Any]) -> str:
    why = _ensure_sentence(str(story.get("article_summary") or story.get("why_it_matters") or ""))
    if not why:
        return ""
    return why


def _message_meta(story: dict[str, Any]) -> str:
    bits: list[str] = []
    source = str(story.get("source") or "").strip()
    if source:
        bits.append(f"출처 {source}")
    cluster_size = int(story.get("cluster_size") or 1)
    if cluster_size > 1:
        bits.append(f"관련 기사 {cluster_size}건 묶음")
    verification_note = str(story.get("verification_note") or "").strip()
    if verification_note:
        bits.append(verification_note)
    return " | ".join(bits)


def _render_summary(
    *,
    config: AppConfig,
    run_dir: Path,
    start_utc: datetime,
    end_utc: datetime,
    news_by_category: dict[str, list[NewsItem]],
    selected_by_category: dict[str, list[NewsItem]],
    briefs: list[CategoryBrief],
    show: RadioShow,
    audio_metadata: dict[str, Any],
    telegram_metadata: dict[str, Any],
    quota_log: dict[str, Any],
) -> str:
    lines = [
        f"# {show.show_title}",
        "",
        f"- 실행 디렉터리: `{run_dir}`",
        f"- 시간 범위(UTC): `{start_utc.isoformat()}` ~ `{end_utc.isoformat()}`",
        f"- LLM 사용: `{config.llm_enabled}`",
        f"- TTS 사용: `{config.tts_enabled}`",
        f"- 오디오 생성: `{audio_metadata.get('generated', False)}`",
        f"- 텔레그램 전송: `{telegram_metadata.get('sent', False)}`",
        f"- TTS 모드: `{config.tts_quality_mode}`",
        f"- 점수 임계치: `{config.score_threshold}`",
        "",
        "## 기사 수집 현황",
    ]

    for category in CATEGORIES:
        total_count = len(news_by_category.get(category.key, []))
        selected = selected_by_category.get(category.key, [])
        selected_text = ", ".join(f"{article.score:.1f}" for article in selected) or "-"
        lines.append(
            f"- {category.label}: 전체 {total_count}건, 선정 {len(selected)}건, 점수 {selected_text}"
        )

    lines.append("")
    lines.append("## 브리프 개요")
    for brief in briefs:
        lines.append(f"- {brief.label}: {brief.lead}")

    if show.quiet_categories:
        lines.append("")
        lines.append("## 저신호 분야")
        lines.append(f"- {', '.join(show.quiet_categories)}")

    lines.append("")
    lines.append("## 쿼터 로그")
    lines.append(f"- 예상 텍스트 호출 수: `{quota_log['estimated_text_calls']}`")
    lines.append(f"- 예상 TTS 호출 수: `{quota_log['estimated_tts_calls']}`")
    lines.append(f"- TTS 비트레이트: `{config.tts_bitrate_kbps} kbps`")
    lines.append(f"- TTS 품질 모드: `{quota_log['tts_mode']}`")
    if telegram_metadata.get("target_type"):
        target_bits = [
            str(telegram_metadata.get("target_type")),
            str(telegram_metadata.get("target_title") or "").strip(),
        ]
        lines.append(f"- 텔레그램 대상: `{' / '.join(bit for bit in target_bits if bit)}`")
        if telegram_metadata.get("thread_id"):
            lines.append(f"- 텔레그램 스레드: `{telegram_metadata['thread_id']}`")

    lines.append("")
    lines.append("## 라디오 요약")
    lines.append(show.show_summary)
    lines.append("")
    lines.append("## 산출물")
    lines.append("- `news_items.json`")
    lines.append("- `selected_items.json`")
    lines.append("- `category_briefs.json`")
    lines.append("- `radio_show.json`")
    lines.append("- `radio_script.md`")
    lines.append("- `radio_script.txt`")
    lines.append("- `message_digest.md`")
    lines.append("- `index.html`")
    if audio_metadata.get("generated") and audio_metadata.get("path"):
        lines.append(f"- `{audio_metadata['path']}`")
    return "\n".join(lines).strip() + "\n"


def _write_audio_output(run_dir: Path, audio_bytes: bytes, mime_type: str, config: AppConfig) -> Path:
    lowered = mime_type.lower()
    if "audio/l16" in lowered or "codec=pcm" in lowered:
        sample_rate = _parse_sample_rate(lowered)
        pcm_bytes = _select_pcm_stream(audio_bytes)
        output_path = run_dir / "audio.mp3"
        output_path.write_bytes(_encode_mp3(pcm_bytes, sample_rate, config.tts_bitrate_kbps))
        return output_path

    output_path = run_dir / "audio.mp3"
    output_path.write_bytes(audio_bytes)
    return output_path


def _parse_sample_rate(mime_type: str) -> int:
    match = re.search(r"rate=(\d+)", mime_type)
    if match:
        return int(match.group(1))
    return 24000


def _emphasize_summary(text: str) -> str:
    if "**" in text:
        return text

    stripped = text.rstrip(".!?")
    if ", " in stripped:
        lead, rest = stripped.split(", ", 1)
        if 4 <= len(lead) <= 36 and rest:
            return f"**{lead}**, {rest}."

    return f"**{stripped}**."


def _first_sentence(text: str) -> str:
    cleaned = _ensure_sentence(text)
    match = re.match(r"(.+?[.!?])(?:\s|$)", cleaned)
    if match:
        return match.group(1)
    return cleaned


def _headline_overlap_ratio(headline: str, summary: str) -> float:
    headline_tokens = _title_tokens(headline)
    summary_tokens = _title_tokens(summary)
    if not headline_tokens or not summary_tokens:
        return 0.0
    return len(headline_tokens & summary_tokens) / len(headline_tokens)


def _is_generic_digest_sentence(text: str) -> bool:
    generic_phrases = (
        "입장을 내놨습니다",
        "움직임이 보도됐습니다",
        "흐름이 부각됐습니다",
        "판단하는 데 중요합니다",
        "영향을 줄 수 있습니다",
        "연쇄적으로 연결될 수 있습니다",
    )
    return any(phrase in text for phrase in generic_phrases)


def _select_pcm_stream(audio_bytes: bytes) -> bytes:
    trimmed = audio_bytes[: len(audio_bytes) - (len(audio_bytes) % 2)]
    if not trimmed:
        raise ValueError("Gemini TTS returned an empty PCM payload.")

    swapped = b"".join(
        trimmed[index + 1 : index + 2] + trimmed[index : index + 1]
        for index in range(0, len(trimmed), 2)
    )
    return trimmed if _pcm_score(trimmed) <= _pcm_score(swapped) else swapped


def _pcm_score(pcm_bytes: bytes) -> float:
    probe = pcm_bytes[: min(len(pcm_bytes), 24000 * 2 * 12)]
    samples = array.array("h")
    samples.frombytes(probe)
    if not samples:
        return float("inf")

    if len(samples) > 12000:
        samples = samples[::4]

    mean_abs = sum(abs(sample) for sample in samples) / len(samples)
    delta = sum(abs(samples[index] - samples[index - 1]) for index in range(1, len(samples))) / max(len(samples) - 1, 1)
    clip_ratio = sum(1 for sample in samples if abs(sample) >= 30000) / len(samples)
    return (delta / max(mean_abs, 1.0)) + (clip_ratio * 3.0)


def _encode_mp3(pcm_bytes: bytes, sample_rate: int, bitrate_kbps: int) -> bytes:
    import lameenc

    encoder = lameenc.Encoder()
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1)
    encoder.set_bit_rate(bitrate_kbps)
    encoder.set_quality(5)
    return encoder.encode(pcm_bytes) + encoder.flush()


def _quota_log(config: AppConfig, selected_by_category: dict[str, list[NewsItem]]) -> dict[str, Any]:
    populated_categories = sum(1 for items in selected_by_category.values() if items)
    return {
        "estimated_text_calls": (populated_categories + 1) if config.llm_enabled else 0,
        "estimated_tts_calls": (config.tts_retry_count + 1) if config.tts_enabled else 0,
        "tts_mode": config.tts_quality_mode,
    }


def _public_links(
    config: AppConfig,
    run_dir: Path,
    audio_path: Path | None,
) -> dict[str, str] | None:
    base_url = (config.public_archive_base_url or "").strip()
    if not base_url:
        return None

    base = base_url.rstrip("/") + "/"
    run_prefix = f"{run_dir.name}/"
    links = {
        "archive": urljoin(base, f"{run_prefix}index.html"),
        "summary": urljoin(base, f"{run_prefix}summary.md"),
        "digest": urljoin(base, f"{run_prefix}message_digest.md"),
    }
    if audio_path is not None and audio_path.exists():
        links["audio"] = urljoin(base, f"{run_prefix}{audio_path.name}")
    return links


def _write_run_archive_page(
    run_dir: Path,
    show: RadioShow,
    briefs: list[CategoryBrief],
    audio_metadata: dict[str, Any],
) -> None:
    sections: list[str] = [
        "<!doctype html>",
        "<html lang='ko'>",
        "<head>",
        "<meta charset='utf-8'>",
        f"<title>{html.escape(show.show_title)}</title>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<style>",
        "body{font-family:Segoe UI,Apple SD Gothic Neo,sans-serif;max-width:960px;margin:40px auto;padding:0 20px;line-height:1.6;background:#f7f8fb;color:#101828;}",
        "main{background:#fff;border:1px solid #e4e7ec;border-radius:18px;padding:28px 32px;box-shadow:0 10px 30px rgba(16,24,40,.06);}",
        "h1,h2{margin-top:0;}",
        ".meta,.story{border-top:1px solid #eaecf0;padding-top:16px;margin-top:16px;}",
        ".eyebrow{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:999px;padding:4px 10px;font-size:13px;font-weight:600;}",
        "audio{width:100%;margin:16px 0;}",
        "a{color:#1d4ed8;text-decoration:none;}",
        "</style>",
        "</head>",
        "<body><main>",
        f"<span class='eyebrow'>Morning Radio Archive</span><h1>{html.escape(show.show_title)}</h1>",
        f"<p>{html.escape(show.show_summary)}</p>",
    ]

    if audio_metadata.get("generated") and audio_metadata.get("path"):
        audio_file = html.escape(str(audio_metadata["path"]))
        sections.append(f"<audio controls src='{audio_file}'></audio>")

    sections.append("<div class='meta'><h2>Files</h2><ul>")
    for filename in ("summary.md", "message_digest.md", "radio_script.md", "radio_script.txt", "radio_show.json"):
        sections.append(f"<li><a href='{html.escape(filename)}'>{html.escape(filename)}</a></li>")
    if audio_metadata.get("generated") and audio_metadata.get("path"):
        audio_name = str(audio_metadata["path"])
        sections.append(f"<li><a href='{html.escape(audio_name)}'>{html.escape(audio_name)}</a></li>")
    sections.append("</ul></div>")

    for brief in briefs:
        if not brief.stories:
            continue
        sections.append(f"<section class='story'><h2>{html.escape(brief.label)}</h2>")
        sections.append(f"<p>{html.escape(brief.lead)}</p>")
        sections.append("<ul>")
        for story in brief.stories:
            sections.append(f"<li><strong>{html.escape(str(story.get('headline', '')))}</strong><br>")
            plain_summary = re.sub(r"\*\*(.*?)\*\*", r"\1", _message_summary(story))
            sections.append(f"{html.escape(plain_summary)}<br>")
            sections.append(f"<small>{html.escape(_message_why(story))}</small></li>")
        sections.append("</ul></section>")

    sections.append("</main></body></html>")
    (run_dir / "index.html").write_text("\n".join(sections), encoding="utf-8")


def _write_archive_index(output_dir: Path, limit: int) -> None:
    run_dirs = sorted(
        [path for path in output_dir.iterdir() if path.is_dir() and re.fullmatch(r"\d{8}-\d{6}", path.name)],
        key=lambda path: path.name,
        reverse=True,
    )[:limit]
    sections = [
        "<!doctype html>",
        "<html lang='ko'>",
        "<head><meta charset='utf-8'><title>Morning Radio Archive</title>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<style>body{font-family:Segoe UI,Apple SD Gothic Neo,sans-serif;max-width:960px;margin:40px auto;padding:0 20px;background:#f7f8fb;color:#101828;}main{background:#fff;border:1px solid #e4e7ec;border-radius:18px;padding:28px 32px;}li{margin:12px 0;}a{color:#1d4ed8;text-decoration:none;}</style>",
        "</head><body><main><h1>Morning Radio Archive</h1><ul>",
    ]
    for run_dir in run_dirs:
        summary_path = run_dir / "summary.md"
        title = run_dir.name
        if summary_path.exists():
            first_line = summary_path.read_text(encoding="utf-8").splitlines()[0].replace("# ", "").strip()
            if first_line:
                title = first_line
        sections.append(
            f"<li><a href='{html.escape(run_dir.name)}/index.html'>{html.escape(title)}</a> <small>({html.escape(run_dir.name)})</small></li>"
        )
    sections.append("</ul></main></body></html>")
    (output_dir / "index.html").write_text("\n".join(sections), encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
