import random
import time
import json
import os
import threading
from datetime import datetime
import pytz
from azure.iot.device import IoTHubDeviceClient, MethodResponse

# Configuration values for simulator runtime and telemetry behavior
RUNTIME_FILE = 'motor_runtime.json'
RESET_THRESHOLD_SECONDS = 7200
TELEMETRY_INTERVAL_SECONDS = 10
MAIN_LOOP_SLEEP_SECONDS = 60

IOT_HUB_ENV_VARS = [
    'IOT_HUB_CONNECTION_STRING_TOOL_1',
    'IOT_HUB_CONNECTION_STRING_TOOL_2',
    'IOT_HUB_CONNECTION_STRING_TOOL_3',
    'IOT_HUB_CONNECTION_STRING_TOOL_4',
    'IOT_HUB_CONNECTION_STRING_TOOL_5',
]

# Load saved runtimes from disk when the simulator starts
def load_motor_runtimes():
    if os.path.exists(RUNTIME_FILE):
        with open(RUNTIME_FILE, 'r') as f:
            return json.load(f)
    return {}

# Persist runtimes to disk after each telemetry update
def save_motor_runtimes(runtimes):
    with open(RUNTIME_FILE, 'w') as f:
        json.dump(runtimes, f)

# Ensure all required connection strings are provided through environment variables
def validate_connection_strings(connection_strings):
    missing = [name for name, value in zip(IOT_HUB_ENV_VARS, connection_strings) if not value]
    if missing:
        print('Missing required environment variables for IoT Hub connection strings.')
        for name in missing:
            print(f'  - {name}')
        print('Create a .env file or set the missing values before running the simulator.')
        raise SystemExit(1)

DEVICE_CONNECTION_STRINGS = [os.getenv(name) for name in IOT_HUB_ENV_VARS]
pause_flags = [False] * len(DEVICE_CONNECTION_STRINGS)

# Simulates a single IoT tool device and sends telemetry to Azure IoT Hub.
# The device also handles direct methods for shutdown and reset.
class SimulatedDevice:
    def __init__(self, device_id, product_number, battery_product_number, connection_string, index, motor_runtimes):
        self.device_id = device_id
        self.product_number = product_number
        self.battery_product_number = battery_product_number
        self.connection_string = connection_string
        self.index = index
        self.client = IoTHubDeviceClient.create_from_connection_string(connection_string)
        self.client.on_method_request_received = self.handle_method_request
        self.motor_runtimes = motor_runtimes
        self.motor_runtime = self.motor_runtimes.get(self.device_id, 0)
        self.state_of_charge = random.randint(80, 100)
        self.is_charging = False
        self.temperature = random.randint(20, 35)
        self.voltage = random.uniform(18.5, 20.5)
        self.current = 0
        self.location = {
            'coordinates': [random.uniform(48.0, 50.0), random.uniform(8.0, 10.0)],
            'updatedAt': datetime.now(pytz.timezone('Europe/Berlin')).isoformat()
        }

    def handle_method_request(self, method_request):
        if method_request.name == 'shutdown':
            print(f'{self.device_id} received shutdown command.')
            pause_flags[self.index] = True
            payload = {'result': 'Device paused'}
            status = 200
        elif method_request.name == 'reset':
            print(f'{self.device_id} received reset command.')
            pause_flags[self.index] = False
            payload = {'result': 'Device resumed'}
            status = 200
        else:
            payload = {'error': 'Unknown method'}
            status = 400

        response = MethodResponse.create_from_method_request(method_request, status, payload)
        self.client.send_method_response(response)

    # Update simulated telemetry values for this device.
    def update_metrics(self):
        if not self.is_charging:
            self.motor_runtime += 10
            self.state_of_charge = max(0, self.state_of_charge - random.uniform(0.1, 0.5))
            self.current = round(random.uniform(10.0, 30.0), 2)
            self.voltage = round(random.uniform(15.5, 20.5), 2)
            self.temperature = random.randint(20, 50)
        else:
            self.state_of_charge = min(100, self.state_of_charge + random.uniform(1, 3))
            self.current = -round(random.uniform(5.0, 15.0), 2)
            self.voltage = round(random.uniform(18.5, 20.5), 2)
            self.temperature = random.randint(20, 35)

        # Toggle charge state when the battery is empty or full.
        if self.state_of_charge <= 0:
            self.is_charging = True
        elif self.state_of_charge >= 100:
            self.is_charging = False

        error_chance = 0.05
        if random.random() < error_chance:
            error_type = random.choice(['current', 'voltage', 'temperature'])
            if error_type == 'current':
                self.current = round(random.uniform(51.0, 70.0), 2)
            elif error_type == 'voltage':
                self.voltage = round(random.uniform(13.0, 14.9), 2)
            elif error_type == 'temperature':
                self.temperature = random.randint(61, 80)

        self.location['coordinates'] = [
            self.location['coordinates'][0] + random.uniform(-0.0005, 0.0005),
            self.location['coordinates'][1] + random.uniform(-0.0005, 0.0005)
        ]
        self.location['updatedAt'] = datetime.now(pytz.timezone('Europe/Berlin')).isoformat()

        # Reset runtime when the threshold is exceeded.
        if self.motor_runtime >= RESET_THRESHOLD_SECONDS:
            print(f'{self.device_id}: Runtime exceeds {RESET_THRESHOLD_SECONDS}s → resetting.')
            self.motor_runtime = 0

        self.motor_runtimes[self.device_id] = self.motor_runtime
        save_motor_runtimes(self.motor_runtimes)

        event_code = 0
        if random.random() < 0.02:
            event_code = random.randint(1, 6)

        return {
            'deviceId': self.device_id,
            'productNumber': self.product_number,
            'motorRuntime': self.motor_runtime,
            'createdAt': datetime.now(pytz.timezone('Europe/Berlin')).isoformat(),
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
                'createdAt': datetime.now(pytz.timezone('Europe/Berlin')).isoformat()
            }
        }

    # Main loop sends telemetry at a fixed interval and respects shutdown/reset state.
    def run(self):
        self.client.connect()
        while True:
            if not pause_flags[self.index]:
                message = self.update_metrics()
                try:
                    self.client.send_message(json.dumps(message))
                    print(f'✅ {self.device_id} sent data')
                except Exception as e:
                    print(f'{self.device_id} failed to send: {e}')
            time.sleep(TELEMETRY_INTERVAL_SECONDS)

if __name__ == '__main__':
    motor_runtimes = load_motor_runtimes()
    validate_connection_strings(DEVICE_CONNECTION_STRINGS)

    # Start device threads
    for i in range(len(DEVICE_CONNECTION_STRINGS)):
        device = SimulatedDevice(
            device_id=f'Tool_{i+1}',
            product_number=1000000000 + i + 1,
            battery_product_number=2000000000 + i + 1,
            connection_string=DEVICE_CONNECTION_STRINGS[i],
            index=i,
            motor_runtimes=motor_runtimes
        )
        threading.Thread(target=device.run, daemon=True).start()

    while True:
        time.sleep(MAIN_LOOP_SLEEP_SECONDS)
