# ESP32-CAM competition setup

Open **Rover fleet -> ESP32 Camera**. The camera panel has Live/Offline status, the age of the displayed image, measured displayed fps, and Expand/Collapse controls. Camera connectivity is separate from rover sensor connectivity.

## Production upload mode: JPEG before HTTPS

The camera still captures **320 x 240 RGB565**, preserving the source color precision. The ESP32 now converts it to **JPEG quality 88 with 4:4:4 chroma** before uploading. The server validates and serves those JPEG bytes without re-encoding. JPEG is lossy; matching the previous quality setting does not imply pixel-identical results between encoders.

The previous raw mode sent **153,600 bytes per frame** over the internet and encoded JPEG on the server. Production measurements on 14 November 2026 showed roughly **0.48 incoming fps**, despite image retrieval taking around 3 ms and the server CPU being 95-97% idle. Compressing before the internet transfer addresses that bandwidth cost. After flashing the updated firmware on the physical GC2145 camera, a separate 30-second production sample measured **2.643 incoming fps** (80 distinct frames), about 5.5 times that earlier baseline. This is a short observation on the current Wi-Fi/internet path, not a sustained guarantee; the **10 fps** setting remains a target.

Resolution, RGB565 capture, and chroma resolution are not automatically reduced on a slow connection. Without PSRAM the explicit fallback remains 160 x 120 RGB565. One raw driver buffer and a bounded, reusable JPEG packet prevent an accumulating queue. The buffer is returned after encoding and before network I/O, allowing capture during upload. Overflow or encoding failures drop the frame instead of uploading corrupt data. HTTP/1.1 reuse, response-body consumption, and reset-on-error remain enabled.

**Pulling the server repository does not update the ESP32. Re-upload the generated firmware to activate compressed uploads.** Existing raw RGB565, RGB332, and older JPEG firmware remain supported by the backend.

- Configured sketch: `instance/AgriSentinelCamera/AgriSentinelCamera.ino`.
- Keep its `partitions.csv` alongside it.
- Production origin: **https://agri-sentinel.qunivex.com**.
- Credentials remain in ignored `instance/`; the tracked template contains placeholders.
- HTTPS verifies certificates with the built-in CA bundle and synchronizes its clock with NTP. It does not disable certificate verification.

## Upload with Arduino IDE

1. Open the configured `.ino` under `instance/`, replacing the previous sketch. Do not append it below an existing `setup()` and `loop()`.
2. Select **AI Thinker ESP32-CAM**, **4 MB flash**, **DIO**, **40 MHz flash frequency**, the correct COM port, and initially **115200 upload speed**. The sketch compiled with Espressif ESP32 core **3.3.12**. Keep the supplied custom `partitions.csv` alongside the sketch.
3. If using manual programming wiring, connect GPIO0 to GND and reset for uploading. After uploading, disconnect GPIO0 from GND and reset again. A programming base may handle bootloader entry automatically.
4. Open Serial Monitor at **115200 baud**. Successful uploads print `HTTP 200` and measured fps, with separate **capture**, **encode**, **network**, and **total** times in milliseconds. These identify where physical-device delay remains.
5. Reload the dashboard with **Ctrl+Shift+R**, sign in if needed, and select **ESP32 Camera**.

The pin map assumes a **classic AI Thinker ESP32-CAM**, not ESP32-S3. The camera ribbon marking alone does not identify the board. RHYX M21-45 / GC2145 does not provide hardware JPEG, so the firmware captures RGB565 and encodes JPEG in software. Do not enable multiple raw driver buffers to chase fps. Power the camera through a suitable programming base or regulated 5V input; GPIO logic is 3.3V. Use a stable supply and keep motor power noise away from the camera. Disconnect power before adjusting its ribbon cable.

## Run the server

From the Backend folder:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

On this computer open **http://127.0.0.1:5000/#rovers**. Other devices on the same reachable LAN can use `http://YOUR_COMPUTER_WIFI_IP:5000/#rovers`; check the current address with `ipconfig`. The server must listen on `0.0.0.0`, not only localhost; `app.py` defaults to this unless `.env` overrides `HOST`. Run only one server on port 5000. Do not start the separate old camera-upload project on the same port.

