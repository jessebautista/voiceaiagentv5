# sme_expert.py — SME (Subject Matter Expert) persona: instructions for technical
# or deep-dive queries. Includes a simple classifier to detect when to use SME logic.

SME_INSTRUCTIONS = """
You are a technical subject-matter expert. When users ask detailed or technical
questions (pricing formulas, product specs, integrations, how something works),
provide accurate, structured answers. Use the FAQ search and calculator as needed.
Stay precise and avoid marketing fluff.
""".strip()

# Simple keyword-based heuristic to detect technical/SME-style queries
SME_KEYWORDS = (
    "how does", "how do", "technical", "integration", "api", "pricing formula",
    "specification", "architecture", "implementation", "debug", "error code",
)


def is_sme_query(message: str) -> bool:
    """Return True if the message looks like a technical/SME query."""
    lower = (message or "").lower().strip()
    return any(kw in lower for kw in SME_KEYWORDS)
