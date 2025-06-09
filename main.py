import os
import time
import smbus2
import bme280
import json

from datetime import datetime

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

import influxdb_client
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.rest import ApiException

import meshtastic.serial_interface
from meshtastic.protobuf import telemetry_pb2, portnums_pb2

from meshtastic import BROADCAST_ADDR

# --- InfluxDB config ---
token = os.getenv("INFLUX_TOKEN")
if token is None:
    raise RuntimeError("Environment variable INFLUX_TOKEN is not set.")

org = os.getenv("INFLUX_ORG")
if org is None:
    raise RuntimeError("Environment variable INFLUX_ORG is not set.")

url = os.getenv("INFLUX_URL")
if url is None:
    raise RuntimeError("Environment variable INFLUX_URL is not set.")

bucket = os.getenv("INFLUX_BUCKET")
if bucket is None:
    raise RuntimeError("Environment variable INFLUX_BUCKET is not set.")

# --- Modbus config ---
port = os.getenv("MODBUS_DEVICE")
if port is None:
    raise RuntimeError("Environment variable MODBUS_DEVICE is not set.")
baudrate = 4800
unit_id = 1  # Modbus slave address

# --- Set up clients ---
client = ModbusSerialClient(
    port=port,
    baudrate=baudrate,
    timeout=1,
    parity='N',
    stopbits=1,
    bytesize=8
)

# --- Buffer file ---
buffer_file = os.getenv("BUFFER_FILE")
if buffer_file is None:
    buffer_file = "influx_buffer.jsonl"

def buffer_point(point: influxdb_client.Point):
    """Write unsent point to a buffer file."""
    with open(buffer_file, "a") as f:
        f.write(point.to_line_protocol() + "\n")

def flush_buffered_points():
    """Try to send buffered points if any."""
    if not os.path.exists(buffer_file):
        return

    try:
        with open(buffer_file, "r") as f:
            lines = f.readlines()

        if lines:
            write_api.write(bucket=bucket, org=org, record=lines)
            os.remove(buffer_file)  # Clear buffer if successful
            print(f"✅ Flushed {len(lines)} buffered points.")
    except Exception as e:
        print(f"⚠️ Failed to flush buffered data: {e}")


influx_client = None
write_api = None

# --- BME280 config ---
BME280_ADDRESS = 0x76  # Change if needed

# Set up I2C and BME280
bus = smbus2.SMBus(1)
calibration_params = bme280.load_calibration_params(bus, BME280_ADDRESS)

# Timing for BME280 reads
bme280_last_read = 0
bme280_interval = 10  # seconds

# Telemetry Buffer
wind_speed = 0
wind_direction = 0
last_wind_data = 0

try:
    # Connect to InfluxDB
    try:
        influx_client = InfluxDBClient(url=url, token=token, org=org)
        write_api = influx_client.write_api(write_options=SYNCHRONOUS)
        print("Connected to InfluxDB.")
    except Exception as e:
        print(f"⚠️ InfluxDB connection failed: {e}")

    # Connect to Modbus
    if client.connect():
        print("Connected to Modbus. Reading every second (Ctrl+C to stop)...")

        while True:
            flush_buffered_points()  # Try to send previously buffered data
            
            now = time.time()

            # --- Read wind sensor ---
            try:
                speed_response = client.read_holding_registers(address=0, count=1, slave=unit_id)
                dir_response = client.read_holding_registers(address=1, count=1, slave=unit_id)

                if (
                    speed_response is not None and not speed_response.isError() and
                    dir_response is not None and not dir_response.isError() and
                    hasattr(speed_response, 'registers') and hasattr(dir_response, 'registers')
                ):
                    wind_speed_raw = speed_response.registers[0]
                    wind_direction = dir_response.registers[0]
                    wind_speed = wind_speed_raw / 100.0

                    last_wind_data = now

                    print(f"Wind speed: {wind_speed:.2f} m/s | Direction: {wind_direction}°")

                    if write_api:
                        timestamp = datetime.utcnow()  # or use time.time_ns() for nanosecond precision
                            
                        point = (
                            Point("wind_sensor")
                            .field("speed", wind_speed)
                            .field("direction", wind_direction)
                            .time(timestamp)
                        )
                        
                        try:                
                            write_api.write(bucket=bucket, org=org, record=point)
                        except (ApiException, Exception) as e:
                            print(f"⚠️ InfluxDB write failed: {e}, buffering locally.")
                            buffer_point(point)
                    else:
                        print("⚠️ Skipping InfluxDB write (not connected).")
                else:
                    print("⚠️ Sensor not responding or data invalid.")
            except (ModbusException, Exception) as e:
                print(f"⚠️ Modbus error: {e}")

            # --- Read BME280 every 10 seconds ---
            if now - bme280_last_read >= bme280_interval:
                try:
                    bme_data = bme280.sample(bus, BME280_ADDRESS, calibration_params)
                    print(f"BME280 | Temp: {bme_data.temperature:.2f} °C | Pressure: {bme_data.pressure:.2f} hPa | Humidity: {bme_data.humidity:.2f} %")

                    if write_api:
                        timestamp = datetime.utcnow()
                            
                        point = (
                            Point("temperature_sensor")
                            .field("temperature", bme_data.temperature)
                            .field("pressure", bme_data.pressure)
                            .field("humidity", bme_data.humidity)
                            .time(timestamp)
                        )
                        
                        try:
                            write_api.write(bucket=bucket, org=org, record=point)
                        except (ApiException, Exception) as e:
                            print(f"⚠️ InfluxDB write failed: {e}, buffering locally.")
                            buffer_point(point)
                except Exception as e:
                    print(f"⚠️ BME280 read/write failed: {e}")

                bme280_last_read = now

            time.sleep(1)

    else:
        print("❌ Failed to connect to Modbus device.")

finally:
    client.close()
    if influx_client:
        influx_client.close()
    bus.close()
    print("Connections closed.")