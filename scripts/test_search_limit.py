import requests
import time
import sys

BASE_URL = "http://127.0.0.1:8005"

def run_test():
    print("1. Running search for 'Python' with limit=5...")
    payload = {
        "keywords": "Python",
        "limit": 5,
        "easy_apply_only": False,
        "locations": ["Remote"]
    }
    try:
        r = requests.post(f"{BASE_URL}/api/search/run", json=payload)
        r.raise_for_status()
        data = r.json()
        search_id = data["search_id"]
        db_search_id = data["db_search_id"]
        print(f"   Search started. Search ID: {search_id}, DB ID: {db_search_id}")
    except Exception as e:
        print(f"   Failed to start search: {e}")
        return

    print("2. Polling for results API (checking if filter works)...")
    # Poll for 30s
    for i in range(10):
        time.sleep(3)
        # Check filtered Count
        r = requests.get(f"{BASE_URL}/api/jobs?search_id={db_search_id}&per_page=1")
        filtered_total = r.json().get("total", 0)
        
        # Check UNFILTERED Count (to replicate bug)
        r2 = requests.get(f"{BASE_URL}/api/jobs?per_page=1")
        total_in_db = r2.json().get("total", 0)
        
        print(f"   [Poll {i}] Filtered (ID {db_search_id}): {filtered_total} jobs. Total DB: {total_in_db} jobs.")
        
        if filtered_total >= 5:
            print("   Target reached!")
            break

    print("3. Verification:")
    if filtered_total > 5:
        print(f"FAIL: Found {filtered_total} jobs, expected limit 5.")
    elif filtered_total == 0 and total_in_db > 0:
        print(f"PASS: Filter correctly reports 0 (or low count) while DB has {total_in_db}.")
    else:
        print(f"Status: Filtered={filtered_total}, Total={total_in_db}")

if __name__ == "__main__":
    run_test()
