# scripts/seed_upstash.py — Seeds Upstash Redis with sample FAQ entries and a sample
# chat session. Run from project root with UPSTASH_REDIS_REST_* in .env:
#   python scripts/seed_upstash.py

import json
import os
import sys
from pathlib import Path

# Load .env from project root
ROOT = Path(__file__).resolve().parent.parent
env_file = ROOT / ".env"
if env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
    except ImportError:
        pass

SAMPLE_FAQ = [
    {"q": "What are your opening hours?", "a": "We are open Monday–Friday 9am–6pm and Saturday 10am–4pm."},
    {"q": "How can I contact support?", "a": "Email support@company.com or call +1 (555) 123-4567."},
    {"q": "Do you offer refunds?", "a": "Yes. Refunds are available within 30 days with receipt."},
    {"q": "Where is the office?", "a": "Our office is at 123 Main Street, Suite 100."},
    {"q": "Do you have parking?", "a": "Yes. Free parking is available in the lot behind the building."},
    {"q": "What payment methods do you accept?", "a": "We accept credit cards, debit cards, and bank transfer."},
]

SAMPLE_CHAT_SESSION = [
    {"role": "human", "content": "Hi, what are your opening hours?"},
    {"role": "ai", "content": "We're open Monday–Friday 9am–6pm and Saturday 10am–4pm. Is there anything else you'd like to know?"},
    {"role": "human", "content": "Do you have parking?"},
    {"role": "ai", "content": "Yes. Free parking is available in the lot behind the building."},
]


def main():
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if not url or not token:
        print("Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN in .env", file=sys.stderr)
        sys.exit(1)

    from upstash_redis import Redis
    redis = Redis(url=url, token=token)

    # 1. Seed FAQ (key: faq:entries — JSON list; 30 days TTL)
    faq_key = "faq:entries"
    redis.set(faq_key, json.dumps(SAMPLE_FAQ), ex=86400 * 30)
    print(f"Seeded {faq_key} with {len(SAMPLE_FAQ)} FAQ entries.")

    # 2. Seed sample chat session (same format as redis_store agent:chat:*)
    chat_key = "agent:chat:seed-sample-session"
    redis.set(chat_key, json.dumps(SAMPLE_CHAT_SESSION), ex=86400 * 7)
    print(f"Seeded {chat_key} with sample conversation ({len(SAMPLE_CHAT_SESSION)} messages).")

    # 3. Optional: individual FAQ keys for lookup by id (faq:0, faq:1, ...)
    for i, entry in enumerate(SAMPLE_FAQ):
        key = f"faq:{i}"
        redis.set(key, json.dumps(entry), ex=86400 * 30)
    print(f"Seeded faq:0..faq:{len(SAMPLE_FAQ) - 1} for direct lookup.")

    print("Done. You can inspect keys in the Upstash dashboard.")


if __name__ == "__main__":
    main()
