import os
import time
import json
import threading
from datetime import datetime, timezone
from flask import Flask, Response, render_template, request, redirect, url_for, flash
import paho.mqtt.client as mqtt

# Determine database type by checking if DATABASE_URL is set.
USE_SQLITE = not bool(os.getenv("DATABASE_URL"))
if USE_SQLITE:
    import sqlite3
    DATABASE = 'sensor_data.db'
else:
    import psycopg2
    import psycopg2.extras
    DATABASE = os.getenv("DATABASE_URL")

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Change to a secure secret in production

current_training_round_id = None  # Global flag: None means no round is active
online_sensors = {}  # Global dictionary to track last message timestamp for each sensor.

########################################
#  Database Connection Wrapper
########################################
class DBConnection:
    def __init__(self, conn, use_sqlite):
        self.conn = conn
        self.use_sqlite = use_sqlite

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.conn.close()

    def execute(self, query, params=None):
        if params is None:
            params = []
        # For production (PostgreSQL) convert '?' placeholders to '%s'
        if not self.use_sqlite:
            query = query.replace("?", "%s")
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = self.conn.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        self.conn.commit()

def get_db_connection():
    if USE_SQLITE:
        conn = sqlite3.connect(DATABASE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return DBConnection(conn, use_sqlite=True)
    else:
        conn = psycopg2.connect(DATABASE)
        return DBConnection(conn, use_sqlite=False)

########################################
# Helper function for config upsert
########################################
def insert_config(conn, key, value):
    if USE_SQLITE:
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (key, value))
    else:
        # PostgreSQL upsert syntax.
        conn.execute("INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING", (key, value))

########################################
# Initialize Database
########################################
def init_db():
    with get_db_connection() as conn:
        # Create config table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Insert default config values
        insert_config(conn, 'training_name', 'Default Training')
        insert_config(conn, 'sensor_id', '64E833ACC838652B')
        insert_config(conn, 'mqtt_broker', 'broker.mqtt.cool')
        insert_config(conn, 'mqtt_port', '1883')

        # Create training_round table (auto-increment syntax depends on DB)
        if USE_SQLITE:
            training_round_sql = '''
                CREATE TABLE IF NOT EXISTS training_round (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    training_name TEXT,
                    sensor_id TEXT,
                    start_time TEXT,
                    stop_time TEXT
                )
            '''
            sensor_history_sql = '''
                CREATE TABLE IF NOT EXISTS sensor_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    reed_value INTEGER,
                    event TEXT,
                    forces TEXT,
                    training_round_id INTEGER,
                    FOREIGN KEY(training_round_id) REFERENCES training_round(id)
                )
            '''
        else:
            training_round_sql = '''
                CREATE TABLE IF NOT EXISTS training_round (
                    id SERIAL PRIMARY KEY,
                    training_name TEXT,
                    sensor_id TEXT,
                    start_time TEXT,
                    stop_time TEXT
                )
            '''
            sensor_history_sql = '''
                CREATE TABLE IF NOT EXISTS sensor_history (
                    id SERIAL PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    reed_value INTEGER,
                    event TEXT,
                    forces TEXT,
                    training_round_id INTEGER,
                    FOREIGN KEY(training_round_id) REFERENCES training_round(id)
                )
            '''
        conn.execute(training_round_sql)
        conn.execute(sensor_history_sql)
        conn.commit()

########################################
# MQTT Subscriber Setup
########################################
MQTT_BROKER = "broker.mqtt.cool"
MQTT_PORT = 1883
MQTT_TOPIC = "espboxing/sensors/#"

def on_connect(client, userdata, flags, rc):
    print("MQTT connected with result code " + str(rc))
    client.subscribe(MQTT_TOPIC)

