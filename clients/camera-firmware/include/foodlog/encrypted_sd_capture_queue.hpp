#pragma once

#include <Arduino.h>

#include <cstddef>
#include <cstdint>
#include <vector>

#include "foodlog/device_config.hpp"

namespace foodlog {

inline constexpr std::size_t kPersistentCaptureCapacity = 100;

struct StoredCapture {
  StoredCapture() = default;
  ~StoredCapture();
  StoredCapture(const StoredCapture&) = delete;
  StoredCapture& operator=(const StoredCapture&) = delete;

  void reset();

  String idempotency_key;
  String metadata_json;
  String source;
  std::uint8_t* jpeg = nullptr;
  std::size_t jpeg_length = 0;
};

// A bounded, application-encrypted, power-loss-safe queue on the onboard
// microSD card. Each capture is committed through write-flush-rename and is
// removed only after the backend acknowledges it or rejects the item
// permanently. When full, the oldest unsent capture is evicted so the card
// always retains the most recent kPersistentCaptureCapacity pictures.
class EncryptedSdCaptureQueue {
 public:
  [[nodiscard]] bool begin(const DeviceConfig& config);
  [[nodiscard]] bool ready() const noexcept;
  [[nodiscard]] std::size_t size() const noexcept;
  [[nodiscard]] bool contains_idempotency_key(
      const String& idempotency_key) const;

  [[nodiscard]] bool enqueue(const std::uint8_t* jpeg,
                             std::size_t jpeg_length,
                             const String& metadata_json,
                             const String& idempotency_key,
                             const String& source);
  [[nodiscard]] bool load_oldest(StoredCapture& capture);
  [[nodiscard]] bool remove_oldest();

 private:
  [[nodiscard]] bool derive_key(const DeviceConfig& config);
  [[nodiscard]] String idempotency_token(
      const String& idempotency_key) const;
  void discover_committed_files();
  [[nodiscard]] bool write_encrypted_file(
      const String& temporary_path, const String& committed_path,
      const std::uint8_t* plaintext, std::size_t plaintext_length);
  [[nodiscard]] bool read_encrypted_file(const String& path,
                                         std::uint8_t*& plaintext,
                                         std::size_t& plaintext_length) const;
  [[nodiscard]] bool run_storage_self_test();
  [[nodiscard]] bool discard_oldest(const char* reason);

  std::vector<String> paths_;
  std::uint8_t key_[32] = {};
  std::uint64_t capacity_bytes_ = 0;
  bool ready_ = false;
};

}  // namespace foodlog
