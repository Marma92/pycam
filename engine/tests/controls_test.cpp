// Checks the JSON control codec and remember_controls (meson test).

#include "../src/control_codec.hpp"

#include <cstdio>

#include <libcamera/control_ids.h>

using namespace pycam;
using nlohmann::json;
namespace controls = libcamera::controls;

namespace
{

int failures = 0;

void expect(bool ok, char const *what)
{
	if (!ok)
	{
		std::fprintf(stderr, "FAIL: %s\n", what);
		failures++;
	}
}

libcamera::ControlList remembered(std::initializer_list<json> commands)
{
	libcamera::ControlList kept;
	for (auto const &command : commands)
		remember_controls(kept, json_to_controls(command));
	return kept;
}

bool has(libcamera::ControlList const &list, libcamera::ControlId const &id)
{
	return list.contains(id.id());
}

} // namespace

int main()
{
	// Values and names.
	auto list = json_to_controls({ { "AwbMode", "Daylight" }, { "ColourGains", { 2.0, 1.5 } }, { "ExposureTime", 8000 } });
	expect(list.get(controls::AwbMode) == controls::AwbDaylight, "an enum by its short name");
	auto gains = list.get(controls::ColourGains);
	expect(gains && (*gains)[0] == 2.0f && (*gains)[1] == 1.5f, "an array of floats");
	expect(controls_to_json(list)["ExposureTime"] == 8000, "back to JSON");
	try
	{
		json_to_controls({ { "ColourGains", 2.0 } });
		expect(false, "a scalar for an array is refused");
	}
	catch (std::invalid_argument const &)
	{
	}

	// Memorised white balance, then automatic again: the gains are forgotten, otherwise
	// they come back at the next start of the camera and turn the automatic balance off.
	auto kept = remembered({ { { "AwbEnable", false }, { "ColourGains", { 2.0, 1.5 } } }, { { "AwbEnable", true } } });
	expect(!has(kept, controls::ColourGains), "AwbEnable on forgets ColourGains");
	expect(kept.get(controls::AwbEnable) == true, "AwbEnable kept");

	// A temperature, then gains, then a temperature: only the last manual balance stays.
	kept = remembered({ { { "AwbEnable", false }, { "ColourTemperature", 3200 } },
						{ { "AwbEnable", false }, { "ColourGains", { 2.0, 1.5 } } } });
	expect(has(kept, controls::ColourGains) && !has(kept, controls::ColourTemperature), "gains replace a temperature");
	kept = remembered({ { { "ColourGains", { 2.0, 1.5 } } }, { { "ColourTemperature", 5500 } } });
	expect(has(kept, controls::ColourTemperature) && !has(kept, controls::ColourGains), "a temperature replaces gains");

	// Automatic exposure again: the manual time and gain are forgotten; others stay.
	kept = remembered({ { { "ExposureTime", 133322 }, { "ExposureTimeMode", "Manual" }, { "AnalogueGain", 16.0 },
						  { "AnalogueGainMode", "Manual" }, { "Sharpness", 1.5 } },
						{ { "ExposureTimeMode", "Auto" } } });
	expect(!has(kept, controls::ExposureTime), "ExposureTimeMode Auto forgets ExposureTime");
	expect(has(kept, controls::AnalogueGain), "the gain stays manual");
	expect(kept.get(controls::Sharpness) == 1.5f, "unrelated controls stay");
	kept = remembered({ { { "AnalogueGain", 4.0 }, { "AnalogueGainMode", "Manual" } }, { { "AnalogueGainMode", "Auto" } } });
	expect(!has(kept, controls::AnalogueGain), "AnalogueGainMode Auto forgets AnalogueGain");

	// A value with its automatic mode in the same command (odd, but kept as asked).
	kept = remembered({ { { "AwbEnable", true }, { "ColourTemperature", 4000 } } });
	expect(has(kept, controls::ColourTemperature), "a value in the same command stays");

	if (failures)
		return 1;
	std::printf("controls: all checks passed\n");
	return 0;
}
