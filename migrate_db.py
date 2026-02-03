#!/usr/bin/env python3
"""
Database migration script: Yeni kolonları ve tabloları ekler.
- WebhookEvent tablosuna endpoint kolonu
- OrderRecord tablosuna endpoint kolonu
- EndpointPosition tablosu
- EndpointConfig tablosu
"""
import sqlite3
import sys

def migrate():
    db_path = "data.db"
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        print("=" * 50)
        print("DATABASE MIGRATION")
        print("=" * 50)
        
        # ===== WEBHOOK_EVENTS TABLOSU =====
        print("\n[webhook_events table]")
        cursor.execute("PRAGMA table_info(webhook_events)")
        columns = [row[1] for row in cursor.fetchall()]
        
        # status kolonu yoksa ekle
        if "status" not in columns:
            print("  Adding 'status' column...")
            cursor.execute("ALTER TABLE webhook_events ADD COLUMN status VARCHAR(16) DEFAULT 'completed'")
            cursor.execute("UPDATE webhook_events SET status = 'completed' WHERE status IS NULL")
            print("  ✓ 'status' column added")
        else:
            print("  ✓ 'status' column already exists")
        
        # retry_count kolonu yoksa ekle
        if "retry_count" not in columns:
            print("  Adding 'retry_count' column...")
            cursor.execute("ALTER TABLE webhook_events ADD COLUMN retry_count INTEGER DEFAULT 0")
            cursor.execute("UPDATE webhook_events SET retry_count = 0 WHERE retry_count IS NULL")
            print("  ✓ 'retry_count' column added")
        else:
            print("  ✓ 'retry_count' column already exists")
        
        # endpoint kolonu yoksa ekle
        if "endpoint" not in columns:
            print("  Adding 'endpoint' column...")
            cursor.execute("ALTER TABLE webhook_events ADD COLUMN endpoint VARCHAR(32) DEFAULT 'layer1'")
            cursor.execute("UPDATE webhook_events SET endpoint = 'layer1' WHERE endpoint IS NULL")
            print("  ✓ 'endpoint' column added (default: layer1)")
        else:
            print("  ✓ 'endpoint' column already exists")
        
        # Index ekle (status için)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_webhook_events_status ON webhook_events (status)")
            print("  ✓ Index on 'status' created/exists")
        except sqlite3.OperationalError:
            pass
        
        # Index ekle (endpoint için)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_webhook_events_endpoint ON webhook_events (endpoint)")
            print("  ✓ Index on 'endpoint' created/exists")
        except sqlite3.OperationalError:
            pass
        
        # ===== ORDERS TABLOSU =====
        print("\n[orders table]")
        cursor.execute("PRAGMA table_info(orders)")
        order_columns = [row[1] for row in cursor.fetchall()]
        
        # endpoint kolonu yoksa ekle
        if "endpoint" not in order_columns:
            print("  Adding 'endpoint' column...")
            cursor.execute("ALTER TABLE orders ADD COLUMN endpoint VARCHAR(32) DEFAULT 'layer1'")
            cursor.execute("UPDATE orders SET endpoint = 'layer1' WHERE endpoint IS NULL")
            print("  ✓ 'endpoint' column added (default: layer1)")
        else:
            print("  ✓ 'endpoint' column already exists")
        
        # Index ekle (endpoint için)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_orders_endpoint ON orders (endpoint)")
            print("  ✓ Index on 'endpoint' created/exists")
        except sqlite3.OperationalError:
            pass
        
        # ===== ENDPOINT_POSITIONS TABLOSU =====
        print("\n[endpoint_positions table]")
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='endpoint_positions'")
        if not cursor.fetchone():
            print("  Creating 'endpoint_positions' table...")
            cursor.execute("""
                CREATE TABLE endpoint_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint VARCHAR(32) NOT NULL,
                    symbol VARCHAR(32) NOT NULL,
                    side VARCHAR(16),
                    qty FLOAT DEFAULT 0,
                    entry_price FLOAT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX ix_endpoint_positions_endpoint ON endpoint_positions (endpoint)")
            cursor.execute("CREATE INDEX ix_endpoint_positions_symbol ON endpoint_positions (symbol)")
            print("  ✓ 'endpoint_positions' table created")
        else:
            print("  ✓ 'endpoint_positions' table already exists")
        
        # ===== ENDPOINT_CONFIGS TABLOSU =====
        print("\n[endpoint_configs table]")
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='endpoint_configs'")
        if not cursor.fetchone():
            print("  Creating 'endpoint_configs' table...")
            cursor.execute("""
                CREATE TABLE endpoint_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint VARCHAR(32) UNIQUE NOT NULL,
                    trade_amount_usd FLOAT DEFAULT 100.0,
                    multiplier FLOAT DEFAULT 1.0,
                    leverage INTEGER DEFAULT 5,
                    enabled BOOLEAN DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE UNIQUE INDEX ix_endpoint_configs_endpoint ON endpoint_configs (endpoint)")
            print("  ✓ 'endpoint_configs' table created")
        else:
            print("  ✓ 'endpoint_configs' table already exists")
        
        # ===== LAYER_SNAPSHOTS TABLOSU =====
        print("\n[layer_snapshots table]")
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='layer_snapshots'")
        if not cursor.fetchone():
            print("  Creating 'layer_snapshots' table...")
            cursor.execute("""
                CREATE TABLE layer_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint VARCHAR(32) NOT NULL,
                    unrealized_pnl FLOAT DEFAULT 0.0,
                    total_cost FLOAT DEFAULT 0.0,
                    position_count INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX ix_layer_snapshots_endpoint ON layer_snapshots (endpoint)")
            cursor.execute("CREATE INDEX ix_layer_snapshots_created_at ON layer_snapshots (created_at)")
            print("  ✓ 'layer_snapshots' table created")
        else:
            print("  ✓ 'layer_snapshots' table already exists")
        
        conn.commit()
        conn.close()
        
        print("\n" + "=" * 50)
        print("✅ Migration completed successfully!")
        print("=" * 50)
        return True
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = migrate()
    sys.exit(0 if success else 1)
