#!/usr/bin/env python3
"""Elmageddon — Raspberry Pi weather station logger.

Samples wind (Modbus RTU), BME280 (I2C) and CPU temperature on independent
intervals and publishes the results to InfluxDB and MQTT. Points that cannot
be sent to InfluxDB are buffered to a local file and retried. MQTT publishes
each field to `weather/elmageddon/<measurement>/<field>` and announces the
entities to Home Assistant via `homeassistant/sensor/...` auto-discovery.

Each sensor is a small class implementing `sample() -> list[Reading]` and
declaring its `entities` (fields with units / device classes). The scheduler
hands readings to a list of sinks; add a sink (or swap a sensor) without
touching the loop.
"""

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import smbus2
import bme280
import paho.mqtt.client as mqtt
from pymodbus.client import ModbusSerialClient
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.rest import ApiException
from gpiozero import CPUTemperature


# --------------------------------------------------------------------------
# Logging — timestamped, level-prefixed, and warning-deduplicated so a
# disconnected sensor logs once instead of every sample.
# --------------------------------------------------------------------------
def _ts():
    return datetime.now().strftime("%H:%M:%S")

def log_info(msg):
    print(f"{_ts()} {msg}")

def log_warn(msg):
    print(f"{_ts()} WARN  {msg}")

_last_warnings = {}

def warn_once(key, msg):
    """Log `msg` only when it differs from the last warning for `key`."""
    if _last_warnings.get(key) != msg:
        _last_warnings[key] = msg
        log_warn(msg)

def clear_warning(key):
    _last_warnings.pop(key, None)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def require_env(name):
    value = os.getenv(name)
    if value is None:
        raise RuntimeError(f"Environment variable {name} is not set.")
    return value

def optional_env(name, default):
    return os.getenv(name) or default


@dataclass
class Config:
    influx_url: str
    influx_token: str
    influx_org: str
    influx_bucket: str
    modbus_device: str
    modbus_baudrate: int = 4800
    modbus_unit_id: int = 1
    bme280_address: int = 0x76
    bme280_bus: int = 1
    buffer_file: str = "influx_buffer.jsonl"
    loop_sleep: float = 1.0          # how often the scheduler wakes up
    flush_interval: float = 10.0    # how often to retry buffered points
    # MQTT — optional; if MQTT_HOST is unset, MQTT publishing is skipped.
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_prefix: str = "weather/elmageddon"   # data topic prefix
    mqtt_node: str = "elmageddon"             # HA node / device identifier

    @classmethod
    def from_env(cls):
        return cls(
            influx_url=require_env("INFLUX_URL"),
            influx_token=require_env("INFLUX_TOKEN"),
            influx_org=require_env("INFLUX_ORG"),
            influx_bucket=require_env("INFLUX_BUCKET"),
            modbus_device=require_env("MODBUS_DEVICE"),
            buffer_file=optional_env("BUFFER_FILE", "influx_buffer.jsonl"),
            mqtt_host=optional_env("MQTT_HOST", ""),
            mqtt_port=int(optional_env("MQTT_PORT", "1883")),
            mqtt_user=optional_env("MQTT_USER", ""),
            mqtt_password=optional_env("MQTT_PASSWORD", ""),
            mqtt_prefix=optional_env("MQTT_PREFIX", "weather/elmageddon"),
            mqtt_node=optional_env("MQTT_NODE", "elmageddon"),
        )


# --------------------------------------------------------------------------
# Readings & entities — the sensor output model shared by all sinks.
# --------------------------------------------------------------------------
@dataclass
class Reading:
    """A single measured value: one field of one measurement."""
    measurement: str
    field: str
    value: float


@dataclass
class Entity:
    """Declaration of a publishable field, used for HA auto-discovery."""
    measurement: str
    field: str
    name: str            # friendly name shown in Home Assistant
    unit: str = ""
    device_class: str = ""
    icon: str = ""


