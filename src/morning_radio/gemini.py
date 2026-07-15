from __future__ import annotations

import json
import re
import time
from typing import Any

from google import genai
from google.genai import errors
from google.genai import types

from morning_radio.config import AppConfig
from morning_radio.models import CategoryBrief, NewsItem, RadioShow


def _extract_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return text

    candidates = getattr(response, "candidates", None) or []
    parts: list[str] = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            value = getattr(part, "text", None)
            if value:
                parts.append(value)
    return "\n".join(parts).strip()


def _extract_json_payload(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```json\s*", "", cleaned)
        cleaned = re.sub(r"^```\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Model did not return a JSON object.")
    return json.loads(cleaned[start : end + 1])


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _markdown_to_plaintext(markdown: str) -> str:
    text = markdown.strip()
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.strip()


def _attach_story_metadata(stories: list[dict[str, Any]], articles: list[NewsItem]) -> list[dict[str, Any]]:
    available = articles.copy()
    normalized_stories: list[dict[str, Any]] = []

    for story in stories:
        normalized = dict(story)
        headline = str(normalized.get("headline", "")).strip()
        article = next((item for item in available if item.title == headline), None)
        if article is None and available:
            article = available[0]
        if article is not None:
            normalized.setdefault("headline", article.title)
            normalized.setdefault("source", article.source)
            normalized.setdefault("source_domain", article.source_domain)
            normalized.setdefault("source_urls", [article.resolved_url or article.url])
            normalized.setdefault("score", article.score)
            normalized.setdefault("source_weight", article.source_weight)
            normalized.setdefault("cluster_size", article.cluster_size)
            normalized.setdefault("verification_flags", article.verification_flags or [])
            normalized.setdefault("verification_note", "")
            normalized.setdefault("category", article.category)
            normalized.setdefault("article_summary", normalized.get("why_it_matters", ""))
            if article in available:
                available.remove(article)
        normalized_stories.append(normalized)

    return normalized_stories


class GeminiEditor:
    def __init__(self, config: AppConfig) -> None:
        if not config.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiEditor.")
        self.config = config
        self.client = genai.Client(api_key=config.gemini_api_key)

    def _generate_json(
        self,
        *,
        model: str,
        system_instruction: str,
        prompt: str,
        max_output_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        response = self.client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                systemInstruction=system_instruction,
                temperature=temperature,
                maxOutputTokens=max_output_tokens,
                responseMimeType="application/json",
            ),
        )
        return _extract_json_payload(_extract_text(response))

    def create_category_brief(
        self,
        *,
        category: str,
        label: str,
        articles: list[NewsItem],
        max_story_count: int,
    ) -> CategoryBrief:
        serializable_items = [
            {
                "title": article.title,
                "source": article.source,
                "source_domain": article.source_domain,
                "published_at": article.published_at.isoformat(),
                "summary": article.summary,
                "url": article.resolved_url or article.url,
                "score": article.score,
                "source_weight": article.source_weight,
                "cluster_size": article.cluster_size,
                "verification_flags": article.verification_flags or [],
            }
            for article in articles
        ]
        system_instruction = (
            "You are a careful Korean morning radio editor. "
            "Use only the supplied article metadata. "
            "Summarize substance, not headlines. "
            "Do not repeat titles or source names in angle unless absolutely necessary. "
            "If specifics are uncertain, explicitly say details are still developing. "
            "Never invent quotes, figures, motives, or classified context."
        )
        category_guidance = {
            "cheongyang_weather_today": "날씨 코드나 API 설명문을 반복하지 말고 기온, 강수 가능성, 바람을 농작업 관점에서 설명하세요.",
            "cheongyang_weather_week": "7일 전망의 기온 범위와 비가 올 가능성이 높은 시기를 짚고, 농작업 일정에 미칠 영향을 설명하세요.",
            "cheongyang_weather_month": "기상캐스터처럼 이번 달 또는 다음 달의 전반적인 기온·강수 흐름과 불확실성을 4-5문장으로 설명하세요. 농작업의 구체적 날짜를 단정하지 마세요.",
            "agriculture_news": "정책·기술·판로·재배·병해충 중 무엇이 바뀌었는지와 청양 농가에 적용할 때의 의미를 구분하세요.",
            "fertilizer_news": "비료 가격·공급·정책·제품·시비 기술 중 핵심 변화를 구체적으로 설명하고 사실과 전망을 구분하세요.",
            "fertilizer_learning": "초보 농업인도 이해하도록 용어를 풀어 쓰고, 역할·사용 시기·주의할 점을 실제 재배 상황과 연결하세요.",
        }.get(category, "무엇이 바뀌었고 농가에 어떤 영향이 있는지 구체적으로 설명하세요.")
        prompt = f"""
Category: {label} ({category})
Goal: Build a Korean morning radio brief using only the supplied article metadata.
Category guidance: {category_guidance}
Return exactly one JSON object.

Required JSON shape:
{{
  "lead": "2-4 Korean sentences",
  "stories": [
    {{
      "headline": "string",
      "angle": "2-3 Korean sentences",
      "message_summary": "1 Korean sentence for messenger delivery",
      "article_summary": "2-3 Korean sentences covering who, what, when, where, why, and how when available",
      "verification_note": "short Korean note or empty string",
      "source_urls": ["url"]
    }}
  ],
  "watch": "1-2 Korean sentences"
}}

Rules:
- Write all text fields in natural Korean.
- Pick up to {max_story_count} stories, prioritizing high score, reliable sources, and bigger clusters.
- `lead` should summarize the category's main movement, not list headlines, and add one extra sentence of context or implication.
- `angle` should explain the actual development, not rephrase the headline, and should be slightly more explanatory than a terse bulletin.
- `message_summary` should be compact, readable in a messenger, avoid headline duplication, and bold only the 1-2 most important changes using **...**.
- `article_summary` must be a factual 2-3 sentence summary using the six journalistic questions when the metadata supports them. For `cheongyang_weather_month`, use 4-5 sentences; for `fertilizer_learning`, use 7-8 sentences. Do not evaluate importance and never use generic phrases such as "분야 흐름을 읽는 데 의미가 있습니다".
- For `fertilizer_learning`, write 7-8 sentences at an introductory-to-intermediate level. On Friday, explain the research question, method, main finding, limitations, and practical farm implication of the latest supplied fertilizer paper.
- `verification_note` should be empty if not needed. Use a short note such as "숫자와 인용은 원문 확인 필요" only when the metadata suggests extra caution.
- Never invent quotes, figures, motives, battlefield details, or unnamed-source claims.
- If details are thin, say the story is still developing.

Articles:
{_json_dumps(serializable_items)}
""".strip()

        payload = self._generate_json(
            model=self.config.triage_model,
            system_instruction=system_instruction,
            prompt=prompt,
            max_output_tokens=self.config.max_output_tokens,
            temperature=0.25,
        )

        stories = _attach_story_metadata(list(payload.get("stories", []))[:max_story_count], articles)

        return CategoryBrief(
            category=category,
            label=label,
            lead=str(payload.get("lead", "")).strip(),
            stories=stories,
            watch=str(payload.get("watch", "")).strip(),
        )

    def create_radio_show(
        self,
        *,
        briefs: list[CategoryBrief],
        quiet_categories: list[str],
        opening_pair: tuple[str, str],
        start_iso: str,
        end_iso: str,
    ) -> RadioShow:
        system_instruction = (
            "You are writing a Korean two-person morning news radio script. "
            "The voice should feel calm, informed, concise, and conversational. "
            "The host is a composed male main anchor who speaks in short, clean setups. "
            "The analyst is a bright female commentator who responds crisply, adds context, and occasionally reacts with brief natural bridge phrases. "
            f"Use the exact speaker labels {self.config.host_name} and {self.config.analyst_name} "
            "on every dialogue line. "
            "Summarize meaning and implications, not headlines. "
            "For weather sections, speak like a Korean TV weathercaster: give morning, afternoon, and night details, mention rain or snow amounts when present, and omit an importance assessment. "
            "Use only facts present in the supplied briefs. Never invent weather values, dates, article facts, quotations, causes, or outcomes. "
            "Keep the exchange feeling like live radio banter rather than a long monologue. "
            "Avoid hype, avoid unverified claims, and mention uncertainty when needed."
        )

        prompt = f"""
Write a Korean morning radio script covering the last 24 hours.
Time window: {start_iso} ~ {end_iso}
Return exactly one JSON object.

Required JSON shape:
{{
  "show_title": "string",
  "show_summary": "up to 2 Korean sentences",
  "estimated_minutes": 5-7,
  "script_markdown": "markdown string"
}}

Script rules:
- Use the exact speaker labels `{self.config.host_name}:` and `{self.config.analyst_name}:`.
- Use the opening pair exactly as written.
- For each category, follow this rhythm:
  1. HOST setup question
  2. ANALYST answer in 2-4 sentences
  3. HOST short follow-up
  4. ANALYST answer in 2-3 sentences that explains why it matters or what to watch
- Aim for a final spoken runtime around five minutes at a brisk morning-radio pace.
- Make it feel like polished live radio: crisp back-and-forth, brief acknowledgements, and no long monologues.
- Vary transitions and category handoffs so the show does not sound repetitive.
- Let the host sound steady and framing-focused; let the analyst sound quick, bright, and insight-driven.
- Do not mechanically repeat headlines.
- Focus on what happened, who was involved, what changed, and what comes next; avoid generic importance statements.
- Let each category breathe slightly longer than a headline recap by adding one more sentence of context or consequence.
- For news and fertilizer updates, explain who did what, what changed, when or where it happened, and the stated reason or next step when supplied. Do not turn every item into a generic importance statement.
- For weather, preserve the supplied time-window and figures exactly. Do not add a percentage or amount that is not in the brief.
- If the supplied brief is thin, say that the available source metadata is limited instead of filling the gap with plausible-sounding details.
- Do not include operational filler such as "we picked the top three stories."
- If `quiet_categories` is not empty, mention those categories once near the end in a single short exchange.
- Do not include URLs in the script.
- No investment advice, sensationalism, or overconfident claims.
- For weather, use natural broadcast phrasing and do not say "why it matters". For the daily study, explain the concept at an introductory-to-intermediate level.

opening_pair:
{_json_dumps(list(opening_pair))}

quiet_categories:
{_json_dumps(quiet_categories)}

briefs:
{_json_dumps([brief.to_dict() for brief in briefs])}
""".strip()

        payload = self._generate_json(
            model=self.config.editor_model,
            system_instruction=system_instruction,
            prompt=prompt,
            max_output_tokens=self.config.max_output_tokens + 1024,
            temperature=0.45,
        )
        script_markdown = str(payload.get("script_markdown", "")).strip()
        return RadioShow(
            show_title=str(payload.get("show_title", "Morning Radio Briefing")).strip(),
            show_summary=str(payload.get("show_summary", "")).strip(),
            estimated_minutes=int(payload.get("estimated_minutes", 8)),
            script_markdown=script_markdown,
            script_plaintext=_markdown_to_plaintext(script_markdown),
            quiet_categories=quiet_categories,
        )

    def generate_audio(self, script_text: str) -> tuple[bytes, str]:
        attempts = self.config.tts_retry_count + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._generate_audio_once(script_text)
            except errors.ClientError as exc:
                last_error = exc
                status_code = getattr(exc, "status_code", None)
                is_retryable = status_code == 429 or "429" in str(exc)
                if attempt >= attempts - 1 or not is_retryable:
                    raise
                time.sleep(_retry_delay_seconds(str(exc), self.config.tts_retry_delay_seconds))
            except ValueError as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    raise
                time.sleep(2)
        raise ValueError(f"Gemini TTS failed after retries: {last_error}")

    def _generate_audio_once(self, script_text: str) -> tuple[bytes, str]:
        response = self.client.models.generate_content(
            model=self.config.tts_model,
            contents=self._build_tts_prompt(script_text),
            config=types.GenerateContentConfig(
                responseModalities=["AUDIO"],
                speechConfig=types.SpeechConfig(
                    languageCode="ko-KR",
                    multiSpeakerVoiceConfig=types.MultiSpeakerVoiceConfig(
                        speakerVoiceConfigs=[
                            types.SpeakerVoiceConfig(
                                speaker=self.config.host_name,
                                voiceConfig=types.VoiceConfig(
                                    prebuiltVoiceConfig=types.PrebuiltVoiceConfig(
                                        voiceName=self.config.host_voice,
                                    ),
                                ),
                            ),
                            types.SpeakerVoiceConfig(
                                speaker=self.config.analyst_name,
                                voiceConfig=types.VoiceConfig(
                                    prebuiltVoiceConfig=types.PrebuiltVoiceConfig(
                                        voiceName=self.config.analyst_voice,
                                    ),
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        )

        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", []) or []:
                inline_data = getattr(part, "inline_data", None)
                if inline_data and getattr(inline_data, "data", None):
                    return inline_data.data, inline_data.mime_type or "audio/wav"

        raise ValueError("Gemini TTS response did not contain audio data.")

    def _build_tts_prompt(self, script_text: str) -> str:
        transcript = _format_tts_transcript(script_text, self.config.tts_turn_pause_multiplier)
        return (
            "# Korean Morning Radio TTS\n"
            "Generate audio only for the transcript below.\n"
            "Do not add explanations, labels, or extra narration.\n"
            "Blank lines indicate silent handoff beats and must not be spoken.\n\n"
            "## Audio Profile\n"
            f"- {self.config.host_name}: an upbeat male morning-radio anchor with clear projection, lively momentum, and confident warmth.\n"
            f"- {self.config.analyst_name}: an energetic female commentator with brisk pickup, brighter color, and sparkling conversational rhythm.\n\n"
            "## Director Notes\n"
            "- Deliver at approximately "
            f"{self.config.tts_speed_multiplier:.2f}x the pace of a standard Korean radio briefing.\n"
            "- Keep diction crisp, fresh, and animated, never flat, rushed, or clipped.\n"
            "- After each speaker change, leave a clean pause about "
            f"{self.config.tts_turn_pause_multiplier:.2f}x longer than a normal broadcast handoff.\n"
            "- Make the handoff feel like polished radio tteki-taka: clear turns, quick rebounds, and audible smile in the delivery.\n"
            "- Maintain a bright, lively morning-show tone with gentle excitement and steady loudness.\n"
            "- Avoid sleepy, overly serious, or documentary-style delivery.\n"
            "- Read each speaker turn exactly as written in Korean.\n\n"
            "## Transcript\n"
            f"{transcript}"
        )


def _format_tts_transcript(script_text: str, pause_multiplier: float) -> str:
    lines = [line.strip() for line in script_text.splitlines() if line.strip()]
    if pause_multiplier >= 1.7:
        separator = "\n\n\n\n"
    elif pause_multiplier >= 1.4:
        separator = "\n\n\n"
    else:
        separator = "\n\n"
    return separator.join(lines)


def _retry_delay_seconds(message: str, default_seconds: int) -> int:
    match = re.search(r"retry in ([0-9.]+)s", message, flags=re.IGNORECASE)
    if match:
        return max(1, int(float(match.group(1))) + 1)

    match = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+)s", message, flags=re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))

    return max(1, default_seconds)
