import sqlite3

DB_PATH = "mcorp.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    db = get_connection()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            user_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, item_id)
        )
    """)

    db.commit()
    db.close()


def add_item(user_id, item_id, quantity=1):
    db = get_connection()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO inventory (
            user_id,
            item_id,
            quantity
        )
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, item_id)
        DO UPDATE SET
            quantity = quantity + excluded.quantity
    """, (
        user_id,
        item_id,
        quantity
    ))

    db.commit()
    db.close()


def remove_item(user_id, item_id, quantity=1):
    db = get_connection()
    cursor = db.cursor()

    cursor.execute("""
        UPDATE inventory
        SET quantity = quantity - ?
        WHERE user_id = ?
          AND item_id = ?
          AND quantity >= ?
    """, (
        quantity,
        user_id,
        item_id,
        quantity
    ))

    success = cursor.rowcount > 0

    cursor.execute("""
        DELETE FROM inventory
        WHERE quantity <= 0
    """)

    db.commit()
    db.close()

    return success


def get_inventory(user_id):
    db = get_connection()
    cursor = db.cursor()

    cursor.execute("""
        SELECT item_id, quantity
        FROM inventory
        WHERE user_id = ?
          AND quantity > 0
        ORDER BY item_id
    """, (user_id,))

    items = cursor.fetchall()

    db.close()

    return items
