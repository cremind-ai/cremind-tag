/* Minimal self-contained test runner: every test of suites.h, in order. */
#include <stdio.h>
#include <string.h>

#include "check.h"
#include "suites.h"

static unsigned int failures;

void check_fail(const char *file, int line, const char *expr, const char *name)
{
	failures++;
	printf("  %s:%d: CHECK(%s) failed%s%s\n", file, line, expr, name ? " for " : "",
	       name ? name : "");
}

struct test {
	const char *name;
	void (*fn)(void);
};

#define CTAG_TEST_ENTRY(name) {#name, name},

static const struct test tests[] = {CTAG_HOST_TESTS(CTAG_TEST_ENTRY)};

int main(int argc, char **argv)
{
	unsigned int failed = 0u;
	unsigned int run = 0u;
	size_t i;

	for (i = 0u; i < V_COUNT(tests); i++) {
		unsigned int before = failures;

		if (argc > 1 && strstr(tests[i].name, argv[1]) == NULL) {
			continue;
		}
		tests[i].fn();
		run++;
		if (failures != before) {
			failed++;
		}
		printf("%s %s\n", failures != before ? "FAIL" : "PASS", tests[i].name);
	}
	printf("%u tests, %u passed, %u failed\n", run, run - failed, failed);
	return failed != 0u || run == 0u;
}
