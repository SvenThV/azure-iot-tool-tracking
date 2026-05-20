import logging
import json
import os
import base64
import requests
import azure.functions as func
from azure.cosmos import CosmosClient
from azure.iot.hub import IoTHubRegistryManager
from azure.iot.hub.models import CloudToDeviceMethod

# Environment variables
COSMOS_DB_URL = os.getenv("COSMOS_DB_URL")
COSMOS_DB_KEY = os.getenv("COSMOS_DB_KEY")
IOT_HUB_CONNECTION_STRING = os.getenv("IOT_HUB_CONNECTION_STRING")
WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL")
WHATSAPP_API_KEY = os.getenv("WHATSAPP_API_KEY")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
DATABASE_NAME = "IoTDeviceData"
STATUS_CONTAINER = "DeviceStatus"

REQUIRED_ENV_VARS = [
    "COSMOS_DB_URL",
    "COSMOS_DB_KEY",
    "IOT_HUB_CONNECTION_STRING",
    "WHATSAPP_API_URL",
    "WHATSAPP_API_KEY",
    "VERIFY_TOKEN",
]


def validate_environment():
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        logging.error("Missing required environment variables: %s", ", ".join(missing))
        return False
    return True

# Tool error codes
CRITICAL_ERRORS = {
    4: "🛑 Motorausfall",
    5: "🛑 Akku nicht erkannt",
    6: "🛑 Softwarefehler"
}

NON_CRITICAL_ERRORS = {
    1: "⚠️ Geringfügiger Sensorfehler",
    2: "⚠️ Geringe Motoreffizienz",
    3: "⚠️ Kurzzeitiges Kommunikationsproblem"
}

# Device labels
DEVICE_LABELS = {
    "Tool_1": "Bohrmaschine",
    "Tool_2": "Winkelschleifer",
    "Tool_3": "Bohrhammer",
    "Tool_4": "Stichsäge",
    "Tool_5": "Säbelsäge"
}

# Send WhatsApp message
def send_whatsapp_message(phone_number, message):
    if not WHATSAPP_API_URL or not WHATSAPP_API_KEY:
        logging.error("WhatsApp API configuration is missing")
        return

    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "text",
        "text": { "body": message }
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {WHATSAPP_API_KEY}"
    }

    response = requests.post(WHATSAPP_API_URL, headers=headers, json=payload)
    logging.info(f"Sent message to {phone_number}: {response.status_code} - {response.text}")
    if response.status_code >= 400:
        logging.error("WhatsApp API returned error: %s", response.text)

