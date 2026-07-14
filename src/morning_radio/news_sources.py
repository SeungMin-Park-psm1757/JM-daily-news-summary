from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus, urlparse

import feedparser
import requests
from dateutil import parser as date_parser

from morning_radio.models import CategoryDefinition, NewsItem

USER_AGENT = (
    "Mozilla/5.0 (compatible; MorningRadio/0.1; +https://github.com/actions)"
)

GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"

DOMAIN_BOOSTS = {
    "reuters.com": 8.0,
    "apnews.com": 8.0,
    "bloomberg.com": 8.0,
    "ft.com": 6.0,
    "wsj.com": 6.0,
    "economist.com": 5.0,
    "yna.co.kr": 7.0,
    "joongang.co.kr": 4.0,
    "khan.co.kr": 4.0,
    "mk.co.kr": 4.0,
    "hankyung.com": 4.0,
    "chosun.com": 4.0,
    "donga.com": 4.0,
    "sedaily.com": 4.0,
}

LOW_SIGNAL_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "blog",
    "tistory.com",
    "brunch.co.kr",
)

SOURCE_BOOSTS = {
    "reuters": 8,
    "associated press": 8,
    "ap": 8,
    "bloomberg": 8,
    "연합뉴스": 7,
    "연합인포맥스": 6,
    "financial times": 6,
    "wsj": 6,
    "wall street journal": 6,
    "economist": 5,
    "중앙일보": 4,
    "한겨레": 4,
    "한국경제": 4,
    "매일경제": 4,
    "조선일보": 4,
    "동아일보": 4,
    "경향신문": 4,
    "서울경제": 4,
    "아시아경제": 3,
    "문화일보": 3,
}

GLOBAL_PRIORITY_TERMS = (
    "속보",
    "긴급",
    "합의",
    "회담",
    "정상",
    "제재",
    "관세",
    "휴전",
    "공격",
    "전쟁",
    "파병",
    "미사일",
    "배치",
    "훈련",
    "금리",
    "환율",
    "inflation",
    "tariff",
    "oil",
    "fed",
    "openai",
    "deepmind",
    "anthropic",
    "nvidia",
    "quantum",
)

GLOBAL_PENALTY_TERMS = (
    "opinion",
    "사설",
    "칼럼",
    "기고",
    "홍보",
    "광고",
    "sponsored",
    "행사",
    "개최",
    "세미나",
    "포럼",
    "박람회",
    "presented by",
)

CATEGORIES: tuple[CategoryDefinition, ...] = (
    CategoryDefinition(
        key="korea_politics",
        label="한국정치",
        queries=(
            "한국 정치 OR 대통령 OR 국회 OR 여당 OR 야당 when:1d",
            "헌법재판소 OR 선거 OR 총리 OR 내각 when:1d",
        ),
        priority_terms=("대통령", "국회", "여야", "개헌", "선거", "내각", "헌재"),
        penalty_terms=("지방행사", "축제", "개최"),
    ),
    CategoryDefinition(
        key="global_affairs",
        label="세계정세",
        queries=(
            "외교 OR 정상회담 OR 제재 OR 중동 OR 유럽 OR 중국 OR 미국 when:1d",
            "world affairs OR diplomacy OR summit OR sanctions when:1d",
        ),
        priority_terms=("중동", "정상회담", "외교", "제재", "관세", "유가", "중국", "미국"),
        penalty_terms=("지역축제", "관광", "개최", "wbc", "축구", "야구", "농구", "선수", "리그"),
    ),
    CategoryDefinition(
        key="military_strategy",
        label="군사학",
        queries=(
            "군사 OR 안보 OR conflict OR 전쟁 OR 훈련 when:1d",
            "military strategy OR defense posture OR military exercise when:1d",
        ),
        priority_terms=("전쟁", "휴전", "훈련", "병력", "안보", "공습", "종전"),
        penalty_terms=("opinion", "주가"),
    ),
    CategoryDefinition(
        key="weapon_systems",
        label="무기체계",
        queries=(
            "무기체계 OR missile OR drone OR radar OR fighter jet when:1d",
            "air defense OR naval weapons OR hypersonic when:1d",
        ),
        priority_terms=("미사일", "방공", "드론", "레이더", "전투기", "잠수함", "hypersonic"),
        penalty_terms=("opinion", "stocks", "주가"),
    ),
    CategoryDefinition(
        key="artificial_intelligence",
        label="AI",
        queries=(
            "AI OR 인공지능 OR LLM OR generative AI when:1d",
            "OpenAI OR Google DeepMind OR Anthropic OR Nvidia AI when:1d",
        ),
        priority_terms=("openai", "deepmind", "anthropic", "nvidia", "llm", "추론", "모델"),
        penalty_terms=("행사", "개최", "세미나", "홍보", "presented by", "모집", "program"),
    ),
    CategoryDefinition(
        key="quantum",
        label="양자",
        queries=(
            "양자 OR quantum computing OR quantum chip when:1d",
            "quantum error correction OR superconducting qubit OR photonic quantum when:1d",
        ),
        priority_terms=("quantum", "qubit", "양자컴퓨팅", "오류정정", "칩", "pqc"),
        penalty_terms=("홍보", "행사", "개최"),
    ),
    CategoryDefinition(
        key="economy",
        label="경제",
        queries=(
            "경제 OR inflation OR interest rate OR 환율 OR stock market when:1d",
            "oil prices OR tariffs OR trade OR central bank when:1d",
        ),
        priority_terms=("금리", "환율", "인플레이션", "관세", "유가", "수출", "중앙은행"),
        penalty_terms=("코인광고", "세미나", "개최"),
    ),
)


