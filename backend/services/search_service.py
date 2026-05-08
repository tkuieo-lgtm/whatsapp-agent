import logging
from config import settings

logger = logging.getLogger(__name__)


async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via Tavily and return formatted results."""
    if not settings.tavily_api_key:
        return "❌ חיפוש אינטרנטי אינו מוגדר (חסר TAVILY_API_KEY)."

    try:
        from tavily import TavilyClient
        logger.info(f"[SEARCH] Tavily query: {query!r}")
        client = TavilyClient(api_key=settings.tavily_api_key)
        response = client.search(query, max_results=max_results)
        results = response.get("results", [])
        logger.info(f"[SEARCH] Tavily returned {len(results)} results")

        if not results:
            return f"לא נמצאו תוצאות עבור: {query}"

        lines = [f"🔍 תוצאות חיפוש עבור \"{query}\":\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "ללא כותרת")
            url = r.get("url", "")
            snippet = r.get("content", "")[:200].strip()
            lines.append(f"{i}. **{title}**\n   {snippet}\n   {url}\n")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"[SEARCH] Tavily error: {type(e).__name__}: {e}")
        # Don't bubble up as a rate-limit error — give a clear, specific message
        return f"⚠️ החיפוש אינו זמין כרגע. נסה שוב מאוחר יותר.\n(שגיאה: {type(e).__name__})"
