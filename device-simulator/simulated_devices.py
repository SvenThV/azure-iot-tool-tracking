import json
import logging
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path

import pytz
from azure.iot.device import IoTHubDeviceClient, MethodResponse

# Configuration values for simulator runtime and telemetry behavior
RUNTIME_FILE = Path('motor_runtime.json')
RESET_THRESHOLD_SECONDS = 7200
TELEMETRY_INTERVAL_SECONDS = 10
MAIN_LOOP_SLEEP_SECONDS = 60
IOT_HUB_ENV_VARS = [f'IOT_HUB_CONNECTION_STRING_TOOL_{i}' for i in range(1, 6)]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger(__name__)


def load_motor_runtimes() -> dict:
    if RUNTIME_FILE.exists():
        try:
            return json.loads(RUNTIME_FILE.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning('Unable to read runtime file %s: %s', RUNTIME_FILE, exc)
    return {}


def save_motor_runtimes(runtimes: dict) -> None:
    try:
        RUNTIME_FILE.write_text(json.dumps(runtimes, indent=2))
    except OSError as exc:
        logger.error('Unable to save runtime file %s: %s', RUNTIME_FILE, exc)


def validate_connection_strings(connection_strings: list[str]) -> None:
    missing = [name for name, value in zip(IOT_HUB_ENV_VARS, connection_strings) if not value]
    if missing:
        logger.error('Missing required environment variables for IoT Hub connection strings: %s', missing)
        logger.error('Create a .env file or set the missing values before running the simulator.')
        raise SystemExit(1)


def get_device_connection_strings() -> list[str]:
    return [os.getenv(name, '') for name in IOT_HUB_ENV_VARS]


class SimulatedDevice:
    def __init__(
        self,
        device_id: str,
        product_number: int,
        battery_product_number: int,
        connection_string: str,
        motor_runtimes: dict,
    ) -> None:
        self.device_id = device_id
        self.product_number = product_number
        self.battery_product_number = battery_product_number
        self.connection_string = connection_string
        self.client = IoTHubDeviceClient.create_from_connection_string(connection_string)
        self.client.on_method_request_received = self.handle_method_request
        self.motor_runtimes = motor_runtimes
        self.motor_runtime = self.motor_runtimes.get(self.device_id, 0)
        self.state_of_charge = random.randint(80, 100)
        self.is_charging = False
        self.temperature = random.randint(20, 35)
        self.voltage = random.uniform(18.5, 20.5)
        self.current = 0.0
        self.location = {
            'coordinates': [random.uniform(48.0, 50.0), random.uniform(8.0, 10.0)],
            'updatedAt': datetime.now(pytz.timezone('Europe/Berlin')).isoformat(),
        }
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()

    def handle_method_request(self, method_request):
        if method_request.name == 'shutdown':
            logger.info('%s received shutdown command.', self.device_id)
            self.pause_event.set()
            payload = {'result': 'Device paused'}
            status = 200
        elif method_request.name == 'reset':
            logger.info('%s received reset command.', self.device_id)
            self.pause_event.clear()
            payload = {'result': 'Device resumed'}
            status = 200
        else:
            payload = {'error': 'Unknown method'}
            status = 400

        response = MethodResponse.create_from_method_request(method_request, status, payload)
        self.client.send_method_response(response)

    def update_metrics(self) -> dict:
        if not self.is_charging:
            self.motor_runtime += 10
            self.state_of_charge = max(0.0, self.state_of_charge - random.uniform(0.1, 0.5))
            self.current = round(random.uniform(10.0, 30.0), 2)
            self.voltage = round(random.uniform(15.5, 20.5), 2)
            self.temperature = random.randint(20, 50)
        else:
            self.state_of_charge = min(100.0, self.state_of_charge + random.uniform(1.0, 3.0))
            self.current = -round(random.uniform(5.0, 15.0), 2)
            self.voltage = round(random.uniform(18.5, 20.5), 2)
            self.temperature = random.randint(20, 35)

        if self.state_of_charge <= 0:
            self.is_charging = True
        elif self.state_of_charge >= 100:
            self.is_charging = False

        if random.random() < 0.05:
            error_type = random.choice(['current', 'voltage', 'temperature'])
            if error_type == 'current':
                self.current = round(random.uniform(51.0, 70.0), 2)
            elif error_type == 'voltage':
                self.voltage = round(random.uniform(13.0, 14.9), 2)
            else:
                self.temperature = random.randint(61, 80)

        self.location['coordinates'] = [
            self.location['coordinates'][0] + random.uniform(-0.0005, 0.0005),
            self.location['coordinates'][1] + random.uniform(-0.0005, 0.0005),
        ]
        self.location['updatedAt'] = datetime.now(pytz.timezone('Europe/Berlin')).isoformat()

        if self.motor_runtime >= RESET_THRESHOLD_SECONDS:
            logger.info('%s: Runtime exceeded %ss, resetting.', self.device_id, RESET_THRESHOLD_SECONDS)
            self.motor_runtime = 0

        self.motor_runtimes[self.device_id] = self.motor_runtime
        save_motor_runtimes(self.motor_runtimes)

        event_code = 0
        if random.random() < 0.02:
            event_code = random.randint(1, 6)

        now_iso = datetime.now(pytz.timezone('Europe/Berlin')).isoformat()
        return {
            'deviceId': self.device_id,
            'productNumber': self.product_number,
            'motorRuntime': self.motor_runtime,
            'createdAt': now_iso,
            'toolEvents': [event_code],
            'location': self.location,
            'BatteryMeasurement': {
                'batteryProductNumber': self.battery_product_number,
                'chargingStatus': self.is_charging,
                'stateOfCharge': int(round(self.state_of_charge)),
                'temperature': self.temperature,
                'voltage': self.voltage,
                'current': self.current,
                'timeToCharge': random.randint(300, 1800) if self.is_charging else 0,
                'createdAt': now_iso,
            },
        }

    def run(self, stop_event: threading.Event) -> None:
        self.client.connect()
        logger.info('Device %s connected.', self.device_id)

        while not stop_event.is_set():
            if not self.pause_event.is_set():
                payload = self.update_metrics()
                try:
                    self.client.send_message(json.dumps(payload))
                    logger.info('Sent telemetry for %s.', self.device_id)
                except Exception as exc:
                    logger.error('Failed to send telemetry for %s: %s', self.device_id, exc)
            time.sleep(TELEMETRY_INTERVAL_SECONDS)

        self.client.shutdown()
        logger.info('Device %s disconnected.', self.device_id)


def main() -> None:
    connection_strings = get_device_connection_strings()
    validate_connection_strings(connection_strings)
    motor_runtimes = load_motor_runtimes()
    stop_event = threading.Event()
    threads: list[threading.Thread] = []

    for index, connection_string in enumerate(connection_strings):
        device = SimulatedDevice(
            device_id=f'Tool_{index + 1}',
            product_number=1_000_000_000 + index + 1,
            battery_product_number=2_000_000_000 + index + 1,
            connection_string=connection_string,
            motor_runtimes=motor_runtimes,
        )
        thread = threading.Thread(target=device.run, args=(stop_event,), daemon=True)
        thread.start()
        threads.append(thread)

    try:
        while not stop_event.is_set():
            time.sleep(MAIN_LOOP_SLEEP_SECONDS)
    except KeyboardInterrupt:
        logger.info('Shutdown requested, stopping simulator...')
        stop_event.set()

    for thread in threads:
        thread.join(timeout=2)


if __name__ == '__main__':
    main()
