# SPDX-License-Identifier: MIT

# power@40000000, clock@40000000 and bprot@40000000 overlap by design on nRF52810.
list(APPEND EXTRA_DTC_FLAGS "-Wno-unique_unit_address_if_enabled")
