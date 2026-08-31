#include <Arduino.h>
#include <WiFi.h>
#include <esp_camera.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <time.h>

#include "foodlog/board_motion_detector.hpp"
#include "foodlog/capture_core.hpp"
#include "foodlog/device_config.hpp"
#include "foodlog/foodlog_http.hpp"

namespace {

constexpr char kFirmwareVersion[] = "foodlog-fnk0085-0.1.0";
constexpr std::uint32_t kSerialBaud = 115200;
constexpr std::uint32_t kMotionFrameIntervalMilliseconds = 200;
constexpr std::uint32_t kSnapshotPollIntervalMilliseconds = 2'000;
constexpr std::uint32_t kWifiRetryIntervalMilliseconds = 10'000;
constexpr std::uint32_t kStatusRetryIntervalMilliseconds = 30'000;
constexpr int kRequiredMotionFrames = 2;

// Freenove ESP32-S3 WROOM camera wiring (FNK0085), verified on this board.
constexpr int kCameraPinPowerDown = -1;
constexpr int kCameraPinReset = -1;
constexpr int kCameraPinXclk = 15;
constexpr int kCameraPinSiod = 4;
constexpr int kCameraPinSioc = 5;
constexpr int kCameraPinD0 = 11;
constexpr int kCameraPinD1 = 9;
constexpr int kCameraPinD2 = 8;
constexpr int kCameraPinD3 = 10;
constexpr int kCameraPinD4 = 12;
constexpr int kCameraPinD5 = 18;
constexpr int kCameraPinD6 = 17;
constexpr int kCameraPinD7 = 16;
constexpr int kCameraPinVsync = 6;
constexpr int kCameraPinHref = 7;
constexpr int kCameraPinPclk = 13;

foodlog::DeviceConfig device_config;
foodlog::DeviceConfigStore config_store;
foodlog::SerialProvisioner provisioner;
foodlog::BoardMotionDetector motion_detector;
foodlog::MotionController motion_controller;

bool configured = false;
bool camera_ready = false;
bool time_ready = false;
bool api_ready = false;
int consecutive_motion_frames = 0;
std::uint32_t sequence_number = 0;
std::uint64_t last_motion_frame_at = 0;
std::uint64_t last_snapshot_poll_at = 0;
std::uint64_t last_wifi_retry_at = 0;
std::uint64_t last_status_retry_at = 0;
bool status_check_started = false;
String boot_sequence_id;

std::uint64_t uptime_milliseconds() {
  return static_cast<std::uint64_t>(esp_timer_get_time()) / 1'000;
}

String make_boot_sequence_id() {
  char value[40];
  snprintf(value, sizeof(value), "fnk0085-%08lx-%08lx",
           static_cast<unsigned long>(esp_random()),
           static_cast<unsigned long>(esp_random()));
  return String(value);
}

String utc_timestamp() {
  const time_t now = time(nullptr);
  if (now < 1'700'000'000) {
    return "";
  }
  tm utc = {};
  gmtime_r(&now, &utc);
  char value[25];
  strftime(value, sizeof(value), "%Y-%m-%dT%H:%M:%SZ", &utc);
  return String(value);
}

camera_config_t camera_configuration() {
  camera_config_t config = {};
  config.pin_pwdn = kCameraPinPowerDown;
  config.pin_reset = kCameraPinReset;
  config.pin_xclk = kCameraPinXclk;
  config.pin_sccb_sda = kCameraPinSiod;
  config.pin_sccb_scl = kCameraPinSioc;
  config.pin_d0 = kCameraPinD0;
  config.pin_d1 = kCameraPinD1;
  config.pin_d2 = kCameraPinD2;
  config.pin_d3 = kCameraPinD3;
  config.pin_d4 = kCameraPinD4;
  config.pin_d5 = kCameraPinD5;
  config.pin_d6 = kCameraPinD6;
  config.pin_d7 = kCameraPinD7;
  config.pin_vsync = kCameraPinVsync;
  config.pin_href = kCameraPinHref;
  config.pin_pclk = kCameraPinPclk;
  config.xclk_freq_hz = 20'000'000;
  config.ledc_timer = LEDC_TIMER_0;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_VGA;
  config.jpeg_quality = 12;
  config.fb_count = psramFound() ? 2 : 1;
  config.grab_mode = psramFound() ? CAMERA_GRAB_LATEST
                                  : CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM
                                    : CAMERA_FB_IN_DRAM;
  return config;
}

void begin_wifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  const String suffix =
      device_config.camera_id.substring(device_config.camera_id.length() - 6);
  WiFi.setHostname(("foodlog-camera-" + suffix).c_str());
  WiFi.begin(device_config.wifi_ssid.c_str(),
             device_config.wifi_password.c_str());
  last_wifi_retry_at = uptime_milliseconds();
  api_ready = false;
  time_ready = false;
}

