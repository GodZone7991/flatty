#!/usr/bin/env python3
"""
Property Analyzer - Multi-agent batch evaluation + Telegram notifications.
Properties are batched (5 at a time) to save tokens — system prompt sent once per batch.
Only properties where ALL agents vote NO are skipped. Everything else goes to Telegram.
"""

import os
import json
import time
import base64
import requests
from datetime import datetime, timedelta
from pathlib import Path

from agents import AGENTS
from profile import BUYER_PROFILE, BLACKLISTED_ZONES

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Provider: "anthropic" (default, Sonnet) or "gemini"
ANALYZER_PROVIDER = os.environ.get("ANALYZER_PROVIDER", "anthropic")

# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

# Gemini (fallback / free option)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HISTORY_FILE = Path("properties_history.json")
ANALYZED_FILE = Path("analyzed_properties.json")

DAYS_TO_ANALYZE = 4
LLM_DELAY = 2
BATCH_SIZE = 5


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_properties() -> list:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return []


def load_analyzed() -> dict:
    if ANALYZED_FILE.exists():
        try:
            with open(ANALYZED_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {"analyzed_ids": [], "results": [], "last_run": ""}
    return {"analyzed_ids": [], "results": [], "last_run": ""}


def save_analyzed(data: dict):
    data["last_run"] = datetime.now().isoformat()
    with open(ANALYZED_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_unanalyzed(properties: list, analyzed_ids: set) -> list:
    cutoff = datetime.now() - timedelta(days=DAYS_TO_ANALYZE)
    filtered = []
    for prop in properties:
        if prop["id"] in analyzed_ids:
            continue
        scraped_at = prop.get("scraped_at", "")
        if scraped_at:
            try:
                if datetime.fromisoformat(scraped_at) < cutoff:
                    continue
            except ValueError:
                pass
        filtered.append(prop)
    return filtered


def is_blacklisted(prop: dict) -> bool:
    """Check if property is in a blacklisted zone (Raval, Poble Sec, Gótico)."""
    text = f"{prop.get('address', '')} {prop.get('title', '')}".lower()
    for zone in BLACKLISTED_ZONES:
        if zone.lower() in text:
            return True
    return False


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------

def build_property_text(prop: dict, num: int) -> str:
    """Format a single property with a number for batch identification."""
    lines = [
        f"### Property {num}",
        f"Title: {prop.get('title', 'N/A')}",
        f"Price: {prop.get('price', 0):,} EUR".replace(",", "."),
        f"Size: {prop.get('size_m2', 0)} m²",
        f"Rooms: {prop.get('rooms', 0)}",
        f"Address: {prop.get('address', 'N/A')}",
        f"City: {prop.get('city', 'N/A')}",
        f"Source: {prop.get('source', 'N/A')}",
        f"URL: {prop.get('url', '')}",
    ]

    if prop.get("price_per_m2"):
        lines.append(f"Price/m²: {prop['price_per_m2']} EUR")
    if prop.get("floor"):
        lines.append(f"Floor: {prop['floor']}")
    if prop.get("has_elevator") is not None:
        lines.append(f"Elevator: {'Yes' if prop['has_elevator'] else 'No'}")
    if prop.get("energy_certificate"):
        lines.append(f"Energy Certificate: {prop['energy_certificate']}")
    if prop.get("bathrooms"):
        lines.append(f"Bathrooms: {prop['bathrooms']}")
    if prop.get("raw_description"):
        lines.append(f"Description: {prop['raw_description'][:400]}")

    return "\n".join(lines)


def build_batch_text(batch: list) -> str:
    """Format multiple properties for a single LLM call."""
    parts = []
    for i, prop in enumerate(batch, 1):
        parts.append(build_property_text(prop, i))
    return "\n\n---\n\n".join(parts)


BATCH_VOTE_SCHEMA = """\
You are evaluating {count} properties. Respond ONLY with a valid JSON array (no markdown fences).
Each element must have this exact format:

[
  {{
    "property_num": 1,
    "vote": "YES|NO|UNCERTAIN",
    "confidence": <0.0-1.0>,
    "summary": "1-2 sentence justification"
  }},
  ...
]

Rules:
- Return exactly {count} elements, one per property, in order.
- Vote YES only if the property clearly meets criteria with no red flags.
- Vote NO if there is a clear dealbreaker.
- Vote UNCERTAIN if key information is missing but no obvious dealbreakers.
- Be concise. Each summary should be 1-2 sentences max.
- IMPORTANT: Write ALL summaries in Russian (русский язык)."""


def call_anthropic(system_prompt: str, user_text: str) -> str:
    """Call Anthropic Messages API (Claude Sonnet)."""
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set")

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        "temperature": 0.1,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_text}],
    }

    for attempt in range(3):
        response = requests.post(url, json=payload, headers=headers, timeout=120)
        if response.status_code == 529 and attempt < 2:
            wait = 15 * (attempt + 1)
            print(f"      Anthropic overloaded, retrying in {wait}s...")
            time.sleep(wait)
            continue
        response.raise_for_status()
        result = response.json()
        return result["content"][0]["text"]


