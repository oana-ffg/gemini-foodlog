#include "foodlog/board_motion_detector.hpp"

#include <esp_jpg_decode.h>

#include <algorithm>

namespace foodlog {
namespace {

constexpr int kChangedPixelThreshold = 16;

struct DecodeContext {
  const std::uint8_t* jpeg;
  std::size_t jpeg_length;
  std::uint8_t* grayscale;
  std::size_t output_width;
  std::size_t output_height;
};

std::size_t read_jpeg(void* argument, std::size_t index, std::uint8_t* buffer,
                      std::size_t length) {
  auto& context = *static_cast<DecodeContext*>(argument);
  if (index >= context.jpeg_length) {
    return 0;
  }
  const std::size_t available =
      std::min(length, context.jpeg_length - index);
  if (buffer != nullptr) {
    memcpy(buffer, context.jpeg + index, available);
  }
  return available;
}

bool write_decoded_block(void* argument, std::uint16_t x, std::uint16_t y,
                         std::uint16_t width, std::uint16_t height,
                         std::uint8_t* rgb) {
  auto& context = *static_cast<DecodeContext*>(argument);
  if (rgb == nullptr) {
    return true;
  }
  if (x + width > context.output_width || y + height > context.output_height) {
    return false;
  }
  for (std::size_t row = 0; row < height; ++row) {
    for (std::size_t column = 0; column < width; ++column) {
      const std::size_t source = (row * width + column) * 3;
      const std::uint16_t luminance =
          static_cast<std::uint16_t>(rgb[source]) * 77 +
          static_cast<std::uint16_t>(rgb[source + 1]) * 150 +
          static_cast<std::uint16_t>(rgb[source + 2]) * 29;
      context.grayscale[(y + row) * context.output_width + x + column] =
          static_cast<std::uint8_t>(luminance >> 8);
    }
  }
  return true;
}

}  // namespace

BoardMotionAnalysis BoardMotionDetector::analyze(const std::uint8_t* jpeg,
                                                 const std::size_t length) {
  BoardMotionAnalysis analysis;
  DecodeContext context{jpeg, length, current_, kWidth, kHeight};
  if (esp_jpg_decode(length, JPG_SCALE_8X, read_jpeg, write_decoded_block,
                     &context) != ESP_OK) {
    return analysis;
  }
  analysis.valid = true;
  if (!has_previous_) {
    memcpy(previous_, current_, sizeof(previous_));
    has_previous_ = true;
    return analysis;
  }

  std::int32_t signed_difference_sum = 0;
  for (std::size_t index = 0; index < kPixels; ++index) {
    signed_difference_sum +=
        static_cast<std::int16_t>(current_[index]) - previous_[index];
  }
  const std::int32_t global_brightness_shift =
      signed_difference_sum / static_cast<std::int32_t>(kPixels);

  std::uint32_t adjusted_difference_sum = 0;
  std::size_t changed_pixels = 0;
  for (std::size_t index = 0; index < kPixels; ++index) {
    const int difference = static_cast<int>(current_[index]) -
                           previous_[index] - global_brightness_shift;
    const int adjusted_difference = abs(difference);
    adjusted_difference_sum += adjusted_difference;
    if (adjusted_difference >= kChangedPixelThreshold) {
      ++changed_pixels;
    }
  }
  memcpy(previous_, current_, sizeof(previous_));
  analysis.score = static_cast<double>(adjusted_difference_sum) /
                   static_cast<double>(kPixels * 255);
  analysis.changed_pixel_ratio =
      static_cast<double>(changed_pixels) / static_cast<double>(kPixels);
  analysis.detected =
      analysis.score >= score_threshold() &&
      analysis.changed_pixel_ratio >= changed_pixel_ratio_threshold();
  return analysis;
}

}  // namespace foodlog
