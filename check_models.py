import os
from anthropic import Anthropic
from dotenv import load_dotenv

def main():
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    print(f"Key loaded: {api_key[:15]}..." if api_key else "NO KEY FOUND")
    
    try:
        client = Anthropic(api_key=api_key)
        response = client.models.list()
        print("\nAvailable Models for this API Key:")
        for model in response.data:
            print(f"- {model.id}")
            
    except Exception as e:
        print(f"Error listing models: {e}")

if __name__ == "__main__":
    main()
