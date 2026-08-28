#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
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
    std::span<const std::uint8_t> previous_luma,
    std::span<const std::uint8_t> current_luma,
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

  bool operator==(const CaptureInstruction&) const = default;
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

enum class DeliveryResult {
  kAcknowledged,
  kTransientFailure,
  kPermanentAuthenticationFailure,
  kPermanentQuotaFailure,
  kPermanentItemFailure,
};

enum class QueueBlockReason {
  kAuthentication,
  kQuota,
};

struct QueueItem {
  std::string id;
  std::string idempotency_key;
  std::uint64_t captured_at_ms;
  std::uint32_t attempt_count = 0;
  std::uint64_t next_attempt_at_ms = 0;

  bool operator==(const QueueItem&) const = default;
};

struct QueueSnapshot {
  std::vector<QueueItem> items;
  std::uint64_t capacity_drop_count = 0;
  std::uint64_t permanent_item_failure_count = 0;
  std::optional<QueueBlockReason> block_reason;

  bool operator==(const QueueSnapshot&) const = default;
};

class QueueSnapshotStore {
 public:
  virtual ~QueueSnapshotStore() = default;
  [[nodiscard]] virtual QueueSnapshot load() const = 0;
  virtual void save(const QueueSnapshot& snapshot) = 0;
};

class DeliveryQueue {
 public:
  DeliveryQueue(QueueSnapshotStore& store, std::size_t capacity);

  bool enqueue(QueueItem item);
  [[nodiscard]] std::optional<QueueItem> next_ready(std::uint64_t now_ms) const;
  void record_result(const std::string& item_id, DeliveryResult result,
                     std::uint64_t now_ms);
  void resume_after_operator_action(std::uint64_t now_ms);

  [[nodiscard]] const QueueSnapshot& snapshot() const noexcept;

 private:
  static std::uint64_t retry_delay_ms(std::uint32_t attempt_count);
  void validate_snapshot(const QueueSnapshot& snapshot) const;
  void persist(QueueSnapshot candidate);

  QueueSnapshotStore& store_;
  std::size_t capacity_;
  QueueSnapshot snapshot_;
};

}  // namespace foodlog
