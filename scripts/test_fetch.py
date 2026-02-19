import asyncio
import sys
import os

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tools.web_fetcher import fetch_web_page

async def main():
    url = "https://example.com"
    print(f"Fetching {url}...")
    try:
        # The tool is a Pydantic tool, so we call .ainvoke or extract the function.
        # LangChain tools when decorated with @tool expose the coroutine as .coroutine or via direct call if not bound.
        # But @tool creates a StructuredTool. Let's call the underlying function logic if possible, 
        # or use ainvoke. 
        
        # Using ainvoke (standard LangChain usage)
        result = await fetch_web_page.ainvoke(url)
        print("\n--- Result ---\n")
        print(result)
    except Exception as e:
        print(f"\nError: {e}")

if __name__ == "__main__":
    asyncio.run(main())
