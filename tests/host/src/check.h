/*
 * Assertions shared by the host runner (runner.c) and the Zephyr smoke test
 * (tests/ztest/host_suites), which compiles some suites unchanged.
 */
#ifndef CTAG_TEST_CHECK_H_
#define CTAG_TEST_CHECK_H_

#include <stddef.h>

#define V_COUNT(a) (sizeof(a) / sizeof((a)[0]))

#ifdef __ZEPHYR__
#include <zephyr/ztest.h>

static inline const char *check_name(const char *name)
{
	return name != NULL ? name : "";
}

#define CHECK_CASE(cond, name) zassert_true(cond, "%s [%s]", #cond, check_name(name))
#else
void check_fail(const char *file, int line, const char *expr, const char *name);

/* Record the failure and leave the current function. */
#define CHECK_CASE(cond, name)                                                                     \
	do {                                                                                       \
		if (!(cond)) {                                                                     \
			check_fail(__FILE__, __LINE__, #cond, name);                               \
			return;                                                                    \
		}                                                                                  \
	} while (0)
#endif

#define CHECK(cond) CHECK_CASE(cond, NULL)

#endif /* CTAG_TEST_CHECK_H_ */
