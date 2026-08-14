import requests
import json
import os
import sys

import cbc_env

# API key is read from .env (MES_API_KEY) — never hardcoded.
API_KEY = cbc_env.mes_api_key()

payload = {
    "sql": "Select * from curingpcr where dtandtime >= '2026-04-11 00:00:00'",
    "tables":["mes/smartmesbtp/dbo/curingpcr"],
    "export":"true",
}

url = "https://c7hzxcvdxh.execute-api.ap-south-1.amazonaws.com/query"

headers = {
    "x-api-key": API_KEY,
    "Content-Type": "application/json"
}

response = requests.post(url, json=payload, headers=headers)

print("Status Code:", response.status_code)

# ------------------------------------------------
# Parse JSON directly (no Lambda body handling now)
# ------------------------------------------------
try:
    data = response.json()
except ValueError:
    print("Invalid JSON response")
    sys.exit(1)

# ------------------------------------------------
# PRINT RESPONSE
# ------------------------------------------------
print("\nResponse:")
json_string = json.dumps(data, indent=2)

CHUNK_SIZE = 1000
for i in range(0, len(json_string), CHUNK_SIZE):
    sys.stdout.write(json_string[i:i + CHUNK_SIZE])
    sys.stdout.flush()

print()

# ------------------------------------------------
# HANDLE EXPORT DOWNLOAD
# ------------------------------------------------
download_url = data.get("downloadUrl")

if download_url:
    print("\nDownloading file from presigned URL...")

    try:
        csv_response = requests.get(download_url)
        csv_response.raise_for_status()

        file_name = f"{data.get('queryId', 'query_result')}.csv"
        file_path = os.path.join(os.getcwd(), file_name)

        with open(file_path, "wb") as f:
            f.write(csv_response.content)

        print(f"CSV downloaded successfully: {file_path}")

    except Exception as e:
        print(f"Download failed: {str(e)}")

else:
    print("\nNo download URL found in response.")