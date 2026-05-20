import json
import requests

def fetch_live_india_chargers():
    print("⚡ Connecting to Official Open Charge Map Global Data Engine...")
    master_list = []
    
    # ==========================================
    # 🛑 PASTE YOUR NEW API KEY BETWEEN THE QUOTES
    # ==========================================
    API_KEY = "7505eeb2-c5b4-4c7c-b509-10c1824778a4"
    
    url = "https://api.openchargemap.io/v3/poi"
    params = {
        "output": "json",
        "countrycode": "IN",
        "maxresults": 50000,  # <-- Increased to capture the entire national infrastructure
        "compact": "true",
        "verbose": "false",
        "key": API_KEY
    }
    
    headers = {
        "User-Agent": "EVCopilotApp/1.0",
        "Accept": "application/json"
    }
    
    try:
        if API_KEY == "PASTE_YOUR_KEY_HERE":
            print("❌ Execution Halted: You need to paste your OCM API key into the script.")
            return

        response = requests.get(url, params=params, headers=headers, timeout=25)
        
        if response.status_code == 200:
            raw_data = response.json()
            print(f"📡 Connection Established. Dynamically parsing {len(raw_data)} active infrastructure nodes...")
            
            for item in raw_data:
                addr = item.get("AddressInfo", {})
                if addr.get("Latitude") and addr.get("Longitude"):
                    connections = item.get("Connections", [])
                    power_kw = 50
                    if connections and isinstance(connections, list) and len(connections) > 0:
                        power_kw = connections[0].get("PowerKW") or 50

                    master_list.append({
                        "ID": item.get("ID"),
                        "AddressInfo": {
                            "Title": addr.get("Title", "Public EV Charger"),
                            "Latitude": float(addr["Latitude"]),
                            "Longitude": float(addr["Longitude"]),
                            "AddressLine1": addr.get("AddressLine1", "Highway Access Corridor")
                        },
                        "Connections": [{"PowerKW": int(power_kw)}]
                    })
            
            if master_list:
                with open("every_charger_india.json", "w", encoding="utf-8") as f:
                    json.dump(master_list, f, indent=2)
                print(f"🎉 Success! Dynamically generated database with {len(master_list)} live multi-network chargers.")
            else:
                print("⚠️ Stream parsing returned zero valid coordinates.")
        else:
            print(f"❌ Production Server Rejection. Status Code: {response.status_code}")
            print(f"📄 Response Content: {response.text[:200]}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Network Interruption: {str(e)}")

if __name__ == "__main__":
    fetch_live_india_chargers()