# --------------------------------------------------------------------------
# Buffer — append-only JSONL of line-protocol points, retried as one batch.
# A successful batch write deletes the file, so no point is lost to a
# partial flush.
# --------------------------------------------------------------------------
class Buffer:
    def __init__(self, path):
        self.path = path

    def append(self, point):
        with open(self.path, "a") as f:
            f.write(point.to_line_protocol() + "\n")

    def flush(self, write_api, bucket, org):
        if not os.path.exists(self.path):
            return 0
        with open(self.path) as f:
            lines = [line.strip() for line in f if line.strip()]
        if not lines:
            return 0
        try:
            write_api.write(bucket=bucket, org=org, record=lines)
        except ApiException as e:
            log_warn(f"buffer flush failed: {e}")
            return 0
        os.remove(self.path)
        log_info(f"flushed {len(lines)} buffered points")
        return len(lines)


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------
class Sink(ABC):
    """Receives the readings produced by each sensor sample."""

    @abstractmethod
    def publish(self, readings: list[Reading]) -> None: ...

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class InfluxSink(Sink):
    """Writes readings to InfluxDB, grouped into one point per measurement
    (preserving the multi-field schema). Failed writes are buffered to disk."""

    def __init__(self, config):
        self.bucket = config.influx_bucket
        self.org = config.influx_org
        self.buffer = Buffer(config.buffer_file)
        self.client = InfluxDBClient(
            url=config.influx_url, token=config.influx_token, org=config.influx_org
        )
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        log_info("InfluxDB client ready")

    def publish(self, readings):
        if not readings:
            return
        by_measurement: dict[str, list[Reading]] = {}
        for r in readings:
            by_measurement.setdefault(r.measurement, []).append(r)
        timestamp = datetime.utcnow()
        for measurement, group in by_measurement.items():
            point = Point(measurement).time(timestamp)
            for r in group:
                point = point.field(r.field, r.value)
            self._write(point)

    def _write(self, point):
        try:
            self.write_api.write(bucket=self.bucket, org=self.org, record=point)
        except Exception as e:
            log_warn(f"write failed, buffering: {e}")
            self.buffer.append(point)

    def flush(self):
        self.buffer.flush(self.write_api, self.bucket, self.org)

    def close(self):
        self.flush()                      # try to drain on shutdown
        self.write_api.close()
        self.client.close()


class MqttSink(Sink):
    """Publishes each field to `<prefix>/<measurement>/<field>` and announces
    entities to Home Assistant via `homeassistant/sensor/<node>/<id>/config`.
    Best-effort: paho queues and reconnects automatically while the broker is
    unreachable; InfluxDB remains the durable store."""

    def __init__(self, config, sensors):
        self.prefix = config.mqtt_prefix
        self.node = config.mqtt_node
        self.entities = [e for s in sensors for e in s.entities]

        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=self.node)
        except AttributeError:  # paho-mqtt < 2.0 has no CallbackAPIVersion
            client = mqtt.Client(client_id=self.node)

        if config.mqtt_user:
            client.username_pw_set(config.mqtt_user, config.mqtt_password)
        client.reconnect_delay_set(min_delay=1, max_delay=120)
        client.on_connect = self._on_connect
        self.client = client
        self.client.connect(config.mqtt_host, config.mqtt_port, 60)
        self.client.loop_start()
        log_info(f"MQTT connecting to {config.mqtt_host}:{config.mqtt_port}")

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log_warn(f"MQTT connect rejected (rc={rc})")
            return
        log_info("MQTT connected")
        self._publish_discovery()

    def _publish_discovery(self):
        for e in self.entities:
            topic = f"homeassistant/sensor/{self.node}/{e.measurement}_{e.field}/config"
            payload = {
                "name": e.name,
                "state_topic": f"{self.prefix}/{e.measurement}/{e.field}",
                "unique_id": f"{self.node}_{e.measurement}_{e.field}",
                "device": {
                    "identifiers": [self.node],
                    "name": "Elmageddon",
                    "model": "Weather Station",
                },
            }
            if e.unit:
                payload["unit_of_measurement"] = e.unit
            if e.device_class:
                payload["device_class"] = e.device_class
            if e.icon:
                payload["icon"] = e.icon
            self.client.publish(topic, json.dumps(payload), retain=True)

    def publish(self, readings):
        for r in readings:
            topic = f"{self.prefix}/{r.measurement}/{r.field}"
            self.client.publish(topic, str(r.value))

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


# --------------------------------------------------------------------------
# Sensors
# --------------------------------------------------------------------------
class Sensor(ABC):
    """A device sampled on a fixed interval. `sample()` returns readings and
    `entities` declares them for Home Assistant discovery."""

    name = "sensor"
    entities: list[Entity] = []

    def __init__(self, interval):
        self.interval = interval
        self.last_sample = 0.0

    @abstractmethod
    def sample(self) -> list[Reading]:
        ...

    def close(self):
        pass


class WindSensor(Sensor):
    """Anemometer over Modbus RTU. Register 0 = speed (/100 → m/s), 1 = direction."""

    name = "wind"
    entities = [
        Entity("wind_sensor", "speed", "Wind Speed", "m/s", "wind_speed"),
        Entity("wind_sensor", "direction", "Wind Direction", "°", "", "mdi:compass"),
    ]

    def __init__(self, client, unit_id, interval=1.0):
        super().__init__(interval)
        self.client = client
        self.unit_id = unit_id

    def sample(self):
        speed_resp = self.client.read_holding_registers(address=0, count=1, slave=self.unit_id)
        dir_resp = self.client.read_holding_registers(address=1, count=1, slave=self.unit_id)
        if speed_resp.isError() or dir_resp.isError():
            raise RuntimeError("no response from sensor")
        speed = speed_resp.registers[0] / 100.0
        direction = dir_resp.registers[0]
        log_info(f"wind   {speed:5.2f} m/s  {direction:3d}°")
        return [
            Reading("wind_sensor", "speed", speed),
            Reading("wind_sensor", "direction", direction),
        ]

    def close(self):
        self.client.close()


