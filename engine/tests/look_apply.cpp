// Development tool: applies an engine look to a planar YUV420 file (full range BT.601),
// to tune looks on any picture (tools/look_apply.py converts from and to JPEG/PNG).
//
//   look-apply in.yuv out.yuv WIDTH HEIGHT '{"lut": "...", "grain": 0.3, "halation": 0.5, ...}'
//
// The JSON takes the keys of the engine's "look" command (docs/protocol.md), but "preview".

#include "../src/look.hpp"

#include <cstdio>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

using namespace pycam;

int main(int argc, char *argv[])
{
	if (argc != 6)
	{
		std::cerr << "usage: look-apply in.yuv out.yuv WIDTH HEIGHT LOOK_JSON\n";
		return 2;
	}
	unsigned const width = std::stoul(argv[3]), height = std::stoul(argv[4]);
	std::ifstream in(argv[1], std::ios::binary);
	std::vector<uint8_t> frame((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
	if (frame.size() != size_t(width) * height * 3 / 2)
	{
		std::cerr << "look-apply: " << argv[1] << " is not a " << width << "x" << height << " YUV420 frame\n";
		return 1;
	}
	auto const params = nlohmann::json::parse(argv[5]);
	Look look;
	if (params.contains("lut") && !params["lut"].is_null())
		look.lut = Lut3D::Load(params["lut"].get<std::string>());
	look.vignette = params.value("vignette", 0.0f);
	look.grain = params.value("grain", 0.0f);
	look.grain_size = params.value("grain_size", 1.0f);
	look.grain_colour = params.value("grain_colour", 0.0f);
	look.grain_shadows = params.value("grain_shadows", 0.0f);
	look.grain_clumps = params.value("grain_clumps", 0.0f);
	look.halation = params.value("halation", 0.0f);
	look.halation_radius = params.value("halation_radius", 24.0f);
	look.halation_threshold = params.value("halation_threshold", 0.8f);
	if (params.contains("halation_colour"))
	{
		auto colour = params["halation_colour"].get<std::vector<float>>();
		for (unsigned i = 0; i < 3 && i < colour.size(); i++)
			look.halation_colour[i] = colour[i];
	}
	apply_look(look, frame.data(), width, height, width, 1, 3);
	std::ofstream(argv[2], std::ios::binary).write(reinterpret_cast<char const *>(frame.data()), frame.size());
	return 0;
}
