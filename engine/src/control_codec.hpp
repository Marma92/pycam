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

// The controls set so far, `kept`, updated with a new `list`: what the camera is given
// again each time it starts. An automatic mode forgets the manual value it replaces
// (ExposureTime, AnalogueGain; ColourGains and ColourTemperature for AwbEnable), and one
// manual white balance forgets the other: kept, a stale value would come back at the
// next start, and a colour gain turns the automatic white balance off.
void remember_controls(libcamera::ControlList &kept, libcamera::ControlList const &list);

} // namespace pycam