void maintain_wifi(const std::uint64_t now) {
  if (WiFi.status() == WL_CONNECTED ||
      now - last_wifi_retry_at < kWifiRetryIntervalMilliseconds) {
    return;
  }
  last_wifi_retry_at = now;
  WiFi.disconnect(false, false);
  WiFi.begin(device_config.wifi_ssid.c_str(),
             device_config.wifi_password.c_str());
  api_ready = false;
  time_ready = false;
}

void maintain_trusted_time_and_api(const std::uint64_t now) {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }
  if (!time_ready) {
    configTime(0, 0, "time.google.com", "pool.ntp.org");
    const std::uint32_t deadline = millis() + 15'000;
    while (time(nullptr) < 1'700'000'000 &&
           static_cast<std::int32_t>(deadline - millis()) > 0) {
      delay(100);
      bool restart_requested = false;
      provisioner.poll(device_config, restart_requested);
      if (restart_requested) {
        ESP.restart();
      }
    }
    time_ready = time(nullptr) >= 1'700'000'000;
    Serial.printf("TIME_SYNC status=%s\n", time_ready ? "ready" : "failed");
  }
  if (!time_ready ||
      (status_check_started &&
       now - last_status_retry_at < kStatusRetryIntervalMilliseconds)) {
    return;
  }
  status_check_started = true;
  last_status_retry_at = now;
  foodlog::FoodLogHttpClient client(device_config);
  api_ready = client.check_device_status();
  Serial.printf("API_STATUS status=%s ip=%s\n", api_ready ? "ready" : "failed",
                WiFi.localIP().toString().c_str());
}

void restore_motion_frame_size() {
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor != nullptr) {
    sensor->set_framesize(sensor, FRAMESIZE_VGA);
  }
  camera_fb_t* stale = esp_camera_fb_get();
  if (stale != nullptr) {
    esp_camera_fb_return(stale);
  }
}

