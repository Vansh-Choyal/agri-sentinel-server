// Complete sketch for the classic AI Thinker ESP32-CAM pinout.
// Run scripts/setup_camera.py to create a configured copy under instance/.
// Capture RGB565, encode JPEG once on-device, then upload over persistent HTTPS.
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include "esp_camera.h"
#include "img_converters.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include <time.h>
#include <atomic>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

const char *WIFI_SSID = @WIFI_SSID@;
const char *WIFI_PASSWORD = @WIFI_PASSWORD@;
const char *SERVER_URL = @SERVER_URL@;
const char *ROBOT_ID = @ROBOT_ID@;
const char *DEVICE_KEY = @DEVICE_KEY@;
const uint32_t TARGET_FPS = 10;
const uint32_t FRAME_INTERVAL_MS = 1000 / TARGET_FPS;
const uint8_t UPLOAD_JPEG_QUALITY = 88;
// Preserve full sensor color and restore four times the pixels of speed mode.
const framesize_t CAMERA_FRAME_SIZE = FRAMESIZE_QVGA;

bool cameraReady = false;
uint32_t lastCameraAttempt = 0, lastWifiAttempt = 0, lastFrameAt = 0;
uint8_t captureFailures = 0;
WiFiClient connection;
WiFiClientSecure secureConnection;
HTTPClient http;
bool httpReady = false;
uint32_t fpsWindowStart = 0, uploadedFrames = 0, frameWorkMs = 0;
// One packet can upload while another is encoded and one latest packet waits.
// Only slot ownership changes are locked; no capture, encoding or I/O under a lock.
struct JpegPacket {
  uint8_t *data = nullptr;
  size_t capacity = 0, size = 0;
  bool overflow = false;
  uint16_t width = 0, height = 0;
  uint32_t startedAt = 0, captureMs = 0, encodeMs = 0;
};
JpegPacket packets[3];
// Producer-owned scratch lets the single camera buffer refill during encoding.
uint8_t *captureCopy = nullptr;
size_t captureCapacity = 0;
enum SlotState { SLOT_FREE, SLOT_ENCODING, SLOT_READY, SLOT_SENDING };
SlotState slots[3] = {SLOT_FREE, SLOT_FREE, SLOT_FREE};
portMUX_TYPE packetMux = portMUX_INITIALIZER_UNLOCKED;
TaskHandle_t senderTask = nullptr;
int pendingPacket = -1;
uint32_t replacedFrames = 0;
std::atomic<bool> discardQueuedFrame{false};
uint32_t captureTotalMs = 0, packTotalMs = 0, sendTotalMs = 0;

int acquireEncodingSlot() {
  int result = -1;
  portENTER_CRITICAL(&packetMux);
  for (int i = 0; i < 3; ++i) {
    if (slots[i] == SLOT_FREE) { slots[i] = SLOT_ENCODING; result = i; break; }
  }
  portEXIT_CRITICAL(&packetMux);
  return result;
}

void releasePacket(int index) {
  portENTER_CRITICAL(&packetMux);
  slots[index] = SLOT_FREE;
  portEXIT_CRITICAL(&packetMux);
}

void publishPacket(int index) {
  portENTER_CRITICAL(&packetMux);
  if (pendingPacket >= 0) {
    slots[pendingPacket] = SLOT_FREE;
    ++replacedFrames;
  }
  slots[index] = SLOT_READY;
  pendingPacket = index;
  portEXIT_CRITICAL(&packetMux);
  if (senderTask) xTaskNotifyGive(senderTask);
}

int takeLatestPacket() {
  portENTER_CRITICAL(&packetMux);
  int index = pendingPacket;
  if (index >= 0) {
    pendingPacket = -1;
    slots[index] = SLOT_SENDING;
  }
  portEXIT_CRITICAL(&packetMux);
  return index;
}

void clearPendingPacket() {
  portENTER_CRITICAL(&packetMux);
  if (pendingPacket >= 0) { slots[pendingPacket] = SLOT_FREE; pendingPacket = -1; }
  portEXIT_CRITICAL(&packetMux);
}

