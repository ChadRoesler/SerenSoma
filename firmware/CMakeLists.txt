# The following five lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

# Keep compiler and linker arguments below Windows command-line limits.
if(CMAKE_GENERATOR MATCHES "^Ninja")
    set(CMAKE_NINJA_FORCE_RESPONSE_FILE TRUE CACHE INTERNAL "Use Ninja response files")
endif()

# The release workflow passes the tag's version (fw-v0.1.1 builds 0.1.1). Your own
# builds are 0.0.0-dev. This used to be 99.6.2, higher than anything upstream ships,
# because the check-in went to Xiaozhi's cloud and it installs any newer version it is
# offered. The check-in is seren-soma now (CONFIG_OTA_URL is blank by default), and
# seren-soma only offers firmware when asked to, so the real number can be used.
if(DEFINED ENV{SOMA_FW_VERSION} AND NOT "$ENV{SOMA_FW_VERSION}" STREQUAL "")
    set(PROJECT_VER "$ENV{SOMA_FW_VERSION}")
else()
    set(PROJECT_VER "0.0.0-dev")
endif()
add_definitions(-DFIRMWARE_VERSION=\"${PROJECT_VER}\")

# Add this line to disable the specific warning
add_compile_options(-Wno-missing-field-initializers)

# Auto-load a local sdkconfig overlay (git-ignored) when present. Lets
# self-hosted / custom deployments pin CONFIG_STACKCHAN_SERVER_URL,
# CONFIG_OTA_URL, etc. without touching committed defaults.
# Manual equivalent: SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.local" idf.py build
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/sdkconfig.defaults.local")
    set(SDKCONFIG_DEFAULTS
        "${CMAKE_CURRENT_SOURCE_DIR}/sdkconfig.defaults;${CMAKE_CURRENT_SOURCE_DIR}/sdkconfig.defaults.local"
        CACHE STRING "ESP-IDF sdkconfig default files (auto-extended with local overlay)" FORCE)
    message(STATUS "StackChan: detected sdkconfig.defaults.local, applying overlay")
endif()

# Tailnet (microlink) is opt-in and costs nothing when CONFIG_STACKCHAN_TAILNET
# is off. EXTRA_COMPONENT_DIRS must come BEFORE project.cmake for the components
# to be discoverable at all; the EXCLUDE_COMPONENTS gate must come AFTER
# project() so sdkconfig has been read.
#
# Only the top-level components/ dir is registered. microlink vendors a second,
# byte-identical copy of wireguard_lwip under components/microlink/components/,
# and adding both would fail on a duplicate component name. This matches
# microlink's own examples/basic_connect/CMakeLists.txt.
#
# Fetch it first:  python3 ./fetch_repos.py
set(MICROLINK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third-party/microlink/components")
# IDF 6 resolves a registered component's requirements even if it is excluded later,
# and microlink still asks for the removed 'json' component. So it is only registered
# when something actually turns the tailnet on; sdkconfig isn't parsed yet, so read it.
set(TAILNET_REQUESTED FALSE)
foreach(cfg "sdkconfig" "sdkconfig.defaults.local")
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/${cfg}")
        file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/${cfg}" tailnet_on REGEX "^CONFIG_STACKCHAN_TAILNET=y")
        if(tailnet_on)
            set(TAILNET_REQUESTED TRUE)
        endif()
    endif()
endforeach()
if(TAILNET_REQUESTED AND EXISTS "${MICROLINK_DIR}")
    list(APPEND EXTRA_COMPONENT_DIRS "${MICROLINK_DIR}")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(stack-chan)

if(NOT CONFIG_STACKCHAN_TAILNET)
    # Keep the default build byte-identical to a tree without microlink.
    idf_build_set_property(EXCLUDE_COMPONENTS "microlink;wireguard_lwip" APPEND)
    message(STATUS "StackChan: tailnet disabled, microlink excluded from the build")
else()
    if(NOT EXISTS "${MICROLINK_DIR}")
        message(FATAL_ERROR
            "CONFIG_STACKCHAN_TAILNET is enabled but third-party/microlink is missing. "
            "Run: python3 ./fetch_repos.py")
    endif()
    message(STATUS "StackChan: tailnet ENABLED (microlink)")
endif()