bool upload_capture(const foodlog::BoardMotionAnalysis& motion,
                    const String& burst_id,
                    const std::uint32_t burst_frame_index,
                    const String& snapshot_request_id) {
  if (!api_ready || !time_ready) {
    return false;
  }
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor != nullptr && psramFound()) {
    sensor->set_framesize(sensor, FRAMESIZE_UXGA);
    delay(80);
    camera_fb_t* stale = esp_camera_fb_get();
    if (stale != nullptr) {
      esp_camera_fb_return(stale);
    }
  }
  camera_fb_t* frame = esp_camera_fb_get();
  if (frame == nullptr || frame->format != PIXFORMAT_JPEG) {
    if (frame != nullptr) {
      esp_camera_fb_return(frame);
    }
    restore_motion_frame_size();
    Serial.println("CAPTURE_FAILED reason=camera_frame_unavailable");
    return false;
  }

  foodlog::CaptureMetadata metadata;
  metadata.captured_at = utc_timestamp();
  metadata.sequence_id = boot_sequence_id;
  metadata.sequence_number = sequence_number++;
  metadata.burst_id = burst_id;
  metadata.burst_frame_index = burst_frame_index;
  metadata.snapshot_request_id = snapshot_request_id;
  metadata.width = frame->width;
  metadata.height = frame->height;
  metadata.motion = motion;

  const String idempotency_key = snapshot_request_id.isEmpty()
                                     ? boot_sequence_id + "-" +
                                           String(metadata.sequence_number)
                                     : "manual-" + snapshot_request_id;
  foodlog::FoodLogHttpClient client(device_config);
  String capture_id;
  foodlog::UploadResult result = foodlog::UploadResult::kTransientFailure;
  for (int attempt = 0; attempt < 3; ++attempt) {
    result = client.upload_jpeg(frame->buf, frame->len, metadata,
                                idempotency_key, capture_id);
    if (result != foodlog::UploadResult::kTransientFailure) {
      break;
    }
    delay(static_cast<std::uint32_t>(1'000U << attempt));
  }
  esp_camera_fb_return(frame);
  restore_motion_frame_size();

  switch (result) {
    case foodlog::UploadResult::kAcknowledged:
      Serial.printf("CAPTURE_ACCEPTED capture_id=%s source=%s\n",
                    capture_id.c_str(),
                    snapshot_request_id.isEmpty() ? "motion" : "manual");
      return true;
    case foodlog::UploadResult::kAuthenticationFailure:
      api_ready = false;
      Serial.println("CAPTURE_BLOCKED reason=authentication");
      break;
    case foodlog::UploadResult::kQuotaFailure:
      api_ready = false;
      Serial.println("CAPTURE_BLOCKED reason=quota");
      break;
    case foodlog::UploadResult::kPermanentFailure:
      Serial.println("CAPTURE_DROPPED reason=invalid_request");
      break;
    case foodlog::UploadResult::kTransientFailure:
      api_ready = false;
      Serial.println("CAPTURE_DROPPED reason=network_after_three_attempts");
      break;
  }
  return false;
}

void poll_manual_snapshot(const std::uint64_t now) {
  if (!api_ready || now - last_snapshot_poll_at < kSnapshotPollIntervalMilliseconds) {
    return;
  }
  last_snapshot_poll_at = now;
  foodlog::FoodLogHttpClient client(device_config);
  String request_id;
  if (!client.poll_snapshot_request(request_id)) {
    api_ready = false;
    return;
  }
  if (request_id.isEmpty()) {
    return;
  }
  foodlog::BoardMotionAnalysis manual_motion;
  manual_motion.valid = true;
  upload_capture(manual_motion, "", 0, request_id);
}

void analyze_motion(const std::uint64_t now) {
  if (!api_ready || now - last_motion_frame_at < kMotionFrameIntervalMilliseconds) {
    return;
  }
  last_motion_frame_at = now;
  camera_fb_t* frame = esp_camera_fb_get();
  if (frame == nullptr || frame->format != PIXFORMAT_JPEG) {
    if (frame != nullptr) {
      esp_camera_fb_return(frame);
    }
    return;
  }
  const foodlog::BoardMotionAnalysis analysis =
      motion_detector.analyze(frame->buf, frame->len);
  esp_camera_fb_return(frame);
  if (!analysis.valid) {
    return;
  }
  consecutive_motion_frames =
      analysis.detected ? consecutive_motion_frames + 1 : 0;
  const bool confirmed_motion =
      consecutive_motion_frames >= kRequiredMotionFrames;
  const auto instruction = motion_controller.observe(
      foodlog::MotionSample{now, confirmed_motion});
  if (!instruction.has_value()) {
    return;
  }
  const String burst_id = boot_sequence_id + "-burst-" +
                          String(instruction->burst_number);
  upload_capture(analysis, burst_id, instruction->burst_frame_index, "");
}

}  // namespace

void setup() {
  Serial.begin(kSerialBaud);
  delay(1'200);
  provisioner.begin();
  configured = config_store.load(device_config);
  camera_config_t camera_config = camera_configuration();
  camera_ready = esp_camera_init(&camera_config) == ESP_OK;
  boot_sequence_id = make_boot_sequence_id();
  Serial.printf(
      "FOODLOG_CAMERA_READY firmware=%s configured=%s camera=%s psram=%s\n",
      kFirmwareVersion, configured ? "true" : "false",
      camera_ready ? "ready" : "failed", psramFound() ? "ready" : "unavailable");
  if (configured) {
    begin_wifi();
  }
}

void loop() {
  bool restart_requested = false;
  provisioner.poll(device_config, restart_requested);
  if (restart_requested) {
    Serial.flush();
    delay(250);
    ESP.restart();
  }
  if (!configured || !camera_ready) {
    delay(10);
    return;
  }

  const std::uint64_t now = uptime_milliseconds();
  maintain_wifi(now);
  maintain_trusted_time_and_api(now);
  poll_manual_snapshot(now);
  analyze_motion(now);
  delay(1);
}
