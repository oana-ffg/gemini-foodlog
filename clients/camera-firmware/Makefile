CXX ?= c++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -Werror -pedantic -O2

BUILD_DIR := .build
TEST_BINARY := $(BUILD_DIR)/capture_core_test

.PHONY: test clean

test: $(TEST_BINARY)
	$(TEST_BINARY)

$(TEST_BINARY): src/capture_core.cpp tests/capture_core_test.cpp include/foodlog/capture_core.hpp
	mkdir -p $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -Iinclude src/capture_core.cpp tests/capture_core_test.cpp -o $(TEST_BINARY)

clean:
	rm -f $(TEST_BINARY)