def call_gemini(system_prompt: str, user_text: str) -> str:
    """Call Gemini API (free tier fallback)."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_text}"}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 4096,
        },
    }

    response = requests.post(url, json=payload, timeout=120)
    response.raise_for_status()
    result = response.json()
    return result["candidates"][0]["content"]["parts"][0]["text"]


def call_llm(system_prompt: str, user_text: str) -> str:
    """Route to configured provider."""
    provider = ANALYZER_PROVIDER.lower()
    if provider == "anthropic":
        return call_anthropic(system_prompt, user_text)
    else:
        return call_gemini(system_prompt, user_text)


def parse_batch_votes(text: str, expected_count: int) -> list:
    """Parse a JSON array of votes from LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    if text.endswith("```"):
        text = text[:-3]

    try:
        votes = json.loads(text.strip())
        if not isinstance(votes, list):
            votes = [votes]

        # Validate and normalize
        result = []
        for v in votes:
            if v.get("vote") not in ("YES", "NO", "UNCERTAIN"):
                v["vote"] = "UNCERTAIN"
            result.append(v)

        # Pad if LLM returned fewer than expected
        while len(result) < expected_count:
            result.append({
                "property_num": len(result) + 1,
                "vote": "UNCERTAIN",
                "confidence": 0.0,
                "summary": "No response from agent for this property",
            })

        return result[:expected_count]

    except json.JSONDecodeError:
        # Return UNCERTAIN for all if parse fails
        return [
            {
                "property_num": i + 1,
                "vote": "UNCERTAIN",
                "confidence": 0.0,
                "summary": "Failed to parse agent response",
            }
            for i in range(expected_count)
        ]


# ---------------------------------------------------------------------------
# Batch evaluation
# ---------------------------------------------------------------------------

def evaluate_batch(batch: list) -> list:
    """Evaluate a batch of properties through ALL agents. Returns list of results."""
    batch_text = build_batch_text(batch)

    # Collect votes per property: {prop_index: {agent_name: vote}}
    all_votes = {i: {} for i in range(len(batch))}

    for agent in AGENTS:
        agent_name = agent["name"]
        vote_instruction = BATCH_VOTE_SCHEMA.format(count=len(batch))

        prompt = f"{agent['system_prompt']}\n\n{vote_instruction}"
        user_text = f"## Properties to Evaluate\n\n{batch_text}"

        try:
            print(f"  {agent['emoji']} {agent_name}: evaluating {len(batch)} properties...")
            raw = call_llm(prompt, user_text)
            votes = parse_batch_votes(raw, len(batch))

            for j, vote in enumerate(votes):
                all_votes[j][agent_name] = vote
                v = vote["vote"]
                print(f"    Property {j+1}: {v} ({vote.get('confidence', '?')})")

        except Exception as e:
            print(f"  {agent['emoji']} {agent_name}: ERROR - {e}")
            for j in range(len(batch)):
                all_votes[j][agent_name] = {
                    "property_num": j + 1,
                    "vote": "UNCERTAIN",
                    "confidence": 0.0,
                    "summary": f"Agent error: {e}",
                }

        time.sleep(LLM_DELAY)

    # Build results
    results = []
    for i, prop in enumerate(batch):
        votes = all_votes[i]
        vote_values = [v["vote"] for v in votes.values()]
        no_count = vote_values.count("NO")
        yes_count = vote_values.count("YES")

        # SKIP if all agents say NO, or 3+ NO with no YES (not worth sending)
        if no_count == len(votes) and len(votes) > 0:
            decision = "SKIP"
        elif no_count >= 3 and yes_count == 0:
            decision = "SKIP"
        else:
            decision = "SEND"

        score_map = {"YES": 2, "UNCERTAIN": 1, "NO": 0}
        agent_score = sum(score_map.get(v["vote"], 0) for v in votes.values())
        confidence_avg = sum(v.get("confidence", 0.5) for v in votes.values()) / max(len(votes), 1)

        results.append({
            "property_id": prop["id"],
            "decision": decision,
            "votes": votes,
            "score": agent_score,
            "yes_count": yes_count,
            "no_count": no_count,
            "confidence_avg": round(confidence_avg, 2),
            "evaluated_at": datetime.now().isoformat(),
        })

    return results


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured — printing to console:")
        print(message)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"Error sending Telegram: {e}")
        return False


