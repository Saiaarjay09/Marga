import requests
from typing import List, Dict, Any

def get_charging_stations(lat: float, lng: float, max_range_km: float, api_key: str) -> List[Dict[str, Any]]:
    """
    Queries Open Charge Map for public, operational, verified CCS Type 2 fast chargers 
    within the target vehicle search circle.
    """
    url = "https://api.openchargemap.io/v3/poi/"
    
    params = {
        "output": "json",
        "key": api_key,
        "latitude": lat,
        "longitude": lng,
        "distance": max_range_km, 
        "distanceunit": "KM",
        "maxresults": 20,
        
        # STRICT VERIFICATION FILTERS
        "connectiontypeid": 33,  # CCS Type 2 (Standard public DC Fast Charging across India)
        "usagetypeid": "1,4,5",  # 1=Public, 4=Public (Notice), 5=Public (Membership). Blocks residential/private plugs.
        "statustypeid": 50       # 50 = Strictly Operational. Blocks broken or broken/under-construction locations.
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        verified_stations = []
        for item in data:
            address_info = item.get('AddressInfo', {})
            if address_info and address_info.get('Title'):
                verified_stations.append({
                    "name": address_info.get('Title'),
                    "lat": address_info.get('Latitude'),
                    "lng": address_info.get('Longitude'),
                    "address": address_info.get('AddressLine1', 'No address details listed'),
                    "operator": item.get('OperatorInfo', {}).get('Title', 'Independent/Unknown Network'),
                    "verified": True
                })
        return verified_stations
        
    except Exception as e:
        print(f"Error filtering charging endpoints: {e}")
        return []
