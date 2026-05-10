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
    return f"{hour} ועוד {mi} דקות"


def prepare_for_speech(text: str) -> str:
    """Clean text so it reads naturally when spoken aloud."""
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"_+", "", text)
    text = re.sub(r"#{1,6}\s*", "", text)
    text = re.sub(r"`[^`]*`", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", _fmt_time, text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 600:
        text = text[:597] + "..."
    return text


# ---------------------------------------------------------------------------
# gTTS fallback (no API key, always available)
# ---------------------------------------------------------------------------

async def _gtts_tts(text: str) -> bytes:
    from gtts import gTTS
    from pydub import AudioSegment

    mp3_fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    ogg_fd, ogg_path = tempfile.mkstemp(suffix=".ogg")
    os.close(mp3_fd)
    os.close(ogg_fd)
    try:
        gTTS(text=text, lang="iw").save(mp3_path)
        AudioSegment.from_mp3(mp3_path).export(ogg_path, format="ogg", codec="libopus")
        with open(ogg_path, "rb") as f:
            return f.read()
    finally:
        for p in (mp3_path, ogg_path):
            if os.path.exists(p):
                os.unlink(p)


# ---------------------------------------------------------------------------
# Main TTS — edge-tts with gTTS fallback
# ---------------------------------------------------------------------------

async def text_to_speech(text: str) -> bytes:
    """Convert text to OGG/Opus bytes. Tries edge-tts first, falls back to gTTS."""
    from pydub import AudioSegment

    clean = prepare_for_speech(text)
    logger.info(f"[TTS] Generating with edge-tts ({VOICE}) — {len(clean)} chars: {clean[:60]!r}…")

    mp3_data = b""
    try:
        import edge_tts
        communicate = edge_tts.Communicate(clean, voice=VOICE)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_data += chunk["data"]

        if not mp3_data:
            raise RuntimeError("edge-tts returned empty audio stream")

        logger.info(f"[TTS] Generated {len(mp3_data)} bytes from edge-tts")

    except Exception as e:
        logger.warning(f"[TTS] edge-tts failed ({type(e).__name__}: {e}) — falling back to gTTS")
        result = await _gtts_tts(clean)
        logger.info(f"[TTS] gTTS fallback: {len(result)} bytes")
        return result

    # Convert mp3 → ogg/opus
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
        logger.info(f"[TTS] Converted to ogg/opus: {len(result)} bytes")
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
    """
    Default is VOICE unless the response is clearly text-optimised.
    User must explicitly request text to get a text-only reply.
    """
    # Always voice for these contexts
    if was_voice_input or context_type in ("morning_briefing", "reminder", "alert"):
        return True

    word_count = len(text.split())
    # Switch to text for: long structured content, email/calendar data, code
    has_heavy_structure = (
        (text.count("\n") > 5 and word_count > 40)
        or text.count("\n•") > 2
        or text.count("\n-") > 3
    )
    has_data_fields = any(c in text for c in ["📧", "🔍", "```", "http", "ID:", "id="])

    if word_count > 120 or has_heavy_structure or has_data_fields:
        return False

    return True  # default: voice
