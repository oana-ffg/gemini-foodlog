#pragma once

#include <cstdint>
#include <optional>
#include <vector>

namespace foodlog {

inline constexpr char kMotionAlgorithm[] = "physical-luma-delta-v1";

struct MotionDetectionConfig {
  double pixel_luma_delta_threshold = 0.12;
  double changed_pixel_ratio_threshold = 0.03;
};

struct MotionAnalysis {
  bool detected;
  double score;
  double changed_pixel_ratio;
  double threshold;
};

MotionAnalysis analyze_luma_motion(
    const std::vector<std::uint8_t>& previous_luma,
    const std::vector<std::uint8_t>& current_luma,
    MotionDetectionConfig config = {});

struct MotionConfig {
  std::uint64_t burst_duration_ms = 15'000;
  std::uint64_t burst_capture_interval_ms = 1'000;
  std::uint64_t active_capture_interval_ms = 60'000;
  std::uint64_t inactivity_timeout_ms = 300'000;
};

struct MotionSample {
  std::uint64_t now_ms;
  bool detected;
};

struct CaptureInstruction {
  bool in_motion_burst;
  std::uint64_t burst_number;
  std::uint32_t burst_frame_index;

  bool operator==(const CaptureInstruction& other) const {
    return in_motion_burst == other.in_motion_burst &&
           burst_number == other.burst_number &&
           burst_frame_index == other.burst_frame_index;
  }
};

class MotionController {
 public:
  explicit MotionController(MotionConfig config = {});

  std::optional<CaptureInstruction> observe(const MotionSample& sample);
  [[nodiscard]] bool activity_open() const noexcept;

 private:
  void begin_burst(std::uint64_t now_ms);
  void require_monotonic(std::uint64_t now_ms);

  MotionConfig config_;
  bool activity_open_ = false;
  bool have_observation_ = false;
  std::uint64_t last_observation_ms_ = 0;
  std::uint64_t last_motion_ms_ = 0;
  std::uint64_t burst_until_ms_ = 0;
  std::uint64_t next_burst_capture_ms_ = 0;
  std::uint64_t next_active_capture_ms_ = 0;
  std::uint64_t burst_number_ = 0;
  std::uint32_t burst_frame_index_ = 0;
};

[[nodiscard]] std::uint64_t delivery_retry_delay_ms(
    std::uint32_t attempt_count);

}  // namespace foodlog
