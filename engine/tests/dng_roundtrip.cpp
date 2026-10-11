/*
 * Round trip test of the DNG writer: raw 16-bit samples (from a real frame) are packed
 * like the sensor sends them (12-bit CSI2P, BGGR), written with dng_write, compressed
 * or not. tests/dng_roundtrip.sh then decodes the DNGs with LibRaw, an independent
 * reader, and checks every sample.
 *
 * Usage: dng-roundtrip <raw16 file> <width> <height> <output prefix>
 */

#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <vector>

#include <libcamera/control_ids.h>
#include <libcamera/formats.h>

#include "../src/dng_writer.hpp"

int main(int argc, char *argv[])
{
	if (argc != 5)
	{
		std::cerr << "usage: dng-roundtrip <raw16 file> <width> <height> <output prefix>\n";
		return 2;
	}
	unsigned const width = std::atoi(argv[2]), height = std::atoi(argv[3]);
	std::vector<uint16_t> samples(size_t(width) * height);
	FILE *f = std::fopen(argv[1], "rb");
	if (!f || std::fread(samples.data(), 2, samples.size(), f) != samples.size())
	{
		std::cerr << "cannot read " << argv[1] << "\n";
		return 1;
	}
	std::fclose(f);

	// Pack as SBGGR12_CSI2P: two samples in three bytes, high bits first.
	StreamInfo info;
	info.width = width;
	info.height = height;
	info.stride = (width * 3 / 2 + 31) & ~31u;
	info.pixel_format = libcamera::formats::SBGGR12_CSI2P;
	std::vector<uint8_t> packed(size_t(info.stride) * height);
	for (unsigned y = 0; y < height; y++)
	{
		uint8_t *row = &packed[size_t(y) * info.stride];
		for (unsigned x = 0; x < width; x += 2)
		{
			uint16_t a = samples[size_t(y) * width + x], b = samples[size_t(y) * width + x + 1];
			row[x / 2 * 3] = a >> 4;
			row[x / 2 * 3 + 1] = b >> 4;
			row[x / 2 * 3 + 2] = (a & 15) | ((b & 15) << 4);
		}
	}

	libcamera::ControlList metadata;
	pycam::DngOptions options;
	options.exif = { "EXIF.LensModel=Test lens", "EXIF.FocalLength=1600/100" };
	for (bool compress : { false, true })
	{
		options.compress = compress;
		std::string name = std::string(argv[4]) + (compress ? "_lj92.dng" : "_plain.dng");
		pycam::dng_write({ packed.data(), packed.size() }, info, metadata, name, "imx477", options);
		std::cout << name << "\n";
	}
	return 0;
}
