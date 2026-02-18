import psycopg2
from urllib.parse import urlparse
import os
from dotenv import load_dotenv

load_dotenv()

# Parse the DATABASE_URL
url = os.getenv('DATABASE_URL')
print(f"Testing connection with: {url}")

# Remove the +psycopg2 part for parsing
url_for_parsing = url.replace('postgresql+psycopg2://', 'postgresql://')
parsed = urlparse(url_for_parsing)

print(f"\nParsed components:")
print(f"  Host: {parsed.hostname}")
print(f"  Port: {parsed.port}")
print(f"  Database: {parsed.path[1:]}")
print(f"  Username: {parsed.username}")
print(f"  Password: {'*' * len(parsed.password) if parsed.password else 'None'}")

try:
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path[1:],
        user=parsed.username,
        password=parsed.password
    )
    print("\n✅ Connection successful!")
    conn.close()
except Exception as e:
    print(f"\n❌ Connection failed: {e}")
