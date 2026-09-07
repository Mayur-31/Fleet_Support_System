"""
Single shared Supabase client. Every script does:
    from database import supabase
instead of creating its own client, so there's exactly one place
the connection is configured.
"""
from supabase import create_client
from config import SUPABASE_URL, SUPABASE_SECRET_KEY

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
