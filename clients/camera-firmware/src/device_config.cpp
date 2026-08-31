#include "foodlog/device_config.hpp"

#include <Preferences.h>
#include <WiFi.h>
#include <mbedtls/base64.h>

namespace foodlog {
namespace {

constexpr char kPreferencesNamespace[] = "foodlog-camera";
constexpr std::size_t kMaximumProvisioningLine = 512;

bool decode_base64(const String& encoded, String& decoded) {
  std::size_t required_length = 0;
  const int sizing_result = mbedtls_base64_decode(
      nullptr, 0, &required_length,
      reinterpret_cast<const unsigned char*>(encoded.c_str()), encoded.length());
  if (sizing_result != MBEDTLS_ERR_BASE64_BUFFER_TOO_SMALL ||
      required_length > 384) {
    return false;
  }
  if (required_length == 0) {
    decoded = "";
    return true;
  }

  auto* buffer = static_cast<unsigned char*>(malloc(required_length + 1));
  if (buffer == nullptr) {
    return false;
  }
  std::size_t decoded_length = 0;
  const int result = mbedtls_base64_decode(
      buffer, required_length, &decoded_length,
      reinterpret_cast<const unsigned char*>(encoded.c_str()), encoded.length());
  if (result != 0) {
    free(buffer);
    return false;
  }
  buffer[decoded_length] = '\0';
  decoded = String(reinterpret_cast<char*>(buffer), decoded_length);
  free(buffer);
  return true;
}

bool is_camera_id(const String& value) {
  if (value.length() != 36 || value[8] != '-' || value[13] != '-' ||
      value[18] != '-' || value[23] != '-') {
    return false;
  }
  for (std::size_t index = 0; index < value.length(); ++index) {
    if (index == 8 || index == 13 || index == 18 || index == 23) {
      continue;
    }
    if (!isxdigit(static_cast<unsigned char>(value[index]))) {
      return false;
    }
  }
  return true;
}

const char* wifi_status_name(const wl_status_t status) {
  switch (status) {
    case WL_IDLE_STATUS:
      return "idle";
    case WL_NO_SSID_AVAIL:
      return "ssid_unavailable";
    case WL_SCAN_COMPLETED:
      return "scan_completed";
    case WL_CONNECTED:
      return "connected";
    case WL_CONNECT_FAILED:
      return "authentication_failed";
    case WL_CONNECTION_LOST:
      return "connection_lost";
    case WL_DISCONNECTED:
      return "disconnected";
    default:
      return "unknown";
  }
}

}  // namespace

bool DeviceConfigStore::load(DeviceConfig& config) const {
  Preferences preferences;
  if (!preferences.begin(kPreferencesNamespace, true)) {
    return false;
  }
  config.wifi_ssid = preferences.getString("wifi_ssid");
  config.wifi_password = preferences.getString("wifi_pass");
  config.camera_id = preferences.getString("camera_id");
  config.credential = preferences.getString("credential");
  preferences.end();

  String reason;
  return validate(config, reason);
}

bool DeviceConfigStore::save(const DeviceConfig& config) const {
  String reason;
  if (!validate(config, reason)) {
    return false;
  }
  Preferences preferences;
  if (!preferences.begin(kPreferencesNamespace, false)) {
    return false;
  }
  const bool saved =
      preferences.putString("wifi_ssid", config.wifi_ssid) ==
          config.wifi_ssid.length() &&
      preferences.putString("wifi_pass", config.wifi_password) ==
          config.wifi_password.length() &&
      preferences.putString("camera_id", config.camera_id) ==
          config.camera_id.length() &&
      preferences.putString("credential", config.credential) ==
          config.credential.length();
  preferences.end();
  return saved;
}

bool DeviceConfigStore::validate(const DeviceConfig& config, String& reason) {
  if (config.wifi_ssid.isEmpty() || config.wifi_ssid.length() > 32) {
    reason = "invalid_wifi_ssid";
  } else if (config.wifi_password.length() > 63) {
    reason = "invalid_wifi_password";
  } else if (!is_camera_id(config.camera_id)) {
    reason = "invalid_camera_id";
  } else if (!config.credential.startsWith("flc_v1_") ||
             config.credential.length() > 256) {
    reason = "invalid_camera_credential";
  } else {
    reason = "ok";
    return true;
  }
  return false;
}

void SerialProvisioner::begin() {
  input_line_.reserve(kMaximumProvisioningLine);
  Serial.println("FOODLOG_PROVISIONING_READY version=1");
}

void SerialProvisioner::poll(DeviceConfig& active_config,
                             bool& restart_requested) {
  while (Serial.available() > 0) {
    const char input = static_cast<char>(Serial.read());
    if (input == '\r' || input == '\n') {
      if (!input_line_.isEmpty()) {
        process_line(input_line_, active_config, restart_requested);
        input_line_ = "";
      }
      continue;
    }
    if (input_line_.length() >= kMaximumProvisioningLine) {
      input_line_ = "";
      receiving_ = false;
      Serial.println("PROVISION_ERROR line_too_long");
      continue;
    }
    input_line_ += input;
  }
}

void SerialProvisioner::process_line(const String& line,
                                     DeviceConfig& active_config,
                                     bool& restart_requested) {
  if (line == "STATUS") {
    String reason;
    Serial.printf(
        "STATUS configured=%s wifi=%s camera_id=%s\n",
        DeviceConfigStore::validate(active_config, reason) ? "true" : "false",
        wifi_status_name(WiFi.status()),
        active_config.camera_id.isEmpty() ? "unavailable"
                                           : active_config.camera_id.c_str());
    return;
  }
  if (line == "PROVISION_BEGIN") {
    pending_config_ = DeviceConfig{};
    receiving_ = true;
    Serial.println("PROVISION_ACCEPTED");
    return;
  }
  if (line == "PROVISION_COMMIT") {
    if (!receiving_) {
      Serial.println("PROVISION_ERROR begin_required");
      return;
    }
    String reason;
    if (!DeviceConfigStore::validate(pending_config_, reason)) {
      Serial.printf("PROVISION_ERROR %s\n", reason.c_str());
      return;
    }
    DeviceConfigStore store;
    if (!store.save(pending_config_)) {
      Serial.println("PROVISION_ERROR nvs_write_failed");
      return;
    }
    active_config = pending_config_;
    receiving_ = false;
    restart_requested = true;
    Serial.printf("PROVISION_OK camera_id=%s\n",
                  active_config.camera_id.c_str());
    return;
  }
  if (!receiving_) {
    Serial.println("PROVISION_ERROR begin_required");
    return;
  }

  const int separator = line.indexOf(' ');
  if (separator <= 0 ||
      !set_field(line.substring(0, separator), line.substring(separator + 1))) {
    Serial.println("PROVISION_ERROR invalid_field");
    return;
  }
  Serial.printf("PROVISION_FIELD_OK name=%s\n",
                line.substring(0, separator).c_str());
}

bool SerialProvisioner::set_field(const String& name,
                                  const String& encoded_value) {
  String decoded;
  if (!decode_base64(encoded_value, decoded)) {
    return false;
  }
  if (name == "WIFI_SSID") {
    pending_config_.wifi_ssid = decoded;
  } else if (name == "WIFI_PASSWORD") {
    pending_config_.wifi_password = decoded;
  } else if (name == "CAMERA_ID") {
    pending_config_.camera_id = decoded;
  } else if (name == "CAMERA_CREDENTIAL") {
    pending_config_.credential = decoded;
  } else {
    return false;
  }
  return true;
}

}  // namespace foodlog