class Bme280Sensor(Sensor):
    """BME280 temperature/pressure/humidity over I2C."""

    name = "bme280"
    entities = [
        Entity("temperature_sensor", "temperature", "Temperature", "°C", "temperature"),
        Entity("temperature_sensor", "pressure", "Pressure", "hPa", "pressure"),
        Entity("temperature_sensor", "humidity", "Humidity", "%", "humidity"),
    ]

    def __init__(self, bus, address, calibration, interval=30.0):
        super().__init__(interval)
        self.bus = bus
        self.address = address
        self.calibration = calibration

    def sample(self):
        d = bme280.sample(self.bus, self.address, self.calibration)
        log_info(f"bme280  {d.temperature:5.2f} °C  {d.pressure:7.2f} hPa  {d.humidity:5.2f} %")
        return [
            Reading("temperature_sensor", "temperature", d.temperature),
            Reading("temperature_sensor", "pressure", d.pressure),
            Reading("temperature_sensor", "humidity", d.humidity),
        ]

    def close(self):
        self.bus.close()


class CpuTempSensor(Sensor):
    """On-board CPU temperature via gpiozero."""

    name = "cpu"
    entities = [
        Entity("device_metrics", "cpu_temperature", "CPU Temperature", "°C", "temperature"),
    ]

    def __init__(self, interval=30.0):
        super().__init__(interval)
        self._probe = CPUTemperature()

    def sample(self):
        temp = self._probe.temperature
        log_info(f"cpu    {temp:5.2f} °C")
        return [Reading("device_metrics", "cpu_temperature", temp)]


def build_sensors(config):
    """Construct sensors, tolerating individual failures: a sensor that can't
    be opened is skipped (with a warning) rather than aborting the whole logger."""
    sensors = []

    try:
        modbus = ModbusSerialClient(
            port=config.modbus_device, baudrate=config.modbus_baudrate,
            timeout=1, parity="N", stopbits=1, bytesize=8,
        )
        if modbus.connect():
            sensors.append(WindSensor(modbus, config.modbus_unit_id))
            log_info(f"wind   Modbus connected on {config.modbus_device}")
        else:
            log_warn("wind   Modbus connect failed, skipping")
    except Exception as e:
        log_warn(f"wind   {e}, skipping")

    try:
        bus = smbus2.SMBus(config.bme280_bus)
        calibration = bme280.load_calibration_params(bus, config.bme280_address)
        sensors.append(Bme280Sensor(bus, config.bme280_address, calibration))
        log_info("bme280  ready")
    except Exception as e:
        log_warn(f"bme280  {e}, skipping")

    sensors.append(CpuTempSensor())
    log_info("cpu    ready")

    if not sensors:
        raise RuntimeError("no sensors available")
    return sensors


def build_sinks(config, sensors):
    """Construct sinks. InfluxDB is required; MQTT is optional (skipped when
    MQTT_HOST is unset or the broker can't be reached)."""
    sinks: list[Sink] = [InfluxSink(config)]

    if config.mqtt_host:
        try:
            sinks.append(MqttSink(config, sensors))
        except Exception as e:
            log_warn(f"mqtt   {e}, skipping")
    else:
        log_info("mqtt   MQTT_HOST unset, skipping")

    return sinks


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------
def sample_sensor(sensor, sinks):
    try:
        readings = sensor.sample()
    except Exception as e:
        warn_once(sensor.name, f"{sensor.name}   {e}")
        return
    clear_warning(sensor.name)
    for sink in sinks:
        sink.publish(readings)


def run(sensors, sinks, config):
    log_info(f"logging {len(sensors)} sensor(s) to {len(sinks)} sink(s) (Ctrl+C to stop)")
    last_flush = 0.0
    try:
        while True:
            now = time.time()
            for sensor in sensors:
                if now - sensor.last_sample >= sensor.interval:
                    sample_sensor(sensor, sinks)
                    sensor.last_sample = now
            if now - last_flush >= config.flush_interval:
                for sink in sinks:
                    sink.flush()
                last_flush = now
            time.sleep(config.loop_sleep)
    except KeyboardInterrupt:
        pass


def main():
    config = Config.from_env()
    sensors = build_sensors(config)
    sinks = build_sinks(config, sensors)
    try:
        run(sensors, sinks, config)
    finally:
        for sink in sinks:
            sink.close()
        for sensor in sensors:
            sensor.close()
        log_info("stopped")


if __name__ == "__main__":
    main()