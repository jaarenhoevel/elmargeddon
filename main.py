import os
import time
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

import influxdb_client
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.rest import ApiException

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

influx_client = None
write_api = None

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
                    print(f"Wind speed: {wind_speed:.2f} m/s | Direction: {wind_direction}°")

                    if write_api:
                        try:
                            point = (
                                Point("wind_sensor")
                                .field("speed", wind_speed)
                                .field("direction", wind_direction)
                            )
                            write_api.write(bucket=bucket, org=org, record=point)
                        except (ApiException, Exception) as e:
                            print(f"⚠️ InfluxDB write failed: {e}")
                    else:
                        print("⚠️ Skipping InfluxDB write (not connected).")
                else:
                    print("⚠️ Sensor not responding or data invalid.")

            except (ModbusException, Exception) as e:
                print(f"⚠️ Modbus error: {e}")

            time.sleep(1)

    else:
        print("❌ Failed to connect to Modbus device.")

finally:
    client.close()
    if influx_client:
        influx_client.close()
    print("Connections closed.")
