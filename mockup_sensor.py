import paho.mqtt.client as mqtt
import random
import time
import json

# MQTT Configuration
MQTT_BROKER = "broker.mqtt.cool"
MQTT_PORT = 1883
DEVICE_ID = "mocup_sensor"
MQTT_TOPIC = f"espboxing/sensors/{DEVICE_ID}"

# Initialize MQTT client
client = mqtt.Client()

def connect_mqtt():
    try:
        client.connect(MQTT_BROKER, MQTT_PORT)
        print(f"Connected to MQTT Broker: {MQTT_BROKER}")
        client.loop_start()
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")

def send_sensor_data(active_sensor=None):
    force_values = [0] * 4  # Now 4 force sensors (A0, A1, A3, A4)
    reed_value = 0  # Default reed value
    
    # If a specific sensor is activated
    if active_sensor is not None:
        if active_sensor == 2:  # A2 is reed sensor
            reed_value = random.randint(100, 400)
        else:
            # Map the sensor index for A0, A1, A3, A4
            actual_index = active_sensor if active_sensor < 2 else active_sensor - 1
            force_values[actual_index] = random.randint(100, 400)

    payload = {
        "reed": reed_value,
        "critical": False,
        "forces": {
            "A0": force_values[0],
            "A1": force_values[1],
            "A3": force_values[2],
            "A4": force_values[3]
        }
    }
    
    try:
        client.publish(MQTT_TOPIC, json.dumps(payload))
        print(f"Sent data: {payload}")
    except Exception as e:
        print(f"Failed to send data: {e}")

def main():
    connect_mqtt()
    print("Enter number to simulate sensors:")
    print("0 - Force sensor A0")
    print("1 - Force sensor A1")
    print("2 - Reed sensor A2")
    print("3 - Force sensor A3")
    print("4 - Force sensor A4")
    print("q - Quit")
    
    while True:
        key = input("Enter sensor number (0-4) or 'q' to quit: ")
        
        if key == 'q':
            break
        elif key in ['0', '1', '2', '3', '4']:
            send_sensor_data(int(key))
        else:
            print("Invalid input. Please enter 0-4 or 'q'")

if __name__ == "__main__":
    try:
        main()
    finally:
        client.loop_stop()
        client.disconnect()
        print("Disconnected from MQTT broker")