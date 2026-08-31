#pragma once

#include <Arduino.h>

#include "foodlog/board_motion_detector.hpp"
#include "foodlog/device_config.hpp"

namespace foodlog {

enum class UploadResult {
  kAcknowledged,
  kTransientFailure,
  kAuthenticationFailure,
  kQuotaFailure,
  kPermanentFailure,
};

struct CaptureMetadata {
  String captured_at;
  String sequence_id;
  std::uint32_t sequence_number = 0;
  String burst_id;
  std::uint32_t burst_frame_index = 0;
  String snapshot_request_id;
  std::uint16_t width = 0;
  std::uint16_t height = 0;
  BoardMotionAnalysis motion;
};

class FoodLogHttpClient {
 public:
  explicit FoodLogHttpClient(const DeviceConfig& config) : config_(config) {}

  [[nodiscard]] bool check_device_status();
  [[nodiscard]] bool poll_snapshot_request(String& request_id);
  [[nodiscard]] UploadResult upload_jpeg(const std::uint8_t* jpeg,
                                         std::size_t jpeg_length,
                                         const CaptureMetadata& metadata,
                                         const String& idempotency_key,
                                         String& capture_id);

 private:
  [[nodiscard]] String metadata_json(const CaptureMetadata& metadata) const;

  const DeviceConfig& config_;
};

}  // namespace foodlog
