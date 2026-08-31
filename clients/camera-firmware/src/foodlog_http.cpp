#include "foodlog/foodlog_http.hpp"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_heap_caps.h>

namespace foodlog {
namespace {

constexpr char kApiHost[] = "foodlog-api-sptvo5nsga-ew.a.run.app";
constexpr char kFirmwareVersion[] = "foodlog-fnk0085-0.2.0";
constexpr char kBoundary[] = "----foodlog-fnk0085-capture-v1";
constexpr std::uint32_t kHttpTimeoutMilliseconds = 30'000;

extern const std::uint8_t google_roots_start[]
    asm("_binary_data_cert_google_trust_services_roots_pem_start");
extern const std::uint8_t google_roots_end[]
    asm("_binary_data_cert_google_trust_services_roots_pem_end");

const char* google_root_ca_pem() {
  static const String roots(
      reinterpret_cast<const char*>(google_roots_start),
      static_cast<unsigned int>(google_roots_end - google_roots_start));
  return roots.c_str();
}

String json_string(const String& value) {
  String encoded;
  encoded.reserve(value.length() + 2);
  encoded += '"';
  for (std::size_t index = 0; index < value.length(); ++index) {
    const char character = value[index];
    if (character == '"' || character == '\\') {
      encoded += '\\';
    }
    encoded += character;
  }
  encoded += '"';
  return encoded;
}

String json_string_value(const String& json, const char* key) {
  const String marker = String('"') + key + "\":";
  int value_start = json.indexOf(marker);
  if (value_start < 0) {
    return "";
  }
  value_start += marker.length();
  while (value_start < static_cast<int>(json.length()) &&
         isspace(static_cast<unsigned char>(json[value_start]))) {
    ++value_start;
  }
  if (json.startsWith("null", value_start) || json[value_start] != '"') {
    return "";
  }
  ++value_start;
  const int value_end = json.indexOf('"', value_start);
  return value_end < 0 ? String() : json.substring(value_start, value_end);
}

void configure_http(HTTPClient& http) {
  http.setConnectTimeout(kHttpTimeoutMilliseconds);
  http.setTimeout(kHttpTimeoutMilliseconds);
}

bool connect_https(WiFiClientSecure& client) {
  IPAddress api_address;
  if (WiFi.hostByName(kApiHost, api_address) != 1) {
    Serial.println("HTTPS_CONNECT status=failed stage=dns");
    return false;
  }
  client.setTimeout(30);
  client.setHandshakeTimeout(15);
  if (client.connect(api_address, 443, kApiHost, google_root_ca_pem(), nullptr,
                     nullptr)) {
    return true;
  }
  char tls_error[96] = {};
  const int tls_error_code = client.lastError(tls_error, sizeof(tls_error));
  Serial.printf("HTTPS_CONNECT status=failed stage=tls code=%d detail=%s\n",
                tls_error_code,
                tls_error_code == 0 ? "unavailable" : tls_error);
  return false;
}

std::uint8_t* allocate_payload(const std::size_t length) {
  if (psramFound()) {
    auto* payload = static_cast<std::uint8_t*>(
        heap_caps_malloc(length, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (payload != nullptr) {
      return payload;
    }
  }
  return static_cast<std::uint8_t*>(malloc(length));
}

}  // namespace

DeviceStatusResult FoodLogHttpClient::check_device_status() {
  WiFiClientSecure client;
  HTTPClient http;
  if (!connect_https(client)) {
    return DeviceStatusResult::kTransportFailure;
  }
  configure_http(http);
  if (!http.begin(client, kApiHost, 443, "/v1/device/status", true)) {
    return DeviceStatusResult::kTransportFailure;
  }
  http.addHeader("Authorization", "FoodLogCamera " + config_.credential);
  const int status_code = http.GET();
  Serial.printf("DEVICE_STATUS_HTTP code=%d\n", status_code);
  http.end();
  if (status_code == HTTP_CODE_OK) {
    return DeviceStatusResult::kReady;
  }
  if (status_code == HTTP_CODE_UNAUTHORIZED ||
      status_code == HTTP_CODE_FORBIDDEN) {
    return DeviceStatusResult::kAuthenticationFailure;
  }
  if (status_code <= 0 || status_code == HTTP_CODE_REQUEST_TIMEOUT ||
      status_code >= 500) {
    return DeviceStatusResult::kTransportFailure;
  }
  return DeviceStatusResult::kPermanentFailure;
}

bool FoodLogHttpClient::poll_snapshot_request(String& request_id) {
  request_id = "";
  WiFiClientSecure client;
  HTTPClient http;
  if (!connect_https(client)) {
    return false;
  }
  configure_http(http);
  if (!http.begin(client, kApiHost, 443, "/v1/device/snapshot-request", true)) {
    return false;
  }
  http.addHeader("Authorization", "FoodLogCamera " + config_.credential);
  const int status_code = http.GET();
  if (status_code == HTTP_CODE_OK) {
    request_id = json_string_value(http.getString(), "request_id");
  }
  http.end();
  // Firmware may be installed shortly before the matching backend release.
  // A missing optional command route must not block ordinary motion capture.
  return status_code == HTTP_CODE_OK || status_code == HTTP_CODE_NOT_FOUND;
}

UploadResult FoodLogHttpClient::upload_jpeg_json(
    const std::uint8_t* jpeg, const std::size_t jpeg_length,
    const String& metadata_body, const String& idempotency_key,
    String& capture_id) {
  capture_id = "";
  const String prefix =
      String("--") + kBoundary +
      "\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
      "Content-Type: application/json\r\n\r\n" +
      metadata_body + "\r\n--" + kBoundary +
      "\r\nContent-Disposition: form-data; name=\"image\"; "
      "filename=\"capture.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n";
  const String suffix = String("\r\n--") + kBoundary + "--\r\n";
  const std::size_t payload_length =
      prefix.length() + jpeg_length + suffix.length();
  std::uint8_t* payload = allocate_payload(payload_length);
  if (payload == nullptr) {
    return UploadResult::kTransientFailure;
  }
  std::size_t offset = 0;
  memcpy(payload + offset, prefix.c_str(), prefix.length());
  offset += prefix.length();
  memcpy(payload + offset, jpeg, jpeg_length);
  offset += jpeg_length;
  memcpy(payload + offset, suffix.c_str(), suffix.length());

  WiFiClientSecure client;
  HTTPClient http;
  if (!connect_https(client)) {
    free(payload);
    return UploadResult::kTransientFailure;
  }
  configure_http(http);
  if (!http.begin(client, kApiHost, 443, "/v1/captures", true)) {
    free(payload);
    return UploadResult::kTransientFailure;
  }
  http.addHeader("Authorization", "FoodLogCamera " + config_.credential);
  http.addHeader("Idempotency-Key", idempotency_key);
  http.addHeader("Content-Type", String("multipart/form-data; boundary=") + kBoundary);
  const int status_code = http.POST(payload, payload_length);
  free(payload);

  String response_body;
  if (status_code > 0) {
    response_body = http.getString();
  }
  http.end();
  if (status_code == HTTP_CODE_ACCEPTED) {
    capture_id = json_string_value(response_body, "capture_id");
    return capture_id.isEmpty() ? UploadResult::kTransientFailure
                                : UploadResult::kAcknowledged;
  }
  if (status_code == HTTP_CODE_UNAUTHORIZED || status_code == HTTP_CODE_FORBIDDEN) {
    return UploadResult::kAuthenticationFailure;
  }
  if (status_code == HTTP_CODE_TOO_MANY_REQUESTS) {
    return UploadResult::kQuotaFailure;
  }
  if (status_code <= 0 || status_code == HTTP_CODE_REQUEST_TIMEOUT ||
      status_code >= 500) {
    return UploadResult::kTransientFailure;
  }
  return UploadResult::kPermanentFailure;
}

String FoodLogHttpClient::metadata_json(const CaptureMetadata& metadata) const {
  String json;
  json.reserve(720);
  json = "{\"schema_version\":1,\"camera_id\":" +
         json_string(config_.camera_id) + ",\"captured_at\":" +
         json_string(metadata.captured_at) +
         ",\"client_kind\":\"physical\",\"client_version\":" +
         json_string(kFirmwareVersion) + ",\"sequence_id\":" +
         json_string(metadata.sequence_id) + ",\"sequence_number\":" +
         String(metadata.sequence_number);
  if (!metadata.burst_id.isEmpty()) {
    json += ",\"burst_id\":" + json_string(metadata.burst_id) +
            ",\"burst_frame_index\":" +
            String(metadata.burst_frame_index);
  }
  if (!metadata.snapshot_request_id.isEmpty()) {
    json += ",\"snapshot_request_id\":" +
            json_string(metadata.snapshot_request_id);
  }
  json += ",\"width\":" + String(metadata.width) +
          ",\"height\":" + String(metadata.height) +
          ",\"motion\":{\"detected\":" +
          String(metadata.motion.detected ? "true" : "false") +
          ",\"algorithm\":\"physical-luma-delta-v1\",\"score\":" +
          String(metadata.motion.score, 6) +
          ",\"changed_pixel_ratio\":" +
          String(metadata.motion.changed_pixel_ratio, 6) +
          ",\"threshold\":" +
          String(BoardMotionDetector::changed_pixel_ratio_threshold(), 6) +
          "}}";
  return json;
}

}  // namespace foodlog
