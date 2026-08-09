# Elmageddon

## Preparation
Set up virtual environment and install dependencies
```console
python -m venv env
source env/bin/activate
pip install rpi.bme280 pymodbus influxdb-client pyserial gpiozero smbus2 paho-mqtt
```
Set up the environment variables:
```console
export INFLUX_TOKEN=[TOKEN]
export INFLUX_ORG=[ORG]
export INFLUX_URL=[URL]
export INFLUX_BUCKET=[BUCKET]

export MODBUS_DEVICE=[DEVICE]

# Optional — enables MQTT publishing and Home Assistant auto-discovery
export MQTT_HOST=[HOST]
export MQTT_PORT=1883
export MQTT_USER=[USER]         # optional
export MQTT_PASSWORD=[PASSWORD] # optional
```

## Run
```console
source env/bin/activate
python main.py
```
