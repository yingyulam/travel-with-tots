"""Everything that persists, and the only place SQL is written.

db.py holds every query, schema.py the tables and the migrations that bring an
older database up to them, postgres.py the same SQL in Supabase's dialect, and
supabase_sync.py the two directions between them. candidates.py and results.py
persist to CSV and JSON rather than SQL, and belong here for the same reason:
this is where the app's durable state lives.
"""