If Wi-Fi or the computer's address changes, regenerate the configured sketch and upload it again:

```powershell
.\.venv\Scripts\python.exe scripts/setup_camera.py --server-url http://NEW_COMPUTER_WIFI_IP:5000 --ssid "YOUR_WIFI_NAME"
```

Omitting `--password` prompts privately. Existing matching rover credentials are reused. The ESP32 must use the computer's address, not its own address. Both LAN HTTP and production HTTPS are supported. For production, use `--server-url https://agri-sentinel.qunivex.com`. Existing rover provisioning must match the destination server; this tool provisions the local database and does not register devices on a remote installation.

## If it still lags

- Check the **actual fps and stage times** in Serial Monitor. A high capture time points to sensor/capture timing; high encode time points to software compression; high network time includes Wi-Fi, internet transfer, TLS, and server response time. The viewer's age is measured from receipt at the server, not a synchronized camera shutter timestamp.
- Keep the camera close to the Wi-Fi router/hotspot and use stable power. Hotspots may isolate clients. Verify Windows Firewall allows the Python server on this network; do not disable the firewall globally.
- If colors/detail are still coarse, verify that the new 320 x 240 JPEG-upload sketch was flashed and that PSRAM was detected. The new default is already 320 x 240. Prefer improving Wi-Fi or lowering `TARGET_FPS` over reducing resolution/color depth; reflash after changing firmware constants.
- `HTTP 401/403`: use the configured hardware rover/key. `HTTP 404/415`: check the destination URL and backend version. `HTTP 422`: packet dimensions/length do not match. No serial port: check the programming adapter, driver and USB data cable.
- Camera live with rover not connected is expected for a camera-only ESP32. The rover status refers to its telemetry controller.

## Live transport and storage

- Upload: `POST /api/v1/robots/<id>/camera/frame`, Bearer device key. New firmware sends `image/jpeg`; the endpoint also accepts legacy `application/x-rgb332`, and quality-mode `application/x-rgb565` bodies with `X-Camera-Width` / `X-Camera-Height`. RGB565 is high-byte-first, two bytes per pixel (5 red, 6 green, 5 blue bits); its exact body length is width * height * 2. Legacy RGB332 uses one byte per pixel. Raw dimensions are capped at 320 x 240. JPEG remains limited to 640 x 480; bodies remain capped at 256 KiB.
- View: one authenticated request to `.../camera/latest.jpg?after=<last version>`. A new image returns JPEG plus version, size and freshness headers. An unchanged image returns **204 without retransmitting its body**. The viewer makes no extra JSON status request per frame, decodes one image at a time, and revokes obsolete object URLs.
- The viewer samples up to every 20 ms while the panel and tab are active. No-image/error states back off to 200 ms; hidden tabs pause requests. Closing the panel aborts the request, stops timers and releases image URLs. The image CSP explicitly allows local blob URLs for authenticated image data.
- Frames are served from a bounded per-process latest-frame RAM cache. The first frame and then at most one checkpoint every two seconds are persisted per rover. A restart can lose up to two seconds of recent camera images; the last checkpoint remains available. This path is intended for the supplied **single-process Waitress** competition server. Multiple worker processes would need a shared frame cache.
- Existing sensor data, keys, history, 16 KiB JSON limits and telemetry connectivity remain separate. No video archive is created.

## Verification

Server and HTTP/DOM tests use isolated synthetic frames. Firmware compilation and a server throughput test do not establish the actual speed of the newly flashed physical camera. Browser image events in jsdom are simulated, not visual rendering proof. See `docs/VERIFICATION.md` for measured results.

References: [Espressif camera driver](https://github.com/espressif/esp32-camera), [RGB565 byte order used by Espressif's converter](https://github.com/espressif/esp32-camera/blob/master/conversions/to_jpg.cpp), [official AI Thinker pin map](https://github.com/espressif/arduino-esp32/blob/3.3.12/libraries/ESP32/examples/Camera/CameraWebServer/camera_pins.h).