# Azure Function entry point
def main(req: func.HttpRequest) -> func.HttpResponse:
    if not validate_environment():
        return func.HttpResponse("Environment misconfigured", status_code=500)

    try:
        if req.method == "GET":
            # Verify webhook subscription
            mode = req.params.get("hub.mode")
            token = req.params.get("hub.verify_token")
            challenge = req.params.get("hub.challenge")

            if mode == "subscribe" and token == VERIFY_TOKEN:
                logging.info("Webhook verification successful")
                return func.HttpResponse(challenge, status_code=200)
            else:
                logging.warning("Invalid verification token")
                return func.HttpResponse("Unauthorized", status_code=403)

        elif req.method == "POST":
            # Process incoming WhatsApp message
            data = req.get_json()
            logging.info(f"Incoming WhatsApp webhook: {json.dumps(data)}")

            if "messages" not in data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}):
                return func.HttpResponse("No message found", status_code=200)

            message = data["entry"][0]["changes"][0]["value"]["messages"][0]
            user_message = message["text"]["body"].lower()
            sender_phone = message["from"]

            # Search for device name in user message
            found_device = None
            for label in DEVICE_LABELS.values():
                if label.lower() in user_message:
                    found_device = label
                    break

            if not found_device:
                send_whatsapp_message(sender_phone, "⚠️ Bitte gib ein Gerät an, z. B. 'Status Bohrmaschine'")
                return func.HttpResponse("OK", status_code=200)

            # Derive device ID from label
            device_id = next((key for key, val in DEVICE_LABELS.items() if val == found_device), None)
            device_label = found_device

            if not device_id:
                logging.warning("Device label not found: %s", found_device)
                send_whatsapp_message(sender_phone, f"⚠️ Gerät {device_label} konnte nicht gefunden werden.")
                return func.HttpResponse("OK", status_code=200)

            # Connect to Cosmos DB
            client = CosmosClient(COSMOS_DB_URL, COSMOS_DB_KEY)
            database = client.get_database_client(DATABASE_NAME)
            status_container = database.get_container_client(STATUS_CONTAINER)

            # Query the latest telemetry record
            telemetry_query = (
                "SELECT TOP 1 * FROM c "
                "WHERE c.SystemProperties[\"iothub-connection-device-id\"] = @deviceId "
                "ORDER BY c._ts DESC"
            )
            telemetry_items = list(
                status_container.query_items(
                    query=telemetry_query,
                    parameters=[{"name": "@deviceId", "value": device_id}],
                    enable_cross_partition_query=True,
                )
            )

            telemetry_data = None
            latitude = None
            longitude = None

            if telemetry_items and "Body" in telemetry_items[0]:
                try:
                    body = telemetry_items[0]["Body"]
                    decoded = base64.b64decode(body).decode("utf-8")
                    telemetry_data = json.loads(decoded)
                    coordinates = telemetry_data.get("location", {}).get("coordinates", [])
                    if len(coordinates) >= 2:
                        latitude, longitude = coordinates[0], coordinates[1]
                except Exception as e:
                    logging.warning(f"Failed to decode telemetry: {e}")

            # Handle control commands
            if "reset" in user_message:
                try:
                    registry = IoTHubRegistryManager(IOT_HUB_CONNECTION_STRING)
                    method = CloudToDeviceMethod(
                        method_name="reset",
                        payload={"message": "Reset command triggered via WhatsApp"},
                        response_timeout_in_seconds=30
                    )
                    response = registry.invoke_device_method(device_id, method)
                    logging.info(f"Reset command sent to {device_id}: {response}")
                    send_whatsapp_message(sender_phone, f"Reset command sent to {device_label}.")
                except Exception as e:
                    logging.error(f"Failed to send reset command to {device_id}: {e}")
                    send_whatsapp_message(sender_phone, f"Failed to send reset command to {device_label}.")

            elif "position" in user_message:
                if latitude and longitude:
                    send_whatsapp_message(sender_phone, f"📍 Position von {device_label}:\nhttps://www.google.com/maps?q={latitude},{longitude}")
                else:
                    send_whatsapp_message(sender_phone, f"⚠️ Keine Positionsdaten für {device_label} gefunden.")

            elif "status" in user_message:
                if telemetry_data:
                    created_at = telemetry_data.get("createdAt", "")[:19].replace("T", " ")
                    motor_runtime = telemetry_data.get("motorRuntime", 0)

                    # Convert runtime to hours and minutes
                    hours = motor_runtime // 3600
                    minutes = (motor_runtime % 3600) // 60

                    battery = telemetry_data.get("BatteryMeasurement", {})
                    tool_event_code = telemetry_data.get("toolEvents", [0])[0]

                    status_text = "Kein Fehler"
                    if tool_event_code in CRITICAL_ERRORS:
                        status_text = CRITICAL_ERRORS[tool_event_code]
                    elif tool_event_code in NON_CRITICAL_ERRORS:
                        status_text = NON_CRITICAL_ERRORS[tool_event_code]

                    charging = "Ja" if battery.get("chargingStatus") else "Nein"
                    soc = battery.get("stateOfCharge", "–")
                    temp = battery.get("temperature", "–")
                    voltage = battery.get("voltage", "–")
                    current = battery.get("current", "–")
                    ttc = battery.get("timeToCharge", "–")

                    message_text = (
                        f"📊 *Status von {device_label}*\n\n"
                        f"🟦 *Allgemein*\n"
                        f"🕒 Letztes Update: {created_at}\n"
                        f"⏱ Gesamtlaufzeit: {hours} Std. {minutes} Min.\n"
                        f"💡 Systemmeldung: {status_text}\n\n"
                        f"🟩 *Batterie*\n"
                        f"🔌 Lädt: {charging}\n"
                        f"🪫 Ladestand: {soc} %\n"
                        f"🌡 Temperatur: {temp} °C\n"
                        f"⚡ Spannung: {voltage} V\n"
                        f"🔋 Strom: {current} A\n"
                        f"⌛ Zeit bis voll: {ttc} Sek."
                    )
                    send_whatsapp_message(sender_phone, message_text)
                else:
                    send_whatsapp_message(sender_phone, f"⚠️ Keine Telemetriedaten für {device_label} gefunden.")

            return func.HttpResponse("OK", status_code=200)

        else:
            return func.HttpResponse("Method Not Allowed", status_code=405)

    except Exception as e:
        logging.error(f"Error in WhatsApp Webhook: {e}")
        return func.HttpResponse("Fehler beim Verarbeiten", status_code=500)
