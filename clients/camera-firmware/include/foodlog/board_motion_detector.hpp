#pragma once

#include <Arduino.h>

namespace foodlog {

struct BoardMotionAnalysis {
  bool valid = false;
  bool detected = false;
  double score = 0.0;
  double changed_pixel_ratio = 0.0;
};

class BoardMotionDetector {
 public:
  static constexpr double score_threshold() { return 2.0 / 255.0; }
  static constexpr double changed_pixel_ratio_threshold() { return 0.025; }

  BoardMotionAnalysis analyze(const std::uint8_t* jpeg, std::size_t length);

 private:
  static constexpr std::size_t kWidth = 80;
  static constexpr std::size_t kHeight = 60;
  static constexpr std::size_t kPixels = kWidth * kHeight;

  std::uint8_t previous_[kPixels] = {};
  std::uint8_t current_[kPixels] = {};
  bool has_previous_ = false;
};

}  // namespace foodlog