def _source_label(source: str) -> str:
    return {
        "pisos.com": "pisos.com",
        "fotocasa": "Fotocasa",
        "idealista": "Idealista",
    }.get(source, source)


def _vote_bar(votes: dict) -> str:
    emojis = []
    for agent in AGENTS:
        v = votes.get(agent["name"], {}).get("vote", "UNCERTAIN")
        emojis.append({"YES": "✅", "NO": "❌", "UNCERTAIN": "❓"}.get(v, "❓"))
    return "".join(emojis)


def format_property_compact(prop: dict, result: dict, num: int) -> str:
    """Format a property as a compact entry for consolidated city message."""
    votes = result["votes"]
    score = result.get("score", 0)
    max_score = len(AGENTS) * 2

    price = prop.get("price", 0)
    size = prop.get("size_m2", 0)
    ppm2 = round(price / size, 0) if size > 0 else 0
    rooms = prop.get("rooms", 0)
    source = _source_label(prop.get("source", ""))
    vote_bar = _vote_bar(votes)

    lines = [
        f"<b>{num}. {prop.get('title', 'Объект')[:100]}</b>",
        f"💰 {price:,} € · {size} м² · {rooms} комн · {ppm2:,.0f} €/м²".replace(",", "."),
        f"📍 {prop.get('address', 'N/A')}",
        f"🔗 <a href=\"{prop.get('url', '')}\">{source}</a>",
        f"{vote_bar} ({score}/{max_score})",
    ]

    # Compact agent summaries: emoji + vote + short summary on one line each
    for agent in AGENTS:
        vote = votes.get(agent["name"])
        if not vote:
            continue
        v = vote["vote"]
        vote_emoji = {"YES": "✅", "NO": "❌", "UNCERTAIN": "❓"}.get(v, "❓")
        summary = vote.get("summary", "")[:80]
        lines.append(f"{agent['emoji']}{vote_emoji} {summary}")

    return "\n".join(lines)


def _split_message(text: str, max_len: int = 4096) -> list:
    """Split message at property boundaries if it exceeds Telegram's limit."""
    if len(text) <= max_len:
        return [text]

    separator = "\n\n━━━━━━━━━━━━\n\n"
    parts = text.split(separator)

    messages = []
    current = ""
    for part in parts:
        candidate = current + separator + part if current else part
        if len(candidate) > max_len and current:
            messages.append(current)
            current = part
        else:
            current = candidate
    if current:
        messages.append(current)

    return messages


