import logging   
import json
import os
import base64
from datetime import datetime
import azure.functions as func
from azure.iot.hub import IoTHubRegistryManager
from azure.iot.hub.models import CloudToDeviceMethod
import requests
from azure.cosmos import CosmosClient

# Umgebungsvariablen
IOT_HUB_CONNECTION_STRING = os.getenv("IOT_HUB_CONNECTION_STRING")
COSMOS_DB_URL = os.getenv("COSMOS_DB_URL")
COSMOS_DB_KEY = os.getenv("COSMOS_DB_KEY")
DATABASE_NAME = "IoTDeviceData"
WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL")
WHATSAPP_API_KEY = os.getenv("WHATSAPP_API_KEY")
WHATSAPP_TEST_NUMBER = os.getenv("WHATSAPP_TEST_NUMBER")

# Tool-Fehlercodes
CRITICAL_ERRORS = {
    4: "🛑 Motorausfall erkannt – Gerät wird abgeschaltet.",
    5: "🛑 Akku nicht erkannt – Gerät wird abgeschaltet.",
    6: "🛑 Softwarefehler erkannt – Gerät wird abgeschaltet."
}

NON_CRITICAL_ERRORS = {
    1: "⚠️ Geringfügiger Sensorfehler erkannt.",
    2: "⚠️ Geringe Motoreffizienz erkannt.",
    3: "⚠️ Kurzzeitiges Kommunikationsproblem erkannt."
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
def send_whatsapp_message(to_number: str, message: str):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {
            "body": message
        }
    }
    try:
        response = requests.post(WHATSAPP_API_URL, headers=headers, json=data)
        logging.info(f"WhatsApp sent ({response.status_code}): {response.text}")
    except Exception as e:
        logging.error(f"WhatsApp API error: {e}")

# Einstiegspunkt der Azure Function
def main(documents: func.DocumentList) -> None:
    logging.info("Cosmos DB Trigger function started")

    if not documents:
        logging.info("ℹNo documents received — exiting function.")
        return

    logging.info(f"Received {len(documents)} documents")

    cosmos_client = CosmosClient(COSMOS_DB_URL, COSMOS_DB_KEY)
    database = cosmos_client.get_database_client(DATABASE_NAME)

    try:
        registry_manager = IoTHubRegistryManager(IOT_HUB_CONNECTION_STRING)

        for document in documents:
            try:
                raw_body = document.get("Body")
                if raw_body:
                    decoded = json.loads(base64.b64decode(raw_body).decode("utf-8"))
                    event_data = decoded
                    logging.info(f"Decoded telemetry body for device: {event_data}")
                else:
                    event_data = document
                    logging.warning("No 'Body' field found - using raw document.")
            except Exception as e:
                logging.error(f"Error decoding telemetry body: {e}")
                event_data = {}

            device_id = event_data.get("deviceId")
            device_label = DEVICE_LABELS.get(device_id, device_id)
            tool_events = event_data.get("toolEvents")
            created_at = event_data.get("createdAt")

            battery = event_data.get("BatteryMeasurement")
            temperature = battery.get("temperature")
            state_of_charge = battery.get("stateOfCharge")
            voltage = battery.get("voltage")
            current = battery.get("current")
            charging = battery.get("chargingStatus")
            motor_runtime = event_data.get("motorRuntime")

            log_msg = (
                f"Extracted from {device_id} | "
                f"SOC: {state_of_charge}%, Temp: {temperature}°C, "
                f"Voltage: {voltage}V, Current: {current}A, ToolEvent: {tool_events}"
            )
            logging.info(log_msg)

            # Prüfung auf kritische und nicht-kritische Bedingungen
            warning_alerts = []
            shutdown_alerts = []

            # Temperatur
            if temperature > 60:
                shutdown_alerts.append("🛑 Überhitzung! Temperatur über 60 °C – Gerät wird abgeschaltet.")

            # Spannung
            if voltage < 15.0:
                shutdown_alerts.append("🛑 Kritisch niedrige Spannung (<16.0 V) – Gerät wird abgeschaltet.")

            # Strom
            if current > 50:
                shutdown_alerts.append("🛑 Auffällige Stromaufnahme (>50 A) – Gerät wird abgeschaltet.")

            # ToolEvents
            event_code = tool_events[0]
            if event_code in CRITICAL_ERRORS:
                shutdown_alerts.append(CRITICAL_ERRORS[event_code])
            elif event_code in NON_CRITICAL_ERRORS:
                warning_alerts.append(NON_CRITICAL_ERRORS[event_code])

            # Wartungshinweis bei langer Laufzeit
            if motor_runtime > 7100: # 2 Stunden 
                warning_alerts.append("🔁 Hohe Laufzeit – Wartung empfohlen.")

            # Hinweis bei vollem Akku
            if state_of_charge == 100 and not charging:
                warning_alerts.append("✅ Akku vollständig geladen – bereit für den Einsatz.")

            # WhatsApp Nachrichten senden
            for msg in shutdown_alerts + warning_alerts:
                send_whatsapp_message(WHATSAPP_TEST_NUMBER, f"{device_label} {msg}")

            # Shutdown an Gerät senden
            if shutdown_alerts:
                try:
                    payload = {
                        "message": f"Device shutting down due to: {', '.join(shutdown_alerts)}"
                    }
                    method = CloudToDeviceMethod(
                        method_name="shutdown",
                        payload=payload,
                        response_timeout_in_seconds=30
                    )
                    response = registry_manager.invoke_device_method(device_id, method)
                    logging.info(f"Shutdown command sent to {device_id}: {response}")
                except Exception as e:
                    logging.error(f"Failed to send shutdown command to {device_id}: {e}")

    except Exception as e:
        logging.error(f"Error in main alert handler: {e}")
