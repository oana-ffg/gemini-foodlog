#include "foodlog/capture_core.hpp"

#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using foodlog::CaptureInstruction;
using foodlog::MotionController;
using foodlog::MotionDetectionConfig;
using foodlog::MotionSample;

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

void test_delivery_retry_backoff() {
  expect(foodlog::delivery_retry_delay_ms(1) == 1'000,
         "first retry must wait one second");
  expect(foodlog::delivery_retry_delay_ms(6) == 32'000,
         "retry delay must grow exponentially");
  expect(foodlog::delivery_retry_delay_ms(7) == 60'000,
         "retry delay must cap at one minute");
  expect(foodlog::delivery_retry_delay_ms(1'000) == 60'000,
         "large attempt counts must remain capped");
  expect_invalid_argument(
      [] {
        const auto invalid_delay = foodlog::delivery_retry_delay_ms(0);
        static_cast<void>(invalid_delay);
      },
      "zero is not a valid retry attempt");
}

}  // namespace

int main() {
  test_luma_motion_analysis();
  test_motion_cadence_and_inactivity();
  test_motion_restarts_a_new_burst();
  test_delivery_retry_backoff();
  std::cout << "capture core tests passed\n";
  return 0;
}
