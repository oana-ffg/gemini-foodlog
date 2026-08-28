#include "foodlog/capture_core.hpp"

#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using foodlog::CaptureInstruction;
using foodlog::DeliveryQueue;
using foodlog::DeliveryResult;
using foodlog::MotionController;
using foodlog::MotionDetectionConfig;
using foodlog::MotionSample;
using foodlog::QueueBlockReason;
using foodlog::QueueItem;
using foodlog::QueueSnapshot;
using foodlog::QueueSnapshotStore;

class MemoryStore final : public QueueSnapshotStore {
 public:
  QueueSnapshot load() const override { return snapshot; }

  void save(const QueueSnapshot& candidate) override {
    if (fail_next_save) {
      fail_next_save = false;
      throw std::runtime_error("synthetic persistence failure");
    }
    snapshot = candidate;
  }

  QueueSnapshot snapshot;
  bool fail_next_save = false;
};

[[noreturn]] void fail(const std::string& message) {
  std::cerr << "FAIL: " << message << '\n';
  std::exit(1);
}

void expect(const bool condition, const std::string& message) {
  if (!condition) {
    fail(message);
  }
}

template <typename Callable>
void expect_invalid_argument(Callable&& callable, const std::string& message) {
  try {
    callable();
  } catch (const std::invalid_argument&) {
    return;
  }
  fail(message);
}

QueueItem item(const std::string& id, const std::uint64_t captured_at_ms) {
  return QueueItem{id, "idempotency-" + id, captured_at_ms};
}

void test_luma_motion_analysis() {
  const std::vector<std::uint8_t> previous{0, 0, 0, 0};
  const std::vector<std::uint8_t> current{255, 0, 255, 0};
  const auto analysis = foodlog::analyze_luma_motion(
      previous, current, MotionDetectionConfig{0.1, 0.4});
  expect(analysis.detected, "half-frame change must cross a 40-percent threshold");
  expect(analysis.changed_pixel_ratio == 0.5,
         "changed-pixel ratio must be exact");
  expect(analysis.score == 0.5, "normalized motion score must be exact");
  expect(analysis.threshold == 0.4, "reported threshold must match configuration");
  expect_invalid_argument(
      [&previous] {
        foodlog::analyze_luma_motion(previous, std::vector<std::uint8_t>{0});
      },
      "mismatched motion frames must fail closed");
  expect_invalid_argument(
      [&previous] {
        foodlog::analyze_luma_motion(previous, previous,
                                     MotionDetectionConfig{1.1, 0.5});
      },
      "unnormalized motion threshold must fail closed");
}

