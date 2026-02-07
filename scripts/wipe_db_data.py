from job_search.database import SessionLocal
from job_search.models import Job, SearchQuery, Application

def reset_db():
    db = SessionLocal()
    try:
        print("Cleaning up database records...")
        
        # Order matters for foreign keys
        deleted_apps = db.query(Application).delete()
        print(f"Deleted {deleted_apps} applications.")
        
        deleted_jobs = db.query(Job).delete()
        print(f"Deleted {deleted_jobs} job listings.")
        
        deleted_queries = db.query(SearchQuery).delete()
        print(f"Deleted {deleted_queries} search history records.")
        
        db.commit()
        print("Database cleanup complete. You have a clean slate.")
    except Exception as e:
        print(f"Error resetting database: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    reset_db()
