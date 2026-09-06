// A test harness in forty lines.
//
// No GoogleTest, no Catch2. The firmware core has no dependencies at all --
// that is much of the point of it -- and pulling a test framework into the
// build to get `EXPECT_EQ` would be the largest dependency in the project by
// an order of magnitude.
//
// This gives named cases, a failure count, file and line on failure, and a
// non-zero exit status. That is what CI needs.

#pragma once

#include <cstdio>
#include <cstring>
#include <string>

namespace check {

inline int failures = 0;
inline int checks = 0;
// Failures at the point the current case started. Without this, `end()`
// compares against the running total and every case after the first failure
// reports FAILED, which hides where the problem actually is.
inline int failures_at_case_start = 0;

inline void begin(const char *name) {
    failures_at_case_start = failures;
    std::printf("  %-58s", name);
}

inline void end() {
    std::printf("%s\n", failures == failures_at_case_start ? "ok" : "FAILED");
}

inline void fail(const char *file, int line, const std::string &detail) {
    ++failures;
    std::printf("\n    %s:%d  %s\n", file, line, detail.c_str());
}

template <typename A, typename B>
void equal(const A &actual, const B &expected, const char *file, int line) {
    ++checks;
    if (!(actual == expected)) {
        fail(file, line, "values differ");
    }
}

inline void equal_str(const char *actual, const char *expected, const char *file, int line) {
    ++checks;
    if (std::strcmp(actual, expected) != 0) {
        fail(file, line,
             std::string("expected \"") + expected + "\", got \"" + actual + "\"");
    }
}

inline void equal_int(long long actual, long long expected, const char *file, int line) {
    ++checks;
    if (actual != expected) {
        fail(file, line, "expected " + std::to_string(expected) + ", got " +
                             std::to_string(actual));
    }
}

inline void is_true(bool value, const char *expression, const char *file, int line) {
    ++checks;
    if (!value) {
        fail(file, line, std::string("expected true: ") + expression);
    }
}

inline int report(const char *suite) {
    std::printf("\n%s: %d checks, %d failure%s\n", suite, checks, failures,
                failures == 1 ? "" : "s");
    return failures == 0 ? 0 : 1;
}

}  // namespace check

#define CASE(name)                                       \
    for (bool _once = (check::begin(name), true); _once; \
         _once = (check::end(), false))

#define CHECK_INT(actual, expected) check::equal_int((actual), (expected), __FILE__, __LINE__)
#define CHECK_STR(actual, expected) check::equal_str((actual), (expected), __FILE__, __LINE__)
#define CHECK(expression) check::is_true((expression), #expression, __FILE__, __LINE__)
