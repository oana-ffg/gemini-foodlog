#include "foodlog/capture_core.hpp"

#include <algorithm>
#include <cstdlib>
#include <limits>
#include <stdexcept>

namespace foodlog {

namespace {

std::uint64_t saturating_add(const std::uint64_t left,
                             const std::uint64_t right) {
  if (right > std::numeric_limits<std::uint64_t>::max() - left) {
    return std::numeric_limits<std::uint64_t>::max();
  }
  return left + right;
}

}  // namespace

MotionAnalysis analyze_luma_motion(
    const std::vector<std::uint8_t>& previous_luma,
    const std::vector<std::uint8_t>& current_luma,
    const MotionDetectionConfig config) {
  if (previous_luma.empty() || previous_luma.size() != current_luma.size()) {
    throw std::invalid_argument(
        "motion frames must be equally sized and non-empty");
  }
  if (config.pixel_luma_delta_threshold < 0.0 ||
      config.pixel_luma_delta_threshold > 1.0 ||
      config.changed_pixel_ratio_threshold < 0.0 ||
      config.changed_pixel_ratio_threshold > 1.0) {
    throw std::invalid_argument("motion thresholds must be normalized");
  }

  std::size_t changed_pixels = 0;
  double total_delta = 0.0;
  for (std::size_t index = 0; index < current_luma.size(); ++index) {
    const int delta = std::abs(static_cast<int>(current_luma[index]) -
                               static_cast<int>(previous_luma[index]));
    const double normalized_delta = static_cast<double>(delta) / 255.0;
    total_delta += normalized_delta;
    if (normalized_delta >= config.pixel_luma_delta_threshold) {
      ++changed_pixels;
    }
  }
  const double pixel_count = static_cast<double>(current_luma.size());
  const double changed_pixel_ratio =
      static_cast<double>(changed_pixels) / pixel_count;
  return MotionAnalysis{
      changed_pixel_ratio >= config.changed_pixel_ratio_threshold,
      total_delta / pixel_count,
      changed_pixel_ratio,
      config.changed_pixel_ratio_threshold,
  };
}

MotionController::MotionController(MotionConfig config) : config_(config) {
  if (config_.burst_duration_ms == 0 ||
      config_.burst_capture_interval_ms == 0 ||
      config_.active_capture_interval_ms == 0 ||
      config_.inactivity_timeout_ms <= config_.burst_duration_ms) {
    throw std::invalid_argument("invalid motion configuration");
  }
}

std::optional<CaptureInstruction> MotionController::observe(
    const MotionSample& sample) {
  require_monotonic(sample.now_ms);

  if (sample.detected) {
    last_motion_ms_ = sample.now_ms;
    if (!activity_open_) {
      activity_open_ = true;
      begin_burst(sample.now_ms);
      return CaptureInstruction{true, burst_number_, burst_frame_index_++};
    }
    if (sample.now_ms > burst_until_ms_) {
      begin_burst(sample.now_ms);
      return CaptureInstruction{true, burst_number_, burst_frame_index_++};
    }
    burst_until_ms_ = saturating_add(sample.now_ms, config_.burst_duration_ms);
    next_active_capture_ms_ =
        saturating_add(burst_until_ms_, config_.active_capture_interval_ms);
  }

  if (!activity_open_) {
    return std::nullopt;
  }

  if (!sample.detected &&
      sample.now_ms >=
          saturating_add(last_motion_ms_, config_.inactivity_timeout_ms)) {
    activity_open_ = false;
    return std::nullopt;
  }

  if (sample.now_ms <= burst_until_ms_ &&
      sample.now_ms >= next_burst_capture_ms_) {
    next_burst_capture_ms_ =
        saturating_add(sample.now_ms, config_.burst_capture_interval_ms);
    return CaptureInstruction{true, burst_number_, burst_frame_index_++};
  }

  if (sample.now_ms > burst_until_ms_ &&
      sample.now_ms >= next_active_capture_ms_) {
    next_active_capture_ms_ =
        saturating_add(sample.now_ms, config_.active_capture_interval_ms);
    return CaptureInstruction{false, burst_number_, 0};
  }

  return std::nullopt;
}

bool MotionController::activity_open() const noexcept { return activity_open_; }

void MotionController::begin_burst(const std::uint64_t now_ms) {
  ++burst_number_;
  burst_frame_index_ = 0;
  burst_until_ms_ = saturating_add(now_ms, config_.burst_duration_ms);
  next_burst_capture_ms_ =
      saturating_add(now_ms, config_.burst_capture_interval_ms);
  next_active_capture_ms_ =
      saturating_add(burst_until_ms_, config_.active_capture_interval_ms);
}

void MotionController::require_monotonic(const std::uint64_t now_ms) {
  if (have_observation_ && now_ms < last_observation_ms_) {
    throw std::invalid_argument("motion observations must be monotonic");
  }
  have_observation_ = true;
  last_observation_ms_ = now_ms;
}

std::uint64_t delivery_retry_delay_ms(
    const std::uint32_t attempt_count) {
  if (attempt_count == 0) {
    throw std::invalid_argument("retry attempt count must be positive");
  }
  constexpr std::uint64_t kMaximumDelayMs = 60'000;
  if (attempt_count >= 7) {
    return kMaximumDelayMs;
  }
  return std::min(kMaximumDelayMs,
                  std::uint64_t{1'000} << (attempt_count - 1));
}

}  // namespace foodlog