def send_notifications(to_send: list, skipped: int, props_by_id: dict):
    """Send consolidated Telegram messages: 1 summary + 1 per city (in Russian)."""
    if not to_send:
        if skipped > 0:
            send_telegram(
                f"🏠 <b>Обзор недвижимости</b> — {datetime.now().strftime('%Y-%m-%d')}\n\n"
                f"Все {skipped} новых объектов отклонены агентами. Нечего показывать."
            )
        else:
            print("No properties to notify about")
        return

    to_send.sort(key=lambda r: (r.get("score", 0), r.get("confidence_avg", 0)), reverse=True)

    # Group by city
    by_city = {}
    for result in to_send:
        prop = props_by_id.get(result["property_id"], {})
        city = prop.get("city", "unknown")
        by_city.setdefault(city, []).append(result)

    # Summary header
    city_labels = {"barcelona": "Барселона", "madrid": "Мадрид"}
    city_counts = " | ".join(
        f"{city_labels.get(c, c.title())}: {len(rs)}" for c, rs in by_city.items()
    )
    summary = f"🏠 <b>Обзор недвижимости</b> — {datetime.now().strftime('%Y-%m-%d')}\n"
    summary += f"📋 {len(to_send)} объектов ({city_counts})"
    if skipped:
        summary += f"\n🗑 {skipped} отклонено"
    send_telegram(summary)
    time.sleep(1)

    # One consolidated message per city
    for city in ["barcelona", "madrid"]:
        results = by_city.get(city, [])
        if not results:
            continue

        label = city_labels.get(city, city.title())
        header = f"🇪🇸 <b>{label}</b> — {len(results)} объектов\n"

        property_blocks = []
        for i, result in enumerate(results, 1):
            prop = props_by_id.get(result["property_id"])
            if prop:
                property_blocks.append(format_property_compact(prop, result, i))

        full_msg = header + "\n\n━━━━━━━━━━━━\n\n".join(property_blocks)

        # Split if exceeds Telegram's 4096 char limit
        parts = _split_message(full_msg)
        for j, part in enumerate(parts):
            # Add continuation header for split messages
            if j > 0:
                part = f"🇪🇸 <b>{label}</b> (прод.)\n\n" + part
            send_telegram(part)
            time.sleep(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Starting property analysis at {datetime.now().isoformat()}")

    provider = ANALYZER_PROVIDER.lower()
    print(f"Provider: {provider} | Batch size: {BATCH_SIZE}")

    if provider == "anthropic" and not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set. Exiting.")
        return
    if provider == "gemini" and not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY not set. Exiting.")
        return

    properties = load_properties()
    analyzed_data = load_analyzed()
    analyzed_ids = set(analyzed_data.get("analyzed_ids", []))
    all_results = analyzed_data.get("results", [])

    print(f"Loaded {len(properties)} properties, {len(analyzed_ids)} previously analyzed")

    to_analyze = get_unanalyzed(properties, analyzed_ids)
    print(f"{len(to_analyze)} new properties to analyze")

    # Filter out blacklisted zones before agent evaluation (saves tokens)
    blacklisted = [p for p in to_analyze if is_blacklisted(p)]
    to_analyze = [p for p in to_analyze if not is_blacklisted(p)]
    if blacklisted:
        print(f"Filtered {len(blacklisted)} properties in blacklisted zones: {', '.join(p.get('address', '?')[:40] for p in blacklisted)}")
        # Mark blacklisted as analyzed so they don't reappear
        for p in blacklisted:
            analyzed_ids.add(p["id"])

    if not to_analyze:
        print("Nothing new to analyze.")
        return

    props_by_id = {p["id"]: p for p in to_analyze}

    to_send = []
    skipped = 0

    # Process in batches
    num_batches = (len(to_analyze) + BATCH_SIZE - 1) // BATCH_SIZE
    total_calls = num_batches * len(AGENTS)
    print(f"Processing {len(to_analyze)} properties in {num_batches} batches ({total_calls} LLM calls)")

    for batch_idx in range(0, len(to_analyze), BATCH_SIZE):
        batch = to_analyze[batch_idx:batch_idx + BATCH_SIZE]
        batch_num = batch_idx // BATCH_SIZE + 1

        print(f"\n=== Batch {batch_num}/{num_batches} ({len(batch)} properties) ===")
        for j, p in enumerate(batch):
            print(f"  {j+1}. {p.get('title', '?')[:60]} | {p.get('price', 0):,} EUR".replace(",", "."))

        results = evaluate_batch(batch)

        for result in results:
            all_results.append(result)
            analyzed_ids.add(result["property_id"])

            prop = props_by_id.get(result["property_id"], {})
            if result["decision"] == "SEND":
                to_send.append(result)
                print(f"  -> SEND: {prop.get('title', '?')[:50]} (score: {result['score']}/{len(AGENTS)*2})")
            else:
                skipped += 1
                print(f"  -> SKIP: {prop.get('title', '?')[:50]}")

        # Save after each batch (crash recovery)
        save_analyzed({"analyzed_ids": list(analyzed_ids), "results": all_results})

    print(f"\nResults: {len(to_send)} to send, {skipped} skipped")
    print(f"Total LLM calls: {total_calls} (vs {len(to_analyze) * len(AGENTS)} without batching)")

    send_notifications(to_send, skipped, props_by_id)

    print(f"\nCompleted at {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
