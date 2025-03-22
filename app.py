import time
import json
import sqlite3
import threading
from datetime import datetime, timezone
from flask import Flask, Response, render_template, request, redirect, url_for, flash
import paho.mqtt.client as mqtt

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Change to a secure secret in production

DATABASE = 'sensor_data.db'
current_training_round_id = None  # Global flag: None means no round is active

# Global dictionary to track last message timestamp for each sensor.
online_sensors = {}

def get_db_connection():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    with conn:
        # Configuration table for training and MQTT settings.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Default training settings.
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('training_name', 'Default Training')")
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('sensor_id', '64E833ACC838652B')")
        # Default MQTT settings.
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('mqtt_broker', 'broker.mqtt.cool')")
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('mqtt_port', '1883')")
        
        # Training round table: each round has start and stop times.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS training_round (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                training_name TEXT,
                sensor_id TEXT,
                start_time TEXT,
                stop_time TEXT
            )
        ''')
        
        # Sensor history table: records sensor events during a training round.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sensor_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                reed_value INTEGER,
                event TEXT,
                forces TEXT,
                training_round_id INTEGER,
                FOREIGN KEY(training_round_id) REFERENCES training_round(id)
            )
        ''')
    conn.close()

# ---------------- MQTT Subscriber ----------------
# Global variables for MQTT. These will be read from the DB on startup.
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
        # Extract sensor id from topic (last part)
        sensor_id_in_topic = topic.split('/')[-1]
        # Update online sensor timestamp.
        online_sensors[sensor_id_in_topic] = time.time()

        payload = json.loads(msg.payload.decode())
        # Expected payload: {"reed": int, "critical": bool, "forces": [int, int, ...]}
        reed_value = payload.get("reed", None)
        event = "Head" if payload.get("critical", False) else "Body"
        forces = payload.get("forces", [])
        forces_json = json.dumps(forces)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Get the configured sensor id from settings
        conn = get_db_connection()
        cur = conn.execute("SELECT value FROM config WHERE key = 'sensor_id'")
        row = cur.fetchone()
        config_sensor_id = row['value'] if row else None
        conn.close()
        
        # Record sensor data only if sensor id matches and a round is active.
        if sensor_id_in_topic == config_sensor_id and current_training_round_id is not None:
            conn = get_db_connection()
            conn.execute(
                "INSERT INTO sensor_history (timestamp, reed_value, event, forces, training_round_id) VALUES (?, ?, ?, ?, ?)",
                (timestamp, reed_value, event, forces_json, current_training_round_id)
            )
            conn.commit()
            conn.close()
            print(f"Recorded sensor data: {timestamp} - Reed:{reed_value} - {event} - {forces_json}")
    except Exception as e:
        print("Error in on_message:", e)

def mqtt_thread():
    # Load MQTT config from database
    conn = get_db_connection()
    cur = conn.execute("SELECT value FROM config WHERE key = 'mqtt_broker'")
    row = cur.fetchone()
    broker = row['value'] if row else MQTT_BROKER
    cur = conn.execute("SELECT value FROM config WHERE key = 'mqtt_port'")
    row = cur.fetchone()
    try:
        port = int(row['value']) if row else MQTT_PORT
    except ValueError:
        port = MQTT_PORT
    conn.close()
    
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker, port, 60)
    client.loop_forever()

mqtt_thread_instance = threading.Thread(target=mqtt_thread)
mqtt_thread_instance.daemon = True
mqtt_thread_instance.start()

# ---------------- Routes ----------------

@app.route('/')
def index():
     return render_template('index.html', current_year=datetime.now(timezone.utc).year)

# Changed settings page to "Config MQTT"
@app.route('/settings', methods=['GET', 'POST'])
def settings():
    conn = get_db_connection()
    if request.method == 'POST':
        training_name = request.form.get('training_name', 'Default Training')
        sensor_id = request.form.get('sensor_id', '64E833ACC838652B')
        mqtt_broker = request.form.get('mqtt_broker', 'broker.mqtt.cool')
        mqtt_port = request.form.get('mqtt_port', '1883')
        conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)", ('training_name', training_name))
        conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)", ('sensor_id', sensor_id))
        conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)", ('mqtt_broker', mqtt_broker))
        conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)", ('mqtt_port', mqtt_port))
        conn.commit()
        conn.close()
        flash("MQTT configuration updated successfully!")
        return redirect(url_for('settings'))
    else:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        config = {row['key']: row['value'] for row in rows}
        conn.close()
        return render_template('settings.html', config=config)

@app.route('/record', methods=['GET', 'POST'])
def record():
    global current_training_round_id, online_sensors
    conn = get_db_connection()
    if request.method == 'POST':
        training_name = request.form.get('training_name', 'Default Training')
        sensor_id = request.form.get('sensor_id', '64E833ACC838652B')
        conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)", ('training_name', training_name))
        conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)", ('sensor_id', sensor_id))
        conn.commit()
        conn.close()
        flash("Settings updated successfully!")
        return redirect(url_for('start'), code=307)
    else:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        config = {row['key']: row['value'] for row in rows}
        conn.close()

        # Filter online sensors from the global online_sensors dict.
        threshold = time.time() - 60  # sensors active in the last 60 seconds
        online_list = []
        for sensor_id, last_seen in online_sensors.items():
            if last_seen > threshold:
                online_list.append({'sensor_id': sensor_id,
                                    'last_seen': datetime.fromtimestamp(last_seen).strftime('%Y-%m-%d %H:%M:%S')})
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
    
    conn = get_db_connection()
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
    current_training_round_id = cur.lastrowid
    conn.close()
    flash("Training round started!")
    return redirect(url_for('record'))

@app.route('/stop', methods=['POST'])
def stop():
    global current_training_round_id
    if current_training_round_id is None:
        flash("No training round in progress!")
        return redirect(url_for('record'))
    
    stop_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    conn.execute("UPDATE training_round SET stop_time = ? WHERE id = ?", (stop_time, current_training_round_id))
    conn.commit()
    conn.close()
    flash("Training round stopped and saved!")
    current_training_round_id = None
    return redirect(url_for('record'))

# SSE: Stream sensor data for the current training round.
@app.route('/stream')
def stream():
    def event_stream():
        last_sent_id = 0
        while True:
            if current_training_round_id is not None:
                conn = get_db_connection()
                rows = conn.execute("""
                    SELECT id, timestamp, reed_value, event, forces 
                    FROM sensor_history 
                    WHERE training_round_id = ? AND id > ?
                    ORDER BY id ASC
                """, (current_training_round_id, last_sent_id)).fetchall()
                conn.close()
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

# Real-time visualization page (boxing silhouette with marker and force values)
@app.route('/visualize_mockup')
def visualize_mockup():
    return render_template('visualize_mockup.html', training_active=(current_training_round_id is not None))

# History: List all past training rounds.
@app.route('/history')
def history():
    conn = get_db_connection()
    rounds = conn.execute("SELECT * FROM training_round ORDER BY start_time DESC").fetchall()
    conn.close()
    return render_template('history.html', rounds=rounds)

# Round Details: Show sensor events for a specific training round.
@app.route('/history/<int:round_id>')
def round_details(round_id):
    conn = get_db_connection()
    round_info = conn.execute("SELECT * FROM training_round WHERE id = ?", (round_id,)).fetchone()
    sensor_events = conn.execute("SELECT * FROM sensor_history WHERE training_round_id = ? ORDER BY timestamp ASC", (round_id,)).fetchall()
    conn.close()
    # Process each sensor event to add a list for force sensor values.
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

# New page: Current online sensors.
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