# ESP32-CAM competition setup

Open **Rover fleet -> ESP32 Camera**. The camera panel has Live/Offline status, the age of the displayed image, measured displayed fps, and Expand/Collapse controls. Camera connectivity is separate from rover sensor connectivity.

## Quality-first balance: re-upload the sketch

The default is now **320 x 240**, full **RGB565 source color** (up to 65,536 source color values), and **JPEG quality 88 with 4:4:4 chroma** on the computer. This restores four times the pixels of 160 x 120 speed mode and removes the RGB332 reduction to 256 colors. JPEG remains lossy, but finer color gradients and colored edges are retained much better.

The firmware targets **10 fps**, giving capture and upload more time rather than sacrificing detail to chase 20-30 fps. This is a target, not a measured physical-device guarantee. Resolution and source color depth remain fixed when Wi-Fi is slow; actual fps decreases instead of automatically downgrading the image.

The ESP32 sends 153,600 bytes per 320 x 240 RGB565 frame and the computer encodes JPEG. This keeps JPEG work off the ESP32, but uses more Wi-Fi bandwidth than the old speed mode. Working PSRAM holds both the driver frame and independent upload packet. If PSRAM is unavailable, the firmware explicitly logs a 160 x 120 fallback while retaining full RGB565 color. Check Serial Monitor if detail remains low.

The low-latency changes remain: one driver buffer returned before network I/O, a reusable independent upload copy, persistent HTTP, bounded latest-frame storage and no growing queue. Shorter timeouts and discarding a queued frame after a network error limit delayed-image playback.

**Re-upload the NEW configured sketch to restore resolution and source colors.** Refreshing the dashboard alone cannot recover detail already discarded by the old RGB332 firmware. The server continues accepting both older JPEG and RGB332 uploads.

- Complete configured sketch: `instance/AgriSentinelCamera/AgriSentinelCamera.ino`.
- Keep `instance/AgriSentinelCamera/partitions.csv` beside it.
- Rover: `rv-camera-01`, named **ESP32 Camera**.
- Wi-Fi: **Password Nahi Pata**, with the password supplied in the conversation.
- Server IP verified on 14 November 2026: **http://192.168.107.147:5000**.
- Wi-Fi settings and the device key are already filled in. Keep `instance/` private; it is ignored by Git.

## Upload with Arduino IDE

1. Open the configured `.ino` under `instance/`, replacing the previous sketch. Do not append it below an existing `setup()` and `loop()`.
2. Select **AI Thinker ESP32-CAM**, **4 MB flash**, **DIO**, **40 MHz flash frequency**, the correct COM port, and initially **115200 upload speed**. The sketch compiled with Espressif ESP32 core **3.3.12**. Keep the supplied custom `partitions.csv` alongside the sketch.
3. If using manual programming wiring, connect GPIO0 to GND and reset for uploading. After uploading, disconnect GPIO0 from GND and reset again. A programming base may handle bootloader entry automatically.
4. Open Serial Monitor at **115200 baud**. Successful uploads print `HTTP 200` and measured fps, with separate **capture**, **copy**, **network**, and **total** times (the copy is labeled `pack` in Serial Monitor) in milliseconds. These identify where physical-device delay remains.
5. Reload the dashboard with **Ctrl+Shift+R**, sign in if needed, and select **ESP32 Camera**.

The pin map assumes a **classic AI Thinker ESP32-CAM**, not ESP32-S3. The camera ribbon marking alone does not identify the board. RHYX M21-45 / GC2145 does not provide hardware JPEG, so this mode requests RGB565 capture. Do not enable multiple raw driver buffers to chase fps. Power the camera through a suitable programming base or regulated 5V input; GPIO logic is 3.3V. Use a stable supply and keep motor power noise away from the camera. Disconnect power before adjusting its ribbon cable.

## Run the server

From the Backend folder:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

