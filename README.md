# Elmargeddon

## Preparation
Set up virtual environment and install dependencies
```console
python -m venv env
source env/bin/activate
pip install rpi.bme280 pymodbus influxdb-client pyserial
```
Set up the environment variables:
```console
export INFLUX_TOKEN=[TOKEN]
export INFLUX_ORG=[ORG]
export INFLUX_URL=[URL]
export INFLUX_BUCKET=[BUCKET]

export MODBUS_DEVICE=[DEVICE]
```

## Run
```console
source env/bin/activate
python main.py
```