def _build_feed_url(query: str) -> str:
    return GOOGLE_NEWS_SEARCH.format(query=quote_plus(query))


# Daily briefing scope: Cheongyang weather plus agriculture and fertilizer.
CATEGORIES = (
    CategoryDefinition("cheongyang_weather_today", "청양 오늘 날씨", ()),
    CategoryDefinition("cheongyang_weather_week", "청양 이번 주 날씨", ()),
    CategoryDefinition(
        "agriculture_news", "농사·농업 뉴스",
        ("농사 농업 농촌 작물 재배 when:1d", "농업 정책 농업기술 농산물 when:1d"),
        ("농업", "농사", "작물", "농촌", "재배", "농산물"),
    ),
    CategoryDefinition(
        "fertilizer_news", "비료 관련 새 소식",
        ("비료 fertilizer 농업 when:1d", "유기질비료 무기질비료 비료 가격 when:1d"),
        ("비료", "질소", "인산", "칼리", "퇴비", "fertilizer"),
    ),
    CategoryDefinition(
        "fertilizer_learning", "오늘의 비료 공부",
        ("비료 사용법 토양 양분 작물 when:7d", "비료 종류 시비 방법 농업기술 when:7d"),
        ("비료", "시비", "토양", "양분", "질소", "인산", "칼리"),
    ),
)


def _clean_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_source(entry: feedparser.FeedParserDict, title: str) -> tuple[str, str]:
    if " - " in title:
        maybe_title, maybe_source = title.rsplit(" - ", 1)
        if maybe_source:
            return maybe_title.strip(), maybe_source.strip()
    source = ""
    if "source" in entry and getattr(entry.source, "title", None):
        source = str(entry.source.title).strip()
    return title.strip(), source or "Unknown source"


def _parse_published(entry: feedparser.FeedParserDict) -> datetime | None:
    for field in ("published", "updated", "pubDate"):
        raw = entry.get(field)
        if raw:
            try:
                parsed = date_parser.parse(str(raw))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC)
            except (ValueError, TypeError, OverflowError):
                continue
    return None


def _fingerprint(title: str, source: str) -> str:
    normalized = re.sub(r"\s+", " ", title.lower()).strip()
    normalized = re.sub(r"[\"'`“”‘’]", "", normalized)
    payload = f"{normalized}|{source.lower().strip()}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _count_hits(text: str, terms: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term.lower() in lowered)


def _source_boost(source: str) -> float:
    lowered = source.lower()
    for key, value in SOURCE_BOOSTS.items():
        if key in lowered:
            return float(value)
    if "." in source:
        return -1.0
    return 0.0


