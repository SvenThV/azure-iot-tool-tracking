import logging
import json
import os
import base64
import requests
import azure.functions as func
from azure.cosmos import CosmosClient
from azure.iot.hub import IoTHubRegistryManager
from azure.iot.hub.models import CloudToDeviceMethod

# Umgebungsvariablen
COSMOS_DB_URL = os.getenv("COSMOS_DB_URL")
COSMOS_DB_KEY = os.getenv("COSMOS_DB_KEY")
IOT_HUB_CONNECTION_STRING = os.getenv("IOT_HUB_CONNECTION_STRING")
WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL")
WHATSAPP_API_KEY = os.getenv("WHATSAPP_API_KEY")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
DATABASE_NAME = "IoTDeviceData"
STATUS_CONTAINER = "DeviceStatus"

# Tool-Fehlercodes
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

# Gerätebezeichnungen
DEVICE_LABELS = {
    "Tool_1": "Bohrmaschine",
    "Tool_2": "Winkelschleifer",
    "Tool_3": "Bohrhammer",
    "Tool_4": "Stichsäge",
    "Tool_5": "Säbelsäge"
}

# WhatsApp Nachricht senden
def send_whatsapp_message(phone_number, message):
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

# Einstiegspunkt der Azure Function
def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        if req.method == "GET":
            # Verifizierung des Meta-Webhooks
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
            # Verarbeitung eingehender WhatsApp-Nachricht
            data = req.get_json()
            logging.info(f"Incoming WhatsApp webhook: {json.dumps(data)}")

            if "messages" not in data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}):
                return func.HttpResponse("No message found", status_code=200)

            message = data["entry"][0]["changes"][0]["value"]["messages"][0]
            user_message = message["text"]["body"].lower()
            sender_phone = message["from"]

            # Suche nach Gerätename im Nachrichtentext
            found_device = None
            for label in DEVICE_LABELS.values():
                if label.lower() in user_message:
                    found_device = label
                    break

            if not found_device:
                send_whatsapp_message(sender_phone, "⚠️ Bitte gib ein Gerät an, z. B. 'Status Bohrmaschine'")
                return func.HttpResponse("OK", status_code=200)

            # Device-ID aus Label ableiten
            device_id = [key for key, val in DEVICE_LABELS.items() if val == found_device][0]
            device_label = found_device

            # Verbindung zu Cosmos DB herstellen
            client = CosmosClient(COSMOS_DB_URL, COSMOS_DB_KEY)
            database = client.get_database_client(DATABASE_NAME)
            status_container = database.get_container_client(STATUS_CONTAINER)

            # Abfrage des letzten Telemetrie-Datensatzes
            telemetry_query = f"""
            SELECT TOP 1 * FROM c 
            WHERE c.SystemProperties["iothub-connection-device-id"] = '{device_id}' 
            ORDER BY c._ts DESC
            """
            telemetry_items = list(status_container.query_items(query=telemetry_query, enable_cross_partition_query=True))

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

            # Steuerbefehle verarbeiten
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

                    # Umrechnung der Laufzeit in Stunden und Minuten
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
