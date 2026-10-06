# Cremind Tag firmware (docs/tag-firmware.md). Built by tools/build.py with
# -DZEPHYR_EXTRA_MODULES=<repository>, which provides boards/, dts/ and lib/.
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(cremind_tag)

target_sources(app PRIVATE
  src/main.c
  src/tag_core.c
  src/gatt.c
  src/adv.c
  src/store_nvs.c
  src/enroll_uicr.c
  src/battery.c)

# One panel driver per image: the controller of the chosen cremind,panel node.
if(CONFIG_DT_HAS_CREMIND_SSD1619_ENABLED)
  target_sources(app PRIVATE src/panel_ssd1619.c)
else()
  target_sources(app PRIVATE src/panel_uc8176.c)
endif()

# Link-time optimisation (prj.conf CONFIG_LTO) for everything except:
# - the Zephyr Link Layer: the timing-critical controller is compiled exactly
#   as upstream builds it, and tools/verify_stack.py keeps finding its symbols;
# - ctag_session: LTO would inline the transcript (a 232-byte PSA hash
#   operation) and the HKDF chain into one frame of the handshake handler;
#   kept apart, the frames are those measured in docs/firmware-libs.md and
#   the system work queue budget holds (docs/tag-firmware.md "Stack budget").
if(CONFIG_LTO)
  foreach(lib subsys__bluetooth__controller subsys__bluetooth__controller__ll_sw__nordic
              ctag_session)
    if(TARGET ${lib})
      set_property(TARGET ${lib} PROPERTY LTO 0)
    endif()
  endforeach()
endif()