def _extract_domain(url: str) -> str:
    host = urlparse(url).netloc.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def _domain_boost(url: str) -> float:
    domain = _extract_domain(url)
    if not domain:
        return 0.0
    for key, value in DOMAIN_BOOSTS.items():
        if domain.endswith(key):
            return value
    if any(signal in domain for signal in LOW_SIGNAL_DOMAINS):
        return -4.0
    return 0.0


def _source_weight(source: str, url: str) -> float:
    return round(_source_boost(source) + _domain_boost(url), 1)


def _score_article(
    *,
    category: CategoryDefinition,
    title: str,
    summary: str,
    source: str,
    url: str,
    published_at: datetime,
    now: datetime,
) -> float:
    combined = f"{title} {summary}"
    age_hours = max((now - published_at).total_seconds() / 3600.0, 0.0)
    recency_score = max(3.0, 20.0 - (age_hours * 0.9))
    priority_score = min(_count_hits(combined, GLOBAL_PRIORITY_TERMS) * 4.0, 20.0)
    category_score = min(_count_hits(combined, category.priority_terms) * 6.0, 18.0)
    penalty_score = min(_count_hits(combined, GLOBAL_PENALTY_TERMS) * 10.0, 20.0)
    category_penalty = min(_count_hits(combined, category.penalty_terms) * 8.0, 16.0)
    summary_bonus = 4.0 if summary else 0.0

    total = 18.0 + recency_score + priority_score + category_score + summary_bonus + _source_weight(source, url)
    total -= penalty_score + category_penalty
    return round(max(0.0, min(total, 100.0)), 1)


def verification_flags_for_article(*, category_key: str, title: str, summary: str) -> list[str]:
    combined = f"{title} {summary}"
    flags: list[str] = []
    if re.search(r"\b\d[\d,./%]*\b", combined):
        flags.append("numeric_claim")
    if any(mark in combined for mark in ('"', "“", "”", "'")):
        flags.append("quoted_claim")
    if category_key in {"global_affairs", "military_strategy", "weapon_systems"}:
        flags.append("sensitive_geopolitics")
    if "속보" in combined.lower() or "breaking" in combined.lower():
        flags.append("breaking_update")
    return flags