// The encoder callback does not propagate short writes. Track overflow ourselves
// and never send a truncated JPEG. Each encoder invocation owns its packet.
size_t writeJpeg(void *arg, size_t index, const void *data, size_t length) {
  JpegPacket &packet = *static_cast<JpegPacket *>(arg);
  if (!length) return 0;
  if (packet.overflow || index != packet.size || index > packet.capacity || length > packet.capacity - index) {
    packet.overflow = true;
    return 0;
  }
  memcpy(packet.data + index, data, length);
  packet.size += length;
  return length;
}

void closeTransport() {
  // Force-close only on errors/disconnection; successful requests reuse the socket.
  http.setReuse(false);
  http.end();
  connection.stop();
  secureConnection.stop();
  httpReady = false;
}

bool initializeCamera() {
  const bool hasPsram = psramFound();
  Serial.printf("PSRAM: %s; free heap: %u bytes\n", hasPsram ? "yes" : "no", ESP.getFreeHeap());
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = 5;
  config.pin_d1 = 18;
  config.pin_d2 = 19;
  config.pin_d3 = 21;
  config.pin_d4 = 36;
  config.pin_d5 = 39;
  config.pin_d6 = 34;
  config.pin_d7 = 35;
  config.pin_xclk = 0;
  config.pin_pclk = 22;
  config.pin_vsync = 25;
  config.pin_href = 23;
  config.pin_sccb_sda = 26;
  config.pin_sccb_scl = 27;
  config.pin_pwdn = 32;
  config.pin_reset = -1;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_RGB565;
  config.frame_size = hasPsram ? CAMERA_FRAME_SIZE : FRAMESIZE_QQVGA;
  // The 38,400-byte QQVGA raw frame fits in faster internal RAM.
  config.fb_location = CAMERA_FRAME_SIZE == FRAMESIZE_QQVGA || !hasPsram ? CAMERA_FB_IN_DRAM : CAMERA_FB_IN_PSRAM;
  if (!hasPsram) Serial.println("PSRAM not available: using 160x120 fallback with full RGB565 color.");
  config.fb_count = 1;
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  config.jpeg_quality = 15;
  // Match the server's existing quality mode; do not reduce colored-edge detail.
  jpgSetChroma(CHROMA_444);
  jpgSetRgb565BE(true);
  esp_err_t error = esp_camera_init(&config);
  if (error != ESP_OK) {
    Serial.printf("Camera init failed: 0x%x (%s). Retrying in 10s.\n", error, esp_err_to_name(error));
    return false;
  }
  sensor_t *sensor = esp_camera_sensor_get();
  if (!sensor) {
    esp_camera_deinit();
    Serial.println("Sensor not detected. Check ribbon cable with power disconnected.");
    return false;
  }
  camera_sensor_info_t *info = esp_camera_sensor_get_info(&sensor->id);
  Serial.printf("Camera: %s, PID 0x%04x; RGB565 -> device JPEG quality 88, 4:4:4; frame size %d; target %u fps\n",
      info ? info->name : "unknown", sensor->id.PID, (int)config.frame_size, (unsigned)TARGET_FPS);
  captureFailures = 0;
  return true;
}

