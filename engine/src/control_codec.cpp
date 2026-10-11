#include "control_codec.hpp"

#include <stdexcept>
#include <vector>

#include <libcamera/control_ids.h>
#include <libcamera/geometry.h>

using libcamera::ControlId;
using libcamera::ControlValue;
using nlohmann::json;

namespace pycam
{

namespace
{

template <typename T>
ControlValue scalar_or_array(ControlId const &id, json const &value)
{
	if (!id.isArray())
		return ControlValue(value.get<T>());
	if (!value.is_array())
		throw std::invalid_argument(id.name() + ": expected an array");
	std::vector<T> values = value.get<std::vector<T>>();
	if (id.size() != libcamera::dynamic_extent && values.size() != id.size())
		throw std::invalid_argument(id.name() + ": expected " + std::to_string(id.size()) + " values");
	return ControlValue(libcamera::Span<const T>(values.data(), values.size()));
}

ControlValue enum_value(ControlId const &id, std::string const &name)
{
	// Exact name first ("AwbDaylight"), then a unique suffix ("Daylight").
	int32_t suffix_value = 0;
	int suffix_matches = 0;
	for (auto const &[value, enumerator] : id.enumerators())
	{
		if (enumerator == name)
			return ControlValue(value);
		if (enumerator.size() > name.size() && enumerator.ends_with(name))
		{
			suffix_value = value;
			suffix_matches++;
		}
	}
	if (suffix_matches == 1)
		return ControlValue(suffix_value);
	throw std::invalid_argument(id.name() + ": unknown value \"" + name + "\"");
}

template <typename T>
json to_json(ControlValue const &value)
{
	if (!value.isArray())
		return value.get<T>();
	auto span = value.get<libcamera::Span<const T>>();
	return std::vector<T>(span.begin(), span.end());
}

json rectangle_to_json(libcamera::Rectangle const &r)
{
	return { r.x, r.y, r.width, r.height };
}

} // namespace

ControlId const *find_control(std::string const &name)
{
	for (auto const &[id, control] : libcamera::controls::controls)
		if (control->name() == name)
			return control;
	return nullptr;
}

ControlValue json_to_control_value(ControlId const &id, json const &value)
{
	try
	{
		switch (id.type())
		{
		case libcamera::ControlTypeBool:
			if (id.isArray())
				throw std::invalid_argument(id.name() + ": boolean arrays not supported");
			return ControlValue(value.get<bool>());
		case libcamera::ControlTypeInteger32:
			if (value.is_string() && !id.isArray())
				return enum_value(id, value.get<std::string>());
			return scalar_or_array<int32_t>(id, value);
		case libcamera::ControlTypeInteger64:
			return scalar_or_array<int64_t>(id, value);
		case libcamera::ControlTypeFloat:
			return scalar_or_array<float>(id, value);
		case libcamera::ControlTypeRectangle:
		{
			if (id.isArray() || !value.is_array() || value.size() != 4)
				throw std::invalid_argument(id.name() + ": expected [x, y, width, height]");
			return ControlValue(libcamera::Rectangle(value[0].get<int>(), value[1].get<int>(),
													 value[2].get<unsigned>(), value[3].get<unsigned>()));
		}
		default:
			throw std::invalid_argument(id.name() + ": control type not supported");
		}
	}
	catch (json::exception const &e)
	{
		throw std::invalid_argument(id.name() + ": bad value (" + e.what() + ")");
	}
}

libcamera::ControlList json_to_controls(json const &object)
{
	if (!object.is_object())
		throw std::invalid_argument("controls must be an object {name: value}");
	libcamera::ControlList list;
	for (auto const &[name, value] : object.items())
	{
		ControlId const *id = find_control(name);
		if (!id)
			throw std::invalid_argument("unknown control " + name);
		list.set(id->id(), json_to_control_value(*id, value));
	}
	return list;
}

json control_value_to_json(ControlValue const &value)
{
	switch (value.type())
	{
	case libcamera::ControlTypeNone:
		return nullptr;
	case libcamera::ControlTypeBool:
		if (value.isArray())
			return value.toString();
		return value.get<bool>();
	case libcamera::ControlTypeByte:
		return to_json<uint8_t>(value);
	case libcamera::ControlTypeInteger32:
		return to_json<int32_t>(value);
	case libcamera::ControlTypeInteger64:
		return to_json<int64_t>(value);
	case libcamera::ControlTypeFloat:
		return to_json<float>(value);
	case libcamera::ControlTypeString:
		return value.toString();
	case libcamera::ControlTypeRectangle:
	{
		if (!value.isArray())
			return rectangle_to_json(value.get<libcamera::Rectangle>());
		json out = json::array();
		for (auto const &r : value.get<libcamera::Span<const libcamera::Rectangle>>())
			out.push_back(rectangle_to_json(r));
		return out;
	}
	case libcamera::ControlTypeSize:
	{
		if (value.isArray())
			return value.toString();
		auto s = value.get<libcamera::Size>();
		return { s.width, s.height };
	}
	default:
		return value.toString();
	}
}

json controls_to_json(libcamera::ControlList const &list)
{
	json out = json::object();
	for (auto const &[id, value] : list)
	{
		auto it = libcamera::controls::controls.find(id);
		std::string name = it != libcamera::controls::controls.end() ? it->second->name() : "id_" + std::to_string(id);
		out[name] = control_value_to_json(value);
	}
	return out;
}

} // namespace pycam