def on_message(client, userdata, msg):
    global current_training_round_id, online_sensors
    topic = msg.topic  # e.g., "espboxing/sensors/64E833ACC838652B"
    try:
        sensor_id_in_topic = topic.split('/')[-1]
        online_sensors[sensor_id_in_topic] = time.time()

        payload = json.loads(msg.payload.decode())
        # Expected payload: {"reed": int, "critical": bool, "forces": [int, int, ...]}
        reed_value = payload.get("reed", None)
        event = "Head" if payload.get("critical", False) else "Body"
        forces = payload.get("forces", [])
        forces_json = json.dumps(forces)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Get the configured sensor id from settings
        with get_db_connection() as conn:
            cur = conn.execute("SELECT value FROM config WHERE key = 'sensor_id'")
            row = cur.fetchone()
            config_sensor_id = row['value'] if row else None

        # Record sensor data only if sensor id matches and a round is active.
        if sensor_id_in_topic == config_sensor_id and current_training_round_id is not None:
            with get_db_connection() as conn:
                conn.execute(
                    "INSERT INTO sensor_history (timestamp, reed_value, event, forces, training_round_id) VALUES (?, ?, ?, ?, ?)",
                    (timestamp, reed_value, event, forces_json, current_training_round_id)
                )
                conn.commit()
            print(f"Recorded sensor data: {timestamp} - Reed:{reed_value} - {event} - {forces_json}")
    except Exception as e:
        print("Error in on_message:", e)

def mqtt_thread():
    with get_db_connection() as conn:
        cur = conn.execute("SELECT value FROM config WHERE key = 'mqtt_broker'")
        row = cur.fetchone()
        broker = row['value'] if row else MQTT_BROKER
        cur = conn.execute("SELECT value FROM config WHERE key = 'mqtt_port'")
        row = cur.fetchone()
        try:
            port = int(row['value']) if row else MQTT_PORT
        except ValueError:
            port = MQTT_PORT

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker, port, 60)
    client.loop_forever()

mqtt_thread_instance = threading.Thread(target=mqtt_thread)
mqtt_thread_instance.daemon = True
mqtt_thread_instance.start()

########################################
# Routes
########################################
@app.route('/')
def index():
    return render_template('index.html', current_year=datetime.now(timezone.utc).year)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        training_name = request.form.get('training_name', 'Default Training')
        sensor_id = request.form.get('sensor_id', '64E833ACC838652B')
        mqtt_broker = request.form.get('mqtt_broker', 'broker.mqtt.cool')
        mqtt_port = request.form.get('mqtt_port', '1883')
        with get_db_connection() as conn:
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else 
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('training_name', training_name))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else 
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('sensor_id', sensor_id))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else 
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('mqtt_broker', mqtt_broker))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else 
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('mqtt_port', mqtt_port))
            conn.commit()
        flash("MQTT configuration updated successfully!")
        return redirect(url_for('settings'))
    else:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT key, value FROM config")
            rows = cur.fetchall()
            config = {row['key']: row['value'] for row in rows}
        return render_template('settings.html', config=config)

@app.route('/record', methods=['GET', 'POST'])
def record():
    global current_training_round_id, online_sensors
    if request.method == 'POST':
        training_name = request.form.get('training_name', 'Training Name')
        sensor_id = request.form.get('sensor_id', '')
        with get_db_connection() as conn:
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else 
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('training_name', training_name))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else 
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('sensor_id', sensor_id))
            conn.commit()
        flash("Settings updated successfully!")
        return redirect(url_for('start'), code=307)
    else:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT key, value FROM config")
            rows = cur.fetchall()
            config = {row['key']: row['value'] for row in rows}

        # Determine which sensors are online (active in the last 60 seconds).
        threshold = time.time() - 60
        online_list = []
        for sensor_id, last_seen in online_sensors.items():
            if last_seen > threshold:
                online_list.append({
                    'sensor_id': sensor_id,
                    'last_seen': datetime.fromtimestamp(last_seen).strftime('%Y-%m-%d %H:%M:%S')
                })
        return render_template('record.html',
                               training_active=(current_training_round_id is not None),
                               config=config,
                               online_sensors=online_list)