void encodeFrame() {
  if (String(SERVER_URL).startsWith("https://") && time(nullptr) < 1700000000) return;
  const int index = acquireEncodingSlot();
  if (index < 0) return;
  JpegPacket &packet = packets[index];
  const uint32_t startedAt = millis();
  camera_fb_t *frame = esp_camera_fb_get();
  if (frame && discardQueuedFrame.exchange(false)) {
    esp_camera_fb_return(frame);
    frame = esp_camera_fb_get();
  }
  if (!frame) {
    releasePacket(index);
    Serial.println("Capture failed.");
    if (++captureFailures >= 3) {
      esp_camera_deinit();
      cameraReady = false;
      lastCameraAttempt = millis();
    }
    return;
  }
  captureFailures = 0;
  const uint32_t capturedAt = millis();
  const size_t rawBytes = (size_t)frame->width * frame->height * 2;
  if (frame->format != PIXFORMAT_RGB565 || frame->len < rawBytes) {
    esp_camera_fb_return(frame);
    releasePacket(index);
    Serial.println("Incomplete RGB565 frame. Retrying...");
    return;
  }
  const size_t capacity = rawBytes + 4096;
  if (packet.capacity < capacity) {
    const uint32_t caps = MALLOC_CAP_8BIT | (capacity > 65536 && psramFound() ? MALLOC_CAP_SPIRAM : MALLOC_CAP_INTERNAL);
    uint8_t *allocated = (uint8_t *)heap_caps_realloc(packet.data, capacity, caps);
    if (!allocated) {
      esp_camera_fb_return(frame);
      releasePacket(index);
      Serial.println("JPEG packet allocation failed. Frame dropped.");
      delay(100);
      return;
    }
    packet.data = allocated;
    packet.capacity = capacity;
  }
  packet.width = frame->width;
  packet.height = frame->height;
  packet.startedAt = startedAt;
  packet.captureMs = capturedAt - startedAt;
  packet.size = 0;
  packet.overflow = false;
  bool encoded;
  if (senderTask) {
    if (captureCapacity < rawBytes) {
      uint8_t *allocated = (uint8_t *)heap_caps_realloc(captureCopy, rawBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
      if (!allocated) {
        esp_camera_fb_return(frame);
        releasePacket(index);
        Serial.println("Capture scratch allocation failed. Frame dropped.");
        delay(100);
        return;
      }
      captureCopy = allocated;
      captureCapacity = rawBytes;
    }
    memcpy(captureCopy, frame->buf, rawBytes);
    // Return BEFORE JPEG encoding: the driver can now refill while the encoder
    // reads the independent copy. Keep one raw driver buffer for GC2145 safety.
    esp_camera_fb_return(frame);
    encoded = fmt2jpg_cb(captureCopy, rawBytes, packet.width, packet.height,
                        PIXFORMAT_RGB565, UPLOAD_JPEG_QUALITY, writeJpeg, &packet);
  } else {
    encoded = frame2jpg_cb(frame, UPLOAD_JPEG_QUALITY, writeJpeg, &packet);
    esp_camera_fb_return(frame);
  }
  packet.encodeMs = millis() - capturedAt;
  if (!encoded || packet.overflow || packet.size < 4 || packet.data[0] != 0xff || packet.data[1] != 0xd8 ||
      packet.data[packet.size - 2] != 0xff || packet.data[packet.size - 1] != 0xd9) {
    releasePacket(index);
    Serial.println("JPEG encoding failed or exceeded the bounded packet. Frame dropped.");
    delay(25);
    return;
  }
  publishPacket(index);
}

// Only the sender owns the HTTP/TLS clients, including resets and disconnects.
void sendLatestPacket() {
  if (WiFi.status() != WL_CONNECTED) {
    if (httpReady) closeTransport();
    clearPendingPacket();
    discardQueuedFrame.store(true);
    return;
  }
  // Certificate validity checks need a synchronized clock before HTTPS.
  if (String(SERVER_URL).startsWith("https://") && time(nullptr) < 1700000000) return;
  const int index = takeLatestPacket();
  if (index < 0) return;
  JpegPacket &packet = packets[index];
  // Do not replay a queued image after a network stall/reconnect.
  if (millis() - packet.startedAt > 1000) { releasePacket(index); return; }
  const uint32_t networkStarted = millis();
  if (!httpReady) {
    String endpoint = String(SERVER_URL) + "/api/v1/robots/" + ROBOT_ID + "/camera/frame";
    http.setConnectTimeout(10000);
    http.setTimeout(10000);
    http.setReuse(true);
    bool requestStarted;
    if (String(SERVER_URL).startsWith("https://")) {
      secureConnection.useBuiltinCACertBundle();
      requestStarted = http.begin(secureConnection, endpoint);
    } else {
      requestStarted = http.begin(connection, endpoint);
    }
    if (!requestStarted) {
      releasePacket(index);
      Serial.println("Could not start HTTP request. Check SERVER_URL and TLS certificate bundle.");
      delay(100);
      return;
    }
    httpReady = true;
  }
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Camera-Width", String(packet.width));
  http.addHeader("X-Camera-Height", String(packet.height));
  http.addHeader("Authorization", String("Bearer ") + DEVICE_KEY);
  int status = http.POST(packet.data, packet.size);
  // Consume every response body before reusing HTTP/1.1.
  String responseBody = status > 0 ? http.getString() : String();
  if (status == 200) {
    uploadedFrames++;
    frameWorkMs += millis() - packet.startedAt;
    captureTotalMs += packet.captureMs;
    packTotalMs += packet.encodeMs;
    sendTotalMs += millis() - networkStarted;
    const uint32_t elapsed = millis() - fpsWindowStart;
    if (elapsed >= 1000) {
      portENTER_CRITICAL(&packetMux);
      const uint32_t replaced = replacedFrames;
      replacedFrames = 0;
      portEXIT_CRITICAL(&packetMux);
      Serial.printf("Frame %ux%u, JPEG %u bytes -> HTTP 200; %.1f fps / target %u; capture %u ms, encode %u ms, network %u ms, age %u ms; replaced %u; heap %u bytes\n",
          packet.width, packet.height, (unsigned)packet.size, uploadedFrames * 1000.0f / elapsed, (unsigned)TARGET_FPS,
          (unsigned)(captureTotalMs / uploadedFrames), (unsigned)(packTotalMs / uploadedFrames),
          (unsigned)(sendTotalMs / uploadedFrames), (unsigned)(frameWorkMs / uploadedFrames),
          (unsigned)replaced, ESP.getFreeHeap());
      uploadedFrames = frameWorkMs = 0;
      captureTotalMs = packTotalMs = sendTotalMs = 0;
      fpsWindowStart = millis();
    }
  } else {
    if (status > 0) { Serial.printf("HTTP %d\n", status); Serial.println(responseBody); }
    else Serial.println(HTTPClient::errorToString(status));
    closeTransport();
    clearPendingPacket();
    discardQueuedFrame.store(true);
  }
  releasePacket(index);
  if (status != 200) delay(100);
}

void uploadTask(void *) {
  for (;;) {
    sendLatestPacket();
    // Notifications wake this task as soon as a fresh JPEG is published. Yield
    // even during startup/offline periods so Wi-Fi and system tasks can run.
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(100));
  }
}

