# Zephyr module entry point (zephyr/module.yml: build.cmake = .).
# Applications do not include this file; Zephyr adds it for every build that
# lists this repository as a module (ZEPHYR_EXTRA_MODULES or a west workspace).

zephyr_include_directories(include)

if(EXISTS ${CMAKE_CURRENT_LIST_DIR}/lib/CMakeLists.txt)
  add_subdirectory(lib)
endif()