def _extract_meta_content(html_text: str, key: str, attribute: str) -> str:
    pattern = rf'<meta[^>]+{attribute}=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']'
    match = re.search(pattern, html_text, flags=re.IGNORECASE)
    if match:
        return html.unescape(match.group(1)).strip()
    reverse_pattern = rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+{attribute}=["\']{re.escape(key)}["\']'
    match = re.search(reverse_pattern, html_text, flags=re.IGNORECASE)
    if match:
        return html.unescape(match.group(1)).strip()
    return ""


def _clean_snippet(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:500]


def fetch_category_news(
    category: CategoryDefinition,
    *,
    hours_back: int,
    per_query_limit: int,
    now: datetime | None = None,
) -> list[NewsItem]:
    reference_time = now or datetime.now(tz=UTC)
    cutoff = reference_time - timedelta(hours=hours_back)
    collected: dict[str, NewsItem] = {}

    if category.key == "fertilizer_learning":
        topics = (
            "질소 비료의 역할과 과다 시비", "인산 비료와 뿌리 발달",
            "칼리 비료와 작물의 병해·수분 관리", "퇴비와 유기질비료",
            "토양검정과 맞춤형 시비", "밑거름과 웃거름의 차이",
            "비료 혼용과 시비 시기",
        )
        topic = topics[reference_time.astimezone().timetuple().tm_yday % len(topics)]
        category = CategoryDefinition(category.key, category.label, (topic,), category.priority_terms)

    if not category.queries:
        try:
            response = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": 36.4592,
                    "longitude": 126.8022,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "Asia/Seoul",
                    "forecast_days": 7,
                },
                timeout=20,
            )
            response.raise_for_status()
            weather = response.json()
            current = weather.get("current", {})
            daily = weather.get("daily", {})
            if category.key.endswith("today"):
                title = "청양 오늘 날씨"
                summary = (
                    f"현재 기온 {current.get('temperature_2m')}°C, 습도 {current.get('relative_humidity_2m')}%, "
                    f"풍속 {current.get('wind_speed_10m')}km/h입니다. 날씨 코드 {current.get('weather_code')}입니다."
                )
            else:
                title = "청양 이번 주 날씨 개요"
                highs = daily.get("temperature_2m_max", [])
                lows = daily.get("temperature_2m_min", [])
                rain = daily.get("precipitation_probability_max", [])
                summary = f"앞으로 7일 최고기온 {highs}, 최저기온 {lows}, 강수확률 {rain}의 전망입니다."
            item = NewsItem(
                category=category.key, title=title, source="Open-Meteo",
                source_domain="open-meteo.com", url="https://open-meteo.com/",
                published_at=reference_time, summary=summary, query="청양 날씨",
                fingerprint=_fingerprint(title, "Open-Meteo"), score=100.0,
                source_weight=10.0, verification_flags=["forecast_data"],
            )
            return [item]
        except (requests.RequestException, ValueError, TypeError):
            return []

    for query in category.queries:
        url = _build_feed_url(query)
        try:
            response = requests.get(
                url,
                timeout=20,
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml"},
            )
            response.raise_for_status()
        except requests.RequestException:
            continue

        parsed = feedparser.parse(response.text)
        for entry in parsed.entries[: per_query_limit * 2]:
            published_at = _parse_published(entry)
            if not published_at or published_at < cutoff:
                continue

            raw_title = str(entry.get("title", "")).strip()
            clean_title, source = _extract_source(entry, raw_title)
            if not clean_title:
                continue

            item = NewsItem(
                category=category.key,
                title=clean_title,
                source=source,
                source_domain=_extract_domain(str(entry.get("link", "")).strip()),
                url=str(entry.get("link", "")).strip(),
                published_at=published_at,
                summary=_clean_html(entry.get("summary", "")),
                query=query,
                fingerprint=_fingerprint(clean_title, source),
                score=_score_article(
                    category=category,
                    title=clean_title,
                    summary=_clean_html(entry.get("summary", "")),
                    source=source,
                    url=str(entry.get("link", "")).strip(),
                    published_at=published_at,
                    now=reference_time,
                ),
                source_weight=_source_weight(source, str(entry.get("link", "")).strip()),
                verification_flags=verification_flags_for_article(
                    category_key=category.key,
                    title=clean_title,
                    summary=_clean_html(entry.get("summary", "")),
                ),
            )
            collected.setdefault(item.fingerprint, item)

    items = sorted(
        collected.values(),
        key=lambda article: (article.score, article.published_at),
        reverse=True,
    )
    return items[:per_query_limit]


def collect_news(
    *,
    hours_back: int,
    per_query_limit: int,
    now: datetime | None = None,
) -> dict[str, list[NewsItem]]:
    grouped: dict[str, list[NewsItem]] = {}
    global_seen: set[str] = set()

    for category in CATEGORIES:
        items = []
        for article in fetch_category_news(
            category,
            hours_back=hours_back,
            per_query_limit=per_query_limit,
            now=now,
        ):
            title_key = article.title.casefold()
            if title_key in global_seen:
                continue
            global_seen.add(title_key)
            items.append(article)
        grouped[category.key] = items

    return grouped


def flatten_news(news_by_category: dict[str, list[NewsItem]]) -> list[NewsItem]:
    flat: list[NewsItem] = []
    for items in news_by_category.values():
        flat.extend(items)
    return sorted(flat, key=lambda item: item.published_at, reverse=True)


def category_labels() -> dict[str, str]:
    return {category.key: category.label for category in CATEGORIES}


def enrich_articles(articles: list[NewsItem]) -> None:
    for article in articles:
        try:
            response = requests.get(
                article.url,
                timeout=20,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
        except requests.RequestException:
            continue

        article.resolved_url = response.url
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.lower():
            continue

        html_text = response.text[:250000]
        description = (
            _extract_meta_content(html_text, "og:description", "property")
            or _extract_meta_content(html_text, "description", "name")
            or _extract_meta_content(html_text, "twitter:description", "name")
        )
        if description:
            article.summary = _clean_snippet(description)
