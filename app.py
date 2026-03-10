from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import asyncio
import re
import os
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Startup check — logs mein dikhega
if GROQ_API_KEY:
    print(f"✅ GROQ KEY MILI: {GROQ_API_KEY[:8]}...")
else:
    print("❌ GROQ KEY NAHI MILI — Environment variable check karo!")

# ─────────────────────────────────────────
# REQUEST MODEL
# ─────────────────────────────────────────
class SearchRequest(BaseModel):
    business_name: str
    location: str

# ─────────────────────────────────────────
# SCRAPER — Google Maps Reviews
# ─────────────────────────────────────────
async def scrape_google_reviews(business_name: str, location: str):
    query = f"{business_name} {location}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    rating = "N/A"
    review_count = "N/A"
    reviews = []

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers=headers
    ) as client:

        # ── Step 1: Google Search for basic info ──
        try:
            search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}+reviews&hl=en"
            resp = await client.get(search_url)
            html = resp.text

            # Extract rating
            r = re.search(r'(\d\.\d)\s*/\s*5', html)
            if not r:
                r = re.search(r'"(\d\.\d)"\s*out of 5', html)
            if r:
                rating = r.group(1)

            # Extract review count
            c = re.search(r'([\d,]+)\s*(?:Google\s+)?reviews?', html, re.IGNORECASE)
            if c:
                review_count = c.group(1)

        except Exception:
            pass

        # ── Step 2: Google Maps scrape for reviews ──
        try:
            maps_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
            resp2 = await client.get(maps_url)
            html2 = resp2.text

            # Try to find review snippets
            # Google Maps embeds some review text in JS data
            patterns = [
                r'\["([^"]{40,400})"\s*,\s*null\s*,\s*\d+\s*\]',
                r'"snippet"\s*:\s*"([^"]{30,400})"',
                r'class="[^"]*review[^"]*"[^>]*>([^<]{30,400})<',
            ]

            found = set()
            for pat in patterns:
                matches = re.findall(pat, html2, re.IGNORECASE)
                for m in matches:
                    clean = m.strip().replace('\\n', ' ').replace('\\"', '"')
                    if (
                        len(clean) > 30
                        and len(clean) < 500
                        and clean not in found
                        and not clean.startswith('http')
                        and not re.search(r'[<>{}\[\]]', clean)
                    ):
                        found.add(clean)
                        reviews.append({"text": clean, "date": "Recent"})
                        if len(reviews) >= 20:
                            break
                if len(reviews) >= 20:
                    break

        except Exception:
            pass

        # ── Step 3: Google Search reviews snippet fallback ──
        if len(reviews) < 5:
            try:
                search2 = f"https://www.google.com/search?q={query.replace(' ', '+')}+customer+reviews&num=20&hl=en"
                resp3 = await client.get(search2)
                html3 = resp3.text

                snippets = re.findall(r'<span[^>]*>([^<]{40,300})</span>', html3)
                found2 = set(r["text"] for r in reviews)
                for s in snippets:
                    clean = s.strip()
                    if (
                        len(clean) > 40
                        and clean not in found2
                        and not clean.startswith('http')
                        and not re.search(r'[<>{}\[\]©®]', clean)
                        and any(w in clean.lower() for w in [
                            'good', 'great', 'bad', 'nice', 'best', 'worst',
                            'service', 'food', 'staff', 'place', 'recommend',
                            'quality', 'price', 'clean', 'fast', 'slow',
                            'acha', 'bura', 'badhiya', 'mast', 'bekar'
                        ])
                    ):
                        found2.add(clean)
                        reviews.append({"text": clean, "date": "Recent"})
                        if len(reviews) >= 20:
                            break

            except Exception:
                pass

    return {
        "rating": rating,
        "review_count": review_count,
        "reviews": reviews[:20]
    }

# ─────────────────────────────────────────
# AI SUMMARY — Groq (Llama 3)
# ─────────────────────────────────────────
async def generate_summary(business_name: str, location: str, reviews: list, rating: str):
    if not GROQ_API_KEY:
        return "⚠️ Groq API key set nahi hai."

    if not reviews:
        review_text = "Koi review nahi mila."
    else:
        review_text = "\n".join([f"- {r['text']}" for r in reviews])

    prompt = f"""Tu ek helpful Indian assistant hai jo Hinglish mein baat karta hai.

Business: {business_name}
Location: {location}
Rating: {rating}/5

Reviews:
{review_text}

In reviews ke basis pe ek SHORT summary de (max 120 words) jo bataye:
1. Overall kaisa hai yeh jagah?
2. Kya acha hai? (2-3 points)
3. Kya theek nahi? (1-2 points)
4. Jana chahiye ya nahi? (ek line mein)

Hinglish mein likh, friendly tone mein."""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    body = {
        "model": "llama3-8b-8192",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.7
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=body
            )
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Summary generate karne mein error: {str(e)}"

# ─────────────────────────────────────────
# API ENDPOINT
# ─────────────────────────────────────────
@app.post("/api/search")
async def search(req: SearchRequest):
    if not req.business_name.strip() or not req.location.strip():
        raise HTTPException(400, "Business name aur location dono chahiye")

    # Run scraping
    scraped = await scrape_google_reviews(req.business_name, req.location)

    # Generate AI summary
    summary = await generate_summary(
        req.business_name,
        req.location,
        scraped["reviews"],
        scraped["rating"]
    )

    return {
        "success": True,
        "business_name": req.business_name,
        "location": req.location,
        "rating": scraped["rating"],
        "total_reviews": scraped["review_count"],
        "reviews": scraped["reviews"],
        "reviews_found": len(scraped["reviews"]),
        "ai_summary": summary
    }

# ─────────────────────────────────────────
# SERVE FRONTEND
# ─────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
