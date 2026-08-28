#include "foodlog/capture_core.hpp"

#include <algorithm>
#include <cstdlib>
#include <limits>
#include <stdexcept>
#include <unordered_set>
#include <utility>

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
    const std::span<const std::uint8_t> previous_luma,
    const std::span<const std::uint8_t> current_luma,
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

DeliveryQueue::DeliveryQueue(QueueSnapshotStore& store,
                             const std::size_t capacity)
    : store_(store), capacity_(capacity), snapshot_(store.load()) {
  if (capacity_ == 0) {
    throw std::invalid_argument("queue capacity must be positive");
  }
  validate_snapshot(snapshot_);
}

bool DeliveryQueue::enqueue(QueueItem item) {
  if (item.id.empty() || item.idempotency_key.empty()) {
    throw std::invalid_argument("queue item identity must not be empty");
  }
  if (snapshot_.block_reason.has_value()) {
    return false;
  }
  const auto existing = std::find_if(
      snapshot_.items.begin(), snapshot_.items.end(),
      [&item](const QueueItem& candidate) {
        return candidate.id == item.id ||
               candidate.idempotency_key == item.idempotency_key;
      });
  if (existing != snapshot_.items.end()) {
    if (existing->id == item.id &&
        existing->idempotency_key == item.idempotency_key &&
        existing->captured_at_ms == item.captured_at_ms) {
      return true;
    }
    throw std::invalid_argument("queue item identity must be unique");
  }
  QueueSnapshot candidate = snapshot_;
  if (candidate.items.size() >= capacity_) {
    ++candidate.capacity_drop_count;
    persist(std::move(candidate));
    return false;
  }
  item.attempt_count = 0;
  item.next_attempt_at_ms = 0;
  candidate.items.push_back(std::move(item));
  persist(std::move(candidate));
  return true;
}

std::optional<QueueItem> DeliveryQueue::next_ready(
    const std::uint64_t now_ms) const {
  if (snapshot_.block_reason.has_value() || snapshot_.items.empty()) {
    return std::nullopt;
  }
  const QueueItem& oldest = snapshot_.items.front();
  if (oldest.next_attempt_at_ms > now_ms) {
    return std::nullopt;
  }
  return oldest;
}

void DeliveryQueue::record_result(const std::string& item_id,
                                  const DeliveryResult result,
                                  const std::uint64_t now_ms) {
  if (snapshot_.items.empty() || snapshot_.items.front().id != item_id) {
    throw std::invalid_argument("delivery result must target the oldest item");
  }
  QueueSnapshot candidate = snapshot_;
  QueueItem& item = candidate.items.front();
  if (item.next_attempt_at_ms > now_ms) {
    throw std::invalid_argument("delivery result precedes the retry deadline");
  }
  if (item.attempt_count == std::numeric_limits<std::uint32_t>::max()) {
    throw std::overflow_error("queue attempt counter exhausted");
  }
  ++item.attempt_count;

  switch (result) {
    case DeliveryResult::kAcknowledged:
      candidate.items.erase(candidate.items.begin());
      break;
    case DeliveryResult::kTransientFailure:
      item.next_attempt_at_ms =
          saturating_add(now_ms, retry_delay_ms(item.attempt_count));
      break;
    case DeliveryResult::kPermanentAuthenticationFailure:
      candidate.block_reason = QueueBlockReason::kAuthentication;
      break;
    case DeliveryResult::kPermanentQuotaFailure:
      candidate.block_reason = QueueBlockReason::kQuota;
      break;
    case DeliveryResult::kPermanentItemFailure:
      candidate.items.erase(candidate.items.begin());
      ++candidate.permanent_item_failure_count;
      break;
  }
  persist(std::move(candidate));
}

void DeliveryQueue::resume_after_operator_action(const std::uint64_t now_ms) {
  if (!snapshot_.block_reason.has_value()) {
    return;
  }
  QueueSnapshot candidate = snapshot_;
  candidate.block_reason.reset();
  if (!candidate.items.empty()) {
    candidate.items.front().next_attempt_at_ms = now_ms;
  }
  persist(std::move(candidate));
}

const QueueSnapshot& DeliveryQueue::snapshot() const noexcept { return snapshot_; }

std::uint64_t DeliveryQueue::retry_delay_ms(
    const std::uint32_t attempt_count) {
  constexpr std::uint64_t kMaximumDelayMs = 60'000;
  if (attempt_count >= 7) {
    return kMaximumDelayMs;
  }
  return std::min(kMaximumDelayMs,
                  std::uint64_t{1'000} << (attempt_count - 1));
}

void DeliveryQueue::validate_snapshot(const QueueSnapshot& snapshot) const {
  if (snapshot.items.size() > capacity_) {
    throw std::invalid_argument("persisted queue exceeds configured capacity");
  }
  std::unordered_set<std::string> ids;
  std::unordered_set<std::string> idempotency_keys;
  std::uint64_t previous_captured_at_ms = 0;
  bool first = true;
  for (const QueueItem& item : snapshot.items) {
    if (item.id.empty() || item.idempotency_key.empty() || !ids.insert(item.id).second ||
        !idempotency_keys.insert(item.idempotency_key).second) {
      throw std::invalid_argument("persisted queue identity is invalid");
    }
    if (!first && item.captured_at_ms < previous_captured_at_ms) {
      throw std::invalid_argument("persisted queue is not oldest-first");
    }
    first = false;
    previous_captured_at_ms = item.captured_at_ms;
  }
}

void DeliveryQueue::persist(QueueSnapshot candidate) {
  validate_snapshot(candidate);
  store_.save(candidate);
  snapshot_ = std::move(candidate);
}

}  // namespace foodlog
