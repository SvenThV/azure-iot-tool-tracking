# Cloud-based IoT Tool Tracking with Azure

This project is based on my bachelor's thesis in Business Informatics.  
The goal was to develop a cloud-based prototype for monitoring battery-powered tools using Microsoft Azure.

## Overview

The system simulates multiple IoT devices that send telemetry data such as temperature, battery level, current, voltage, runtime and location to Azure IoT Hub. The data is stored in Azure Cosmos DB and processed by Azure Functions. Critical values trigger automated warnings and shutdown commands. Users can interact with the system through a WhatsApp chatbot.

## Architecture

Main components:

- Python device simulator
- Azure IoT Hub
- Azure Cosmos DB
- Azure Functions
- Meta WhatsApp API
- Google Maps link generation for device location

## Features

- Simulation of five battery-powered tools
- Telemetry transmission via MQTT
- Storage of telemetry data in Cosmos DB
- Event-driven processing with Azure Functions
- Automatic detection of critical device states
- Cloud-to-device commands via IoT Hub Direct Methods
- WhatsApp-based status and location queries
- Reset command for reactivating paused devices

## Repository Structure

- `simulator/` contains the Python-based IoT device simulation
- `azure-functions/process_device_data/` contains the event-driven processing logic
- `azure-functions/get_device_status/` contains the WhatsApp chatbot interaction logic
- `architecture/` contains the system architecture diagram
- `docs/` contains additional notes and explanations

## Notes

This project was developed as a prototype. It does not represent a production-ready IoT platform. Security hardening, dashboard visualization, real hardware integration and scalability testing were outside the project scope.

## Lessons Learned

- Azure IoT Hub is well suited for bidirectional device communication.
- Cosmos DB works well for flexible JSON-based telemetry data.
- Azure Functions are useful for event-driven processing.
- WhatsApp can provide a low-threshold user interface for IoT status queries.
- Critical shutdown logic should also be implemented locally on real devices, not only in the cloud.
