import time
import sys
import os
sys.path.append(os.getcwd())  # Ensure job_search package is importable

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from job_search.models import Job, SearchQuery
from job_search.database import Base

# Assuming standard SQLite path or from config
DB_URL = "sqlite:///./data/job_search.db"
engine = create_engine(DB_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def check_progress(search_id):
    db = SessionLocal()
    try:
        query = db.query(SearchQuery).filter(SearchQuery.id == search_id).first()
        if not query:
            print(f"Search ID {search_id} not found.")
            return

        print(f"Monitoring Search ID: {search_id} for '{query.keywords}'")
        
        start_time = time.time()
        while True:
            count = db.query(Job).filter(Job.search_query_id == search_id).count()
            print(f"Jobs found: {count} / 25")
            
            if count >= 25:
                print("Search target reached!")
                break
            
            if time.time() - start_time > 120:  # 2 minutes timeout
                print("Timeout reached.")
                break
                
            time.sleep(5)
    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        check_progress(int(sys.argv[1]))
    else:
        print("Please provide search ID")
