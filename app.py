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
        # Drop old config table (if exists)
        if (os.getenv("DROP_TABLES_ON_STARTUP") == "True" or os.getenv("DROP_TABLES_ON_STARTUP") == "true"):
            conn.execute("DROP TABLE IF EXISTS training_round")
        
        # Create config table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Insert default config values
        insert_config(conn, 'mqtt_broker', 'broker.mqtt.cool')
        insert_config(conn, 'mqtt_port', '1883')
        insert_config(conn, 'sensor_label1', 'หัว')
        insert_config(conn, 'sensor_label2', 'ลำตัว')
        insert_config(conn, 'sensor_label3', 'ท้อง')
        insert_config(conn, 'sensor_label4', 'ขา')
        
        # Create training_round table (auto-increment syntax depends on DB)
        if USE_SQLITE:
            training_round_sql = '''
                CREATE TABLE IF NOT EXISTS training_round (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    training_name TEXT,
                    sensor_id TEXT,
                    map_force_position TEXT,
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
                    p1 INTEGER,
                    p2 INTEGER,
                    p3 INTEGER,
                    p4 INTEGER,
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
                    map_force_position TEXT,
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
                    p1 INTEGER,
                    p2 INTEGER,
                    p3 INTEGER,
                    p4 INTEGER,
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
    topic = msg.topic  # e.g., "espboxing/sensors/64E833ACC838652B"
    try:
        sensor_id_in_topic = topic.split('/')[-1]
        online_sensors[sensor_id_in_topic] = time.time()

        payload = json.loads(msg.payload.decode())
        # Expected payload: {"reed": int, "critical": bool, "forces": [int, int, ...]}
        reed_value = payload.get("reed", None)
        event = "Head" if payload.get("critical", True) else "Body"
        forces_json = payload.get("forces", [])
        forces_json_str = json.dumps(forces_json)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"Received message: {topic} - {payload}")
        
        # Get the configured sensor id from settings
        with get_db_connection() as conn:
            cur = conn.execute("SELECT sensor_id, map_force_position FROM training_round WHERE id = (?)", (current_training_round_id,))
            row = cur.fetchone()
            config_sensor_id = row['sensor_id'] if row else None
            map_force_position = row['map_force_position'] if row else None
            map_force_position_json = json.loads(map_force_position) if map_force_position else None
            
            # Map forces to positions
            pos1, pos2, pos3, pos4 = None, None, None, None
            if map_force_position_json:
                try:
                    pos1 = forces_json[int(map_force_position_json[0]) - 1] if not reed_value else ''
                    pos2 = forces_json[int(map_force_position_json[1]) - 1]
                    pos3 = forces_json[int(map_force_position_json[2]) - 1]
                    pos4 = forces_json[int(map_force_position_json[3]) - 1]
                    
                except:
                    pass

        # Record sensor data only if sensor id matches and a round is active.
        if sensor_id_in_topic == config_sensor_id and current_training_round_id is not None:
            with get_db_connection() as conn:
                conn.execute("INSERT INTO sensor_history (timestamp, reed_value, event, forces, p1, p2, p3, p4, training_round_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (timestamp, reed_value, event, forces_json_str, pos1, pos2, pos3, pos4, current_training_round_id))
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

@app.context_processor
def inject_current_year():
    from datetime import datetime
    return {'current_year': datetime.now().year}

########################################
# Routes
########################################
@app.route('/')
def index():
    return render_template('index.html', current_year=datetime.now().year)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        mqtt_broker = request.form.get('mqtt_broker', 'broker.mqtt.cool')
        mqtt_port = request.form.get('mqtt_port', '1883')
        sensor_label1 = request.form.get('sensor_label1', 'หัว')
        sensor_label2 = request.form.get('sensor_label2', 'ลำตัว')
        sensor_label3 = request.form.get('sensor_label3', 'ท้อง')
        sensor_label4 = request.form.get('sensor_label4', 'ขา')
        default_position_sensor1 = request.form.get('default_position_sensor1', '1')
        default_position_sensor2 = request.form.get('default_position_sensor2', '2')
        default_position_sensor3 = request.form.get('default_position_sensor3', '3')
        default_position_sensor4 = request.form.get('default_position_sensor4', '4')
        with get_db_connection() as conn:
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else 
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('mqtt_broker', mqtt_broker))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else 
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('mqtt_port', mqtt_port))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('sensor_label1', sensor_label1))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('sensor_label2', sensor_label2))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('sensor_label3', sensor_label3))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('sensor_label4', sensor_label4))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('default_position_sensor1', default_position_sensor1))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('default_position_sensor2', default_position_sensor2))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('default_position_sensor3', default_position_sensor3))
            conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)" if USE_SQLITE else
                         "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         ('default_position_sensor4', default_position_sensor4))
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
        # Fetch training details
        training_name = request.form.get('training_name', '').strip()
        sensor_id = request.form.get('sensor_id', '').strip()

        # Fetch user-defined labels and positions
        sensor_label1 = request.form.get('sensor_label1', '')
        sensor_label2 = request.form.get('sensor_label2', '')
        sensor_label3 = request.form.get('sensor_label3', '')
        sensor_label4 = request.form.get('sensor_label4', '')

        default_position_sensor1 = request.form.get('default_position_sensor1', '')
        default_position_sensor2 = request.form.get('default_position_sensor2', '')
        default_position_sensor3 = request.form.get('default_position_sensor3', '')
        default_position_sensor4 = request.form.get('default_position_sensor4', '')

        # Ensure all sensors and positions are selected
        if "" in {sensor_label1, sensor_label2, sensor_label3, sensor_label4,
                  default_position_sensor1, default_position_sensor2, default_position_sensor3, default_position_sensor4}:
            flash("⚠ การตั้งค่าปัจจุบันเซ็นเซ็อร์ไม่ได้ตั้งค่าครบทุกตำแหน่งโปรดตรวจสอบ!", "warning")

        # Ensure sensors are uniquely assigned
        map_force_position = [sensor_label1, sensor_label2, sensor_label3, sensor_label4]
        
        # Start training session
        if current_training_round_id is None:
            start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with get_db_connection() as conn:
                cur = conn.execute(
                    "INSERT INTO training_round (training_name, sensor_id, map_force_position, start_time) VALUES (?, ?, ?, ?)"
                    if USE_SQLITE else
                    "INSERT INTO training_round (training_name, sensor_id, map_force_position, start_time) VALUES (%s, %s, %s, %s) RETURNING id",
                    (training_name, sensor_id, json.dumps(map_force_position), start_time)
                )
                conn.commit()
                current_training_round_id = cur.lastrowid if USE_SQLITE else cur.fetchone()['id']

            flash("🎯 เริ่มต้นการฝึกซ้อมแล้ว!", "success")

        return redirect(url_for('record'))

    # Load configuration and online sensors
    with get_db_connection() as conn:
        cur = conn.execute("SELECT key, value FROM config")
        config = {row['key']: row['value'] for row in cur.fetchall()}

    threshold = time.time() - 60
    online_list = [
        {'sensor_id': sensor_id, 'last_seen': datetime.fromtimestamp(last_seen).strftime('%Y-%m-%d %H:%M:%S')}
        for sensor_id, last_seen in online_sensors.items() if last_seen > threshold
    ]

    return render_template('record.html', config=config, online_sensors=online_list, training_active=(current_training_round_id is not None))

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
    flash("บันทึกการฝึกซ้อมเสร็จสิ้นแล้ว!")
    current_training_round_id = None
    return redirect(url_for('history'))

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
    training_name_filter = request.args.get('training_name', '').strip()
    sensor_id_filter = request.args.get('sensor_id', '').strip()
    sort_by = request.args.get('sort_by', 'start_time')
    sort_order = request.args.get('sort_order', 'desc').lower()

    # Only allow sorting by these columns
    allowed_columns = ['id', 'training_name', 'sensor_id', 'start_time', 'stop_time']
    if sort_by not in allowed_columns:
        sort_by = 'start_time'
    if sort_order not in ['asc', 'desc']:
        sort_order = 'desc'

    query = "SELECT * FROM training_round"
    conditions = []
    params = []
    
    if training_name_filter:
        conditions.append("training_name LIKE ?")
        params.append(f"%{training_name_filter}%")
    
    if sensor_id_filter:
        conditions.append("sensor_id LIKE ?")
        params.append(f"%{sensor_id_filter}%")
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    
    query += f" ORDER BY {sort_by} {sort_order.upper()}"
    
    with get_db_connection() as conn:
        cur = conn.execute(query, params)
        rounds = cur.fetchall()
    
    return render_template('history.html', rounds=rounds)

@app.route('/history/<int:round_id>')
def round_details(round_id):
    with get_db_connection() as conn:
        # Load sensor labels from config
        cur = conn.execute("SELECT * FROM config")
        config = {row['key']: row['value'] for row in cur.fetchall()}

        # Fetch round details
        cur = conn.execute("SELECT * FROM training_round WHERE id = ?", (round_id,))
        round_info = cur.fetchone()

        # Fetch sensor event history
        cur = conn.execute("SELECT * FROM sensor_history WHERE training_round_id = ? ORDER BY timestamp ASC", (round_id,))
        sensor_events = cur.fetchall()

    # Process sensor event data
    processed_events = []
    for row in sensor_events:
        d = dict(row)
        # If 'forces' data exists, decode it into a list
        d["forces_list"] = json.loads(d["forces"]) if d.get("forces") else []
        processed_events.append(d)

    # Pass round details as "round", sensor events, and config to the template.
    return render_template("round_details.html", round=round_info, sensor_events=processed_events, config=config)

@app.route('/delete/<int:round_id>', methods=['POST'])
def delete_round(round_id):
    with get_db_connection() as conn:
        # Optionally, delete sensor_history records linked to the round
        conn.execute("DELETE FROM sensor_history WHERE training_round_id = ?", (round_id,))
        # Delete the training round
        conn.execute("DELETE FROM training_round WHERE id = ?", (round_id,))
        conn.commit()
    flash("Training round and associated sensor events deleted successfully!")
    return redirect(url_for('history'))

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
    app.run(host="0.0.0.0", port=5000, debug=True)
