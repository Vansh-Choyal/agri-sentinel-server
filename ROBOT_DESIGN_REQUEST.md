# AgriSentinel — reusable robot design request

Use the brief below when commissioning the physical robot, asking an engineering assistant for a design, or preparing a CAD/firmware request. It describes the hardware companion to the working monitoring system in this repository. Proposed dimensions, runtime, cost and field capability must be identified as design targets until tested.

---

## Request

Design **AgriSentinel**, a practical, serviceable agricultural inspection rover for a Bal Vigyaan demonstration, with a credible path to field trials. Deliver a complete mechanical, electrical and firmware proposal that integrates with the existing Flask monitoring backend. Prefer parts that can be sourced in India, but verify availability and pricing before making purchase recommendations.

The rover monitors assigned agricultural partitions and reports actual environmental observations. It does not diagnose crop diseases, predict yield, or calculate crop-health scores. The existing dashboard is a schematic of partitions; it has no GPS position feed. Do not add decorative tracking hardware or pretend a compass alone provides localization.

### Physical design

- Develop a compact, stable wheeled chassis with a low center of gravity, accessible electronics compartment, protected wiring, serviceable power pack, and labeled connectors.
- Compare two-wheel differential drive with a caster against a four-wheel layout. Select one using terrain, traction, turning radius, mass, motor stall current and fabrication requirements. Do not assume a particular drivetrain is adequate for mud or slopes.
- State proposed dimensions, wheel diameter, clearance, mass budget, motor torque calculation, target speed and operating surface. Separate a supervised classroom prototype from an outdoor-rated design.
- Plan sensor mounting so temperature/humidity readings are shielded from motor heat and direct solar heating, the ultrasonic cone is unobstructed, the compass is separated from motors and power wiring, and any soil probe can make repeatable contact without damaging plants.
- Include a physically reachable emergency stop, motor power isolation, strain relief and a protected battery enclosure. An online dashboard stop button is a queued command, not an emergency stop.
- Provide front, side, top, exploded and cable-routing drawings. Include editable CAD sources, dimensioned fabrication drawings, fastener specifications and an assembly sequence.
- Use a restrained technical identity: dark graphite structure, olive identification panels, warm legible labels, visible rover ID, zone label and status indicator. Avoid a toy-like or science-fiction appearance.

### Electronics and measurements

Choose an exact ESP32 board and documented sensors. For each component, provide its manufacturer data sheet, voltage domain, interface, pin allocation, resolution, warm-up behavior, expected uncertainty and calibration procedure. Do not invent accuracy claims. Verify pin compatibility for the selected ESP32 variant, including ADC/Wi-Fi constraints, boot strapping pins and input-only pins.

| Required capability | Backend field | Implementation requirement |
| --- | --- | --- |
| Air temperature | `temperature_c` | Real measurement in Celsius; null if unavailable |
| Relative humidity | `humidity_pct` | Real measurement on a 0–100 scale |
| Soil moisture | `soil_moisture_pct` | Documented dry/wet calibration in the intended substrate; null until valid calibration exists; do not call it volumetric water content without a validated conversion |
| MQ2 sensor | `mq2_raw` | Raw ADC integer 0–4095, with documented scaling if the chosen ADC has a different resolution |
| MQ3 sensor | `mq3_raw` | Raw ADC integer 0–4095; no uncalibrated ppm claims |
| Compass | `heading_deg` | Calibrated heading in [0, 360); document reference and magnetic interference |
| Tilt | `tilt_deg` | Defined signed tilt axis in degrees; rule engine uses its absolute value |
| Sound module | `sound_level_raw` | Raw ADC integer, not decibels without calibration |
| Obstacle sensing | `obstacle_distance_cm` | Measured centimeters, null for invalid/no echo rather than invented distance |
| Battery | `robot.battery_pct` | Null unless voltage/current measurement and a validated estimate are implemented |

Provide a complete power tree: battery chemistry and protection, charging arrangement, fuse selection, motor driver sizing against measured stall current, regulator capacity, current and runtime budget, grounding, motor-noise suppression and signal-level conversion. Verify every sensor output against the selected ESP32's permitted input range. Include an electrical schematic, connector table and a pin-conflict review before assembly.

