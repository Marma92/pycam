#pragma once

#include <string>

#include <libcamera/controls.h>
#include <nlohmann/json.hpp>

// Conversion between JSON and libcamera controls, addressed by their name ("ExposureTime").
// Enum controls also accept the enumerator name, with or without its prefix:
// {"AwbMode": "Daylight"} or {"AwbMode": "AwbDaylight"}.
namespace pycam
{

libcamera::ControlId const *find_control(std::string const &name);

// Throws std::invalid_argument on an unknown control or a value of the wrong shape.
libcamera::ControlValue json_to_control_value(libcamera::ControlId const &id, nlohmann::json const &value);
libcamera::ControlList json_to_controls(nlohmann::json const &object);

nlohmann::json control_value_to_json(libcamera::ControlValue const &value);
nlohmann::json controls_to_json(libcamera::ControlList const &list);

} // namespace pycam
