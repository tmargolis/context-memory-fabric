# Project Aether: Autonomous Environmental Sensor Mesh

Project Aether is a distributed, low-power environmental monitoring mesh designed for microclimate tracking, air quality telemetry, and local meteorological analysis.

## System Architecture

The system consists of three distinct layers:
1. **Edge Sensing Nodes:** Solar-assisted sensor units running FreeRTOS on ESP32-S3 microcontrollers.
2. **Gateway Concentrator:** Raspberry Pi 5 with a SX1302 LoRaWAN gateway concentrator HAT located at the central site.
3. **Telemetry Ingestion & Storage:** MQTT broker (Mosquitto) ingesting into TimescaleDB with a Grafana dashboard for real-time visualization.

## Hardware Specifications

### Node Hardware
- **MCU:** ESP32-S3-WROOM-1 (dual-core 240MHz, 8MB Flash, 2MB PSRAM)
- **Radio:** Semtech SX1262 LoRa transceiver operating at 915 MHz (US915 channel plan)
- **Primary Sensors:**
  - Bosch BME680 (temperature, barometric pressure, relative humidity, VOC gas resistance)
  - Sensirion SPS30 (optical particulate matter: PM1.0, PM2.5, PM4.0, PM10)
  - Vishay VEML7700 (high-accuracy ambient light lux sensor)
- **Power Management:**
  - 3.2V 3200mAh LiFePO4 battery pack (chosen for thermal stability and high cycle life)
  - Waveshare 5V/6V Solar Power Management Module with MPPT charging
  - 2W monocrystalline solar panel with 15-degree elevation mount

## Firmware & Power Budget

- **Sleep Profile:** Deep sleep cycle consuming 18 uA in quiescent state.
- **Transmit Interval:** Normal cadence is 1 reading every 5 minutes. If PM2.5 delta exceeds 15 ug/m³ within 60 seconds, transmit triggers immediately in alert mode.
- **Estimated Autonomy:** 28 days without sunlight based on a 42-day worst-case winter cycle.

## Integration & Telemetry Pipeline

- Telemetry payloads are serialized using Protocol Buffers to minimize radio airtime (time-on-air < 45 ms per packet).
- Gateway forwards packets to the local MQTT broker at `mqtt.lan.internal:1883` on topic `aether/telemetry/v1/{node_id}`.
- An ingestion daemon validates HMAC signatures and writes timeseries records directly to TimescaleDB.

## Roadmap & Status

- [x] Prototype node built and tested on breadboard (Q2 2026).
- [x] Gateway concentrator provisioned with failover cellular LTE backhaul.
- [ ] Deploy 5 field enclosures across target test perimeter.
- [ ] Evaluate LoRa mesh routing (Meshtastic integration) for nodes out of line-of-sight from the gateway.