void setup() {
  Serial.begin(115200);
  setCpuFrequencyMhz(240);
  delay(1000);
  Serial.printf("\nAgriSentinel camera; reset reason %d\n", (int)esp_reset_reason());
  // Keep the bright flash LED off. It is not needed for streaming.
  pinMode(4, OUTPUT);
  digitalWrite(4, LOW);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWifiAttempt = millis();
  Serial.printf("Connecting to %s\n", WIFI_SSID);
  cameraReady = initializeCamera();
  lastCameraAttempt = millis();
  fpsWindowStart = millis();
  if (psramFound() && xTaskCreatePinnedToCore(uploadTask, "camera-upload", 8192, nullptr, 1, &senderTask, 0) == pdPASS) {
    Serial.println("JPEG pipeline enabled: encoder core 1, uploader core 0; one latest waiting frame.");
  } else {
    senderTask = nullptr;
    Serial.println("Using sequential JPEG uploads (PSRAM/task unavailable).");
  }
}

void loop() {
  static bool wifiReported = false;
  const uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    if (!senderTask) sendLatestPacket();
    if (wifiReported) Serial.println("Wi-Fi disconnected. Reconnecting...");
    wifiReported = false;
    if (now - lastWifiAttempt >= 15000) {
      lastWifiAttempt = now;
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      Serial.println("Retrying Wi-Fi connection...");
    }
    delay(25);
    return;
  }
  if (!wifiReported) {
    wifiReported = true;
    if (String(SERVER_URL).startsWith("https://")) {
      configTime(0, 0, "pool.ntp.org", "time.google.com");
      Serial.println("Synchronizing clock for HTTPS certificate validation...");
    }
    Serial.print("Wi-Fi connected. Camera IP: ");
    Serial.println(WiFi.localIP());
    Serial.printf("Server: %s; Rover: %s\n", SERVER_URL, ROBOT_ID);
  }
  if (!cameraReady) {
    if (now - lastCameraAttempt >= 10000) {
      lastCameraAttempt = now;
      cameraReady = initializeCamera();
    }
    delay(25);
    return;
  }
  if (now - lastFrameAt >= FRAME_INTERVAL_MS) {
    lastFrameAt = now;
    encodeFrame();
    if (!senderTask) sendLatestPacket();
  }
  // Never queue catch-up frames when capture/upload is slower than the target.
  delay(1);
}