On this computer open **http://127.0.0.1:5000/#rovers**. Other devices on the same reachable network can use **http://192.168.107.147:5000/#rovers**. The server must listen on `0.0.0.0`, not only localhost; `app.py` defaults to this unless `.env` overrides `HOST`. Run only one server on port 5000. Do not start the separate old camera-upload project on the same port.

If Wi-Fi or the computer's address changes, regenerate the configured sketch and upload it again:

```powershell
.\.venv\Scripts\python.exe scripts/setup_camera.py --server-url http://NEW_COMPUTER_WIFI_IP:5000 --ssid "YOUR_WIFI_NAME"
```

Omitting `--password` prompts privately. Existing matching rover credentials are reused. The ESP32 must use the computer's address, not its own address. The firmware uses local HTTP; HTTPS/cloud needs a separately configured secure client.

## If it still lags

- Check the **actual fps and stage times** in Serial Monitor. A high capture time points to sensor/capture timing; high network time points to Wi-Fi reachability/congestion. The viewer's age is measured from receipt at the server, not a synchronized camera shutter timestamp.
- Keep the camera close to the Wi-Fi router/hotspot and use stable power. Hotspots may isolate clients. Verify Windows Firewall allows the Python server on this network; do not disable the firewall globally.
- If colors/detail are still coarse, verify that the new RGB565 sketch was uploaded and that PSRAM was detected. The new default is already 320 x 240. Prefer improving Wi-Fi or lowering `TARGET_FPS` over reducing resolution/color depth; reflash after changing firmware constants.
- `HTTP 401/403`: use the configured hardware rover/key. `HTTP 404/415`: restart the updated Backend server; the old server may not understand RGB565 packets. `HTTP 422`: packet dimensions/length do not match. No serial port: check the programming adapter, driver and USB data cable.
- Camera live with rover not connected is expected for a camera-only ESP32. The rover status refers to its telemetry controller.

## Live transport and storage

- Upload: `POST /api/v1/robots/<id>/camera/frame`, Bearer device key. Accepts `image/jpeg`, legacy `application/x-rgb332`, and quality-mode `application/x-rgb565` bodies with `X-Camera-Width` / `X-Camera-Height`. RGB565 is high-byte-first, two bytes per pixel (5 red, 6 green, 5 blue bits); its exact body length is width * height * 2. Legacy RGB332 uses one byte per pixel. Raw dimensions are capped at 320 x 240. JPEG remains limited to 640 x 480; bodies remain capped at 256 KiB.
- View: one authenticated request to `.../camera/latest.jpg?after=<last version>`. A new image returns JPEG plus version, size and freshness headers. An unchanged image returns **204 without retransmitting its body**. The viewer makes no extra JSON status request per frame, decodes one image at a time, and revokes obsolete object URLs.
- The viewer samples up to every 20 ms while the panel and tab are active. No-image/error states back off to 200 ms; hidden tabs pause requests. Closing the panel aborts the request, stops timers and releases image URLs. The image CSP explicitly allows local blob URLs for authenticated image data.
- Frames are served from a bounded per-process latest-frame RAM cache. The first frame and then at most one checkpoint every two seconds are persisted per rover. A restart can lose up to two seconds of recent camera images; the last checkpoint remains available. This path is intended for the supplied **single-process Waitress** competition server. Multiple worker processes would need a shared frame cache.
- Existing sensor data, keys, history, 16 KiB JSON limits and telemetry connectivity remain separate. No video archive is created.

## Verification

Server and HTTP/DOM tests use isolated synthetic frames. Firmware compilation and a server throughput test do not establish the actual speed of the newly flashed physical camera. Browser image events in jsdom are simulated, not visual rendering proof. See `docs/VERIFICATION.md` for measured results.

References: [Espressif camera driver](https://github.com/espressif/esp32-camera), [RGB565 byte order used by Espressif's converter](https://github.com/espressif/esp32-camera/blob/master/conversions/to_jpg.cpp), [official AI Thinker pin map](https://github.com/espressif/arduino-esp32/blob/3.3.12/libraries/ESP32/examples/Camera/CameraWebServer/camera_pins.h).
