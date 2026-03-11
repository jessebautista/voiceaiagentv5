import requests
import json

def run_test():
    url = "http://localhost:8000/api/dev/fix"
    payload = {
        "id": 5,
        "title": "Add a prominent red border to the Bugs page title",
        "description": "Just a small UI test. In `src/routes/bugs/+page.svelte`, can you add an inline style `border: 2px solid red;` to the main h1 heading tag that says 'Bugs'?",
        "category": "UI Update",
        "status": "In Progress"
    }
    
    headers = {
        "Content-Type": "application/json"
    }
    
    print("Triggering development agent...")
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        print(f"Success! Agent started. Response: {json.dumps(response.json(), indent=2)}")
        print("\nNow monitor the 'app.temporal_worker' terminal to see the AI's internal thoughts and tool calls!")
    except requests.exceptions.RequestException as e:
        print(f"Failed to trigger agent: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response body: {e.response.text}")

if __name__ == "__main__":
    run_test()
