#pragma once

// Keep real Wi-Fi/server settings in the ignored project_network.local.h.
#if defined(__has_include)
#if __has_include("project_network.local.h")
#include "project_network.local.h"
#endif
#endif

#ifndef WIFI_SSID
#define WIFI_SSID "YOUR_WIFI_SSID"
#endif
#ifndef WIFI_PASS
#define WIFI_PASS "YOUR_WIFI_PASSWORD"
#endif
#ifndef PROJECT_SERVER_HOST
#define PROJECT_SERVER_HOST "192.168.1.152"
#endif
#ifndef PROJECT_SERVER_PORT
#define PROJECT_SERVER_PORT 8000
#endif

#define PROJECT_NETWORK_STRINGIFY_INNER(value) #value
#define PROJECT_NETWORK_STRINGIFY(value) PROJECT_NETWORK_STRINGIFY_INNER(value)
#define SERVER_BASE_URL "http://" PROJECT_SERVER_HOST ":" PROJECT_NETWORK_STRINGIFY(PROJECT_SERVER_PORT)
