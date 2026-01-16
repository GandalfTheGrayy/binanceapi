#!/usr/bin/env python3
"""
Database migration script: WebhookEvent tablosuna status ve retry_count kolonlarını ekler.
"""
import sqlite3
import sys

def migrate():
    db_path = "data.db"
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Mevcut kolonları kontrol et
        cursor.execute("PRAGMA table_info(webhook_events)")
        columns = [row[1] for row in cursor.fetchall()]
        
        # status kolonu yoksa ekle
        if "status" not in columns:
            print("Adding 'status' column to webhook_events table...")
            cursor.execute("ALTER TABLE webhook_events ADD COLUMN status VARCHAR(16) DEFAULT 'completed'")
            # Mevcut kayıtlar için default değer
            cursor.execute("UPDATE webhook_events SET status = 'completed' WHERE status IS NULL")
            print("✓ 'status' column added")
        else:
            print("✓ 'status' column already exists")
        
        # retry_count kolonu yoksa ekle
        if "retry_count" not in columns:
            print("Adding 'retry_count' column to webhook_events table...")
            cursor.execute("ALTER TABLE webhook_events ADD COLUMN retry_count INTEGER DEFAULT 0")
            # Mevcut kayıtlar için default değer
            cursor.execute("UPDATE webhook_events SET retry_count = 0 WHERE retry_count IS NULL")
            print("✓ 'retry_count' column added")
        else:
            print("✓ 'retry_count' column already exists")
        
        # Index ekle (status için)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_webhook_events_status ON webhook_events (status)")
            print("✓ Index on 'status' created")
        except sqlite3.OperationalError:
            print("✓ Index on 'status' already exists")
        
        conn.commit()
        conn.close()
        print("\n✅ Migration completed successfully!")
        return True
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        return False

if __name__ == "__main__":
    success = migrate()
    sys.exit(0 if success else 1)

