import logging
import os
import re
import tempfile

logger = logging.getLogger(__name__)

VOICE = "he-IL-AvriNeural"

# ---------------------------------------------------------------------------
# Text preparation for speech
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile(
    "[\U00002600-\U000027BF"
    "\U0001F300-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "☀-⛿✀-➿]+",
    re.UNICODE,
)

_HOURS_HE = {
    0: "חצות", 1: "אחת", 2: "שתיים", 3: "שלוש", 4: "ארבע",
    5: "חמש", 6: "שש", 7: "שבע", 8: "שמונה", 9: "תשע",
    10: "עשר", 11: "אחת עשרה", 12: "שתים עשרה", 13: "אחת אחר הצהריים",
    14: "שתיים אחר הצהריים", 15: "שלוש אחר הצהריים",
    16: "ארבע אחר הצהריים", 17: "חמש אחר הצהריים",
    18: "שש אחר הצהריים", 19: "שבע בערב", 20: "שמונה בערב",
    21: "תשע בערב", 22: "עשר בלילה", 23: "אחת עשרה בלילה",
}


def _fmt_time(m: re.Match) -> str:
    h, mi = int(m.group(1)), int(m.group(2))
    hour = _HOURS_HE.get(h, str(h))
    if mi == 0:
        return hour
    if mi == 30:
        return f"חצי {hour}"
    if mi == 15:
        return f"רבע ל{hour}"
    return f"{hour} ועוד {mi} דקות"


def prepare_for_speech(text: str) -> str:
    """Clean text so it reads naturally when spoken aloud."""
    # Markdown
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"_+", "", text)
    text = re.sub(r"#{1,6}\s*", "", text)
    text = re.sub(r"`[^`]*`", "", text)
    # URLs
    text = re.sub(r"https?://\S+", "", text)
    # Emojis
    text = _EMOJI_RE.sub("", text)
    # Times  (09:00 → תשע בבוקר)
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", _fmt_time, text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Cap length — long speech is uncomfortable
    if len(text) > 600:
        text = text[:597] + "..."
    return text


# ---------------------------------------------------------------------------
# TTS via edge-tts  (he-IL-AvriNeural — natural male Hebrew voice)
# ---------------------------------------------------------------------------

async def text_to_speech(text: str) -> bytes:
    """Convert text to OGG/Opus bytes suitable for WhatsApp voice notes."""
    import edge_tts
    from pydub import AudioSegment

    clean = prepare_for_speech(text)
    logger.info(f"[TTS] edge-tts ({VOICE}) — {len(clean)} chars: {clean[:60]!r}…")

    # Step 1: edge-tts → mp3 bytes via streaming
    mp3_data = b""
    communicate = edge_tts.Communicate(clean, voice=VOICE)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_data += chunk["data"]

    if not mp3_data:
        raise RuntimeError("edge-tts returned no audio data")

    # Step 2: mp3 → ogg/opus (WhatsApp voice note format)
    mp3_fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    ogg_fd, ogg_path = tempfile.mkstemp(suffix=".ogg")
    os.close(mp3_fd)
    os.close(ogg_fd)
    try:
        with open(mp3_path, "wb") as f:
            f.write(mp3_data)
        AudioSegment.from_mp3(mp3_path).export(ogg_path, format="ogg", codec="libopus")
        with open(ogg_path, "rb") as f:
            result = f.read()
        logger.info(f"[TTS] Generated {len(result)} bytes (ogg/opus)")
        return result
    finally:
        for p in (mp3_path, ogg_path):
            if os.path.exists(p):
                os.unlink(p)


# ---------------------------------------------------------------------------
# Voice-vs-text decision
# ---------------------------------------------------------------------------

def should_use_voice(
    text: str,
    was_voice_input: bool = False,
    context_type: str = "text",
) -> bool:
    if was_voice_input:
        return True
    if context_type in ("morning_briefing", "reminder", "alert"):
        return True
    word_count = len(text.split())
    has_list = "\n•" in text or "\n-" in text or text.count("\n") > 4
    has_structured = any(c in text for c in ["📅", "📧", "🔍", "```", "http"])
    if word_count > 100 or has_list or has_structured:
        return False
    return word_count <= 50