Useful primary references: [ESP32 data sheet](https://documentation.espressif.com/esp32_datasheet_en.html), [Espressif ADC documentation](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/adc/index.html), and the exact chosen board/sensor manufacturer documentation. The ADC voltage calibration requirement is described in Espressif's ADC documentation; raw ADC readings must not be treated as universally calibrated physical measurements.

### Navigation and inspections

Design a clearly bounded demonstration navigation method, such as line following or verified physical checkpoint markers. Explain how the rover recognizes a zone boundary and a completed checkpoint. If reliable localization is not implemented, retain supervised operation and do not advertise unrestricted autonomous navigation.

The backend assigns each rover to one zone. Initially zones have eight configured checkpoints, `cp-01` through `cp-08`. Send `status: "inspecting"` with a checkpoint only when an inspection actually occurs. A checkpoint identifier represents an observation at that checkpoint; it is not a coordinate. The backend counts distinct reported checkpoints per UTC day and retains an inspection history.

Specify the state machine: `idle`, `patrolling`, `inspecting`, `stopped`, `error`. Include obstacle stopping, sensor-fault handling, watchdog behavior, lost-network behavior, startup motor disable, safe restart and supervised recovery. Establish the stopping distance through controlled tests. Safety decisions run locally on the rover and override remote motion requests.

### Backend contract

Use the same public API as the independent Python simulator:

```http
POST /api/v1/telemetry
Authorization: Bearer <this rover's API key>
Content-Type: application/json
```

Use the exact version-1 payload in [docs/telemetry-example.json](docs/telemetry-example.json). Generate a globally unique event ID from rover ID, boot ID and monotonic sequence. Synchronize the clock and send an ISO 8601 timestamp with `Z` or an explicit offset. Current ingestion permits up to 120 seconds of future clock skew and 30 days of historical backfill. Send every 10 seconds by default.

- `201` means stored; `200` with `status: duplicate` means that exact event was already stored. Both are successful delivery.
- Retrying an event must preserve every field and its `event_id`. A reused ID with changed content receives `409`.
- `401` means incorrect credentials; `403` means a zone-ownership mismatch; `422` means invalid data. Correct these conditions instead of endlessly resending invalid payloads.
- Retry timeouts, network errors, `408`, `429` and server failures with exponential backoff and bounded buffering. Preserve measurement time rather than substituting retry time.
- An unavailable sensor is null or omitted. All raw fields are integers. Never substitute zero for a missing reading.
- The operator registers hardware with `source: hardware`. Source is fixed by the server's device registry and cannot be asserted by a telemetry payload.
- Keep the per-device API key in device configuration. Use certificate-validated HTTPS outside a trusted local bench network; do not embed credentials in browser code or a public firmware repository.

Poll `GET /api/v1/robots/<robot_id>/commands` using the same per-device Bearer key. The response includes the current zone assignment and pending commands. Supported actions are `patrol`, `inspect`, `stop` and `idle`. Apply only unexpired commands for this device, then acknowledge with `POST /api/v1/robots/<robot_id>/commands/<command_id>/ack` and `{"status":"completed","note":"..."}` or `{"status":"failed","note":"..."}`. Command execution and acknowledgements must be idempotent across network retries; store a bounded journal of executed command IDs. Never treat a remote command as proof that motion occurred.

### Deliverables and acceptance

1. A requirements sheet with explicit assumptions and unresolved choices: target budget in INR, operating surface, autonomy method, dimensions, runtime target and fabrication tools.
2. Two mechanical concepts and a justified selection, followed by editable CAD and dimensioned drawings.
3. Electrical schematic, power/current calculations, complete pin map, harness/connector map and a BOM with verified supplier links, quantities and dated prices.
4. Modular firmware structure covering sensor drivers, calibration, state machine, navigation, local safety, telemetry transport, retry buffer and commands.
5. A calibration and test plan for every sensor. Include null handling, clock synchronization, duplicate delivery, Wi-Fi loss, reconnect/backfill, API rejection and simulated API failure.
6. A demonstration checklist that proves telemetry appears on the existing dashboard, hardware is labeled correctly, zone ownership is enforced, a threshold condition opens one alert, recovery resolves it, and stopping transmissions produces stale/offline status.
7. Motor tests with wheels raised first, followed by supervised surface tests; documented emergency-stop and watchdog results. Do not claim outdoor readiness until environmental and motion tests support it.

Start with the architecture and design assumptions. If the budget or terrain is unknown, propose clearly labeled alternatives and proceed with a conservative supervised demonstration design. Preserve the existing API and dashboard architecture.
