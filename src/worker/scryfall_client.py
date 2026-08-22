import urllib.request
import gzip
import json

# Scryfall strictly requires BOTH a custom User-Agent and an Accept header
HEADERS = {
    "User-Agent": "ManaMarketEngine/1.0",
    "Accept": "application/json;q=0.9,*/*;q=0.8"
}

def test_extract():
    print("Fetching bulk data metadata...")
    
    # 1. Attach the headers to the initial Request
    meta_req = urllib.request.Request(
        "https://api.scryfall.com/bulk-data/default-cards", 
        headers=HEADERS
    )
    
    meta_response = urllib.request.urlopen(meta_req)
    meta_data = json.load(meta_response)
    
    # Get the JSON Lines URL (fallback to standard if jsonl key changes)
    download_url = meta_data.get("jsonl_download_uri") or meta_data.get("download_uri")
    
    print(f"Streaming compressed JSONL from: {download_url}")
    
    # 2. Attach the headers to the bulk file download request as well
    download_req = urllib.request.Request(download_url, headers=HEADERS)
    stream_response = urllib.request.urlopen(download_req)
    
    # 3. Stream the file line-by-line. This keeps memory usage near zero.
    count = 0
    with gzip.GzipFile(fileobj=stream_response) as gz:
        for line in gz:
            card = json.loads(line)
            
            card_id = card.get("id")
            name = card.get("name")
            prices = card.get("prices", {})
            usd = prices.get("usd")
            
            # Print the first 10 cards that have a valid USD price
            if usd:
                print(f"ID: {card_id} | Name: {name} | USD: ${usd}")
                count += 1
                
            if count >= 10:
                break
                
    print("Streaming extraction successful.")

if __name__ == "__main__":
    test_extract()