void test_motion_cadence_and_inactivity() {
  MotionController controller;
  const auto first = controller.observe(MotionSample{1'000, true});
  expect(first == CaptureInstruction{true, 1, 0},
         "first motion must capture immediately");
  expect(!controller.observe(MotionSample{1'250, false}).has_value(),
         "burst must not exceed one capture per second");
  expect(controller.observe(MotionSample{2'000, false}) ==
             CaptureInstruction{true, 1, 1},
         "burst must capture at one-second cadence");
  expect(!controller.observe(MotionSample{16'001, false}).has_value(),
         "burst end must not create an extra frame");
  expect(controller.observe(MotionSample{76'000, false}) ==
             CaptureInstruction{false, 1, 0},
         "open activity must sample once per minute after the burst");
  expect(controller.observe(MotionSample{136'000, false}) ==
             CaptureInstruction{false, 1, 0},
         "active sampling must remain minute-bounded");
  expect(controller.observe(MotionSample{196'000, false}) ==
             CaptureInstruction{false, 1, 0},
         "active sampling must remain minute-bounded");
  expect(controller.observe(MotionSample{256'000, false}) ==
             CaptureInstruction{false, 1, 0},
         "active sampling must remain minute-bounded");
  expect(!controller.observe(MotionSample{300'999, false}).has_value(),
         "activity must remain open before the inactivity timeout");
  expect(controller.activity_open(), "activity must still be open");
  expect(!controller.observe(MotionSample{301'000, false}).has_value(),
         "inactivity boundary must close without capturing");
  expect(!controller.activity_open(), "activity must close after five minutes");
}

void test_motion_restarts_a_new_burst() {
  MotionController controller;
  controller.observe(MotionSample{10'000, true});
  controller.observe(MotionSample{26'000, false});
  const auto restarted = controller.observe(MotionSample{30'000, true});
  expect(restarted == CaptureInstruction{true, 2, 0},
         "new motion after burst quiet must start a new burst");
  expect_invalid_argument(
      [&controller] { controller.observe(MotionSample{29'999, false}); },
      "clock regression must fail closed");
}

void test_queue_fifo_retry_reboot_and_acknowledgement() {
  MemoryStore store;
  DeliveryQueue queue(store, 3);
  expect(queue.enqueue(item("a", 1)), "first item must enqueue");
  expect(queue.enqueue(item("a", 1)), "exact enqueue retry must be idempotent");
  expect(queue.snapshot().items.size() == 1,
         "exact enqueue retry must not duplicate work");
  expect(queue.enqueue(item("b", 2)), "second item must enqueue");
  expect(queue.next_ready(0)->id == "a", "oldest item must deliver first");
  queue.record_result("a", DeliveryResult::kTransientFailure, 10'000);
  expect_invalid_argument(
      [&queue] {
        queue.record_result("a", DeliveryResult::kTransientFailure, 10'999);
      },
      "delivery result before retry deadline must fail closed");
  expect(!queue.next_ready(10'999).has_value(), "retry must honor backoff");
  expect(queue.next_ready(11'000)->id == "a", "retry must preserve identity");
  expect(queue.next_ready(11'000)->idempotency_key == "idempotency-a",
         "retry must preserve idempotency key");

  DeliveryQueue rebooted(store, 3);
  expect(rebooted.next_ready(11'000)->id == "a",
         "reboot must recover the oldest item");
  rebooted.record_result("a", DeliveryResult::kAcknowledged, 11'000);
  expect(rebooted.next_ready(11'000)->id == "b",
         "acknowledgement must advance FIFO order");
}

void test_queue_capacity_and_permanent_failures_are_truthful() {
  MemoryStore store;
  DeliveryQueue queue(store, 2);
  queue.enqueue(item("a", 1));
  queue.enqueue(item("b", 2));
  expect(!queue.enqueue(item("c", 3)), "full queue must reject newest item");
  expect(queue.snapshot().capacity_drop_count == 1,
         "capacity loss must increment durable counter");

  queue.record_result("a", DeliveryResult::kPermanentAuthenticationFailure, 0);
  expect(queue.snapshot().block_reason == QueueBlockReason::kAuthentication,
         "authentication failure must block the queue");
  expect(!queue.next_ready(100'000).has_value(),
         "blocked queue must not continue delivery");
  expect(!queue.enqueue(item("d", 4)), "blocked queue must reject new captures");
  queue.resume_after_operator_action(100'000);
  expect(queue.next_ready(100'000)->id == "a",
         "authorized recovery must retry the same oldest item");

  queue.record_result("a", DeliveryResult::kPermanentItemFailure, 100'000);
  expect(queue.snapshot().permanent_item_failure_count == 1,
         "invalid item loss must increment durable counter");
  expect(queue.next_ready(100'000)->id == "b",
         "invalid item must not poison later valid work");

  MemoryStore quota_store;
  DeliveryQueue quota_queue(quota_store, 1);
  quota_queue.enqueue(item("quota", 1));
  quota_queue.record_result("quota", DeliveryResult::kPermanentQuotaFailure, 0);
  expect(quota_queue.snapshot().block_reason == QueueBlockReason::kQuota,
         "trial quota failure must block all delivery");
}

void test_failed_persistence_never_changes_live_state() {
  MemoryStore store;
  DeliveryQueue queue(store, 2);
  queue.enqueue(item("a", 1));
  const QueueSnapshot before = queue.snapshot();
  store.fail_next_save = true;
  try {
    queue.record_result("a", DeliveryResult::kAcknowledged, 0);
    fail("synthetic persistence failure must propagate");
  } catch (const std::runtime_error&) {
  }
  expect(queue.snapshot() == before,
         "failed persistence must not mutate acknowledged state");
  expect(store.snapshot == before,
         "failed persistence must preserve durable state");
}

void test_invalid_persisted_queue_fails_closed() {
  MemoryStore store;
  store.snapshot.items = {item("b", 2), item("a", 1)};
  expect_invalid_argument(
      [&store] { DeliveryQueue invalid(store, 3); },
      "out-of-order persisted queue must fail closed");
}

}  // namespace

int main() {
  test_luma_motion_analysis();
  test_motion_cadence_and_inactivity();
  test_motion_restarts_a_new_burst();
  test_queue_fifo_retry_reboot_and_acknowledgement();
  test_queue_capacity_and_permanent_failures_are_truthful();
  test_failed_persistence_never_changes_live_state();
  test_invalid_persisted_queue_fails_closed();
  std::cout << "capture core tests passed\n";
  return 0;
}
