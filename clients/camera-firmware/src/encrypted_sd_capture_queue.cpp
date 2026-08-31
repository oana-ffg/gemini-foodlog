#include "foodlog/encrypted_sd_capture_queue.hpp"

#include <FS.h>
#include <SD_MMC.h>
#include <esp_heap_caps.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <mbedtls/gcm.h>
#include <mbedtls/sha256.h>
#include <time.h>

#include <algorithm>
#include <cstring>

namespace foodlog {
namespace {

constexpr int kSdPinClock = 39;
constexpr int kSdPinCommand = 38;
constexpr int kSdPinData0 = 40;
constexpr char kQueueDirectory[] = "/foodlog-queue";
constexpr char kQueueDomain[] = "foodlog-encrypted-sd-queue-v1";
constexpr std::uint8_t kFileMagic[] = {'F', 'L', 'Q', '1'};
constexpr std::uint8_t kPayloadMagic[] = {'C', 'A', 'P', '1'};
constexpr std::size_t kHeaderLength = 40;
constexpr std::size_t kAuthenticatedHeaderLength = 24;
constexpr std::size_t kNonceLength = 12;
constexpr std::size_t kTagLength = 16;
constexpr std::size_t kMaximumJpegLength = 4 * 1024 * 1024;
constexpr std::size_t kMaximumMetadataLength = 2 * 1024;
constexpr std::size_t kMaximumIdempotencyKeyLength = 256;
constexpr std::size_t kMaximumSourceLength = 16;

void put_u32(std::uint8_t* target, const std::uint32_t value) {
  target[0] = static_cast<std::uint8_t>(value);
  target[1] = static_cast<std::uint8_t>(value >> 8);
  target[2] = static_cast<std::uint8_t>(value >> 16);
  target[3] = static_cast<std::uint8_t>(value >> 24);
}

std::uint32_t get_u32(const std::uint8_t* source) {
  return static_cast<std::uint32_t>(source[0]) |
         (static_cast<std::uint32_t>(source[1]) << 8) |
         (static_cast<std::uint32_t>(source[2]) << 16) |
         (static_cast<std::uint32_t>(source[3]) << 24);
}

std::uint8_t* allocate_buffer(const std::size_t length) {
  if (psramFound()) {
    auto* buffer = static_cast<std::uint8_t*>(
        heap_caps_malloc(length, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (buffer != nullptr) {
      return buffer;
    }
  }
  return static_cast<std::uint8_t*>(malloc(length));
}

bool ends_with(const String& value, const char* suffix) {
  return value.endsWith(suffix);
}

String normalized_path(const String& discovered) {
  if (discovered.startsWith("/")) {
    return discovered;
  }
  return String(kQueueDirectory) + "/" + discovered;
}

}  // namespace

StoredCapture::~StoredCapture() { reset(); }

void StoredCapture::reset() {
  if (jpeg != nullptr) {
    free(jpeg);
  }
  jpeg = nullptr;
  jpeg_length = 0;
  idempotency_key = "";
  metadata_json = "";
  source = "";
}

bool EncryptedSdCaptureQueue::begin(const DeviceConfig& config) {
  ready_ = false;
  paths_.clear();
  capacity_bytes_ = 0;
  if (!derive_key(config) ||
      !SD_MMC.setPins(kSdPinClock, kSdPinCommand, kSdPinData0) ||
      !SD_MMC.begin("/sdcard", true, false, SDMMC_FREQ_DEFAULT, 5) ||
      SD_MMC.cardType() == CARD_NONE) {
    Serial.println("QUEUE_STORAGE status=unavailable");
    return false;
  }
  capacity_bytes_ = SD_MMC.cardSize();
  if (!SD_MMC.exists(kQueueDirectory) && !SD_MMC.mkdir(kQueueDirectory)) {
    Serial.println("QUEUE_STORAGE status=failed reason=directory");
    return false;
  }
  if (!run_storage_self_test()) {
    Serial.println("QUEUE_STORAGE status=failed reason=encrypted_self_test");
    return false;
  }
  discover_committed_files();
  ready_ = true;
  Serial.printf("QUEUE_STORAGE status=ready capacity=%u queued=%u bytes=%llu\n",
                static_cast<unsigned int>(kPersistentCaptureCapacity),
                static_cast<unsigned int>(paths_.size()),
                static_cast<unsigned long long>(capacity_bytes_));
  return true;
}

bool EncryptedSdCaptureQueue::ready() const noexcept { return ready_; }

std::size_t EncryptedSdCaptureQueue::size() const noexcept {
  return paths_.size();
}

bool EncryptedSdCaptureQueue::contains_idempotency_key(
    const String& idempotency_key) const {
  if (!ready_ || idempotency_key.isEmpty()) {
    return false;
  }
  const String token = idempotency_token(idempotency_key);
  if (token.isEmpty()) {
    return false;
  }
  const String suffix = "-" + token + ".flq";
  return std::any_of(paths_.begin(), paths_.end(),
                     [&suffix](const String& path) {
                       return path.endsWith(suffix);
                     });
}

bool EncryptedSdCaptureQueue::derive_key(const DeviceConfig& config) {
  mbedtls_sha256_context context;
  mbedtls_sha256_init(&context);
  const bool ok =
      mbedtls_sha256_starts_ret(&context, 0) == 0 &&
      mbedtls_sha256_update_ret(
          &context, reinterpret_cast<const unsigned char*>(kQueueDomain),
          sizeof(kQueueDomain) - 1) == 0 &&
      mbedtls_sha256_update_ret(
          &context,
          reinterpret_cast<const unsigned char*>(config.camera_id.c_str()),
          config.camera_id.length()) == 0 &&
      mbedtls_sha256_update_ret(
          &context,
          reinterpret_cast<const unsigned char*>(config.credential.c_str()),
          config.credential.length()) == 0 &&
      mbedtls_sha256_finish_ret(&context, key_) == 0;
  mbedtls_sha256_free(&context);
  return ok;
}

String EncryptedSdCaptureQueue::idempotency_token(
    const String& idempotency_key) const {
  std::uint8_t digest[32] = {};
  mbedtls_sha256_context context;
  mbedtls_sha256_init(&context);
  const bool ok =
      mbedtls_sha256_starts_ret(&context, 0) == 0 &&
      mbedtls_sha256_update_ret(&context, key_, sizeof(key_)) == 0 &&
      mbedtls_sha256_update_ret(
          &context,
          reinterpret_cast<const unsigned char*>(idempotency_key.c_str()),
          idempotency_key.length()) == 0 &&
      mbedtls_sha256_finish_ret(&context, digest) == 0;
  mbedtls_sha256_free(&context);
  if (!ok) {
    return "";
  }
  char token[17] = {};
  for (std::size_t index = 0; index < 8; ++index) {
    snprintf(token + index * 2, 3, "%02x", digest[index]);
  }
  return String(token);
}

void EncryptedSdCaptureQueue::discover_committed_files() {
  File directory = SD_MMC.open(kQueueDirectory);
  if (!directory || !directory.isDirectory()) {
    return;
  }
  String discovered;
  while (!(discovered = directory.getNextFileName()).isEmpty()) {
    const String path = normalized_path(discovered);
    if (ends_with(path, ".tmp")) {
      SD_MMC.remove(path);
    } else if (ends_with(path, ".flq")) {
      paths_.push_back(path);
    }
  }
  directory.close();
  std::sort(paths_.begin(), paths_.end(),
            [](const String& left, const String& right) {
              return strcmp(left.c_str(), right.c_str()) < 0;
            });
  while (paths_.size() > kPersistentCaptureCapacity) {
    if (!discard_oldest("startup_capacity")) {
      break;
    }
  }
}

bool EncryptedSdCaptureQueue::enqueue(
    const std::uint8_t* jpeg, const std::size_t jpeg_length,
    const String& metadata_json, const String& idempotency_key,
    const String& source) {
  if (!ready_ || jpeg == nullptr || jpeg_length == 0 ||
      jpeg_length > kMaximumJpegLength || metadata_json.isEmpty() ||
      metadata_json.length() > kMaximumMetadataLength ||
      idempotency_key.isEmpty() ||
      idempotency_key.length() > kMaximumIdempotencyKeyLength ||
      source.isEmpty() || source.length() > kMaximumSourceLength) {
    return false;
  }
  if (contains_idempotency_key(idempotency_key)) {
    Serial.println("QUEUE_DUPLICATE status=already_committed");
    return true;
  }
  const std::size_t plaintext_length = 20 + idempotency_key.length() +
                                       metadata_json.length() + source.length() +
                                       jpeg_length;
  auto* plaintext = allocate_buffer(plaintext_length);
  if (plaintext == nullptr) {
    return false;
  }
  memcpy(plaintext, kPayloadMagic, sizeof(kPayloadMagic));
  put_u32(plaintext + 4, idempotency_key.length());
  put_u32(plaintext + 8, metadata_json.length());
  put_u32(plaintext + 12, source.length());
  put_u32(plaintext + 16, jpeg_length);
  std::size_t offset = 20;
  memcpy(plaintext + offset, idempotency_key.c_str(), idempotency_key.length());
  offset += idempotency_key.length();
  memcpy(plaintext + offset, metadata_json.c_str(), metadata_json.length());
  offset += metadata_json.length();
  memcpy(plaintext + offset, source.c_str(), source.length());
  offset += source.length();
  memcpy(plaintext + offset, jpeg, jpeg_length);

  const String token = idempotency_token(idempotency_key);
  if (token.isEmpty()) {
    free(plaintext);
    return false;
  }
  char leaf[80];
  snprintf(leaf, sizeof(leaf), "q-%010llu-%010llu-%08lx-%s.flq",
           static_cast<unsigned long long>(time(nullptr)),
           static_cast<unsigned long long>(esp_timer_get_time() / 1'000),
           static_cast<unsigned long>(esp_random()), token.c_str());
  const String committed_path = String(kQueueDirectory) + "/" + leaf;
  const String temporary_path = committed_path + ".tmp";
  const bool written = write_encrypted_file(temporary_path, committed_path,
                                            plaintext, plaintext_length);
  free(plaintext);
  if (!written) {
    return false;
  }
  while (paths_.size() >= kPersistentCaptureCapacity) {
    if (!discard_oldest("capacity")) {
      SD_MMC.remove(committed_path);
      return false;
    }
  }
  paths_.push_back(committed_path);
  std::sort(paths_.begin(), paths_.end(),
            [](const String& left, const String& right) {
              return strcmp(left.c_str(), right.c_str()) < 0;
            });
  return true;
}

bool EncryptedSdCaptureQueue::write_encrypted_file(
    const String& temporary_path, const String& committed_path,
    const std::uint8_t* plaintext, const std::size_t plaintext_length) {
  auto* ciphertext = allocate_buffer(plaintext_length);
  if (ciphertext == nullptr) {
    return false;
  }
  std::uint8_t header[kHeaderLength] = {};
  memcpy(header, kFileMagic, sizeof(kFileMagic));
  header[4] = 1;
  header[5] = 0;
  header[6] = static_cast<std::uint8_t>(kHeaderLength);
  header[7] = 0;
  put_u32(header + 8, plaintext_length);
  esp_fill_random(header + 12, kNonceLength);

  mbedtls_gcm_context context;
  mbedtls_gcm_init(&context);
  const bool encrypted =
      mbedtls_gcm_setkey(&context, MBEDTLS_CIPHER_ID_AES, key_, 256) == 0 &&
      mbedtls_gcm_crypt_and_tag(
          &context, MBEDTLS_GCM_ENCRYPT, plaintext_length, header + 12,
          kNonceLength, header, kAuthenticatedHeaderLength, plaintext,
          ciphertext, kTagLength, header + 24) == 0;
  mbedtls_gcm_free(&context);
  if (!encrypted) {
    free(ciphertext);
    return false;
  }

  if (SD_MMC.exists(temporary_path)) {
    SD_MMC.remove(temporary_path);
  }
  File file = SD_MMC.open(temporary_path, FILE_WRITE, true);
  const bool complete = file &&
                        file.write(header, sizeof(header)) == sizeof(header) &&
                        file.write(ciphertext, plaintext_length) ==
                            plaintext_length;
  if (file) {
    file.flush();
    file.close();
  }
  free(ciphertext);
  if (!complete || !SD_MMC.rename(temporary_path, committed_path)) {
    SD_MMC.remove(temporary_path);
    return false;
  }
  return true;
}

bool EncryptedSdCaptureQueue::read_encrypted_file(
    const String& path, std::uint8_t*& plaintext,
    std::size_t& plaintext_length) const {
  plaintext = nullptr;
  plaintext_length = 0;
  File file = SD_MMC.open(path, FILE_READ);
  std::uint8_t header[kHeaderLength] = {};
  if (!file || file.read(header, sizeof(header)) != sizeof(header) ||
      memcmp(header, kFileMagic, sizeof(kFileMagic)) != 0 || header[4] != 1 ||
      header[6] != kHeaderLength) {
    if (file) {
      file.close();
    }
    return false;
  }
  const std::size_t ciphertext_length = get_u32(header + 8);
  if (ciphertext_length < 20 ||
      ciphertext_length > kMaximumJpegLength + kMaximumMetadataLength +
                              kMaximumIdempotencyKeyLength +
                              kMaximumSourceLength + 20 ||
      file.size() != kHeaderLength + ciphertext_length) {
    file.close();
    return false;
  }
  auto* ciphertext = allocate_buffer(ciphertext_length);
  auto* candidate_plaintext = allocate_buffer(ciphertext_length);
  if (ciphertext == nullptr || candidate_plaintext == nullptr ||
      file.read(ciphertext, ciphertext_length) != ciphertext_length) {
    free(ciphertext);
    free(candidate_plaintext);
    file.close();
    return false;
  }
  file.close();

  mbedtls_gcm_context context;
  mbedtls_gcm_init(&context);
  const bool decrypted =
      mbedtls_gcm_setkey(&context, MBEDTLS_CIPHER_ID_AES, key_, 256) == 0 &&
      mbedtls_gcm_auth_decrypt(
          &context, ciphertext_length, header + 12, kNonceLength, header,
          kAuthenticatedHeaderLength, header + 24, kTagLength, ciphertext,
          candidate_plaintext) == 0;
  mbedtls_gcm_free(&context);
  free(ciphertext);
  if (!decrypted) {
    free(candidate_plaintext);
    return false;
  }
  plaintext = candidate_plaintext;
  plaintext_length = ciphertext_length;
  return true;
}

bool EncryptedSdCaptureQueue::run_storage_self_test() {
  constexpr std::uint8_t plaintext[] = {
      'F', 'o', 'o', 'd', 'L', 'o', 'g', '-', 'S', 'D', '-', 's', 'e', 'l', 'f',
      '-', 't', 'e', 's', 't'};
  const String temporary_path = String(kQueueDirectory) + "/.self-test.tmp";
  const String committed_path = String(kQueueDirectory) + "/.self-test.flq";
  if (SD_MMC.exists(temporary_path)) {
    SD_MMC.remove(temporary_path);
  }
  if (SD_MMC.exists(committed_path)) {
    SD_MMC.remove(committed_path);
  }
  if (!write_encrypted_file(temporary_path, committed_path, plaintext,
                            sizeof(plaintext))) {
    return false;
  }
  std::uint8_t* recovered = nullptr;
  std::size_t recovered_length = 0;
  const bool read =
      read_encrypted_file(committed_path, recovered, recovered_length);
  const bool matches = read && recovered_length == sizeof(plaintext) &&
                       memcmp(recovered, plaintext, sizeof(plaintext)) == 0;
  free(recovered);
  const bool removed = SD_MMC.remove(committed_path);
  return matches && removed;
}

bool EncryptedSdCaptureQueue::load_oldest(StoredCapture& capture) {
  capture.reset();
  while (!paths_.empty()) {
    std::uint8_t* plaintext = nullptr;
    std::size_t plaintext_length = 0;
    if (!read_encrypted_file(paths_.front(), plaintext, plaintext_length) ||
        plaintext_length < 20 ||
        memcmp(plaintext, kPayloadMagic, sizeof(kPayloadMagic)) != 0) {
      free(plaintext);
      if (!discard_oldest("authentication_or_format")) {
        return false;
      }
      continue;
    }
    const std::size_t idempotency_length = get_u32(plaintext + 4);
    const std::size_t metadata_length = get_u32(plaintext + 8);
    const std::size_t source_length = get_u32(plaintext + 12);
    const std::size_t jpeg_length = get_u32(plaintext + 16);
    const std::size_t expected_length =
        20 + idempotency_length + metadata_length + source_length + jpeg_length;
    if (idempotency_length == 0 ||
        idempotency_length > kMaximumIdempotencyKeyLength ||
        metadata_length == 0 || metadata_length > kMaximumMetadataLength ||
        source_length == 0 || source_length > kMaximumSourceLength ||
        jpeg_length == 0 || jpeg_length > kMaximumJpegLength ||
        expected_length != plaintext_length) {
      free(plaintext);
      if (!discard_oldest("payload_format")) {
        return false;
      }
      continue;
    }
    std::size_t offset = 20;
    capture.idempotency_key =
        String(reinterpret_cast<char*>(plaintext + offset), idempotency_length);
    offset += idempotency_length;
    capture.metadata_json =
        String(reinterpret_cast<char*>(plaintext + offset), metadata_length);
    offset += metadata_length;
    capture.source =
        String(reinterpret_cast<char*>(plaintext + offset), source_length);
    offset += source_length;
    memmove(plaintext, plaintext + offset, jpeg_length);
    capture.jpeg = plaintext;
    capture.jpeg_length = jpeg_length;
    return true;
  }
  return false;
}

bool EncryptedSdCaptureQueue::remove_oldest() {
  return discard_oldest("delivered");
}

bool EncryptedSdCaptureQueue::discard_oldest(const char* reason) {
  if (paths_.empty()) {
    return false;
  }
  const String path = paths_.front();
  if (!SD_MMC.remove(path)) {
    Serial.printf("QUEUE_REMOVE status=failed reason=%s\n", reason);
    return false;
  }
  paths_.erase(paths_.begin());
  if (strcmp(reason, "delivered") != 0) {
    Serial.printf("QUEUE_DROPPED reason=%s remaining=%u\n", reason,
                  static_cast<unsigned int>(paths_.size()));
  }
  return true;
}

}  // namespace foodlog
