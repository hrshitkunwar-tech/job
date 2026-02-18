from job_search.database import init_db
import os

if __name__ == "__main__":
    print("Initializing Supabase database tables...")
    init_db()
    print("Done!")
