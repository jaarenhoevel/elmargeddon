#!/usr/bin/env python3
"""Elmargeddon — Raspberry Pi weather station logger.

Samples wind (Modbus RTU), BME280 (I2C) and CPU temperature on independent
intervals and writes the results to InfluxDB. Points that cannot be sent are
buffered to a local file and retried.

Each sensor is a small class implementing `sample() -> list[Point]`; the main
loop just schedules them by interval and hands their points to an `InfluxSink`
that handles writing and on-disk buffering. Add or swap a sensor by editing
the relevant class and one entry in `build_sensors`.
"""

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import smbus2
import bme280
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

    @classmethod
    def from_env(cls):
        return cls(
            influx_url=require_env("INFLUX_URL"),
            influx_token=require_env("INFLUX_TOKEN"),
            influx_org=require_env("INFLUX_ORG"),
            influx_bucket=require_env("INFLUX_BUCKET"),
            modbus_device=require_env("MODBUS_DEVICE"),
            buffer_file=optional_env("BUFFER_FILE", "influx_buffer.jsonl"),
        )


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
# InfluxSink — write points now, buffer on failure, retry the buffer.
# Unlike the original, points are buffered even when InfluxDB was never
# reached, so transient outages no longer drop data.
# --------------------------------------------------------------------------
class InfluxSink:
    def __init__(self, config):
        self.bucket = config.influx_bucket
        self.org = config.influx_org
        self.buffer = Buffer(config.buffer_file)
        self.client = InfluxDBClient(
            url=config.influx_url, token=config.influx_token, org=config.influx_org
        )
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        log_info("InfluxDB client ready")

    def write(self, point):
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


# --------------------------------------------------------------------------
# Sensors
# --------------------------------------------------------------------------
class Sensor(ABC):
    """A device sampled on a fixed interval. `sample()` returns InfluxDB Points."""

    name = "sensor"

    def __init__(self, interval):
        self.interval = interval
        self.last_sample = 0.0

    @abstractmethod
    def sample(self) -> list[Point]:
        ...

    def close(self):
        pass


class WindSensor(Sensor):
    """Anemometer over Modbus RTU. Register 0 = speed (/100 → m/s), 1 = direction."""

    name = "wind"

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
        return [Point("wind_sensor")
                .field("speed", speed)
                .field("direction", direction)
                .time(datetime.utcnow())]

    def close(self):
        self.client.close()


class Bme280Sensor(Sensor):
    """BME280 temperature/pressure/humidity over I2C."""

    name = "bme280"

    def __init__(self, bus, address, calibration, interval=30.0):
        super().__init__(interval)
        self.bus = bus
        self.address = address
        self.calibration = calibration

    def sample(self):
        d = bme280.sample(self.bus, self.address, self.calibration)
        log_info(f"bme280  {d.temperature:5.2f} °C  {d.pressure:7.2f} hPa  {d.humidity:5.2f} %")
        return [Point("temperature_sensor")
                .field("temperature", d.temperature)
                .field("pressure", d.pressure)
                .field("humidity", d.humidity)
                .time(datetime.utcnow())]

    def close(self):
        self.bus.close()


class CpuTempSensor(Sensor):
    """On-board CPU temperature via gpiozero."""

    name = "cpu"

    def __init__(self, interval=30.0):
        super().__init__(interval)
        self._probe = CPUTemperature()

    def sample(self):
        temp = self._probe.temperature
        log_info(f"cpu    {temp:5.2f} °C")
        return [Point("device_metrics")
                .field("cpu_temperature", temp)
                .time(datetime.utcnow())]


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


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------
def sample_sensor(sensor, sink):
    try:
        points = sensor.sample()
    except Exception as e:
        warn_once(sensor.name, f"{sensor.name}   {e}")
        return
    clear_warning(sensor.name)
    for point in points:
        sink.write(point)


def run(sensors, sink, config):
    log_info(f"logging {len(sensors)} sensor(s) (Ctrl+C to stop)")
    last_flush = 0.0
    try:
        while True:
            now = time.time()
            for sensor in sensors:
                if now - sensor.last_sample >= sensor.interval:
                    sample_sensor(sensor, sink)
                    sensor.last_sample = now
            if now - last_flush >= config.flush_interval:
                sink.flush()
                last_flush = now
            time.sleep(config.loop_sleep)
    except KeyboardInterrupt:
        pass


def main():
    config = Config.from_env()
    sink = InfluxSink(config)
    sensors = build_sensors(config)
    try:
        run(sensors, sink, config)
    finally:
        for sensor in sensors:
            sensor.close()
        sink.close()
        log_info("stopped")


if __name__ == "__main__":
    main()