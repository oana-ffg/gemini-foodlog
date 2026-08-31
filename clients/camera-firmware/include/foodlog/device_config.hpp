#pragma once

#include <Arduino.h>

namespace foodlog {

struct DeviceConfig {
  String wifi_ssid;
  String wifi_password;
  String camera_id;
  String credential;
};

class DeviceConfigStore {
 public:
  [[nodiscard]] bool load(DeviceConfig& config) const;
  [[nodiscard]] bool save(const DeviceConfig& config) const;
  [[nodiscard]] static bool validate(const DeviceConfig& config, String& reason);
};

class SerialProvisioner {
 public:
  void begin();
  void poll(DeviceConfig& active_config, bool& restart_requested);

 private:
  void process_line(const String& line, DeviceConfig& active_config,
                    bool& restart_requested);
  [[nodiscard]] bool set_field(const String& name, const String& encoded_value);

  DeviceConfig pending_config_;
  String input_line_;
  bool receiving_ = false;
};

}  // namespace foodlog