@app.route('/start', methods=['POST'])
def start():
    global current_training_round_id
    if current_training_round_id is not None:
        flash("A training round is already in progress!")
        return redirect(url_for('record'))
    
    with get_db_connection() as conn:
        cur = conn.execute("SELECT value FROM config WHERE key = 'training_name'")
        row = cur.fetchone()
        training_name = row['value'] if row else 'Default Training'
        cur = conn.execute("SELECT value FROM config WHERE key = 'sensor_id'")
        row = cur.fetchone()
        sensor_id = row['value'] if row else '64E833ACC838652B'
        start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cur = conn.execute(
            "INSERT INTO training_round (training_name, sensor_id, start_time) VALUES (?, ?, ?)",
            (training_name, sensor_id, start_time)
        )
        conn.commit()
        current_training_round_id = cur.lastrowid  if USE_SQLITE else cur.fetchone()['id']  # For PostgreSQL, you might need to adjust
    flash("Training round started!")
    return redirect(url_for('record'))

@app.route('/stop', methods=['POST'])
def stop():
    global current_training_round_id
    if current_training_round_id is None:
        flash("No training round in progress!")
        return redirect(url_for('record'))
    
    stop_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_db_connection() as conn:
        conn.execute("UPDATE training_round SET stop_time = ? WHERE id = ?", (stop_time, current_training_round_id))
        conn.commit()
    flash("Training round stopped and saved!")
    current_training_round_id = None
    return redirect(url_for('record'))

@app.route('/stream')
def stream():
    def event_stream():
        last_sent_id = 0
        while True:
            if current_training_round_id is not None:
                with get_db_connection() as conn:
                    cur = conn.execute("""
                        SELECT id, timestamp, reed_value, event, forces 
                        FROM sensor_history 
                        WHERE training_round_id = ? AND id > ?
                        ORDER BY id ASC
                    """, (current_training_round_id, last_sent_id))
                    rows = cur.fetchall()
                for row in rows:
                    last_sent_id = row["id"]
                    data = {
                        "timestamp": row["timestamp"],
                        "reed_value": row["reed_value"],
                        "event": row["event"],
                        "forces": json.loads(row["forces"]) if row["forces"] else []
                    }
                    yield f"data: {json.dumps(data)}\n\n"
            else:
                yield f"data: {json.dumps({'heartbeat': True})}\n\n"
            time.sleep(1)
    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/visualize_mockup')
def visualize_mockup():
    return render_template('visualize_mockup.html', training_active=(current_training_round_id is not None))

@app.route('/history')
def history():
    with get_db_connection() as conn:
        cur = conn.execute("SELECT * FROM training_round ORDER BY start_time DESC")
        rounds = cur.fetchall()
    return render_template('history.html', rounds=rounds)

@app.route('/history/<int:round_id>')
def round_details(round_id):
    with get_db_connection() as conn:
        cur = conn.execute("SELECT * FROM training_round WHERE id = ?", (round_id,))
        round_info = cur.fetchone()
        cur = conn.execute("SELECT * FROM sensor_history WHERE training_round_id = ? ORDER BY timestamp ASC", (round_id,))
        sensor_events = cur.fetchall()
    processed_events = []
    for row in sensor_events:
        d = dict(row)
        if d.get("forces"):
            try:
                d["forces_list"] = json.loads(d["forces"])
            except:
                d["forces_list"] = []
        else:
            d["forces_list"] = []
        processed_events.append(d)
    return render_template('round_details.html', round=round_info, sensor_events=processed_events)

@app.route('/online')
def online():
    threshold = time.time() - 60  # sensors active in the last 60 seconds
    online_list = []
    for sensor_id, last_seen in online_sensors.items():
        if last_seen > threshold:
            online_list.append({
                'sensor_id': sensor_id,
                'last_seen': datetime.fromtimestamp(last_seen).strftime('%Y-%m-%d %H:%M:%S')
            })
    return render_template('online.html', online_list=online_list)

if __name__ == '__main__':
    init_db()
    app.run(host="0.0.0.0", port=5000)
