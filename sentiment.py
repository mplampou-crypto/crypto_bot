import httpx
import asyncio
from datetime import datetime, timedelta
from config import NEWSAPI_KEY, BULLISH_KEYWORDS, BEARISH_KEYWORDS, NEWS_KEYWORDS


NEWSAPI_URL = "https://newsapi.org/v2/everything"

# Influencer keywords (χωρίς Twitter, ψάχνουμε στα articles)
INFLUENCER_NAMES = [
    "elon musk", "michael saylor", "cz binance", "vitalik buterin",
    "cathie wood", "peter schiff", "changpeng zhao",
]


async def fetch_news(query: str, hours_back: int = 6) -> list:
    """Παίρνει νέα από το NewsAPI"""
    from_time = (datetime.utcnow() - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%S")

    params = {
        "q": query,
        "from": from_time,
        "sortBy": "publishedAt",
        "language": "en",
        "apiKey": NEWSAPI_KEY,
        "pageSize": 20,
    }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(NEWSAPI_URL, params=params, timeout=10)
            data = resp.json()
            return data.get("articles", [])
        except Exception as e:
            print(f"NewsAPI error: {e}")
            return []


def analyze_sentiment(text: str) -> dict:
    """
    Αναλύει το sentiment ενός κειμένου.
    Επιστρέφει: score (-100 έως +100), label, details
    """
    text_lower = text.lower()

    bull_hits = [kw for kw in BULLISH_KEYWORDS if kw in text_lower]
    bear_hits = [kw for kw in BEARISH_KEYWORDS if kw in text_lower]

    bull_score = len(bull_hits) * 10
    bear_score = len(bear_hits) * 10

    net_score = min(bull_score - bear_score, 100)
    net_score = max(net_score, -100)

    if net_score >= 30:
        label = "🟢 BULLISH"
    elif net_score <= -30:
        label = "🔴 BEARISH"
    else:
        label = "🟡 NEUTRAL"

    return {
        "score": net_score,
        "label": label,
        "bull_keywords": bull_hits,
        "bear_keywords": bear_hits,
    }


def estimate_price_target(symbol: str, current_price: float, sentiment_score: int, side: str) -> float:
    """
    Εκτιμά price target βάσει sentiment strength.
    Bullish score 30-50  → +2-3%
    Bullish score 50-80  → +3-5%
    Bullish score 80-100 → +5-8%
    """
    if side == "LONG":
        if sentiment_score >= 80:
            pct = 0.07
        elif sentiment_score >= 50:
            pct = 0.04
        else:
            pct = 0.025
        return round(current_price * (1 + pct), 4)
    else:  # SHORT
        if sentiment_score <= -80:
            pct = 0.07
        elif sentiment_score <= -50:
            pct = 0.04
        else:
            pct = 0.025
        return round(current_price * (1 - pct), 4)


async def get_market_sentiment(symbol: str = "bitcoin") -> dict:
    """
    Συνολικό sentiment για ένα asset.
    Ψάχνει νέα + influencer mentions.
    """
    coin_name = symbol.replace("USDT", "").lower()
    all_articles = []

    # Ψάξε για το specific coin
    articles = await fetch_news(f"{coin_name} cryptocurrency", hours_back=6)
    all_articles.extend(articles)

    # Ψάξε για influencer mentions
    for influencer in INFLUENCER_NAMES[:3]:  # Limit για να μη σπαταλάμε requests
        inf_articles = await fetch_news(f"{influencer} {coin_name}", hours_back=12)
        all_articles.extend(inf_articles)

    if not all_articles:
        return {
            "score": 0,
            "label": "🟡 NEUTRAL",
            "articles_count": 0,
            "top_news": [],
            "influencer_alerts": [],
        }

    # Ανάλυσε όλα τα άρθρα
    total_score = 0
    top_news = []
    influencer_alerts = []

    for article in all_articles[:15]:
        title       = article.get("title", "") or ""
        description = article.get("description", "") or ""
        source_name = article.get("source", {}).get("name", "")
        url         = article.get("url", "")
        published   = article.get("publishedAt", "")

        combined = f"{title} {description}"
        sentiment = analyze_sentiment(combined)

        total_score += sentiment["score"]

        # Top news (αν έχει strong sentiment)
        if abs(sentiment["score"]) >= 20:
            top_news.append({
                "title": title[:100],
                "source": source_name,
                "sentiment": sentiment["label"],
                "score": sentiment["score"],
                "url": url,
            })

        # Influencer alerts
        for inf in INFLUENCER_NAMES:
            if inf in combined.lower():
                influencer_alerts.append({
                    "influencer": inf.title(),
                    "title": title[:100],
                    "sentiment": sentiment["label"],
                    "published": published,
                })

    avg_score = int(total_score / len(all_articles)) if all_articles else 0
    avg_score = max(-100, min(100, avg_score))

    if avg_score >= 30:
        overall_label = "🟢 BULLISH"
    elif avg_score <= -30:
        overall_label = "🔴 BEARISH"
    else:
        overall_label = "🟡 NEUTRAL"

    return {
        "score": avg_score,
        "label": overall_label,
        "articles_count": len(all_articles),
        "top_news": top_news[:5],
        "influencer_alerts": influencer_alerts[:3],
    }


def format_sentiment_message(symbol: str, sentiment: dict, price_target: float = None) -> str:
    """Φτιάχνει το μήνυμα για Telegram"""
    coin = symbol.replace("USDT", "")

    lines = [
        f"📰 <b>Market Sentiment — {coin}</b>",
        f"",
        f"Συνολικό Sentiment: <b>{sentiment['label']}</b>",
        f"Score: <b>{sentiment['score']:+d}/100</b>",
        f"Άρθρα αναλύθηκαν: {sentiment['articles_count']}",
    ]

    if price_target:
        lines.append(f"🎯 Εκτιμώμενη τιμή: <b>${price_target:,.2f}</b>")

    if sentiment["influencer_alerts"]:
        lines.append(f"\n👤 <b>Influencer Mentions:</b>")
        for alert in sentiment["influencer_alerts"]:
            lines.append(f"  • {alert['influencer']}: {alert['title'][:60]}... {alert['sentiment']}")

    if sentiment["top_news"]:
        lines.append(f"\n📌 <b>Top News:</b>")
        for news in sentiment["top_news"][:3]:
            lines.append(f"  {news['sentiment']} <a href='{news['url']}'>{news['title'][:60]}...</a>")
            lines.append(f"     <i>{news['source']}</i>")

    return "\n".join(lines)
