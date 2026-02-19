# web_fetcher.py — Fetch and read web pages safe for LLM consumption.
# Uses httpx for async fetching and BeautifulSoup for text extraction.

import httpx
from bs4 import BeautifulSoup
from langchain_core.tools import tool

@tool
async def fetch_web_page(url: str) -> str:
    """
    Safely retrieves the text content of a public web page.
    Use this tool whenever a user asks to read, summarize, or extract information from a specific URL.
    Returns the simplified text content, truncated to the first 5000 characters.
    """
    try:
        # Ensure URL has a scheme
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        # Add a user-agent to avoid being blocked by some sites
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=10.0, verify=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.content

        soup = BeautifulSoup(content, "html.parser")
        
        # Remove script and style elements
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.decompose()

        # Get text
        text = soup.get_text()

        # Break into lines and remove leading and trailing space on each
        lines = (line.strip() for line in text.splitlines())
        # Break multi-headlines into a line each
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        # Drop blank lines
        text = '\n'.join(chunk for chunk in chunks if chunk)

        # Truncate to avoid token limits
        max_chars = 5000
        if len(text) > max_chars:
            text = text[:max_chars] + "... [content truncated]"

        return text
    except Exception as e:
        return f"Error fetching {url}: {str(e)}"
